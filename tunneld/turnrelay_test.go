package main

import (
	"bytes"
	"net"
	"net/netip"
	"testing"
	"time"

	"github.com/pion/turn/v4"
)

// A non-loopback local IPv4 address (a LAN or VPN interface) standing in for
// the tailnet address the relay binds in production: same-host delivery to
// it goes through the same path as to a VPN address.
func localNonLoopbackIPv4(t *testing.T) net.IP {
	t.Helper()
	ifaces, err := net.Interfaces()
	if err != nil {
		t.Fatalf("interfaces: %v", err)
	}
	for _, iface := range ifaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, _ := iface.Addrs()
		for _, addr := range addrs {
			if ipNet, ok := addr.(*net.IPNet); ok {
				if ip4 := ipNet.IP.To4(); ip4 != nil {
					return ip4
				}
			}
		}
	}
	t.Skip("no non-loopback IPv4 interface")
	return nil
}

func TestTailnetInterfaceIPv4OnlyAcceptsTheCGNATBlock(t *testing.T) {
	if ip := tailnetInterfaceIPv4(func() ([]net.Interface, error) { return nil, net.ErrClosed }); ip != nil {
		t.Fatalf("interface error produced %v", ip)
	}
	if ip := tailnetInterfaceIPv4(func() ([]net.Interface, error) { return nil, nil }); ip != nil {
		t.Fatalf("no interfaces produced %v", ip)
	}
	for raw, want := range map[string]bool{
		"100.64.0.21":   true,
		"100.127.255.9": true,
		"100.128.0.1":   false,
		"192.168.64.2":  false,
		"127.0.0.1":     false,
	} {
		if got := tailnetIPv4Prefix.Contains(netip.MustParseAddr(raw)); got != want {
			t.Fatalf("%s in tailnet prefix = %v, want %v", raw, got, want)
		}
	}
	// Whatever this host has, the result is either nil or a tailnet address.
	if ip := tailnetInterfaceIPv4(net.Interfaces); ip != nil {
		if addr, ok := netip.AddrFromSlice(ip); !ok || !tailnetIPv4Prefix.Contains(addr) {
			t.Fatalf("live lookup returned non-tailnet %v", ip)
		}
	}
}

// The relay on an OS socket must let a client allocate, install a permission
// for the computer's own advertised address, and exchange packets with a
// wildcard-bound local socket (what LiveKit's media port looks like).
func TestTurnRelayRelaysToAWildcardBoundLocalPeer(t *testing.T) {
	ip := localNonLoopbackIPv4(t)
	creds := &turnCredentials{Username: "u", Password: "p", Port: 0, Realm: turnRealm}
	relay, err := startTurnRelay(ip, 0, creds)
	if err != nil {
		t.Fatalf("start relay: %v", err)
	}
	defer relay.Close()
	turnAddr := relay.conn.LocalAddr().String()

	echo, err := net.ListenPacket("udp4", "0.0.0.0:0")
	if err != nil {
		t.Fatalf("echo listen: %v", err)
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

	conn, err := net.ListenPacket("udp4", net.JoinHostPort(ip.String(), "0"))
	if err != nil {
		t.Fatalf("client listen: %v", err)
	}
	defer conn.Close()
	client, err := turn.NewClient(&turn.ClientConfig{
		STUNServerAddr: turnAddr,
		TURNServerAddr: turnAddr,
		Conn:           conn,
		Username:       creds.Username,
		Password:       creds.Password,
		Realm:          turnRealm,
	})
	if err != nil {
		t.Fatalf("turn client: %v", err)
	}
	defer client.Close()
	if err := client.Listen(); err != nil {
		t.Fatalf("client listen: %v", err)
	}
	relayConn, err := client.Allocate()
	if err != nil {
		t.Fatalf("allocate: %v", err)
	}
	defer relayConn.Close()
	if got := relayConn.LocalAddr().(*net.UDPAddr).IP; !got.Equal(ip) {
		t.Fatalf("relay allocated on %v, want the tailnet address %v", got, ip)
	}

	// The peer address the phone would learn from LiveKit's candidate: the
	// advertised (tailnet) address, not the wildcard the socket is bound to.
	peer := &net.UDPAddr{IP: ip, Port: echo.LocalAddr().(*net.UDPAddr).Port}
	payload := []byte("openbase-turn-relay")
	if _, err := relayConn.WriteTo(payload, peer); err != nil {
		t.Fatalf("relay write: %v", err)
	}
	relayConn.SetReadDeadline(time.Now().Add(5 * time.Second))
	buf := make([]byte, 1500)
	n, from, err := relayConn.ReadFrom(buf)
	if err != nil {
		t.Fatalf("relay read: %v", err)
	}
	if !bytes.Equal(buf[:n], payload) || from.String() != peer.String() {
		t.Fatalf("relayed %q from %v, want %q from %v", buf[:n], from, payload, peer)
	}
}
