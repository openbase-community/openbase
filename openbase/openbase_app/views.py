import os
import subprocess

from django.conf import settings
from django.http import Http404, HttpResponse
from dotenv import load_dotenv
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from ..parsing import parse_django_file_ast

# Import boilersync functionality
try:
    from boilersync.commands.pull import pull as boilersync_pull

    BOILERSYNC_AVAILABLE = True
except ImportError:
    BOILERSYNC_AVAILABLE = False


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


def find_app_file(app_name, file_path):
    """
    Find a specific file within an app across all configured directories.
    Returns the Path object for the file, or None if not found.

    Args:
        app_name: Name of the Django app
        file_path: Relative path within the app (e.g., "models.py", "tasks/my_task.py")
    """
    app_dir = find_app_directory(app_name)
    if app_dir:
        target_file = app_dir / file_path
        if target_file.exists():
            return target_file
    return None


@api_view(["POST"])
@permission_classes([AllowAny])
def run_management_command(request):
    """
    Securely execute Django management commands.
    Expected JSON payload: {"command": "migrate", "args": ["--noinput"], "app_name": "myapp"}
    """
    try:
        data = request.data
        if not data:
            return Response(
                {"error": "JSON payload required"}, status=status.HTTP_400_BAD_REQUEST
            )

        command = data.get("command")
        args = data.get("args", [])
        app_name = data.get("app_name")  # Optional, for app-specific commands

        if not command:
            return Response(
                {"error": "Command is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        # Security: Validate command is in whitelist
        if command not in settings.ALLOWED_DJANGO_COMMANDS:
            return Response(
                {
                    "error": f"Command '{command}' not allowed. Allowed commands: {sorted(settings.ALLOWED_DJANGO_COMMANDS)}"
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # Security: Validate arguments are strings and don't contain dangerous characters
        if not isinstance(args, list):
            return Response(
                {"error": "Args must be a list"}, status=status.HTTP_400_BAD_REQUEST
            )

        for arg in args:
            if not isinstance(arg, str):
                return Response(
                    {"error": "All arguments must be strings"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # Prevent command injection by checking for dangerous characters
            if any(char in arg for char in [";", "&&", "||", "|", "`", "$", ">", "<"]):
                return Response(
                    {"error": f"Argument '{arg}' contains invalid characters"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

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

    except subprocess.TimeoutExpired:
        return Response(
            {"error": "Command timed out after 5 minutes"},
            status=status.HTTP_408_REQUEST_TIMEOUT,
        )
    except Exception as e:
        return Response(
            {"error": f"Failed to execute command: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([AllowAny])
def list_management_commands(request):
    """List all allowed Django management commands."""
    return Response({"commands": sorted(settings.ALLOWED_DJANGO_COMMANDS)})


@api_view(["GET"])
@permission_classes([AllowAny])
def debug(request):
    """Debug endpoint to check configuration."""
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
@permission_classes([AllowAny])
def list_apps(request):
    """List all Django apps."""
    apps = get_django_apps()
    return Response({"apps": apps})


@api_view(["GET"])
@permission_classes([AllowAny])
def get_models(request, appname):
    """Get models for a specific Django app."""
    app_file = find_app_file(appname, "models.py")
    if not app_file:
        return Response(
            {"error": f"models.py not found for app {appname}"},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        models_data = parse_django_file_ast(app_file)
        return Response(models_data)
    except Exception as e:
        return Response(
            {"error": f"Failed to parse models.py: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([AllowAny])
def get_tasks(request, appname):
    """Get tasks for a specific Django app."""
    app_dir = find_app_directory(appname)
    if not app_dir:
        return Response(
            {"error": f"App {appname} not found"}, status=status.HTTP_404_NOT_FOUND
        )

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
@permission_classes([AllowAny])
def get_task_details(request, appname, taskname):
    """Get details for a specific task."""
    task_file = find_app_file(appname, f"tasks/{taskname}.py")
    if not task_file:
        return Response(
            {"error": f"Task {taskname} not found for app {appname}"},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        task_data = parse_django_file_ast(task_file)
        return Response(task_data)
    except Exception as e:
        return Response(
            {"error": f"Failed to parse task file: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([AllowAny])
def get_commands(request, appname):
    """Get management commands for a specific Django app."""
    app_dir = find_app_directory(appname)
    if not app_dir:
        return Response(
            {"error": f"App {appname} not found"}, status=status.HTTP_404_NOT_FOUND
        )

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
@permission_classes([AllowAny])
def get_command_details(request, appname, commandname):
    """Get details for a specific management command."""
    command_file = find_app_file(appname, f"management/commands/{commandname}.py")
    if not command_file:
        return Response(
            {"error": f"Command {commandname} not found for app {appname}"},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        command_data = parse_django_file_ast(command_file)
        return Response(command_data)
    except Exception as e:
        return Response(
            {"error": f"Failed to parse command file: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["DELETE"])
@permission_classes([AllowAny])
def delete_command(request, appname, commandname):
    """Delete a management command."""
    command_file = find_app_file(appname, f"management/commands/{commandname}.py")
    if not command_file:
        return Response(
            {"error": f"Command {commandname} not found for app {appname}"},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        command_file.unlink()
        return Response({"message": f"Command {commandname} deleted successfully"})
    except Exception as e:
        return Response(
            {"error": f"Failed to delete command: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([AllowAny])
def get_endpoints(request, appname):
    """Get URL endpoints for a specific Django app."""
    urls_file = find_app_file(appname, "urls.py")
    if not urls_file:
        return Response(
            {"error": f"urls.py not found for app {appname}"},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        urls_data = parse_django_file_ast(urls_file)
        return Response(urls_data)
    except Exception as e:
        return Response(
            {"error": f"Failed to parse urls.py: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([AllowAny])
def get_serializers(request, appname):
    """Get serializers for a specific Django app."""
    serializers_file = find_app_file(appname, "serializers.py")
    if not serializers_file:
        return Response(
            {"error": f"serializers.py not found for app {appname}"},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        serializers_data = parse_django_file_ast(serializers_file)
        return Response(serializers_data)
    except Exception as e:
        return Response(
            {"error": f"Failed to parse serializers.py: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([AllowAny])
def get_views(request, appname):
    """Get views for a specific Django app."""
    views_file = find_app_file(appname, "views.py")
    if not views_file:
        return Response(
            {"error": f"views.py not found for app {appname}"},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        views_data = parse_django_file_ast(views_file)
        return Response(views_data)
    except Exception as e:
        return Response(
            {"error": f"Failed to parse views.py: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([AllowAny])
def get_api_prefix(request, appname):
    """Get API prefix for a specific Django app."""
    return Response({"api_prefix": settings.API_PREFIX})


@api_view(["POST"])
@permission_classes([AllowAny])
def create_superuser(request):
    """Create a Django superuser."""
    data = request.data
    username = data.get("username")
    email = data.get("email")
    password = data.get("password")

    if not all([username, email, password]):
        return Response(
            {"error": "Username, email, and password are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
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
            return Response(
                {"error": result.stderr}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    except Exception as e:
        return Response(
            {"error": f"Failed to create superuser: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST", "GET"])
@permission_classes([AllowAny])
def create_app(request):
    """Create a new Django app."""
    if request.method == "GET":
        return Response(
            {
                "message": "Use POST to create an app",
                "required_fields": ["app_name", "app_type", "boilerplate_data"],
            }
        )

    data = request.data
    app_name = data.get("app_name")
    app_type = data.get("app_type")
    boilerplate_data = data.get("boilerplate_data", {})

    if not app_name:
        return Response(
            {"error": "app_name is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    # Create the app directory
    app_dir = settings.DJANGO_PROJECT_APPS_DIRS[0] / app_name
    if app_dir.exists():
        return Response(
            {"error": f"App {app_name} already exists"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
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

    except Exception as e:
        return Response(
            {"error": f"Failed to create app: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


def serve_index(request):
    """Serve the index.html file."""
    # For now, return a simple response
    return HttpResponse("Openbase Django Meta-Server")


def serve_static_or_fallback(request, path):
    """Serve static files or return 404."""
    raise Http404("File not found")
