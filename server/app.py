from flask import Flask
from parsing import parse_django_file_ast
from settings import DJANGO_PROJECT_BASE_DIR

from transformation.transform_commands import transform_commands_py
from transformation.transform_models import transform_models_py
from transformation.transform_serializers import transform_serializers_py
from transformation.transform_tasks import transform_tasks_py
from transformation.transform_urls_py import transform_urls_py
from transformation.transform_views_py import transform_views_py

app = Flask(__name__)


def get_django_apps():
    """
    Identifies Django apps by looking for directories containing an apps.py file.
    """
    apps = []
    for item in DJANGO_PROJECT_BASE_DIR.iterdir():
        if item.is_dir():
            # Check if it's a Django app (common indicators: apps.py, models.py)
            if (item / "apps.py").exists() or (item / "models.py").exists():
                apps.append(item.name)
    return apps


@app.route("/debug/")
def debug():
    file_path = (
        DJANGO_PROJECT_BASE_DIR / "finance/management/commands/fill_initial_form.py"
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
    model_file_path = DJANGO_PROJECT_BASE_DIR / appname / "models.py"
    raw_ast = parse_django_file_ast(model_file_path)
    return transform_models_py(raw_ast)


@app.route("/apps/<appname>/tasks/")
def get_tasks(appname):
    tasks_dir = DJANGO_PROJECT_BASE_DIR / appname / "tasks"
    task_files = []
    if tasks_dir.is_dir():
        for f_path in tasks_dir.iterdir():
            if f_path.suffix == ".py" and not f_path.name.startswith("_"):
                task_files.append(f_path.stem)
    return {"tasks": task_files}


@app.route("/apps/<appname>/tasks/<taskname>/")
def get_task_details(appname, taskname):
    task_file_path = DJANGO_PROJECT_BASE_DIR / appname / "tasks" / f"{taskname}.py"
    raw_ast = parse_django_file_ast(task_file_path)
    return transform_tasks_py(raw_ast)


@app.route("/apps/<appname>/commands/")
def get_commands(appname):
    commands_dir = DJANGO_PROJECT_BASE_DIR / appname / "management" / "commands"
    command_files = []
    if commands_dir.is_dir():
        for f_path in commands_dir.iterdir():
            if f_path.suffix == ".py" and not f_path.name.startswith("_"):
                command_files.append(f_path.stem)
    return {"commands": command_files}


@app.route("/apps/<appname>/commands/<commandname>/")
def get_command_details(appname, commandname):
    command_file_path = (
        DJANGO_PROJECT_BASE_DIR
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
    urls_file_path = DJANGO_PROJECT_BASE_DIR / appname / "urls.py"
    raw_ast = parse_django_file_ast(urls_file_path)
    return transform_urls_py(raw_ast)


@app.route("/apps/<appname>/serializers/")
def get_serializers(appname):
    serializer_file_path = DJANGO_PROJECT_BASE_DIR / appname / "serializers.py"
    raw_ast = parse_django_file_ast(serializer_file_path)
    return transform_serializers_py(raw_ast)


@app.route("/apps/<appname>/views/")
def get_views(appname):
    views_file_path = DJANGO_PROJECT_BASE_DIR / appname / "views.py"
    raw_ast = parse_django_file_ast(views_file_path)
    return transform_views_py(raw_ast)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")
