"""User-facing tailnet choices shared by onboarding clients.

The provider ids are compatibility values used in env files and cloud payloads.
Product names and capability claims live here so Electron, CLI output, and API
consumers do not invent different explanations for the same transports.
"""

from __future__ import annotations

from typing import Any

from openbase_coder_cli.services import tailscale_provider as tp

TAILNET_EXPERIENCES: tuple[dict[str, Any], ...] = (
    {
        "provider": tp.PROVIDER_NETMESH,
        "name": "Openbase VPN",
        "recommended": True,
        "requires_vpn": True,
        "browser_site_access": True,
        "electron_onboarding": True,
        "electron_platforms": ["darwin"],
        "summary": (
            "Recommended. No telemetry. Based on Headscale OSS + Tailscale DERP "
            "servers. Openbase VPN collects no VPN traffic or usage analytics "
            "and sends no VPN analytics to Tailscale. Allows seamless handoff "
            "between phone and computer."
        ),
    },
    {
        "provider": tp.PROVIDER_NETMESH_TSNET,
        "name": "Openbase Direct",
        "recommended": False,
        "requires_vpn": False,
        "browser_site_access": False,
        "electron_onboarding": True,
        "electron_platforms": ["darwin", "linux", "win32"],
        "summary": (
            "Fallback only. An embedded connection for environments that cannot "
            "support a VPN. Openbase app traffic stays available, but created "
            "web apps and CLI authentication will not be available from your phone."
        ),
    },
    {
        "provider": tp.PROVIDER_TAILSCALE,
        "name": "Official Tailscale",
        "recommended": False,
        "requires_vpn": True,
        "browser_site_access": True,
        "electron_onboarding": False,
        "electron_platforms": [],
        "summary": (
            "Beta. Try it if you are already using Tailscale. Compatibility "
            "transport for developer and headless CLI installs."
        ),
    },
)


def tailnet_provider_name(provider_name: str | None = None) -> str:
    """Return the canonical user-facing name for a transport provider."""
    selected = provider_name or tp.provider()
    option = next(
        (item for item in TAILNET_EXPERIENCES if item["provider"] == selected), None
    )
    return str(option["name"]) if option is not None else "Private network"


def tailnet_experience_payload() -> dict[str, Any]:
    """The active provider plus the canonical transport catalog."""
    return {
        "provider": tp.provider(),
        "options": [dict(option) for option in TAILNET_EXPERIENCES],
    }
