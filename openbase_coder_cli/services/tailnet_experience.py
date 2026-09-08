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
            "Bundled Netmesh networking uses Openbase-operated Headscale and "
            "Tailscale-compatible open-source clients. It does not require a "
            "Tailscale account. Openbase VPN collects no VPN traffic or usage "
            "analytics and sends no VPN analytics to Tailscale. It gives "
            "full network access including websites created on your computer."
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
            "An embedded connection for environments that cannot support a VPN. "
            "Openbase app traffic stays available, but other apps and browsers "
            "cannot use it to open websites created on your computer."
        ),
    },
    {
        "provider": tp.PROVIDER_TAILSCALE,
        "name": "Tailscale (expert CLI only)",
        "recommended": False,
        "requires_vpn": True,
        "browser_site_access": True,
        "electron_onboarding": False,
        "electron_platforms": [],
        "summary": (
            "Compatibility transport for developer and headless CLI installs. "
            "The Electron onboarding flow does not offer this transport."
        ),
    },
)


def tailnet_experience_payload() -> dict[str, Any]:
    """The active provider plus the canonical transport catalog."""
    return {
        "provider": tp.provider(),
        "options": [dict(option) for option in TAILNET_EXPERIENCES],
    }
