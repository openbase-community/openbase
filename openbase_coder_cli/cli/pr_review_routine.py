from __future__ import annotations

import asyncio
import inspect
import json
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
from super_agents.app_models import LabelQueryInput
from super_agents.app_protocol import extract_thread_id, extract_turn_id
from super_agents.backend_clients import (
    client_from_environment,
    configured_backend_from_environment,
)

Json = dict[str, Any]

DEFAULT_MAX_PRS_PER_TURN = 3
DEFAULT_DISCOVERY_LIMIT = 50
DEFAULT_THREAD_PREFIX = "pr-review"


@dataclass(frozen=True, slots=True)
class Repository:
    name: str
    path: Path
    remote_url: str | None = None

    def to_json(self) -> Json:
        return {
            "name": self.name,
            "path": str(self.path),
            "remoteUrl": self.remote_url,
        }


@dataclass(frozen=True, slots=True)
class PullRequest:
    repo_name: str
    repo_path: Path
    number: int
    title: str
    url: str
    author: str | None
    head_ref: str | None
    base_ref: str | None
    updated_at: str | None
    is_draft: bool

    def to_json(self) -> Json:
        return {
            "repo": self.repo_name,
            "repoPath": str(self.repo_path),
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "author": self.author,
            "headRefName": self.head_ref,
            "baseRefName": self.base_ref,
            "updatedAt": self.updated_at,
            "isDraft": self.is_draft,
        }


@dataclass(frozen=True, slots=True)
class ReviewGroup:
    id: str
    thread_name: str
    cwd: Path
    pull_requests: tuple[PullRequest, ...]

    def to_json(self) -> Json:
        return {
            "id": self.id,
            "threadName": self.thread_name,
            "cwd": str(self.cwd),
            "pullRequests": [
                pull_request.to_json() for pull_request in self.pull_requests
            ],
        }


def _json_echo(value: Json) -> None:
    click.echo(json.dumps(value, indent=2, sort_keys=True))


def _run_text_command(args: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise click.ClickException(f"{' '.join(args)} failed in {cwd}: {message}")
    return completed.stdout.strip()


def _run_json_command(args: list[str], *, cwd: Path) -> Any:
    output = _run_text_command(args, cwd=cwd)
    try:
        return json.loads(output or "null")
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            f"{' '.join(args)} returned invalid JSON: {exc}"
        ) from None


def _is_git_repo(path: Path) -> bool:
    try:
        _run_text_command(["git", "rev-parse", "--is-inside-work-tree"], cwd=path)
    except click.ClickException:
        return False
    return True


def _remote_url(path: Path) -> str | None:
    try:
        return (
            _run_text_command(["git", "config", "--get", "remote.origin.url"], cwd=path)
            or None
        )
    except click.ClickException:
        return None


def _workspace_repositories(
    workspace: Path, *, include_root: bool = True
) -> list[Repository]:
    workspace = workspace.expanduser().resolve()
    repositories: dict[str, Repository] = {}
    if include_root and _is_git_repo(workspace):
        repositories["."] = Repository(
            name=workspace.name,
            path=workspace,
            remote_url=_remote_url(workspace),
        )

    multi_json = workspace / "multi.json"
    if multi_json.exists():
        try:
            config = json.loads(multi_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"Invalid multi.json: {exc}") from None
        for raw_repo in config.get("repos", []):
            if not isinstance(raw_repo, dict):
                continue
            name = str(raw_repo.get("name") or "").strip()
            if not name:
                continue
            repo_path = workspace / name
            if repo_path.exists() and _is_git_repo(repo_path):
                repositories[name] = Repository(
                    name=name,
                    path=repo_path.resolve(),
                    remote_url=_remote_url(repo_path),
                )

    return sorted(repositories.values(), key=lambda repo: repo.name)


def _pull_request_from_gh(repo: Repository, value: Json) -> PullRequest | None:
    try:
        number = int(value["number"])
    except (KeyError, TypeError, ValueError):
        return None
    author = value.get("author")
    author_login = author.get("login") if isinstance(author, dict) else None
    return PullRequest(
        repo_name=repo.name,
        repo_path=repo.path,
        number=number,
        title=str(value.get("title") or ""),
        url=str(value.get("url") or ""),
        author=author_login,
        head_ref=value.get("headRefName"),
        base_ref=value.get("baseRefName"),
        updated_at=value.get("updatedAt"),
        is_draft=bool(value.get("isDraft")),
    )


def _discover_pull_requests(
    repositories: Iterable[Repository],
    *,
    limit_per_repo: int,
    include_drafts: bool,
) -> list[PullRequest]:
    pull_requests: list[PullRequest] = []
    fields = "number,title,url,author,headRefName,baseRefName,updatedAt,isDraft"
    for repo in repositories:
        raw_prs = _run_json_command(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                str(limit_per_repo),
                "--json",
                fields,
            ],
            cwd=repo.path,
        )
        if not isinstance(raw_prs, list):
            raise click.ClickException(
                f"gh pr list returned non-list JSON for {repo.name}."
            )
        for raw_pr in raw_prs:
            if not isinstance(raw_pr, dict):
                continue
            pull_request = _pull_request_from_gh(repo, raw_pr)
            if pull_request and (include_drafts or not pull_request.is_draft):
                pull_requests.append(pull_request)
    return sorted(pull_requests, key=lambda pr: (pr.repo_name, pr.number))


def _slug(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z]+", "-", value).strip("-").lower()
    return slug or "group"


def _chunks(values: list[PullRequest], size: int) -> Iterable[list[PullRequest]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _group_pull_requests(
    pull_requests: list[PullRequest],
    *,
    workspace: Path,
    group_by: str,
    max_prs_per_turn: int,
    thread_prefix: str,
) -> list[ReviewGroup]:
    if max_prs_per_turn < 1:
        raise click.BadParameter("Use at least one PR per review turn.")
    sorted_prs = sorted(pull_requests, key=lambda pr: (pr.repo_name, pr.number))
    grouped: list[ReviewGroup] = []
    if group_by == "all":
        buckets = [("workspace", workspace.resolve(), sorted_prs)]
    else:
        buckets = []
        repo_names = sorted({pull_request.repo_name for pull_request in sorted_prs})
        for repo_name in repo_names:
            repo_prs = [
                pull_request
                for pull_request in sorted_prs
                if pull_request.repo_name == repo_name
            ]
            cwd = repo_prs[0].repo_path if repo_prs else workspace.resolve()
            buckets.append((repo_name, cwd, repo_prs))

    for bucket_name, cwd, bucket_prs in buckets:
        for chunk in _chunks(bucket_prs, max_prs_per_turn):
            suffix = "-".join(f"{pr.repo_name}-{pr.number}" for pr in chunk)
            group_id = _slug(f"{bucket_name}-{suffix}")
            grouped.append(
                ReviewGroup(
                    id=group_id,
                    thread_name=f"{thread_prefix}-{group_id}",
                    cwd=cwd,
                    pull_requests=tuple(chunk),
                )
            )
    return grouped


def _developer_instructions_for_review() -> str:
    return (
        "You are performing a code-review-only Super Agents turn for a bounded group of open pull requests. "
        "Do not edit files, commit, push, approve, request changes, leave GitHub comments, merge, close, or "
        "change PR state. Inspect the listed PRs with git and gh as needed. Prioritize correctness bugs, "
        "behavioral regressions, security issues, data loss risks, and missing tests. Findings must lead the "
        "final response, ordered by severity, with file and line references when available. If there are no "
        "findings, say that clearly and note residual test or verification risk."
    )


def _prompt_for_group(group: ReviewGroup) -> str:
    pull_requests_json = json.dumps(
        [pull_request.to_json() for pull_request in group.pull_requests],
        indent=2,
        sort_keys=True,
    )
    return (
        "Review exactly this bounded group of open pull requests. Discovery and grouping have already run; "
        "do not search for additional PRs unless needed only to inspect one of these listed PRs.\n\n"
        f"Review group: {group.id}\n"
        f"Pull requests:\n{pull_requests_json}\n\n"
        "For each PR, inspect the diff and relevant surrounding code. Report actionable findings only. "
        "Do not make repository or GitHub state changes."
    )


async def _maybe_close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def _launch_review_groups(
    groups: list[ReviewGroup],
    *,
    model: str | None,
    reasoning_effort: str,
) -> list[Json]:
    client = client_from_environment()
    try:
        launches = []
        for group in groups:
            thread_payload: Json = {
                "name": group.thread_name,
                "cwd": str(group.cwd),
                "approvalPolicy": "never",
                "sandboxType": "dangerFullAccess",
                "developerInstructions": _developer_instructions_for_review(),
            }
            if model:
                thread_payload["model"] = model
            thread = await client.start_thread(thread_payload)
            turn_payload: Json = {
                "prompt": _prompt_for_group(group),
                "mode": "default",
                "approvalPolicy": "never",
                "sandboxType": "dangerFullAccess",
                "reasoningEffort": reasoning_effort,
            }
            if model:
                turn_payload["model"] = model
            turn = await client.start_turn_by_label(
                LabelQueryInput(label=group.thread_name, cwd=str(group.cwd)),
                turn_payload,
            )
            launches.append(
                {
                    "group": group.to_json(),
                    "backend": configured_backend_from_environment(),
                    "threadId": extract_thread_id(thread) or extract_thread_id(turn),
                    "turnId": extract_turn_id(turn),
                    "thread": thread,
                    "turn": turn,
                }
            )
        return launches
    finally:
        await _maybe_close_client(client)


@click.group("pr-review-routine")
def pr_review_routine() -> None:
    """Discover open PRs, group them, and launch Super Agents review turns."""


@pr_review_routine.command("dispatch")
@click.option(
    "--workspace",
    "workspace_path",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("."),
    help="Workspace or repository root to scan.",
)
@click.option("--include-root/--no-include-root", default=True, show_default=True)
@click.option("--include-drafts", is_flag=True, help="Include draft pull requests.")
@click.option(
    "--limit-per-repo",
    type=int,
    default=DEFAULT_DISCOVERY_LIMIT,
    show_default=True,
    help="Maximum open PRs to fetch from each repository.",
)
@click.option(
    "--group-by",
    type=click.Choice(("repo", "all")),
    default="repo",
    show_default=True,
)
@click.option(
    "--max-prs-per-turn",
    type=int,
    default=DEFAULT_MAX_PRS_PER_TURN,
    show_default=True,
)
@click.option("--thread-prefix", default=DEFAULT_THREAD_PREFIX, show_default=True)
@click.option("--model")
@click.option(
    "--reasoning-effort",
    type=click.Choice(("high", "xhigh")),
    default="high",
    show_default=True,
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print discovery and grouping without launching agents.",
)
def dispatch_pr_reviews(
    workspace_path: Path,
    include_root: bool,
    include_drafts: bool,
    limit_per_repo: int,
    group_by: str,
    max_prs_per_turn: int,
    thread_prefix: str,
    model: str | None,
    reasoning_effort: str,
    dry_run: bool,
) -> None:
    """Run deterministic PR discovery/grouping, then launch grouped review turns."""
    if limit_per_repo < 1:
        raise click.BadParameter("Use a positive --limit-per-repo.")
    workspace = workspace_path.expanduser().resolve()
    repositories = _workspace_repositories(workspace, include_root=include_root)
    pull_requests = _discover_pull_requests(
        repositories,
        limit_per_repo=limit_per_repo,
        include_drafts=include_drafts,
    )
    groups = _group_pull_requests(
        pull_requests,
        workspace=workspace,
        group_by=group_by,
        max_prs_per_turn=max_prs_per_turn,
        thread_prefix=thread_prefix,
    )
    discovery = {
        "workspace": str(workspace),
        "repositoryCount": len(repositories),
        "repositories": [repository.to_json() for repository in repositories],
        "pullRequestCount": len(pull_requests),
        "pullRequests": [pull_request.to_json() for pull_request in pull_requests],
        "groupCount": len(groups),
        "groups": [group.to_json() for group in groups],
    }
    if dry_run or not groups:
        _json_echo({**discovery, "launched": False, "launches": []})
        return
    launches = asyncio.run(
        _launch_review_groups(
            groups,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    )
    _json_echo({**discovery, "launched": True, "launches": launches})
