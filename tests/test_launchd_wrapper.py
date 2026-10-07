import plistlib
import subprocess

from openbase_coder_cli.runtime import RuntimePackage
from openbase_coder_cli.services import launchd, process_utils
from openbase_coder_cli.services.definitions import ServiceDefinition
from openbase_coder_cli.services.installation import InstallationConfig


def test_standalone_plist_associates_background_item_with_desktop_app(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(launchd, "LAUNCHD_WRAPPER_DIR", tmp_path / "launchd")
    monkeypatch.setattr(launchd, "PLIST_DIR", tmp_path / "plists")
    monkeypatch.setattr(launchd, "DEFAULT_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(launchd, "OPENBASE_BASE_DIR", tmp_path / "openbase")

    service = ServiceDefinition(
        name="sample",
        description="Sample",
        command_template="sample",
        workdir_template="{runtime_workdir}",
    )
    config = InstallationConfig(
        env_file=str(tmp_path / ".env"),
        standalone=True,
    )
    monkeypatch.setattr(launchd, "_installed_desktop_bundle_id", lambda: None)

    plist = launchd.generate_plist(service, config)
    payload = plistlib.loads(plist.read_bytes())

    assert payload["AssociatedBundleIdentifiers"] == ["tech.openbase.coder.desktop"]
    assert payload["ProgramArguments"] == [str(tmp_path / "launchd" / "sample.sh")]


def _sample_service() -> ServiceDefinition:
    return ServiceDefinition(
        name="sample",
        description="Sample",
        command_template="sample",
        workdir_template="{runtime_workdir}",
    )


def _patch_launchd_dirs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(launchd, "LAUNCHD_WRAPPER_DIR", tmp_path / "launchd")
    monkeypatch.setattr(launchd, "PLIST_DIR", tmp_path / "plists")
    monkeypatch.setattr(launchd, "DEFAULT_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(launchd, "OPENBASE_BASE_DIR", tmp_path / "openbase")


def test_plist_associates_with_installed_desktop_app_outside_standalone(
    tmp_path, monkeypatch
):
    _patch_launchd_dirs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        launchd, "_installed_desktop_bundle_id", lambda: "cloud.openbase.coder.dev"
    )
    config = InstallationConfig(
        workspace_path=str(tmp_path), env_file=str(tmp_path / ".env")
    )

    payload = plistlib.loads(
        launchd.generate_plist(_sample_service(), config).read_bytes()
    )

    # A developer install with the desktop app present is attributed to that
    # app (its real bundle id, which differs for dev builds), so background
    # items are not announced as anonymous shell scripts.
    assert payload["AssociatedBundleIdentifiers"] == ["cloud.openbase.coder.dev"]


def test_plist_has_no_association_without_desktop_app(tmp_path, monkeypatch):
    _patch_launchd_dirs(tmp_path, monkeypatch)
    monkeypatch.setattr(launchd, "_installed_desktop_bundle_id", lambda: None)
    config = InstallationConfig(
        workspace_path=str(tmp_path), env_file=str(tmp_path / ".env")
    )

    payload = plistlib.loads(
        launchd.generate_plist(_sample_service(), config).read_bytes()
    )

    assert "AssociatedBundleIdentifiers" not in payload


def test_installed_desktop_bundle_id_reads_info_plist(tmp_path, monkeypatch):
    app = tmp_path / "Applications" / "Openbase.app" / "Contents"
    app.mkdir(parents=True)
    (app / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "cloud.openbase.coder.dev-dashboard"})
    )
    monkeypatch.setattr(launchd.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(launchd, "_SYSTEM_APPLICATIONS_DIR", tmp_path / "nowhere")

    assert (
        launchd._installed_desktop_bundle_id() == "cloud.openbase.coder.dev-dashboard"
    )


def test_write_if_changed_leaves_identical_file_untouched(tmp_path):
    path = tmp_path / "nested" / "sample.plist"

    assert launchd._write_if_changed(path, "content\n", mode=0o755) is True
    stat_before = path.stat()

    assert launchd._write_if_changed(path, "content\n", mode=0o755) is False
    stat_after = path.stat()
    assert stat_after.st_mtime_ns == stat_before.st_mtime_ns
    assert stat_after.st_mode & 0o777 == 0o755

    assert launchd._write_if_changed(path, "other\n") is True
    assert path.read_text() == "other\n"


def test_write_service_files_reports_reload_only_for_plist_changes(
    tmp_path, monkeypatch
):
    _patch_launchd_dirs(tmp_path, monkeypatch)
    monkeypatch.setattr(launchd, "_is_macos", lambda: True)
    monkeypatch.setattr(launchd, "_installed_desktop_bundle_id", lambda: None)
    service = ServiceDefinition(
        name="sample",
        description="Sample",
        command_template="sample",
        workdir_template="{workspace}",
    )
    config = InstallationConfig(
        workspace_path=str(tmp_path), env_file=str(tmp_path / ".env")
    )
    binaries = {"python": "/usr/bin/python3"}

    # First write creates both files: the plist is new, so a bootstrap is due.
    assert launchd._write_service_files(service, config, binaries) is True
    # Nothing changed: an in-place restart is enough.
    assert launchd._write_service_files(service, config, binaries) is False
    # A wrapper-only change (new python) is picked up by the next exec of the
    # wrapper; launchd need not re-read the plist.
    assert launchd._write_service_files(service, config, {"python": "/opt/py"}) is False
    # A plist change (different workdir) requires re-registering the job.
    config_moved = InstallationConfig(
        workspace_path=str(tmp_path / "elsewhere"), env_file=str(tmp_path / ".env")
    )
    assert launchd._write_service_files(service, config_moved, binaries) is True


def _restart_harness(monkeypatch, pids):
    """Fake launchctl where ``pids`` is the sequence of pids ``print`` reports."""
    calls = []
    pid_iter = iter(pids)
    current = {"pid": None}

    def fake_launchctl(*args, check=True):
        calls.append(args)
        if args[0] == "print":
            current["pid"] = next(pid_iter, current["pid"])
            pid = current["pid"]
            stdout = f"\tpid = {pid}\n" if pid else ""
            return subprocess.CompletedProcess(["launchctl", *args], 0, stdout, "")
        return subprocess.CompletedProcess(["launchctl", *args], 0, "", "")

    monkeypatch.setattr(launchd, "_is_macos", lambda: True)
    monkeypatch.setattr(launchd, "_uid", lambda: 501)
    monkeypatch.setattr(launchd, "_launchctl", fake_launchctl)
    monkeypatch.setattr(launchd, "_truncate_existing_logs", lambda _svc: None)
    monkeypatch.setattr(
        launchd, "_cleanup_lingering_processes", lambda _svc, keep=frozenset(): None
    )
    monkeypatch.setattr(process_utils, "process_tree_pids", lambda pid: {pid})
    monkeypatch.setattr(process_utils.time, "sleep", lambda _s: None)
    return calls


def test_launchctl_restart_terminates_and_kickstarts_without_reregistering(
    monkeypatch,
):
    # print: loaded check (111), old pid read (111), poll (111), poll (222),
    # new pid read (222).
    calls = _restart_harness(monkeypatch, [111, 111, 111, 222, 222])

    assert launchd.launchctl_restart(_sample_service()) is True

    target = "gui/501/com.openbase.coder.sample"
    actions = [c for c in calls if c[0] != "print"]
    assert actions == [("kill", "SIGTERM", target), ("kickstart", target)]
    assert not any(c[0] in {"bootout", "bootstrap"} for c in calls)


def test_launchctl_restart_forces_kickstart_when_sigterm_is_ignored(monkeypatch):
    monkeypatch.setattr(launchd, "RESTART_EXIT_TIMEOUT_SECONDS", 0.0)
    calls = _restart_harness(monkeypatch, [111])

    assert launchd.launchctl_restart(_sample_service()) is True

    target = "gui/501/com.openbase.coder.sample"
    actions = [c for c in calls if c[0] != "print"]
    assert actions == [
        ("kill", "SIGTERM", target),
        ("kickstart", "-k", target),
        ("kickstart", target),
    ]


def test_launchctl_restart_returns_false_when_job_not_loaded(monkeypatch):
    calls = []

    def fake_launchctl(*args, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(["launchctl", *args], 113, "", "not found")

    monkeypatch.setattr(launchd, "_is_macos", lambda: True)
    monkeypatch.setattr(launchd, "_uid", lambda: 501)
    monkeypatch.setattr(launchd, "_launchctl", fake_launchctl)

    assert launchd.launchctl_restart(_sample_service()) is False
    assert [c[0] for c in calls] == ["print"]


def test_install_service_restarts_in_place_when_plist_unchanged(monkeypatch):
    events = []
    monkeypatch.setattr(launchd, "_ensure_launchd_paths", lambda: None)
    monkeypatch.setattr(launchd, "_resolve_binaries", lambda _c, _s: {})
    monkeypatch.setattr(launchd, "_write_service_files", lambda *_a: False)
    monkeypatch.setattr(
        launchd, "launchctl_restart", lambda svc: events.append("restart") or True
    )
    monkeypatch.setattr(
        launchd, "launchctl_bootstrap", lambda svc: events.append("bootstrap")
    )

    launchd.install_service(InstallationConfig(env_file=".env"), _sample_service())

    assert events == ["restart"]


def test_install_service_bootstraps_when_plist_changed_or_job_unloaded(monkeypatch):
    events = []
    monkeypatch.setattr(launchd, "_ensure_launchd_paths", lambda: None)
    monkeypatch.setattr(launchd, "_resolve_binaries", lambda _c, _s: {})
    monkeypatch.setattr(
        launchd, "launchctl_bootstrap", lambda svc: events.append("bootstrap")
    )

    monkeypatch.setattr(launchd, "_write_service_files", lambda *_a: True)
    monkeypatch.setattr(
        launchd, "launchctl_restart", lambda svc: events.append("restart") or True
    )
    launchd.install_service(InstallationConfig(env_file=".env"), _sample_service())
    assert events == ["bootstrap"]

    events.clear()
    monkeypatch.setattr(launchd, "_write_service_files", lambda *_a: False)
    monkeypatch.setattr(launchd, "launchctl_restart", lambda svc: False)
    launchd.install_service(InstallationConfig(env_file=".env"), _sample_service())
    assert events == ["bootstrap"]


def test_generate_wrapper_includes_user_bin_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "LAUNCHD_WRAPPER_DIR", tmp_path / "launchd")
    monkeypatch.setattr(launchd, "OPENBASE_BASE_DIR", tmp_path / "openbase")

    service = ServiceDefinition(
        name="sample",
        description="Sample",
        command_template="sample",
        workdir_template="{workspace}",
    )
    config = InstallationConfig(
        workspace_path=str(tmp_path / "workspace"),
        env_file=str(tmp_path / ".env"),
    )

    wrapper = launchd.generate_wrapper(service, config, {"python": "/usr/bin/python3"})

    assert (
        'export PATH="$HOME/.openbase/bin:$HOME/.local/bin:$HOME/bin:'
        '/opt/homebrew/bin:/usr/local/bin:$PATH"' in wrapper.read_text()
    )


def test_generate_wrapper_execs_runner_module_with_service_name(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "LAUNCHD_WRAPPER_DIR", tmp_path / "launchd")
    monkeypatch.setattr(launchd, "OPENBASE_BASE_DIR", tmp_path / "openbase")

    service = ServiceDefinition(
        name="sample",
        description="Sample",
        command_template="sample-runner-key",
        workdir_template="{workspace}",
    )
    config = InstallationConfig(
        workspace_path=str(tmp_path / "workspace"),
        env_file=str(tmp_path / ".env"),
    )

    wrapper = launchd.generate_wrapper(service, config, {"python": "/usr/bin/python3"})

    content = wrapper.read_text()
    assert (
        "exec /usr/bin/python3 -m openbase_coder_cli.services.runners "
        "sample-runner-key" in content
    )


def test_resolve_binaries_prefers_standalone_paths(tmp_path, monkeypatch):
    package_dir = tmp_path / "package"
    bin_dir = package_dir / "bin"
    bin_dir.mkdir(parents=True)
    openbase_coder = bin_dir / "openbase-coder"
    livekit = bin_dir / "livekit-server"
    python = package_dir / "python" / "bin" / "python"
    python.parent.mkdir(parents=True)
    for path in (openbase_coder, livekit, python):
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)

    monkeypatch.setattr(launchd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        launchd, "stable_runtime_package", lambda: RuntimePackage(root=package_dir)
    )
    monkeypatch.setattr(launchd, "OPENBASE_BASE_DIR", tmp_path / "openbase")

    config = InstallationConfig(
        workspace_path="",
        env_file=str(tmp_path / ".env"),
        standalone=True,
    )

    # ``_resolve_binaries`` (used at wrapper-generation time) now only ever
    # needs "python" plus whatever workdir_template references — the other
    # per-service binaries (openbase_coder, livekit, ...) are resolved later,
    # inside services/runners.py at actual runtime. The underlying preferred
    # -path resolution logic is unchanged, so exercise it directly too.
    binaries = launchd._resolve_binaries(config)
    assert binaries["python"] == str(python)
    assert binaries["runtime_workdir"] == str(package_dir)

    resolvers = launchd._binary_resolvers(config)
    assert resolvers["openbase_coder"]() == str(openbase_coder)
    assert resolvers["livekit"]() == str(livekit)


def test_dev_livekit_resolver_skips_stale_download(tmp_path, monkeypatch):
    stale = tmp_path / "openbase" / "bin" / "livekit-server"
    stale.parent.mkdir(parents=True)
    stale.write_text("#!/bin/sh\n", encoding="utf-8")
    stale.chmod(0o755)
    fallback = tmp_path / "homebrew" / "livekit-server"
    fallback.parent.mkdir()
    fallback.write_text("#!/bin/sh\n", encoding="utf-8")
    fallback.chmod(0o755)

    monkeypatch.setattr(launchd, "stable_runtime_package", lambda: None)
    monkeypatch.setattr(
        "openbase_coder_cli.livekit_install.installed_livekit_server_path",
        lambda: stale,
    )
    monkeypatch.setattr(
        "openbase_coder_cli.livekit_install.livekit_binary_matches_pin",
        lambda _binary: False,
    )
    monkeypatch.setattr(
        "openbase_coder_cli.livekit_install.fallback_livekit_server_path",
        lambda: fallback,
    )

    config = InstallationConfig(
        workspace_path="",
        env_file=str(tmp_path / ".env"),
        standalone=False,
    )

    assert launchd._binary_resolvers(config)["livekit"]() == str(fallback)


def test_tunneld_tracks_current_package_despite_old_installed_copy(
    tmp_path, monkeypatch
):
    current = tmp_path / "current"
    old = tmp_path / "old" / "bin" / "openbase-tunneld"
    new = tmp_path / "new" / "bin" / "openbase-tunneld"
    installed = tmp_path / "user-bin" / "openbase-tunneld"
    for binary, content in ((old, "old"), (new, "new"), (installed, "old")):
        binary.parent.mkdir(parents=True)
        binary.write_text(content)
        binary.chmod(0o755)
    current.symlink_to(old.parent.parent)
    monkeypatch.delenv("OPENBASE_TUNNELD_BIN", raising=False)
    monkeypatch.setattr(launchd, "OPENBASE_BIN_DIR", installed.parent)
    monkeypatch.setattr(
        launchd, "stable_runtime_package", lambda: RuntimePackage(root=current)
    )
    config = InstallationConfig(standalone=True)
    resolver = launchd._binary_resolvers(config)["tunneld"]
    assert resolver() == str(current / "bin" / "openbase-tunneld")
    current.unlink()
    current.symlink_to(new.parent.parent)
    assert resolver() == str(current / "bin" / "openbase-tunneld")
    assert (current / "bin" / "openbase-tunneld").read_text() == "new"
    assert installed.read_text() == "old"


def test_install_refreshes_enabled_optional_daemons_not_disabled_or_oneshots(
    monkeypatch,
):
    enabled = {"openbase-tunneld", "sync-daemon", "openbase-cloud-auth-rehydrate"}
    installed = []
    monkeypatch.setattr(launchd, "_ensure_launchd_paths", lambda: None)
    monkeypatch.setattr(launchd, "_selected_backend", lambda _config: "openbase-cloud")
    monkeypatch.setattr(
        launchd, "launchctl_status", lambda svc: {"installed": svc.name in enabled}
    )
    monkeypatch.setattr(launchd, "_resolve_binaries", lambda _config, _services: {})
    monkeypatch.setattr(launchd, "remove_service", lambda _svc: False)
    monkeypatch.setattr(launchd, "_write_service_files", lambda *_args: False)
    monkeypatch.setattr(
        launchd,
        "_activate_service",
        lambda svc, _reload: installed.append(svc.name) or "Restarted",
    )
    launchd.install_all_services(InstallationConfig(standalone=True))
    assert {"django-cli", "livekit-server", "openbase-tunneld", "sync-daemon"} <= set(
        installed
    )
    assert "openbase-cloud-auth-rehydrate" not in installed
    assert "openbase-cloud-heartbeat" not in installed
    assert "codex-app-server" not in installed


def test_launchctl_bootstrap_reenables_disabled_label(tmp_path, monkeypatch):
    service = ServiceDefinition(
        name="sample",
        description="Sample",
        command_template="exec true",
        workdir_template="{workspace}",
    )
    plist = tmp_path / "sample.plist"
    calls = []

    def fake_launchctl(*args, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(["launchctl", *args], 0, "", "")

    monkeypatch.setattr(launchd, "_is_macos", lambda: True)
    monkeypatch.setattr(launchd, "_uid", lambda: 501)
    monkeypatch.setattr(launchd, "_plist_path", lambda _svc: plist)
    monkeypatch.setattr(launchd, "_prepare_service_start", lambda _svc: None)
    monkeypatch.setattr(launchd.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(launchd, "_launchctl", fake_launchctl)

    launchd.launchctl_bootstrap(service)

    assert ("enable", "gui/501/com.openbase.coder.sample") in calls
    assert calls.index(("enable", "gui/501/com.openbase.coder.sample")) < calls.index(
        ("bootstrap", "gui/501", str(plist))
    )


def test_launchctl_bootstrap_kickstarts_once_after_successful_bootstrap(
    tmp_path, monkeypatch
):
    service = ServiceDefinition(
        name="sample",
        description="Sample",
        command_template="exec true",
        workdir_template="{workspace}",
    )
    plist = tmp_path / "sample.plist"
    calls = []
    bootstrap_returncodes = iter([5, 5, 0])

    def fake_launchctl(*args, check=True):
        calls.append(args)
        code = next(bootstrap_returncodes) if args[0] == "bootstrap" else 0
        return subprocess.CompletedProcess(["launchctl", *args], code, "", "")

    monkeypatch.setattr(launchd, "_is_macos", lambda: True)
    monkeypatch.setattr(launchd, "_uid", lambda: 501)
    monkeypatch.setattr(launchd, "_plist_path", lambda _svc: plist)
    monkeypatch.setattr(launchd, "_prepare_service_start", lambda _svc: None)
    monkeypatch.setattr(launchd.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(launchd, "_launchctl", fake_launchctl)

    launchd.launchctl_bootstrap(service)

    target = "gui/501/com.openbase.coder.sample"
    bootstrap_indexes = [i for i, c in enumerate(calls) if c[0] == "bootstrap"]
    kickstart_calls = [c for c in calls if c[0] == "kickstart"]

    # Exactly one kickstart, for the same service target, without -k.
    assert kickstart_calls == [("kickstart", target)]
    # It immediately follows the successful (final) bootstrap, so the two
    # failed attempts before it ran without a kickstart.
    assert len(bootstrap_indexes) == 3
    assert calls.index(("kickstart", target)) == bootstrap_indexes[-1] + 1


def test_generate_wrapper_quotes_python_binary_path_with_spaces(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "LAUNCHD_WRAPPER_DIR", tmp_path / "launchd")
    monkeypatch.setattr(launchd, "OPENBASE_BASE_DIR", tmp_path / "openbase")

    service = ServiceDefinition(
        name="sample",
        description="Sample",
        command_template="sample",
        workdir_template="{workspace}",
    )
    config = InstallationConfig(
        workspace_path=str(tmp_path / "workspace"),
        env_file=str(tmp_path / ".env"),
    )
    bundled_python = (
        "/Applications/Openbase Coder.app/Contents/Resources/"
        "OpenbaseCoderCLI/python/bin/python"
    )

    wrapper = launchd.generate_wrapper(service, config, {"python": bundled_python})

    content = wrapper.read_text()
    assert (
        f"exec '{bundled_python}' -m openbase_coder_cli.services.runners sample"
        in content
    )
    assert f"exec {bundled_python} -m" not in content


def test_cleanup_lingering_processes_terminates_then_forces_via_process_utils(
    monkeypatch,
):
    service = ServiceDefinition(
        name="sample",
        description="Sample",
        command_template="exec true",
        workdir_template="{workspace}",
        cleanup_ports=(7880,),
    )
    candidate_calls = iter([{111}, {111}])
    monkeypatch.setattr(
        launchd, "_cleanup_candidate_pids", lambda _svc: next(candidate_calls)
    )
    monkeypatch.setattr(launchd.time, "sleep", lambda _seconds: None)
    terminate_calls = []
    monkeypatch.setattr(
        process_utils,
        "terminate",
        lambda pid, *, force=False: terminate_calls.append((pid, force)),
    )

    launchd._cleanup_lingering_processes(service)

    # First pass is graceful (no force), second pass on the still-lingering
    # PID is forceful — this never references ``signal.SIGKILL`` directly at
    # the launchd.py call site, so it works identically on Windows.
    assert terminate_calls == [(111, False), (111, True)]


def test_cleanup_lingering_processes_skips_force_pass_when_nothing_lingers(
    monkeypatch,
):
    service = ServiceDefinition(
        name="sample",
        description="Sample",
        command_template="exec true",
        workdir_template="{workspace}",
        cleanup_ports=(7880,),
    )
    monkeypatch.setattr(launchd, "_cleanup_candidate_pids", lambda _svc: set())
    terminate_calls = []
    monkeypatch.setattr(
        process_utils,
        "terminate",
        lambda pid, *, force=False: terminate_calls.append((pid, force)),
    )

    launchd._cleanup_lingering_processes(service)

    assert terminate_calls == []


def test_ensure_launchd_paths_creates_task_scheduler_dir_on_windows(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(launchd, "DEFAULT_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(launchd, "LAUNCHD_WRAPPER_DIR", tmp_path / "launchd")
    monkeypatch.setattr(launchd, "_is_macos", lambda: False)
    monkeypatch.setattr(launchd.sys, "platform", "win32")
    task_scheduler_dir = tmp_path / "tasks"
    monkeypatch.setattr(launchd, "TASK_SCHEDULER_DIR", task_scheduler_dir)

    launchd._ensure_launchd_paths()

    assert task_scheduler_dir.is_dir()


def test_write_service_files_generates_task_xml_on_windows(tmp_path, monkeypatch):
    from openbase_coder_cli.services import windows

    service = ServiceDefinition(
        name="sample",
        description="Sample",
        command_template="sample",
        workdir_template="{workspace}",
    )
    config = InstallationConfig(
        workspace_path=str(tmp_path / "workspace"),
        env_file=str(tmp_path / ".env"),
    )
    monkeypatch.setattr(launchd, "_is_macos", lambda: False)
    monkeypatch.setattr(launchd, "_is_windows", lambda: True)
    calls = []
    monkeypatch.setattr(
        windows,
        "generate_task_xml",
        lambda svc, cfg, python_bin: calls.append((svc, cfg, python_bin)),
    )

    launchd._write_service_files(service, config, {"python": "/usr/bin/python3"})

    assert calls == [(service, config, "/usr/bin/python3")]


def test_launchctl_bootstrap_dispatches_to_windows(monkeypatch):
    from openbase_coder_cli.services import windows

    service = ServiceDefinition(
        name="sample",
        description="Sample",
        command_template="sample",
        workdir_template="{workspace}",
    )
    monkeypatch.setattr(launchd, "_is_macos", lambda: False)
    monkeypatch.setattr(launchd, "_is_windows", lambda: True)
    calls = []
    monkeypatch.setattr(windows, "windows_bootstrap", lambda svc: calls.append(svc))

    launchd.launchctl_bootstrap(service)

    assert calls == [service]


def test_launchctl_status_dispatches_to_windows(monkeypatch):
    from openbase_coder_cli.services import windows

    service = ServiceDefinition(
        name="sample",
        description="Sample",
        command_template="sample",
        workdir_template="{workspace}",
    )
    monkeypatch.setattr(launchd, "_is_macos", lambda: False)
    monkeypatch.setattr(launchd, "_is_windows", lambda: True)
    monkeypatch.setattr(
        windows, "windows_status", lambda svc: {"installed": True, "pid": "1"}
    )

    assert launchd.launchctl_status(service) == {"installed": True, "pid": "1"}


def test_ensure_launchd_paths_creates_systemd_dir_on_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "DEFAULT_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(launchd, "LAUNCHD_WRAPPER_DIR", tmp_path / "launchd")
    monkeypatch.setattr(launchd, "_is_macos", lambda: False)
    monkeypatch.setattr(launchd.sys, "platform", "linux")
    systemd_dir = tmp_path / "systemd"
    monkeypatch.setattr("openbase_coder_cli.paths.SYSTEMD_UNIT_DIR", systemd_dir)

    launchd._ensure_launchd_paths()

    assert systemd_dir.is_dir()
