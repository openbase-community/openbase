from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from openbase_coder_cli.services import runners, tunneld


def test_vpn_turn_runner_uses_existing_address_without_enrollment(monkeypatch):
    monkeypatch.setattr(
        runners, "tailnet_media_endpoint", lambda env: ("100.64.0.144", "", "utun9")
    )
    env = {"OPENBASE_CODER_CLI_TAILSCALE_PROVIDER": "netmesh"}
    argv, out = runners.build_openbase_voice_turn(
        env, {"tunneld": "/private/bin/openbase-tunneld"}
    )
    assert argv[:4] == [
        "/private/bin/openbase-tunneld",
        "voice-turn",
        "--bind-ip",
        "100.64.0.144",
    ]
    assert out == env
    assert "--authkey" not in argv and "--hostname" not in argv
    monkeypatch.setattr(runners, "tailnet_media_endpoint", lambda env: None)
    with pytest.raises(RuntimeError, match="Waiting for the system VPN"):
        runners.build_openbase_voice_turn(env, {"tunneld": "tunneld"})
    with pytest.raises(RuntimeError, match="not selected"):
        runners.build_openbase_voice_turn(
            {"OPENBASE_CODER_CLI_TAILSCALE_PROVIDER": "netmesh-tsnet"}, {}
        )


def test_vpn_advertises_only_live_authenticated_relay(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_TAILSCALE_PROVIDER", "netmesh")
    monkeypatch.setattr(tunneld, "OPENBASE_BASE_DIR", tmp_path / ".openbase")
    state = tmp_path / ".openbase" / "voice-turn"
    state.mkdir(parents=True)
    (state / "control.token").write_text("test-control-token")
    (state / "turn.json").write_text('{"username":"stale","password":"stale"}')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/credentials"
            assert self.headers["Authorization"] == "Bearer test-control-token"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"username": "live", "password": "live-secret", "port": 3478}
                ).encode()
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    monkeypatch.setattr(
        tunneld, "VOICE_TURN_LOCAL_API", f"http://127.0.0.1:{server.server_port}"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert tunneld.voice_turn_info()["username"] == "live"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert tunneld.voice_turn_info() is None
