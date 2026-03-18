"""Tests for the init CLI command."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock, patch

from openbase.core.cli.init import WORKSPACE_TEMPLATE_NAME, init


@patch("openbase.core.cli.init.ProjectScaffolder")
@patch("openbase.core.cli.init.get_github_user", return_value="gabe")
@patch("openbase.core.cli.init.boilersync_init")
@patch("openbase.core.cli.init._boilersync_supports_workspace_init", return_value=True)
@patch("openbase.core.cli.init.TemplateManager")
def test_init_uses_workspace_template_when_supported(
    mock_template_manager_cls: Mock,
    _mock_support: Mock,
    mock_boilersync_init: Mock,
    _mock_github_user: Mock,
    mock_project_scaffolder_cls: Mock,
    artifacts_dir: Path,
) -> None:
    project_dir = artifacts_dir / "dreamlink"
    project_dir.mkdir(exist_ok=True)

    mock_template_manager = Mock()
    mock_template_manager.boilerplate_dir = Path("/tmp/boilerplate")
    mock_template_manager_cls.return_value = mock_template_manager

    with patch.dict(
        os.environ,
        {"DOT_ENV_SYMLINK_SOURCE": str(Path.home() / "Developer" / ".env")},
        clear=False,
    ):
        init(project_dir, with_frontend=False, with_github=True)

    assert mock_template_manager.clone_or_pull_boilerplate_dir.called
    mock_boilersync_init.assert_called_once()
    call_kwargs = mock_boilersync_init.call_args.kwargs
    assert call_kwargs["template_name"] == WORKSPACE_TEMPLATE_NAME
    assert call_kwargs["target_dir"] == project_dir
    assert call_kwargs["options"] == {"with_frontend": False, "with_github": True}
    assert call_kwargs["template_variables"]["with_frontend"] is False
    assert call_kwargs["template_variables"]["with_github"] is True
    assert call_kwargs["template_variables"]["github_user"] == "gabe"

    mock_project_scaffolder_cls.assert_not_called()


@patch("openbase.core.cli.init.ProjectScaffolder")
@patch("openbase.core.cli.init._boilersync_supports_workspace_init", return_value=False)
@patch("openbase.core.cli.init.TemplateManager")
def test_init_falls_back_to_legacy_scaffolder_when_workspace_api_unavailable(
    mock_template_manager_cls: Mock,
    _mock_support: Mock,
    mock_project_scaffolder_cls: Mock,
    artifacts_dir: Path,
) -> None:
    project_dir = artifacts_dir / "dreamlink"
    project_dir.mkdir(exist_ok=True)

    mock_template_manager = Mock()
    mock_template_manager.boilerplate_dir = Path("/tmp/boilerplate")
    mock_template_manager_cls.return_value = mock_template_manager

    legacy_scaffolder = Mock()
    mock_project_scaffolder_cls.return_value = legacy_scaffolder

    init(project_dir, with_frontend=True, with_github=False)

    assert mock_template_manager.clone_or_pull_boilerplate_dir.called
    assert legacy_scaffolder.init_with_boilersync_and_git.called
