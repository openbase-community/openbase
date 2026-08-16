import subprocess

from openbase_coder_cli.runtime import RuntimePackage
from openbase_coder_cli.services import launchd, process_utils
from openbase_coder_cli.services.definitions import ServiceDefinition
from openbase_coder_cli.services.installation import InstallationConfig


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

    wrapper = launchd.generate_wrapper(
        service, config, {"python": "/usr/bin/python3"}
    )

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

    wrapper = launchd.generate_wrapper(
        service, config, {"python": "/usr/bin/python3"}
    )

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

    wrapper = launchd.generate_wrapper(
        service, config, {"python": bundled_python}
    )

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
        name="sample", description="Sample", command_template="sample",
        workdir_template="{workspace}",
    )
    monkeypatch.setattr(launchd, "_is_macos", lambda: False)
    monkeypatch.setattr(launchd, "_is_windows", lambda: True)
    calls = []
    monkeypatch.setattr(
        windows, "windows_bootstrap", lambda svc: calls.append(svc)
    )

    launchd.launchctl_bootstrap(service)

    assert calls == [service]


def test_launchctl_status_dispatches_to_windows(monkeypatch):
    from openbase_coder_cli.services import windows

    service = ServiceDefinition(
        name="sample", description="Sample", command_template="sample",
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
    monkeypatch.setattr(
        "openbase_coder_cli.paths.SYSTEMD_UNIT_DIR", systemd_dir
    )

    launchd._ensure_launchd_paths()

    assert systemd_dir.is_dir()
