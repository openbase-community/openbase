"""Suite-wide isolation from the host's Openbase install.

The installed env file (~/.openbase/.env) is the single source of truth for
the tailnet provider, so without this fixture the suite's behavior would
depend on whatever transport the developer's machine is switched to (the
historical "Openbase Netmesh Serve" label flakes). Call-time readers of
``paths.DEFAULT_ENV_FILE_PATH`` see an empty per-test file instead; tests
drive provider selection through the env var or by writing this file.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_default_env_file(monkeypatch, tmp_path):
    env_path = tmp_path / "openbase-test.env"
    env_path.write_text("")
    monkeypatch.setattr("openbase_coder_cli.paths.DEFAULT_ENV_FILE_PATH", env_path)
    return env_path
