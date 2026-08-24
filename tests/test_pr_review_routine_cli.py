from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from openbase_coder_cli.cli import main

pr_review = importlib.import_module("openbase_coder_cli.cli.pr_review_routine")


class FakeSuperAgentsClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def start_thread(self, input_data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("start_thread", input_data))
        return {"threadId": f"thread-{input_data['name']}"}

    async def start_turn_by_label(
        self,
        input_data: Any,
        turn_input: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "start_turn_by_label",
                {"label": input_data.label, "cwd": input_data.cwd, "turn": turn_input},
            )
        )
        return {"turnId": f"turn-{input_data.label}"}

    async def close(self) -> None:
        self.calls.append(("close", {}))


def _pull_request(repo: str, repo_path: Path, number: int) -> Any:
    return pr_review.PullRequest(
        repo_name=repo,
        repo_path=repo_path,
        number=number,
        title=f"PR {number}",
        url=f"https://example.test/{repo}/pull/{number}",
        author="gabe",
        head_ref=f"feature-{number}",
        base_ref="main",
        updated_at="2026-08-24T12:00:00Z",
        is_draft=False,
    )


def test_group_pull_requests_is_deterministic(tmp_path: Path) -> None:
    cli = tmp_path / "cli"
    console = tmp_path / "console"
    prs = [
        _pull_request("console", console, 3),
        _pull_request("cli", cli, 2),
        _pull_request("cli", cli, 1),
    ]

    groups = pr_review._group_pull_requests(
        prs,
        workspace=tmp_path,
        group_by="repo",
        max_prs_per_turn=2,
        thread_prefix="pr-review",
    )

    assert [group.id for group in groups] == [
        "cli-cli-1-cli-2",
        "console-console-3",
    ]
    assert [pr.number for pr in groups[0].pull_requests] == [1, 2]


def test_dispatch_dry_run_discovers_before_grouping(
    monkeypatch, tmp_path: Path
) -> None:
    repo = pr_review.Repository(name="cli", path=tmp_path / "cli")
    pull_request = _pull_request("cli", repo.path, 7)
    monkeypatch.setattr(
        pr_review, "_workspace_repositories", lambda workspace, include_root: [repo]
    )
    monkeypatch.setattr(
        pr_review,
        "_discover_pull_requests",
        lambda repositories, limit_per_repo, include_drafts: [pull_request],
    )

    result = CliRunner().invoke(
        main,
        [
            "pr-review-routine",
            "dispatch",
            "--workspace",
            str(tmp_path),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert output["launched"] is False
    assert output["pullRequestCount"] == 1
    assert output["groups"][0]["pullRequests"][0]["number"] == 7


def test_dispatch_uses_current_directory_by_default(
    monkeypatch, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.chdir(tmp_path)

    def fake_workspace_repositories(workspace: Path, include_root: bool) -> list[Any]:
        seen["workspace"] = workspace
        return []

    monkeypatch.setattr(
        pr_review, "_workspace_repositories", fake_workspace_repositories
    )
    monkeypatch.setattr(
        pr_review,
        "_discover_pull_requests",
        lambda repositories, limit_per_repo, include_drafts: [],
    )

    result = CliRunner().invoke(
        main,
        ["pr-review-routine", "dispatch", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert seen["workspace"] == tmp_path


def test_dispatch_launches_grouped_high_reasoning_turns(
    monkeypatch, tmp_path: Path
) -> None:
    repo = pr_review.Repository(name="cli", path=tmp_path / "cli")
    prs = [_pull_request("cli", repo.path, 1), _pull_request("cli", repo.path, 2)]
    fake_client = FakeSuperAgentsClient()
    monkeypatch.setattr(
        pr_review, "_workspace_repositories", lambda workspace, include_root: [repo]
    )
    monkeypatch.setattr(
        pr_review,
        "_discover_pull_requests",
        lambda repositories, limit_per_repo, include_drafts: prs,
    )
    monkeypatch.setattr(pr_review, "client_from_environment", lambda: fake_client)
    monkeypatch.setattr(
        pr_review, "configured_backend_from_environment", lambda: "codex"
    )

    result = CliRunner().invoke(
        main,
        [
            "pr-review-routine",
            "dispatch",
            "--workspace",
            str(tmp_path),
            "--max-prs-per-turn",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert fake_client.calls[0][0] == "start_thread"
    assert fake_client.calls[0][1]["name"] == "pr-review-cli-cli-1-cli-2"
    assert fake_client.calls[1][0] == "start_turn_by_label"
    assert fake_client.calls[1][1]["turn"]["reasoningEffort"] == "high"
    assert '"number": 1' in fake_client.calls[1][1]["turn"]["prompt"]
    assert fake_client.calls[-1] == ("close", {})
