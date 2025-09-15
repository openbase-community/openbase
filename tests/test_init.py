"""Tests for the init CLI command."""

from __future__ import annotations

import os
from pathlib import Path

from openbase.core.cli.generate_schema import generate_schema
from openbase.core.cli.init import init

project_description = """
# Dreamlink
Generate an app where registered users can upload dreams that they had the night before. Then they can be matched with others that had the same dream, and chat with them.  Dream in same universe counts. Use embeddings to narrow down candidates.
"""


def test_init_command_full_flow(artifacts_dir):
    """Test the init command with full flow - no mocking."""
    # Set up the hackathon-infra directory
    project_dir = artifacts_dir / "dreamlink"
    project_dir.mkdir(exist_ok=True)

    (project_dir / "DESCRIPTION.md").write_text(project_description)

    os.environ["DOT_ENV_SYMLINK_SOURCE"] = str(Path.home() / "Developer" / ".env")

    # Run the init function directly without github (to avoid needing auth)
    init(project_dir, with_frontend=True, with_github=False)

    # Verify multi.json was created
    multi_json_path = project_dir / "multi.json"
    assert multi_json_path.exists(), "multi.json was not created"


def test_generate_schema(existing_artifacts_dir):
    """Test the generate schema command."""
    project_dir = existing_artifacts_dir / "dreamlink"
    generate_schema(project_dir)
    assert (project_dir / "dreamlink" / "models.py").exists(), (
        "models.py was not created"
    )
    assert (project_dir / "dreamlink" / "urls.py").exists(), "urls.py was not created"
