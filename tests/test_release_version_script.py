"""Version computation shared by auto-release.yml and docker-image.yml."""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "release-version.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(repo: Path, tag: str | None = None) -> None:
    _git(
        repo,
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "c",
    )
    if tag:
        _git(repo, "tag", tag)


def _version(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "v0.50.0")
    _commit(tmp_path, "v0.51.0")
    return tmp_path


def test_next_stable_bumps_highest_stable_tag(repo):
    _commit(repo, "v0.51.34.dev0")
    assert _version(repo, "next", "main") == "0.52.0"
    assert _version(repo, "next", "main", "patch") == "0.51.1"
    assert _version(repo, "next", "main", "major") == "1.0.0"


def test_next_staging_patch_bumps_past_every_tag(repo):
    _commit(repo, "v0.51.34.dev0")
    assert _version(repo, "next", "staging") == "0.51.35.dev0"


def test_stamp_at_a_tag_is_that_tag(repo):
    _commit(repo, "v0.51.34.dev0")
    assert _version(repo, "stamp", "staging") == "0.51.34.dev0"
    assert _version(repo, "stamp", "main") == "0.51.34.dev0"


def test_stamp_on_staging_is_the_release_being_cut(repo):
    # The image builds in parallel with the staging release of the same
    # commit, before that release has tagged it.
    _commit(repo, "v0.51.34.dev0")
    _commit(repo)
    _commit(repo)
    assert _version(repo, "stamp", "staging") == "0.51.35.dev0"


def test_stamp_past_a_dev_tag_is_valid_pep440(repo):
    _commit(repo, "v0.51.34.dev0")
    _commit(repo)
    _commit(repo)
    assert _version(repo, "stamp", "develop") == "0.51.34.dev2"


def test_stamp_past_a_stable_tag_is_a_post_release(repo):
    _commit(repo)
    assert _version(repo, "stamp", "main") == "0.51.0.post1"


@pytest.mark.parametrize(
    "branch,expected", [("main", "0.52.0"), ("staging", "0.51.35.dev0")]
)
def test_next_ignores_alpha_beta_rc_and_hyphenated_tags(repo, branch, expected):
    _commit(repo, "v0.51.34.dev0")
    for tag in ("v9.0.0a1", "v9.0.0b1", "v9.0.0rc1", "v9.0.0-preview"):
        _git(repo, "tag", tag)
    assert _version(repo, "next", branch) == expected
