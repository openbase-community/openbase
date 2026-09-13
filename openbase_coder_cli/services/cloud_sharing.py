"""Cloud sharing client: publish report shares and manage grants.

Openbase Cloud stores explicitly shared items (account-gated, no public
links) and serves them to grantees; this module is the owner-runtime side,
calling the cloud sharing API over the user's authenticated session.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from openbase_coder_cli.services.cloud_registration import (
    CloudReportResult,
    _post_to_cloud,
)

SHARING_ITEMS_PATH = "/api/openbase/sharing/items/"


def publish_item(payload: dict[str, Any]) -> CloudReportResult:
    return _post_to_cloud(f"{SHARING_ITEMS_PATH}publish/", payload)


def list_items(origin_key: str | None = None) -> CloudReportResult:
    path = SHARING_ITEMS_PATH
    if origin_key:
        path = f"{path}?origin_key={quote(origin_key)}"
    return _post_to_cloud(path, {}, method="GET")


def delete_item(item_id: str) -> CloudReportResult:
    return _post_to_cloud(f"{SHARING_ITEMS_PATH}{quote(item_id)}/", {}, method="DELETE")


def list_grants(item_id: str) -> CloudReportResult:
    return _post_to_cloud(
        f"{SHARING_ITEMS_PATH}{quote(item_id)}/grants/", {}, method="GET"
    )


def add_grant(item_id: str, email: str) -> CloudReportResult:
    return _post_to_cloud(
        f"{SHARING_ITEMS_PATH}{quote(item_id)}/grants/", {"email": email}
    )


def revoke_grant(item_id: str, grant_id: str) -> CloudReportResult:
    return _post_to_cloud(
        f"{SHARING_ITEMS_PATH}{quote(item_id)}/revoke-grant/", {"grant_id": grant_id}
    )
