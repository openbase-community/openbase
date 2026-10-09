package main

// Dynamic tailnet forwards: listeners added at runtime through the local
// control API so an agent in a cloud workspace can expose a loopback port
// (a dev server, or the loopback callback server of a CLI login) to the
// owner's other devices. Each forward listens on the tailnet only (tsnet
// never binds a container interface), expires after a TTL, may be one-shot
// (closed after its first completed connection, the OAuth-callback case)
// and may be pinned to a single peer. Reachability is already restricted
// to the owner's own devices by the Openbase VPN policy; the pin, TTL and
// one-shot flags narrow it further.

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/netip"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"tailscale.com/client/local"
	"tailscale.com/tsnet"
)

const (
	forwardMinPort    = 1024
	forwardMaxPort    = 65535
	forwardMaxCount   = 16
	forwardDefaultTTL = 10 * time.Minute
	forwardMaxTTL     = time.Hour
	forwardDialWait   = 5 * time.Second
	forwardPeerWait   = 3 * time.Second
)

// forwardRequest is the POST /forwards body.
type forwardRequest struct {
	Port       int    `json:"port"`
	TTLSeconds int    `json:"ttl_seconds,omitempty"`
	OneShot    bool   `json:"one_shot,omitempty"`
	Peer       string `json:"peer,omitempty"` // tailnet IP or stable node id
}

// forwardInfo is the public description of a live forward.
type forwardInfo struct {
	Port        int       `json:"port"`
	Target      string    `json:"target"`
	CreatedAt   time.Time `json:"created_at"`
	ExpiresAt   time.Time `json:"expires_at"`
	OneShot     bool      `json:"one_shot"`
	Peer        string    `json:"peer,omitempty"`
	Connections int64     `json:"connections"`
}

// forwardError carries an HTTP-ish status so the local API can map
// validation failures (400) and conflicts (409) without string matching.
type forwardError struct {
	status  int
	message string
}

func (e *forwardError) Error() string { return e.message }

func badForward(format string, args ...any) error {
	return &forwardError{status: 400, message: fmt.Sprintf(format, args...)}
}

func conflictForward(format string, args ...any) error {
	return &forwardError{status: 409, message: fmt.Sprintf(format, args...)}
}

type forwardEntry struct {
	info        forwardInfo
	ln          net.Listener
	timer       *time.Timer
	connections atomic.Int64
	closeOnce   sync.Once
	mu          sync.Mutex
	closed      bool
	active      map[net.Conn]struct{}
}

// forwardManager owns the dynamic listeners. The network hooks are fields so
// tests can run the whole lifecycle over loopback without a tailnet.
type forwardManager struct {
	mu       sync.Mutex
	entries  map[int]*forwardEntry
	reserved map[int]bool
	listen   func(port int) (net.Listener, error)
	dial     func(ctx context.Context, port int) (net.Conn, error)
	whois    func(ctx context.Context, remoteAddr string) (nodeID string, ips []netip.Addr, err error)
	now      func() time.Time
}

func newForwardManager(srv *tsnet.Server, lc *local.Client, reserved ...int) *forwardManager {
	m := &forwardManager{
		entries:  map[int]*forwardEntry{},
		reserved: map[int]bool{},
		now:      time.Now,
	}
	for _, port := range reserved {
		m.reserved[port] = true
	}
	m.listen = func(port int) (net.Listener, error) {
		return srv.Listen("tcp", fmt.Sprintf(":%d", port))
	}
	m.dial = func(ctx context.Context, port int) (net.Conn, error) {
		var d net.Dialer
		return d.DialContext(ctx, "tcp", fmt.Sprintf("127.0.0.1:%d", port))
	}
	m.whois = func(ctx context.Context, remoteAddr string) (string, []netip.Addr, error) {
		resp, err := lc.WhoIs(ctx, remoteAddr)
		if err != nil {
			return "", nil, err
		}
		if resp == nil || resp.Node == nil {
			return "", nil, errors.New("whois: no node")
		}
		ips := make([]netip.Addr, 0, len(resp.Node.Addresses))
		for _, prefix := range resp.Node.Addresses {
			ips = append(ips, prefix.Addr())
		}
		return string(resp.Node.StableID), ips, nil
	}
	return m
}

// Add validates the request, opens the tailnet listener and starts serving.
func (m *forwardManager) Add(req forwardRequest) (forwardInfo, error) {
	if req.Port < forwardMinPort || req.Port > forwardMaxPort {
		return forwardInfo{}, badForward("port must be between %d and %d", forwardMinPort, forwardMaxPort)
	}
	ttl := forwardDefaultTTL
	if req.TTLSeconds < 0 {
		return forwardInfo{}, badForward("ttl_seconds must not be negative")
	}
	if req.TTLSeconds > int(forwardMaxTTL/time.Second) {
		return forwardInfo{}, badForward("ttl_seconds must not exceed %d", int(forwardMaxTTL.Seconds()))
	}
	if req.TTLSeconds > 0 {
		ttl = time.Duration(req.TTLSeconds) * time.Second
	}
	peer := strings.TrimSpace(req.Peer)
	if strings.ContainsAny(peer, " \t\r\n/\\@") {
		return forwardInfo{}, badForward("peer must be a tailnet IP or node id")
	}

	m.mu.Lock()
	if m.reserved[req.Port] {
		m.mu.Unlock()
		return forwardInfo{}, conflictForward("port %d is reserved by a fixed forward", req.Port)
	}
	if _, exists := m.entries[req.Port]; exists {
		m.mu.Unlock()
		return forwardInfo{}, conflictForward("port %d is already forwarded", req.Port)
	}
	if len(m.entries) >= forwardMaxCount {
		m.mu.Unlock()
		return forwardInfo{}, conflictForward("at most %d dynamic forwards may be active", forwardMaxCount)
	}
	ln, err := m.listen(req.Port)
	if err != nil {
		m.mu.Unlock()
		return forwardInfo{}, conflictForward("listen tailnet :%d: %v", req.Port, err)
	}
	now := m.now()
	entry := &forwardEntry{
		ln:     ln,
		active: map[net.Conn]struct{}{},
		info: forwardInfo{
			Port:      req.Port,
			Target:    fmt.Sprintf("127.0.0.1:%d", req.Port),
			CreatedAt: now,
			ExpiresAt: now.Add(ttl),
			OneShot:   req.OneShot,
			Peer:      peer,
		},
	}
	entry.timer = time.AfterFunc(ttl, func() {
		if m.remove(req.Port, entry) {
			log.Printf("dynamic forward :%d expired", req.Port)
		}
	})
	m.entries[req.Port] = entry
	m.mu.Unlock()

	go m.serve(entry)
	log.Printf("dynamic forward tailnet :%d -> %s (ttl %s, one_shot %v, peer %q)",
		req.Port, entry.info.Target, ttl, req.OneShot, peer)
	return entry.snapshot(), nil
}

// Remove closes the forward on port; it reports whether one existed.
func (m *forwardManager) Remove(port int) bool {
	m.mu.Lock()
	entry := m.entries[port]
	m.mu.Unlock()
	if entry == nil {
		return false
	}
	return m.remove(port, entry)
}

// remove closes entry if it is still the live forward for port.
func (m *forwardManager) remove(port int, entry *forwardEntry) bool {
	m.mu.Lock()
	current := m.entries[port]
	if current != entry {
		m.mu.Unlock()
		return false
	}
	entry.close()
	delete(m.entries, port)
	m.mu.Unlock()
	return true
}

func (m *forwardManager) List() []forwardInfo {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]forwardInfo, 0, len(m.entries))
	for _, entry := range m.entries {
		out = append(out, entry.snapshot())
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Port < out[j].Port })
	return out
}

// CloseAll drops every forward (daemon shutdown).
func (m *forwardManager) CloseAll() {
	for _, info := range m.List() {
		m.Remove(info.Port)
	}
}

func (e *forwardEntry) snapshot() forwardInfo {
	info := e.info
	info.Connections = e.connections.Load()
	return info
}

func (e *forwardEntry) close() {
	e.closeOnce.Do(func() {
		e.mu.Lock()
		defer e.mu.Unlock()
		e.closed = true
		if e.timer != nil {
			e.timer.Stop()
		}
		e.ln.Close()
		for conn := range e.active {
			conn.Close()
		}
	})
}

func (e *forwardEntry) track(conn net.Conn) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.closed {
		return false
	}
	e.active[conn] = struct{}{}
	return true
}

func (e *forwardEntry) untrack(conn net.Conn) {
	e.mu.Lock()
	defer e.mu.Unlock()
	delete(e.active, conn)
}

func (m *forwardManager) serve(entry *forwardEntry) {
	for {
		conn, err := entry.ln.Accept()
		if err != nil {
			return // listener closed (TTL, one-shot, remove, shutdown)
		}
		go m.handleConn(entry, conn)
	}
}

func (m *forwardManager) handleConn(entry *forwardEntry, conn net.Conn) {
	defer conn.Close()
	if !entry.track(conn) {
		return
	}
	defer entry.untrack(conn)
	if entry.info.Peer != "" && !m.connectionFromPeer(conn.RemoteAddr(), entry.info.Peer) {
		log.Printf("dynamic forward :%d refused %s: not the pinned peer", entry.info.Port, conn.RemoteAddr())
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), forwardDialWait)
	upstream, err := m.dial(ctx, entry.info.Port)
	cancel()
	if err != nil {
		log.Printf("dynamic forward :%d dial %s: %v", entry.info.Port, entry.info.Target, err)
		return
	}
	defer upstream.Close()
	if !entry.track(upstream) {
		return
	}
	defer entry.untrack(upstream)
	entry.connections.Add(1)
	done := make(chan struct{}, 2)
	copyStream := func(destination, source net.Conn) {
		_, err := io.Copy(destination, source)
		if halfCloser, ok := destination.(interface{ CloseWrite() error }); ok && err == nil {
			halfCloser.CloseWrite()
		} else {
			conn.Close()
			upstream.Close()
		}
		done <- struct{}{}
	}
	go copyStream(upstream, conn)
	go copyStream(conn, upstream)
	<-done
	<-done
	if entry.info.OneShot {
		if m.remove(entry.info.Port, entry) {
			log.Printf("dynamic forward :%d retired after its one-shot connection", entry.info.Port)
		}
	}
}

// connectionFromPeer reports whether remote belongs to the pinned peer. The
// pin is either a tailnet IP (compared directly) or a stable node id
// (resolved through the node's WhoIs).
func (m *forwardManager) connectionFromPeer(remote net.Addr, peer string) bool {
	remoteAddrPort, err := netip.ParseAddrPort(remote.String())
	if err != nil {
		return false
	}
	remoteIP := remoteAddrPort.Addr().Unmap()
	ctx, cancel := context.WithTimeout(context.Background(), forwardPeerWait)
	defer cancel()
	nodeID, ips, err := m.whois(ctx, remote.String())
	if err != nil || nodeID == "" {
		return false
	}
	pinnedIP, ipErr := netip.ParseAddr(strings.Trim(peer, "[]"))
	if ipErr == nil {
		if pinnedIP.Unmap() != remoteIP {
			return false
		}
	} else if nodeID != peer {
		return false
	}
	for _, ip := range ips {
		if ip.Unmap() == remoteIP {
			return true
		}
	}
	return false
}
