from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from openbase.core.sourcemapped_dataclass import SourceMappedAppDataclass
from openbase.openbase_app.managers import AppSpecificManager


@dataclass
class DjangoSerializerField:
    name: str
    serializer_class: str
    arguments: dict


@dataclass
class DjangoSerializerCreateMethod:
    body: str
    docstring: Optional[str] = None


class DjangoSerializerManager(AppSpecificManager):
    def list_for_app_path(
        self, app_path: Path, app_name: str, package_name: str
    ) -> list["DjangoSerializer"]:
        from .parsing import parse_serializers_file

        serializers_path = app_path / "serializers.py"
        if not serializers_path.exists():
            return []

        return parse_serializers_file(
            serializers_path, app_name=app_name, package_name=package_name
        )


@dataclass
class DjangoSerializer(SourceMappedAppDataclass):
    name: str
    model: Optional[str]
    fields: list[str]
    read_only_fields: list[str]
    custom_fields: list[DjangoSerializerField]
    create_method: Optional[DjangoSerializerCreateMethod]

    objects = DjangoSerializerManager()