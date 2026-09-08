package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strings"
	"time"

	"tailscale.com/tsnet"
)

// loopbackForward pipes local loopback connections to a tailnet address via
// the embedded node, mirroring TailscaleKit's loopback proxy on iOS.
func loopbackForward(srv *tsnet.Server, ln net.Listener, remote string) {
	for {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		go func() {
			defer conn.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			upstream, err := srv.Dial(ctx, "tcp", remote)
			cancel()
			if err != nil {
				fmt.Fprintf(os.Stderr, "forward dial %s: %v\n", remote, err)
				return
			}
			defer upstream.Close()
			done := make(chan struct{}, 2)
			go func() { io.Copy(upstream, conn); done <- struct{}{} }()
			go func() { io.Copy(conn, upstream); done <- struct{}{} }()
			<-done
		}()
	}
}

// runPeer joins the tailnet as a second embedded node and fetches a URL over
// it. This simulates the phone app (which will embed TailscaleKit) so the
// desktop flow can be verified end-to-end without any Tailscale app.
func runPeer(args []string) error {
	fs := flag.NewFlagSet("peer", flag.ContinueOnError)
	hostname := fs.String("hostname", "openbase-phone-sim", "tailnet hostname for the simulated peer")
	stateDir := fs.String("statedir", defaultStateDir("tsnet-peer"), "directory for peer node state")
	authKey := fs.String("authkey", os.Getenv("TS_AUTHKEY_PEER"), "Tailscale auth key (defaults to $TS_AUTHKEY_PEER, then $TS_AUTHKEY)")
	controlURL := fs.String("control-url", os.Getenv("OPENBASE_TSNET_CONTROL_URL"), "coordination server URL")
	target := fs.String("url", "", "http URL on the tailnet to fetch, e.g. http://my-mac-openbase.tailxxx.ts.net:18080/api/health/")
	host := fs.String("host", "", "tailnet host for --forward targets")
	forwards := fs.String("forward", "", "comma-separated localPort=tailnetPort loopback forwards (keeps running), e.g. 17880=7880,17881=7881; this is what TailscaleKit's loopback proxy does on iOS")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *target == "" && *forwards == "" {
		return fmt.Errorf("--url or --forward is required")
	}
	if *forwards != "" && *host == "" {
		return fmt.Errorf("--forward requires --host")
	}
	if *authKey == "" {
		*authKey = os.Getenv("TS_AUTHKEY")
	}

	srv := &tsnet.Server{
		Hostname:   *hostname,
		Dir:        *stateDir,
		AuthKey:    *authKey,
		ControlURL: *controlURL,
		Ephemeral:  true, // the simulator should clean itself up
		Logf:       func(string, ...any) {},
	}
	defer srv.Close()

	if err := os.MkdirAll(*stateDir, 0o700); err != nil {
		return fmt.Errorf("create state dir: %w", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	status, err := srv.Up(ctx)
	if err != nil {
		return fmt.Errorf("tailnet up: %w", err)
	}
	fmt.Printf("peer joined tailnet as %s (%v)\n", status.Self.DNSName, status.TailscaleIPs)

	if *forwards != "" {
		for _, pair := range strings.Split(*forwards, ",") {
			localPort, remotePort, ok := strings.Cut(strings.TrimSpace(pair), "=")
			if !ok {
				return fmt.Errorf("bad --forward entry %q (want localPort=tailnetPort)", pair)
			}
			ln, err := net.Listen("tcp", "127.0.0.1:"+localPort)
			if err != nil {
				return fmt.Errorf("listen 127.0.0.1:%s: %w", localPort, err)
			}
			remote := net.JoinHostPort(*host, remotePort)
			go loopbackForward(srv, ln, remote)
			fmt.Printf("forwarding 127.0.0.1:%s -> %s (over tailnet)\n", localPort, remote)
		}
		if *target == "" {
			select {} // run until killed
		}
	}

	client := &http.Client{
		Timeout: 15 * time.Second,
		Transport: &http.Transport{
			DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
				return srv.Dial(ctx, network, addr)
			},
		},
	}
	start := time.Now()
	resp, err := client.Get(*target)
	if err != nil {
		return fmt.Errorf("fetch %s: %w", *target, err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	fmt.Printf("GET %s -> %d in %s\n%s\n", *target, resp.StatusCode, time.Since(start).Round(time.Millisecond), body)
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("unexpected status %d", resp.StatusCode)
	}
	return nil
}
