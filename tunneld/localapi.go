package main

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"tailscale.com/client/local"
	"tailscale.com/ipn"
	"tailscale.com/ipn/ipnstate"
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
	srv          *tsnet.Server
	lc           *local.Client
	token        string
	openbaseAddr string
	turnCreds    *turnCredentials
	forwardsUp   atomic.Bool
	forwards     atomic.Pointer[forwardManager]
}

func (a *localAPI) markForwardsUp() { a.forwardsUp.Store(true) }

func (a *localAPI) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /status", a.handleStatus)
	mux.HandleFunc("GET /health", a.handleHealth)
	mux.HandleFunc("GET /probe", a.handleProbe)
	mux.HandleFunc("GET /turnprobe", a.handleTurnProbe)
	mux.HandleFunc("POST /login", a.handleLogin)
	mux.HandleFunc("GET /forwards", a.handleListForwards)
	mux.HandleFunc("POST /forwards", a.handleAddForward)
	mux.HandleFunc("DELETE /forwards/{port}", a.handleRemoveForward)
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
			strconv.Itoa(openbaseTailnetPort): "http://" + a.openbaseAddr,
			strconv.Itoa(livekitTailnetPort):  "tcp://" + livekitLocalAddr,
		},
	}
	if forwards := a.forwards.Load(); forwards != nil {
		payload["dynamic_forwards"] = forwards.List()
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
// The destination must be a peer in the node's current tailnet status. The
// fixed port and path keep this authenticated loopback endpoint from becoming
// a general-purpose proxy with the node's network identity.
func (a *localAPI) handleProbe(w http.ResponseWriter, r *http.Request) {
	host, err := canonicalProbeHost(r.URL.Query().Get("host"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": "missing or invalid host parameter"})
		return
	}
	port := r.URL.Query().Get("port")
	if port == "" {
		port = strconv.Itoa(openbaseTailnetPort)
	}
	if port != strconv.Itoa(openbaseTailnetPort) {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": "unsupported probe port"})
		return
	}
	path := r.URL.Query().Get("path")
	if path == "" {
		path = "/api/health/"
	}
	if path != "/api/health/" {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": "unsupported probe path"})
		return
	}

	status, err := a.lc.Status(r.Context())
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"error": "tailnet status unavailable"})
		return
	}
	if !statusContainsProbePeer(status, host) {
		writeJSON(w, http.StatusForbidden, map[string]any{"error": "probe host is not a current tailnet peer"})
		return
	}

	client := &http.Client{
		Timeout: probeTimeout,
		Transport: &http.Transport{
			DialContext: func(ctx context.Context, network, addr string) (conn net.Conn, err error) {
				return a.srv.Dial(ctx, network, addr)
			},
		},
	}
	target := (&url.URL{
		Scheme: "http",
		Host:   net.JoinHostPort(host, port),
		Path:   path,
	}).String()
	request, err := http.NewRequestWithContext(r.Context(), http.MethodGet, target, nil)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid probe target"})
		return
	}
	start := time.Now()
	resp, err := client.Do(request)
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]any{
			"url": target, "ok": false, "error": err.Error(),
		})
		return
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	writeJSON(w, http.StatusOK, map[string]any{
		"url":         target,
		"ok":          resp.StatusCode == http.StatusOK,
		"status_code": resp.StatusCode,
		"body":        string(body),
		"elapsed_ms":  time.Since(start).Milliseconds(),
	})
}

func canonicalProbeHost(raw string) (string, error) {
	host := strings.TrimSpace(raw)
	if host == "" {
		return "", fmt.Errorf("empty host")
	}
	if addr, err := netip.ParseAddr(strings.Trim(host, "[]")); err == nil {
		return addr.Unmap().String(), nil
	}
	host = strings.ToLower(strings.TrimSuffix(host, "."))
	if host == "" || strings.ContainsAny(host, "/\\:@") {
		return "", fmt.Errorf("invalid host")
	}
	return host, nil
}

func statusContainsProbePeer(status *ipnstate.Status, host string) bool {
	if status == nil {
		return false
	}
	requestedIP, requestedIPErr := netip.ParseAddr(host)
	for _, peer := range status.Peer {
		if peer == nil {
			continue
		}
		if requestedIPErr == nil {
			for _, peerIP := range peer.TailscaleIPs {
				if peerIP.Unmap() == requestedIP.Unmap() {
					return true
				}
			}
			continue
		}
		peerDNSName := strings.ToLower(strings.TrimSuffix(peer.DNSName, "."))
		if host == peerDNSName {
			return true
		}
	}
	return false
}

// Dynamic forwards (see forwards.go). They exist only once the node is up,
// because a tailnet listener needs the node's addresses.

func (a *localAPI) handleListForwards(w http.ResponseWriter, r *http.Request) {
	forwards := a.forwards.Load()
	if forwards == nil {
		writeJSON(w, http.StatusOK, map[string]any{"forwards": []forwardInfo{}})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"forwards": forwards.List()})
}

func (a *localAPI) handleAddForward(w http.ResponseWriter, r *http.Request) {
	forwards := a.forwards.Load()
	if forwards == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"error": "tailnet node is not up yet"})
		return
	}
	var req forwardRequest
	decoder := json.NewDecoder(io.LimitReader(r.Body, 4<<10))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid JSON body"})
		return
	}
	info, err := forwards.Add(req)
	if err != nil {
		status := http.StatusInternalServerError
		var fe *forwardError
		if errors.As(err, &fe) {
			status = fe.status
		}
		writeJSON(w, status, map[string]any{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusCreated, info)
}

func (a *localAPI) handleRemoveForward(w http.ResponseWriter, r *http.Request) {
	port, err := strconv.Atoi(r.PathValue("port"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid port"})
		return
	}
	forwards := a.forwards.Load()
	if forwards == nil || !forwards.Remove(port) {
		writeJSON(w, http.StatusNotFound, map[string]any{"error": "no such forward"})
		return
	}
	w.WriteHeader(http.StatusNoContent)
}
