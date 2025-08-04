from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from openbase.config.managers import ListQuerySet
from openbase.core.sourcemapped_dataclass import SourceMappedDataclass
from openbase.openbase_app.models import DjangoApp


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


class DjangoModelManager:
    def list_for_app_path(self, app_path: Path) -> list["DjangoModel"]:
        from .parsing import parse_models_file

        models_path = app_path / "models.py"
        if not models_path.exists():
            return []

        return parse_models_file(models_path)

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
                model
                for django_app in django_apps
                for model in self.list_for_app_path(django_app.path)
            ]
        )


@dataclass
class DjangoModel(SourceMappedDataclass):
    name: str
    docstring: Optional[str]
    fields: list[DjangoModelField]
    methods: list[DjangoModelMethod]
    properties: list[DjangoModelProperty]
    meta: dict
    save_method: Optional[DjangoModelSpecialMethod]
    str_method: Optional[DjangoModelSpecialMethod]
    app_name: Optional[str] = None

    objects = DjangoModelManager()
