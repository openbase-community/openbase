import pytest

from openbase_coder_cli.services.published_service_routes import (
    _validate_account_hostname,
)


@pytest.mark.parametrize("environment", ["", "-staging"])
@pytest.mark.parametrize("namespace", ["abcd2345efgh", "n" + "1" * 32])
def test_private_services_use_sibling_dns_zone(environment, namespace):
    _validate_account_hostname(
        "crm",
        f"crm.{namespace}.vpn{environment}.example.test",
        f"device.net{environment}.example.test",
    )


@pytest.mark.parametrize(
    "service_zone",
    [
        "svc.net.example.test",
        "vpn.other.test",
        "vpn-staging.example.test",
        "example.test",
    ],
)
def test_rejects_shadowed_or_foreign_service_zone(service_zone):
    with pytest.raises(ValueError):
        _validate_account_hostname(
            "crm", f"crm.n{'1' * 32}.{service_zone}", "device.net.example.test"
        )


@pytest.mark.parametrize(
    "namespace",
    [
        "abc",
        "abcdefgh23456",
        "abcd2345efg0",
        "abcd2345efg1",
        "ABCD2345EFGH",
        "abcd.2345efgh",
    ],
)
def test_rejects_invalid_short_namespaces(namespace):
    with pytest.raises(ValueError):
        _validate_account_hostname(
            "crm", f"crm.{namespace}.vpn.example.test", "device.net.example.test"
        )
