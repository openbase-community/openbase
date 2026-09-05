from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")


def test_load_installed_env_makes_web_backend_url_honor_staging(tmp_path, monkeypatch):
    """Regression: the CLI must load ~/.openbase/.env so web_backend_url() honors
    the configured (e.g. staging) backend instead of falling back to prod.

    Before the fix, only the livekit-agent loaded that file, so netmesh enroll /
    tailnet-provider calls sent a staging token to the prod backend -> 403.
    """
    from openbase_coder_cli.cli import _load_installed_env
    from openbase_coder_cli.services import installation as inst
    from openbase_coder_cli.services.onboarding import web_backend_url

    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENBASE_CODER_CLI_WEB_BACKEND_URL=https://app-staging.openbase.cloud\n"
    )

    cfg = SimpleNamespace(env_file=str(env_file))
    monkeypatch.setattr(inst.InstallationConfig, "exists", staticmethod(lambda: True))
    monkeypatch.setattr(inst.InstallationConfig, "load", staticmethod(lambda: cfg))
    monkeypatch.delenv("OPENBASE_CODER_CLI_WEB_BACKEND_URL", raising=False)

    # Absent from the environment, web_backend_url falls back to prod...
    assert web_backend_url() != "https://app-staging.openbase.cloud"

    _load_installed_env()

    # ...but after the CLI loads the installed .env it honors the real target.
    assert web_backend_url() == "https://app-staging.openbase.cloud"
    assert (
        os.environ["OPENBASE_CODER_CLI_WEB_BACKEND_URL"]
        == "https://app-staging.openbase.cloud"
    )


def test_load_installed_env_does_not_override_explicit_env(tmp_path, monkeypatch):
    """An explicitly-exported backend URL must win over the on-disk .env."""
    from openbase_coder_cli.cli import _load_installed_env
    from openbase_coder_cli.services import installation as inst

    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENBASE_CODER_CLI_WEB_BACKEND_URL=https://app-staging.openbase.cloud\n"
    )

    cfg = SimpleNamespace(env_file=str(env_file))
    monkeypatch.setattr(inst.InstallationConfig, "exists", staticmethod(lambda: True))
    monkeypatch.setattr(inst.InstallationConfig, "load", staticmethod(lambda: cfg))
    monkeypatch.setenv(
        "OPENBASE_CODER_CLI_WEB_BACKEND_URL", "https://explicit.example.com"
    )

    _load_installed_env()

    assert (
        os.environ["OPENBASE_CODER_CLI_WEB_BACKEND_URL"]
        == "https://explicit.example.com"
    )
