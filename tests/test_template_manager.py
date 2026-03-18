from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

from openbase.core.project_config import ProjectConfig
from openbase.core.template_manager import TemplateManager


def _make_template_manager(root_dir: Path) -> TemplateManager:
    config = ProjectConfig(
        project_name_snake="dreamlink",
        project_name_kebab="dreamlink",
        api_package_name="dreamlink_api",
        django_app_name="dreamlink",
        marketing_description="Built with Openbase",
        api_prefix="api/dreamlink",
    )
    paths = Mock()
    paths.root_dir = root_dir
    return TemplateManager(paths=paths, config=config)


@patch("openbase.core.template_manager.boilersync_paths.add_child_to_parent")
@patch("openbase.core.template_manager.boilersync_pull")
def test_init_boilersync_template_ignores_parent_boilersync_directories(
    mock_boilersync_pull: Mock,
    mock_add_child_to_parent: Mock,
    artifacts_dir: Path,
) -> None:
    root_dir = artifacts_dir / "workspace"
    target_dir = root_dir / "dreamlink-api"
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    # Simulate the user's ~/.boilersync directory shape without creating metadata files.
    invalid_parent_boilersync_dir = artifacts_dir / ".boilersync"
    invalid_parent_boilersync_dir.mkdir()

    manager = _make_template_manager(root_dir)
    assert manager._find_parent_boilersync_file(target_dir) is None
    manager._init_boilersync_template(
        template_name="app-package",
        target_dir=target_dir,
        collected_variables={"name_snake": "dreamlink_api"},
    )
    mock_boilersync_pull.assert_called_once()
    mock_add_child_to_parent.assert_not_called()


@patch("openbase.core.template_manager.boilersync_paths.add_child_to_parent")
@patch("openbase.core.template_manager.boilersync_pull")
def test_init_boilersync_template_registers_real_parent_projects(
    mock_boilersync_pull: Mock,
    mock_add_child_to_parent: Mock,
    artifacts_dir: Path,
) -> None:
    parent_dir = artifacts_dir / "workspace"
    parent_dir.mkdir()
    (parent_dir / ".boilersync").write_text("{}", encoding="utf-8")
    target_dir = parent_dir / "dreamlink-api"

    manager = _make_template_manager(parent_dir)
    manager._init_boilersync_template(
        template_name="app-package",
        target_dir=target_dir,
        collected_variables={"name_snake": "dreamlink_api"},
    )

    mock_boilersync_pull.assert_called_once()
    mock_add_child_to_parent.assert_called_once_with(
        target_dir,
        parent_dir / ".boilersync",
    )
