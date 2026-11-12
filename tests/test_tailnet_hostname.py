"""The netmesh node name is the bare sanitized host name, shared by every transport."""

from openbase_coder_cli.services import tailnet_hostname


def test_sanitize_strips_domain_and_reduces_to_a_dns_label():
    assert tailnet_hostname.sanitize_hostname("Gabes-MacBook-Pro.local") == "gabes-macbook-pro"
    assert tailnet_hostname.sanitize_hostname("my box_01") == "my-box-01"
    assert tailnet_hostname.sanitize_hostname("--") == "openbase-mac"


def test_netmesh_hostname_has_no_product_suffix(monkeypatch):
    monkeypatch.setattr(tailnet_hostname.socket, "gethostname", lambda: "Gabes-MacBook-Air.local")
    assert tailnet_hostname.netmesh_hostname() == "gabes-macbook-air"
