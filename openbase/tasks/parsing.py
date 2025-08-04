"""
Transform a tasks.py AST into a more readable format focusing on taskiq tasks.
"""

from pathlib import Path

from openbase.core.parsing import parse_python_file_ast
from openbase.core.parsing_utils import extract_function_info

from .models import TaskArguments, TaskiqTask


def parse_task_file(
    file_path: Path, app_name: str, package_name: str
) -> list[TaskiqTask]:
    """
    Parse a task file and extract taskiq task information.
    """
    declarations = parse_python_file_ast(file_path)
    if not declarations:
        return []

    output_tasks = []

    for dec in declarations:
        # Look for both regular and async function definitions
        if dec.get("_nodetype") in ["FunctionDef", "AsyncFunctionDef"]:
            # Check if it's decorated with @broker.task
            for decorator in dec.get("decorator_list", []):
                if (
                    decorator.get("_nodetype") == "Attribute"
                    and decorator.get("attr") == "task"
                    and decorator.get("value", {}).get("_nodetype") == "Name"
                    and decorator.get("value", {}).get("id") == "broker"
                ):
                    # This is a taskiq task, extract its information
                    task_info = extract_function_info(dec)

                    # Convert to TaskArguments dataclass
                    args = TaskArguments(
                        positional_only=task_info["args"]["positional_only"],
                        regular_args=task_info["args"]["regular_args"],
                        keyword_only=task_info["args"]["keyword_only"],
                        defaults=task_info["args"]["defaults"],
                        vararg=task_info["args"]["vararg"],
                        kwarg=task_info["args"]["kwarg"],
                    )

                    task = TaskiqTask(
                        name=task_info["name"],
                        is_async=task_info["is_async"],
                        docstring=task_info["docstring"],
                        body_source=task_info["body_source"],
                        args=args,
                        path=str(file_path),
                        app_name=app_name,
                        package_name=package_name,
                    )
                    output_tasks.append(task)
                    break  # No need to check other decorators

    return output_tasks
