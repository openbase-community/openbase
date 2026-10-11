package main

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"sync"
	"testing"
	"time"
)

func TestForwardPinnedIPRequiresWhoIs(t *testing.T) {
	manager := newTestForwardManager(t)
	manager.whoisErr = errors.New("whois unavailable")
	if _, err := manager.Add(forwardRequest{Port: 4000, Peer: "127.0.0.1"}); err != nil {
		t.Fatal(err)
	}
	if _, _, err := manager.get(t, 4000, "/"); err == nil {
		t.Fatal("IP pin accepted a connection without WhoIs identity")
	}
}

func TestForwardPinRequiresMatchingNodeAddress(t *testing.T) {
	manager := newTestForwardManager(t)
	manager.whoisIPs = []netip.Addr{netip.MustParseAddr("100.64.0.9")}
	if _, err := manager.Add(forwardRequest{Port: 4000, Peer: "node-phone"}); err != nil {
		t.Fatal(err)
	}
	if _, _, err := manager.get(t, 4000, "/"); err == nil {
		t.Fatal("node pin accepted a connection from an address not owned by the node")
	}
}

func TestForwardShutdownClosesActiveConnections(t *testing.T) {
	for _, mode := range []string{"remove", "ttl", "shutdown"} {
		t.Run(mode, func(t *testing.T) {
			manager := newTestForwardManager(t)
			if _, err := manager.Add(forwardRequest{Port: 4000, TTLSeconds: 1}); err != nil {
				t.Fatal(err)
			}
			conn, err := net.Dial("tcp", manager.addr(4000))
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			deadline := time.Now().Add(time.Second)
			for time.Now().Before(deadline) {
				forwards := manager.List()
				if len(forwards) == 0 {
					t.Fatal("forward expired before connection was established")
				}
				if forwards[0].Connections > 0 {
					break
				}
				time.Sleep(time.Millisecond)
			}
			switch mode {
			case "remove":
				manager.Remove(4000)
			case "shutdown":
				manager.CloseAll()
			}
			conn.SetReadDeadline(time.Now().Add(2 * time.Second))
			buffer := make([]byte, 1)
			if _, err := conn.Read(buffer); err == nil {
				t.Fatal("closed forward still accepts traffic")
			} else if timeout, ok := err.(net.Error); ok && timeout.Timeout() {
				t.Fatal("active connection survived forward shutdown")
			}
		})
	}
}

func TestForwardDrainsResponseAfterClientHalfClose(t *testing.T) {
	manager := newTestForwardManager(t)
	target, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer target.Close()
	go func() {
		upstream, err := target.Accept()
		if err != nil {
			return
		}
		defer upstream.Close()
		upstream.SetDeadline(time.Now().Add(2 * time.Second))
		io.ReadAll(upstream)
		io.WriteString(upstream, "complete callback response")
	}()
	manager.dial = func(ctx context.Context, port int) (net.Conn, error) {
		var dialer net.Dialer
		return dialer.DialContext(ctx, "tcp", target.Addr().String())
	}
	if _, err := manager.Add(forwardRequest{Port: 4000, OneShot: true}); err != nil {
		t.Fatal(err)
	}
	conn, err := net.Dial("tcp", manager.addr(4000))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(time.Second))
	io.WriteString(conn, "callback request")
	conn.(*net.TCPConn).CloseWrite()
	response, err := io.ReadAll(conn)
	if err != nil || string(response) != "complete callback response" {
		t.Fatalf("response = %q, error = %v", response, err)
	}
}

func TestForwardPublicationAndRemovalRaces(t *testing.T) {
	manager := newTestForwardManager(t)
	api := &localAPI{token: "secret"}
	var workers sync.WaitGroup
	workers.Go(func() {
		for range 100 {
			api.forwards.Store(manager.forwardManager)
			api.forwards.Store(nil)
		}
	})
	for range 100 {
		recorder := httptest.NewRecorder()
		api.handleListForwards(recorder, httptest.NewRequest(http.MethodGet, "/forwards", nil))
	}
	workers.Wait()
	if _, err := manager.Add(forwardRequest{Port: 4000}); err != nil {
		t.Fatal(err)
	}
	manager.forwardManager.mu.Lock()
	previous := manager.entries[4000]
	manager.forwardManager.mu.Unlock()
	manager.Remove(4000)
	if _, err := manager.Add(forwardRequest{Port: 4000}); err != nil {
		t.Fatal(err)
	}
	for range 10 {
		workers.Go(func() { manager.remove(4000, previous) })
	}
	workers.Wait()
	if len(manager.List()) != 1 {
		t.Fatal("stale completion removed the replacement forward")
	}
}
