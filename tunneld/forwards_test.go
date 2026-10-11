package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"strings"
	"sync"
	"testing"
	"time"
)

// testForwardManager runs the manager over loopback: "tailnet" listeners are
// ephemeral loopback ports recorded per requested port, and the "local
// target" is a tiny HTTP server that echoes the request path.
type testForwardManager struct {
	*forwardManager
	mu        sync.Mutex
	listeners map[int]net.Listener
	target    *httptest.Server
	whoisID   string
	whoisIPs  []netip.Addr
	whoisErr  error
}

func newTestForwardManager(t *testing.T, reserved ...int) *testForwardManager {
	t.Helper()
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "callback %s", r.URL.RequestURI())
	}))
	t.Cleanup(target.Close)
	tm := &testForwardManager{
		listeners: map[int]net.Listener{}, target: target,
		whoisID: "node-phone", whoisIPs: []netip.Addr{netip.MustParseAddr("127.0.0.1")},
	}
	m := &forwardManager{
		entries:  map[int]*forwardEntry{},
		reserved: map[int]bool{},
		now:      time.Now,
	}
	for _, port := range reserved {
		m.reserved[port] = true
	}
	m.listen = func(port int) (net.Listener, error) {
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			return nil, err
		}
		tm.mu.Lock()
		tm.listeners[port] = ln
		tm.mu.Unlock()
		return ln, nil
	}
	m.dial = func(ctx context.Context, port int) (net.Conn, error) {
		var d net.Dialer
		return d.DialContext(ctx, "tcp", strings.TrimPrefix(target.URL, "http://"))
	}
	m.whois = func(ctx context.Context, remoteAddr string) (string, []netip.Addr, error) {
		tm.mu.Lock()
		defer tm.mu.Unlock()
		return tm.whoisID, tm.whoisIPs, tm.whoisErr
	}
	tm.forwardManager = m
	t.Cleanup(m.CloseAll)
	return tm
}

func (tm *testForwardManager) addr(port int) string {
	tm.mu.Lock()
	defer tm.mu.Unlock()
	return tm.listeners[port].Addr().String()
}

func (tm *testForwardManager) get(t *testing.T, port int, path string) (int, string, error) {
	t.Helper()
	// Each request must open a fresh connection so every accept runs the
	// peer check; keep-alive reuse would bypass it.
	client := &http.Client{
		Timeout:   3 * time.Second,
		Transport: &http.Transport{DisableKeepAlives: true},
	}
	resp, err := client.Get("http://" + tm.addr(port) + path)
	if err != nil {
		return 0, "", err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(body), nil
}

func TestForwardLifecycle(t *testing.T) {
	tm := newTestForwardManager(t, 18080)
	info, err := tm.Add(forwardRequest{Port: 1455, TTLSeconds: 60})
	if err != nil {
		t.Fatalf("add: %v", err)
	}
	if info.Port != 1455 || info.Target != "127.0.0.1:1455" || info.OneShot {
		t.Fatalf("unexpected info %+v", info)
	}
	if ttl := info.ExpiresAt.Sub(info.CreatedAt); ttl != 60*time.Second {
		t.Fatalf("ttl = %s, want 60s", ttl)
	}
	status, body, err := tm.get(t, 1455, "/auth/callback?code=abc&state=xyz")
	if err != nil || status != 200 || body != "callback /auth/callback?code=abc&state=xyz" {
		t.Fatalf("forwarded request: status %d body %q err %v", status, body, err)
	}
	// Not one-shot: a second request still works and the counter advances.
	if _, _, err := tm.get(t, 1455, "/again"); err != nil {
		t.Fatalf("second request: %v", err)
	}
	list := tm.List()
	if len(list) != 1 || list[0].Connections != 2 {
		t.Fatalf("list = %+v, want one forward with 2 connections", list)
	}
	if !tm.Remove(1455) {
		t.Fatal("remove reported no forward")
	}
	if tm.Remove(1455) {
		t.Fatal("second remove should report nothing")
	}
	if _, _, err := tm.get(t, 1455, "/closed"); err == nil {
		t.Fatal("expected the removed forward to refuse connections")
	}
	if len(tm.List()) != 0 {
		t.Fatalf("list after remove = %+v", tm.List())
	}
}

func TestForwardOneShotRetiresAfterFirstConnection(t *testing.T) {
	tm := newTestForwardManager(t)
	if _, err := tm.Add(forwardRequest{Port: 52807, OneShot: true}); err != nil {
		t.Fatalf("add: %v", err)
	}
	status, body, err := tm.get(t, 52807, "/oauth/callback?code=1")
	if err != nil || status != 200 || !strings.Contains(body, "code=1") {
		t.Fatalf("first request: status %d body %q err %v", status, body, err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for len(tm.List()) != 0 && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if len(tm.List()) != 0 {
		t.Fatalf("one-shot forward still listed: %+v", tm.List())
	}
	if _, _, err := tm.get(t, 52807, "/second"); err == nil {
		t.Fatal("expected the one-shot forward to be closed after its first connection")
	}
}

func TestForwardOneShotSurvivesAnEmptyPreconnect(t *testing.T) {
	tm := newTestForwardManager(t)
	if _, err := tm.Add(forwardRequest{Port: 52808, OneShot: true}); err != nil {
		t.Fatalf("add: %v", err)
	}
	// A browser preconnect: open the connection, send nothing, close it.
	preconnect, err := net.Dial("tcp", tm.addr(52808))
	if err != nil {
		t.Fatalf("preconnect: %v", err)
	}
	preconnect.Close()
	time.Sleep(200 * time.Millisecond)
	if len(tm.List()) != 1 {
		t.Fatalf("an empty preconnect retired the one-shot forward: %+v", tm.List())
	}
	status, body, err := tm.get(t, 52808, "/oauth/callback?code=1")
	if err != nil || status != 200 || !strings.Contains(body, "code=1") {
		t.Fatalf("callback after preconnect: status %d body %q err %v", status, body, err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for len(tm.List()) != 0 && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if len(tm.List()) != 0 {
		t.Fatalf("one-shot forward not retired after the real exchange: %+v", tm.List())
	}
}

func TestForwardExpiresAfterTTL(t *testing.T) {
	tm := newTestForwardManager(t)
	if _, err := tm.Add(forwardRequest{Port: 3000, TTLSeconds: 1}); err != nil {
		t.Fatalf("add: %v", err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for len(tm.List()) != 0 && time.Now().Before(deadline) {
		time.Sleep(50 * time.Millisecond)
	}
	if len(tm.List()) != 0 {
		t.Fatalf("forward did not expire: %+v", tm.List())
	}
	// The port is free again after expiry.
	if _, err := tm.Add(forwardRequest{Port: 3000}); err != nil {
		t.Fatalf("re-add after expiry: %v", err)
	}
}

func TestForwardOneShotTTLClosesIdlePreconnect(t *testing.T) {
	tm := newTestForwardManager(t)
	if _, err := tm.Add(forwardRequest{Port: 52809, OneShot: true, TTLSeconds: 1}); err != nil {
		t.Fatalf("add: %v", err)
	}
	preconnect, err := net.Dial("tcp", tm.addr(52809))
	if err != nil {
		t.Fatalf("preconnect: %v", err)
	}
	defer preconnect.Close()
	preconnect.SetReadDeadline(time.Now().Add(5 * time.Second))
	buffer := make([]byte, 1)
	if _, err := preconnect.Read(buffer); err != io.EOF {
		t.Fatalf("idle connection was not closed by expiry: %v", err)
	}
	if len(tm.List()) != 0 {
		t.Fatalf("one-shot forward survived its TTL: %+v", tm.List())
	}
	if connection, err := net.DialTimeout("tcp", tm.addr(52809), time.Second); err == nil {
		connection.Close()
		t.Fatal("expired forward still accepts connections")
	}
}

func TestForwardValidation(t *testing.T) {
	tm := newTestForwardManager(t, 18080)
	cases := []struct {
		name   string
		req    forwardRequest
		status int
	}{
		{"privileged port", forwardRequest{Port: 80}, 400},
		{"port too high", forwardRequest{Port: 70000}, 400},
		{"negative ttl", forwardRequest{Port: 3000, TTLSeconds: -1}, 400},
		{"ttl too long", forwardRequest{Port: 3000, TTLSeconds: 7200}, 400},
		{"ttl overflow", forwardRequest{Port: 3000, TTLSeconds: 1 << 62}, 400},
		{"bad peer", forwardRequest{Port: 3000, Peer: "not a/peer"}, 400},
		{"reserved port", forwardRequest{Port: 18080}, 409},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := tm.Add(tc.req)
			var fe *forwardError
			if !errors.As(err, &fe) || fe.status != tc.status {
				t.Fatalf("Add(%+v) err = %v, want status %d", tc.req, err, tc.status)
			}
		})
	}
	if _, err := tm.Add(forwardRequest{Port: 3000}); err != nil {
		t.Fatalf("add: %v", err)
	}
	_, err := tm.Add(forwardRequest{Port: 3000})
	var fe *forwardError
	if !errors.As(err, &fe) || fe.status != 409 {
		t.Fatalf("duplicate add err = %v, want 409", err)
	}
}

func TestForwardLimit(t *testing.T) {
	tm := newTestForwardManager(t)
	for i := 0; i < forwardMaxCount; i++ {
		if _, err := tm.Add(forwardRequest{Port: 20000 + i}); err != nil {
			t.Fatalf("add %d: %v", i, err)
		}
	}
	_, err := tm.Add(forwardRequest{Port: 20000 + forwardMaxCount})
	var fe *forwardError
	if !errors.As(err, &fe) || fe.status != 409 {
		t.Fatalf("over-limit add err = %v, want 409", err)
	}
}

func TestForwardPeerPinByIP(t *testing.T) {
	tm := newTestForwardManager(t)
	// Loopback connections arrive from 127.0.0.1; pinning another IP refuses them.
	if _, err := tm.Add(forwardRequest{Port: 4000, Peer: "100.64.0.9"}); err != nil {
		t.Fatalf("add: %v", err)
	}
	if _, _, err := tm.get(t, 4000, "/"); err == nil {
		t.Fatal("expected the pinned forward to refuse a connection from another address")
	}
	if _, err := tm.Add(forwardRequest{Port: 4001, Peer: "127.0.0.1"}); err != nil {
		t.Fatalf("add: %v", err)
	}
	if status, _, err := tm.get(t, 4001, "/"); err != nil || status != 200 {
		t.Fatalf("pinned-to-self request: status %d err %v", status, err)
	}
}

func TestForwardPeerPinByNodeID(t *testing.T) {
	tm := newTestForwardManager(t)
	tm.whoisID = "node-phone"
	tm.whoisIPs = []netip.Addr{netip.MustParseAddr("127.0.0.1")}
	if _, err := tm.Add(forwardRequest{Port: 4100, Peer: "node-phone"}); err != nil {
		t.Fatalf("add: %v", err)
	}
	if status, _, err := tm.get(t, 4100, "/"); err != nil || status != 200 {
		t.Fatalf("matching node request: status %d err %v", status, err)
	}
	tm.mu.Lock()
	tm.whoisID = "node-other"
	tm.mu.Unlock()
	if _, _, err := tm.get(t, 4100, "/"); err == nil {
		t.Fatal("expected a different node to be refused")
	}
	tm.mu.Lock()
	tm.whoisErr = errors.New("whois unavailable")
	tm.mu.Unlock()
	if _, _, err := tm.get(t, 4100, "/"); err == nil {
		t.Fatal("expected a whois failure to refuse the connection")
	}
}

func TestForwardLocalAPI(t *testing.T) {
	tm := newTestForwardManager(t)
	api := &localAPI{token: "secret"}
	api.forwards.Store(tm.forwardManager)
	server := httptest.NewServer(api.handler())
	t.Cleanup(server.Close)

	do := func(method, path string, body any) (int, map[string]any) {
		t.Helper()
		var reader io.Reader
		if body != nil {
			raw, _ := json.Marshal(body)
			reader = bytes.NewReader(raw)
		}
		req, _ := http.NewRequest(method, server.URL+path, reader)
		req.Header.Set("Authorization", "Bearer secret")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatalf("%s %s: %v", method, path, err)
		}
		defer resp.Body.Close()
		payload := map[string]any{}
		json.NewDecoder(resp.Body).Decode(&payload)
		return resp.StatusCode, payload
	}

	if status, payload := do("GET", "/forwards", nil); status != 200 || len(payload["forwards"].([]any)) != 0 {
		t.Fatalf("empty list: %d %v", status, payload)
	}
	status, payload := do("POST", "/forwards", map[string]any{"port": 1455, "ttl_seconds": 30, "one_shot": true})
	if status != 201 || payload["port"] != float64(1455) || payload["one_shot"] != true {
		t.Fatalf("create: %d %v", status, payload)
	}
	if status, payload := do("POST", "/forwards", map[string]any{"port": 1455}); status != 409 || payload["error"] == nil {
		t.Fatalf("duplicate create: %d %v", status, payload)
	}
	if status, _ := do("POST", "/forwards", map[string]any{"port": 22}); status != 400 {
		t.Fatalf("bad port: %d", status)
	}
	if status, _ := do("POST", "/forwards", map[string]any{"port": 3000, "peer_node_id": "node-phone"}); status != 400 {
		t.Fatalf("unknown pin field must not create an unpinned forward: %d", status)
	}
	if status, payload := do("GET", "/forwards", nil); status != 200 || len(payload["forwards"].([]any)) != 1 {
		t.Fatalf("list: %d %v", status, payload)
	}
	if status, _ := do("DELETE", "/forwards/1455", nil); status != 204 {
		t.Fatalf("delete: %d", status)
	}
	if status, _ := do("DELETE", "/forwards/1455", nil); status != 404 {
		t.Fatalf("delete again: %d", status)
	}
	if status, _ := do("DELETE", "/forwards/abc", nil); status != 400 {
		t.Fatalf("delete bad port: %d", status)
	}

	// Without the token nothing is reachable.
	for _, method := range []string{"GET", "POST", "DELETE"} {
		path := "/forwards"
		if method == "DELETE" {
			path += "/1455"
		}
		req, _ := http.NewRequest(method, server.URL+path, nil)
		resp, err := http.DefaultClient.Do(req)
		if err != nil || resp.StatusCode != 401 {
			t.Fatalf("unauthenticated %s: %v %v", method, err, resp)
		}
		resp.Body.Close()
	}

	// Before the node is up the endpoint reports unavailable instead of panicking.
	api.forwards.Store(nil)
	if status, _ := do("POST", "/forwards", map[string]any{"port": 3000}); status != 503 {
		t.Fatalf("not up: %d", status)
	}
}

func TestServiceForwardPipesToAnotherLoopbackPort(t *testing.T) {
	tm := newTestForwardManager(t, 18080)
	info, err := tm.Add(forwardRequest{Port: 443, LocalPort: 59443, Persistent: true})
	if err != nil {
		t.Fatalf("add service forward: %v", err)
	}
	if info.LocalPort != 59443 || info.Target != "127.0.0.1:59443" || !info.Persistent || !info.ExpiresAt.IsZero() {
		t.Fatalf("service forward info = %+v", info)
	}
	// The test dial ignores the port, but the manager must hand it the local
	// port, not the tailnet port: capture it.
	var dialed []int
	var mu sync.Mutex
	inner := tm.forwardManager.dial
	tm.forwardManager.dial = func(ctx context.Context, port int) (net.Conn, error) {
		mu.Lock()
		dialed = append(dialed, port)
		mu.Unlock()
		return inner(ctx, port)
	}
	resp, err := http.Get("http://" + tm.addr(443) + "/svc")
	if err != nil {
		t.Fatalf("get through service forward: %v", err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if string(body) != "callback /svc" {
		t.Fatalf("body = %q", body)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(dialed) != 1 || dialed[0] != 59443 {
		t.Fatalf("dialed %v, want [59443]", dialed)
	}
	if len(tm.List()) != 1 {
		t.Fatalf("a persistent forward must not retire after a connection: %v", tm.List())
	}
}

func TestServiceForwardValidation(t *testing.T) {
	tm := newTestForwardManager(t, 18080)
	cases := []struct {
		name string
		req  forwardRequest
	}{
		{"local port privileged", forwardRequest{Port: 443, LocalPort: 80}},
		{"local port too high", forwardRequest{Port: 443, LocalPort: 70000}},
		{"privileged tailnet port that is not a service port", forwardRequest{Port: 22, LocalPort: 59443}},
		{"service forwards are never one-shot", forwardRequest{Port: 443, LocalPort: 59443, OneShot: true}},
		{"persistent needs a local port", forwardRequest{Port: 3000, Persistent: true}},
		{"persistent takes no ttl", forwardRequest{Port: 443, LocalPort: 59443, Persistent: true, TTLSeconds: 30}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := tm.Add(tc.req)
			var fe *forwardError
			if !errors.As(err, &fe) || fe.status != 400 {
				t.Fatalf("Add(%+v) err = %v, want 400", tc.req, err)
			}
		})
	}
	// A plain forward on an unprivileged port may still name a local port
	// and keep a TTL.
	info, err := tm.Add(forwardRequest{Port: 8443, LocalPort: 59443, TTLSeconds: 30})
	if err != nil || info.Persistent || info.ExpiresAt.IsZero() {
		t.Fatalf("ttl service forward: %+v %v", info, err)
	}
	if _, err := tm.Add(forwardRequest{Port: 443, LocalPort: 59443, Persistent: true}); err != nil {
		t.Fatalf("service forward: %v", err)
	}
	if _, err := tm.Add(forwardRequest{Port: 80, LocalPort: 59080, Persistent: true}); err != nil {
		t.Fatalf("http service forward: %v", err)
	}
}

func TestRedirectForwardAnswersWithHTTPS(t *testing.T) {
	tm := newTestForwardManager(t, 18080)
	for _, bad := range []forwardRequest{
		{Port: 8080, RedirectHTTPS: true, Persistent: true},
		{Port: 80, RedirectHTTPS: true},
		{Port: 80, RedirectHTTPS: true, Persistent: true, LocalPort: 3000},
		{Port: 80, RedirectHTTPS: true, Persistent: true, Peer: "100.64.0.9"},
	} {
		_, err := tm.Add(bad)
		var fe *forwardError
		if !errors.As(err, &fe) || fe.status != 400 {
			t.Fatalf("Add(%+v) err = %v, want 400", bad, err)
		}
	}
	info, err := tm.Add(forwardRequest{Port: 80, RedirectHTTPS: true, Persistent: true})
	if err != nil || !info.RedirectHTTPS || !info.Persistent {
		t.Fatalf("redirect forward: %+v %v", info, err)
	}
	client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	req, _ := http.NewRequest("GET", "http://"+tm.addr(80)+"/path?x=1", nil)
	req.Host = "crm.abcdefghijkl.vpn.obs.so"
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusPermanentRedirect || resp.Header.Get("Location") != "https://crm.abcdefghijkl.vpn.obs.so/path?x=1" {
		t.Fatalf("status %d location %q", resp.StatusCode, resp.Header.Get("Location"))
	}
	if !tm.Remove(80) {
		t.Fatal("remove redirect forward")
	}
}
