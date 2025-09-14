"""Tests for the init CLI command."""

from openbase.core.cli.init import init


def test_init_command_full_flow(artifacts_dir):
    """Test the init command with full flow - no mocking."""
    # Set up the hackathon-infra directory
    project_dir = artifacts_dir / "hackathon-infra"
    project_dir.mkdir(exist_ok=True)

    # Run the init function directly without github (to avoid needing auth)
    init(project_dir, with_frontend=True, with_github=False)

    # Verify multi.json was created
    multi_json_path = project_dir / "multi.json"
    assert multi_json_path.exists(), "multi.json was not created"
