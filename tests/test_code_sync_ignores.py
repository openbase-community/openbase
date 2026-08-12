from __future__ import annotations

from pathlib import Path

from openbase_coder_cli.code_sync import ignores
from openbase_coder_cli.sync_config import SyncFolder


def test_render_stignore_contains_vcs_and_default_patterns() -> None:
    content = ignores.render_stignore()

    assert content.startswith("// Managed by openbase-coder")
    for pattern in ("(?d).git", "(?d)**/.git", "(?d).jj", "(?d).hg"):
        assert pattern in content
    for pattern in ("(?d)node_modules", "(?d).venv", "(?d)venv"):
        assert pattern in content
    for pattern in (
        "(?d)dist",
        "(?d)build",
        "(?d)out",
        "(?d)release",
        "(?d)DerivedData",
        "(?d)__pycache__",
        "(?d).next",
        "(?d)target",
    ):
        assert pattern in content
    for pattern in (
        "(?d).DS_Store",
        "(?d)*.sqlite3",
        "(?d).pytest_cache",
        "(?d).terraform",
        "(?d).stversions",
    ):
        assert pattern in content
    # Bare-name patterns (no `**/` prefix) so the folder root is covered too.
    assert "\n**/.jj" not in content
    assert "\n**/venv" not in content
    # Secrets sync by design; the file must say so and never ignore .env.
    assert "deliberately NOT ignored" in content
    assert "\n.env\n" not in content


def test_render_stignore_ignores_dependency_lockfiles() -> None:
    content = ignores.render_stignore()
    for pattern in (
        "(?d)uv.lock",
        "(?d)package-lock.json",
        "(?d)pnpm-lock.yaml",
        "(?d)yarn.lock",
        "(?d)Cargo.lock",
        "(?d)poetry.lock",
        "(?d)Gemfile.lock",
        "(?d)Podfile.lock",
    ):
        assert pattern in content
    # Bare names so the pattern matches lockfiles at any depth, root included.
    assert "\n**/uv.lock" not in content


def test_render_stignore_appends_extra_ignores() -> None:
    content = ignores.render_stignore(("models/weights", "*.bin"))
    assert "// Folder-specific ignores" in content
    assert "models/weights" in content
    assert "*.bin" in content


def test_write_folder_ignores_creates_file(tmp_path: Path) -> None:
    folder = SyncFolder(relpath="Projects/demo", extra_ignores=("*.tmp",))
    path = ignores.write_folder_ignores(folder, home=tmp_path)

    assert path == tmp_path / "Projects" / "demo" / ".stignore"
    content = path.read_text(encoding="utf-8")
    assert "(?d).git" in content
    assert "*.tmp" in content

    # Regeneration overwrites the whole managed file.
    path.write_text("// hand edited\n", encoding="utf-8")
    ignores.write_folder_ignores(folder, home=tmp_path)
    assert "hand edited" not in path.read_text(encoding="utf-8")
