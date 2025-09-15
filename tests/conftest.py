from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def artifacts_dir() -> Path:
    path = Path(__file__).parent / "artifacts"
    # if path.exists():
    #     shutil.rmtree(path)
    path.mkdir(exist_ok=True)
    return path


@pytest.fixture
def existing_artifacts_dir() -> Path:
    path = Path(__file__).parent / "artifacts"
    path.mkdir(exist_ok=True)
    return path


@pytest.hookimpl(tryfirst=True)
def pytest_exception_interact(call):
    raise call.excinfo.value


@pytest.hookimpl(tryfirst=True)
def pytest_internalerror(excinfo):
    raise excinfo.value
