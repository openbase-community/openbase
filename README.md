# Openbase
- `pipx install openbase`
- `pipx install vscode-multi` (CLI dependency)
- `pipx install boilersync` (CLI dependency)
- Make a new directory and `cd` into it
- Run `openbase init`
- Run `./scripts/setup.sh`
- Open in Cursor
- Press the run button
- Bootstrapping a new project
- Coming soon: easy deployment


## Details
A Django-based meta-server that analyzes Django projects and exposes information about Django apps, models, views, tasks, commands, and more through a REST API.

## Features

- Analyze Django apps and their structure
- Parse Django models, views, serializers, and URLs
- Execute Django management commands securely
- Create new Django apps with boilerplate code
- Extract and transform Django code using AST parsing

## Installation

1. Install the package:

```bash
pip install -e .
```

2. Set up environment variables:

```bash
export DJANGO_PROJECT_DIR=/path/to/your/django/project
export DJANGO_PROJECT_APPS_DIR=/path/to/your/django/apps
```

3. Run the Django server:

```bash
cd openbase
python manage.py runserver
```

## API Endpoints

- `GET /apps/` - List all Django apps
- `GET /apps/<appname>/models/` - Get models for an app
- `GET /apps/<appname>/views/` - Get views for an app
- `GET /apps/<appname>/serializers/` - Get serializers for an app
- `GET /apps/<appname>/tasks/` - Get tasks for an app
- `GET /apps/<appname>/commands/` - Get management commands for an app
- `POST /apps/create/` - Create a new Django app
- `POST /manage/` - Execute Django management commands
- `POST /settings/create-superuser/` - Create a Django superuser

## Environment Variables

- `DJANGO_PROJECT_DIR` - Path to the Django project being analyzed
- `DJANGO_PROJECT_APPS_DIR` - Comma-separated list of app directories
- `DJANGO_API_PREFIX` - API prefix (default: `/api`)
- `SECRET_KEY` - Django secret key
- `DEBUG` - Enable debug mode (default: `True`)
- `ALLOWED_HOSTS` - Comma-separated list of allowed hosts
