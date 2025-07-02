import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS

from django_gui.parsing import parse_django_file_ast
from django_gui.settings import (
    DJANGO_PROJECT_APPS_DIRS,
    DJANGO_PROJECT_DIR,
)
from django_gui.transformation.transform_commands import transform_commands_py
from django_gui.transformation.transform_models import transform_models_py
from django_gui.transformation.transform_serializers import transform_serializers_py
from django_gui.transformation.transform_tasks import transform_tasks_py
from django_gui.transformation.transform_urls_py import transform_urls_py
from django_gui.transformation.transform_views_py import transform_views_py

# Import boilersync functionality
try:
    from boilersync.commands.pull import pull as boilersync_pull

    BOILERSYNC_AVAILABLE = True
except ImportError:
    BOILERSYNC_AVAILABLE = False

app = Flask(__name__)
CORS(app)

# Load environment variables
load_dotenv()

# Get API prefix from environment variable, default to '/api'
API_PREFIX = os.getenv("DJANGO_API_PREFIX", "/api").strip("/")

# Security: Whitelist of allowed Django management commands
ALLOWED_DJANGO_COMMANDS = {
    # Core Django commands
    "migrate",
    "makemigrations",
    "runserver",
    "shell",
    "dbshell",
    "check",
    "showmigrations",
    "sqlmigrate",
    "inspectdb",
    "diffsettings",
    # Static files and assets
    "collectstatic",
    "findstatic",
    # Data management
    "loaddata",
    "dumpdata",
    "flush",
    # User management
    "createsuperuser",
    "changepassword",
    # Testing
    "test",
    # Development and debugging
    "help",
    "version",
    "startapp",
    "startproject",
    # Internationalization
    "compilemessages",
    "makemessages",
    # Sessions and cache
    "clearsessions",
    "createcachetable",
    # Migration utilities
    "squashmigrations",
    # Common custom commands (add project-specific ones as needed)
    "import_data",
    "export_data",
    "cleanup",
    "backup_db",
    "restore_db",
    "send_emails",
    "process_queue",
    "update_search_index",
    "rebuild_cache",
}


def get_django_apps():
    """
    Identifies Django apps by looking for directories containing an apps.py file
    across all configured app directories.
    """
    apps = []
    for apps_dir in DJANGO_PROJECT_APPS_DIRS:
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
    for apps_dir in DJANGO_PROJECT_APPS_DIRS:
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


@app.route("/manage/", methods=["POST"])
def run_management_command():
    """
    Securely execute Django management commands.
    Expected JSON payload: {"command": "migrate", "args": ["--noinput"], "app_name": "myapp"}
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "JSON payload required"}), 400

        command = data.get("command")
        args = data.get("args", [])
        app_name = data.get("app_name")  # Optional, for app-specific commands

        if not command:
            return jsonify({"error": "Command is required"}), 400

        # Security: Validate command is in whitelist
        if command not in ALLOWED_DJANGO_COMMANDS:
            return jsonify(
                {
                    "error": f"Command '{command}' not allowed. Allowed commands: {sorted(ALLOWED_DJANGO_COMMANDS)}"
                }
            ), 403

        # Security: Validate arguments are strings and don't contain dangerous characters
        if not isinstance(args, list):
            return jsonify({"error": "Args must be a list"}), 400

        for arg in args:
            if not isinstance(arg, str):
                return jsonify({"error": "All arguments must be strings"}), 400
            # Prevent command injection by checking for dangerous characters
            if any(char in arg for char in [";", "&&", "||", "|", "`", "$", ">", "<"]):
                return jsonify(
                    {"error": f"Argument '{arg}' contains invalid characters"}
                ), 400

        # Load environment variables from .env file in Django project directory
        env_file = DJANGO_PROJECT_DIR / ".env"
        if env_file.exists():
            load_dotenv(env_file)

        # Determine the Python executable from the virtual environment
        # Check for both Unix (bin/python) and Windows (Scripts/python.exe) paths
        venv_python_unix = DJANGO_PROJECT_DIR / "venv" / "bin" / "python"
        venv_python_windows = DJANGO_PROJECT_DIR / "venv" / "Scripts" / "python.exe"

        if venv_python_unix.exists():
            python_executable = str(venv_python_unix)
        elif venv_python_windows.exists():
            python_executable = str(venv_python_windows)
        else:
            # Fallback to system python if venv not found
            python_executable = "python"

        # Build the command
        cmd_list = [python_executable, "manage.py", command]

        # Add app name if specified and command supports it
        if app_name and command in [
            "makemigrations",
            "migrate",
            "test",
            "showmigrations",
            "sqlmigrate",
        ]:
            # Validate app name doesn't contain dangerous characters
            if not app_name.replace("_", "").replace("-", "").isalnum():
                return jsonify({"error": "Invalid app name format"}), 400
            cmd_list.append(app_name)

        # Add additional arguments
        cmd_list.extend(args)

        # Execute the command in the Django project directory
        result = subprocess.run(
            cmd_list,
            cwd=DJANGO_PROJECT_DIR,  # Use Django project directory as working directory
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
            env=os.environ.copy(),  # Pass current environment (including loaded .env vars)
        )

        return jsonify(
            {
                "success": result.returncode == 0,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "command": " ".join(cmd_list),
            }
        )

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Command timed out after 5 minutes"}), 408
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500


@app.route("/manage/", methods=["GET"])
def list_management_commands():
    """
    List available Django management commands.
    """
    return jsonify(
        {
            "allowed_commands": sorted(list(ALLOWED_DJANGO_COMMANDS)),
            "usage": {
                "endpoint": "/manage/",
                "method": "POST",
                "payload": {
                    "command": "Command name (required)",
                    "args": "List of arguments (optional)",
                    "app_name": "App name for app-specific commands (optional)",
                },
                "example": {
                    "command": "migrate",
                    "args": ["--noinput"],
                    "app_name": "myapp",
                },
            },
        }
    )


@app.route("/debug/")
def debug():
    # For debug, try to find the file in any of the configured directories
    file_path = find_app_file("finance", "management/commands/fill_initial_form.py")
    if not file_path:
        return jsonify({"error": "Debug file not found"}), 404

    # Example of using preserve_body_for_functions for debugging if needed
    # raw_ast = parse_django_file_ast(file_path, preserve_body_for_functions=["add_arguments", "handle"])
    raw_ast = parse_django_file_ast(file_path)  # Default behavior for general debug
    return raw_ast


@app.route("/apps/")
def list_apps():
    return get_django_apps()


@app.route("/apps/<appname>/models/")
def get_models(appname):
    model_file_path = find_app_file(appname, "models.py")
    if not model_file_path:
        return jsonify({"error": f"Models file not found for app '{appname}'"}), 404

    raw_ast = parse_django_file_ast(model_file_path)
    models_data = transform_models_py(raw_ast)

    # Get the app directory for django_root
    app_dir = find_app_directory(appname)
    django_root = str(app_dir.parent) if app_dir else str(DJANGO_PROJECT_APPS_DIRS[0])

    # Return as dictionary with Django root path
    return {
        "models": models_data.get("models", [])
        if isinstance(models_data, dict)
        else models_data,
        "django_root": django_root,
    }


@app.route("/apps/<appname>/tasks/")
def get_tasks(appname):
    app_dir = find_app_directory(appname)
    if not app_dir:
        return jsonify({"error": f"App '{appname}' not found"}), 404

    tasks_dir = app_dir / "tasks"
    task_files = []
    if tasks_dir.is_dir():
        for f_path in tasks_dir.iterdir():
            if f_path.suffix == ".py" and not f_path.name.startswith("_"):
                task_files.append(f_path.stem)
    return {"tasks": task_files}


@app.route("/apps/<appname>/tasks/<taskname>/")
def get_task_details(appname, taskname):
    task_file_path = find_app_file(appname, f"tasks/{taskname}.py")
    if not task_file_path:
        return jsonify(
            {"error": f"Task file '{taskname}' not found for app '{appname}'"}
        ), 404

    raw_ast = parse_django_file_ast(task_file_path)
    return transform_tasks_py(raw_ast)


@app.route("/apps/<appname>/commands/")
def get_commands(appname):
    app_dir = find_app_directory(appname)
    if not app_dir:
        return jsonify({"error": f"App '{appname}' not found"}), 404

    commands_dir = app_dir / "management" / "commands"
    command_files = []
    if commands_dir.is_dir():
        for f_path in commands_dir.iterdir():
            if f_path.suffix == ".py" and not f_path.name.startswith("_"):
                command_files.append(f_path.stem)
    return {"commands": command_files}


@app.route("/apps/<appname>/commands/<commandname>/")
def get_command_details(appname, commandname):
    command_file_path = find_app_file(appname, f"management/commands/{commandname}.py")
    if not command_file_path:
        return jsonify(
            {"error": f"Command file '{commandname}' not found for app '{appname}'"}
        ), 404

    raw_ast = parse_django_file_ast(
        command_file_path, preserve_body_for_functions=["add_arguments"]
    )
    return transform_commands_py(raw_ast)


@app.route("/apps/<appname>/commands/<commandname>/", methods=["DELETE"])
def delete_command(appname, commandname):
    """
    Delete a Django management command file.
    """
    try:
        command_file_path = find_app_file(
            appname, f"management/commands/{commandname}.py"
        )
        if not command_file_path:
            return jsonify(
                {"error": f"Command file '{commandname}' not found for app '{appname}'"}
            ), 404

        # Delete the file
        command_file_path.unlink()

        return jsonify(
            {
                "success": True,
                "message": f"Command '{commandname}' deleted successfully",
                "command_name": commandname,
                "app_name": appname,
            }
        )

    except Exception as e:
        return jsonify({"error": f"Failed to delete command: {str(e)}"}), 500


@app.route("/apps/<appname>/endpoints/")
def get_endpoints(appname):
    urls_file_path = find_app_file(appname, "urls.py")
    if not urls_file_path:
        return jsonify({"error": f"URLs file not found for app '{appname}'"}), 404

    raw_ast = parse_django_file_ast(urls_file_path)
    return transform_urls_py(raw_ast)


@app.route("/apps/<appname>/serializers/")
def get_serializers(appname):
    serializer_file_path = find_app_file(appname, "serializers.py")
    if not serializer_file_path:
        return jsonify(
            {"error": f"Serializers file not found for app '{appname}'"}
        ), 404

    raw_ast = parse_django_file_ast(serializer_file_path)
    return transform_serializers_py(raw_ast)


@app.route("/apps/<appname>/views/")
def get_views(appname):
    views_file_path = find_app_file(appname, "views.py")
    if not views_file_path:
        return jsonify({"error": f"Views file not found for app '{appname}'"}), 404

    raw_ast = parse_django_file_ast(views_file_path)
    views_data = transform_views_py(raw_ast)

    # Get the app directory for django_root
    app_dir = find_app_directory(appname)
    django_root = str(app_dir.parent) if app_dir else str(DJANGO_PROJECT_APPS_DIRS[0])

    # Return as dictionary with Django root path
    return {
        "viewsets": views_data.get("viewsets", [])
        if isinstance(views_data, dict)
        else views_data,
        "django_root": django_root,
    }


@app.route("/apps/<appname>/api-prefix/")
def get_api_prefix(appname):
    """
    Get the API prefix for the given app.
    This allows the frontend to construct proper API URLs.
    """
    # For now, return the same prefix for all apps
    # In the future, this could be app-specific if needed
    return {
        "prefix": API_PREFIX,
        "base_url": f"http://localhost:8000/{API_PREFIX}",
        "app_name": appname,
    }


@app.route("/settings/create-superuser/", methods=["POST"])
def create_superuser():
    """
    Create a Django superuser programmatically.
    Expected JSON payload: {"email": "test@example.com", "password": "test"}
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "JSON payload required"}), 400

        email = data.get("email")
        password = data.get("password")

        if not email or not password:
            return jsonify({"error": "Both email and password are required"}), 400

        # Validate email format (basic validation)
        if "@" not in email or "." not in email:
            return jsonify({"error": "Invalid email format"}), 400

        # Load environment variables from .env file in Django project directory
        env_file = DJANGO_PROJECT_DIR / ".env"
        if env_file.exists():
            load_dotenv(env_file)

        # Determine the Python executable from the virtual environment
        venv_python_unix = DJANGO_PROJECT_DIR / "venv" / "bin" / "python"
        venv_python_windows = DJANGO_PROJECT_DIR / "venv" / "Scripts" / "python.exe"

        if venv_python_unix.exists():
            python_executable = str(venv_python_unix)
        elif venv_python_windows.exists():
            python_executable = str(venv_python_windows)
        else:
            python_executable = "python"

        # Create superuser using Django management command with environment variables
        env = os.environ.copy()
        env.update(
            {
                "DJANGO_SUPERUSER_EMAIL": email,
                "DJANGO_SUPERUSER_PASSWORD": password,
            }
        )

        cmd_list = [python_executable, "manage.py", "createsuperuser", "--noinput"]

        result = subprocess.run(
            cmd_list,
            cwd=DJANGO_PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=60,  # 1 minute timeout
            env=env,
        )

        if result.returncode == 0:
            return jsonify(
                {
                    "success": True,
                    "message": f"Superuser created successfully with email: {email}",
                    "stdout": result.stdout,
                }
            )
        else:
            # Check if user already exists
            if (
                "already exists" in result.stderr.lower()
                or "already exists" in result.stdout.lower()
            ):
                return jsonify(
                    {
                        "success": False,
                        "message": f"Superuser with email {email} already exists",
                        "error": result.stderr,
                    }
                ), 409
            else:
                return jsonify(
                    {
                        "success": False,
                        "message": "Failed to create superuser",
                        "error": result.stderr,
                        "stdout": result.stdout,
                    }
                ), 500

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Command timed out after 1 minute"}), 408
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500


@app.route("/apps/create/", methods=["POST"])
def create_app():
    """
    Create a new Django app using boilersync template.
    Expected JSON payload: {
        "app_name": "myapp",
        "directory": "/path/to/apps/dir",
        "template_name": "django-app",
        "project_name": "my_project",  # Optional, defaults to app_name
        "pretty_name": "My Pretty Project",  # Optional, defaults to app_name converted to title case
        "parent_package_name": "my_parent_package"  # Optional parent package name for boilersync
    }
    """
    try:
        if not BOILERSYNC_AVAILABLE:
            return jsonify(
                {
                    "error": "Boilersync is not available. Please install it to use this feature."
                }
            ), 500

        data = request.get_json()
        if not data:
            return jsonify({"error": "JSON payload required"}), 400

        app_name = data.get("app_name")
        directory = data.get("directory")
        template_name = data.get("template_name", "django-app")  # Default template

        if not app_name:
            return jsonify({"error": "app_name is required"}), 400

        if not directory:
            return jsonify({"error": "directory is required"}), 400

        # Prepare extra context for boilersync with defaults
        extra_context = {}

        # Set project_name with default
        project_name = data.get("project_name") or app_name
        extra_context["project_name"] = project_name

        # Set pretty_name with default (convert snake_case to Pretty Name)
        pretty_name = (
            data.get("pretty_name")
            or app_name.replace("_", " ").replace("-", " ").title()
        )
        extra_context["pretty_name"] = pretty_name

        # Set parent_package_name with default
        parent_package_name = data.get("parent_package_name") or app_name
        extra_context["parent_package_name"] = parent_package_name

        # Add any other variables from the payload that might be template variables
        for key, value in data.items():
            if (
                key
                not in [
                    "app_name",
                    "directory",
                    "template_name",
                    "project_name",
                    "pretty_name",
                    "parent_package_name",
                ]
                and value
            ):
                extra_context[key] = value

        # Validate app name format
        if not app_name.replace("_", "").replace("-", "").isalnum():
            return jsonify(
                {
                    "error": "Invalid app name format. Use only letters, numbers, underscores, and hyphens."
                }
            ), 400

        # Convert directory to Path object
        directory_path = Path(directory)

        # Validate that the directory is within one of the configured Django app directories
        is_valid_location = False
        for apps_dir in DJANGO_PROJECT_APPS_DIRS:
            try:
                directory_path.resolve().relative_to(apps_dir.resolve())
                is_valid_location = True
                break
            except ValueError:
                continue

        if not is_valid_location:
            return jsonify(
                {
                    "error": f"Directory must be within one of the configured app directories: {[str(d) for d in DJANGO_PROJECT_APPS_DIRS]}"
                }
            ), 400

        # Create the app directory path
        app_directory = directory_path / app_name

        # Check if app already exists
        if app_directory.exists():
            return jsonify(
                {"error": f"App directory '{app_directory}' already exists"}
            ), 409

        # Create empty directory for boilersync (it expects empty directory to exist)
        app_directory.mkdir(parents=True, exist_ok=False)

        # Save current working directory
        original_cwd = os.getcwd()

        try:
            # Change to the app directory and run boilersync pull
            os.chdir(app_directory)

            # Extract project names from extra_context for direct parameter passing
            pull_extra_context = extra_context.copy()
            pull_project_name = pull_extra_context.pop("project_name", None)
            pull_pretty_name = pull_extra_context.pop("pretty_name", None)

            # Use the pull function directly to pass project names as parameters
            boilersync_pull(
                template_name,
                project_name=pull_project_name,
                pretty_name=pull_pretty_name,
                allow_non_empty=False,
                include_starter=True,
                _recursive=False,
                no_input=True,
                extra_context=pull_extra_context,
            )
        finally:
            # Always restore the original working directory
            os.chdir(original_cwd)

        return jsonify(
            {
                "success": True,
                "message": f"App '{app_name}' created successfully",
                "app_directory": str(app_directory),
                "template_used": template_name,
                "project_name": project_name,
                "pretty_name": pretty_name,
                "parent_package_name": parent_package_name,
            }
        )

    except FileNotFoundError as e:
        return jsonify(
            {"error": f"Template '{template_name}' not found: {str(e)}"}
        ), 404
    except FileExistsError as e:
        return jsonify(
            {"error": f"Directory not empty or already exists: {str(e)}"}
        ), 409
    except Exception as e:
        return jsonify({"error": f"Failed to create app: {str(e)}"}), 500


@app.route("/apps/create/", methods=["GET"])
def create_app_info():
    """
    Get information about creating apps, including available directories and template requirements.
    """
    return jsonify(
        {
            "boilersync_available": BOILERSYNC_AVAILABLE,
            "available_directories": [
                str(d) for d in DJANGO_PROJECT_APPS_DIRS if d.exists()
            ],
            "usage": {
                "endpoint": "/apps/create/",
                "method": "POST",
                "payload": {
                    "app_name": "Name of the app to create (required)",
                    "directory": "Directory where the app will be created (required)",
                    "template_name": "Boilersync template name (optional, defaults to 'django-app')",
                    "project_name": "Project name for boilersync (optional, defaults to app_name)",
                    "pretty_name": "Pretty name for boilersync (optional, defaults to app_name converted to title case)",
                    "parent_package_name": "Parent package name for boilersync (optional, defaults to app_name)",
                },
                "example": {
                    "app_name": "myapp",
                    "directory": str(DJANGO_PROJECT_APPS_DIRS[0])
                    if DJANGO_PROJECT_APPS_DIRS
                    else "/path/to/apps",
                    "template_name": "django-app",
                    "project_name": "My Project",
                    "pretty_name": "My Pretty Project",
                    "parent_package_name": "my_parent_package",
                },
            },
        }
    )


# Frontend serving routes (catch-all routes should be last)
@app.route("/")
def serve_index():
    """Serve the React app's index.html at the root."""
    project_root = Path(__file__).parent.parent
    frontend_dir = project_root / "frontend"

    if not frontend_dir.exists():
        return jsonify(
            {
                "error": "Frontend directory not found. Make sure to build the React app first."
            }
        ), 404

    index_path = frontend_dir / "index.html"
    if not index_path.exists():
        return jsonify({"error": "index.html not found in frontend directory."}), 404

    return send_file(index_path)


@app.route("/<path:path>")
def serve_static_or_fallback(path):
    """Serve static files from frontend directory or fallback to index.html for React Router."""
    project_root = Path(__file__).parent.parent
    frontend_dir = project_root / "frontend"

    if not frontend_dir.exists():
        return jsonify({"error": "Frontend directory not found."}), 404

    try:
        # Try to serve the requested file
        return send_from_directory(frontend_dir, path)
    except:
        # Fallback to index.html for React Router (client-side routing)
        index_path = frontend_dir / "index.html"
        if index_path.exists():
            return send_file(index_path)
        else:
            return jsonify({"error": "Frontend files not found."}), 404


def main():
    """Entry point for the django-gui-server command."""
    app.run(debug=True, host="0.0.0.0", port=5050)


def serve_frontend():
    """Entry point for serving the built React dist folder."""

    from flask import Flask

    # Create a new Flask app for serving static files
    static_app = Flask(__name__)

    # Get the dist directory path (relative to the project root)
    project_root = Path(__file__).parent.parent
    dist_dir = project_root / "frontend"

    if not dist_dir.exists():
        print(f"Error: dist directory not found at {dist_dir}")
        print("Make sure to build the React app first with: npm run build")
        return

    @static_app.route("/")
    def serve_index():
        return send_file(dist_dir / "index.html")

    @static_app.route("/<path:path>")
    def serve_static(path):
        try:
            return send_from_directory(dist_dir, path)
        except:
            # For React Router, fallback to index.html for client-side routing
            return send_file(dist_dir / "index.html")

    print(f"Serving React app from: {dist_dir}")
    print("Server running at: http://localhost:8081")
    static_app.run(debug=False, host="0.0.0.0", port=8081)


if __name__ == "__main__":
    main()
