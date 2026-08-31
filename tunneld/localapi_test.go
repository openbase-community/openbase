package main

import (
	"net/netip"
	"testing"

	"tailscale.com/ipn/ipnstate"
	"tailscale.com/types/key"
)

func TestCanonicalProbeHost(t *testing.T) {
	tests := []struct {
		name    string
		raw     string
		want    string
		wantErr bool
	}{
		{name: "DNS name", raw: " Phone.Example.TS.NET. ", want: "phone.example.ts.net"},
		{name: "IPv4", raw: "100.64.0.10", want: "100.64.0.10"},
		{name: "IPv6", raw: "[fd7a:115c:a1e0::10]", want: "fd7a:115c:a1e0::10"},
		{name: "empty", raw: " ", wantErr: true},
		{name: "URL", raw: "peer.example/path", wantErr: true},
		{name: "authority", raw: "peer.example:8080", wantErr: true},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := canonicalProbeHost(test.raw)
			if test.wantErr {
				if err == nil {
					t.Fatalf("canonicalProbeHost(%q) unexpectedly succeeded", test.raw)
				}
				return
			}
			if err != nil {
				t.Fatalf("canonicalProbeHost(%q): %v", test.raw, err)
			}
			if got != test.want {
				t.Fatalf("canonicalProbeHost(%q) = %q, want %q", test.raw, got, test.want)
			}
		})
	}
}

func TestStatusContainsProbePeer(t *testing.T) {
	var peerKey key.NodePublic
	status := &ipnstate.Status{
		Peer: map[key.NodePublic]*ipnstate.PeerStatus{
			peerKey: {
				DNSName: "phone.example.ts.net.",
				TailscaleIPs: []netip.Addr{
					netip.MustParseAddr("100.64.0.10"),
					netip.MustParseAddr("fd7a:115c:a1e0::10"),
				},
			},
		},
	}

	for _, host := range []string{
		"phone.example.ts.net",
		"100.64.0.10",
		"fd7a:115c:a1e0::10",
	} {
		if !statusContainsProbePeer(status, host) {
			t.Errorf("expected current peer host %q to be allowed", host)
		}
	}
	for _, host := range []string{
		"other.example.ts.net",
		"100.64.0.11",
		"127.0.0.1",
	} {
		if statusContainsProbePeer(status, host) {
			t.Errorf("expected non-peer host %q to be rejected", host)
		}
	}
	if statusContainsProbePeer(nil, "phone.example.ts.net") {
		t.Error("expected a missing status to reject all hosts")
	}
}
