import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/docker-image.yml"


def _step(identifier):
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return next(
        step
        for step in workflow["jobs"]["preflight"]["steps"]
        if step.get("id") == identifier
    )["run"]


def _run(tmp_path, identifier, **environment):
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _step(identifier)],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_OUTPUT": str(output), **environment},
        capture_output=True,
        text=True,
    )
    values = (
        dict(line.split("=", 1) for line in output.read_text().splitlines())
        if output.exists()
        else {}
    )
    return result, values


@pytest.mark.parametrize(
    "platforms,expected",
    [("amd64", ["linux/amd64"]), ("all", ["linux/amd64", "linux/arm64"])],
)
def test_platform_matrix(tmp_path, platforms, expected):
    result, values = _run(tmp_path, "platforms", PLATFORMS=platforms)
    assert result.returncode == 0, result.stderr
    assert [entry["platform"] for entry in json.loads(values["matrix"])] == expected


@pytest.mark.parametrize(
    "branch,expected",
    [("main", "main"), ("staging", "staging"), ("feature/test", "develop")],
)
def test_sibling_refs_and_cache_key(tmp_path, branch, expected):
    executable = tmp_path / "git"
    executable.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALLS"\nprintf "%s\\t%s\\n" "$REVISION" "$3"\n'
    )
    executable.chmod(0o755)
    calls = tmp_path / "calls"
    environment = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "CALLS": str(calls),
        "REF_NAME": branch,
    }
    result, first = _run(tmp_path, "siblings", REVISION="a" * 40, **environment)
    assert result.returncode == 0, result.stderr
    assert first["ref"] == expected
    references = [line.rsplit(" ", 1)[1] for line in calls.read_text().splitlines()]
    assert references == [
        f"refs/heads/{ref}"
        for ref in [expected, expected, "main", "main", expected, expected, expected]
    ]
    result, second = _run(tmp_path, "siblings", REVISION="b" * 40, **environment)
    assert result.returncode == 0, result.stderr
    assert second["revs"] != first["revs"]


def test_version_failure_stops_preflight(tmp_path):
    script = tmp_path / ".github/scripts/release-version.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\nexit 42\n")
    script.chmod(0o755)
    result, values = _run(tmp_path, "version", REF_NAME="staging")
    assert result.returncode == 42
    assert "version" not in values
