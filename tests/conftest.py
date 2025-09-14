from pathlib import Path

import pytest


@pytest.fixture
def artifacts_dir() -> Path:
    result = Path(__file__).parent / "artifacts"
    result.mkdir(exist_ok=True)
    return result
