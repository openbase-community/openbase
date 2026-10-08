"""The actual self-update entry point defers before flipping a busy runtime."""

import threading

import test_self_update_recovery as recovery
from test_self_update import _make_fake_package

from openbase_coder_cli import self_update
from openbase_coder_cli.services import mutation_lock as lock

installed = recovery.installed


def test_update_cannot_flip_inside_another_service_batch(
    tmp_path, monkeypatch, installed
):
    layout, old, calls = installed
    new = _make_fake_package(layout["releases"] / "2.0.0-target", version="2.0.0")
    monkeypatch.setattr(
        self_update,
        "_fetch_manifest",
        lambda _: {
            "version": "2.0.0",
            "targets": {"target": {"url": "fixture", "sha256": "fixture"}},
        },
    )
    monkeypatch.setattr(self_update, "_download_and_extract", lambda **_: new)
    monkeypatch.setattr(
        self_update, "service_mutation", lambda: lock.service_mutation(timeout=0.01)
    )
    ready, release = threading.Event(), threading.Event()

    def restart_batch():
        with lock.service_mutation():
            ready.set()
            assert release.wait(5)

    thread = threading.Thread(target=restart_batch)
    thread.start()
    try:
        assert ready.wait(5)
        result = self_update.run_self_update(report=lambda _: None)
        assert result.status == "deferred"
        assert layout["current"].resolve() == old
        assert calls == []
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert self_update.run_self_update(report=lambda _: None).status == "updated"
    assert layout["current"].resolve() == new
