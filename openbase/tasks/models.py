from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from openbase.core.sourcemapped_dataclass import SourceMappedAppDataclass
from openbase.openbase_app.managers import AppSpecificManager


@dataclass
class TaskArgument:
    name: str
    default: Optional[str] = None


@dataclass
class TaskArguments:
    positional_only: list[str] = field(default_factory=list)
    regular_args: list[str] = field(default_factory=list)
    keyword_only: list[str] = field(default_factory=list)
    defaults: dict = field(default_factory=dict)
    vararg: Optional[str] = None
    kwarg: Optional[str] = None


class TaskiqTaskManager(AppSpecificManager):
    def list_for_app_path(
        self, app_path: Path, app_name: str, package_name: str
    ) -> list["TaskiqTask"]:
        tasks_dir = app_path / "tasks"
        taskiq_tasks = []
        
        # Look for individual task files in the tasks directory
        if tasks_dir.exists() and tasks_dir.is_dir():
            for file in tasks_dir.glob("*.py"):
                if file.name != "__init__.py":
                    from .parsing import parse_task_file
                    
                    tasks_in_file = parse_task_file(
                        file, app_name=app_name, package_name=package_name
                    )
                    taskiq_tasks.extend(tasks_in_file)
        
        return taskiq_tasks


@dataclass
class TaskiqTask(SourceMappedAppDataclass):
    name: str
    is_async: bool
    docstring: str
    body_source: str
    args: TaskArguments

    objects = TaskiqTaskManager()