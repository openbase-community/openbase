from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from openbase.core.sourcemapped_dataclass import SourceMappedAppDataclass
from openbase.openbase_app.managers import AppSpecificManager


@dataclass
class RouterRegistration:
    prefix: str
    viewset: str


@dataclass
class UrlPattern:
    route: str
    name: Optional[str]
    view_type: (
        str  # "unknown", "module_include", "class_based_view", "function_based_view"
    )
    view_name: Optional[str] = None
    include_target: Optional[str] = None


class DjangoUrlsManager(AppSpecificManager):
    def list_for_app_path(
        self, app_path: Path, app_name: str, package_name: str
    ) -> list["DjangoUrls"]:
        from .parsing import parse_urls_file

        urls_path = app_path / "urls.py"
        if not urls_path.exists():
            return []

        urls_info = parse_urls_file(
            urls_path, app_name=app_name, package_name=package_name
        )

        # Return a single DjangoUrls object for the app
        if urls_info:
            return [urls_info]
        return []


@dataclass
class DjangoUrls(SourceMappedAppDataclass):
    router_registrations: list[RouterRegistration] = field(default_factory=list)
    urlpatterns: list[UrlPattern] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "urls"

    objects = DjangoUrlsManager()
