package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"tailscale.com/tsnet"
)

// loginURLLogf discards tsnet's chatty logs but surfaces the interactive
// auth URL so a node can be approved without a pre-provisioned key.
func loginURLLogf(format string, args ...any) {
	msg := fmt.Sprintf(format, args...)
	if strings.Contains(msg, "https://login.tailscale.com") {
		fmt.Fprintln(os.Stderr, strings.TrimSpace(msg))
	}
}

// voicetest measures whether the embedded tailnet can carry voice media.
// LiveKit voice is ~50 frames/sec of small RTP packets; this harness sends
// synthetic frames at that cadence over TCP (the ICE-TCP option) or UDP
// (the in-app relay option) through tsnet and reports RTT/jitter/loss.

const voiceTestPort = 19000

func runVoiceTest(args []string) error {
	if len(args) < 1 {
		return fmt.Errorf("usage: voicetest <serve|client> [flags]")
	}
	switch args[0] {
	case "serve":
		return voiceTestServe(args[1:])
	case "client":
		return voiceTestClient(args[1:])
	}
	return fmt.Errorf("unknown voicetest role %q (expected \"serve\" or \"client\")", args[0])
}

func voiceTestServe(args []string) error {
	fs := flag.NewFlagSet("voicetest serve", flag.ContinueOnError)
	hostname := fs.String("hostname", "openbase-voice-echo", "tailnet hostname for the echo node")
	stateDir := fs.String("statedir", defaultStateDir("tsnet-voice"), "directory for node state")
	authKey := fs.String("authkey", os.Getenv("TS_AUTHKEY"), "Tailscale auth key")
	controlURL := fs.String("control-url", os.Getenv("OPENBASE_TSNET_CONTROL_URL"), "coordination server URL")
	direct := fs.String("direct", "", "skip tsnet; echo on this local addr instead (baseline mode)")
	if err := fs.Parse(args); err != nil {
		return err
	}

	if *direct != "" {
		tcpLn, err := net.Listen("tcp", *direct)
		if err != nil {
			return err
		}
		pc, err := net.ListenPacket("udp", *direct)
		if err != nil {
			return err
		}
		go echoAccept(tcpLn)
		go echoPacket(pc)
		fmt.Printf("baseline echo up on %s (tcp+udp)\n", *direct)
		select {}
	}

	srv := &tsnet.Server{
		Hostname:   *hostname,
		Dir:        *stateDir,
		AuthKey:    *authKey,
		ControlURL: *controlURL,
		Ephemeral:  true,
		Logf:       loginURLLogf,
	}
	defer srv.Close()
	if err := os.MkdirAll(*stateDir, 0o700); err != nil {
		return err
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	status, err := srv.Up(ctx)
	cancel()
	if err != nil {
		return fmt.Errorf("tailnet up: %w", err)
	}

	tcpLn, err := srv.Listen("tcp", fmt.Sprintf(":%d", voiceTestPort))
	if err != nil {
		return fmt.Errorf("listen tcp: %w", err)
	}
	udpLn, err := srv.Listen("udp", fmt.Sprintf(":%d", voiceTestPort))
	if err != nil {
		return fmt.Errorf("listen udp: %w", err)
	}
	go echoAccept(tcpLn)
	go echoAccept(udpLn)
	fmt.Printf("voice echo up at %s (tcp+udp :%d)\n", status.Self.DNSName, voiceTestPort)
	select {}
}

func echoAccept(ln net.Listener) {
	for {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		go func() {
			defer conn.Close()
			io.Copy(conn, conn)
		}()
	}
}

func echoPacket(pc net.PacketConn) {
	buf := make([]byte, 2048)
	for {
		n, addr, err := pc.ReadFrom(buf)
		if err != nil {
			return
		}
		pc.WriteTo(buf[:n], addr)
	}
}

func voiceTestClient(args []string) error {
	fs := flag.NewFlagSet("voicetest client", flag.ContinueOnError)
	host := fs.String("host", "", "echo node tailnet host (DNS name or IP)")
	proto := fs.String("proto", "udp,tcp", "comma-separated transports to test over one tailnet join")
	duration := fs.Duration("duration", 15*time.Second, "measurement duration")
	warmup := fs.Duration("warmup", 3*time.Second, "initial period excluded from stats (path setup, DERP->direct upgrade)")
	rate := fs.Int("rate", 50, "frames per second (50 = 20ms voice cadence)")
	size := fs.Int("size", 160, "frame size in bytes")
	stateDir := fs.String("statedir", defaultStateDir("tsnet-voice-client"), "directory for node state")
	authKey := fs.String("authkey", os.Getenv("TS_AUTHKEY"), "Tailscale auth key")
	controlURL := fs.String("control-url", os.Getenv("OPENBASE_TSNET_CONTROL_URL"), "coordination server URL")
	direct := fs.String("direct", "", "skip tsnet; dial this local addr instead (baseline mode)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *size < 16 {
		return fmt.Errorf("--size must be at least 16")
	}

	protos := strings.Split(*proto, ",")

	var dial func(proto string) (net.Conn, string, error)
	if *direct != "" {
		dial = func(p string) (net.Conn, string, error) {
			conn, err := net.Dial(p, *direct)
			return conn, fmt.Sprintf("direct %s %s", p, *direct), err
		}
	} else {
		if *host == "" {
			return fmt.Errorf("--host is required (or use --direct for baseline)")
		}
		srv := &tsnet.Server{
			Hostname:   "openbase-voice-client",
			Dir:        *stateDir,
			AuthKey:    *authKey,
			ControlURL: *controlURL,
			Ephemeral:  true,
			Logf:       loginURLLogf,
		}
		defer srv.Close()
		if err := os.MkdirAll(*stateDir, 0o700); err != nil {
			return err
		}
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
		_, err := srv.Up(ctx)
		cancel()
		if err != nil {
			return fmt.Errorf("tailnet up: %w", err)
		}
		dial = func(p string) (net.Conn, string, error) {
			dialCtx, dialCancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer dialCancel()
			conn, err := srv.Dial(dialCtx, p, fmt.Sprintf("%s:%d", *host, voiceTestPort))
			return conn, fmt.Sprintf("tsnet %s %s", p, *host), err
		}
	}

	for _, p := range protos {
		p = strings.TrimSpace(p)
		conn, label, err := dial(p)
		if err != nil {
			return fmt.Errorf("dial %s: %w", p, err)
		}
		stats := runVoiceLoad(conn, p, *duration, *warmup, *rate, *size)
		conn.Close()
		stats["transport"] = label
		out, _ := json.MarshalIndent(stats, "", " ")
		fmt.Println(string(out))
	}
	return nil
}

func runVoiceLoad(conn net.Conn, proto string, duration, warmup time.Duration, rate, size int) map[string]any {
	interval := time.Second / time.Duration(rate)
	warmupFrames := uint64(warmup / interval)

	var mu sync.Mutex
	rtts := make([]time.Duration, 0, rate*int(duration/time.Second))
	var received uint64

	done := make(chan struct{})
	go func() {
		defer close(done)
		buf := make([]byte, 2048)
		for {
			var (
				n   int
				err error
			)
			if proto == "tcp" {
				_, err = io.ReadFull(conn, buf[:size])
				n = size
			} else {
				n, err = conn.Read(buf)
			}
			if err != nil {
				return
			}
			if n < 16 {
				continue
			}
			seq := binary.BigEndian.Uint64(buf[:8])
			sent := time.Unix(0, int64(binary.BigEndian.Uint64(buf[8:16])))
			mu.Lock()
			received++
			if seq > warmupFrames {
				rtts = append(rtts, time.Since(sent))
			}
			mu.Unlock()
		}
	}()

	frame := make([]byte, size)
	var sent uint64
	ticker := time.NewTicker(interval)
	deadline := time.Now().Add(duration)
	for now := range ticker.C {
		if now.After(deadline) {
			break
		}
		sent++
		binary.BigEndian.PutUint64(frame[:8], sent)
		binary.BigEndian.PutUint64(frame[8:16], uint64(time.Now().UnixNano()))
		if _, err := conn.Write(frame); err != nil {
			break
		}
	}
	ticker.Stop()
	time.Sleep(750 * time.Millisecond) // drain in-flight echoes
	conn.SetReadDeadline(time.Now())
	<-done

	mu.Lock()
	defer mu.Unlock()
	measured := sent - min(sent, warmupFrames)
	result := map[string]any{
		"frames_sent":     sent,
		"frames_received": received,
		"loss_pct":        pct(sent-min(sent, received), sent),
		"measured_frames": len(rtts),
		"rate_fps":        rate,
		"frame_bytes":     size,
		"warmup_excluded": warmupFrames,
	}
	if len(rtts) == 0 {
		result["error"] = "no frames measured"
		return result
	}
	if uint64(len(rtts)) < measured {
		result["measured_loss_pct"] = pct(measured-uint64(len(rtts)), measured)
	} else {
		result["measured_loss_pct"] = 0.0
	}
	// Mean absolute successive RTT difference approximates perceived jitter;
	// computed on arrival order, before sorting for percentiles.
	var jitterSum float64
	prev := rtts[0]
	for _, r := range rtts[1:] {
		d := ms(r) - ms(prev)
		if d < 0 {
			d = -d
		}
		jitterSum += d
		prev = r
	}
	result["jitter_ms_mean"] = jitterSum / float64(len(rtts)-1)

	sort.Slice(rtts, func(i, j int) bool { return rtts[i] < rtts[j] })
	result["rtt_ms"] = map[string]float64{
		"p50": ms(rtts[len(rtts)/2]),
		"p95": ms(rtts[len(rtts)*95/100]),
		"p99": ms(rtts[len(rtts)*99/100]),
		"max": ms(rtts[len(rtts)-1]),
	}
	return result
}

func ms(d time.Duration) float64 { return float64(d.Microseconds()) / 1000 }

func pct(part, total uint64) float64 {
	if total == 0 {
		return 0
	}
	return float64(part) * 100 / float64(total)
}
