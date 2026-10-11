package main

import (
	"flag"
	"fmt"
	"log"
	"net"
	"net/netip"
	"os"
	"time"

	"github.com/pion/turn/v4"
)

// The CGNAT block every tailnet (Tailscale and the Openbase netmesh
// headscale) assigns node addresses from.
var tailnetIPv4Prefix = netip.MustParsePrefix("100.64.0.0/10")

type turnRelayConfig struct {
	stateDir     string
	port         int
	pollInterval time.Duration
}

func parseTurnRelayFlags(args []string) (*turnRelayConfig, error) {
	fs := flag.NewFlagSet("turn", flag.ContinueOnError)
	cfg := &turnRelayConfig{}
	fs.StringVar(&cfg.stateDir, "statedir", envOr("OPENBASE_TSNET_STATE_DIR", defaultStateDir("tsnet")), "directory holding turn.json")
	fs.IntVar(&cfg.port, "port", turnTailnetPort, "UDP port to serve TURN on")
	fs.DurationVar(&cfg.pollInterval, "poll", 5*time.Second, "how often to re-check the VPN address")
	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	return cfg, nil
}

// runTurnRelay serves only the TURN relay, on the computer's device-wide VPN
// address, for installs whose tailnet transport is the VPN (netmesh or
// tailscale) rather than the embedded tsnet node that `serve` runs.
//
// A phone that cannot run the VPN joins the same tailnet from an in-app node
// instead; its WebRTC sockets have no OS route to this computer, so its media
// must relay through TURN exactly as in embedded transport. The relay binds
// the tailnet address only (never a LAN or public interface) and follows that
// address when the VPN reconnects with a new one. The credentials are the
// same <statedir>/turn.json the Django API advertises as `voice_turn`.
func runTurnRelay(args []string) error {
	cfg, err := parseTurnRelayFlags(args)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(cfg.stateDir, 0o700); err != nil {
		return fmt.Errorf("create state dir: %w", err)
	}
	creds, err := loadOrCreateTurnCredentials(cfg.stateDir)
	if err != nil {
		return fmt.Errorf("turn credentials: %w", err)
	}

	var (
		current net.IP
		relay   *turnRelay
	)
	for {
		ip := tailnetInterfaceIPv4(net.Interfaces)
		if !ip.Equal(current) {
			if relay != nil {
				relay.Close()
				relay = nil
				log.Printf("turn relay stopped: tailnet address %s is gone", current)
			}
			current = ip
			if ip == nil {
				log.Printf("waiting for a tailnet address on a local interface")
			} else if relay, err = startTurnRelay(ip, cfg.port, creds); err != nil {
				log.Printf("turn relay on %s:%d failed: %v (retrying)", ip, cfg.port, err)
				current = nil
			} else {
				log.Printf("turn on tailnet %s:%d (user %s)", ip, cfg.port, creds.Username)
			}
		}
		time.Sleep(cfg.pollInterval)
	}
}

type turnRelay struct {
	server *turn.Server
	conn   net.PacketConn
}

func (r *turnRelay) Close() {
	r.server.Close()
	r.conn.Close()
}

// startTurnRelay binds an OS UDP socket to ip:port and serves TURN on it.
// Relay allocations bind the same address: LiveKit in "tailscale" network
// mode advertises that address, so the phone installs its TURN permission
// for it, and same-host packets LiveKit sends there carry it as their source.
func startTurnRelay(ip net.IP, port int, creds *turnCredentials) (*turnRelay, error) {
	pc, err := net.ListenPacket("udp4", net.JoinHostPort(ip.String(), fmt.Sprintf("%d", port)))
	if err != nil {
		return nil, fmt.Errorf("listen udp: %w", err)
	}
	server, err := newTURNServer(pc, creds, ip)
	if err != nil {
		pc.Close()
		return nil, err
	}
	return &turnRelay{server: server, conn: pc}, nil
}

// tailnetInterfaceIPv4 returns the IPv4 tailnet address assigned to a local
// interface that is up, or nil when the VPN has none.
func tailnetInterfaceIPv4(interfaces func() ([]net.Interface, error)) net.IP {
	ifaces, err := interfaces()
	if err != nil {
		return nil
	}
	for _, iface := range ifaces {
		if iface.Flags&net.FlagUp == 0 {
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
			if ip4 == nil {
				continue
			}
			if parsed, ok := netip.AddrFromSlice(ip4); ok && tailnetIPv4Prefix.Contains(parsed) {
				return ip4
			}
		}
	}
	return nil
}
