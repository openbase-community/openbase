from dataclasses import dataclass
from pathlib import Path

from openbase.config.managers import ListQuerySet
from openbase.core.parsing import SourceMappedString
from openbase.core.sourcemapped_dataclass import SourceMappedDataclass
from openbase.openbase_app.models import DjangoApp


class ManageCommandManager:
    def list_for_app_path(self, app_path: Path) -> list["ManageCommand"]:
        manage_commands_path = app_path / "management" / "commands"
        manage_commands = []
        for file in manage_commands_path.glob("*.py"):
            if file.name != "__init__.py":
                manage_commands.append(
                    ManageCommand(
                        path=file,
                        help="",
                        arguments=[],
                        handle_body_source="",
                    )
                )
        return manage_commands

    def filter(self, **kwargs) -> list["ManageCommand"]:
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

    # def get(self, *, name: str, **kwargs) -> "ManageCommand":
    #     unfilled_command = super().get(name=name, **kwargs)

    #     from openbase.manage_commands.parsing import parse_manage_command_file

    #     return parse_manage_command_file(
    #         path=unfilled_command.path,
    #     )


@dataclass
class ManageCommand(SourceMappedDataclass):
    arguments: list[str]
    handle_body_source: str
    help: SourceMappedString = ""

    objects: ManageCommandManager = ManageCommandManager()

    @property
    def name(self) -> str:
        return self.path.stem
