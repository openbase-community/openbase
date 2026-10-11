# profiles

Install or repair the configuration layers Openbase uses for Codex and Claude Code conversations without rerunning full setup.

```bash
openbase-coder profiles install
```

The default command installs the Openbase-specific Codex and Claude profiles, their session-ID hooks, and the Super Agents MCP configuration while preserving normal terminal defaults.

It also registers the Super Agents MCP server in your default Codex and Claude Code configurations (`~/.codex/config.toml` and `~/.claude.json`), so ordinary terminal `codex` and `claude` sessions can dispatch and steer Super Agents. An existing `super-agents` entry of your own is never replaced. To keep the MCP out of those default configurations, opt out:

```bash
openbase-coder profiles install --no-shared-super-agents-mcp
```

To also register the session-ID hook in the default Codex and Claude Code configurations, opt in explicitly:

```bash
openbase-coder profiles install --include-default-hooks
```

This preserves unrelated settings and makes the `Agent-Thread-Id` guidance available to ordinary terminal sessions as well as Openbase-profile sessions. Restart Openbase services after installation so managed processes load the updated profile environment.
