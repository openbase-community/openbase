from __future__ import annotations

from openbase.core.cli.generate_backend import generate_backend


def test_generate_backend(existing_artifacts_dir):
    """Test the generate schema command."""
    project_dir = existing_artifacts_dir / "dreamlink"
    generate_backend(project_dir)
