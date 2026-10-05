from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

from openbase_coder_cli.services import launchctl_tools


def _write_plist(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(payload, handle)


def test_list_launchctl_services_payload_reads_launch_agents(
    tmp_path: Path, monkeypatch
) -> None:
    launch_agents_dir = tmp_path / "Library" / "LaunchAgents"
    plist_path = launch_agents_dir / "com.example.worker.plist"
    _write_plist(
        plist_path,
        {
            "Label": "com.example.worker",
            "ProgramArguments": ["/usr/bin/python3", "-m", "example"],
            "WorkingDirectory": "/tmp/example",
            "RunAtLoad": True,
            "KeepAlive": True,
        },
    )

    monkeypatch.setattr(launchctl_tools, "LAUNCH_AGENTS_DIR", launch_agents_dir)
    monkeypatch.setattr(
        launchctl_tools,
        "_run_launchctl",
        lambda *args, check=True: subprocess.CompletedProcess(
            ["launchctl", *args],
            0,
            "PID\tStatus\tLabel\n123\t0\tcom.example.worker\n",
            "",
        ),
    )

    payload = launchctl_tools.list_launchctl_services_payload()

    assert payload["error"] is None
    assert len(payload["services"]) == 1
    service = payload["services"][0]
    assert service["label"] == "com.example.worker"
    assert service["loaded"] is True
    assert service["running"] is True
    assert service["pid"] == 123
    assert service["command"] == "/usr/bin/python3 -m example"
    assert service["working_directory"] == "/tmp/example"
    assert service["run_at_load"] is True
    assert service["keep_alive"] is True


def test_start_action_bootstraps_unloaded_launch_agent(
    tmp_path: Path, monkeypatch
) -> None:
    launch_agents_dir = tmp_path / "Library" / "LaunchAgents"
    plist_path = launch_agents_dir / "com.example.worker.plist"
    _write_plist(
        plist_path,
        {
            "Label": "com.example.worker",
            "ProgramArguments": ["/usr/bin/python3", "-m", "example"],
        },
    )

    monkeypatch.setattr(launchctl_tools, "LAUNCH_AGENTS_DIR", launch_agents_dir)
    monkeypatch.setattr(launchctl_tools.os, "getuid", lambda: 501)

    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        calls.append(args)
        if args == ("list",):
            return subprocess.CompletedProcess(
                ["launchctl", *args], 0, "PID\tStatus\tLabel\n", ""
            )
        return subprocess.CompletedProcess(["launchctl", *args], 0, "", "")

    monkeypatch.setattr(launchctl_tools, "_run_launchctl", fake_run)

    launchctl_tools.run_launchctl_service_action("com.example.worker", "start")

    assert calls == [
        ("list",),
        ("bootstrap", "gui/501", str(plist_path)),
    ]


def test_restart_action_restarts_loaded_launch_agent_in_place(
    tmp_path: Path, monkeypatch
) -> None:
    launch_agents_dir = tmp_path / "Library" / "LaunchAgents"
    plist_path = launch_agents_dir / "com.example.worker.plist"
    _write_plist(
        plist_path,
        {
            "Label": "com.example.worker",
            "ProgramArguments": ["/usr/bin/python3", "-m", "example"],
        },
    )

    monkeypatch.setattr(launchctl_tools, "LAUNCH_AGENTS_DIR", launch_agents_dir)
    monkeypatch.setattr(launchctl_tools.os, "getuid", lambda: 501)
    monkeypatch.setattr(launchctl_tools.process_utils.time, "sleep", lambda _s: None)

    calls: list[tuple[str, ...]] = []
    state = {"pid": 456}

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        calls.append(args)
        if args[0] == "kill":
            # KeepAlive respawns the job under a new pid once the old one exits.
            state["pid"] = 789
        if args == ("list",):
            return subprocess.CompletedProcess(
                ["launchctl", *args],
                0,
                f"PID\tStatus\tLabel\n{state['pid']}\t0\tcom.example.worker\n",
                "",
            )
        return subprocess.CompletedProcess(["launchctl", *args], 0, "", "")

    monkeypatch.setattr(launchctl_tools, "_run_launchctl", fake_run)

    launchctl_tools.run_launchctl_service_action("com.example.worker", "restart")

    # Never bootout + bootstrap: that re-registers the background item and
    # macOS announces it again. Terminate, wait for the pid to change, then
    # kickstart (a no-op for the already-respawned job).
    actions = [c for c in calls if c != ("list",)]
    assert actions == [
        ("kill", "SIGTERM", "gui/501/com.example.worker"),
        ("kickstart", "gui/501/com.example.worker"),
    ]


def test_restart_action_force_kickstarts_when_sigterm_is_ignored(
    tmp_path: Path, monkeypatch
) -> None:
    launch_agents_dir = tmp_path / "Library" / "LaunchAgents"
    _write_plist(
        launch_agents_dir / "com.example.worker.plist",
        {"Label": "com.example.worker", "ProgramArguments": ["/usr/bin/true"]},
    )
    monkeypatch.setattr(launchctl_tools, "LAUNCH_AGENTS_DIR", launch_agents_dir)
    monkeypatch.setattr(launchctl_tools.os, "getuid", lambda: 501)
    monkeypatch.setattr(launchctl_tools, "RESTART_EXIT_TIMEOUT_SECONDS", 0.0)

    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        calls.append(args)
        if args == ("list",):
            return subprocess.CompletedProcess(
                ["launchctl", *args],
                0,
                "PID\tStatus\tLabel\n456\t0\tcom.example.worker\n",
                "",
            )
        return subprocess.CompletedProcess(["launchctl", *args], 0, "", "")

    monkeypatch.setattr(launchctl_tools, "_run_launchctl", fake_run)

    launchctl_tools.run_launchctl_service_action("com.example.worker", "restart")

    actions = [c for c in calls if c != ("list",)]
    assert actions == [
        ("kill", "SIGTERM", "gui/501/com.example.worker"),
        ("kickstart", "-k", "gui/501/com.example.worker"),
    ]


def test_livekit_launchctl_restart_warns_before_action(
    tmp_path: Path, monkeypatch
) -> None:
    launch_agents_dir = tmp_path / "Library" / "LaunchAgents"
    label = "com.openbase.coder.livekit-agent"
    plist_path = launch_agents_dir / f"{label}.plist"
    warnings = []
    _write_plist(
        plist_path,
        {
            "Label": label,
            "ProgramArguments": [
                "/usr/bin/python3",
                "-m",
                "openbase_coder_cli.livekit_agent.livekit",
            ],
        },
    )

    monkeypatch.setattr(launchctl_tools, "LAUNCH_AGENTS_DIR", launch_agents_dir)
    monkeypatch.setattr(launchctl_tools.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        launchctl_tools,
        "warn_before_voice_interruption",
        lambda **kwargs: warnings.append(kwargs),
    )

    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        calls.append(args)
        if args == ("list",):
            return subprocess.CompletedProcess(
                ["launchctl", *args],
                0,
                f"PID\tStatus\tLabel\n456\t0\t{label}\n",
                "",
            )
        return subprocess.CompletedProcess(["launchctl", *args], 0, "", "")

    monkeypatch.setattr(launchctl_tools, "_run_launchctl", fake_run)

    launchctl_tools.run_launchctl_service_action(label, "restart")

    assert warnings == [
        {
            "reason": f"launchctl restart {label}",
            "emit_cli_warning": False,
        }
    ]
    assert calls[0] == ("list",)


def test_non_livekit_launchctl_restart_does_not_warn(
    tmp_path: Path, monkeypatch
) -> None:
    launch_agents_dir = tmp_path / "Library" / "LaunchAgents"
    label = "com.example.worker"
    plist_path = launch_agents_dir / f"{label}.plist"
    warnings = []
    _write_plist(
        plist_path,
        {
            "Label": label,
            "ProgramArguments": ["/usr/bin/python3", "-m", "example"],
        },
    )

    monkeypatch.setattr(launchctl_tools, "LAUNCH_AGENTS_DIR", launch_agents_dir)
    monkeypatch.setattr(launchctl_tools.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        launchctl_tools,
        "warn_before_voice_interruption",
        lambda **kwargs: warnings.append(kwargs),
    )

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        if args == ("list",):
            return subprocess.CompletedProcess(
                ["launchctl", *args],
                0,
                f"PID\tStatus\tLabel\n456\t0\t{label}\n",
                "",
            )
        return subprocess.CompletedProcess(["launchctl", *args], 0, "", "")

    monkeypatch.setattr(launchctl_tools, "_run_launchctl", fake_run)

    launchctl_tools.run_launchctl_service_action(label, "restart")

    assert warnings == []
