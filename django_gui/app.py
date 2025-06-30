import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS

from django_gui.parsing import parse_django_file_ast
from django_gui.settings import DJANGO_PROJECT_APPS_DIR, DJANGO_PROJECT_DIR
from django_gui.transformation.transform_commands import transform_commands_py
from django_gui.transformation.transform_models import transform_models_py
from django_gui.transformation.transform_serializers import transform_serializers_py
from django_gui.transformation.transform_tasks import transform_tasks_py
from django_gui.transformation.transform_urls_py import transform_urls_py
from django_gui.transformation.transform_views_py import transform_views_py

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


# Temp
def get_django_apps():
    """
    Identifies Django apps by looking for directories containing an apps.py file.
    """
    apps = []
    for item in DJANGO_PROJECT_APPS_DIR.iterdir():
        if item.is_dir():
            # Check if it's a Django app (common indicators: apps.py, models.py)
            if (item / "apps.py").exists() or (item / "models.py").exists():
                apps.append(item.name)
    return apps


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
    file_path = (
        DJANGO_PROJECT_APPS_DIR / "finance/management/commands/fill_initial_form.py"
    )
    # Example of using preserve_body_for_functions for debugging if needed
    # raw_ast = parse_django_file_ast(file_path, preserve_body_for_functions=["add_arguments", "handle"])
    raw_ast = parse_django_file_ast(file_path)  # Default behavior for general debug
    return raw_ast


@app.route("/apps/")
def list_apps():
    return get_django_apps()


@app.route("/apps/<appname>/models/")
def get_models(appname):
    model_file_path = DJANGO_PROJECT_APPS_DIR / appname / "models.py"
    raw_ast = parse_django_file_ast(model_file_path)
    models_data = transform_models_py(raw_ast)
    # Return as dictionary with Django root path
    return {
        "models": models_data.get("models", [])
        if isinstance(models_data, dict)
        else models_data,
        "django_root": str(DJANGO_PROJECT_APPS_DIR),
    }


@app.route("/apps/<appname>/tasks/")
def get_tasks(appname):
    tasks_dir = DJANGO_PROJECT_APPS_DIR / appname / "tasks"
    task_files = []
    if tasks_dir.is_dir():
        for f_path in tasks_dir.iterdir():
            if f_path.suffix == ".py" and not f_path.name.startswith("_"):
                task_files.append(f_path.stem)
    return {"tasks": task_files}


@app.route("/apps/<appname>/tasks/<taskname>/")
def get_task_details(appname, taskname):
    task_file_path = DJANGO_PROJECT_APPS_DIR / appname / "tasks" / f"{taskname}.py"
    raw_ast = parse_django_file_ast(task_file_path)
    return transform_tasks_py(raw_ast)


@app.route("/apps/<appname>/commands/")
def get_commands(appname):
    commands_dir = DJANGO_PROJECT_APPS_DIR / appname / "management" / "commands"
    command_files = []
    if commands_dir.is_dir():
        for f_path in commands_dir.iterdir():
            if f_path.suffix == ".py" and not f_path.name.startswith("_"):
                command_files.append(f_path.stem)
    return {"commands": command_files}


@app.route("/apps/<appname>/commands/<commandname>/")
def get_command_details(appname, commandname):
    command_file_path = (
        DJANGO_PROJECT_APPS_DIR
        / appname
        / "management"
        / "commands"
        / f"{commandname}.py"
    )
    raw_ast = parse_django_file_ast(
        command_file_path, preserve_body_for_functions=["add_arguments"]
    )
    return transform_commands_py(raw_ast)


@app.route("/apps/<appname>/endpoints/")
def get_endpoints(appname):
    urls_file_path = DJANGO_PROJECT_APPS_DIR / appname / "urls.py"
    raw_ast = parse_django_file_ast(urls_file_path)
    return transform_urls_py(raw_ast)


@app.route("/apps/<appname>/serializers/")
def get_serializers(appname):
    serializer_file_path = DJANGO_PROJECT_APPS_DIR / appname / "serializers.py"
    raw_ast = parse_django_file_ast(serializer_file_path)
    return transform_serializers_py(raw_ast)


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


@app.route("/apps/<appname>/views/")
def get_views(appname):
    views_file_path = DJANGO_PROJECT_APPS_DIR / appname / "views.py"
    raw_ast = parse_django_file_ast(views_file_path)
    views_data = transform_views_py(raw_ast)
    # Return as dictionary with Django root path
    return {
        "viewsets": views_data.get("viewsets", [])
        if isinstance(views_data, dict)
        else views_data,
        "django_root": str(DJANGO_PROJECT_APPS_DIR),
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
