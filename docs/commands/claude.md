# claude

Start Claude Code with Openbase's profile, or inspect the Claude Code login
used by Openbase sessions.

## Usage

```bash
openbase-coder claude [--local | --remote] [CLAUDE ARGS...]
openbase-coder claude status
openbase-coder claude login
```

## Starting a session

`openbase-coder claude` (also `openbase claude`) with no subcommand starts
Claude Code with Openbase's settings and MCP layer and Openbase's
instructions:

```bash
claude --settings ~/.openbase/profiles/claude/settings.json \
  --mcp-config ~/.openbase/profiles/claude/mcp.json \
  --append-system-prompt "<Openbase instructions>" [CLAUDE ARGS...]
```

The session is visible to and steerable from Openbase. Arguments go to
`claude` unchanged; `status` and `login` as the first argument run the
subcommands below instead. `--local` / `--remote` (first) choose whether a
paired Openbase Sync edge runs the session on its hub. Claude Code's own
management subcommands (`mcp`, `doctor`, `update`, …) run plain. Plain
`claude` is never changed. See
[Codex and Claude Code from Your Terminal](../agent-launchers.md).

## Login

Openbase runs Claude Code sessions against your own shared `~/.claude` home
and your own Claude Code login — there is no separate Openbase-managed
Claude config or credential.

`status` reports that shared login. When the cached credentials look expired,
it also runs a short probe turn to catch expired-but-cached logins (Claude
Code keeps reporting cached account state after a login dies); a successful
probe refreshes and persists fresh credentials.

`login` is a thin convenience wrapper around `claude login` (`--sso` forces
the SSO flow, `--email` pre-fills the login email). Running `claude login`
directly is equivalent.
