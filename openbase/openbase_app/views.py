import os
import subprocess
from pathlib import Path
from typing import Optional, overload

from django.conf import settings
from dotenv import load_dotenv
from rest_framework.decorators import api_view
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from openbase.parsing import parse_django_file_ast
from openbase.transformation.transform_commands import transform_commands_py
from openbase.transformation.transform_models import transform_models_py
from openbase.transformation.transform_serializers import transform_serializers_py
from openbase.transformation.transform_tasks import transform_tasks_py
from openbase.transformation.transform_urls_py import transform_urls_py
from openbase.transformation.transform_views_py import transform_views_py

from .serializers import (
    CreateAppSerializer,
    CreateSuperuserSerializer,
    RunManagementCommandSerializer,
)


def get_django_apps():
    """
    Identifies Django apps by looking for directories containing an apps.py file
    across all configured app directories.
    """
    apps = []
    for apps_dir in settings.DJANGO_PROJECT_APPS_DIRS:
        if not apps_dir.exists():
            continue
        for item in apps_dir.iterdir():
            if item.is_dir():
                # Check if it's a Django app (common indicators: apps.py, models.py)
                if (item / "apps.py").exists() or (item / "models.py").exists():
                    # Store app info with directory context
                    app_info = {
                        "name": item.name,
                        "path": str(item),
                        "apps_dir": str(apps_dir),
                    }
                    # Avoid duplicates based on app name
                    if not any(app["name"] == item.name for app in apps):
                        apps.append(app_info)
    return apps


def find_app_directory(app_name):
    """
    Find the directory path for a given Django app across all configured directories.
    Returns the Path object for the app directory, or None if not found.
    """
    for apps_dir in settings.DJANGO_PROJECT_APPS_DIRS:
        if not apps_dir.exists():
            continue
        app_path = apps_dir / app_name
        if app_path.is_dir() and (
            (app_path / "apps.py").exists() or (app_path / "models.py").exists()
        ):
            return app_path
    return None


@overload
def find_app_file(
    app_name: str, file_path: str, raise_if_not_found: bool = False
) -> Optional[Path]: ...


@overload
def find_app_file(
    app_name: str, file_path: str, raise_if_not_found: bool = True
) -> Path: ...


def find_app_file(
    app_name: str, file_path: str, raise_if_not_found: bool = False
) -> Optional[Path]:
    """
    Find a specific file within an app across all configured directories.
    Returns the Path object for the file, or None if not found.

    Args:
        app_name: Name of the Django app
        file_path: Relative path within the app (e.g., "models.py", "tasks/my_task.py")
        raise_if_not_found: If True, raise NotFound exception instead of returning None
    """
    app_dir = find_app_directory(app_name)
    if app_dir:
        target_file = app_dir / file_path
        if target_file.exists():
            return target_file

    if raise_if_not_found:
        raise NotFound(f"{file_path} not found for app {app_name}")
    return None


@api_view(["POST"])
def run_management_command(request):
    """
    Securely execute Django management commands.
    Expected JSON payload: {"command": "migrate", "args": ["--noinput"]}
    """
    serializer = RunManagementCommandSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    validated_data = serializer.validated_data
    command: str = validated_data["command"]
    args: list = validated_data["args"]

    # Load environment variables from .env file in Django project directory
    env_file = settings.DJANGO_PROJECT_DIR / ".env"
    if env_file.exists():
        load_dotenv(env_file)

    # Determine the Python executable from the virtual environment
    # Check for both Unix (bin/python) and Windows (Scripts/python.exe) paths
    venv_python_unix = settings.DJANGO_PROJECT_DIR / "venv" / "bin" / "python"
    venv_python_windows = (
        settings.DJANGO_PROJECT_DIR / "venv" / "Scripts" / "python.exe"
    )

    if venv_python_unix.exists():
        python_executable = str(venv_python_unix)
    elif venv_python_windows.exists():
        python_executable = str(venv_python_windows)
    else:
        # Fall back to system Python if virtual environment not found
        python_executable = "python"

    # Build the command
    cmd = [
        python_executable,
        str(settings.DJANGO_PROJECT_DIR / "manage.py"),
        command,
    ] + args

    # Execute the command
    result = subprocess.run(
        cmd,
        cwd=str(settings.DJANGO_PROJECT_DIR),
        capture_output=True,
        text=True,
        timeout=300,  # 5 minutes timeout
    )

    return Response(
        {
            "command": " ".join(cmd),
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "success": result.returncode == 0,
        }
    )


@api_view(["GET"])
def env_info(request):
    """Environment info endpoint."""
    return Response(
        {
            "django_project_dir": str(settings.DJANGO_PROJECT_DIR),
            "django_project_apps_dirs": [
                str(d) for d in settings.DJANGO_PROJECT_APPS_DIRS
            ],
            "api_prefix": settings.API_PREFIX,
        }
    )


@api_view(["GET"])
def list_apps(request):
    """List all Django apps."""
    apps = get_django_apps()
    return Response({"apps": apps})


@api_view(["GET"])
def get_models(request, appname):
    """Get models for a specific Django app."""
    app_file = find_app_file(appname, "models.py", raise_if_not_found=True)
    models_data = parse_django_file_ast(app_file)
    return Response(transform_models_py(models_data))


@api_view(["GET"])
def get_tasks(request, appname):
    """Get tasks for a specific Django app."""
    app_dir = find_app_directory(appname)
    if not app_dir:
        raise NotFound(f"App {appname} not found")

    tasks_dir = app_dir / "tasks"
    if not tasks_dir.exists():
        return Response({"tasks": []})

    tasks = []
    for task_file in tasks_dir.glob("*.py"):
        if task_file.name == "__init__.py":
            continue
        tasks.append({"name": task_file.stem, "file": str(task_file)})

    return Response({"tasks": tasks})


@api_view(["GET"])
def get_task_details(request, appname, taskname):
    """Get details for a specific task."""
    task_file = find_app_file(appname, f"tasks/{taskname}.py", raise_if_not_found=True)
    task_data = parse_django_file_ast(task_file)
    return Response(transform_tasks_py(task_data))


@api_view(["GET"])
def get_commands(request, appname):
    """Get management commands for a specific Django app."""
    app_dir = find_app_directory(appname)
    if not app_dir:
        raise NotFound(f"App {appname} not found")

    commands_dir = app_dir / "management" / "commands"
    if not commands_dir.exists():
        return Response({"commands": []})

    commands = []
    for command_file in commands_dir.glob("*.py"):
        if command_file.name == "__init__.py":
            continue
        commands.append({"name": command_file.stem, "file": str(command_file)})

    return Response({"commands": commands})


@api_view(["GET"])
def get_command_details(request, appname, commandname):
    """Get details for a specific management command."""
    command_file = find_app_file(
        appname, f"management/commands/{commandname}.py", raise_if_not_found=True
    )
    command_data = parse_django_file_ast(command_file)
    return Response(transform_commands_py(command_data))


@api_view(["DELETE"])
def delete_command(request, appname, commandname):
    """Delete a management command."""
    command_file = find_app_file(
        appname, f"management/commands/{commandname}.py", raise_if_not_found=True
    )
    command_file.unlink()
    return Response({"message": f"Command {commandname} deleted successfully"})


@api_view(["GET"])
def get_endpoints(request, appname):
    """Get URL endpoints for a specific Django app."""
    urls_file = find_app_file(appname, "urls.py", raise_if_not_found=True)
    urls_data = parse_django_file_ast(urls_file)
    return Response(transform_urls_py(urls_data))


@api_view(["GET"])
def get_serializers(request, appname):
    """Get serializers for a specific Django app."""
    serializers_file = find_app_file(appname, "serializers.py", raise_if_not_found=True)
    serializers_data = parse_django_file_ast(serializers_file)
    return Response(transform_serializers_py(serializers_data))


@api_view(["GET"])
def get_views(request, appname):
    """Get views for a specific Django app."""
    views_file = find_app_file(appname, "views.py", raise_if_not_found=True)
    views_data = parse_django_file_ast(views_file)
    return Response(transform_views_py(views_data))


@api_view(["GET"])
def get_api_prefix(request, appname):
    """Get API prefix for a specific Django app."""
    return Response({"api_prefix": settings.API_PREFIX})


@api_view(["POST"])
def create_superuser(request):
    """Create a Django superuser."""
    serializer = CreateSuperuserSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    validated_data = serializer.validated_data
    username: str = validated_data["username"]
    email: str = validated_data["email"]
    password: str = validated_data["password"]

    # Load environment variables from .env file in Django project directory
    env_file = settings.DJANGO_PROJECT_DIR / ".env"
    if env_file.exists():
        load_dotenv(env_file)

    # Determine the Python executable from the virtual environment
    venv_python_unix = settings.DJANGO_PROJECT_DIR / "venv" / "bin" / "python"
    venv_python_windows = (
        settings.DJANGO_PROJECT_DIR / "venv" / "Scripts" / "python.exe"
    )

    if venv_python_unix.exists():
        python_executable = str(venv_python_unix)
    elif venv_python_windows.exists():
        python_executable = str(venv_python_windows)
    else:
        python_executable = "python"

    # Set password environment variable
    env = os.environ.copy()
    env["DJANGO_SUPERUSER_PASSWORD"] = password

    cmd = [
        python_executable,
        str(settings.DJANGO_PROJECT_DIR / "manage.py"),
        "createsuperuser",
        "--noinput",
        "--username",
        username,
        "--email",
        email,
    ]

    result = subprocess.run(
        cmd,
        cwd=str(settings.DJANGO_PROJECT_DIR),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )

    if result.returncode == 0:
        return Response({"message": "Superuser created successfully"})
    else:
        raise ValidationError(result.stderr)


@api_view(["POST", "GET"])
def create_app(request):
    """Create a new Django app."""
    if request.method == "GET":
        return Response(
            {
                "message": "Use POST to create an app",
                "required_fields": ["app_name", "app_type", "boilerplate_data"],
            }
        )

    serializer = CreateAppSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    validated_data = serializer.validated_data
    app_name: str = validated_data["app_name"]
    app_type: str = validated_data["app_type"]
    boilerplate_data: dict = validated_data["boilerplate_data"]

    # Create the app directory
    app_dir = settings.DJANGO_PROJECT_APPS_DIRS[0] / app_name
    if app_dir.exists():
        raise ValidationError(f"App {app_name} already exists")

    app_dir.mkdir(parents=True)

    # Create basic app structure
    (app_dir / "__init__.py").touch()
    (app_dir / "apps.py").write_text(f"""from django.apps import AppConfig


class {app_name.capitalize()}Config(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = '{app_name}'
""")

    # Create other files based on app_type and boilerplate_data
    if app_type == "full":
        (app_dir / "models.py").write_text(
            "from django.db import models\n\n# Create your models here.\n"
        )
        (app_dir / "views.py").write_text(
            "from django.shortcuts import render\n\n# Create your views here.\n"
        )
        (app_dir / "urls.py").write_text(
            "from django.urls import path\n\nurlpatterns = [\n    # Add your URL patterns here\n]\n"
        )
        (app_dir / "serializers.py").write_text(
            "from rest_framework import serializers\n\n# Create your serializers here.\n"
        )
        (app_dir / "admin.py").write_text(
            "from django.contrib import admin\n\n# Register your models here.\n"
        )
        (app_dir / "tests.py").write_text(
            "from django.test import TestCase\n\n# Create your tests here.\n"
        )

    return Response(
        {"message": f"App {app_name} created successfully", "app_dir": str(app_dir)}
    )
