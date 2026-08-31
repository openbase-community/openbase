#!/bin/bash
# SessionStart hook for the Openbase-managed Codex and Claude Code homes.
# Reads the session_id from the hook's stdin JSON and injects it into the
# conversation as additionalContext, together with the instructions for using
# it, so the agent knows its own thread/session ID and stamps commits with the
# Agent-Thread-Id trailer. In Claude Code, it also persists the ID into the
# session environment so Openbase CLI calls are attributed to the same agent.
# The usage instructions ride in the hook (rather than in AGENTS.md) so they
# ship, update, and uninstall with it, and only appear in sessions where the ID
# actually exists.

set -euo pipefail

INPUT=$(cat)

if command -v jq >/dev/null 2>&1; then
    SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
elif command -v python3 >/dev/null 2>&1; then
    SESSION_ID=$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin).get("session_id")
except Exception:
    value = None
print(value if isinstance(value, str) else "")
' 2>/dev/null || true)
else
    exit 0
fi

if [ -z "$SESSION_ID" ]; then
    exit 0
fi

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    printf 'export OPENBASE_AGENT_ID=%q\n' "$SESSION_ID" >> "$CLAUDE_ENV_FILE"
fi

CONTEXT="Current agent thread/session ID: ${SESSION_ID}. When committing, add a git commit message trailer named Agent-Thread-Id with this exact value so the commit is tied to the agent session that produced it. This value is authoritative for the current session: do not query Super Agents or any other tool to discover your own thread ID."

printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$CONTEXT"
