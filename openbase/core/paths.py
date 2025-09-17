"""Path definitions for Openbase."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openbase.core.project_config import ProjectConfig


def get_user_config_dir() -> Path:
    """Get the Openbase home directory."""
    path = Path.home() / ".openbase"
    path.mkdir(exist_ok=True)
    return path


def get_boilerplate_dir() -> Path:
    """Get the boilerplate directory for templates."""
    path = get_user_config_dir() / "boilerplate"
    return path


def get_openbase_dir(root_dir: Path) -> Path:
    result = root_dir / ".openbase"
    result.mkdir(exist_ok=True)
    return result


def get_config_file_path(root_dir: Path) -> Path:
    return get_openbase_dir(root_dir) / "openbase.json"


class ProjectPaths:
    """Containers paths and names of a project"""

    def __init__(self, root_dir: Path, config: ProjectConfig):
        self.root_dir = root_dir
        self.config = config

    @property
    def openbase_dir(self) -> Path:
        return get_openbase_dir(self.root_dir)

    @property
    def config_file_path(self) -> Path:
        return get_config_file_path(self.root_dir)

    @property
    def description_file_path(self) -> Path:
        return self.openbase_dir / "DESCRIPTION.md"

    @property
    def basic_models_file_path(self) -> Path:
        return self.openbase_dir / "MODELS.py"

    @property
    def api_package_dir(self) -> Path:
        return self.root_dir / f"{self.config.api_package_name_snake}"

    @property
    def api_package_src_dir(self) -> Path:
        return self.api_package_dir / f"{self.config.api_package_name}"

    @property
    def api_django_app_dir(self) -> Path:
        return self.api_package_src_dir / f"{self.config.django_app_name}"

    @property
    def models_file_path(self) -> Path:
        return self.api_django_app_dir / "models.py"

    @property
    def urls_file_path(self) -> Path:
        return self.api_django_app_dir / "urls.py"

    @property
    def react_dir(self) -> Path:
        return self.root_dir / f"{self.config.project_name_kebab}-react"
