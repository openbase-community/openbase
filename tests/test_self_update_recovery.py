"""Ordinary transport/storage failures must preserve the running installation."""

import errno
import hashlib
import os
import subprocess
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from test_self_update import (
    _make_fake_package,
    _make_release_tarball,
    _patch_standalone_layout,
)

from openbase_coder_cli import self_update
from openbase_coder_cli import self_update_network as network
from openbase_coder_cli.runtime import RuntimePackage


@pytest.fixture
def server():
    replies = []
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            status, body, length = replies.pop(0)
            self.send_response(status)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.end_headers()
            if isinstance(body, tuple):
                partial, release = body
                self.wfile.write(partial)
                self.wfile.flush()
                release.wait(10)
            else:
                self.wfile.write(body)
            self.close_connection = True

        def log_message(self, *_):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}/package", replies, requests
    finally:
        http.shutdown()
        thread.join()
        http.server_close()


@pytest.fixture
def installed(tmp_path, monkeypatch):
    layout = _patch_standalone_layout(monkeypatch, tmp_path)
    old = _make_fake_package(layout["releases"] / "1.0.0-target", version="1.0.0")
    layout["current"].symlink_to(old)
    package = RuntimePackage(root=old, version="1.0.0", target="target")
    monkeypatch.setattr(self_update, "current_runtime_package", lambda: package)
    monkeypatch.setattr(self_update.InstallationConfig, "exists", lambda: False)
    monkeypatch.setattr(self_update, "OPENBASE_BASE_DIR", tmp_path)
    monkeypatch.setattr(self_update, "_voice_session_active", lambda: False)
    monkeypatch.setattr(self_update, "_refresh_backend_binaries", lambda _: None)
    calls = []
    monkeypatch.setattr(
        self_update, "_run_launcher", lambda *a, **k: calls.append(a) or True
    )
    return layout, old, calls


@pytest.mark.parametrize("content_length", [True, False])
def test_real_truncated_download_recovers_in_same_automatic_worker(
    tmp_path, monkeypatch, installed, server, content_length
):
    layout, old, calls = installed
    url, replies, requests = server
    archive, sha = _make_release_tarball(tmp_path, version="2.0.0")
    data = archive.read_bytes()
    replies.extend(
        [
            (200, data[: len(data) // 2], len(data) if content_length else None),
            (200, data, len(data)),
        ]
    )
    monkeypatch.setattr(
        self_update,
        "_fetch_manifest",
        lambda _: {
            "version": "2.0.0",
            "targets": {"target": {"url": url, "sha256": sha}},
        },
    )
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        assert layout["current"].resolve() == old
        assert list(layout["releases"].iterdir()) == [old]
        assert calls == []

    monkeypatch.setattr(self_update, "time", SimpleNamespace(sleep=sleep))
    result = self_update.run_automatic_self_update(report=lambda _: None)
    assert result.status == "updated"
    assert sleeps == [60]
    assert len(requests) == 2
    assert layout["previous"].resolve() == old
    assert len(calls) == 1


@pytest.mark.parametrize("status,retryable", [(503, True), (429, True), (404, False)])
def test_http_errors_have_actionable_retry_semantics(server, status, retryable):
    url, replies, _ = server
    replies.append((status, b"unavailable", 11))
    with pytest.raises(network.SelfUpdateError) as error:
        self_update._http_get(url)
    assert isinstance(error.value, network.RetryableUpdateError) is retryable


def test_manifest_short_response_is_retryable(server):
    url, replies, _ = server
    replies.append((200, b'{"version":', 100))
    with pytest.raises(network.RetryableUpdateError, match="Incomplete"):
        self_update._http_get(url)


@pytest.mark.parametrize(
    "error", [TimeoutError("timed out"), ConnectionResetError("reset")]
)
def test_socket_read_failure_is_retryable(monkeypatch, error):
    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _):
            raise error

    monkeypatch.setattr(network.urllib.request, "urlopen", lambda *a, **k: Response())
    with pytest.raises(network.RetryableUpdateError):
        self_update._http_get("http://fixture.test")


def test_storage_failure_preserves_current_and_cleans_partial(
    tmp_path, monkeypatch, installed
):
    layout, old, calls = installed

    def disk_full(url, path, **kwargs):
        path.write_bytes(b"partial")
        raise OSError(errno.ENOSPC, "No space left")

    monkeypatch.setattr(self_update, "download_file", disk_full)
    with pytest.raises(OSError, match="No space"):
        self_update._download_and_extract(
            url="fixture",
            sha256="sha",
            version="2",
            target="target",
            report=lambda _: None,
        )
    assert layout["current"].resolve() == old
    assert list(layout["releases"].iterdir()) == [old]
    assert calls == []


def test_waiting_old_worker_cannot_replace_newly_active_release(
    tmp_path, monkeypatch, installed
):
    layout, old, calls = installed
    new = _make_fake_package(layout["releases"] / "2.0.0-target", version="2.0.0")
    self_update._point_symlink(layout["current"], new)
    monkeypatch.setattr(
        self_update, "_fetch_manifest", lambda _: pytest.fail("stale worker fetched")
    )
    assert self_update.run_self_update(report=lambda _: None).status == "blocked"
    assert layout["current"].resolve() == new
    assert old.exists() and new.exists() and calls == []


def test_disk_write_is_not_classified_as_network_error(server):
    url, replies, _ = server
    replies.append((200, b"complete", 8))

    class FullDisk:
        def write(self, _):
            raise OSError(errno.ENOSPC, "No space left")

    with pytest.raises(OSError) as error:
        network._transfer(url, FullDisk(), timeout=1)
    assert error.value.errno == errno.ENOSPC


def test_network_backoff_observes_opt_out(tmp_path, monkeypatch, installed):
    attempts = []

    def fail(**_):
        attempts.append(True)
        raise network.RetryableUpdateError("offline")

    monkeypatch.setattr(self_update, "run_self_update", fail)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 4:
            (tmp_path / ".env").write_text("OPENBASE_CODER_AUTO_UPDATE=0\n")

    monkeypatch.setattr(self_update, "time", SimpleNamespace(sleep=sleep))
    result = self_update.run_automatic_self_update(report=lambda _: None)
    assert len(attempts) == 3  # waits of 60, 120, then cancellation inside 240
    assert sleeps == [60] * 4
    assert "disabled" in result.detail


def test_failed_rollback_services_are_not_reported_as_restored(
    tmp_path, monkeypatch, installed
):
    layout, old, _ = installed
    archive, sha = _make_release_tarball(tmp_path, version="2.0.0")
    monkeypatch.setattr(
        self_update,
        "_fetch_manifest",
        lambda _: {
            "version": "2.0.0",
            "targets": {"target": {"url": archive.as_uri(), "sha256": sha}},
        },
    )
    monkeypatch.setattr(self_update, "_run_launcher", lambda *a, **k: False)
    with pytest.raises(
        self_update.SelfUpdateError, match="restoring its services failed"
    ):
        self_update.run_self_update(report=lambda _: None)
    assert layout["current"].resolve() == old


def test_killed_download_leaves_current_intact_and_next_attempt_recovers(
    tmp_path, monkeypatch, installed, server
):
    layout, old, calls = installed
    url, replies, _ = server
    root = _make_fake_package(tmp_path / "staging", version="2.0.0")
    (root / "padding.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    archive = tmp_path / "package.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(root, arcname="package")
    body = archive.read_bytes()
    sha = hashlib.sha256(body).hexdigest()
    release = threading.Event()
    replies.extend(
        [(200, (body[: 1536 * 1024], release), len(body)), (200, body, len(body))]
    )
    script = """
import sys
from pathlib import Path
from openbase_coder_cli import self_update
self_update.STANDALONE_RELEASES_DIR = Path(sys.argv[1])
self_update._download_and_extract(url=sys.argv[2], sha256=sys.argv[3], version="2.0.0", target="target", report=lambda _: None)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(layout["releases"]), url, sha],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            partials = list(layout["releases"].glob(".download-*/package.tar.gz"))
            if partials and partials[0].stat().st_size >= 1024 * 1024:
                break
            assert child.poll() is None, (
                child.stderr.read().decode() if child.poll() is not None else ""
            )
            time.sleep(0.01)
        else:
            pytest.fail("child never reached a partial archive on disk")
        child.kill()
        child.wait(timeout=5)
        assert child.returncode < 0
        assert layout["current"].resolve() == old
        assert calls == []
        release.set()
        monkeypatch.setattr(
            self_update,
            "_fetch_manifest",
            lambda _: {
                "version": "2.0.0",
                "targets": {"target": {"url": url, "sha256": sha}},
            },
        )
        assert self_update.run_self_update(report=lambda _: None).status == "updated"
        assert not list(layout["releases"].glob(".download-*"))
        assert layout["previous"].resolve() == old
    finally:
        release.set()
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_automatic_preparation_recovers_after_disk_space_returns(
    tmp_path, monkeypatch, installed
):
    layout, old, _ = installed
    archive, sha = _make_release_tarball(tmp_path, version="2.0.0")
    monkeypatch.setattr(
        self_update,
        "_fetch_manifest",
        lambda _: {
            "version": "2.0.0",
            "targets": {"target": {"url": archive.as_uri(), "sha256": sha}},
        },
    )
    download = self_update.download_file
    attempts = []

    def fail_once(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError(errno.ENOSPC, "No space left")
        return download(*args, **kwargs)

    monkeypatch.setattr(self_update, "download_file", fail_once)

    def wait(_):
        assert layout["current"].resolve() == old

    monkeypatch.setattr(self_update, "time", SimpleNamespace(sleep=wait))
    assert (
        self_update.run_automatic_self_update(report=lambda _: None).status == "updated"
    )
    assert len(attempts) == 2


@pytest.mark.parametrize("protected", ["current", "previous"])
def test_download_never_overwrites_referenced_release(
    tmp_path, monkeypatch, installed, protected
):
    layout, old, _ = installed
    archive, sha = _make_release_tarball(tmp_path, version="2.0.0")
    target = _make_fake_package(layout["releases"] / "2.0.0-target", version="2.0.0")
    marker = target / "in-use"
    marker.write_text("preserve")
    self_update._point_symlink(layout[protected], target)
    with pytest.raises(self_update.SelfUpdateError, match="active or rollback"):
        self_update._download_and_extract(
            url=archive.as_uri(),
            sha256=sha,
            version="2.0.0",
            target="target",
            report=lambda _: None,
        )
    assert marker.read_text() == "preserve"
    assert old.exists()
