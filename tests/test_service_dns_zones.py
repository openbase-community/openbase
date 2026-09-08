import pytest

from openbase_coder_cli.services.published_service_routes import (
    _validate_account_hostname,
)


@pytest.mark.parametrize("environment", ["", "-staging"])
def test_private_services_use_sibling_dns_zone(environment):
    _validate_account_hostname(
        "crm",
        f"crm.n{'1' * 32}.vpn{environment}.example.test",
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
