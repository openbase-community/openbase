from __future__ import annotations

import json

import click


@click.command("project-dir")
@click.argument("name")
@click.option("--json", "json_output", is_flag=True, help="Print structured JSON.")
def project_dir(name: str, json_output: bool) -> None:
    """Resolve a project name to its directory, for a Super Agent's cwd.

    Exits non-zero with the known candidates when the name is unknown or
    ambiguous; never falls back to a root directory.
    """
    from openbase_coder_cli.project_resolution import resolve_project_dir

    result = resolve_project_dir(name)
    if json_output:
        click.echo(
            json.dumps(
                {
                    "name": name,
                    "path": result.path,
                    "error": result.error,
                    "candidates": result.candidates,
                },
                sort_keys=True,
            )
        )
        if result.path is None:
            raise SystemExit(1)
        return
    if result.path is None:
        lines = [result.error or "Project not found."]
        lines += [f"  {candidate}" for candidate in result.candidates]
        raise click.ClickException("\n".join(lines))
    click.echo(result.path)
