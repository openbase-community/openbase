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
#
# In Claude Code it additionally records the session's cross-session-messaging
# inbox socket + token into the Openbase inbox registry, so Openbase can steer
# a live turn in a terminal session it did not launch (there is no shared
# app-server daemon for Claude the way there is for Codex; the inbox socket is
# the only external delivery channel and its path/token are exported only to
# the session's own hooks and children).

set -euo pipefail

INPUT=$(cat)

# Extract session_id and cwd from the hook stdin JSON in one pass.
if command -v jq >/dev/null 2>&1; then
    SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
    HOOK_CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)
elif command -v python3 >/dev/null 2>&1; then
    _PARSED=$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
sid = data.get("session_id")
cwd = data.get("cwd")
print(sid if isinstance(sid, str) else "")
print(cwd if isinstance(cwd, str) else "")
' 2>/dev/null || true)
    SESSION_ID=$(printf '%s\n' "$_PARSED" | sed -n '1p')
    HOOK_CWD=$(printf '%s\n' "$_PARSED" | sed -n '2p')
else
    exit 0
fi

if [ -z "$SESSION_ID" ]; then
    exit 0
fi

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    printf 'export AGENT_SESSION_ID=%q\n' "$SESSION_ID" >> "$CLAUDE_ENV_FILE"
fi

# Record the Claude Code inbox socket + token so Openbase can post steering
# messages into this session's active turn. Only Claude Code exports these; a
# Codex session leaves them unset and skips this block. Best-effort: never let
# a registry-write failure break session startup.
if [ -n "${CLAUDE_CODE_MESSAGING_SOCKET:-}" ]; then
    # Generic location inside the Claude home (overridable with
    # CLAUDE_CONFIG_DIR, matching how the shared Claude home is resolved
    # elsewhere), co-located with the sessions it describes.
    _INBOX_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/inbox-registry"
    if mkdir -p "$_INBOX_DIR" 2>/dev/null; then
        chmod 700 "$_INBOX_DIR" 2>/dev/null || true
        _INBOX_FILE="$_INBOX_DIR/${SESSION_ID}.json"
        _INBOX_CWD="${HOOK_CWD:-$PWD}"
        _INBOX_TMP="${_INBOX_FILE}.tmp.$$"
        # Written by the session's own hook; the socket/token are only valid
        # while this process lives, so consumers must verify the socket is
        # connectable and prune dead entries.
        if [ -n "${CLAUDE_CODE_MESSAGING_TOKEN:-}" ] && command -v python3 >/dev/null 2>&1; then
            SESSION_ID="$SESSION_ID" \
            INBOX_SOCK="$CLAUDE_CODE_MESSAGING_SOCKET" \
            INBOX_TOKEN="$CLAUDE_CODE_MESSAGING_TOKEN" \
            INBOX_CWD="$_INBOX_CWD" \
            python3 -c '
import json, os
rec = {
    "sessionId": os.environ["SESSION_ID"],
    "socket": os.environ["INBOX_SOCK"],
    "token": os.environ.get("INBOX_TOKEN") or None,
    "cwd": os.environ.get("INBOX_CWD") or None,
    "recordedAt": __import__("time").time(),
}
import sys
sys.stdout.write(json.dumps(rec))
' > "$_INBOX_TMP" 2>/dev/null || rm -f "$_INBOX_TMP" 2>/dev/null || true
        else
            # No token (older CLI / macOS-Linux where the auth line is
            # optional): still record the socket so delivery can be attempted.
            printf '{"sessionId":%s,"socket":%s,"token":null,"cwd":%s,"recordedAt":%s}' \
                "\"$SESSION_ID\"" "\"$CLAUDE_CODE_MESSAGING_SOCKET\"" "\"$_INBOX_CWD\"" "$(date +%s)" \
                > "$_INBOX_TMP" 2>/dev/null || rm -f "$_INBOX_TMP" 2>/dev/null || true
        fi
        if [ -f "$_INBOX_TMP" ]; then
            chmod 600 "$_INBOX_TMP" 2>/dev/null || true
            mv -f "$_INBOX_TMP" "$_INBOX_FILE" 2>/dev/null || rm -f "$_INBOX_TMP" 2>/dev/null || true
        fi
    fi
fi

CONTEXT="Current agent thread/session ID: ${SESSION_ID}. When committing, add a git commit message trailer named Agent-Thread-Id with this exact value so the commit is tied to the agent session that produced it. This value is authoritative for the current session: do not query Super Agents or any other tool to discover your own thread ID."

printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$CONTEXT"
