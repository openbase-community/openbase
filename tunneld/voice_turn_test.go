package main

import (
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"sync/atomic"
	"testing"
	"time"

	"github.com/pion/turn/v4"
)

func TestVPNBindRejectsExposureAndUnassignedAddresses(t *testing.T) {
	assigned := []net.Addr{&net.IPNet{IP: net.ParseIP("100.64.0.144"), Mask: net.CIDRMask(32, 32)}}
	for _, ip := range []string{"0.0.0.0", "127.0.0.1", "192.168.64.2", "8.8.8.8", "::", "100.64.0.145"} {
		if validateVPNBind(net.ParseIP(ip), assigned) == nil {
			t.Errorf("accepted unsafe/unassigned bind %s", ip)
		}
	}
	if err := validateVPNBind(net.ParseIP("100.64.0.144"), assigned); err != nil {
		t.Fatal(err)
	}
}

// Real UDP/TURN allocation and packet exchange, with no mocked transport.
func TestVPNVoiceTURNAuthenticatedLoopbackRoundTrip(t *testing.T) {
	state := t.TempDir()
	creds, err := loadOrCreateTurnCredentials(state)
	if err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(state + "/turn.json")
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatal("credentials must be private")
	}
	serverConn, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server, err := startTURNOnPacketConn(serverConn, creds)
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	clientConn, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer clientConn.Close()
	client, err := turn.NewClient(&turn.ClientConfig{STUNServerAddr: serverConn.LocalAddr().String(), TURNServerAddr: serverConn.LocalAddr().String(), Conn: clientConn, Username: creds.Username, Password: creds.Password, Realm: turnRealm})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	if err := client.Listen(); err != nil {
		t.Fatal(err)
	}
	allocation, err := client.Allocate()
	if err != nil {
		t.Fatal(err)
	}
	defer allocation.Close()
	if !allocation.LocalAddr().(*net.UDPAddr).IP.Equal(net.IPv4(127, 0, 0, 1)) {
		t.Fatal("relay allocation escaped loopback")
	}
	peer, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer peer.Close()
	_ = peer.SetDeadline(time.Now().Add(3 * time.Second))
	if _, err := allocation.WriteTo([]byte("voice-out"), peer.LocalAddr()); err != nil {
		t.Fatal(err)
	}
	buffer := make([]byte, 32)
	n, addr, err := peer.ReadFrom(buffer)
	if err != nil || string(buffer[:n]) != "voice-out" {
		t.Fatalf("outbound: %q %v", buffer[:n], err)
	}
	if _, err := peer.WriteTo([]byte("voice-in"), addr); err != nil {
		t.Fatal(err)
	}
	_ = allocation.SetReadDeadline(time.Now().Add(3 * time.Second))
	n, _, err = allocation.ReadFrom(buffer)
	if err != nil || string(buffer[:n]) != "voice-in" {
		t.Fatalf("inbound: %q %v", buffer[:n], err)
	}
	if err := client.CreatePermission(&net.UDPAddr{IP: net.ParseIP("8.8.8.8"), Port: 53}); err == nil {
		t.Fatal("relay accepted non-local peer")
	}
}

func TestVoiceTURNControlRequiresAuthAndLiveInterface(t *testing.T) {
	creds := &turnCredentials{Username: "test", Password: "test-secret", Port: 3478}
	var live atomic.Bool
	live.Store(true)
	server := httptest.NewServer(voiceTURNControl(creds, "test-control", func() bool { return live.Load() }))
	defer server.Close()
	response, err := http.Get(server.URL + "/credentials")
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != 401 {
		t.Fatal("anonymous caller received credentials")
	}
	for _, ready := range []bool{true, false} {
		live.Store(ready)
		req, _ := http.NewRequest("GET", server.URL+"/credentials", nil)
		req.Header.Set("Authorization", "Bearer test-control")
		response, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
		expected := 200
		if !ready {
			expected = 503
		}
		if response.StatusCode != expected {
			t.Fatalf("ready=%v status=%d", ready, response.StatusCode)
		}
	}
}
