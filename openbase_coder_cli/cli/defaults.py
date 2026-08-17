from __future__ import annotations

import click

from openbase_coder_cli import dispatcher_config


@click.group("defaults")
def defaults() -> None:
    """Manage default dispatcher and Super Agents settings."""


@defaults.command("dispatcher-reasoning")
@click.argument("level", required=False)
def dispatcher_reasoning(level: str | None) -> None:
    """Show or set the default dispatcher reasoning effort."""
    if level is None:
        current = dispatcher_config.dispatcher_reasoning_effort() or "app-server default"
        click.echo(f"Default dispatcher reasoning effort: {current}")
        return

    normalized = _normalize_reasoning_effort(level)
    dispatcher_config.set_dispatcher_reasoning_effort(normalized)
    click.echo(f"Default dispatcher reasoning effort set to {normalized}.")


@defaults.command("dispatcher-model")
@click.argument("model", required=False)
@click.option("--backend", default=None, help="Backend to configure. Defaults to the selected coding backend.")
def dispatcher_model(model: str | None, backend: str | None) -> None:
    """Show or set the default dispatcher model."""
    if model is None:
        current = (
            dispatcher_config.backend_model(
                dispatcher_config.DISPATCHER_MODEL_ROLE,
                backend=backend,
            )
            or "backend default"
        )
        click.echo(f"Default dispatcher model: {current}")
        return

    normalized = _set_backend_model(
        dispatcher_config.DISPATCHER_MODEL_ROLE,
        model,
        backend=backend,
    )
    click.echo(f"Default dispatcher model set to {normalized}.")


@defaults.command("super-agents-reasoning")
@click.argument("level", required=False)
def super_agents_reasoning(level: str | None) -> None:
    """Show or set the default Super Agents reasoning effort."""
    if level is None:
        current = dispatcher_config.super_agents_reasoning_effort() or "high"
        click.echo(f"Default Super Agents reasoning effort: {current}")
        return

    normalized = _normalize_reasoning_effort(level)
    dispatcher_config.set_super_agents_reasoning_effort(normalized)
    click.echo(f"Default Super Agents reasoning effort set to {normalized}.")


@defaults.command("super-agents-model")
@click.argument("model", required=False)
@click.option("--backend", default=None, help="Backend to configure. Defaults to the selected coding backend.")
def super_agents_model(model: str | None, backend: str | None) -> None:
    """Show or set the default Super Agents model."""
    if model is None:
        current = (
            dispatcher_config.backend_model(
                dispatcher_config.SUPER_AGENTS_MODEL_ROLE,
                backend=backend,
            )
            or "backend default"
        )
        click.echo(f"Default Super Agents model: {current}")
        return

    normalized = _set_backend_model(
        dispatcher_config.SUPER_AGENTS_MODEL_ROLE,
        model,
        backend=backend,
    )
    click.echo(f"Default Super Agents model set to {normalized}.")


def _normalize_reasoning_effort(level: str) -> str:
    normalized = level.strip().lower()
    if normalized not in dispatcher_config.REASONING_EFFORTS:
        allowed = ", ".join(sorted(dispatcher_config.REASONING_EFFORTS))
        raise click.ClickException(f"Reasoning effort must be one of: {allowed}.")
    return normalized


def _set_backend_model(role: str, model: str, *, backend: str | None) -> str:
    normalized = " ".join(model.split())
    try:
        dispatcher_config.set_backend_model(role, normalized, backend=backend)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    return normalized
