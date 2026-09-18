import logging

from uvicorn.logging import AccessFormatter

from openbase_coder_cli.logging_redaction import CredentialRedactionFilter, install_uvicorn_credential_redaction


def test_websocket_accept_log_redacts_query_credential():
    record = logging.LogRecord("uvicorn.error", logging.INFO, "", 0,
        '%s - "WebSocket %s" [accepted]', ("peer", "/ws/ios-app-control/?token=private-value&mode=test"), None)
    CredentialRedactionFilter().filter(record)
    assert "private-value" not in record.getMessage()
    assert "mode=test" in record.getMessage()


def test_access_formatter_keeps_status_code_type_and_redacts_url():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
        '%s - "%s %s HTTP/%s" %d', ("peer", "GET", "/api/?session_token=private-value", "1.1", 200), None)
    CredentialRedactionFilter().filter(record)
    rendered = AccessFormatter('%(request_line)s %(status_code)s', use_colors=False).format(record)
    assert "private-value" not in rendered
    assert "200" in rendered
    assert record.args[-1] == 200


def test_filter_install_is_idempotent():
    install_uvicorn_credential_redaction()
    install_uvicorn_credential_redaction()
    for name in ("uvicorn.error", "uvicorn.access"):
        assert sum(isinstance(item, CredentialRedactionFilter) for item in logging.getLogger(name).filters) == 1
