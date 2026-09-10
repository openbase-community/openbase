"""Read-only, layered publication diagnostics; never repairs or republishes."""

from __future__ import annotations

import socket
from datetime import UTC, datetime

from cryptography.x509 import ExtensionNotFound

from openbase_coder_cli.services import published_services as published
from openbase_coder_cli.services import service_certificates as certificates
from openbase_coder_cli.services import service_recovery
from openbase_coder_cli.services import tailscale_provider as provider


def diagnose(service):
    checks = []

    def add(name, ok, message):
        checks.append({"check": name, "ok": bool(ok), "message": message})

    if service_recovery.journal_path().exists():
        add(
            "transaction",
            False,
            "Interrupted publication; run openbase-coder service recover",
        )

    status = provider.status_json()
    running = status.get("BackendState") == "Running" and not status.get("error")
    add(
        "vpn",
        running,
        "VPN connected" if running else "VPN disconnected or helper unavailable",
    )
    try:
        addresses = {
            a[4][0]
            for a in socket.getaddrinfo(service.hostname, 443, type=socket.SOCK_STREAM)
        }
        own = set(status.get("Self", {}).get("TailscaleIPs") or [])
        ok = bool(addresses) and addresses <= own
        add(
            "dns",
            ok,
            "Hostname resolves to this device"
            if ok
            else "Hostname does not resolve exclusively to this device",
        )
    except OSError:
        add("dns", False, "Private hostname DNS lookup failed")
    try:
        # Do not call certificate_directory(): it creates/chmods directories.
        name = certificates.certificate_name(service)
        path = (
            certificates.PUBLISHED_SERVICES_PATH.parent
            / "service-certificates"
            / name[2:]
            / "certificate.pem"
        )
        cert = certificates.load_certificate(path, name)
        now = datetime.now(UTC)
        ok = cert.not_valid_before_utc <= now < cert.not_valid_after_utc
        message = (
            f"Certificate expires {cert.not_valid_after_utc.isoformat()}"
            if ok
            else "Certificate expired or not yet valid"
        )
        add("certificate", ok, message)
    except (OSError, ValueError, ExtensionNotFound):
        add("certificate", False, "Certificate missing, invalid, or inaccessible")
    routes = provider.serve_status_json()
    target = (routes.get("TCP") or {}).get("443", {}).get("TCPForward")
    add(
        "route",
        target == f"127.0.0.1:{published.HTTPS_PROXY_PORT}",
        "VPN HTTPS forwarding configured"
        if target == f"127.0.0.1:{published.HTTPS_PROXY_PORT}"
        else "VPN HTTPS forwarding missing or incorrect",
    )
    app_ok = published.local_service_available(service.local_port)
    add(
        "application",
        app_ok,
        "Local application is listening"
        if app_ok
        else "Local application stopped or port unavailable",
    )
    proxy_ok = published.gateway_healthy(service, timeout=1)
    add(
        "gateway",
        proxy_ok,
        "HTTPS gateway is reachable"
        if proxy_ok
        else "Gateway stopped, TLS rejected, or publication inactive",
    )
    return {
        "name": service.name,
        "url": published.service_url(service),
        "ready": all(c["ok"] for c in checks),
        "checks": checks,
    }
