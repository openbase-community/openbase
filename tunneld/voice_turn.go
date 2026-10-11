package main

import (
	"crypto/subtle"
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

// A VPN relay must never bind a wildcard, LAN, public, or loopback address.
// Check interface ownership as well as the tailnet IPv4 range before listening.
func validateVPNBind(ip net.IP, addrs []net.Addr) error {
	if ip == nil || ip.To4() == nil || !net.ParseIP("100.64.0.0").Equal(ip.Mask(net.CIDRMask(10, 32))) {
		return fmt.Errorf("voice TURN requires an assigned tailnet IPv4 address")
	}
	for _, addr := range addrs {
		host, _, err := net.ParseCIDR(addr.String())
		if err == nil && host.Equal(ip) {
			return nil
		}
	}
	return fmt.Errorf("voice TURN address is not assigned to a local VPN interface")
}

func runVoiceTURN(args []string) error {
	fs := flag.NewFlagSet("voice-turn", flag.ContinueOnError)
	bind := fs.String("bind-ip", "", "existing system VPN IPv4 address")
	state := fs.String("statedir", defaultStateDir("voice-turn"), "private relay state directory")
	if err := fs.Parse(args); err != nil {
		return err
	}
	ip := net.ParseIP(*bind)
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		return err
	}
	if err := validateVPNBind(ip, addrs); err != nil {
		return err
	}
	if err := os.MkdirAll(*state, 0700); err != nil {
		return err
	}
	// Restrict an existing directory too; it contains relay and control secrets.
	if err := os.Chmod(*state, 0700); err != nil {
		return err
	}
	creds, err := loadOrCreateTurnCredentials(*state)
	if err != nil {
		return err
	}
	token, err := loadOrCreateControlToken(*state)
	if err != nil {
		return err
	}
	for _, name := range []string{"turn.json", "control.token"} {
		if err := os.Chmod(filepath.Join(*state, name), 0600); err != nil {
			return err
		}
	}
	pc, err := net.ListenPacket("udp4", net.JoinHostPort(ip.String(), "3478"))
	if err != nil {
		return err
	}
	defer pc.Close()
	relay, err := startTURNOnPacketConn(pc, creds)
	if err != nil {
		return err
	}
	defer relay.Close()
	// Control is separate from the embedded node's 7998 API. It reports only
	// live credentials: stale on-disk files can never advertise a dead relay.
	mux := voiceTURNControl(creds, token, func() bool {
		current, err := net.InterfaceAddrs()
		return err == nil && validateVPNBind(ip, current) == nil
	})
	control := &http.Server{Addr: "127.0.0.1:7997", Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	defer control.Close()
	failure := make(chan error, 1)
	go func() { failure <- control.ListenAndServe() }()
	ticker := time.NewTicker(3 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case err := <-failure:
			return err
		case <-ticker.C:
			current, err := net.InterfaceAddrs()
			if err != nil {
				return err
			}
			if err := validateVPNBind(ip, current); err != nil {
				return err
			}
		}
	}
}

func voiceTURNControl(creds *turnCredentials, token string, ready func() bool) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/credentials", func(w http.ResponseWriter, r *http.Request) {
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+token)) != 1 {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		if !ready() {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		_ = json.NewEncoder(w).Encode(creds)
	})
	return mux
}
