from __future__ import annotations

from openbase.core.cli.generate_schema import generate_schema

project_description = """
# Dreamlink
Generate an app where registered users can upload dreams that they had the night before. Then they can be matched with others that had the same dream, and chat with them.  Dream in same universe counts. Use embeddings to narrow down candidates.
"""


def test_generate_schema(existing_artifacts_dir):
    """Test the generate schema command."""
    project_dir = existing_artifacts_dir / "dreamlink"
    (project_dir / ".openbase" / "DESCRIPTION.md").write_text(project_description)
    generate_schema(project_dir)
