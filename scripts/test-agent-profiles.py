"""Paid integration checks against the installed Codex daemon and Claude login.

Run from the CLI checkout: uv run python scripts/test-agent-profiles.py --paid
This creates ephemeral Codex threads and non-persistent Claude turns. No mocks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from super_agents.app_endpoint import open_app_server_connection
from super_agents.app_server_client import CodexAppServerClient
from super_agents.claude_options import agent_options

from openbase_coder_cli.agent_profiles import profile_environment
from openbase_coder_cli.codex_control_plane import managed_codex_app_server_endpoint
from openbase_coder_cli.paths import CLAUDE_SETTINGS_PATH

PROMPT = "Reply exactly PROFILE_OK. Do not use tools."


class Probe:
    def __init__(self, connection):
        self.connection = connection
        self.counter = 0
        self.events = []

    async def receive(self):
        event = json.loads(await asyncio.wait_for(self.connection.recv(), 120))
        if "method" in event:
            self.events.append(event)
        return event

    async def request(self, method, params):
        self.counter += 1
        await self.connection.send(
            json.dumps({"id": self.counter, "method": method, "params": params})
        )
        while True:
            event = await self.receive()
            if event.get("id") == self.counter:
                assert "error" not in event, event.get("error")
                return event["result"]

    async def turn(self, cwd, config, label):
        self.events = []
        thread = await self.request(
            "thread/start", {"cwd": cwd, "ephemeral": True, "config": config}
        )
        thread_id = thread["thread"]["id"]
        inventory = await self.request(
            "mcpServerStatus/list", {"threadId": thread_id, "limit": 100}
        )
        names = [entry["name"] for entry in inventory["data"]]
        assert ("super-agents" in names) == bool(config), names
        await self.request(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": PROMPT}]},
        )
        while True:
            event = await self.receive()
            if (
                event.get("method") == "turn/completed"
                and event["params"]["threadId"] == thread_id
            ):
                assert event["params"]["turn"]["status"] == "completed", event[
                    "params"
                ]["turn"].get("error")
                break
        messages = [
            e["params"]["item"]["text"]
            for e in self.events
            if e.get("method") == "item/completed"
            and e["params"]["item"]["type"] == "agentMessage"
        ]
        assert "PROFILE_OK" in "\n".join(messages), messages
        usage = [
            e["params"]["tokenUsage"]["last"]
            for e in self.events
            if e.get("method") == "thread/tokenUsage/updated"
        ]
        assert usage and usage[-1]["inputTokens"] > 0
        hooks = [
            e["params"] for e in self.events if e.get("method") == "hook/completed"
        ]
        print(
            json.dumps(
                {
                    "case": label,
                    "model": thread["model"],
                    "effort": thread.get("reasoningEffort"),
                    "usage": usage[-1],
                    "hooks": [
                        {
                            "source": Path(h["run"]["sourcePath"]).name,
                            "status": h["run"]["status"],
                        }
                        for h in hooks
                    ],
                }
            ),
            flush=True,
        )
        return thread


async def codex_checks(cwd):
    endpoint = managed_codex_app_server_endpoint()
    connection = await open_app_server_connection(endpoint, max_size=None)
    try:
        probe = Probe(connection)
        await probe.request(
            "initialize",
            {
                "clientInfo": {"name": "openbase_profile_test", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await connection.send(json.dumps({"method": "initialized", "params": {}}))
        baseline = (await probe.request("config/read", {"includeLayers": True}))[
            "config"
        ]
        assert "super-agents" not in baseline.get("mcp_servers", {})
        # Exercise the actual Super Agents profile + shell environment adapter.
        client = CodexAppServerClient(endpoint=endpoint.value)
        config = await client._login_shell_config_override()
        assert "super-agents" in config["mcp_servers"]
        profiled = await probe.turn(cwd, config, "codex-profile")
        assert profiled["model"] == config["model"]
        assert profiled["reasoningEffort"] == config["model_reasoning_effort"]
        ordinary = await probe.turn(cwd, {}, "codex-normal-after-profile")
        assert ordinary["model"] == baseline["model"]
        assert ordinary["reasoningEffort"] == baseline["model_reasoning_effort"]
        after = (await probe.request("config/read", {"includeLayers": True}))["config"]
        # Codex records newly visited working directories in projects. That
        # bookkeeping is independent of configuration isolation.
        assert {k: v for k, v in baseline.items() if k != "projects"} == {
            k: v for k, v in after.items() if k != "projects"
        }
    finally:
        await connection.close()


def assert_claude_model(actual, requested):
    if not requested:
        return
    requested = requested.removesuffix("[1m]")
    if requested in {"sonnet", "opus", "haiku", "fable"}:
        assert actual.startswith(f"claude-{requested}-"), (actual, requested)
    else:
        assert actual.removesuffix("[1m]") == requested, (actual, requested)


async def claude_checks(cwd):
    import claude_agent_sdk as sdk

    options = agent_options(sdk, cwd, None, None, resume=None, backend="claude_code")
    assert (
        options.settings == profile_environment()["SUPER_AGENTS_CLAUDE_SETTINGS_PATH"]
    )
    assert "super-agents" in options.mcp_servers
    options.extra_args = {"no-session-persistence": None}
    expected_profile = json.loads(Path(options.settings).read_text())
    expected_normal = (
        json.loads(CLAUDE_SETTINGS_PATH.read_text())
        if CLAUDE_SETTINGS_PATH.exists()
        else {}
    )
    result = None
    initialized = False
    async with asyncio.timeout(120):
        async for message in sdk.query(prompt=PROMPT, options=options):
            if isinstance(message, sdk.SystemMessage) and message.subtype == "init":
                initialized = True
                assert_claude_model(
                    message.data["model"], expected_profile.get("model")
                )
                assert any(
                    server["name"] == "super-agents" and server["status"] == "connected"
                    for server in message.data.get("mcp_servers", [])
                )
                print(
                    json.dumps(
                        {
                            "case": "claude-sdk-profile-init",
                            "model": message.data.get("model"),
                            "mcp": message.data.get("mcp_servers"),
                        }
                    ),
                    flush=True,
                )
            if isinstance(message, sdk.ResultMessage):
                result = message
    assert initialized and result and not result.is_error
    assert "PROFILE_OK" in (result.result or "")
    assert (
        result.usage["input_tokens"] + result.usage.get("cache_read_input_tokens", 0)
        > 0
    )
    print(
        json.dumps(
            {
                "case": "claude-sdk-profile",
                "usage": result.usage,
                "cost_usd": result.total_cost_usd,
            }
        ),
        flush=True,
    )
    # A fresh ordinary CLI invocation does not receive the Openbase settings
    # or MCP layer, even when launched by the test process.
    normal_env = {
        key: value
        for key, value in os.environ.items()
        if key not in profile_environment()
    }
    process = await asyncio.create_subprocess_exec(
        "claude",
        "--print",
        "--output-format",
        "json",
        "--no-session-persistence",
        PROMPT,
        cwd=cwd,
        env=normal_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 120)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, stderr.decode()
    ordinary = json.loads(stdout)
    assert not ordinary["is_error"] and "PROFILE_OK" in ordinary["result"]
    assert ordinary["modelUsage"]
    for model in ordinary["modelUsage"]:
        assert_claude_model(model, expected_normal.get("model"))
    print(
        json.dumps(
            {
                "case": "claude-normal-after-profile",
                "models": list(ordinary["modelUsage"]),
                "usage": ordinary["usage"],
                "cost_usd": ordinary["total_cost_usd"],
            }
        ),
        flush=True,
    )


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paid", action="store_true", help="Run real paid Codex and Claude turns"
    )
    args = parser.parse_args()
    if not args.paid:
        parser.error("--paid is required because this test consumes model credits")
    os.environ.update(profile_environment())
    with tempfile.TemporaryDirectory(prefix="openbase-profile-test-") as directory:
        await codex_checks(str(Path(directory).resolve()))
        await claude_checks(str(Path(directory).resolve()))


if __name__ == "__main__":
    asyncio.run(main())
