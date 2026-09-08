package main

import (
	"bytes"
	"context"
	"fmt"
	"net"
	"net/http"
	"time"

	"github.com/pion/turn/v4"
)

// handleTurnProbe validates the voice media path end to end without a phone:
// it dials this node's own tailnet TURN listener through the netstack (the
// same route a peer's packets take), authenticates, allocates a relay, and
// bounces a packet off a temporary local UDP echo — the stand-in for the
// LiveKit media port.
func (a *localAPI) handleTurnProbe(w http.ResponseWriter, r *http.Request) {
	if a.turnCreds == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"error": "turn not running"})
		return
	}
	st, err := a.lc.Status(r.Context())
	if err != nil || st.Self == nil || len(st.TailscaleIPs) == 0 {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"error": "node not on tailnet"})
		return
	}
	var selfIP4 string
	for _, ip := range st.TailscaleIPs {
		if ip.Is4() {
			selfIP4 = ip.String()
			break
		}
	}
	if selfIP4 == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"error": "no IPv4 tailnet address"})
		return
	}

	result, err := a.runTurnProbe(r.Context(), selfIP4)
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (a *localAPI) runTurnProbe(ctx context.Context, selfIP4 string) (map[string]any, error) {
	// Temporary UDP echo standing in for LiveKit's media socket.
	echo, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		return nil, fmt.Errorf("echo listen: %w", err)
	}
	defer echo.Close()
	go func() {
		buf := make([]byte, 1500)
		for {
			n, addr, err := echo.ReadFrom(buf)
			if err != nil {
				return
			}
			echo.WriteTo(buf[:n], addr)
		}
	}()

	dialCtx, cancel := context.WithTimeout(ctx, probeTimeout)
	defer cancel()
	turnAddr := net.JoinHostPort(selfIP4, fmt.Sprintf("%d", turnTailnetPort))
	conn, err := a.srv.Dial(dialCtx, "udp", turnAddr)
	if err != nil {
		return nil, fmt.Errorf("dial tailnet turn %s: %w", turnAddr, err)
	}
	defer conn.Close()

	client, err := turn.NewClient(&turn.ClientConfig{
		STUNServerAddr: turnAddr,
		TURNServerAddr: turnAddr,
		Conn:           turn.NewSTUNConn(conn),
		Username:       a.turnCreds.Username,
		Password:       a.turnCreds.Password,
		Realm:          turnRealm,
	})
	if err != nil {
		return nil, fmt.Errorf("turn client: %w", err)
	}
	defer client.Close()
	if err := client.Listen(); err != nil {
		return nil, fmt.Errorf("turn client listen: %w", err)
	}

	relayConn, err := client.Allocate()
	if err != nil {
		return nil, fmt.Errorf("turn allocate: %w", err)
	}
	defer relayConn.Close()

	payload := []byte("openbase-turnprobe")
	start := time.Now()
	if _, err := relayConn.WriteTo(payload, echo.LocalAddr()); err != nil {
		return nil, fmt.Errorf("relay write: %w", err)
	}
	relayConn.SetReadDeadline(time.Now().Add(probeTimeout))
	buf := make([]byte, 1500)
	n, _, err := relayConn.ReadFrom(buf)
	if err != nil {
		return nil, fmt.Errorf("relay read: %w", err)
	}
	rtt := time.Since(start)
	if !bytes.Equal(buf[:n], payload) {
		return nil, fmt.Errorf("relay echoed %d unexpected bytes", n)
	}

	return map[string]any{
		"ok":         true,
		"relay_addr": relayConn.LocalAddr().String(),
		"turn_addr":  turnAddr,
		"rtt_ms":     float64(rtt.Microseconds()) / 1000.0,
	}, nil
}
