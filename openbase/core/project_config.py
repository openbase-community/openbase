from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from boilersync.names import snake_to_kebab

if TYPE_CHECKING:
    from click import Path


@dataclass
class ProjectConfig:
    project_name_snake: str
    project_name_kebab: str
    api_package_name: str
    django_app_name: str
    marketing_description: str

    @property
    def api_package_name_snake(self) -> str:
        return snake_to_kebab(self.api_package_name)

    @classmethod
    def from_file(cls, file_path: Path) -> ProjectConfig:
        with file_path.open() as f:
            return cls(**json.load(f))

    def to_file(self, file_path: Path):
        with file_path.open("w") as f:
            json.dump(asdict(self), f, indent=4)
