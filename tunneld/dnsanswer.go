package main

import (
	"net/netip"

	"golang.org/x/net/dns/dnsmessage"
)

// dnsAnswerAddresses extracts the A and AAAA answers of a raw DNS response.
func dnsAnswerAddresses(raw []byte) ([]string, error) {
	var parser dnsmessage.Parser
	if _, err := parser.Start(raw); err != nil {
		return nil, err
	}
	if err := parser.SkipAllQuestions(); err != nil {
		return nil, err
	}
	addresses := []string{}
	for {
		header, err := parser.AnswerHeader()
		if err == dnsmessage.ErrSectionDone {
			break
		}
		if err != nil {
			return nil, err
		}
		switch header.Type {
		case dnsmessage.TypeA:
			resource, err := parser.AResource()
			if err != nil {
				return nil, err
			}
			addresses = append(addresses, netip.AddrFrom4(resource.A).String())
		case dnsmessage.TypeAAAA:
			resource, err := parser.AAAAResource()
			if err != nil {
				return nil, err
			}
			addresses = append(addresses, netip.AddrFrom16(resource.AAAA).String())
		default:
			if err := parser.SkipAnswer(); err != nil {
				return nil, err
			}
		}
	}
	return addresses, nil
}
