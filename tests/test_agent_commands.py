"""The `openbase-coder codex|claude` commands (argument routing and fallback)."""

from __future__ import annotations

import importlib

import click
import pytest
from click.testing import CliRunner

from openbase_coder_cli.agent_launch import AgentLaunch
from openbase_coder_cli.agent_remote import (
    LOCAL,
    REMOTE,
    ModeDecision,
    RemoteUnavailableError,
)
from openbase_coder_cli.cli import agents as agents_module
from openbase_coder_cli.cli import main

claude_module = importlib.import_module("openbase_coder_cli.cli.claude")


@pytest.mark.parametrize(
    ("args", "force", "rest"),
    [
        ([], None, []),
        (["--local", "-m", "o3"], LOCAL, ["-m", "o3"]),
        (["--remote", "hi"], REMOTE, ["hi"]),
        (["--local", "--local"], LOCAL, []),
        # Only leading flags are Openbase's; later ones belong to the agent.
        (["resume", "--remote", "unix://"], None, ["resume", "--remote", "unix://"]),
    ],
)
def test_parse_launcher_args(args, force, rest):
    assert agents_module.parse_launcher_args(args) == (force, rest)


def test_parse_launcher_args_rejects_both_modes():
    with pytest.raises(click.UsageError):
        agents_module.parse_launcher_args(["--local", "--remote"])


@pytest.fixture
def launched(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        agents_module,
        "launch_agent",
        lambda agent, args: calls.append((agent, list(args))),
    )
    return calls


def test_codex_command_passes_every_argument_through(launched):
    result = CliRunner().invoke(
        main, ["codex", "--local", "-m", "o3", "--search", "fix it"]
    )

    assert result.exit_code == 0, result.output
    assert launched == [("codex", ["--local", "-m", "o3", "--search", "fix it"])]


def test_codex_help_is_the_launchers(launched):
    result = CliRunner().invoke(main, ["codex", "--help"])

    assert result.exit_code == 0
    assert "Openbase's profile" in result.output
    assert "--remote" in result.output
    assert launched == []


def test_claude_without_a_subcommand_launches(launched):
    result = CliRunner().invoke(main, ["claude", "-c", "--model", "opus"])

    assert result.exit_code == 0, result.output
    assert launched == [("claude", ["-c", "--model", "opus"])]


def test_bare_claude_launches(launched):
    result = CliRunner().invoke(main, ["claude"])

    assert result.exit_code == 0, result.output
    assert launched == [("claude", [])]


def test_claude_subcommands_still_run(launched, monkeypatch):
    monkeypatch.setattr(
        claude_module,
        "verified_claude_auth_status",
        lambda: type("R", (), {"raw_output": "Logged in", "logged_in": True})(),
    )

    result = CliRunner().invoke(main, ["claude", "status"])

    assert result.exit_code == 0, result.output
    assert "Logged in" in result.output
    assert launched == []


def test_claude_help_lists_subcommands_and_launcher_flags(launched):
    result = CliRunner().invoke(main, ["claude", "--help"])

    assert result.exit_code == 0
    assert "status" in result.output and "login" in result.output
    assert "--local" in result.output
    assert launched == []


# --- launch_agent flow -------------------------------------------------------


@pytest.fixture
def flow(monkeypatch, tmp_path):
    state: dict = {"exec": None, "remote": None}
    monkeypatch.chdir(tmp_path)

    def fake_plan(agent, args, cwd, context, *, base_env):
        return AgentLaunch(agent, [f"/opt/bin/{agent}", *args], str(cwd), {}, ("n1",))

    def fake_execve(path, argv, env):
        state["exec"] = argv
        raise SystemExit(0)

    monkeypatch.setattr(agents_module, "plan_agent_launch", fake_plan)
    monkeypatch.setattr(agents_module, "default_launch_context", lambda: None)
    monkeypatch.setattr(agents_module.os, "execve", fake_execve)
    monkeypatch.setattr(agents_module, "read_sync_facts", lambda: None)

    def decide(decision):
        monkeypatch.setattr(agents_module, "select_mode", lambda **kw: decision)

    def remote(result):
        def fake_run_remote(**kwargs):
            state["remote"] = kwargs
            kwargs["on_ready"](["hub-notice"])
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(agents_module, "run_remote", fake_run_remote)

    def token(value):
        import openbase_coder_cli.services.fleet_aggregation as fleet

        monkeypatch.setattr(fleet, "owner_access_token", lambda: value)

    state.update(decide=decide, remote=None, set_remote=remote, token=token)
    return state


def _invoke(agent, args):
    runner = CliRunner()
    return runner.invoke(
        click.Command("x", callback=lambda: agents_module.launch_agent(agent, args)),
        [],
    )


def test_local_mode_execs_the_planned_argv(flow):
    flow["decide"](ModeDecision(LOCAL))

    result = _invoke("codex", ["--local", "hi"])

    assert result.exit_code == 0
    assert flow["exec"] == ["/opt/bin/codex", "hi"]
    assert "n1" in result.output


def test_remote_mode_runs_on_the_hub_with_a_home_relative_cwd(flow, monkeypatch):
    flow["decide"](ModeDecision(REMOTE, hub_url="http://mini:18080"))
    flow["token"]("jwt")
    flow["set_remote"](7)
    monkeypatch.setattr(agents_module, "home_relative", lambda cwd, home: "~/p")

    result = _invoke("claude", ["-c"])

    assert result.exit_code == 7
    assert flow["exec"] is None
    assert flow["remote"]["cwd"] == "~/p"
    assert flow["remote"]["args"] == ["-c"]
    assert flow["remote"]["token"] == "jwt"
    assert "Running on the Openbase Sync hub" in result.output
    assert "Hub: hub-notice" in result.output


def test_remote_refusal_falls_back_to_local(flow):
    flow["decide"](ModeDecision(REMOTE, hub_url="http://mini:18080"))
    flow["token"]("jwt")
    flow["set_remote"](RemoteUnavailableError("~/p does not exist on this computer."))

    result = _invoke("codex", [])

    assert result.exit_code == 0
    assert flow["exec"] == ["/opt/bin/codex"]
    assert "does not exist on this computer. Running locally." in result.output


def test_forced_remote_refusal_is_an_error(flow):
    flow["decide"](ModeDecision(REMOTE, hub_url="http://mini:18080"))
    flow["token"]("jwt")
    flow["set_remote"](RemoteUnavailableError("The hub refused."))

    result = _invoke("codex", ["--remote"])

    assert result.exit_code == 1
    assert flow["exec"] is None
    assert "The hub refused." in result.output


def test_signed_out_edge_runs_locally(flow):
    flow["decide"](ModeDecision(REMOTE, hub_url="http://mini:18080"))
    flow["token"](None)

    result = _invoke("codex", [])

    assert flow["exec"] == ["/opt/bin/codex"]
    assert "Not signed in to Openbase" in result.output


def test_fallback_reason_is_printed(flow):
    flow["decide"](
        ModeDecision(LOCAL, "~/x is not in a synced folder; running locally.")
    )

    result = _invoke("claude", [])

    assert "not in a synced folder" in result.output
    assert flow["exec"] == ["/opt/bin/claude"]


def test_select_mode_errors_become_usage_errors(flow, monkeypatch):
    def refuse(**kwargs):
        raise RemoteUnavailableError("This computer is not an Openbase Sync edge.")

    monkeypatch.setattr(agents_module, "select_mode", refuse)

    result = _invoke("codex", ["--remote"])

    assert result.exit_code == 1
    assert "not an Openbase Sync edge" in result.output
    assert flow["exec"] is None
