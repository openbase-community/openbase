# claude

Inspect the Claude Code login used by Openbase sessions.

## Usage

```bash
openbase-coder claude status
openbase-coder claude login
```

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
