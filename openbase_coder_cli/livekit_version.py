"""The livekit-server version Openbase Coder ships and dev installs run.

Single source of truth for the pinned LiveKit engine: the release workflow
refuses to package any other version (bump this constant deliberately when
upgrading), and dev setup downloads the same pin into ``~/.openbase/bin``
(see ``livekit_install.py``). Dev installs that end up on a different local
binary (PATH/Homebrew fallback) warn when their version diverges, so engine
differences can't sail through development testing unnoticed.
"""

from __future__ import annotations

LIVEKIT_SERVER_PINNED_VERSION = "1.13.5"
