from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from openbase_coder_cli import codex_home_instructions


def test_concurrent_instruction_writes_use_distinct_temporary_files(tmp_path, monkeypatch):
    tmp_path = tmp_path / "writes"
    tmp_path.mkdir()
    target = tmp_path / "instructions.md"
    target.write_text("previous instructions", encoding="utf-8")
    barrier = Barrier(2)
    replace = codex_home_instructions.os.replace

    def concurrent_replace(source, destination):
        barrier.wait(timeout=5)
        replace(source, destination)

    monkeypatch.setattr(codex_home_instructions.os, "replace", concurrent_replace)
    contents = ["first instructions" * 1000, "second instructions" * 1000]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(codex_home_instructions._write_atomically, target, content)
            for content in contents
        ]
        for future in futures:
            future.result(timeout=10)

    assert target.read_text(encoding="utf-8") in contents
    assert list(tmp_path.iterdir()) == [target]


def test_failed_instruction_replace_preserves_target_and_cleans_up(tmp_path, monkeypatch):
    tmp_path = tmp_path / "writes"
    tmp_path.mkdir()
    target = tmp_path / "instructions.md"
    target.write_text("previous instructions", encoding="utf-8")

    def fail_replace(source, destination):
        raise PermissionError("replacement denied")

    monkeypatch.setattr(codex_home_instructions.os, "replace", fail_replace)
    with pytest.raises(PermissionError):
        codex_home_instructions._write_atomically(target, "new instructions")

    assert target.read_text(encoding="utf-8") == "previous instructions"
    assert list(tmp_path.iterdir()) == [target]


def test_windows_read_only_instruction_file_can_be_refreshed(tmp_path, monkeypatch):
    tmp_path = tmp_path / "writes"
    tmp_path.mkdir()
    source = tmp_path / "source.md"
    source.write_text("new instructions", encoding="utf-8")
    target = tmp_path / "instructions.md"
    target.write_text("previous instructions", encoding="utf-8")
    target.chmod(0o444)
    replace = codex_home_instructions.os.replace

    def windows_replace(source, destination):
        if Path(destination).stat().st_mode & 0o200 == 0:
            raise PermissionError("Windows rejects replacing a read-only destination")
        replace(source, destination)

    monkeypatch.setattr(
        codex_home_instructions,
        "os",
        SimpleNamespace(name="nt", replace=windows_replace),
    )
    monkeypatch.setattr(codex_home_instructions, "is_standalone_runtime", lambda: True)

    assert codex_home_instructions.ensure_rendered_instruction_file(
        source, target, document_label="instructions"
    )
    assert "new instructions" in target.read_text(encoding="utf-8")
    assert target.stat().st_mode & 0o222 == 0
    assert sorted(path.name for path in tmp_path.iterdir()) == ["instructions.md", "source.md"]
