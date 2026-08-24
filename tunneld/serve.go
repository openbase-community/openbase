package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"tailscale.com/tsnet"
)

const (
	openbaseTailnetPort = 18080
	openbaseLocalAddr   = "127.0.0.1:7999"
	livekitTailnetPort  = 7880
	livekitLocalAddr    = "127.0.0.1:7880"
	livekitICETCPPort   = 7881
	livekitICETCPAddr   = "127.0.0.1:7881"
	localAPIAddr        = "127.0.0.1:7998"

	probeTimeout = 5 * time.Second
)

type serveConfig struct {
	hostname   string
	stateDir   string
	authKey    string
	controlURL string
	localAPI   string
	ephemeral  bool
}

func parseServeFlags(args []string) (*serveConfig, error) {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	cfg := &serveConfig{}
	defaultHost, _ := os.Hostname()
	fs.StringVar(&cfg.hostname, "hostname", envOr("OPENBASE_TSNET_HOSTNAME", defaultHost+"-openbase"), "tailnet hostname for this node")
	fs.StringVar(&cfg.stateDir, "statedir", envOr("OPENBASE_TSNET_STATE_DIR", defaultStateDir("tsnet")), "directory for tsnet node state")
	fs.StringVar(&cfg.authKey, "authkey", os.Getenv("TS_AUTHKEY"), "Tailscale auth key (defaults to $TS_AUTHKEY)")
	fs.StringVar(&cfg.controlURL, "control-url", os.Getenv("OPENBASE_TSNET_CONTROL_URL"), "coordination server URL (empty = Tailscale hosted)")
	fs.StringVar(&cfg.localAPI, "local-api", envOr("OPENBASE_TUNNELD_LOCAL_API", localAPIAddr), "loopback address for the control API")
	fs.BoolVar(&cfg.ephemeral, "ephemeral", false, "register as an ephemeral node")
	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	return cfg, nil
}

func runServe(args []string) error {
	cfg, err := parseServeFlags(args)
	if err != nil {
		return err
	}

	srv := &tsnet.Server{
		Hostname:   cfg.hostname,
		Dir:        cfg.stateDir,
		AuthKey:    cfg.authKey,
		ControlURL: cfg.controlURL,
		Ephemeral:  cfg.ephemeral,
		Logf:       func(string, ...any) {}, // tsnet is chatty; surface state via /status instead
	}
	defer srv.Close()

	if err := os.MkdirAll(cfg.stateDir, 0o700); err != nil {
		return fmt.Errorf("create state dir: %w", err)
	}
	token, err := loadOrCreateControlToken(cfg.stateDir)
	if err != nil {
		return fmt.Errorf("control token: %w", err)
	}

	// Start the local control API before the node is up so the CLI can watch
	// login progress (including the interactive AuthURL) from the beginning.
	lc, err := srv.LocalClient()
	if err != nil {
		return fmt.Errorf("local client: %w", err)
	}
	api := &localAPI{srv: srv, lc: lc, token: token}
	apiLn, err := net.Listen("tcp", cfg.localAPI)
	if err != nil {
		// The control port doubles as a single-instance lock.
		return fmt.Errorf(
			"listen local api %s (another openbase-tunneld already running?): %w",
			cfg.localAPI, err,
		)
	}
	go func() {
		if err := http.Serve(apiLn, api.handler()); err != nil {
			log.Printf("local api stopped: %v", err)
		}
	}()
	log.Printf("control api on http://%s", cfg.localAPI)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	status, err := srv.Up(ctx)
	cancel()
	if err != nil {
		return fmt.Errorf("tailnet up: %w", err)
	}
	log.Printf("joined tailnet as %s (%v)", status.Self.DNSName, status.TailscaleIPs)

	// tailnet :18080 -> local Django API, mirroring `tailscale serve --http`.
	openbaseLn, err := srv.Listen("tcp", ":"+strconv.Itoa(openbaseTailnetPort))
	if err != nil {
		return fmt.Errorf("listen tailnet :%d: %w", openbaseTailnetPort, err)
	}
	target := &url.URL{Scheme: "http", Host: openbaseLocalAddr}
	proxy := httputil.NewSingleHostReverseProxy(target)
	go func() {
		if err := http.Serve(openbaseLn, proxy); err != nil {
			log.Printf("openbase forward stopped: %v", err)
		}
	}()

	// tailnet :7880 -> local LiveKit, mirroring `tailscale serve --tcp`.
	livekitLn, err := srv.Listen("tcp", ":"+strconv.Itoa(livekitTailnetPort))
	if err != nil {
		return fmt.Errorf("listen tailnet :%d: %w", livekitTailnetPort, err)
	}
	go forwardTCP(livekitLn, livekitLocalAddr)

	// tailnet :7881 -> LiveKit ICE-TCP, so WebRTC media can ride the tailnet
	// as TCP when the phone has no OS-level tunnel (v1 sent media directly to
	// the desktop's tailnet IP, which only worked with the Tailscale app).
	iceLn, err := srv.Listen("tcp", ":"+strconv.Itoa(livekitICETCPPort))
	if err != nil {
		return fmt.Errorf("listen tailnet :%d: %w", livekitICETCPPort, err)
	}
	go forwardTCP(iceLn, livekitICETCPAddr)

	// tailnet :3478 -> embedded TURN relay for WebRTC media (see turn.go).
	var turnIP net.IP
	for _, ip := range status.TailscaleIPs {
		if ip.Is4() {
			turnIP = ip.AsSlice()
			break
		}
	}
	if turnIP == nil {
		return fmt.Errorf("start turn: node has no IPv4 tailnet address")
	}
	turnCreds, err := startTURN(srv, cfg.stateDir, turnIP)
	if err != nil {
		return fmt.Errorf("start turn: %w", err)
	}
	api.turnCreds = turnCreds

	api.markForwardsUp()
	log.Printf("forwarding tailnet :%d -> %s, :%d -> %s, :%d -> %s; turn on tailnet :%d (user %s)",
		openbaseTailnetPort, openbaseLocalAddr, livekitTailnetPort, livekitLocalAddr,
		livekitICETCPPort, livekitICETCPAddr, turnTailnetPort, turnCreds.Username)

	select {} // run until killed
}

func forwardTCP(ln net.Listener, targetAddr string) {
	for {
		conn, err := ln.Accept()
		if err != nil {
			log.Printf("tcp forward accept: %v", err)
			return
		}
		go func() {
			defer conn.Close()
			upstream, err := net.DialTimeout("tcp", targetAddr, 5*time.Second)
			if err != nil {
				log.Printf("tcp forward dial %s: %v", targetAddr, err)
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

func defaultStateDir(name string) string {
	home, err := os.UserHomeDir()
	if err != nil {
		return filepath.Join(".", ".openbase", name)
	}
	return filepath.Join(home, ".openbase", name)
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}
