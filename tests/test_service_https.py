from __future__ import annotations

import socket
import ssl
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp import ClientConnectorError, ClientSession, TCPConnector, web
from aiohttp.abc import AbstractResolver
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from openbase_coder_cli.services import published_services as published
from openbase_coder_cli.services import service_certificates as certificates
from openbase_coder_cli.services import service_gateway as gateway
from openbase_coder_cli.services import service_https as https


@pytest.fixture
def service(monkeypatch, isolated_registry):
    monkeypatch.setattr(certificates, "PUBLISHED_SERVICES_PATH", isolated_registry)
    return published.PublishedService(
        "crm",
        3000,
        443,
        52808,
        mode="hostname",
        hostname="crm.abcd2345efgh.vpn.obs.so",
        node_id="7",
    )


def signed_certificate(public_key, signer, names, *, days=90):
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "Test CA")])
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), False
        )
        .sign(signer, hashes.SHA256())
    )


def bundle(service, *, days=90, names=None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = signed_certificate(
        key.public_key(),
        key,
        names or [certificates.certificate_name(service)],
        days=days,
    )
    path = certificates.certificate_directory(service) / "certificate.pem"
    certificates.private_write(
        path,
        cert.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    return path, cert


def test_private_wildcard_bundle_cache_and_lifetime(service):
    path, cert = bundle(service)
    assert certificates.ensure_certificate(service) == path
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not certificates.renewal_due(cert)
    assert certificates.certificate_name(service) == "*.abcd2345efgh.vpn.obs.so"
    assert "crm" not in certificates.certificate_name(service)
    _, expired = bundle(service, days=0)
    assert certificates.renewal_due(expired)


def test_reject_unexpected_names_future_registry_and_old_names(
    service, isolated_registry
):
    path, _ = bundle(
        service, names=["*.abcd2345efgh.vpn.obs.so", "crm.abcd2345efgh.vpn.obs.so"]
    )
    with pytest.raises(ValueError, match="unexpected names"):
        certificates.load_certificate(path, certificates.certificate_name(service))
    with pytest.raises(ValueError, match="current account-private"):
        certificates.certificate_name(replace(service, hostname="crm.attacker.test"))
    isolated_registry.write_text('{"version":999,"services":[]}')
    with pytest.raises(ValueError, match="Unsupported"):
        published.load_registry()


def test_pending_cleanup_runs_even_with_valid_certificate(service, monkeypatch):
    path, _ = bundle(service)
    journal = path.parent / "pending-dns.json"
    journal.write_text('["pending-validation"]')
    broker = Mock()
    monkeypatch.setattr(certificates, "dns_challenge", broker)
    certificates.ensure_certificate(service)
    broker.assert_called_once_with(service, "pending-validation", remove=True)
    assert not journal.exists()


def test_issue_dns01_and_renew_without_leaking_service_name(service, monkeypatch):
    from acme import challenges, client, messages

    signer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    acme = Mock()
    acme.new_account.return_value = messages.RegistrationResource(
        body=messages.Registration(status=messages.STATUS_VALID),
        uri="https://acme.example/account/1",
    )
    challenge = messages.ChallengeBody(
        chall=challenges.DNS01(token=b"a" * 16), uri="https://acme.example/challenge/1"
    )
    order = SimpleNamespace(
        authorizations=[
            SimpleNamespace(
                body=SimpleNamespace(
                    status=messages.STATUS_PENDING, challenges=[challenge]
                )
            )
        ]
    )

    def new_order(csr_pem):
        csr = x509.load_pem_x509_csr(csr_pem)
        names = csr.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
        assert names == ["*.abcd2345efgh.vpn.obs.so"]
        acme.poll_and_finalize.return_value = SimpleNamespace(
            fullchain_pem=signed_certificate(csr.public_key(), signer, names)
            .public_bytes(serialization.Encoding.PEM)
            .decode()
        )
        return order

    acme.new_order.side_effect = new_order
    factory = Mock(return_value=acme)
    monkeypatch.setattr(client, "ClientV2", factory)
    broker = Mock()
    monkeypatch.setattr(certificates, "dns_challenge", broker)
    monkeypatch.setattr(certificates, "wait_for_dns", Mock())
    path = certificates.ensure_certificate(service)
    assert path.is_file()
    assert broker.call_count == 2
    assert broker.call_args.kwargs == {"remove": True}
    assert len(broker.call_args.args[1]) == 43
    assert not (path.parent / "pending-dns.json").exists()
    assert not certificates.renewal_due(
        certificates.load_certificate(path, certificates.certificate_name(service))
    )
    # A due certificate renews with the same ACME account and replaces the bundle.
    bundle(service, days=0)
    certificates.ensure_certificate(service)
    assert acme.new_order.call_count == 2
    assert acme.new_account.call_count == 1


def test_inactive_session_does_not_reappear(service, monkeypatch):
    monkeypatch.setattr(published, "local_service_available", lambda *a, **k: True)
    monkeypatch.setattr(published, "_pid_is_gateway", lambda s: False)
    assert not https.is_active(replace(service, pid=123))


class LocalResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        return [
            {
                "hostname": host,
                "host": "127.0.0.1",
                "port": port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": 0,
            }
        ]

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_real_tls_host_sni_forwarded_headers_and_unknown_services(
    service, monkeypatch
):
    async def echo(request):
        return web.json_response(dict(request.headers))

    upstream = web.Application()
    upstream.router.add_get("/", echo)
    upstream_runner = web.AppRunner(upstream)
    await upstream_runner.setup()
    upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
    await upstream_site.start()
    service = replace(
        service, local_port=upstream_site._server.sockets[0].getsockname()[1]
    )
    published.save_services([service])
    path, _ = bundle(service)
    monkeypatch.setattr(https, "is_active", lambda s: True)
    app = web.Application()
    app.cleanup_ctx.append(gateway.client_session)
    app.router.add_route("*", "/{path:.*}", https.dispatch)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=https.tls_context())
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    trust = ssl.create_default_context(cafile=str(path))
    try:
        async with ClientSession(
            connector=TCPConnector(resolver=LocalResolver(), ssl=trust)
        ) as session:
            url = f"https://{service.hostname}:{port}/"
            headers = {
                "Host": service.hostname,
                "X-Forwarded-Proto": "http",
                "X-Forwarded-Host": "attacker.example",
            }
            async with session.get(url, headers=headers) as response:
                assert response.status == 200
                forwarded = await response.json()
                assert forwarded["X-Forwarded-Proto"] == "https"
                assert forwarded["X-Forwarded-Host"] == service.hostname
                assert forwarded["X-Forwarded-Port"] == "443"
            async with session.get(
                url, headers={"Host": "other.abcd2345efgh.vpn.obs.so"}
            ) as response:
                assert response.status == 421
            monkeypatch.setattr(https, "is_active", lambda s: False)
            async with session.get(url, headers={"Host": service.hostname}) as response:
                assert response.status == 404
            with pytest.raises(ClientConnectorError):
                await session.get(f"https://unknown.abcd2345efgh.vpn.obs.so:{port}/")
    finally:
        await runner.cleanup()
        await upstream_runner.cleanup()


@pytest.mark.asyncio
async def test_http_redirects_without_contacting_upstream(service):
    published.save_services([service])
    runner = web.AppRunner(gateway.create_app(service.name))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{port}/a?x=1",
                headers={"Host": service.hostname},
                allow_redirects=False,
            ) as response:
                assert response.status == 308
                assert (
                    response.headers["Location"] == f"https://{service.hostname}/a?x=1"
                )
    finally:
        await runner.cleanup()
