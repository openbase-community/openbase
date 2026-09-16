from openbase_coder_cli.services import tailnet_experience


def test_electron_tailnet_choices_are_vpn_or_direct(monkeypatch):
    monkeypatch.setattr(tailnet_experience.tp, "provider", lambda: "netmesh")

    payload = tailnet_experience.tailnet_experience_payload()
    electron = [option for option in payload["options"] if option["electron_onboarding"]]

    assert payload["provider"] == "netmesh"
    assert [option["name"] for option in electron] == [
        "Openbase VPN",
        "Openbase Direct",
    ]
    assert electron[0]["recommended"] is True
    assert electron[0]["browser_site_access"] is True
    assert electron[0]["electron_platforms"] == ["darwin"]
    assert electron[1]["requires_vpn"] is False
    assert electron[1]["browser_site_access"] is False
    assert electron[1]["electron_platforms"] == ["darwin", "linux", "win32"]
    assert not any(
        option["provider"] == "tailscale" and option["electron_onboarding"]
        for option in payload["options"]
    )


def test_provider_names_are_user_facing(monkeypatch):
    assert tailnet_experience.tailnet_provider_name("netmesh") == "Openbase VPN"
    assert tailnet_experience.tailnet_provider_name("netmesh-tsnet") == "Openbase Direct"
    assert tailnet_experience.tailnet_provider_name("tailscale") == "Official Tailscale"
    assert tailnet_experience.tailnet_provider_name("unknown") == "Private network"
