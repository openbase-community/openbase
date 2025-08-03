from dataclasses import dataclass
from pathlib import Path

from openbase.manage_commands.parsing import parse_manage_command_file
from openbase.openbase_app.models import DjangoApp


class ManageCommandManager:
    def filter(self, *, django_app: DjangoApp) -> list["ManageCommand"]:
        app_path = django_app.path
        manage_commands_path = app_path / "management" / "commands"
        manage_commands = []
        for file in manage_commands_path.glob("*.py"):
            if file.name != "__init__.py":
                manage_commands.append(
                    ManageCommand(
                        name=file.stem,
                        path=file,
                        description="",
                        arguments=[],
                        handle_body_source="",
                    )
                )
        return manage_commands

    def get(self, *, django_app: DjangoApp, name: str) -> "ManageCommand":
        unfilled_command = next(
            command
            for command in self.filter(django_app=django_app)
            if command.name == name
        )

        return parse_manage_command_file(
            name=unfilled_command.name,
            path=unfilled_command.path,
        )


@dataclass
class ManageCommand:
    name: str
    path: Path
    arguments: list[str]
    handle_body_source: str
    description: str = ""
    help: str = ""

    objects: ManageCommandManager = ManageCommandManager()
