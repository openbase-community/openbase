"""Mode selection, framing and the terminal client of the edge→hub launcher."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from openbase_coder_cli.agent_remote import (
    LOCAL,
    REMOTE,
    RemoteSession,
    RemoteUnavailableError,
    SyncFacts,
    decode_frame,
    home_relative,
    read_sync_facts,
    resize_message,
    select_mode,
    socket_url,
    start_message,
    synced_root_for,
)


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    (home / "Projects" / "app" / "src").mkdir(parents=True)
    (home / "Projects" / "big" / "data").mkdir(parents=True)
    (home / "Scratch").mkdir()
    return home


def _edge(**overrides) -> SyncFacts:
    values = {
        "configured": True,
        "role": "edge",
        "hub_host": "mini.example.ts.net",
        "roots": ({"path": "~/Projects", "ignore": ["/big/data", ".generated"]},),
    }
    values.update(overrides)
    return SyncFacts(**values)


def _select(home, facts, cwd, *, force=None, reachable=True, interactive=True, **kw):
    probes: list[str] = []

    def probe(url: str) -> bool:
        probes.append(url)
        return reachable

    decision = select_mode(
        force=force,
        facts=facts,
        cwd=cwd,
        home=home,
        interactive=interactive,
        probe=probe,
        environ=kw.pop("environ", {}),
        platform=kw.pop("platform", "darwin"),
    )
    return decision, probes


# --- mode selection --------------------------------------------------------


def test_unpaired_computer_runs_locally_without_a_word(home):
    decision, probes = _select(home, SyncFacts(configured=False), home / "Projects")

    assert decision.mode == LOCAL
    assert decision.reason is None
    assert probes == []


def test_hub_itself_runs_locally_without_a_word(home):
    decision, probes = _select(home, _edge(role="hub"), home / "Projects" / "app")

    assert decision.mode == LOCAL
    assert decision.reason is None
    assert probes == []


def test_edge_in_a_synced_folder_runs_on_the_reachable_hub(home):
    decision, probes = _select(home, _edge(), home / "Projects" / "app" / "src")

    assert decision.mode == REMOTE
    assert decision.hub_url == "http://mini.example.ts.net:18080"
    assert probes == ["http://mini.example.ts.net:18080"]


def test_edge_outside_synced_folders_runs_locally_and_says_why(home):
    decision, probes = _select(home, _edge(), home / "Scratch")

    assert decision.mode == LOCAL
    assert decision.reason == "~/Scratch is not in a synced folder; running locally."
    assert probes == []


@pytest.mark.parametrize("sub", [("big", "data"), ("app", ".generated")])
def test_edge_in_an_ignored_subfolder_runs_locally(home, sub):
    cwd = home / "Projects" / Path(*sub)
    cwd.mkdir(parents=True, exist_ok=True)

    decision, _ = _select(home, _edge(), cwd)

    assert decision.mode == LOCAL
    assert "not in a synced folder" in decision.reason


def test_edge_with_unreachable_hub_runs_locally_and_says_why(home):
    decision, _ = _select(home, _edge(), home / "Projects", reachable=False)

    assert decision.mode == LOCAL
    assert decision.reason == (
        "The hub (mini.example.ts.net) is unreachable; running locally."
    )


def test_force_local_skips_everything(home):
    decision, probes = _select(home, _edge(), home / "Projects", force=LOCAL)

    assert decision.mode == LOCAL
    assert probes == []


@pytest.mark.parametrize(
    ("facts", "cwd_parts", "reachable"),
    [
        (SyncFacts(configured=False), ("Projects",), True),
        (_edge(), ("Scratch",), True),
        (_edge(), ("Projects",), False),
    ],
)
def test_force_remote_fails_instead_of_falling_back(home, facts, cwd_parts, reachable):
    with pytest.raises(RemoteUnavailableError):
        _select(
            home, facts, home.joinpath(*cwd_parts), force=REMOTE, reachable=reachable
        )


def test_non_interactive_invocations_stay_local(home):
    decision, probes = _select(home, _edge(), home / "Projects", interactive=False)

    assert decision.mode == LOCAL
    assert probes == []


def test_hub_url_override(home):
    decision, probes = _select(
        home,
        _edge(),
        home / "Projects",
        environ={"OPENBASE_HUB_URL": "http://127.0.0.1:9999/"},
    )

    assert decision.hub_url == "http://127.0.0.1:9999"
    assert probes == ["http://127.0.0.1:9999"]


def test_windows_edges_run_locally(home):
    decision, _ = _select(home, _edge(), home / "Projects", platform="win32")

    assert decision.mode == LOCAL
    assert "POSIX" in decision.reason


# --- sync facts and paths --------------------------------------------------


def test_read_sync_facts_parses_role_hub_and_roots(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        'device_id = "laptop"\nrole = "edge"\n'
        'peer_hot = "mini.example.ts.net:22100"\n\n'
        '[[roots]]\nid = "projects"\npath = "~/Projects"\n'
        'ignore = ["/big/data"]\n\n'
        '[[roots]]\nid = "skills"\npath = "~/.agents/skills"\n'
    )

    facts = read_sync_facts(config)

    assert facts.configured is True
    assert facts.role == "edge"
    assert facts.hub_host == "mini.example.ts.net"
    assert facts.roots == (
        {"path": "~/Projects", "ignore": ["/big/data"]},
        {"path": "~/.agents/skills", "ignore": []},
    )


def test_read_sync_facts_handles_ipv6_and_missing_files(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('role = "edge"\npeer_hot = "[fd7a::1]:22100"\n')

    assert read_sync_facts(config).hub_host == "fd7a::1"
    assert read_sync_facts(tmp_path / "missing.toml").configured is False


def test_synced_root_for_matches_the_root_itself_and_children(home):
    roots = ({"path": "~/Projects", "ignore": []},)

    assert synced_root_for(home / "Projects", roots, home) is not None
    assert synced_root_for(home / "Projects" / "app", roots, home) is not None
    assert synced_root_for(home / "Scratch", roots, home) is None


def test_home_relative_paths_are_portable_between_computers(home, tmp_path):
    assert home_relative(home / "Projects" / "app", home) == "~/Projects/app"
    assert home_relative(home, home) == "~"
    outside = tmp_path / "srv"
    outside.mkdir()
    assert home_relative(outside, home) == os.path.realpath(outside)


# --- framing ---------------------------------------------------------------


def test_client_control_frames():
    assert json.loads(start_message("codex", "~/p", ["-m", "x"], 100, 30)) == {
        "type": "start",
        "agent": "codex",
        "cwd": "~/p",
        "args": ["-m", "x"],
        "cols": 100,
        "rows": 30,
    }
    assert json.loads(resize_message(80, 24)) == {
        "type": "resize",
        "cols": 80,
        "rows": 24,
    }


def test_decode_server_frames():
    assert decode_frame(b"\x1b[2Jhi") == ("output", b"\x1b[2Jhi")
    assert decode_frame('{"type": "ready", "data": {"id": "agent-1"}}') == (
        "ready",
        {"id": "agent-1"},
    )
    assert decode_frame('{"type": "exit", "data": {"code": 3}}') == (
        "exit",
        {"code": 3},
    )
    assert decode_frame("not json")[0] == "unknown"


def test_socket_url_switches_scheme_and_carries_the_token():
    assert (
        socket_url("http://hub:18080", "/ws/agent-terminals/", "tok", cols=80)
        == "ws://hub:18080/ws/agent-terminals/?token=tok&cols=80"
    )
    assert socket_url("https://hub", "/x/", "t").startswith("wss://hub/x/?token=t")


# --- terminal client against a fake hub -------------------------------------


class FakeHub:
    """Speaks the hub side of ws/agent-terminals/ on a loopback port."""

    def __init__(self, *, refuse: str | None = None, drop_after_first: bool = False):
        self.refuse = refuse
        self.drop_after_first = drop_after_first
        self.paths: list[str] = []
        self.starts: list[dict] = []
        self.resizes: list[dict] = []
        self.inputs: list[bytes] = []
        self.dropped = False

    async def handler(self, ws):
        path = ws.request.path
        self.paths.append(path)
        if path.startswith("/ws/agent-terminals/?"):
            start = json.loads(await ws.recv())
            self.starts.append(start)
            if self.refuse:
                await ws.send(
                    json.dumps({"type": "error", "data": {"message": self.refuse}})
                )
                return
        await ws.send(
            json.dumps(
                {"type": "ready", "data": {"id": "agent-abc", "notices": ["hello"]}}
            )
        )
        async for frame in ws:
            if isinstance(frame, str):
                self.resizes.append(json.loads(frame))
                continue
            self.inputs.append(frame)
            if frame == b"q":
                await ws.send(json.dumps({"type": "exit", "data": {"code": 3}}))
                return
            await ws.send(b"echo:" + frame)
            if self.drop_after_first and not self.dropped:
                self.dropped = True
                return  # connection lost; the PTY "keeps running"


async def _run_client(hub: FakeHub, keys: list[bytes]):
    async with serve(hub.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        os.set_blocking(stdout_r, False)
        session = RemoteSession(
            base_url=f"http://127.0.0.1:{port}",
            token="tok",
            agent="codex",
            cwd="~/Projects/app",
            args=["fix"],
            stdin_fd=stdin_r,
            stdout_fd=stdout_w,
        )
        try:
            notices = await session.start()
            runner = asyncio.ensure_future(session.run())
            for key in keys:
                os.write(stdin_w, key)
                await asyncio.sleep(0.3)
            code = await asyncio.wait_for(runner, 15)
            output = b""
            while True:
                try:
                    chunk = os.read(stdout_r, 65536)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                output += chunk
            return session, notices, code, output
        finally:
            for fd in (stdin_r, stdin_w, stdout_r, stdout_w):
                os.close(fd)


async def test_client_starts_pumps_input_and_returns_the_exit_code():
    hub = FakeHub()

    session, notices, code, output = await _run_client(hub, [b"ab", b"\x03", b"q"])

    assert notices == ["hello"]
    assert session.session_id == "agent-abc"
    assert hub.starts[0]["agent"] == "codex"
    assert hub.starts[0]["cwd"] == "~/Projects/app"
    assert hub.starts[0]["args"] == ["fix"]
    assert hub.paths[0].startswith("/ws/agent-terminals/?token=tok")
    # Ctrl-C travels as a byte, not a signal.
    assert hub.inputs == [b"ab", b"\x03", b"q"]
    assert b"echo:ab" in output and b"echo:\x03" in output
    assert code == 3


async def test_client_reattaches_to_the_same_session_without_replay():
    hub = FakeHub(drop_after_first=True)

    _, _, code, output = await _run_client(hub, [b"one", b"two", b"q"])

    assert code == 3
    assert len(hub.paths) == 2
    assert hub.paths[1].startswith("/ws/agent-terminals/agent-abc/?token=tok")
    assert "replay=0" in hub.paths[1]
    assert b"echo:one" in output
    assert b"echo:two" in output


async def test_client_reports_a_refused_start():
    hub = FakeHub(refuse="~/Projects/app does not exist on this computer.")

    with pytest.raises(RemoteUnavailableError, match="does not exist"):
        await _run_client(hub, [])


async def test_client_reports_an_unreachable_hub():
    session = RemoteSession(
        base_url="http://127.0.0.1:9", token="t", agent="claude", cwd="~", args=[]
    )

    with pytest.raises(RemoteUnavailableError):
        await session.start()
