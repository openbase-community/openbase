package main

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"sync/atomic"
	"time"

	"tailscale.com/client/local"
	"tailscale.com/ipn"
	"tailscale.com/tsnet"
)

// loadOrCreateControlToken returns the control-API token, minting one into
// <statedir>/control.token (0600) on first run.
func loadOrCreateControlToken(stateDir string) (string, error) {
	path := filepath.Join(stateDir, "control.token")
	if data, err := os.ReadFile(path); err == nil {
		if token := string(data); token != "" {
			return token, nil
		}
	}
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	token := hex.EncodeToString(raw)
	if err := os.WriteFile(path, []byte(token), 0o600); err != nil {
		return "", err
	}
	return token, nil
}

// localAPI is the loopback control surface consumed by the Python CLI in
// place of `tailscale status --json` / `tailscale serve status --json`.
type localAPI struct {
	srv        *tsnet.Server
	lc         *local.Client
	token      string
	turnCreds  *turnCredentials
	forwardsUp atomic.Bool
}

func (a *localAPI) markForwardsUp() { a.forwardsUp.Store(true) }

func (a *localAPI) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /status", a.handleStatus)
	mux.HandleFunc("GET /health", a.handleHealth)
	mux.HandleFunc("GET /probe", a.handleProbe)
	mux.HandleFunc("GET /turnprobe", a.handleTurnProbe)
	mux.HandleFunc("POST /login", a.handleLogin)
	return a.requireToken(mux)
}

// requireToken guards the control API: any local process can reach loopback,
// but /probe dials tailnet peers as this node and /health exposes the auth
// URL, so callers must present the token from <statedir>/control.token.
func (a *localAPI) requireToken(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth := r.Header.Get("Authorization")
		if subtle.ConstantTimeCompare([]byte(auth), []byte("Bearer "+a.token)) != 1 {
			writeJSON(w, http.StatusUnauthorized, map[string]any{
				"error": "missing or invalid control token (read <statedir>/control.token)",
			})
			return
		}
		next.ServeHTTP(w, r)
	})
}

// handleStatus emits ipnstate.Status, which marshals to the same JSON schema
// as `tailscale status --json` (Self.DNSName, Self.TailscaleIPs, Peer,
// CurrentTailnet.MagicDNSSuffix, ...), so existing parsers keep working.
func (a *localAPI) handleStatus(w http.ResponseWriter, r *http.Request) {
	st, err := a.lc.Status(r.Context())
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, st)
}

func (a *localAPI) handleHealth(w http.ResponseWriter, r *http.Request) {
	payload := map[string]any{
		"forwards_up": a.forwardsUp.Load(),
		"forwards": map[string]string{
			strconv.Itoa(openbaseTailnetPort): "http://" + openbaseLocalAddr,
			strconv.Itoa(livekitTailnetPort):  "tcp://" + livekitLocalAddr,
		},
	}
	st, err := a.lc.Status(r.Context())
	if err != nil {
		payload["backend_state"] = "Unknown"
		payload["error"] = err.Error()
		writeJSON(w, http.StatusOK, payload)
		return
	}
	payload["backend_state"] = st.BackendState
	payload["auth_url"] = st.AuthURL
	if st.Self != nil {
		payload["self_dns_name"] = st.Self.DNSName
	}
	writeJSON(w, http.StatusOK, payload)
}

// handleLogin logs the node in with an auth key at runtime, so the CLI can
// start the daemon first and supply a cloud-minted key once available
// (production flow: cloud mints per-device keys at login).
func (a *localAPI) handleLogin(w http.ResponseWriter, r *http.Request) {
	var body struct {
		AuthKey string `json:"auth_key"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.AuthKey == "" {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": "auth_key is required"})
		return
	}
	if err := a.lc.Start(r.Context(), ipn.Options{AuthKey: body.AuthKey}); err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]any{"error": err.Error()})
		return
	}
	// Start only stages the key; the login must be kicked explicitly. With an
	// auth key staged this redeems it rather than producing a browser URL.
	if err := a.lc.StartLoginInteractive(r.Context()); err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]any{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

// handleProbe dials a tailnet peer through the embedded node and relays the
// response, because the host network stack can no longer reach tailnet IPs.
// Example: /probe?host=phone.tailxxxx.ts.net&port=18080&path=/api/health/
func (a *localAPI) handleProbe(w http.ResponseWriter, r *http.Request) {
	host := r.URL.Query().Get("host")
	if host == "" {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": "missing host parameter"})
		return
	}
	port := r.URL.Query().Get("port")
	if port == "" {
		port = strconv.Itoa(openbaseTailnetPort)
	}
	path := r.URL.Query().Get("path")
	if path == "" {
		path = "/api/health/"
	}

	client := &http.Client{
		Timeout: probeTimeout,
		Transport: &http.Transport{
			DialContext: func(ctx context.Context, network, addr string) (conn net.Conn, err error) {
				return a.srv.Dial(ctx, network, addr)
			},
		},
	}
	url := fmt.Sprintf("http://%s:%s%s", host, port, path)
	start := time.Now()
	resp, err := client.Get(url)
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]any{
			"url": url, "ok": false, "error": err.Error(),
		})
		return
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	writeJSON(w, http.StatusOK, map[string]any{
		"url":         url,
		"ok":          resp.StatusCode == http.StatusOK,
		"status_code": resp.StatusCode,
		"body":        string(body),
		"elapsed_ms":  time.Since(start).Milliseconds(),
	})
}
