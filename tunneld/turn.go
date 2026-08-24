package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"path/filepath"

	"github.com/pion/turn/v4"
	"tailscale.com/tsnet"
)

const (
	turnTailnetPort = 3478
	turnRealm       = "openbase"
)

// turnCredentials is the static long-term credential for the embedded TURN
// relay. It lives in <statedir>/turn.json (0600); the Django API serves it to
// the user's own devices over the tailnet so the phone's WebRTC stack can
// authenticate. The relay is only reachable over the tailnet, so tailnet ACLs
// are the primary access control and this credential is defense in depth.
type turnCredentials struct {
	Username string `json:"username"`
	Password string `json:"password"`
	Port     int    `json:"port"`
	Realm    string `json:"realm"`
}

func loadOrCreateTurnCredentials(stateDir string) (*turnCredentials, error) {
	path := filepath.Join(stateDir, "turn.json")
	if raw, err := os.ReadFile(path); err == nil {
		var creds turnCredentials
		if json.Unmarshal(raw, &creds) == nil && creds.Username != "" && creds.Password != "" {
			creds.Port = turnTailnetPort
			creds.Realm = turnRealm
			return &creds, nil
		}
	}
	creds := &turnCredentials{
		Username: randomHex(8),
		Password: randomHex(16),
		Port:     turnTailnetPort,
		Realm:    turnRealm,
	}
	raw, err := json.MarshalIndent(creds, "", "  ")
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(path, append(raw, '\n'), 0o600); err != nil {
		return nil, err
	}
	return creds, nil
}

// startTURN runs a TURN relay listening on the tailnet. WebRTC media cannot
// ride the userspace tailnet directly (the phone's OS has no route into an
// in-app tsnet node), so the phone forces its media through this relay: its
// LiveKit client is configured with a loopback TURN address that an in-app
// forwarder shuttles into the tailnet, and the relay's allocation sockets
// live on this host where the local LiveKit server can reach them.
func startTURN(srv *tsnet.Server, stateDir string, tailnetIP net.IP) (*turnCredentials, error) {
	creds, err := loadOrCreateTurnCredentials(stateDir)
	if err != nil {
		return nil, fmt.Errorf("turn credentials: %w", err)
	}

	// tsnet's ListenPacket requires a concrete node address, not a wildcard.
	addr := net.JoinHostPort(tailnetIP.String(), fmt.Sprintf("%d", turnTailnetPort))
	pc, err := srv.ListenPacket("udp", addr)
	if err != nil {
		return nil, fmt.Errorf("listen tailnet udp %s: %w", addr, err)
	}

	authKey := turn.GenerateAuthKey(creds.Username, turnRealm, creds.Password)
	_, err = turn.NewServer(turn.ServerConfig{
		Realm: turnRealm,
		AuthHandler: func(username, realm string, srcAddr net.Addr) ([]byte, bool) {
			if username == creds.Username {
				return authKey, true
			}
			return nil, false
		},
		PacketConnConfigs: []turn.PacketConnConfig{{
			PacketConn: pc,
			RelayAddressGenerator: &turn.RelayAddressGeneratorStatic{
				// Loopback-only allocation sockets: every peer the relay talks
				// to is host-local (LiveKit runs in "local" network mode in
				// embedded transport, advertising 127.0.0.1 candidates), and
				// macOS delivers same-host packets with a loopback source
				// anyway. Binding loopback keeps relay ports off the LAN
				// entirely — pion permissions were the only gate before.
				RelayAddress: net.IPv4(127, 0, 0, 1),
				Address:      "127.0.0.1",
			},
		}},
	})
	if err != nil {
		pc.Close()
		return nil, fmt.Errorf("turn server: %w", err)
	}
	return creds, nil
}

func randomHex(bytes int) string {
	raw := make([]byte, bytes)
	if _, err := rand.Read(raw); err != nil {
		panic(err) // crypto/rand failure is unrecoverable
	}
	return hex.EncodeToString(raw)
}
