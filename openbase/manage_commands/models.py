from dataclasses import dataclass, field
from pathlib import Path

from openbase.core.parsing import SourceMappedString
from openbase.core.sourcemapped_dataclass import SourceMappedAppDataclass
from openbase.openbase_app.managers import AppSpecificManager


class ManageCommandManager(AppSpecificManager):
    def list_for_app_path(
        self, app_path: Path, app_name: str, package_name: str
    ) -> list["ManageCommand"]:
        manage_commands_path = app_path / "management" / "commands"
        manage_commands = []
        for file in manage_commands_path.glob("*.py"):
            if file.name != "__init__.py":
                manage_commands.append(
                    ManageCommand(
                        path=file,
                        app_name=app_name,
                        package_name=package_name,
                    )
                )
        return manage_commands


@dataclass
class ManageCommand(SourceMappedAppDataclass):
    arguments: list[str] = field(default_factory=list)
    handle_body_source: str = ""
    help: SourceMappedString = ""

    objects: ManageCommandManager = ManageCommandManager()

    @property
    def name(self) -> str:
        return self.path.stem

    def load_full(self):
        from .parsing import parse_manage_command_file

        return parse_manage_command_file(
            self.path, app_name=self.app_name, package_name=self.package_name
        )
