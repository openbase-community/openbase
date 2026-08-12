"""Shared logger and constants for the Super Agents LiveKit client.

The client class is assembled from mixins that live in sibling modules
(`super_agents_client_turns`, `super_agents_client_threads`) plus the speech
helpers in `super_agents_speech`. They all need the same logger and timing-log
tag; keeping those here lets every piece share one object without importing the
top-level client module (which would be circular). The logger name is pinned to
the client module's dotted path so emitted log lines are byte-identical to when
everything lived in one file.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("openbase_coder_cli.livekit_agent.super_agents_client")
DISPATCH_TIMING_LOG = "dispatch_timing"
DEFAULT_CODEX_MODEL = "gpt-5.5"
DEFAULT_DISPATCHER_LABEL = "dispatcher"
# A repeat of content a just-spoken turn already covered (an STT twin of a
# steered correction, or the user restating it) must not spawn a fresh
# backend turn that answers with the same gist again.
SPOKEN_TURN_DUPLICATE_SUPPRESSION_SECONDS = 10.0
