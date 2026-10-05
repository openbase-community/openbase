from __future__ import annotations

import json

import pytest

from openbase_coder_cli.thread_model_overrides import (
    MODEL_OVERRIDES_FILE,
    get_thread_model_override,
    set_thread_model_override,
)


@pytest.fixture(autouse=True)
def _data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    return tmp_path


def test_override_round_trips_and_persists(tmp_path) -> None:
    assert get_thread_model_override("thread-1") is None

    assert set_thread_model_override("thread-1", "  opus ") == "opus"
    assert get_thread_model_override("thread-1") == "opus"

    stored = json.loads((tmp_path / MODEL_OVERRIDES_FILE).read_text())
    assert stored == {"threads": {"thread-1": "opus"}}


def test_clearing_override_removes_entry(tmp_path) -> None:
    set_thread_model_override("thread-1", "opus")
    set_thread_model_override("thread-2", "gpt-5.5")

    assert set_thread_model_override("thread-1", None) is None
    assert get_thread_model_override("thread-1") is None
    assert get_thread_model_override("thread-2") == "gpt-5.5"

    set_thread_model_override("thread-2", "   ")
    stored = json.loads((tmp_path / MODEL_OVERRIDES_FILE).read_text())
    assert stored == {"threads": {}}


def test_corrupt_or_malformed_file_reads_as_empty(tmp_path) -> None:
    path = tmp_path / MODEL_OVERRIDES_FILE
    path.write_text("{", encoding="utf-8")
    assert get_thread_model_override("thread-1") is None

    path.write_text(json.dumps({"threads": ["not", "a", "dict"]}), encoding="utf-8")
    assert get_thread_model_override("thread-1") is None

    path.write_text(json.dumps({"threads": {"thread-1": 7, "": "opus"}}))
    assert get_thread_model_override("thread-1") is None

    # A write after a corrupt read replaces the file with a clean one.
    set_thread_model_override("thread-1", "sonnet")
    assert json.loads(path.read_text()) == {"threads": {"thread-1": "sonnet"}}


def test_blank_thread_id_rejected() -> None:
    assert get_thread_model_override(None) is None
    assert get_thread_model_override("   ") is None
    with pytest.raises(ValueError):
        set_thread_model_override("  ", "opus")
