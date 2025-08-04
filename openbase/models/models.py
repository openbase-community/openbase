from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from openbase.core.sourcemapped_dataclass import SourceMappedAppDataclass
from openbase.openbase_app.managers import AppSpecificManager


@dataclass
class DjangoModelField:
    name: str
    type: str
    kwargs: dict
    choices: Optional[list[tuple[str, str]]] = None


@dataclass
class DjangoModelMethod:
    name: str
    body: str
    docstring: str
    args: dict


@dataclass
class DjangoModelProperty:
    name: str
    body: str
    docstring: str


@dataclass
class DjangoModelSpecialMethod:
    body: str
    docstring: str = ""


class DjangoModelManager(AppSpecificManager):
    def list_for_app_path(
        self, app_path: Path, app_name: str, package_name: str
    ) -> list["DjangoModel"]:
        from .parsing import parse_models_file

        models_path = app_path / "models.py"
        if not models_path.exists():
            return []

        return parse_models_file(
            models_path, app_name=app_name, package_name=package_name
        )


@dataclass
class DjangoModel(SourceMappedAppDataclass):
    name: str
    docstring: Optional[str]
    fields: list[DjangoModelField]
    methods: list[DjangoModelMethod]
    properties: list[DjangoModelProperty]
    meta: dict
    save_method: Optional[DjangoModelSpecialMethod]
    str_method: Optional[DjangoModelSpecialMethod]

    objects = DjangoModelManager()
