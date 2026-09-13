from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import replace

import pytest
from click.testing import CliRunner
from test_service_https import service as https_service

from openbase_coder_cli.services import published_service_routes as routes
from openbase_coder_cli.services import published_services as p
from openbase_coder_cli.services import service_recovery as recovery
from openbase_coder_cli.services import tailscale_provider as provider

service_fixture = https_service


@pytest.fixture
def transport(monkeypatch):
    state = {"hash": "", "etag": "test-etag", "released": [], "stopped": []}
    monkeypatch.setattr(
        routes, "_desired_rules", lambda services: [s.serve_rule() for s in services]
    )

    def plan(rules):
        return {
            "hash": hashlib.sha256(
                json.dumps(rules, sort_keys=True).encode()
            ).hexdigest()
        }

    def apply(rules, *, expected_etag, expected_hash):
        assert expected_etag == state["etag"] and expected_hash == state["hash"]
        state["hash"] = plan(rules)["hash"]
        return {"hash": state["hash"]}

    monkeypatch.setattr(provider, "plan_serve", plan)
    monkeypatch.setattr(provider, "serve_snapshot", lambda: dict(state))
    monkeypatch.setattr(provider, "apply_serve", apply)
    monkeypatch.setattr(
        routes,
        "release_private_service_hostname",
        lambda name, node: state["released"].append((name, node)),
    )
    monkeypatch.setattr(
        p, "stop_gateway", lambda service: state["stopped"].append(service)
    )
    return state


def digest(services):
    return provider.plan_serve(routes._desired_rules(services))["hash"]


@pytest.mark.parametrize("operation", ["publish", "unpublish"])
@pytest.mark.parametrize(
    "phase", ["prepared", "registry-written", "route-applied", "registry-committed"]
)
def test_interrupted_transaction_recovers_every_durable_phase(
    service_fixture, transport, operation, phase
):
    target = replace(service_fixture, pid=123)
    other = replace(
        target,
        name="other",
        hostname="other.abcd2345efgh.vpn.obs.so",
        proxy_port=52809,
        pid=456,
    )
    remaining = [other]
    with_target = [other, target]
    before = remaining if operation == "publish" else with_target
    after = with_target if operation == "publish" else remaining
    before_hash = digest(before)
    p.save_registry(p.ServiceRegistry(tuple(before), before_hash))
    recovery.begin(operation, p.load_registry(), target)
    assert recovery.journal_path().stat().st_mode & 0o777 == 0o600
    if phase != "prepared":
        p.save_registry(
            p.ServiceRegistry(
                tuple(after),
                digest(after) if phase == "registry-committed" else before_hash,
            )
        )
    transport["hash"] = (
        digest(after)
        if phase in {"route-applied", "registry-committed"}
        else before_hash
    )
    with p.registry_lock():
        assert recovery.recover() == target.name
    assert p.load_registry() == p.ServiceRegistry(tuple(remaining), digest(remaining))
    assert transport["released"] == [(target.name, target.node_id)]
    assert transport["stopped"] == [target]
    assert not recovery.journal_path().exists()
    assert recovery.recover() is None


def test_recovery_keeps_journal_until_cleanup_succeeds(
    service_fixture, transport, monkeypatch
):
    before = p.ServiceRegistry((), digest([]))
    p.save_registry(before)
    recovery.begin("publish", before, service_fixture)
    transport["hash"] = digest([])
    original = provider.apply_serve

    def interrupted(*args, **kwargs):
        raise RuntimeError("helper unavailable")

    monkeypatch.setattr(provider, "apply_serve", interrupted)
    with pytest.raises(RuntimeError, match="helper unavailable"):
        recovery.recover()
    assert recovery.journal_path().exists()
    monkeypatch.setattr(provider, "apply_serve", original)
    assert recovery.recover() == service_fixture.name


def test_recovery_refuses_unknown_routes_and_future_schema(service_fixture, transport):
    before = p.ServiceRegistry((), digest([]))
    p.save_registry(before)
    recovery.begin("publish", before, service_fixture)
    transport["hash"] = "unknown-routes"
    with pytest.raises(RuntimeError, match="Unknown VPN routes"):
        recovery.recover()
    assert p.load_registry() == before
    assert transport["released"] == []
    recovery.journal_path().write_text('{"schema_version":999}')
    with pytest.raises(ValueError, match="update the CLI"):
        recovery.recover()


def test_retry_unpublish_finishes_interrupted_removal(service_fixture, transport):
    command = importlib.import_module("openbase_coder_cli.cli.service")
    before = p.ServiceRegistry((service_fixture,), digest([service_fixture]))
    p.save_registry(before)
    recovery.begin("unpublish", before, service_fixture)
    p.save_registry(p.ServiceRegistry((), before.last_applied_serve_hash))
    transport["hash"] = before.last_applied_serve_hash
    result = CliRunner().invoke(command.service, ["unpublish", service_fixture.name])
    assert result.exit_code == 0, result.output
    assert "recovered interrupted" in result.output
    assert p.load_services() == []
