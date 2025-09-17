from __future__ import annotations

from openbase.core.cli.generate_frontend import generate_frontend


def test_generate_frontend(existing_artifacts_dir):
    """Test the generate schema command."""
    project_dir = existing_artifacts_dir / "dreamlink"
    generate_frontend(project_dir)
