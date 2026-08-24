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
				// Advertise the host's primary interface so LiveKit's ICE
				// (which may prune loopback remote candidates) can pair with
				// the relayed candidate; sockets bind all interfaces.
				RelayAddress: primaryHostIP(),
				Address:      "0.0.0.0",
			},
		}},
	})
	if err != nil {
		pc.Close()
		return nil, fmt.Errorf("turn server: %w", err)
	}
	return creds, nil
}

// primaryHostIP picks the address advertised for relay allocations: the
// local LiveKit server must be able to send UDP to it. Skip point-to-point
// interfaces — VPN/Tailscale tunnels are P2P and their addresses die with
// the owning app, while physical LAN interfaces are broadcast (LAN address
// ranges can't be trusted instead: some ISPs hand out CGNAT space on WiFi).
// Falls back to loopback when the machine has no usable interface.
func primaryHostIP() net.IP {
	ifaces, err := net.Interfaces()
	if err != nil {
		return net.IPv4(127, 0, 0, 1)
	}
	var fallback net.IP
	for _, iface := range ifaces {
		if iface.Flags&net.FlagUp == 0 ||
			iface.Flags&net.FlagLoopback != 0 ||
			iface.Flags&net.FlagPointToPoint != 0 {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, addr := range addrs {
			ipNet, ok := addr.(*net.IPNet)
			if !ok {
				continue
			}
			ip4 := ipNet.IP.To4()
			if ip4 == nil || ip4.IsLinkLocalUnicast() {
				continue
			}
			if ip4.IsPrivate() {
				return ip4
			}
			if fallback == nil {
				fallback = ip4
			}
		}
	}
	if fallback != nil {
		return fallback
	}
	return net.IPv4(127, 0, 0, 1)
}

func randomHex(bytes int) string {
	raw := make([]byte, bytes)
	if _, err := rand.Read(raw); err != nil {
		panic(err) // crypto/rand failure is unrecoverable
	}
	return hex.EncodeToString(raw)
}
