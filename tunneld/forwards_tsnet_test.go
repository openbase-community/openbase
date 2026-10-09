package main

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	"tailscale.com/derp/derpserver"
	"tailscale.com/ipn/ipnstate"
	"tailscale.com/ipn/store/mem"
	"tailscale.com/net/netns"
	"tailscale.com/tailcfg"
	"tailscale.com/tsnet"
	"tailscale.com/tstest/integration/testcontrol"
	"tailscale.com/types/key"
	"tailscale.com/types/logger"
)

func TestForwardWithTsnetPeers(t *testing.T) {
	t.Setenv("TS_NO_LOGS_NO_SUPPORT", "true")
	netns.SetEnabled(false)
	t.Cleanup(func() { netns.SetEnabled(true) })
	relay := derpserver.New(key.NewNode(), logger.Discard)
	t.Cleanup(func() { relay.Close() })
	relayServer := httptest.NewTLSServer(derpserver.Handler(relay))
	t.Cleanup(relayServer.Close)
	control := &testcontrol.Server{
		DERPMap: &tailcfg.DERPMap{Regions: map[int]*tailcfg.DERPRegion{
			1: {RegionID: 1, RegionCode: "test", Nodes: []*tailcfg.DERPNode{
				{
					Name: "test", RegionID: 1, HostName: "127.0.0.1", IPv4: "127.0.0.1", IPv6: "none",
					STUNPort: -1, DERPPort: relayServer.Listener.Addr().(*net.TCPAddr).Port, InsecureForTests: true,
				},
			}},
		}},
		AllNodesSameUser: true,
		Logf:             logger.Discard,
	}
	control.HTTPTestServer = httptest.NewServer(control)
	t.Cleanup(control.HTTPTestServer.Close)
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	defer cancel()
	startNode := func(hostname string) (*tsnet.Server, *ipnstate.Status) {
		t.Helper()
		node := &tsnet.Server{
			Dir: t.TempDir(), Store: new(mem.Store), Ephemeral: true,
			ControlURL: control.HTTPTestServer.URL, Hostname: hostname,
			Logf: logger.Discard, UserLogf: logger.Discard,
		}
		t.Cleanup(func() { node.Close() })
		status, err := node.Up(ctx)
		if err != nil {
			t.Fatal(err)
		}
		return node, status
	}
	workspace, workspaceStatus := startNode("forward-workspace")
	phone, phoneStatus := startNode("forward-phone")
	localClient, err := workspace.LocalClient()
	if err != nil {
		t.Fatal(err)
	}
	manager := newForwardManager(workspace, localClient)
	t.Cleanup(manager.CloseAll)
	target := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		io.WriteString(writer, request.URL.RequestURI())
	}))
	t.Cleanup(target.Close)
	port := target.Listener.Addr().(*net.TCPAddr).Port
	transport := &http.Transport{DialContext: phone.Dial, DisableKeepAlives: true}
	t.Cleanup(transport.CloseIdleConnections)
	client := &http.Client{Transport: transport, Timeout: 5 * time.Second}
	url := "http://" + net.JoinHostPort(workspaceStatus.TailscaleIPs[0].String(), strconv.Itoa(port)) + "/callback?code=test&state=test"
	request := func() error {
		response, err := client.Get(url)
		if err != nil {
			return err
		}
		defer response.Body.Close()
		body, err := io.ReadAll(response.Body)
		if err == nil && string(body) != "/callback?code=test&state=test" {
			t.Fatalf("callback changed: %q", body)
		}
		return err
	}
	for _, peer := range []string{string(phoneStatus.Self.ID), phoneStatus.TailscaleIPs[0].String()} {
		if _, err := manager.Add(forwardRequest{Port: port, Peer: peer, OneShot: true}); err != nil {
			t.Fatal(err)
		}
		if err := request(); err != nil {
			t.Fatalf("pinned callback from %s: %v", peer, err)
		}
		deadline := time.Now().Add(time.Second)
		for len(manager.List()) != 0 && time.Now().Before(deadline) {
			time.Sleep(time.Millisecond)
		}
		if len(manager.List()) != 0 {
			t.Fatal("one-shot listener did not retire")
		}
	}
	if _, err := manager.Add(forwardRequest{Port: port, Peer: string(workspaceStatus.Self.ID)}); err != nil {
		t.Fatal(err)
	}
	if err := request(); err == nil {
		t.Fatal("a different tsnet peer bypassed the pin")
	}
	manager.Remove(port)
	if _, err := manager.Add(forwardRequest{Port: port, TTLSeconds: 1}); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for len(manager.List()) != 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if len(manager.List()) != 0 {
		t.Fatal("tsnet listener did not expire")
	}
}
