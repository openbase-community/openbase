"""Device-local wildcard certificates, using Cloud's account-scoped DNS-01 broker."""

from __future__ import annotations

import json
import os
import re
import ssl
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from openbase_coder_cli.file_lock import LOCK_EX, LOCK_UN, flock
from openbase_coder_cli.paths import PUBLISHED_SERVICES_PATH
from openbase_coder_cli.services.published_services import PublishedService

ACME_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
ACME_TEST_DIRECTORY = "https://acme-staging-v02.api.letsencrypt.org/directory"
DNS_ENDPOINT = "/api/openbase/netmesh/certificate-dns/"


def certificate_name(service: PublishedService) -> str:
    hostname = service.hostname or ""
    if (
        re.fullmatch(
            r"[a-z][a-z0-9-]*\.[a-z2-7]{12}\.vpn(?:-staging)?\.obs\.so", hostname
        )
        is None
    ):
        raise ValueError("HTTPS requires a current account-private service hostname.")
    return "*." + hostname.split(".", 1)[1]


def certificate_directory(service: PublishedService, *, staging=False) -> Path:
    namespace = certificate_name(service)[2:]
    path = PUBLISHED_SERVICES_PATH.parent / "service-certificates" / namespace
    if staging:
        path /= "acme-staging"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


@contextmanager
def private_lock(path: Path):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a+b") as handle:
        os.fchmod(handle.fileno(), 0o600)
        flock(handle, LOCK_EX)
        try:
            yield
        finally:
            flock(handle, LOCK_UN)


def private_write(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".certificate-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_certificate(path: Path, name: str) -> x509.Certificate:
    data = path.read_bytes()
    certificate = x509.load_pem_x509_certificate(data)
    names = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    if names.get_values_for_type(x509.DNSName) != [name]:
        raise ValueError("Certificate contains unexpected names.")
    key = serialization.load_pem_private_key(data, password=None)
    if key.public_key() != certificate.public_key():
        raise ValueError("Certificate key does not match.")
    return certificate


def renewal_due(certificate: x509.Certificate) -> bool:
    lifetime = certificate.not_valid_after_utc - certificate.not_valid_before_utc
    return datetime.now(UTC) >= certificate.not_valid_after_utc - lifetime / 3


def dns_challenge(service: PublishedService, validation: str, *, remove=False) -> None:
    from openbase_coder_cli.services.cloud_registration import _post_to_cloud

    result = _post_to_cloud(
        DNS_ENDPOINT,
        {"node_id": service.node_id, "validation": validation},
        method="DELETE" if remove else "POST",
    )
    if not result.ok:
        raise RuntimeError(
            f"Certificate DNS {'cleanup' if remove else 'validation'} failed: {result.error}"
        )
    if not remove and result.response.get("certificate_name") != certificate_name(
        service
    ):
        raise RuntimeError(
            "Cloud returned a certificate namespace belonging to another account."
        )


def wait_for_dns(name: str, validation: str) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        response = httpx.get(
            "https://cloudflare-dns.com/dns-query",
            params={"name": name, "type": "TXT"},
            headers={"Accept": "application/dns-json"},
            timeout=15,
        )
        response.raise_for_status()
        if any(
            answer.get("data", "").strip('"') == validation
            for answer in response.json().get("Answer", [])
        ):
            return
        time.sleep(5)
    raise RuntimeError("Certificate validation TXT record did not propagate in time.")


def ensure_certificate(service: PublishedService, *, staging=False) -> Path:
    import josepy
    from acme import challenges, client, errors, messages

    directory = certificate_directory(service, staging=staging)
    bundle = directory / "certificate.pem"
    name = certificate_name(service)
    with private_lock(directory / "issuance.lock"):
        journal = directory / "pending-dns.json"
        if journal.exists():
            for validation in json.loads(journal.read_text()):
                dns_challenge(service, validation, remove=True)
            journal.unlink()
        if bundle.exists() and not renewal_due(load_certificate(bundle, name)):
            return bundle
        account_key_file = directory / "account.key"
        if not account_key_file.exists():
            account_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            private_write(
                account_key_file,
                account_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
            )
        account_key = josepy.JWKRSA(
            key=serialization.load_pem_private_key(
                account_key_file.read_bytes(), password=None
            )
        )
        account_file = directory / "account.json"
        account = (
            messages.RegistrationResource.json_loads(account_file.read_text())
            if account_file.exists()
            else None
        )
        network = client.ClientNetwork(
            account_key,
            account=account,
            user_agent="Openbase-private-services",
            timeout=30,
        )
        acme = client.ClientV2(
            client.ClientV2.get_directory(
                ACME_TEST_DIRECTORY if staging else ACME_DIRECTORY, network
            ),
            network,
        )
        if account is None:
            try:
                account = acme.new_account(
                    messages.NewRegistration.from_data(terms_of_service_agreed=True)
                )
            except errors.ConflictError as conflict:
                # Recover an account created before an interrupted local write.
                account = acme.query_registration(
                    messages.RegistrationResource(uri=conflict.location)
                )
            private_write(account_file, account.json_dumps().encode())
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([]))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False
            )
            .sign(key, hashes.SHA256())
        )
        order = acme.new_order(csr.public_bytes(serialization.Encoding.PEM))
        pending = []
        try:
            for authorization in order.authorizations:
                if authorization.body.status == messages.STATUS_VALID:
                    continue
                challenge = next(
                    c
                    for c in authorization.body.challenges
                    if isinstance(c.chall, challenges.DNS01)
                )
                response, validation = challenge.response_and_validation(account_key)
                pending.append(validation)
                private_write(journal, json.dumps(pending).encode())
                dns_challenge(service, validation)
                wait_for_dns("_acme-challenge." + name[2:], validation)
                acme.answer_challenge(challenge, response)
            issued = acme.poll_and_finalize(
                order, deadline=datetime.now() + timedelta(minutes=5)
            )
            content = issued.fullchain_pem.encode() + key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            candidate = directory / "candidate.pem"
            private_write(candidate, content)
            certificate = load_certificate(candidate, name)
            if (
                not certificate.not_valid_before_utc
                <= datetime.now(UTC)
                < certificate.not_valid_after_utc
            ):
                raise ValueError("Issued certificate is not currently valid.")
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(candidate)
            os.replace(candidate, bundle)
        finally:
            for validation in pending:
                dns_challenge(service, validation, remove=True)
            journal.unlink(missing_ok=True)
        return bundle
