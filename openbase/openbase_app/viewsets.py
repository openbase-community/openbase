import os
import subprocess
from pathlib import Path

from django.conf import settings
from dotenv import load_dotenv
from rest_framework import status, viewsets
from rest_framework.decorators import action
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
    ApiPrefixSerializer,
    AppCreatedSerializer,
    AppsListSerializer,
    CommandResultSerializer,
    CommandsResponseSerializer,
    CreateAppSerializer,
    CreateSuperuserSerializer,
    EnvInfoSerializer,
    FileListSerializer,
    MessageSerializer,
    ModelsResponseSerializer,
    RunManagementCommandSerializer,
    SerializersResponseSerializer,
    SourceCodeModificationSerializer,
    TasksResponseSerializer,
    URLsResponseSerializer,
    ViewsResponseSerializer,
)
from .utils import (
    find_app_file,
    get_django_apps,
    get_file_list_from_directory,
    replace_ast_node_source,
    validate_app_exists,
)


class SystemViewSet(viewsets.ViewSet):
    """ViewSet for system-level operations like environment info and management commands."""

    @action(detail=False, methods=['get'])
    def env_info(self, request):
        """Get environment information."""
        data = {
            "django_project_dir": str(settings.DJANGO_PROJECT_DIR),
            "django_project_apps_dirs": [
                str(d) for d in settings.DJANGO_PROJECT_APPS_DIRS
            ],
            "api_prefix": settings.API_PREFIX,
        }
        serializer = EnvInfoSerializer(data)
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def manage(self, request):
        """Execute Django management commands securely."""
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

        response_data = {
            "command": " ".join(cmd),
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "success": result.returncode == 0,
        }
        serializer = CommandResultSerializer(response_data)
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def create_superuser(self, request):
        """Create a Django superuser."""
        serializer = CreateSuperuserSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        validated_data = serializer.validated_data
        username: str = validated_data["username"]
        email: str = validated_data["email"]
        password: str = validated_data["password"]

        # Load environment variables
        env_file = settings.DJANGO_PROJECT_DIR / ".env"
        if env_file.exists():
            load_dotenv(env_file)

        # Determine Python executable
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
            response_data = {"message": "Superuser created successfully"}
            serializer = MessageSerializer(response_data)
            return Response(serializer.data)
        else:
            raise ValidationError(result.stderr)


class AppsViewSet(viewsets.ViewSet):
    """ViewSet for Django app operations."""

    def list(self, request):
        """List all Django apps."""
        apps = get_django_apps()
        data = {"apps": apps}
        serializer = AppsListSerializer(data)
        return Response(serializer.data)

    def create(self, request):
        """Create a new Django app."""
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

        response_data = {
            "message": f"App {app_name} created successfully", 
            "app_dir": str(app_dir)
        }
        serializer = AppCreatedSerializer(response_data)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def models(self, request, pk=None):
        """Get models for a specific Django app."""
        app_name = pk
        app_file = find_app_file(app_name, "models.py", raise_if_not_found=True)
        models_data = parse_django_file_ast(app_file)
        transformed_data = transform_models_py(models_data)
        serializer = ModelsResponseSerializer(transformed_data)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def views(self, request, pk=None):
        """Get views for a specific Django app."""
        app_name = pk
        views_file = find_app_file(app_name, "views.py", raise_if_not_found=True)
        views_data = parse_django_file_ast(views_file)
        transformed_data = transform_views_py(views_data)
        serializer = ViewsResponseSerializer(transformed_data)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def serializers(self, request, pk=None):
        """Get serializers for a specific Django app."""
        app_name = pk
        serializers_file = find_app_file(app_name, "serializers.py", raise_if_not_found=True)
        serializers_data = parse_django_file_ast(serializers_file)
        transformed_data = transform_serializers_py(serializers_data)
        serializer = SerializersResponseSerializer(transformed_data)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def endpoints(self, request, pk=None):
        """Get URL endpoints for a specific Django app."""
        app_name = pk
        urls_file = find_app_file(app_name, "urls.py", raise_if_not_found=True)
        urls_data = parse_django_file_ast(urls_file)
        transformed_data = transform_urls_py(urls_data)
        serializer = URLsResponseSerializer(transformed_data)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def tasks(self, request, pk=None):
        """Get tasks for a specific Django app."""
        app_name = pk
        app_dir = validate_app_exists(app_name)
        tasks_dir = app_dir / "tasks"
        
        if not tasks_dir.exists():
            data = {"tasks": []}
        else:
            tasks = get_file_list_from_directory(tasks_dir)
            data = {"tasks": tasks}
        
        serializer = FileListSerializer(data)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def commands(self, request, pk=None):
        """Get management commands for a specific Django app."""
        app_name = pk
        app_dir = validate_app_exists(app_name)
        commands_dir = app_dir / "management" / "commands"
        
        if not commands_dir.exists():
            data = {"commands": []}
        else:
            commands = get_file_list_from_directory(commands_dir)
            data = {"commands": commands}
        
        serializer = FileListSerializer(data)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def api_prefix(self, request, pk=None):
        """Get API prefix for a specific Django app."""
        data = {"api_prefix": settings.API_PREFIX}
        serializer = ApiPrefixSerializer(data)
        return Response(serializer.data)


class TasksViewSet(viewsets.ViewSet):
    """ViewSet for Django app task operations."""

    def retrieve(self, request, app_name=None, task_name=None):
        """Get details for a specific task."""
        task_file = find_app_file(app_name, f"tasks/{task_name}.py", raise_if_not_found=True)
        task_data = parse_django_file_ast(task_file)
        transformed_data = transform_tasks_py(task_data)
        serializer = TasksResponseSerializer(transformed_data)
        return Response(serializer.data)


class CommandsViewSet(viewsets.ViewSet):
    """ViewSet for Django app management command operations."""

    def retrieve(self, request, app_name=None, command_name=None):
        """Get details for a specific management command."""
        command_file = find_app_file(
            app_name, f"management/commands/{command_name}.py", raise_if_not_found=True
        )
        command_data = parse_django_file_ast(command_file)
        transformed_data = transform_commands_py(command_data)
        serializer = CommandsResponseSerializer(transformed_data)
        return Response(serializer.data)

    def destroy(self, request, app_name=None, command_name=None):
        """Delete a management command."""
        command_file = find_app_file(
            app_name, f"management/commands/{command_name}.py", raise_if_not_found=True
        )
        command_file.unlink()
        response_data = {"message": f"Command {command_name} deleted successfully"}
        serializer = MessageSerializer(response_data)
        return Response(serializer.data)



class SourceCodeViewSet(viewsets.ViewSet):
    """ViewSet for source code modification operations using AST positions."""

    @action(detail=False, methods=['post'])
    def modify(self, request):
        """Modify source code using AST line/column positions."""
        serializer = SourceCodeModificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        validated_data = serializer.validated_data
        file_path = request.data.get('file_path')
        if not file_path:
            raise ValidationError("file_path is required")

        file_path = Path(file_path)
        if not file_path.exists():
            raise NotFound(f"File {file_path} not found")

        # Read the source code
        source_code = file_path.read_text(encoding="utf-8")

        # Perform the replacement
        modified_code = replace_ast_node_source(
            source_code,
            validated_data['start_line'],
            validated_data['start_col'],
            validated_data['end_line'],
            validated_data['end_col'],
            validated_data['replacement']
        )

        # Write back to file
        file_path.write_text(modified_code, encoding="utf-8")

        response_data = {"message": f"Successfully modified {file_path}"}
        serializer = MessageSerializer(response_data)
        return Response(serializer.data)