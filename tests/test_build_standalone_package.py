from __future__ import annotations

import importlib.util
import py_compile
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "build_standalone_package.py"
SPEC = importlib.util.spec_from_file_location("build_standalone_package", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
build_standalone_package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_standalone_package)


def test_prune_rebuildable_bytecode_preserves_pyc_only_modules(tmp_path: Path) -> None:
    python_dir = tmp_path / "python"
    package_dir = python_dir / "lib" / "python3.12" / "site-packages" / "example"
    package_dir.mkdir(parents=True)

    source = package_dir / "source_backed.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    source_bytecode = Path(py_compile.compile(str(source), doraise=True))

    pyc_only_source = package_dir / "pyc_only.py"
    pyc_only_source.write_text("VALUE = 2\n", encoding="utf-8")
    pyc_only_bytecode = Path(py_compile.compile(str(pyc_only_source), doraise=True))
    pyc_only_source.unlink()

    unrelated = source_bytecode.parent / "keep.txt"
    unrelated.write_text("not bytecode\n", encoding="utf-8")
    expected_freed = source_bytecode.stat().st_size

    count, freed_bytes = build_standalone_package.prune_rebuildable_bytecode(python_dir)

    assert count == 1
    assert freed_bytes == expected_freed
    assert not source_bytecode.exists()
    assert pyc_only_bytecode.exists()
    assert unrelated.exists()


def test_prune_rebuildable_bytecode_removes_empty_cache_directory(
    tmp_path: Path,
) -> None:
    python_dir = tmp_path / "python"
    source = python_dir / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    bytecode = Path(py_compile.compile(str(source), doraise=True))
    cache_dir = bytecode.parent

    build_standalone_package.prune_rebuildable_bytecode(python_dir)

    assert not cache_dir.exists()


def test_validate_no_rebuildable_bytecode_rejects_source_backed_cache(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    source = package_dir / "python" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    bytecode = Path(py_compile.compile(str(source), doraise=True))

    with pytest.raises(RuntimeError) as exc_info:
        build_standalone_package._validate_no_rebuildable_bytecode(package_dir)

    assert str(bytecode) in str(exc_info.value)
