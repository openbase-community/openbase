package main

import (
	"net/netip"
	"testing"

	"golang.org/x/net/dns/dnsmessage"
)

func TestDNSAnswerAddresses(t *testing.T) {
	name := dnsmessage.MustNewName("crm.abcdefghijkl.vpn.obs.so.")
	builder := dnsmessage.NewBuilder(nil, dnsmessage.Header{Response: true})
	builder.EnableCompression()
	if err := builder.StartQuestions(); err != nil {
		t.Fatal(err)
	}
	if err := builder.Question(dnsmessage.Question{Name: name, Type: dnsmessage.TypeA, Class: dnsmessage.ClassINET}); err != nil {
		t.Fatal(err)
	}
	if err := builder.StartAnswers(); err != nil {
		t.Fatal(err)
	}
	header := dnsmessage.ResourceHeader{Name: name, Class: dnsmessage.ClassINET, TTL: 60}
	if err := builder.CNAMEResource(header, dnsmessage.CNAMEResource{CNAME: name}); err != nil {
		t.Fatal(err)
	}
	if err := builder.AResource(header, dnsmessage.AResource{A: netip.MustParseAddr("100.64.0.10").As4()}); err != nil {
		t.Fatal(err)
	}
	if err := builder.AAAAResource(header, dnsmessage.AAAAResource{AAAA: netip.MustParseAddr("fd7a:115c:a1e0::10").As16()}); err != nil {
		t.Fatal(err)
	}
	raw, err := builder.Finish()
	if err != nil {
		t.Fatal(err)
	}
	addresses, err := dnsAnswerAddresses(raw)
	if err != nil {
		t.Fatal(err)
	}
	if len(addresses) != 2 || addresses[0] != "100.64.0.10" || addresses[1] != "fd7a:115c:a1e0::10" {
		t.Fatalf("addresses = %v", addresses)
	}
	if _, err := dnsAnswerAddresses([]byte("junk")); err == nil {
		t.Fatal("junk must not parse")
	}
}
