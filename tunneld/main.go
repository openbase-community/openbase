// Command openbase-tunneld embeds a Tailscale node (tsnet) inside the
// Openbase Coder runtime so users no longer need the Tailscale app.
//
// It replaces the two `tailscale serve` routes configured by the CLI today:
//
//	tailnet :18080 (HTTP)  -> 127.0.0.1:7999  (Openbase Coder Django API)
//	tailnet :7880  (TCP)   -> 127.0.0.1:7880  (LiveKit signaling)
//
// and exposes a loopback control API on 127.0.0.1:7998 that the Python CLI
// uses instead of shelling out to the `tailscale` binary:
//
//	GET /status  -> ipnstate.Status JSON (same schema as `tailscale status --json`)
//	GET /health  -> daemon + forward health summary
//	GET /probe   -> dial a tailnet peer through the embedded node
//
// Subcommands:
//
//	openbase-tunneld serve   run the desktop daemon (default)
//	openbase-tunneld peer    join as a second node and fetch a URL over the
//	                         tailnet (simulates the phone; used for e2e tests)
package main

import (
	"fmt"
	"os"
)

func main() {
	args := os.Args[1:]
	cmd := "serve"
	if len(args) > 0 && args[0] != "" && args[0][0] != '-' {
		cmd = args[0]
		args = args[1:]
	}

	var err error
	switch cmd {
	case "serve":
		err = runServe(args)
	case "peer":
		err = runPeer(args)
	case "voicetest":
		err = runVoiceTest(args)
	default:
		err = fmt.Errorf("unknown subcommand %q (expected \"serve\", \"peer\", or \"voicetest\")", cmd)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "openbase-tunneld:", err)
		os.Exit(1)
	}
}
