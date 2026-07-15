from __future__ import annotations

from pathlib import Path

from openbase_coder_cli.routines.open_pr_reviews import (
    PullRequestRef,
    ReviewBundle,
    ReviewHandoffConfig,
    discover_multi_workspace_pull_requests,
    group_linked_pull_requests,
    launch_review_handoffs,
    normalize_pr_url,
    pull_request_from_json,
    review_bundle_to_json,
    reviewed_pr_keys,
    unique_pull_requests,
    unreviewed_pull_requests,
)


class Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeReviewClient:
    def __init__(self) -> None:
        self.calls = []

    async def queue_turn_by_label(self, query, turn_input):
        self.calls.append(("queue_turn_by_label", query, turn_input))
        return {"queued": True, "threadId": "thread-review", "turnId": "q_1"}


def test_pull_request_from_json_normalizes_repo_and_url() -> None:
    pr = pull_request_from_json(
        "https://github.com/OpenBase-Community/openbase-coder.git",
        {
            "number": 5,
            "url": "https://github.com/openbase-community/openbase-coder/pull/5/",
            "headRefOid": "head",
            "baseRefOid": "base",
        },
        repo_path="/workspace/cli",
    )

    assert pr is not None
    assert pr.repo == "openbase-community/openbase-coder"
    assert pr.url == "https://github.com/openbase-community/openbase-coder/pull/5"
    assert pr.repo_path == "/workspace/cli"


def test_unique_pull_requests_dedupes_same_pr_across_local_repo_paths() -> None:
    first = PullRequestRef(
        repo="OpenBase-Community/openbase-coder",
        number=5,
        url="https://github.com/openbase-community/openbase-coder/pull/5",
        head_sha="head-a",
        base_sha="base-a",
        repo_path="/workspace-a/cli",
    )
    duplicate = PullRequestRef(
        repo="https://github.com/openbase-community/openbase-coder.git",
        number=5,
        url="https://github.com/openbase-community/openbase-coder/pull/5",
        head_sha="head-a",
        base_sha="base-a",
        repo_path="/workspace-b/openbase-coder",
    )
    other = PullRequestRef(
        repo="openbase-community/openbase-coder",
        number=6,
        url="https://github.com/openbase-community/openbase-coder/pull/6",
        head_sha="head-b",
        base_sha="base-a",
    )

    assert unique_pull_requests([first, duplicate, other]) == [first, other]


def test_unreviewed_pull_requests_skips_existing_report_and_in_run_duplicates(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "open-pr-reviews"
    report_dir.mkdir()
    (report_dir / "reviewed.md").write_text(
        """---
automation: open-pr-review-routine
pr_url: https://github.com/openbase-community/openbase-coder/pull/5/
repo: openbase-community/openbase-coder
pr_number: 5
head_sha: head-a
base_sha: base-a
reviewed_at: 2026-07-02T12:00:00Z
---

# Reviewed
""",
        encoding="utf-8",
    )
    reviewed = PullRequestRef(
        repo="openbase-community/openbase-coder",
        number=5,
        url="https://github.com/openbase-community/openbase-coder/pull/5",
        head_sha="head-a",
        base_sha="base-a",
    )
    changed = PullRequestRef(
        repo="openbase-community/openbase-coder",
        number=5,
        url="https://github.com/openbase-community/openbase-coder/pull/5",
        head_sha="head-b",
        base_sha="base-a",
    )
    duplicate_page = PullRequestRef(
        repo="openbase-community/openbase-coder",
        number=5,
        url="https://github.com/openbase-community/openbase-coder/pull/5",
        head_sha="head-b",
        base_sha="base-a",
    )

    assert unreviewed_pull_requests(
        [reviewed, changed, duplicate_page], report_dir
    ) == [changed]


def test_reviewed_pr_keys_ignores_other_automation(tmp_path: Path) -> None:
    report_dir = tmp_path / "open-pr-reviews"
    report_dir.mkdir()
    (report_dir / "other.md").write_text(
        """---
automation: another-routine
pr_url: https://github.com/openbase-community/openbase-coder/pull/5
head_sha: head-a
base_sha: base-a
---
""",
        encoding="utf-8",
    )

    assert reviewed_pr_keys(report_dir) == set()


def test_normalize_pr_url_preserves_non_url_fallback() -> None:
    assert normalize_pr_url(" owner/repo#5 ") == "owner/repo#5"


def test_discover_multi_workspace_pull_requests_outputs_only_eligible_material(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "multi.json").write_text(
        '{"repos":[{"name":"cli","url":"https://github.com/openbase-community/openbase-coder"}]}',
        encoding="utf-8",
    )
    (workspace / "cli").mkdir()
    report_dir = workspace / ".reports" / "open-pr-reviews"
    report_dir.mkdir(parents=True)
    (report_dir / "reviewed.md").write_text(
        """---
automation: open-pr-review-routine
pr_url: https://github.com/openbase-community/openbase-coder/pull/5
head_sha: reviewed-head
base_sha: base
---
""",
        encoding="utf-8",
    )

    def runner(args, **kwargs):
        if args[:4] == ["git", "-C", str(workspace / "cli"), "remote"]:
            return Completed(
                stdout="https://github.com/openbase-community/openbase-coder.git\n"
            )
        if args[:3] == ["gh", "pr", "list"]:
            return Completed(
                stdout="""[
                  {
                    "number": 5,
                    "url": "https://github.com/openbase-community/openbase-coder/pull/5",
                    "headRefOid": "reviewed-head",
                    "baseRefOid": "base"
                  },
                  {
                    "number": 6,
                    "url": "https://github.com/openbase-community/openbase-coder/pull/6",
                    "headRefOid": "new-head",
                    "baseRefOid": "base"
                  }
                ]"""
            )
        return Completed(returncode=1)

    result = discover_multi_workspace_pull_requests(workspace, runner=runner)

    assert result["candidateCount"] == 2
    assert result["eligibleCount"] == 1
    assert result["eligibleCount"] == 1
    assert result["reviewRequestCount"] == 1
    assert result["eligible"] == [
        {
            "repo": "openbase-community/openbase-coder",
            "number": 6,
            "url": "https://github.com/openbase-community/openbase-coder/pull/6",
            "headSha": "new-head",
            "baseSha": "base",
            "repoPath": str(workspace / "cli"),
            "title": None,
            "author": None,
            "headRefName": None,
            "baseRefName": None,
            "labels": [],
            "reviewKey": {
                "prUrl": "https://github.com/openbase-community/openbase-coder/pull/6",
                "headSha": "new-head",
                "baseSha": "base",
            },
        }
    ]
    assert result["reviewRequests"][0]["pullRequests"] == result["eligible"]
    assert result["skippedSummary"]["alreadyReviewedOrDuplicateMaterial"] == 1


def test_group_linked_pull_requests_uses_shared_branch_and_feature_labels() -> None:
    cli = PullRequestRef(
        repo="openbase-community/openbase-coder",
        number=11,
        url="https://github.com/openbase-community/openbase-coder/pull/11",
        head_sha="cli-head",
        base_sha="main",
        repo_path="/workspace/cli",
        title="Add onboarding routine discovery",
        author="gabe",
        head_ref_name="feature/onboarding-routine-discovery",
        labels=("feature:onboarding",),
    )
    console = PullRequestRef(
        repo="openbase-community/openbase-coder-console",
        number=22,
        url="https://github.com/openbase-community/openbase-coder-console/pull/22",
        head_sha="console-head",
        base_sha="main",
        repo_path="/workspace/console",
        title="Add onboarding routine console",
        author="gabe",
        head_ref_name="feature/onboarding-routine-discovery",
        labels=("feature:onboarding",),
    )
    unrelated = PullRequestRef(
        repo="openbase-community/openbase-coder-react",
        number=33,
        url="https://github.com/openbase-community/openbase-coder-react/pull/33",
        head_sha="react-head",
        base_sha="main",
        title="Fix badge spacing",
        author="gabe",
        head_ref_name="fix/badge-spacing",
    )

    bundles = group_linked_pull_requests([unrelated, console, cli])

    assert len(bundles) == 2
    linked = next(bundle for bundle in bundles if len(bundle.pull_requests) == 2)
    assert [item.number for item in linked.pull_requests] == [11, 22]
    assert "shared-branch-topic:onboarding-routine-discovery" in linked.reasons
    assert "shared-label:feature-onboarding" in linked.reasons

    request = review_bundle_to_json(linked)
    assert len(request["pullRequests"]) == 2
    assert "Do not rediscover repositories" in request["prompt"]
    assert "onboarding-routine-discovery" in request["prompt"]


def test_group_linked_pull_requests_uses_same_author_exact_title_topic() -> None:
    first = PullRequestRef(
        repo="owner/ios",
        number=1,
        url="https://github.com/owner/ios/pull/1",
        head_sha="ios-head",
        base_sha="main",
        title="Implement onboarding settings",
        author="siddharth",
    )
    second = PullRequestRef(
        repo="owner/android",
        number=2,
        url="https://github.com/owner/android/pull/2",
        head_sha="android-head",
        base_sha="main",
        title="Add onboarding settings",
        author="siddharth",
    )
    different_author = PullRequestRef(
        repo="owner/web",
        number=3,
        url="https://github.com/owner/web/pull/3",
        head_sha="web-head",
        base_sha="main",
        title="Add onboarding settings",
        author="gabe",
    )

    bundles = group_linked_pull_requests([first, second, different_author])

    grouped = [bundle for bundle in bundles if len(bundle.pull_requests) == 2]
    assert len(grouped) == 1
    assert {item.repo for item in grouped[0].pull_requests} == {
        "owner/ios",
        "owner/android",
    }
    assert grouped[0].reasons == ("same-author-title-topic:onboarding-settings",)


def test_group_linked_pull_requests_uses_workspace_stack_title_topic_for_related_repos() -> (
    None
):
    console = PullRequestRef(
        repo="openbase-community/openbase-coder-console",
        number=12,
        url="https://github.com/openbase-community/openbase-coder-console/pull/12",
        head_sha="console-head",
        base_sha="main",
        title="Improve routine status badges",
        author="gabe",
    )
    react = PullRequestRef(
        repo="openbase-community/openbase-coder-react",
        number=13,
        url="https://github.com/openbase-community/openbase-coder-react/pull/13",
        head_sha="react-head",
        base_sha="main",
        title="Improve routine status badges",
        author="siddharth",
    )
    android = PullRequestRef(
        repo="openbase-community/openbase-android",
        number=14,
        url="https://github.com/openbase-community/openbase-android/pull/14",
        head_sha="android-head",
        base_sha="main",
        title="Improve settings panel badges",
        author="gabe",
    )

    bundles = group_linked_pull_requests([android, react, console])

    grouped = [bundle for bundle in bundles if len(bundle.pull_requests) == 2]
    assert len(grouped) == 1
    assert {item.repo for item in grouped[0].pull_requests} == {
        "openbase-community/openbase-coder-console",
        "openbase-community/openbase-coder-react",
    }
    assert grouped[0].reasons == (
        "workspace-stack-title:coder-app:improve-routine-status-badges",
    )


def test_group_linked_pull_requests_uses_cross_repo_onboarding_topic_without_same_author() -> (
    None
):
    ios = PullRequestRef(
        repo="owner/ios",
        number=1,
        url="https://github.com/owner/ios/pull/1",
        head_sha="ios-head",
        base_sha="main",
        title="Implement onboarding checklist",
        author="gabe",
    )
    android = PullRequestRef(
        repo="owner/android",
        number=2,
        url="https://github.com/owner/android/pull/2",
        head_sha="android-head",
        base_sha="main",
        title="Add onboarding checklist",
        author="siddharth",
    )
    unrelated = PullRequestRef(
        repo="owner/web",
        number=3,
        url="https://github.com/owner/web/pull/3",
        head_sha="web-head",
        base_sha="main",
        title="Add onboarding preferences",
        author="gabe",
    )

    bundles = group_linked_pull_requests([ios, android, unrelated])

    grouped = [bundle for bundle in bundles if len(bundle.pull_requests) == 2]
    assert len(grouped) == 1
    assert {item.number for item in grouped[0].pull_requests} == {1, 2}
    assert grouped[0].reasons == ("cross-repo-onboarding-topic:onboarding-checklist",)


def test_discovery_includes_explicit_high_reasoning_handoff_metadata(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = discover_multi_workspace_pull_requests(workspace)

    assert result["reviewRequestCount"] == 0

    bundle = ReviewBundle(
        bundle_id="settings-owner-ios-1",
        pull_requests=(
            PullRequestRef(
                repo="owner/ios",
                number=1,
                url="https://github.com/owner/ios/pull/1",
                head_sha="ios-head",
                base_sha="main",
            ),
        ),
        reasons=("single-eligible-pr",),
    )
    request = review_bundle_to_json(bundle)

    assert request["handoff"] == {
        "targetName": "open-pr-review-routine",
        "model": "gpt-5.5",
        "reasoningEffort": "high",
        "serviceTier": "standard",
        "mode": "default",
        "approvalPolicy": "never",
        "sandboxType": "dangerFullAccess",
    }
    assert '"reasoningEffort": "high"' in request["prompt"]


async def test_launch_review_handoffs_uses_super_agents_client_with_scoped_prompt(
    tmp_path: Path,
) -> None:
    bundle = ReviewBundle(
        bundle_id="settings-owner-ios-1",
        pull_requests=(
            PullRequestRef(
                repo="owner/ios",
                number=1,
                url="https://github.com/owner/ios/pull/1",
                head_sha="ios-head",
                base_sha="main",
                repo_path=str(tmp_path / "ios"),
                title="Add settings",
            ),
        ),
        reasons=("single-eligible-pr",),
    )
    client = FakeReviewClient()

    results = await launch_review_handoffs(
        [bundle],
        workspace=tmp_path,
        config=ReviewHandoffConfig(),
        client=client,
    )

    assert results[0]["bundleId"] == "settings-owner-ios-1"
    assert results[0]["model"] == "gpt-5.5"
    assert results[0]["reasoningEffort"] == "high"
    assert results[0]["serviceTier"] == "standard"
    assert len(client.calls) == 1
    _, query, turn_input = client.calls[0]
    assert query.label == "open-pr-review-routine"
    assert turn_input["model"] == "gpt-5.5"
    assert turn_input["reasoningEffort"] == "high"
    assert turn_input["serviceTier"] == "standard"
    assert turn_input["approvalPolicy"] == "never"
    assert turn_input["sandboxType"] == "dangerFullAccess"
    assert "Do not rediscover repositories" in turn_input["prompt"]
    assert "Do not post public comments" in turn_input["prompt"]
    assert "https://github.com/owner/ios/pull/1" in turn_input["prompt"]
