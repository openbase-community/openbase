from dataclasses import dataclass
from pathlib import Path

from openbase.config.managers import ListQuerySet
from openbase.core.sourcemapped_dataclass import SourceMappedDataclass
from openbase.openbase_app.models import DjangoApp


class DjangoModelManager:
    def list_for_app_path(self, app_path: Path) -> list["DjangoModel"]:
        models_path = app_path / "models.py"
        models = []

    def filter(self, **kwargs) -> list["DjangoModel"]:
        app_name = kwargs.pop("app_name", None)
        if app_name is not None:
            django_apps = [DjangoApp.objects.get(name=app_name, **kwargs)]
        elif "app_package_name" in kwargs:
            django_apps = [
                django_app for django_app in DjangoApp.objects.filter(**kwargs)
            ]
        else:
            django_apps = DjangoApp.objects.all()

        return ListQuerySet(
            [
                command
                for django_app in django_apps
                for command in self.list_for_app_path(django_app.path)
            ]
        )


@dataclass
class DjangoModel(SourceMappedDataclass):
    app_name: str
    name: str
    fields: list[str]

    @property
    def name(self) -> str:
        return self.path.stem

    def load_full(self):
        from .parsing import parse_manage_command_file

        return parse_manage_command_file(self.path)
