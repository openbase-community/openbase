from flask import Flask, request

from parsing import parse_django_file_ast
from settings import DJANGO_PROJECT_BASE_DIR
from transformation.transform_models import transform_models_file

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


@app.route("/apps/")
def list_apps():
    return get_django_apps()


@app.route("/apps/<appname>/models/")
def get_models(appname):
    model_file_path = DJANGO_PROJECT_BASE_DIR / appname / "models.py"
    raw_ast = parse_django_file_ast(model_file_path)
    if "error" in raw_ast:
        return raw_ast, 404 if "not found" in raw_ast["error"].lower() else 500

    # Return raw AST if raw=true query parameter is present
    if request.args.get("raw", "").lower() == "true":
        return raw_ast

    return transform_models_file(raw_ast)


@app.route("/apps/<appname>/tasks/")
def get_tasks(appname):
    task_file_path = DJANGO_PROJECT_BASE_DIR / appname / "tasks.py"
    return parse_django_file_ast(task_file_path)


@app.route("/apps/<appname>/commands/")
def get_commands(appname):
    commands_dir = DJANGO_PROJECT_BASE_DIR / appname / "management" / "commands"
    if not commands_dir.is_dir():
        return {"error": f"Commands directory not found: {commands_dir}"}, 404

    command_files = []
    for f_path in commands_dir.iterdir():
        if f_path.suffix == ".py" and not f_path.name.startswith("_"):
            command_files.append(f_path.stem)
    return {"app": appname, "commands_found": command_files}


@app.route("/apps/<appname>/endpoints/")
def get_endpoints(appname):
    urls_file_path = DJANGO_PROJECT_BASE_DIR / appname / "urls.py"
    return parse_django_file_ast(urls_file_path)


@app.route("/apps/<appname>/serializers/")
def get_serializers(appname):
    serializer_file_path = DJANGO_PROJECT_BASE_DIR / appname / "serializers.py"
    return parse_django_file_ast(serializer_file_path, "serializers")


@app.route("/apps/<appname>/views/")
def get_views(appname):
    views_file_path = DJANGO_PROJECT_BASE_DIR / appname / "views.py"
    return parse_django_file_ast(views_file_path, "views")


if __name__ == "__main__":
    app.run(debug=True)
