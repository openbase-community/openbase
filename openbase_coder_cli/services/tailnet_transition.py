"""Restart the transport services once a fresh install's VPN gets an address.

Openbase VPN enrolls at sign-in/pairing, after setup has started every
service. Until then livekit-server serves loopback only and django-cli hands
out a localhost LiveKit URL (see ``runners.build_livekit_server`` and
``runners.build_django_cli``). Sign-in through ``openbase-coder login``
restarts them itself, but the VPN can also come up later or by another path
(the desktop app's companion, an approval the user gives afterwards, a VPN
that was down at boot). This job runs on the ``sync-workers`` tick and closes
that gap: while livekit-server is marked as waiting for the tailnet and the
tailnet now resolves, it restarts the transport services in place.

In-place restarts never re-register a LaunchAgent, so this adds no macOS
"Background Items Added" notification.
"""

from __future__ import annotations

import logging
import time

from openbase_coder_cli.paths import OPENBASE_BASE_DIR

logger = logging.getLogger(__name__)

# Services whose behaviour depends on the transport (LiveKit's rtc candidate
# mode, the backend's serve/status probing, the agent's LiveKit connection).
TRANSPORT_SERVICES = ("livekit-server", "livekit-agent", "django-cli")

# Present while the running livekit-server started without a tailnet address.
AWAITING_TAILNET_MARKER = OPENBASE_BASE_DIR / "livekit-awaiting-tailnet"

# If a restart still leaves livekit-server waiting (the address vanished again
# mid-restart), retry no sooner than this.
RETRY_SECONDS = 600.0

_last_restart_monotonic: float | None = None


def record_livekit_start(*, awaiting_tailnet: bool) -> None:
    """Called by the livekit-server runner just before it execs LiveKit."""
    if awaiting_tailnet:
        AWAITING_TAILNET_MARKER.parent.mkdir(parents=True, exist_ok=True)
        AWAITING_TAILNET_MARKER.touch()
    else:
        AWAITING_TAILNET_MARKER.unlink(missing_ok=True)


def _livekit_server_running() -> bool:
    from openbase_coder_cli.services.launchd import launchctl_status
    from openbase_coder_cli.services.registry import find_service

    status = launchctl_status(find_service("livekit-server"))
    return bool(status.get("installed")) and bool(status.get("pid"))


def _tailnet_ready() -> bool:
    """Whether livekit-server would now start with a tailnet address."""
    from openbase_coder_cli.services import runners
    from openbase_coder_cli.services.installation import InstallationConfig

    config = (
        InstallationConfig.load()
        if InstallationConfig.exists()
        else InstallationConfig()
    )
    env = runners.load_service_env(config)
    if env.get("LIVEKIT_NETWORK_MODE", "tailscale") != "tailscale":
        # A transport switch restarts these services itself.
        return False
    return runners.tailnet_media_endpoint(env) is not None


def _restart_transport_services() -> None:
    from openbase_coder_cli.services.restart import (
        RestartRequest,
        build_restart_plan,
        execute_restart_plan,
    )

    plan = build_restart_plan(
        RestartRequest(services=TRANSPORT_SERVICES, delay_seconds=0.0)
    )
    execute_restart_plan(plan)


def run_tick() -> bool:
    """One pass; True when it restarted the transport services."""
    global _last_restart_monotonic

    if not AWAITING_TAILNET_MARKER.exists():
        return False
    now = time.monotonic()
    if (
        _last_restart_monotonic is not None
        and now - _last_restart_monotonic < RETRY_SECONDS
    ):
        return False
    if not _livekit_server_running() or not _tailnet_ready():
        return False

    from openbase_coder_cli.services.livekit_pool_watchdog import (
        _voice_session_active,
    )

    if _voice_session_active():
        logger.info("tailnet_transition deferred voice_session_active")
        return False

    logger.info(
        "tailnet_transition tailnet_ready restarting services=%s",
        list(TRANSPORT_SERVICES),
    )
    _last_restart_monotonic = now
    _restart_transport_services()
    return True
