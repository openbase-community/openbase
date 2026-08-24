from openbase_coder_cli.services.definitions import SERVICES

# The bash-body behavioral assertions that used to live here now live in
# tests/test_service_runners.py, which exercises the actual runner logic
# (services/runners.py) that replaced the per-service bash command_template
# bodies. This file just approves the ServiceDefinition -> runner-key wiring.


def test_livekit_server_service_supports_tailscale_and_local_modes():
    service = next(svc for svc in SERVICES if svc.name == "livekit-server")

    assert service.command_template == "livekit-server"
    assert service.cleanup_ports == (7880, 7881)


def test_codex_app_server_service_sets_model_defaults():
    service = next(svc for svc in SERVICES if svc.name == "codex-app-server")

    assert service.command_template == "codex-app-server"


def test_livekit_agent_service_does_not_export_dispatcher_instructions_path():
    service = next(svc for svc in SERVICES if svc.name == "livekit-agent")

    assert service.command_template == "livekit-agent"
    assert service.workdir_template == "{runtime_workdir}"


def test_django_service_uses_livekit_network_mode_for_room_url():
    service = next(svc for svc in SERVICES if svc.name == "django-cli")

    assert service.command_template == "django-cli"


def test_sync_workers_service_is_auto_installed_service():
    service = next(svc for svc in SERVICES if svc.name == "sync-workers")

    assert service.workdir_template == "{data_dir}"
    assert service.install_by_default is True
    assert service.command_template == "sync-workers"


def test_thread_sync_services_are_retired():
    from openbase_coder_cli.services.definitions import (
        RETIRED_SERVICE_NAMES,
        retired_service_stub,
    )

    defined_names = {svc.name for svc in SERVICES}
    for name in (
        "codex-thread-sync",
        "claude-thread-sync",
        "codex-thread-device-sync",
        "claude-thread-device-sync",
    ):
        assert name in RETIRED_SERVICE_NAMES
        assert name not in defined_names
        stub = retired_service_stub(name)
        assert stub.name == name
        assert stub.install_by_default is False


def test_sync_workers_jobs_cover_the_retired_services_and_reconcile():
    from openbase_coder_cli.cli.sync_workers import build_jobs

    names = {job.name for job in build_jobs()}
    assert names == {
        "codex_thread_sync",
        "claude_thread_sync",
        "codex_thread_device_sync",
        "claude_thread_device_sync",
        "code_sync_reconcile",
        "cloud_registration",
        "livekit_pool_watchdog",
    }


def test_sync_workers_intervals_respect_env_overrides(monkeypatch):
    from openbase_coder_cli.cli.sync_workers import build_jobs

    monkeypatch.setenv("CODEX_THREAD_SYNC_INTERVAL", "120")
    monkeypatch.setenv("CODE_SYNC_TICK_SECONDS", "30")
    monkeypatch.setenv("CLAUDE_THREAD_SYNC_INTERVAL", "not-a-number")
    jobs = {job.name: job for job in build_jobs()}
    assert jobs["codex_thread_sync"].interval == 120.0
    assert jobs["code_sync_reconcile"].interval == 30.0
    # Bad values fall back to the default rather than crashing the service.
    assert jobs["claude_thread_sync"].interval == 60.0


def test_openbase_routines_service_is_auto_installed_service():
    service = next(svc for svc in SERVICES if svc.name == "openbase-routines")

    assert service.workdir_template == "{data_dir}"
    assert service.command_template == "openbase-routines"
