from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from super_agents.app_models import LabelQueryInput
from super_agents.app_server_client import CodexAppServerClient
from super_agents.state import JsonObject, get_string

AUTOMATION_NAME = "open-pr-review-routine"
DEFAULT_REPORT_SUBDIR = "open-pr-reviews"
DEFAULT_REVIEW_THREAD_NAME = "open-pr-review-routine"
DEFAULT_REVIEW_MODEL = "gpt-5.5"
DEFAULT_REVIEW_REASONING_EFFORT = "high"
DEFAULT_REVIEW_SERVICE_TIER = "standard"
DEFAULT_REVIEW_MODE = "default"
DEFAULT_REVIEW_APPROVAL_POLICY = "never"
DEFAULT_REVIEW_SANDBOX_TYPE = "dangerFullAccess"


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    repo: str
    number: int
    url: str
    head_sha: str
    base_sha: str
    repo_path: str | None = None
    title: str | None = None
    author: str | None = None
    head_ref_name: str | None = None
    base_ref_name: str | None = None
    labels: tuple[str, ...] = ()

    @property
    def review_key(self) -> tuple[str, str, str]:
        return (normalize_pr_url(self.url), self.head_sha, self.base_sha)

    @property
    def discovery_key(self) -> tuple[str, int]:
        return (normalize_repo_name(self.repo), self.number)

    @property
    def material_key(self) -> tuple[str, int, str, str]:
        return (*self.discovery_key, self.head_sha, self.base_sha)


def normalize_repo_name(value: str) -> str:
    repo = value.strip()
    if repo.endswith(".git"):
        repo = repo[:-4]
    parsed = urlparse(repo)
    if parsed.netloc:
        repo = parsed.path.strip("/")
    if repo.startswith("git@github.com:"):
        repo = repo.removeprefix("git@github.com:")
    if repo.startswith("github.com/"):
        repo = repo.removeprefix("github.com/")
    return repo.strip("/").lower()


def normalize_pr_url(value: str) -> str:
    url = value.strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.netloc:
        return url.rstrip("/")
    path = parsed.path.rstrip("/")
    return f"https://{parsed.netloc.lower()}{path}"


def pull_request_from_json(
    repo: str, value: JsonObject, *, repo_path: str | None = None
) -> PullRequestRef | None:
    number = value.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        return None
    url = get_string(value, "url")
    head_sha = get_string(value, "headRefOid")
    base_sha = get_string(value, "baseRefOid")
    if not url or not head_sha or not base_sha:
        return None
    return PullRequestRef(
        repo=normalize_repo_name(repo),
        number=number,
        url=normalize_pr_url(url),
        head_sha=head_sha,
        base_sha=base_sha,
        repo_path=repo_path,
        title=get_string(value, "title"),
        author=author_login(value.get("author")),
        head_ref_name=get_string(value, "headRefName"),
        base_ref_name=get_string(value, "baseRefName"),
        labels=label_names(value.get("labels")),
    )


def unique_pull_requests(items: list[PullRequestRef]) -> list[PullRequestRef]:
    seen: set[tuple[str, int, str, str]] = set()
    unique: list[PullRequestRef] = []
    for item in items:
        key = item.material_key
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def reviewed_pr_keys(
    report_dir: Path, *, automation: str = AUTOMATION_NAME
) -> set[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    if not report_dir.exists():
        return keys
    for path in sorted(report_dir.glob("*.md")):
        front_matter = read_front_matter(path)
        if front_matter.get("automation") != automation:
            continue
        pr_url = normalize_pr_url(front_matter.get("pr_url", ""))
        head_sha = front_matter.get("head_sha", "")
        base_sha = front_matter.get("base_sha", "")
        if pr_url and head_sha and base_sha:
            keys.add((pr_url, head_sha, base_sha))
    return keys


def unreviewed_pull_requests(
    items: list[PullRequestRef], report_dir: Path
) -> list[PullRequestRef]:
    reviewed = reviewed_pr_keys(report_dir)
    unique = unique_pull_requests(items)
    pending: list[PullRequestRef] = []
    seen_review_keys: set[tuple[str, str, str]] = set()
    for item in unique:
        review_key = item.review_key
        if review_key in reviewed or review_key in seen_review_keys:
            continue
        seen_review_keys.add(review_key)
        pending.append(item)
    return pending


def discover_multi_workspace_pull_requests(
    workspace: Path,
    *,
    report_dir: Path | None = None,
    handoff_config: ReviewHandoffConfig | None = None,
    runner=subprocess.run,
) -> JsonObject:
    report_dir = report_dir or workspace / ".reports" / DEFAULT_REPORT_SUBDIR
    config = handoff_config or ReviewHandoffConfig()
    candidates: list[PullRequestRef] = []
    skipped_repos: list[JsonObject] = []
    for repo_name in multi_workspace_repo_names(workspace):
        repo_path = workspace / repo_name
        if not repo_path.is_dir():
            skipped_repos.append({"repo": repo_name, "reason": "missing-local-path"})
            continue
        remote = git_remote_url(repo_path, runner=runner)
        if not remote:
            skipped_repos.append(
                {
                    "repo": repo_name,
                    "path": str(repo_path),
                    "reason": "missing-origin-remote",
                }
            )
            continue
        prs = gh_open_pull_requests(repo_path, runner=runner)
        if prs is None:
            skipped_repos.append(
                {
                    "repo": normalize_repo_name(remote),
                    "path": str(repo_path),
                    "reason": "gh-pr-list-failed",
                }
            )
            continue
        for raw_pr in prs:
            pr = pull_request_from_json(remote, raw_pr, repo_path=str(repo_path))
            if pr is not None:
                candidates.append(pr)
    eligible = unreviewed_pull_requests(candidates, report_dir)
    bundles = group_linked_pull_requests(eligible)
    return {
        "automation": AUTOMATION_NAME,
        "workspace": str(workspace),
        "reportDir": str(report_dir),
        "candidateCount": len(unique_pull_requests(candidates)),
        "eligibleCount": len(eligible),
        "reviewRequestCount": len(bundles),
        "eligible": [pull_request_to_json(item) for item in eligible],
        "reviewRequests": [review_bundle_to_json(bundle, config) for bundle in bundles],
        "skippedSummary": {
            "alreadyReviewedOrDuplicateMaterial": len(unique_pull_requests(candidates))
            - len(eligible),
            "repos": skipped_repos,
        },
    }


def multi_workspace_repo_names(workspace: Path) -> list[str]:
    config_path = workspace / "multi.json"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    repos = raw.get("repos")
    if not isinstance(repos, list):
        return []
    names: list[str] = []
    for item in repos:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        url = item.get("url")
        if isinstance(name, str) and name:
            names.append(name)
        elif isinstance(url, str) and url:
            names.append(Path(normalize_repo_name(url)).name)
    return names


def git_remote_url(repo_path: Path, *, runner=subprocess.run) -> str | None:
    result = runner(
        ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def gh_open_pull_requests(
    repo_path: Path, *, runner=subprocess.run
) -> list[JsonObject] | None:
    result = runner(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--json",
            "number,url,title,author,labels,headRefName,baseRefName,headRefOid,baseRefOid",
        ],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return (
        [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, list)
        else None
    )


def pull_request_to_json(item: PullRequestRef) -> JsonObject:
    return {
        "repo": item.repo,
        "number": item.number,
        "url": item.url,
        "headSha": item.head_sha,
        "baseSha": item.base_sha,
        "repoPath": item.repo_path,
        "title": item.title,
        "author": item.author,
        "headRefName": item.head_ref_name,
        "baseRefName": item.base_ref_name,
        "labels": list(item.labels),
        "reviewKey": {
            "prUrl": item.review_key[0],
            "headSha": item.review_key[1],
            "baseSha": item.review_key[2],
        },
    }


@dataclass(frozen=True, slots=True)
class ReviewBundle:
    bundle_id: str
    pull_requests: tuple[PullRequestRef, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewHandoffConfig:
    target_name: str = DEFAULT_REVIEW_THREAD_NAME
    model: str = DEFAULT_REVIEW_MODEL
    reasoning_effort: str = DEFAULT_REVIEW_REASONING_EFFORT
    service_tier: str = DEFAULT_REVIEW_SERVICE_TIER
    mode: str = DEFAULT_REVIEW_MODE
    approval_policy: str = DEFAULT_REVIEW_APPROVAL_POLICY
    sandbox_type: str = DEFAULT_REVIEW_SANDBOX_TYPE


def group_linked_pull_requests(items: list[PullRequestRef]) -> list[ReviewBundle]:
    unique = unique_pull_requests(items)
    if not unique:
        return []
    links = deterministic_link_keys(unique)
    parent = {idx: idx for idx in range(len(unique))}
    reasons_by_root: dict[int, set[str]] = {}

    def find(idx: int) -> int:
        while parent[idx] != idx:
            parent[idx] = parent[parent[idx]]
            idx = parent[idx]
        return idx

    def union(left: int, right: int, reason: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root
            reasons_by_root.setdefault(left_root, set()).update(
                reasons_by_root.pop(right_root, set())
            )
        reasons_by_root.setdefault(find(left), set()).add(reason)

    for reason, indexes in links.items():
        if len(indexes) < 2:
            continue
        first = indexes[0]
        for idx in indexes[1:]:
            union(first, idx, reason)

    groups: dict[int, list[PullRequestRef]] = {}
    for idx, item in enumerate(unique):
        groups.setdefault(find(idx), []).append(item)

    bundles: list[ReviewBundle] = []
    for root, group in groups.items():
        sorted_group = tuple(
            sorted(
                group,
                key=lambda item: (item.repo, item.number, item.head_sha, item.base_sha),
            )
        )
        reasons = tuple(sorted(reasons_by_root.get(find(root), {"single-eligible-pr"})))
        bundles.append(
            ReviewBundle(
                bundle_id=review_bundle_id(sorted_group),
                pull_requests=sorted_group,
                reasons=reasons,
            )
        )
    return sorted(bundles, key=lambda bundle: bundle.bundle_id)


def deterministic_link_keys(items: list[PullRequestRef]) -> dict[str, list[int]]:
    keys: dict[str, list[int]] = {}
    label_counts: dict[str, int] = {}
    branch_counts: dict[str, int] = {}
    title_counts: dict[str, int] = {}
    stack_title_counts: dict[str, int] = {}
    stack_title_repos: dict[str, set[str]] = {}
    onboarding_counts: dict[str, int] = {}
    onboarding_authors: dict[str, set[str]] = {}
    onboarding_repos: dict[str, set[str]] = {}
    label_values = [feature_labels(item) for item in items]
    branch_values = [branch_topic(item.head_ref_name) for item in items]
    title_values = [title_topic(item.title) for item in items]
    stack_values = [workspace_stack_names(item) for item in items]
    onboarding_values = [onboarding_feature_topic(item) for item in items]
    for labels in label_values:
        for label in labels:
            label_counts[label] = label_counts.get(label, 0) + 1
    for branch in branch_values:
        if branch:
            branch_counts[branch] = branch_counts.get(branch, 0) + 1
    for item, title in zip(items, title_values, strict=True):
        if title and item.author:
            key = f"{item.author.lower()}:{title}"
            title_counts[key] = title_counts.get(key, 0) + 1
    for idx, (stacks, title) in enumerate(zip(stack_values, title_values, strict=True)):
        if title:
            for stack in stacks:
                key = f"{stack}:{title}"
                stack_title_counts[key] = stack_title_counts.get(key, 0) + 1
                stack_title_repos.setdefault(key, set()).add(
                    normalize_repo_name(items[idx].repo)
                )
    for onboarding in onboarding_values:
        if onboarding:
            onboarding_counts[onboarding] = onboarding_counts.get(onboarding, 0) + 1
    for item, onboarding in zip(items, onboarding_values, strict=True):
        if onboarding and item.author:
            onboarding_authors.setdefault(onboarding, set()).add(item.author.lower())
        if onboarding:
            onboarding_repos.setdefault(onboarding, set()).add(
                normalize_repo_name(item.repo)
            )

    for idx, item in enumerate(items):
        for label in label_values[idx]:
            if label_counts.get(label, 0) > 1:
                keys.setdefault(f"shared-label:{label}", []).append(idx)
        branch = branch_values[idx]
        if branch and branch_counts.get(branch, 0) > 1:
            keys.setdefault(f"shared-branch-topic:{branch}", []).append(idx)
        title = title_values[idx]
        if title and item.author:
            title_key = f"{item.author.lower()}:{title}"
            if title_counts.get(title_key, 0) > 1:
                keys.setdefault(f"same-author-title-topic:{title}", []).append(idx)
        if title:
            for stack in stack_values[idx]:
                stack_title_key = f"{stack}:{title}"
                if (
                    stack_title_counts.get(stack_title_key, 0) > 1
                    and len(stack_title_repos.get(stack_title_key, set())) > 1
                ):
                    keys.setdefault(
                        f"workspace-stack-title:{stack}:{title}", []
                    ).append(idx)
        onboarding = onboarding_values[idx]
        if (
            onboarding
            and onboarding_counts.get(onboarding, 0) > 1
            and len(onboarding_authors.get(onboarding, set())) > 1
            and len(onboarding_repos.get(onboarding, set())) > 1
        ):
            keys.setdefault(f"cross-repo-onboarding-topic:{onboarding}", []).append(idx)
    return keys


def feature_labels(item: PullRequestRef) -> tuple[str, ...]:
    prefixes = ("feature", "stack", "epic", "issue", "onboarding")
    labels: list[str] = []
    for label in item.labels:
        normalized = slug(label)
        if normalized and any(
            normalized == prefix or normalized.startswith(f"{prefix}-")
            for prefix in prefixes
        ):
            labels.append(normalized)
    return tuple(sorted(set(labels)))


def workspace_stack_names(item: PullRequestRef) -> tuple[str, ...]:
    repo_name = Path(normalize_repo_name(item.repo)).name
    stack_repos = {
        "agent-runtime": {
            "agent-work-scheduler",
            "openbase-coder",
            "openbase-coder-skills",
            "super-agents",
        },
        "coder-app": {
            "boilersync-react",
            "multi-react",
            "openbase-coder",
            "openbase-coder-console",
            "openbase-coder-desktop",
            "openbase-coder-react",
        },
        "ios-auth": {
            "allauth-client-swift",
            "openbase-ios",
        },
        "android-auth": {
            "allauth-client-kotlin",
            "openbase-android",
        },
    }
    return tuple(stack for stack, repos in stack_repos.items() if repo_name in repos)


def onboarding_feature_topic(item: PullRequestRef) -> str | None:
    repo_name = Path(normalize_repo_name(item.repo)).name
    onboarding_repos = {
        "allauth-client-kotlin",
        "allauth-client-swift",
        "android",
        "ios",
        "openbase-android",
        "openbase-coder",
        "openbase-coder-console",
        "openbase-coder-desktop",
        "openbase-coder-react",
        "openbase-ios",
    }
    if repo_name not in onboarding_repos:
        return None
    candidates = [
        branch_topic(item.head_ref_name),
        title_topic(item.title),
        *(label for label in feature_labels(item) if "onboarding" in label),
    ]
    for candidate in candidates:
        if candidate and "onboarding" in candidate:
            return candidate
    return None


def branch_topic(value: str | None) -> str | None:
    if not value:
        return None
    normalized = slug(value)
    prefixes = (
        "feature",
        "feat",
        "stack",
        "topic",
        "bugfix",
        "fix",
        "chore",
        "agent",
        "pr",
    )
    parts = [
        part
        for part in normalized.split("-")
        if part and part not in prefixes and not part.isdigit()
    ]
    topic = "-".join(parts)
    return topic if len(topic) >= 6 else None


def title_topic(value: str | None) -> str | None:
    if not value:
        return None
    normalized = slug(value)
    stopwords = {
        "add",
        "adds",
        "implement",
        "implements",
        "support",
        "for",
        "the",
        "and",
        "to",
        "in",
        "part",
        "phase",
    }
    parts = [
        part
        for part in normalized.split("-")
        if part and part not in stopwords and not part.isdigit()
    ]
    topic = "-".join(parts)
    return topic if len(topic) >= 8 else None


def review_bundle_id(items: tuple[PullRequestRef, ...]) -> str:
    if not items:
        return "empty"
    topic = (
        branch_topic(items[0].head_ref_name)
        or title_topic(items[0].title)
        or f"{Path(items[0].repo).name}-{items[0].number}"
    )
    suffix = "-".join(f"{Path(item.repo).name}-{item.number}" for item in items)
    return slug(f"{topic}-{suffix}")[:120]


def review_bundle_to_json(
    bundle: ReviewBundle, config: ReviewHandoffConfig | None = None
) -> JsonObject:
    resolved_config = config or ReviewHandoffConfig()
    return {
        "bundleId": bundle.bundle_id,
        "reasons": list(bundle.reasons),
        "pullRequests": [pull_request_to_json(item) for item in bundle.pull_requests],
        "prompt": review_bundle_prompt(bundle, resolved_config),
        "handoff": review_handoff_metadata(resolved_config),
    }


def review_bundle_prompt(bundle: ReviewBundle, config: ReviewHandoffConfig) -> str:
    payload = {
        "bundleId": bundle.bundle_id,
        "reasons": list(bundle.reasons),
        "pullRequests": [pull_request_to_json(item) for item in bundle.pull_requests],
        "reviewConfig": {
            "model": config.model,
            "reasoningEffort": config.reasoning_effort,
            "serviceTier": config.service_tier,
        },
    }
    return (
        "Review the already-discovered eligible PR bundle below. "
        "Do not rediscover repositories, list PRs, or broaden scope; review only these material states. "
        "Write one grouped local Markdown review report for the bundle, de-duping shared concerns across linked PRs. "
        "Do not post public comments, approve, request changes, merge, push, deploy, or publish anything.\n\n"
        f"```json\n{json.dumps(payload, indent=2, sort_keys=True)}\n```"
    )


def review_handoff_metadata(config: ReviewHandoffConfig) -> JsonObject:
    return {
        "targetName": config.target_name,
        "model": config.model,
        "reasoningEffort": config.reasoning_effort,
        "serviceTier": config.service_tier,
        "mode": config.mode,
        "approvalPolicy": config.approval_policy,
        "sandboxType": config.sandbox_type,
    }


def review_turn_input(
    bundle: ReviewBundle, workspace: Path, config: ReviewHandoffConfig
) -> JsonObject:
    return {
        "prompt": review_bundle_prompt(bundle, config),
        "cwd": str(workspace),
        "approvalPolicy": config.approval_policy,
        "sandboxType": config.sandbox_type,
        "mode": config.mode,
        "model": config.model,
        "reasoningEffort": config.reasoning_effort,
        "serviceTier": config.service_tier,
        "name": config.target_name,
        "label": config.target_name,
    }


async def launch_review_handoffs(
    bundles: list[ReviewBundle],
    *,
    workspace: Path,
    config: ReviewHandoffConfig,
    client: CodexAppServerClient | None = None,
) -> list[JsonObject]:
    owns_client = client is None
    active_client = client or CodexAppServerClient()
    results: list[JsonObject] = []
    try:
        for bundle in bundles:
            turn_input = review_turn_input(bundle, workspace, config)
            query = LabelQueryInput(
                label=config.target_name, cwd=str(workspace), prefer="latest_any"
            )
            try:
                result = await active_client.queue_turn_by_label(query, turn_input)
            except ValueError as exc:
                thread_result = await active_client.start_thread(
                    {
                        "name": config.target_name,
                        "cwd": str(workspace),
                        "approvalPolicy": config.approval_policy,
                        "sandboxType": config.sandbox_type,
                    }
                )
                raw_thread = thread_result.get("thread")
                thread_id = get_string(thread_result, "threadId") or (
                    get_string(raw_thread, "id")
                    if isinstance(raw_thread, dict)
                    else None
                )
                if not thread_id:
                    raise RuntimeError(
                        f"Could not start review thread {config.target_name}."
                    ) from exc
                result = await active_client.start_turn(
                    {**turn_input, "threadId": thread_id}
                )
            results.append(
                {
                    "bundleId": bundle.bundle_id,
                    "targetName": config.target_name,
                    "model": config.model,
                    "reasoningEffort": config.reasoning_effort,
                    "serviceTier": config.service_tier,
                    **result,
                }
            )
        return results
    finally:
        if owns_client:
            await active_client.close()


def author_login(value: object) -> str | None:
    return get_string(value, "login") if isinstance(value, dict) else None


def label_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names = [get_string(item, "name") for item in value if isinstance(item, dict)]
    return tuple(sorted(name for name in names if name))


def slug(value: str) -> str:
    return re.sub(
        r"-+", "-", re.sub(r"[^0-9A-Za-z]+", "-", value.strip().lower())
    ).strip("-")


def read_front_matter(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    front_matter: dict[str, str] = {}
    for line in text[4:end].splitlines():
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if match:
            front_matter[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return front_matter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Discover open PRs eligible for deterministic review."
    )
    parser.add_argument("--workspace", default=".", help="Multi workspace root.")
    parser.add_argument(
        "--report-dir", help="Directory containing open PR review reports."
    )
    parser.add_argument(
        "--launch-reviews",
        action="store_true",
        help="Start or queue grouped review turns.",
    )
    parser.add_argument(
        "--target-name",
        default=DEFAULT_REVIEW_THREAD_NAME,
        help="Super Agents review thread name.",
    )
    parser.add_argument(
        "--model", default=DEFAULT_REVIEW_MODEL, help="Model for spawned review turns."
    )
    parser.add_argument(
        "--reasoning-effort",
        default=DEFAULT_REVIEW_REASONING_EFFORT,
        choices=["low", "medium", "high", "xhigh"],
        help="Reasoning effort for spawned review turns.",
    )
    parser.add_argument(
        "--service-tier",
        default=DEFAULT_REVIEW_SERVICE_TIER,
        help="Service tier for review turns.",
    )
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()
    report_dir = (
        Path(args.report_dir).expanduser().resolve() if args.report_dir else None
    )
    config = ReviewHandoffConfig(
        target_name=args.target_name,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        service_tier=args.service_tier,
    )
    result = discover_multi_workspace_pull_requests(
        workspace, report_dir=report_dir, handoff_config=config
    )
    if args.launch_reviews:
        bundles = [
            ReviewBundle(
                bundle_id=str(request["bundleId"]),
                pull_requests=tuple(
                    PullRequestRef(
                        repo=str(item["repo"]),
                        number=int(item["number"]),
                        url=str(item["url"]),
                        head_sha=str(item["headSha"]),
                        base_sha=str(item["baseSha"]),
                        repo_path=get_string(item, "repoPath"),
                        title=get_string(item, "title"),
                        author=get_string(item, "author"),
                        head_ref_name=get_string(item, "headRefName"),
                        base_ref_name=get_string(item, "baseRefName"),
                        labels=tuple(
                            str(label)
                            for label in item.get("labels", [])
                            if isinstance(label, str)
                        ),
                    )
                    for item in request.get("pullRequests", [])
                    if isinstance(item, dict)
                ),
                reasons=tuple(
                    str(reason)
                    for reason in request.get("reasons", [])
                    if isinstance(reason, str)
                ),
            )
            for request in result["reviewRequests"]
            if isinstance(request, dict)
        ]
        result["handoffResults"] = asyncio.run(
            launch_review_handoffs(bundles, workspace=workspace, config=config)
        )
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
