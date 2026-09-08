"""Shared thread/session sync helpers for Openbase Coder.

This package holds the CLI's thread and session persistence-and-sync layer:
Claude/Codex cross-device thread sync (``claude_thread_sync``,
``thread_exchange``, ``codex_state``, ``thread_sync_common``), the Codex
app-server session
manager (``session_manager``), thread models/payloads, and recent-project
tracking.

The CLI does not expose its own MCP server. Openbase-specific agent-facing
workflows belong in agent skills; general agent-thread management belongs in
the standalone, Openbase-agnostic ``super-agents`` MCP server. Do not add a
CLI-owned MCP surface here.
"""
