"""Keep this machine's Openbase VPN node bound to the signed-in account.

A netmesh node belongs to the account whose pre-auth key registered it, and
the node engines (the macOS root ``tailscaled``, the embedded tunneld node, the
stock Tailscale client) persist that login across disconnects and restarts. A
new account's pre-auth key is ignored while an old login is stored, so
without an explicit check a sign-in as a different account silently keeps the
machine inside the previous account's private network, with its peers
visible (field test 2026-10-11).

Every node status payload names its owner (``Self.UserID`` resolved through
``User``) as the headscale user, and every enrollment names the signed-in
account's headscale user (``tailnet_user``). Comparing the two is a local,
key-free ownership check. Sign-out leaves the network entirely: the node is
deleted server-side while the account's token still works, then the engine
forgets its login so nothing can resume it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from openbase_coder_cli.services import tailscale_provider as tp

logger = logging.getLogger(__name__)

# Backend states in which the engine holds no node login to reuse.
LOGGED_OUT_STATES = frozenset({"NeedsLogin", "NoState"})


def node_owner(payload: dict[str, Any] | None) -> str | None:
    """Headscale user that owns the local node in a status payload, or None.

    None means the payload carries no login (NeedsLogin, no state, an error),
    which is never a reason to keep or to leave a network.
    """
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    if payload.get("BackendState") in LOGGED_OUT_STATES:
        # A signed-out engine may still describe its last node, but it holds
        # no login a connect could resume.
        return None
    self_node = payload.get("Self")
    users = payload.get("User")
    if not isinstance(self_node, dict) or not isinstance(users, dict):
        return None
    user_id = self_node.get("UserID")
    if user_id in (None, ""):
        return None
    user = users.get(str(user_id))
    if not isinstance(user, dict):
        return None
    login = str(user.get("LoginName") or "").strip()
    return login or None


def node_ips(payload: dict[str, Any] | None) -> set[str]:
    """This node's tailnet addresses from a status payload."""
    if not isinstance(payload, dict):
        return set()
    self_node = payload.get("Self")
    raw = self_node.get("TailscaleIPs") if isinstance(self_node, dict) else None
    if not isinstance(raw, list):
        raw = payload.get("TailscaleIPs")
    return {str(ip) for ip in raw or [] if ip}


def belongs_to_other_account(
    payload: dict[str, Any] | None, enrollment: dict[str, Any] | None
) -> bool:
    """Whether the local node is positively logged into a different account.

    Only a known owner that differs from the signed-in account's headscale
    user counts. Unknown on either side is not a mismatch: there is no stored
    login to reuse, or nothing to compare it with.
    """
    owner = node_owner(payload)
    expected = str((enrollment or {}).get("tailnet_user") or "").strip()
    return bool(owner and expected and owner != expected)


def leave_if_other_account(
    provider_name: str,
    enrollment: dict[str, Any] | None,
    *,
    echo: Callable[[str], None] = logger.info,
) -> bool:
    """Sign the local node out when it belongs to a different account.

    Returns True when the engine may now be (re)connected for the signed-in
    account, False when a foreign login is still stored and connecting would
    reuse it. Callers must not report or keep a connection on False.
    """
    if not belongs_to_other_account(netmesh_status(provider_name), enrollment):
        return True
    echo(
        "This machine's Openbase VPN node belongs to a different Openbase "
        "account; signing it out before joining yours."
    )
    error = forget_node_login(provider_name)
    if error:
        echo(f"Could not sign the previous account's VPN node out: {error}")
        return False
    return True


def netmesh_status(provider_name: str) -> dict[str, Any] | None:
    """Full node status for a netmesh transport, or None when unavailable."""
    if provider_name not in (tp.PROVIDER_NETMESH, tp.PROVIDER_NETMESH_TSNET):
        return None
    try:
        payload = tp.status_json(provider_name=provider_name)
    except Exception:  # noqa: BLE001 - status is advisory here
        return None
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    return payload


def revoke_own_node(payload: dict[str, Any] | None) -> bool:
    """Delete this machine's node from the signed-in account's network.

    Matches by tailnet address: addresses are unique per control plane, and
    the devices API only lists (and only deletes) the caller's own nodes, so a
    node of another account can never be touched. Never raises.
    """
    from openbase_coder_cli.services.cloud_registration import (
        list_netmesh_devices,
        revoke_netmesh_device,
    )

    ips = node_ips(payload)
    if not ips:
        return False
    for device in list_netmesh_devices():
        if ips & {str(ip) for ip in device.get("ip_addresses") or []}:
            return revoke_netmesh_device(str(device.get("id")))
    return False


def forget_node_login(provider_name: str) -> str | None:
    """Make the local engine leave its network and forget the node login.

    Returns None on success or a human-readable reason it could not.
    """
    if provider_name == tp.PROVIDER_NETMESH_TSNET:
        from openbase_coder_cli.services.tunneld import tunneld_logout

        return None if tunneld_logout() else "openbase-tunneld did not log out"
    if provider_name != tp.PROVIDER_NETMESH:
        return None
    if tp.netmesh_uses_stock_tailscale():
        return _stock_tailscale_logout()
    return _companion_logout()


def _companion_logout() -> str | None:
    from openbase_coder_cli.services.netmesh_companion import (
        NetmeshCompanion,
        NetmeshCompanionError,
        _workspace_dir_quiet,
    )

    companion = None
    try:
        companion = NetmeshCompanion(workspace_dir=_workspace_dir_quiet())
        status = companion.ensure_running(build_if_missing=False)
        if status.helper_enabled:
            # An older helper has no logout call; the version gate swaps in
            # the bundled one first.
            companion.replace_helper_if_needed()
        companion.logout()
        return None
    except (NetmeshCompanionError, OSError) as exc:
        # Fail closed: an engine that cannot forget its login must at least
        # stop carrying traffic for the old account.
        reason = str(exc)
        if companion is not None:
            try:
                companion.disconnect()
            except Exception:  # noqa: BLE001 - best effort after a failure
                pass
        return reason
    finally:
        if companion is not None:
            companion.close()


def _stock_tailscale_logout() -> str | None:
    import subprocess

    tailscale_bin = tp.tailscale_bin()
    if not tailscale_bin:
        return None
    result = subprocess.run(  # noqa: S603 - fixed argv
        [tailscale_bin, "logout"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return (
            result.stderr.strip() or result.stdout.strip() or "tailscale logout failed"
        )
    return None


def leave_network(
    provider_name: str, *, echo: Callable[[str], None] = logger.info
) -> None:
    """Sign-out: remove this node from the account's network, then forget it.

    Must run while the account's credentials are still stored. Best-effort and
    never raises; each step reports what it could not do.
    """
    if provider_name not in (tp.PROVIDER_NETMESH, tp.PROVIDER_NETMESH_TSNET):
        return
    # Capture the node's addresses first: after the engine logs out, its
    # status no longer names them. Log out before the server-side delete so
    # the engine can still reach the control server to expire its own node.
    payload = netmesh_status(provider_name)
    error = forget_node_login(provider_name)
    if error:
        echo(f"Note: could not fully sign the Openbase VPN out: {error}")
    else:
        echo("Signed the Openbase VPN out on this machine.")
    if revoke_own_node(payload):
        echo("Removed this machine from your Openbase VPN network.")


def deregister_device(*, echo: Callable[[str], None] = logger.info) -> None:
    """Remove this machine from the account's device list. Never raises."""
    from openbase_coder_cli.services.cloud_registration import (
        deregister_device_with_cloud,
    )

    result = deregister_device_with_cloud()
    if result.ok:
        echo("Removed this machine from your Openbase devices.")
    elif result.supported:
        echo(f"Note: could not remove this machine from your devices: {result.error}")


def sign_out_account(*, echo: Callable[[str], None] = logger.info) -> None:
    """Undo everything this machine holds for the signed-in account.

    Runs before stored tokens are cleared (logout, or a login that replaces a
    different account), because both cloud calls need that account's token.
    """
    leave_network(tp.provider(), echo=echo)
    deregister_device(echo=echo)
