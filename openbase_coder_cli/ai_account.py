"""Optional "AI account" linking for a workspace: Codex or Claude Code.

By default a workspace uses Openbase Cloud and needs no separate AI account.
The phone may instead link the user's own Codex (ChatGPT) or Claude Code
account. The login always runs here, on the computer that will use it; the
phone only completes the browser step:

* ``codex login`` redirects to ``http://localhost:1455``. It opens its page
  through ``$BROWSER`` (``openbase-browser``), which delivers it to the phone
  and asks the phone to forward its own localhost:1455 back here over the
  Openbase VPN, so the redirect reaches the waiting CLI.
* ``claude auth login`` ends on a page that shows a code. The phone shows the
  page and a field for that code, which is typed into the waiting CLI.

Only one login runs at a time. Output is kept in memory only; neither the
sign-in URL's state nor any pasted code is logged.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from openbase_coder_cli.backend_binaries import find_backend_binary
from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    OPENBASE_CLOUD_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
)
from openbase_coder_cli.paths import CODEX_HOME_DIR, DEFAULT_ENV_FILE_PATH

OPENBASE_CLOUD = "openbase_cloud"
CODEX = CODEX_BACKEND
CLAUDE_CODE = CLAUDE_CODE_BACKEND
PROVIDERS = (CODEX, CLAUDE_CODE)
CHOICES = (OPENBASE_CLOUD, *PROVIDERS)

LABELS = {
    OPENBASE_CLOUD: "Openbase Cloud",
    CODEX: "Codex (ChatGPT)",
    CLAUDE_CODE: "Claude Code",
}

LOGIN_TIMEOUT_SECONDS = 15 * 60
MAX_OUTPUT_CHARS = 20_000
MAX_CODE_LENGTH = 512

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")
_URL = re.compile(r"https://[^\s\"'<>\x1b]+")


def selected_choice() -> str:
    """Which account this workspace's agents use right now."""
    from openbase_coder_cli.cli.backend import read_backend

    backend = read_backend(DEFAULT_ENV_FILE_PATH)
    if backend in (OPENBASE_CLOUD_BACKEND, OPENBASE_CLOUD_CODEX_BACKEND):
        return OPENBASE_CLOUD
    if backend in PROVIDERS:
        return backend
    return OPENBASE_CLOUD


def is_linked(provider: str) -> bool:
    if provider == CODEX:
        from openbase_coder_cli.services.onboarding import codex_auth_present

        return codex_auth_present()
    if provider == CLAUDE_CODE:
        return _claude_status().logged_in
    return provider == OPENBASE_CLOUD


_CLAUDE_STATUS_TTL_SECONDS = 10.0
_claude_status_cache: tuple[float, object] | None = None


def _claude_status():
    """`claude auth status`, cached briefly: the phone polls during a login."""
    global _claude_status_cache
    from openbase_coder_cli.claude_auth import claude_auth_status

    now = time.monotonic()
    if (
        _claude_status_cache
        and now - _claude_status_cache[0] < _CLAUDE_STATUS_TTL_SECONDS
    ):
        return _claude_status_cache[1]
    status = claude_auth_status(timeout=15)
    _claude_status_cache = (now, status)
    return status


def _forget_claude_status() -> None:
    global _claude_status_cache
    _claude_status_cache = None


def linked_account(provider: str) -> str | None:
    """The signed-in account's email (or name) for display, when known.

    Codex keeps it in the ID token's claims (read without verification:
    display only, never trusted); Claude Code reports it in `auth status`.
    """
    import base64
    import json

    if provider == CODEX:
        try:
            payload = json.loads(
                (CODEX_HOME_DIR / "auth.json").read_text(encoding="utf-8")
            )
            token = payload["tokens"]["id_token"]
            body = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
            return None
        value = claims.get("email") or claims.get("name")
        return str(value) if value else None
    if provider == CLAUDE_CODE:
        status = _claude_status()
        try:
            payload = json.loads(status.raw_output)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        value = (
            payload.get("email")
            or payload.get("emailAddress")
            or payload.get("account")
        )
        return str(value) if isinstance(value, str) and value else None
    return None


def is_available(provider: str) -> bool:
    """Whether this computer has the provider's CLI to run the login with."""
    if provider == OPENBASE_CLOUD:
        return True
    name = "codex" if provider == CODEX else "claude"
    return bool(find_backend_binary(name) or shutil.which(name))


def _command(provider: str) -> list[str]:
    if provider == CODEX:
        return [
            str(find_backend_binary("codex") or shutil.which("codex") or "codex"),
            "login",
        ]
    return [
        str(find_backend_binary("claude") or shutil.which("claude") or "claude"),
        "auth",
        "login",
        "--claudeai",
    ]


def _logout_command(provider: str) -> list[str]:
    if provider == CODEX:
        return [
            str(find_backend_binary("codex") or shutil.which("codex") or "codex"),
            "logout",
        ]
    return [
        str(find_backend_binary("claude") or shutil.which("claude") or "claude"),
        "auth",
        "logout",
    ]


def _login_env() -> dict[str, str]:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(CODEX_HOME_DIR)
    # The CLI's browser step goes to the phone, with the callback forwarded.
    from openbase_coder_cli.pty_session import phone_browser_command

    env["BROWSER"] = phone_browser_command()
    env.setdefault("GH_BROWSER", env["BROWSER"])
    env.setdefault("TERM", "xterm-256color")
    # Never let a Django settings module leak into the child CLIs.
    env.pop("DJANGO_SETTINGS_MODULE", None)
    return env


@dataclass
class LoginJob:
    provider: str
    process: subprocess.Popen | None = None
    master_fd: int | None = None
    started_at: float = field(default_factory=time.monotonic)
    output: str = ""
    url: str | None = None
    needs_code: bool = False
    code_sent: bool = False
    state: str = "starting"  # starting | waiting | succeeded | failed | cancelled
    message: str = ""
    # The credentials as they were before this login (path -> bytes, or None
    # when absent), put back if it does not succeed.
    saved_credentials: dict[Path, bytes | None] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "provider": self.provider,
                "state": self.state,
                "url": self.url,
                "needs_code": self.needs_code and not self.code_sent,
                "message": self.message,
                "elapsed_seconds": int(time.monotonic() - self.started_at),
            }


class LoginManager:
    """One login process at a time, read through a pseudo-terminal.

    A pty because Claude Code reads the pasted code from an interactive
    prompt, and both CLIs print their sign-in URL only to a terminal.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: LoginJob | None = None

    def current(self) -> dict | None:
        with self._lock:
            job = self._job
        return job.snapshot() if job else None

    def start(self, provider: str) -> dict:
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown AI account: {provider}")
        self.cancel()
        import pty

        master_fd, slave_fd = pty.openpty()
        job = LoginJob(
            provider=provider,
            master_fd=master_fd,
            # `codex login` deletes auth.json as it starts, so a relink that
            # is cancelled or fails would otherwise sign a working account
            # out (QA on Maritime 376).
            saved_credentials=_snapshot_credentials(provider),
        )
        try:
            job.process = subprocess.Popen(
                _command(provider),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=_login_env(),
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            os.close(master_fd)
            os.close(slave_fd)
            raise RuntimeError(
                f"{LABELS[provider]} CLI is not available here: {exc}"
            ) from exc
        os.close(slave_fd)
        with self._lock:
            self._job = job
        threading.Thread(target=self._read, args=(job,), daemon=True).start()
        threading.Thread(target=self._watch, args=(job,), daemon=True).start()
        return job.snapshot()

    def submit_code(self, code: str) -> dict:
        code = code.strip()
        if not code or len(code) > MAX_CODE_LENGTH or any(ord(c) < 32 for c in code):
            raise ValueError("Paste the code exactly as the sign-in page shows it.")
        with self._lock:
            job = self._job
        if (
            job is None
            or job.state not in ("starting", "waiting")
            or job.master_fd is None
        ):
            raise ValueError("No sign-in is waiting for a code.")
        os.write(job.master_fd, code.encode("utf-8") + b"\r")
        with job.lock:
            job.code_sent = True
            job.message = "Code sent; finishing sign-in…"
        return job.snapshot()

    def cancel(self) -> None:
        with self._lock:
            job = self._job
        if job is None or job.process is None or job.process.poll() is not None:
            return
        with job.lock:
            job.state = "cancelled"
            job.message = "Sign-in cancelled."
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(job.process.pid, signal.SIGTERM)

    def _read(self, job: LoginJob) -> None:
        assert job.master_fd is not None
        while True:
            try:
                chunk = os.read(job.master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            text = _ANSI.sub("", chunk.decode("utf-8", errors="replace"))
            with job.lock:
                job.output = (job.output + text)[-MAX_OUTPUT_CHARS:]
                urls = _URL.findall(job.output)
                if urls:
                    job.url = urls[-1].rstrip(".,)")
                    if job.state == "starting":
                        job.state = "waiting"
                        job.message = "Finish signing in on your phone."
                        if browser_env_ignored():
                            _open_on_phone(job.url)
                        # Claude Code takes a pasted code for as long as its
                        # sign-in page is open, so the paste field is offered
                        # with the link rather than inferred from the prompt
                        # wording (which can change and stall the sign-in).
                        job.needs_code = job.provider == CLAUDE_CODE
        with contextlib.suppress(OSError):
            os.close(job.master_fd)

    def _watch(self, job: LoginJob) -> None:
        assert job.process is not None
        try:
            returncode = job.process.wait(timeout=LOGIN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(job.process.pid, signal.SIGTERM)
            with job.lock:
                job.state = "failed"
                job.message = "Sign-in timed out. Start again when you are ready."
            _restore_credentials(job.saved_credentials)
            _forget_claude_status()
            return
        with job.lock:
            cancelled = job.state == "cancelled"
        if cancelled:
            _restore_credentials(job.saved_credentials)
            _forget_claude_status()
            return
        _forget_claude_status()
        linked = is_linked(job.provider)
        with job.lock:
            if returncode == 0 and linked:
                from openbase_coder_cli.backend_auth import clear_relink_needed

                clear_relink_needed(job.provider)
                job.state = "succeeded"
                job.message = f"{LABELS[job.provider]} account linked."
            else:
                job.state = "failed"
                job.message = (
                    f"{LABELS[job.provider]} sign-in did not finish"
                    f" (exit {returncode}). Start again to retry."
                )
        if job.state == "failed":
            _restore_credentials(job.saved_credentials)
            _forget_claude_status()


def _credential_paths(provider: str) -> list[Path]:
    """Files holding ``provider``'s CLI login on this computer.

    Claude Code on macOS keeps its login in the Keychain, which a cancelled
    `claude auth login` leaves alone; only file-based logins need guarding.
    """
    if provider == CODEX:
        return [CODEX_HOME_DIR / "auth.json"]
    if provider == CLAUDE_CODE:
        from openbase_coder_cli.paths import CLAUDE_CONFIG_DIR

        return [CLAUDE_CONFIG_DIR / ".credentials.json"]
    return []


def _snapshot_credentials(provider: str) -> dict[Path, bytes | None]:
    saved: dict[Path, bytes | None] = {}
    for path in _credential_paths(provider):
        try:
            saved[path] = path.read_bytes()
        except OSError:
            saved[path] = None
    return saved


def _restore_credentials(saved: dict[Path, bytes | None]) -> None:
    """Put back a login an unfinished sign-in removed or replaced."""
    for path, content in saved.items():
        if content is None:
            continue
        try:
            if path.read_bytes() == content:
                continue
        except OSError:
            pass
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.restore")
            tmp.write_bytes(content)
            tmp.chmod(0o600)
            os.replace(tmp, path)


def browser_env_ignored() -> bool:
    """Whether CLIs here ignore $BROWSER and open this computer's own browser.

    On macOS the common URL openers (Rust `webbrowser`, Node `open`) go
    straight to LaunchServices, so the phone shim never runs: send the
    printed sign-in URL to the phone ourselves.
    """
    import platform

    return platform.system() == "Darwin"


def _open_on_phone(url: str) -> None:
    """`openbase-coder browser open URL` in the background (phone + forward)."""
    import sys

    def run() -> None:
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                [sys.executable, "-m", "openbase_coder_cli", "browser", "open", url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=90,
                check=False,
            )

    threading.Thread(target=run, daemon=True).start()


LOGINS = LoginManager()


def select(choice: str) -> bool:
    """Make ``choice`` the account this workspace's agents use.

    Returns whether anything changed; the dispatcher restarts when it did.
    """
    from openbase_coder_cli.backend_config import CODING_BACKENDS_ENV_KEY
    from openbase_coder_cli.cli.backend import (
        BACKEND_ENV_KEY,
        schedule_backend_restart,
        write_backend_location,
    )
    from openbase_coder_cli.env_file import upsert_env_file_values

    if choice not in CHOICES:
        raise ValueError(f"Unknown AI account: {choice}")
    if choice != OPENBASE_CLOUD and not is_linked(choice):
        raise ValueError(f"Link your {LABELS[choice]} account first.")
    if selected_choice() == choice:
        return False
    if choice == OPENBASE_CLOUD:
        write_backend_location(DEFAULT_ENV_FILE_PATH, "cloud")
    else:
        # The linked engine runs new work; the other mixed backend keeps
        # earlier threads visible.
        companion = OPENBASE_CLOUD_BACKEND if choice == CODEX else CODEX_BACKEND
        DEFAULT_ENV_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not DEFAULT_ENV_FILE_PATH.exists():
            DEFAULT_ENV_FILE_PATH.write_text("", encoding="utf-8")
        upsert_env_file_values(
            DEFAULT_ENV_FILE_PATH,
            {
                BACKEND_ENV_KEY: choice,
                CODING_BACKENDS_ENV_KEY: f"{choice},{companion}",
            },
        )
    if choice != OPENBASE_CLOUD:
        _align_role_models(choice)
    schedule_backend_restart()
    return True


def _align_role_models(provider: str) -> None:
    """Drop role model choices that would route work away from ``provider``.

    Role models are backend-independent and their model implies the engine,
    so a Claude model left as the default would keep new work off a just
    linked Codex account (and the reverse). Clearing them lets the linked
    engine's own default apply; the user can pick another model later.
    """
    from openbase_coder_cli.dispatcher_config import (
        CLAUDE_ENGINE,
        CODEX_ENGINE,
        ROLE_MODELS_KEY,
        _write_dispatcher_config,
        model_engine,
        read_dispatcher_config,
    )

    wanted = CODEX_ENGINE if provider == CODEX else CLAUDE_ENGINE
    payload = read_dispatcher_config()
    role_models = payload.get(ROLE_MODELS_KEY)
    if not isinstance(role_models, dict):
        return
    kept = {
        role: model
        for role, model in role_models.items()
        if not isinstance(model, str) or model_engine(model) in (wanted, None)
    }
    if kept != role_models:
        from openbase_coder_cli.paths import CODEX_DISPATCHER_CONFIG_PATH

        _write_dispatcher_config(
            {**payload, ROLE_MODELS_KEY: kept}, CODEX_DISPATCHER_CONFIG_PATH
        )


def unlink(provider: str) -> None:
    """Sign the workspace out of ``provider``; falls back to Openbase Cloud."""
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown AI account: {provider}")
    if selected_choice() == provider:
        select(OPENBASE_CLOUD)
    from openbase_coder_cli.backend_auth import clear_relink_needed

    clear_relink_needed(provider)
    _forget_claude_status()
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            _logout_command(provider),
            env=_login_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )


def login_homes() -> dict[str, str]:
    """Where this computer's agents keep their Codex and Claude Code logins.

    The API process is the source of truth: a login written anywhere else
    (an agent shell with a different HOME or CODEX_HOME) links nothing.
    """
    from openbase_coder_cli.paths import CLAUDE_CONFIG_DIR

    return {
        "codex_home": str(CODEX_HOME_DIR),
        "claude_config_dir": str(CLAUDE_CONFIG_DIR),
    }


def status() -> dict:
    from openbase_coder_cli.backend_auth import relink_needed_backends

    selected = selected_choice()
    failed_logins = relink_needed_backends()
    options = []
    homes = login_homes()
    for choice in CHOICES:
        linked = True if choice == OPENBASE_CLOUD else is_linked(choice)
        options.append(
            {
                "id": choice,
                "label": LABELS[choice],
                "available": is_available(choice),
                "linked": linked,
                "account": None
                if choice == OPENBASE_CLOUD or not linked
                else linked_account(choice),
                "selected": choice == selected,
                # Its login failed during a turn, or it is in use without a
                # login: agents cannot run on it until it is relinked.
                "needs_relink": choice != OPENBASE_CLOUD
                and (choice in failed_logins or (choice == selected and not linked)),
            }
        )
    return {
        "homes": homes,
        "selected": selected,
        "default": OPENBASE_CLOUD,
        "options": options,
        "login": LOGINS.current(),
    }
