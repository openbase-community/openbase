"""Crash recovery for private publications, under the existing registry lock."""

from __future__ import annotations

import json
from dataclasses import asdict, replace

from openbase_coder_cli.services import published_service_routes as routes
from openbase_coder_cli.services import published_services as published
from openbase_coder_cli.services import tailscale_provider as provider
from openbase_coder_cli.services.service_certificates import private_write


def journal_path():
    return published._registry_path().with_name("published-service-transaction.json")


def begin(operation, before, target, *, release_hostname=True):
    if operation not in {"publish", "unpublish"}:
        raise ValueError("Unknown publication transaction.")
    remaining = [s for s in before.services if s.name != target.name]
    # Plan while DNS still exists: the helper intentionally refuses to plan a
    # hostname after its allocation has been removed. Record only exact hashes.
    allowed = {
        str(provider.plan_serve(routes._desired_rules(remaining))["hash"]),
        str(provider.plan_serve(routes._desired_rules([*remaining, target]))["hash"]),
        before.last_applied_serve_hash or str(provider.plan_serve([])["hash"]),
    }
    private_write(
        journal_path(),
        json.dumps(
            {
                "schema_version": 1,
                "operation": operation,
                "before": published.registry_payload(before),
                "target": asdict(target),
                "release_hostname": release_hostname,
                "allowed_hashes": sorted(allowed),
            }
        ).encode(),
    )


def finish():
    journal_path().unlink(missing_ok=True)


def recover() -> str | None:
    """Remove an interrupted publication; never overwrite an unknown route."""
    path = journal_path()
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Unsupported publication recovery state; update the CLI.")
    operation = payload.get("operation")
    if operation not in {"publish", "unpublish"}:
        raise ValueError("Invalid publication recovery operation.")
    before = published.decode_registry(payload["before"])
    target = published.decode_registry(
        {"version": 5, "services": [payload["target"]]}
    ).services[0]
    remaining = [s for s in before.services if s.name != target.name]
    # The child PID may have reached the registry before the journal update.
    current = published.load_registry()
    stored = next((s for s in current.services if s.name == target.name), None)
    if stored is not None:
        if replace(stored, pid=target.pid) != target:
            raise RuntimeError(
                "Publication changed since interruption; refusing recovery."
            )
        target = stored
    if [s for s in current.services if s.name != target.name] != remaining:
        raise RuntimeError("Registry changed since interruption; refusing recovery.")
    desired_rules = routes._desired_rules(remaining)
    snapshot = provider.serve_snapshot()
    allowed = payload.get("allowed_hashes")
    if (
        not isinstance(allowed, list)
        or not 1 <= len(allowed) <= 3
        or any(not isinstance(h, str) or not h for h in allowed)
    ):
        raise ValueError("Invalid publication recovery route hashes.")
    if snapshot.get("hash") not in allowed:
        raise RuntimeError("Unknown VPN routes; refusing publication recovery.")
    # Deny new HTTPS requests first, even if cleanup is interrupted again.
    published.save_registry(
        published.ServiceRegistry(tuple(remaining), before.last_applied_serve_hash)
    )
    if payload.get("release_hostname") and target.node_id:
        routes.release_private_service_hostname(target.name, target.node_id)
    published.stop_gateway(target)
    result = provider.apply_serve(
        desired_rules,
        expected_etag=str(snapshot["etag"]),
        expected_hash=str(snapshot["hash"]),
    )
    applied_hash = result.get("hash") if isinstance(result, dict) else None
    if not isinstance(applied_hash, str) or not applied_hash:
        raise RuntimeError("VPN helper did not confirm publication recovery.")
    published.save_registry(published.ServiceRegistry(tuple(remaining), applied_hash))
    finish()
    return target.name
