"""Client for Openbase Cloud's managed connections vault
(``/api/openbase/connections/``).

Credentials are returned only by ``lease`` and stay in the calling process:
the proxy or query command uses them and never writes them anywhere. On a
cloud workspace the machine token with the ``connections`` scope is used
when no owner login exists."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from openbase_coder_cli.services.cloud_registration import (
    CloudReportResult,
    _post_to_cloud,
)

CONNECTIONS_PATH = "/api/openbase/connections/"
CONNECTIONS_SCOPE = "connections"
KIND_POSTGRES = "postgres"
KIND_S3 = "s3"
ROLE_READWRITE = "readwrite"
ROLE_READONLY = "readonly"
NOT_SUPPORTED_MESSAGE = (
    "This Openbase Cloud does not offer managed connections yet; "
    "update the cloud deployment."
)


class ConnectionsError(RuntimeError):
    """Cloud refused or failed a managed-connection request."""


def _call(
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    method: str = "POST",
    detail: bool = False,
) -> dict[str, Any] | list[Any]:
    result: CloudReportResult = _post_to_cloud(
        path, payload or {}, method=method, machine_scope=CONNECTIONS_SCOPE
    )
    if not result.supported:
        # The collection endpoint only 404s when Cloud lacks the feature; a
        # detail endpoint 404s when the connection was removed.
        if detail and result.status_code == 404:
            raise ConnectionsError(
                "This connection no longer exists in Openbase Cloud."
            )
        raise ConnectionsError(NOT_SUPPORTED_MESSAGE)
    if not result.ok:
        raise ConnectionsError(result.error or "Openbase Cloud request failed.")
    return result.response if result.response is not None else {}


def _detail(connection_id: str, suffix: str = "") -> str:
    return f"{CONNECTIONS_PATH}{quote(connection_id, safe='')}/{suffix}"


def list_connections(kind: str | None = None) -> list[dict[str, Any]]:
    query = f"?{urlencode({'kind': kind})}" if kind else ""
    response = _call(f"{CONNECTIONS_PATH}{query}", method="GET")
    return response if isinstance(response, list) else []


def find_connection(kind: str, name: str) -> dict[str, Any]:
    for item in list_connections(kind):
        if item.get("name") == name:
            return item
    raise ConnectionsError(
        f"No {kind} connection named '{name}'. "
        f"Run 'openbase-coder {'db' if kind == KIND_POSTGRES else 'storage'} list'."
    )


def add_connection(
    kind: str, name: str, target: dict[str, Any], credentials: dict[str, dict[str, str]]
) -> dict[str, Any]:
    response = _call(
        CONNECTIONS_PATH,
        {"kind": kind, "name": name, "target": target, "credentials": credentials},
    )
    return response if isinstance(response, dict) else {}


def provision_connection(name: str, *, region: str = "") -> dict[str, Any]:
    response = _call(
        f"{CONNECTIONS_PATH}provision/",
        {"kind": KIND_POSTGRES, "name": name, "provider": "neon", "region": region},
    )
    return response if isinstance(response, dict) else {}


def remove_connection(connection_id: str, *, teardown: bool = False) -> None:
    suffix = "?teardown=1" if teardown else ""
    _call(_detail(connection_id) + suffix, method="DELETE", detail=True)


def lease_connection(
    connection_id: str, *, role: str, purpose: str, device: str
) -> dict[str, Any]:
    response = _call(
        _detail(connection_id, "lease/"),
        {"role": role, "purpose": purpose[:200], "device": device[:120]},
        detail=True,
    )
    if not isinstance(response, dict) or "credentials" not in response:
        raise ConnectionsError("Openbase Cloud returned an incomplete lease.")
    return response


def connection_allowed(connection_id: str) -> bool:
    """Whether the connection still exists and is not revoked (for proxies)."""
    try:
        response = _call(_detail(connection_id), method="GET", detail=True)
    except ConnectionsError as exc:
        if "no longer exists" in str(exc):
            return False
        # A transient Cloud failure must not kill a working proxy.
        return True
    return isinstance(response, dict) and not response.get("revoked_at")


def revoke_connection(connection_id: str) -> None:
    _call(_detail(connection_id, "revoke/"), detail=True)


def restore_connection(connection_id: str) -> None:
    _call(_detail(connection_id, "restore/"), detail=True)


def set_role_credentials(
    connection_id: str, role: str, credentials: dict[str, str] | None
) -> dict[str, Any]:
    response = _call(
        _detail(connection_id, "credentials/"),
        {"role": role, "credentials": credentials},
    )
    return response if isinstance(response, dict) else {}


def connection_events(connection_id: str) -> list[dict[str, Any]]:
    response = _call(_detail(connection_id, "events/"), method="GET", detail=True)
    return response if isinstance(response, list) else []
