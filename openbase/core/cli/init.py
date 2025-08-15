"""Init command for Openbase CLI."""

import json
import os
import subprocess
from pathlib import Path

import click
from boilersync.commands.init import init as boilersync_init
from vscode_multi.sync import sync

from ..paths import get_boilerplate_dir, get_openbase_dir


def setup_boilerplate_dir():
    """Set up the boilerplate directory, cloning from repo if needed."""
    openbase_dir = get_openbase_dir()
    boilerplate_dir = get_boilerplate_dir()

    # Create ~/.openbase if it doesn't exist
    openbase_dir.mkdir(parents=True, exist_ok=True)

    # If boilerplate directory doesn't exist, clone it
    if not boilerplate_dir.exists():
        click.echo("Boilerplate directory not found. Cloning from repository...")
        try:
            subprocess.run(
                [
                    "git",
                    "clone",
                    "https://github.com/openbase-community/openbase-boilerplate.git",
                    str(boilerplate_dir),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            click.echo(f"Successfully cloned boilerplate to {boilerplate_dir}")
        except subprocess.CalledProcessError as e:
            click.echo(f"Error cloning boilerplate repository: {e}")
            if e.stderr:
                click.echo(f"Error details: {e.stderr}")
            raise click.Exit(1)
    else:
        click.echo(f"Using existing boilerplate directory: {boilerplate_dir}")
        # Pull latest changes from origin
        click.echo("Pulling latest changes from origin...")
        try:
            result = subprocess.run(
                ["git", "pull", "origin"],
                cwd=str(boilerplate_dir),
                capture_output=True,
                text=True,
                check=False,  # Don't raise exception on non-zero exit
            )
            if result.returncode != 0:
                click.echo(f"Warning: git pull failed with: {result.stderr}")
            else:
                click.echo("Successfully pulled latest changes")
        except Exception as e:
            click.echo(f"Warning: Could not pull from origin: {e}")

    return boilerplate_dir


@click.command()
def init():
    """Initialize a new Openbase project in the current directory."""
    # Set up the boilerplate directory
    boilerplate_dir = setup_boilerplate_dir()

    # Set the BOILERSYNC_TEMPLATE_DIR environment variable
    os.environ["BOILERSYNC_TEMPLATE_DIR"] = str(boilerplate_dir)

    # Run boilersync init with the app-package template
    current_dir = Path.cwd()

    click.echo("Initializing Openbase project...")
    project_name_kebab = current_dir.name
    project_name_snake = project_name_kebab.replace("-", "_")
    app_name = f"{project_name_snake}_app"
    package_name_snake = project_name_snake
    apps = f'"{package_name_snake}.{app_name}"'
    app_package_dir = current_dir / project_name_kebab
    app_package_dir.mkdir(parents=True, exist_ok=True)
    boilersync_init(
        "app-package",
        app_package_dir,
        collected_variables={
            "apps": apps,
            "name_snake": project_name_snake,
            "name_kebab": project_name_kebab,
        },
    )

    # Set BOILERSYNC_ROOT_DIR and run boilersync init for the app
    click.echo("Initializing app repository with template...")
    app_dir = app_package_dir / project_name_snake / app_name
    app_dir.mkdir(parents=True, exist_ok=True)
    boilersync_init(
        "django-app",
        app_dir,
        collected_variables={"apps": apps},
    )

    # Create multi.json file
    multi_json_path = current_dir / "multi.json"
    multi_config = {
        "repos": [
            {"url": "https://github.com/openbase-community/web"},
            {"url": f"https://github.com/montaguegabe/{project_name_kebab}"},
        ]
    }

    with open(multi_json_path, "w") as f:
        json.dump(multi_config, f, indent=2)

    click.echo(f"Created multi.json at {multi_json_path}")

    # Create the GitHub repo if it doesn't exist
    click.echo(
        f"Checking if GitHub repository montaguegabe/{project_name_kebab} exists..."
    )
    try:
        # Check if repo exists
        check_result = subprocess.run(
            ["gh", "repo", "view", f"montaguegabe/{project_name_kebab}"],
            capture_output=True,
            text=True,
            check=False,
        )

        if check_result.returncode != 0:
            # Repo doesn't exist, create it
            click.echo(
                f"Creating GitHub repository montaguegabe/{project_name_kebab}..."
            )
            subprocess.run(
                [
                    "gh",
                    "repo",
                    "create",
                    f"montaguegabe/{project_name_kebab}",
                    "--private",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            click.echo(
                f"Successfully created repository montaguegabe/{project_name_kebab}"
            )
        else:
            click.echo(f"Repository montaguegabe/{project_name_kebab} already exists")
    except subprocess.CalledProcessError as e:
        click.echo(f"Error creating GitHub repository: {e}")
        if e.stderr:
            click.echo(f"Error details: {e.stderr}")
        click.echo("You may need to create the repository manually")
    except Exception as e:
        click.echo(f"Warning: Could not check/create GitHub repository: {e}")

    # Run vscode_multi sync
    click.echo("Syncing multi-repository workspace...")
    sync(ensure_on_same_branch=False)

    # Initialize git repository
    click.echo("Initializing git repository...")
    init_result = subprocess.run(
        ["git", "init"],
        cwd=str(current_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if init_result.returncode != 0:
        click.echo(f"Warning: git init failed with: {init_result.stderr}")

    # Create an initial git commit after syncing
    click.echo("Creating initial git commit...")
    add_result = subprocess.run(
        ["git", "add", "."],
        cwd=str(current_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if add_result.returncode != 0:
        click.echo(f"Warning: git add failed with: {add_result.stderr}")
    else:
        commit_result = subprocess.run(
            ["git", "commit", "-am", "Initial commit"],
            cwd=str(current_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        if commit_result.returncode != 0:
            click.echo(f"Warning: git commit failed with: {commit_result.stderr}")

    click.echo("Openbase project initialized successfully!")
