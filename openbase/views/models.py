from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from openbase.core.sourcemapped_dataclass import SourceMappedAppDataclass
from openbase.openbase_app.managers import AppSpecificManager


@dataclass
class ViewSetMethod:
    name: str
    docstring: Optional[str]
    body: str


@dataclass
class ViewSetAction:
    name: str
    docstring: Optional[str]
    body: str
    decorator_args: dict = field(default_factory=dict)


class DjangoViewSetManager(AppSpecificManager):
    def list_for_app_path(
        self, app_path: Path, app_name: str, package_name: str
    ) -> list["DjangoViewSet"]:
        from .parsing import parse_views_file

        views_path = app_path / "views.py"
        if not views_path.exists():
            return []

        return parse_views_file(
            views_path, app_name=app_name, package_name=package_name
        )


@dataclass
class DjangoViewSet(SourceMappedAppDataclass):
    name: str
    docstring: Optional[str]
    serializer_class: Optional[str]
    permission_classes: list[str] = field(default_factory=list)
    lookup_field: Optional[str] = None
    lookup_url_kwarg: Optional[str] = None
    queryset_definition: Optional[str] = None
    methods: list[ViewSetMethod] = field(default_factory=list)
    actions: list[ViewSetAction] = field(default_factory=list)

    objects = DjangoViewSetManager()
