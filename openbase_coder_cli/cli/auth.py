"""CLI commands for authentication: browser login and logout."""

from __future__ import annotations

import html
import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import click
import httpx

from openbase_coder_cli.config.local_api_token import (
    get_local_api_token,
    rotate_local_api_token,
)
from openbase_coder_cli.config.machine_token_manager import (
    MachineTokenError,
    MachineTokenManager,
)
from openbase_coder_cli.config.token_manager import (
    DEFAULT_OAUTH_CLIENT_ID,
    DEFAULT_OAUTH_REDIRECT_URI,
    AuthLoginRequiredError,
    AuthTransientError,
    TokenManager,
    create_pkce_challenge,
    create_pkce_verifier,
)
from openbase_coder_cli.paths import AUTH_JSON_PATH, MACHINE_TOKEN_JSON_PATH
from openbase_coder_cli.services.cloud_registration import register_and_report

from .password_auth import exchange_password_for_jwts

DEFAULT_WEB_BACKEND_URL = "https://app.openbase.cloud"
DESKTOP_LOGIN_COMPLETE_URL = (
    "openbase-coder://open?source=cli-auth&intent=login-complete"
)
# How long the local OAuth callback listener waits for the browser redirect
# before giving up and releasing the port.
LOGIN_CALLBACK_TIMEOUT_SECONDS = 300.0


def _oauth_success_html(*, desktop_url: str = DESKTOP_LOGIN_COMPLETE_URL) -> bytes:
    escaped_desktop_url = html.escape(desktop_url, quote=True)
    script_desktop_url = json.dumps(desktop_url)
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>Openbase Coder login complete</title>
    <style>
      body {{
        color: #18181b;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        margin: 0;
        padding: 48px;
      }}
      main {{
        margin: 0 auto;
        max-width: 560px;
      }}
      a {{
        color: #2563eb;
      }}
    </style>
  </head>
  <body>
    <main>
      <h1>Logged in successfully</h1>
      <p>Openbase Coder has received your login. You can return to the terminal.</p>
      <p>If you started from the Mac app, it should reopen automatically.</p>
      <p><a href="{escaped_desktop_url}">Open the Mac app</a></p>
    </main>
    <script>
      window.setTimeout(function () {{
        window.location.href = {script_desktop_url};
      }}, 250);
    </script>
  </body>
</html>
""".encode("utf-8")


def _get_web_backend_url() -> str:
    return os.environ.get(
        "OPENBASE_CODER_CLI_WEB_BACKEND_URL", DEFAULT_WEB_BACKEND_URL
    ).rstrip("/")


def _get_oauth_client_id() -> str:
    return os.environ.get("OPENBASE_CODER_CLI_OAUTH_CLIENT_ID", DEFAULT_OAUTH_CLIENT_ID)


def _get_oauth_redirect_uri() -> str:
    return os.environ.get(
        "OPENBASE_CODER_CLI_OAUTH_REDIRECT_URI", DEFAULT_OAUTH_REDIRECT_URI
    )


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    server_version = "OpenbaseCoderOAuth/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        expected_path = getattr(self.server, "callback_path", "/oauth/callback")

        if parsed.path != expected_path:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        params = parse_qs(parsed.query)
        result = {key: values[0] for key, values in params.items()}

        if "code" not in result and "error" not in result:
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Missing OAuth callback parameters")
            return

        expected_state = getattr(self.server, "expected_state", "")
        if expected_state and result.get("state") != expected_state:
            self.send_response(409)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>Older login attempt ignored</h1>"
                b"<p>Please continue in the newest Openbase login tab.</p>"
                b"</body></html>"
            )
            return

        self.server.result = result
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_oauth_success_html())
        self.server.done.set()

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class _OAuthCallbackServer(HTTPServer):
    allow_reuse_address = True


def _parse_pasted_oauth_callback(pasted: str, expected_state: str) -> dict[str, str]:
    pasted = pasted.strip()
    if "://" in pasted or "?" in pasted or "code=" in pasted:
        if "?" in pasted:
            query = pasted.split("?", 1)[1]
        elif "://" in pasted:
            query = urlparse(pasted).query
        else:
            query = pasted
        q = parse_qs(query)
        code = q.get("code", [""])[0]
        state = q.get("state", [""])[0]
        error = q.get("error", [""])[0]
        error_desc = q.get("error_description", [""])[0]
        if code:
            return {
                "code": code,
                "state": state,
                "error": error,
                "error_description": error_desc,
            }

    if "&state=" in pasted:
        parts = pasted.split("&state=", 1)
        code_part = parts[0]
        if code_part.startswith("code="):
            code_part = code_part[5:]
        return {"code": code_part, "state": parts[1]}

    if pasted.startswith("code="):
        pasted = pasted[5:]
    return {"code": pasted, "state": expected_state}


def _prompt_for_pasted_callback(expected_state: str) -> dict[str, str]:
    click.echo(
        "Please complete the sign-in in your browser, then copy the full redirect URL\n"
        "(e.g. http://127.0.0.1:52807/oauth/callback?code=...&state=...) from your browser's address bar and paste it below:\n"
    )
    pasted = click.prompt("Redirect URL or code").strip()
    return _parse_pasted_oauth_callback(pasted, expected_state)


def _wait_for_callback(
    redirect_uri: str,
    *,
    expected_state: str = "",
    timeout_seconds: float = LOGIN_CALLBACK_TIMEOUT_SECONDS,
) -> dict[str, str]:
    parsed = urlparse(redirect_uri)
    try:
        server = _OAuthCallbackServer(
            (parsed.hostname or "127.0.0.1", parsed.port or 80),
            _OAuthCallbackHandler,
        )
    except OSError as exc:
        click.echo(
            click.style(
                f"\nWarning: Could not bind callback listener on {redirect_uri} ({exc}).",
                fg="yellow",
            )
        )
        return _prompt_for_pasted_callback(expected_state)

    server.timeout = 1
    server.done = threading.Event()
    server.result = {}
    server.callback_path = parsed.path or "/oauth/callback"
    server.expected_state = expected_state
    # Bounded: an abandoned browser flow must not pin the callback port
    # forever. A listener that never returns keeps the port bound for the
    # life of the process, and every later login attempt then falls back to
    # manual paste because it cannot bind.
    deadline = time.monotonic() + timeout_seconds
    try:
        while not server.done.wait(timeout=0):
            if time.monotonic() >= deadline:
                break
            server.handle_request()
    finally:
        server.server_close()

    if not server.done.is_set():
        click.echo(
            click.style(
                f"\nTimed out after {int(timeout_seconds)}s waiting for the browser "
                "callback; released the callback port.",
                fg="yellow",
            )
        )
        return _prompt_for_pasted_callback(expected_state)

    return server.result


def _exchange_oauth_code(
    *, web_backend_url: str, code: str, redirect_uri: str, code_verifier: str
) -> str:
    token_url = f"{web_backend_url}/o/token/"
    response = httpx.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "client_id": _get_oauth_client_id(),
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    access_token = payload.get("access_token", "")
    if not access_token:
        raise click.ClickException(
            "OAuth login succeeded but no access token was returned."
        )
    return access_token


def _exchange_oauth_token_for_jwts(
    *, web_backend_url: str, oauth_access_token: str
) -> tuple[str, str, int]:
    exchange_url = f"{web_backend_url}/api/openbase/auth/cli/token-exchange/"
    response = httpx.post(
        exchange_url,
        headers={"Authorization": f"Bearer {oauth_access_token}"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    access_token = payload.get("access_token", "")
    refresh_token = payload.get("refresh_token", "")
    expires_in = int(payload.get("access_token_expires_in") or 300)
    if not access_token or not refresh_token:
        raise click.ClickException(
            "Token exchange succeeded but no JWT access/refresh token pair was returned."
        )
    return access_token, refresh_token, expires_in


def _complete_login(
    *, web_backend_url: str, access_token: str, refresh_token: str, expires_in: int
) -> None:
    manager = TokenManager(web_backend_url)
    manager.store_tokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )
    try:
        MachineTokenManager(web_backend_url, manager).get_machine_token(rotate=True)
    except (
        AuthLoginRequiredError,
        AuthTransientError,
        MachineTokenError,
        httpx.HTTPError,
    ) as exc:
        click.echo(
            click.style(
                f"Warning: logged in, but could not create an Openbase Cloud machine token: {exc}",
                fg="yellow",
            )
        )

    report = register_and_report()
    if not report.ok and report.supported:
        click.echo(
            click.style(
                f"Warning: logged in, but could not register this device with Openbase Cloud: {report.error}",
                fg="yellow",
            )
        )

    click.echo(f"Logged in successfully. Tokens saved to {AUTH_JSON_PATH}")


@click.command()
@click.option("--email", help="Email address for a non-browser password login.")
@click.option(
    "--password-stdin",
    is_flag=True,
    help="Read the password from standard input instead of process arguments.",
)
def login(email: str | None, password_stdin: bool) -> None:
    """Log in with browser OAuth or an explicitly requested stdin password."""
    web_backend_url = _get_web_backend_url()
    if bool(email) != password_stdin:
        raise click.UsageError("--email and --password-stdin must be used together")
    if email and password_stdin:
        password = click.get_text_stream("stdin").read()
        if password.endswith("\n"):
            password = password[:-1]
            if password.endswith("\r"):
                password = password[:-1]
        if not password:
            raise click.UsageError("No password was received on standard input")
        try:
            access_token, refresh_token, expires_in = exchange_password_for_jwts(
                web_backend_url=web_backend_url,
                email=email,
                password=password,
            )
        except httpx.HTTPStatusError as exc:
            raise click.ClickException(
                f"Password login failed with status {exc.response.status_code}."
            ) from None
        _complete_login(
            web_backend_url=web_backend_url,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
        )
        return

    redirect_uri = _get_oauth_redirect_uri()
    state = os.urandom(24).hex()
    code_verifier = create_pkce_verifier()
    code_challenge = create_pkce_challenge(code_verifier)

    authorize_url = urljoin(web_backend_url + "/", "o/authorize/")
    query = urlencode(
        {
            "response_type": "code",
            "client_id": _get_oauth_client_id(),
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    auth_url = f"{authorize_url}?{query}"

    click.echo("Opening browser for Openbase login...")
    click.echo(auth_url)
    webbrowser.open(auth_url)
    click.echo(
        "Waiting for you to finish logging in in the browser... "
        "(if no tab opened, open the URL above; Ctrl-C to abort)"
    )

    callback = _wait_for_callback(redirect_uri, expected_state=state)
    if callback.get("state") != state:
        raise click.ClickException("OAuth callback state did not match.")

    error = callback.get("error")
    if error:
        description = callback.get("error_description") or error
        raise click.ClickException(f"OAuth login failed: {description}")

    code = callback.get("code")
    if not code:
        raise click.ClickException("OAuth login failed: missing authorization code.")

    try:
        oauth_access_token = _exchange_oauth_code(
            web_backend_url=web_backend_url,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )
        access_token, refresh_token, expires_in = _exchange_oauth_token_for_jwts(
            web_backend_url=web_backend_url,
            oauth_access_token=oauth_access_token,
        )
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        try:
            detail = json.dumps(exc.response.json())
        except ValueError:
            pass
        raise click.ClickException(
            f"OAuth login failed: {exc.response.status_code} — {detail}"
        ) from None

    _complete_login(
        web_backend_url=web_backend_url,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )


@click.command()
def logout() -> None:
    """Log out and clear stored tokens."""
    if AUTH_JSON_PATH.is_file():
        AUTH_JSON_PATH.unlink()
        if MACHINE_TOKEN_JSON_PATH.is_file():
            MACHINE_TOKEN_JSON_PATH.unlink()
        click.echo("Logged out. Tokens removed.")
    else:
        click.echo("Not logged in (no stored tokens found).")


@click.group()
def auth() -> None:
    """Authentication helpers."""


@auth.command("print-local-api-token")
@click.option(
    "--rotate",
    is_flag=True,
    help="Invalidate the current installation capability and print a new one.",
)
def print_local_api_token(rotate: bool) -> None:
    """Print the owner-only capability for this local Coder runtime."""
    click.echo(rotate_local_api_token() if rotate else get_local_api_token())


@auth.command("open-console")
@click.option("--port", type=int, default=None, help="Override the local API port.")
def open_console(port: int | None) -> None:
    """Open a browser console with the local capability delivered in-fragment."""
    resolved_port = port or int(os.environ.get("OPENBASE_CODER_CLI_PORT", "7999"))
    fragment = urlencode({"openbase-local-token": get_local_api_token()})
    url = f"http://127.0.0.1:{resolved_port}/#{fragment}"
    if not webbrowser.open(url):
        raise click.ClickException("Could not open the local browser console.")
    click.echo("Opened the authenticated local console.")


@auth.command("status")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print machine-readable JSON (always exits 0).",
)
def auth_status(as_json: bool) -> None:
    """Report validated Openbase Cloud login status.

    Unlike a token-file presence check, this validates the stored refresh
    token against Openbase Cloud (with short-lived caching), so an expired
    or revoked login reports ``login_expired`` instead of logged in.
    """
    manager = TokenManager(_get_web_backend_url())
    login = manager.login_status()
    if as_json:
        identity = manager.get_owner_identity()
        click.echo(json.dumps({**login, "email": identity.get("email", "")}))
        return
    if login["status"] == "logged_in":
        email = manager.get_owner_identity().get("email", "")
        suffix = f" as {email}" if email else ""
        click.echo(f"Logged in{suffix}")
        if not login["validated"]:
            click.echo(click.style(f"Warning: {login['detail']}", fg="yellow"))
    elif login["status"] == "login_expired":
        raise click.ClickException(
            "Login expired: Openbase Cloud rejected the stored login. "
            "Run 'openbase-coder login' again."
        )
    else:
        raise click.ClickException("Not logged in. Run 'openbase-coder login'.")


@auth.command("print-access-token")
def print_access_token() -> None:
    """Print a fresh Openbase JWT access token for command-backed auth."""
    try:
        token = TokenManager(_get_web_backend_url()).get_access_token()
    except AuthLoginRequiredError as exc:
        raise click.ClickException(
            "Login required. Run `openbase-coder login` first."
        ) from exc
    except AuthTransientError as exc:
        raise click.ClickException(f"Unable to refresh Openbase token: {exc}") from exc
    if not token:
        raise click.ClickException("Openbase token refresh returned an empty token.")
    click.echo(token)


@auth.command("print-machine-token")
@click.option(
    "--rotate",
    is_flag=True,
    help="Mint a fresh machine token even if a cached token exists.",
)
def print_machine_token(rotate: bool) -> None:
    """Print a stable Openbase Cloud proxy machine token."""
    try:
        token = MachineTokenManager(_get_web_backend_url()).get_machine_token(
            rotate=rotate
        )
    except AuthLoginRequiredError as exc:
        raise click.ClickException(
            "Login required. Run `openbase-coder login` first."
        ) from exc
    except AuthTransientError as exc:
        raise click.ClickException(
            f"Unable to refresh Openbase login or mint machine token: {exc}"
        ) from exc
    except (MachineTokenError, httpx.HTTPError) as exc:
        raise click.ClickException(
            f"Unable to mint Openbase machine token: {exc}"
        ) from exc
    if not token:
        raise click.ClickException(
            "Openbase machine token command returned empty token."
        )
    click.echo(token)
