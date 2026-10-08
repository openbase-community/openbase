"""What the Sync page shows: health, backlog, conflicts, stale git locks.

Everything here is derived from the Openbase Sync daemon (status, conflicts,
stale-locks), its configuration, and two read-only looks at the disk: the
daemon's content-addressed version store (to show both sides of a text
conflict) and the repositories a ``git-branch`` conflict names. The daemon
owns all sync state. The only state kept here is in process memory: when each
peer was last seen connected, and the last stale-lock scan (which the daemon
needs minutes to answer on a large tree).
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from openbase_coder_cli import sync_daemon
from openbase_coder_cli.paths import OPENBASE_BASE_DIR

# A git lock file older than this is a leftover from a git process that died,
# not an operation in flight (the daemon uses the same threshold).
STALE_LOCK_AGE_S = 600

# How long a stale-lock scan stays fresh before the next request refreshes it
# in the background.
STALE_LOCK_TTL_S = 300

# The daemon walks every replicated repository to list stale locks.
STALE_LOCK_SCAN_TIMEOUT_S = 300.0

# Resolving waits for the root's worker, which may be busy applying changes.
RESOLVE_TIMEOUT_S = 30.0

# Text larger than this is not inlined (or diffed) by the conflict detail view.
MAX_TEXT_BYTES = 256 * 1024
MAX_DIFF_LINES = 4000

GIT_LOCK_NAMES = {
    "index.lock",
    "HEAD.lock",
    "packed-refs.lock",
    "config.lock",
    "shallow.lock",
}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


# --- peers ------------------------------------------------------------------


class PeerPresence:
    """Remembers when each peer was last connected (this process's memory).

    The daemon lists only connected peers, so "last seen" is what this
    process observed while polling; it resets when Openbase restarts.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, dict[str, Any]] = {}

    def observe(self, peers: Iterable[dict[str, Any]], now: float) -> None:
        with self._lock:
            for peer in peers:
                device = str(peer.get("device") or "")
                if device:
                    self._seen[device] = {
                        "device": device,
                        "role": str(peer.get("role") or ""),
                        "last_seen": now,
                    }

    def offline(self, connected: Iterable[str]) -> list[dict[str, Any]]:
        connected = set(connected)
        with self._lock:
            return [
                {**entry, "last_seen": _iso(entry["last_seen"])}
                for device, entry in sorted(self._seen.items())
                if device not in connected
            ]

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()


PRESENCE = PeerPresence()


def _iso(epoch_s: float | None) -> str | None:
    if epoch_s is None:
        return None
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# --- overview ---------------------------------------------------------------


def overview(
    status: dict[str, Any],
    *,
    config_roots: list[dict[str, Any]] | None = None,
    offline_peers: list[dict[str, Any]] | None = None,
    stale_lock_count: int | None = None,
) -> dict[str, Any]:
    """Health summary and per-root backlog from a daemon ``status`` payload.

    Per root and peer: ``unsent`` is the journal head minus what was sent,
    ``unacked`` what was sent minus what the peer confirmed, and
    ``received_seq`` how far this computer has applied the peer's journal
    (the peer's own head is only known on that computer).

    ``state`` is the headline: ``offline`` (an edge whose hub is not
    connected), ``waiting`` (a hub with no computer connected), ``scanning``,
    ``syncing`` or ``in_sync``. ``attention`` counts what needs the user,
    independent of the headline.
    """
    roots = [root for root in status.get("roots") or [] if isinstance(root, dict)]
    peers = [peer for peer in status.get("peers") or [] if isinstance(peer, dict)]
    config_by_id = {
        str(root.get("id") or ""): root for root in config_roots or [] if root
    }
    role = str(status.get("role") or "")
    out_roots: list[dict[str, Any]] = []
    totals = {"unsent": 0, "unacked": 0, "pending_fetches": 0, "entries": 0}
    for root in roots:
        root_id = str(root.get("id") or "")
        seq = _int(root.get("seq"))
        peer_rows = []
        for peer in peers:
            peer_roots = peer.get("roots") or {}
            if root_id not in peer_roots:
                # a project-only computer that syncs only some folders: it
                # has nothing to send or receive for this one
                continue
            progress = peer_roots.get(root_id) or {}
            sent = _int(progress.get("sent_seq"))
            acked = _int(progress.get("acked_seq"))
            peer_rows.append(
                {
                    "device": str(peer.get("device") or ""),
                    "sent_seq": sent,
                    "acked_seq": acked,
                    "received_seq": _int(progress.get("applied_peer_seq")),
                    "unsent": max(0, seq - sent),
                    "unacked": max(0, sent - acked),
                }
            )
        unsent = max((row["unsent"] for row in peer_rows), default=0)
        unacked = max((row["unacked"] for row in peer_rows), default=0)
        pending = _int(root.get("pending_fetches"))
        config = config_by_id.get(root_id, {})
        disk = root.get("disk") if isinstance(root.get("disk"), dict) else None
        out_roots.append(
            {
                "id": root_id,
                "path": str(root.get("path") or ""),
                "entries": _int(root.get("entries")),
                "seq": seq,
                "pending_fetches": pending,
                "scanning": bool(root.get("scanning")),
                "pins": list(config.get("pins") or []),
                "ignore": list(config.get("ignore") or []),
                "unsent": unsent,
                "unacked": unacked,
                "peers": peer_rows,
                "bytes": _int(root.get("bytes")),
                "disk": _disk_summary(disk),
            }
        )
        totals["unsent"] += unsent
        totals["unacked"] += unacked
        totals["pending_fetches"] += pending
        totals["entries"] += _int(root.get("entries"))

    if not peers:
        state = "waiting" if role == "hub" else "offline"
    elif any(root["scanning"] for root in out_roots):
        state = "scanning"
    elif totals["unsent"] or totals["unacked"] or totals["pending_fetches"]:
        state = "syncing"
    else:
        state = "in_sync"
    conflicts = _int(status.get("open_conflicts"))
    low_disk = [
        root["path"]
        for root in out_roots
        if root["disk"] and (root["disk"]["below_low_water"] or root["disk"]["held_files"])
    ]
    versions = status.get("versions") if isinstance(status.get("versions"), dict) else None
    placement = status.get("placement") if isinstance(status.get("placement"), dict) else {}
    return {
        "state": state,
        "role": role,
        "device": str(status.get("device") or ""),
        "peers_connected": len(peers),
        "offline_peers": offline_peers or [],
        "totals": totals,
        "roots": out_roots,
        "versions": _versions_summary(versions),
        "thin": bool(placement.get("thin")),
        "attention": {
            "conflicts": conflicts,
            "stale_locks": stale_lock_count,
            "low_disk": low_disk,
            "needed": bool(conflicts or stale_lock_count or low_disk),
        },
    }


def _disk_summary(disk: dict[str, Any] | None) -> dict[str, Any] | None:
    """A root's volume and limits as the daemon reports them (None: older daemon)."""
    if disk is None:
        return None
    return {
        "free_bytes": _known_bytes(disk.get("free_bytes")),
        "total_bytes": _known_bytes(disk.get("total_bytes")),
        "low_water_bytes": _int(disk.get("low_water_bytes")),
        "low_water_auto": bool(disk.get("low_water_auto")),
        "below_low_water": bool(disk.get("below_low_water")),
        "held_files": _int(disk.get("held_files")),
        "held_bytes": _int(disk.get("held_bytes")),
        "refused_writes": _int(disk.get("refused_writes")),
        "lazy_threshold_bytes": _int(disk.get("lazy_threshold_bytes")),
        "pinned_threshold_bytes": _int(disk.get("pinned_threshold_bytes")),
    }


def _known_bytes(value: Any) -> int | None:
    """A byte count, or None when the daemon could not read it (-1)."""
    if value is None:
        return None
    number = _int(value)
    return number if number >= 0 else None


def _versions_summary(versions: dict[str, Any] | None) -> dict[str, Any] | None:
    if versions is None:
        return None
    return {
        "usage_bytes": _int(versions.get("usage_bytes")),
        "quota_bytes": _int(versions.get("quota_bytes")),
        "quota_auto": bool(versions.get("quota_auto")),
        "retention_days": versions.get("retention_days"),
        "over_quota": bool(versions.get("over_quota")),
    }


# --- conflicts --------------------------------------------------------------


def _root_paths(roots: Iterable[dict[str, Any]]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for root in roots:
        if root.get("id") and root.get("path"):
            out[str(root["id"])] = Path(str(root["path"])).expanduser()
    return out


class _RepoFinder:
    """Finds the git repository (if any) a root-relative path lives in."""

    def __init__(self, root_path: Path | None):
        self.root_path = root_path
        self._cache: dict[str, str | None] = {}

    def repo_for_dir(self, rel_dir: str) -> str | None:
        if rel_dir in self._cache:
            return self._cache[rel_dir]
        found: str | None = None
        if self.root_path is not None:
            candidate = self.root_path / rel_dir if rel_dir else self.root_path
            if (candidate / ".git").exists():
                found = rel_dir
            elif rel_dir:
                found = self.repo_for_dir(_parent(rel_dir))
        self._cache[rel_dir] = found
        return found


def _parent(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def enrich_conflicts(
    conflicts: Iterable[dict[str, Any]],
    *,
    roots: Iterable[dict[str, Any]],
    local_device: str = "",
) -> list[dict[str, Any]]:
    """Adds what the page groups and labels by.

    ``repo`` is the root-relative git repository the conflict belongs to
    (``""`` when none), ``ref`` the git ref of a ``git-branch`` conflict,
    ``group`` the repository or, outside one, the parent folder, and
    ``a_is_local`` whether side A is this computer.
    """
    root_paths = _root_paths(roots)
    finders: dict[str, _RepoFinder] = {}
    out: list[dict[str, Any]] = []
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            continue
        root_id = str(conflict.get("root") or "")
        path = str(conflict.get("path") or "")
        kind = str(conflict.get("kind") or "")
        finder = finders.setdefault(root_id, _RepoFinder(root_paths.get(root_id)))
        ref = ""
        if kind == "git-branch" and ":" in path:
            repo, ref = path.split(":", 1)
            repo = "" if repo == "." else repo
            in_repo = True
        else:
            found = finder.repo_for_dir(_parent(path))
            in_repo = found is not None
            repo = found or ""
        group = repo if in_repo else _parent(path)
        root_path = root_paths.get(root_id)
        out.append(
            {
                **conflict,
                "root_path": str(root_path) if root_path else "",
                "repo": repo if in_repo else "",
                "ref": ref,
                "group": group,
                "a_is_local": not local_device
                or str(conflict.get("a_device") or "") == local_device,
            }
        )
    return out


def find_conflict(
    conflicts: Iterable[dict[str, Any]], conflict_id: int
) -> dict[str, Any] | None:
    for conflict in conflicts:
        if isinstance(conflict, dict) and _int(conflict.get("id")) == conflict_id:
            return conflict
    return None


GIT_BRANCH_REFUSAL = (
    "A diverged branch is not resolved by picking a file version: neither "
    "computer's branch was moved. Merge or rebase in git on either computer; "
    "the conflict closes once both point at the same commit."
)


def branch_refusal(conflicts: Iterable[dict[str, Any]], conflict_id: int) -> str | None:
    """Why keep/take must not be sent for this conflict (``None``: it may).

    The daemon's file resolution does not apply to ``git-branch`` conflicts:
    it would close the record without moving either ref.
    """
    conflict = find_conflict(conflicts, conflict_id)
    if conflict is not None and conflict.get("kind") == "git-branch":
        return GIT_BRANCH_REFUSAL
    return None


def resolution_choice(conflict: dict[str, Any], action: str, local_device: str) -> str:
    """Daemon side (``a`` or ``b``) for a user-facing resolution action."""
    if action in {"a", "b"}:
        return action
    a_is_local = (
        not local_device or str(conflict.get("a_device") or "") == local_device
    )
    if action in {"keep_local", "keep_mine"}:
        return "a" if a_is_local else "b"
    return "b" if a_is_local else "a"


# --- conflict detail: versions ----------------------------------------------


def version_path(content_hash: str, store: Path) -> Path | None:
    """Where the daemon's version store keeps content with this hash."""
    if not _HEX64.match(content_hash or ""):
        return None
    return store / content_hash[:2] / content_hash


def describe_version(content_hash: str, store: Path) -> dict[str, Any]:
    """Size and, for small UTF-8 text, the content of one stored version."""
    info: dict[str, Any] = {
        "hash": content_hash or "",
        "available": False,
        "size": None,
        "binary": False,
        "truncated": False,
        "text": None,
    }
    path = version_path(content_hash, store)
    if path is None or not path.is_file():
        return info
    size = path.stat().st_size
    info.update(available=True, size=size)
    if size > MAX_TEXT_BYTES:
        info["truncated"] = True
        return info
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        info["binary"] = True
        return info
    try:
        info["text"] = data.decode("utf-8")
    except UnicodeDecodeError:
        info["binary"] = True
    return info


def unified_diff(
    before: str, after: str, *, before_label: str, after_label: str
) -> tuple[str, bool]:
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=before_label,
            tofile=after_label,
        )
    )
    truncated = len(lines) > MAX_DIFF_LINES
    text = "".join(
        line if line.endswith("\n") else line + "\n" for line in lines[:MAX_DIFF_LINES]
    )
    return text, truncated


def file_conflict_detail(
    conflict: dict[str, Any], *, store: Path, root_path: Path | None
) -> dict[str, Any]:
    a = describe_version(str(conflict.get("a_hash") or ""), store)
    b = describe_version(str(conflict.get("b_hash") or ""), store)
    ancestor = describe_version(str(conflict.get("ancestor") or ""), store)
    detail: dict[str, Any] = {
        "kind": conflict.get("kind"),
        "versions": {"a": a, "b": b, "ancestor": ancestor},
        "diff": None,
        "diff_truncated": False,
        "current": None,
    }
    if isinstance(a.get("text"), str) and isinstance(b.get("text"), str):
        detail["diff"], detail["diff_truncated"] = unified_diff(
            b["text"],
            a["text"],
            before_label=f"{conflict.get('b_device') or 'other computer'}",
            after_label=f"{conflict.get('a_device') or 'this computer'}",
        )
    if root_path is not None:
        target = root_path / str(conflict.get("path") or "")
        try:
            stat = target.lstat()
            detail["current"] = {
                "exists": True,
                "size": stat.st_size,
                "modified": _iso(stat.st_mtime),
                "is_dir": target.is_dir(),
            }
        except OSError:
            detail["current"] = {"exists": False}
    return detail


# --- conflict detail: git branches ------------------------------------------


def _git(repo: Path, *args: str, timeout: float = 5.0) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _commits(repo: Path, spec: str, limit: int = 20) -> list[dict[str, str]]:
    out = _git(repo, "log", f"--max-count={limit}", "--format=%H%x09%s", spec)
    commits = []
    for line in (out or "").splitlines():
        sha, _, subject = line.partition("\t")
        commits.append({"sha": sha, "subject": subject})
    return commits


def git_branch_detail(
    conflict: dict[str, Any], *, root_path: Path | None, local_device: str = ""
) -> dict[str, Any]:
    """Where each computer's branch points, and how the two relate.

    The daemon never moves either ref for a diverged branch; the conflict
    closes by itself once both computers point at the same commit.
    """
    path = str(conflict.get("path") or "")
    repo_rel, _, ref = path.partition(":")
    repo_rel = "" if repo_rel == "." else repo_rel
    a_is_local = (
        not local_device or str(conflict.get("a_device") or "") == local_device
    )
    this_sha = str(conflict.get("a_hash" if a_is_local else "b_hash") or "")
    other_sha = str(conflict.get("b_hash" if a_is_local else "a_hash") or "")
    detail: dict[str, Any] = {
        "kind": "git-branch",
        "repo": repo_rel,
        "repo_path": "",
        "ref": ref,
        "branch": ref.removeprefix("refs/heads/")
        if ref.startswith("refs/heads/")
        else "",
        "this_sha": this_sha,
        "other_sha": other_sha,
        "current_sha": None,
        "moved_since": False,
        "other_available": False,
        "merge_base": None,
        "this_only": [],
        "other_only": [],
        "this_ahead": None,
        "other_ahead": None,
        "checked_out": False,
    }
    if root_path is None:
        return detail
    repo = root_path / repo_rel if repo_rel else root_path
    detail["repo_path"] = str(repo)
    if not (repo / ".git").exists():
        return detail
    current = _git(repo, "rev-parse", "--verify", "--quiet", ref)
    detail["current_sha"] = current
    detail["moved_since"] = bool(current) and current != this_sha
    head = _git(repo, "symbolic-ref", "--quiet", "HEAD")
    detail["checked_out"] = bool(head) and head == ref
    if not (_HEX40.match(this_sha) and _HEX40.match(other_sha)):
        return detail
    if _git(repo, "cat-file", "-e", f"{other_sha}^{{commit}}") is None:
        return detail
    detail["other_available"] = True
    detail["merge_base"] = _git(repo, "merge-base", this_sha, other_sha)
    counts = _git(
        repo, "rev-list", "--left-right", "--count", f"{this_sha}...{other_sha}"
    )
    if counts:
        left, _, right = counts.partition("\t")
        detail["this_ahead"], detail["other_ahead"] = _int(left), _int(right)
    detail["this_only"] = _commits(repo, f"{other_sha}..{this_sha}")
    detail["other_only"] = _commits(repo, f"{this_sha}..{other_sha}")
    return detail


# --- stale git locks --------------------------------------------------------


@dataclass
class StaleLockCache:
    """The last stale-lock scan, refreshed in the background.

    The daemon walks every replicated repository to answer, which takes
    minutes on a large tree, so requests never wait for it: they get the
    last result (``None`` before the first scan finishes) and start a
    refresh when it is older than ``ttl_s``.
    """

    fetch: Callable[[], dict[str, list[str]]]
    ttl_s: float = STALE_LOCK_TTL_S
    clock: Callable[[], float] = time.time
    locks: dict[str, list[str]] | None = None
    checked_at: float | None = None
    error: str | None = None
    refreshing: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _thread: threading.Thread | None = None

    def snapshot(self, *, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            due = self.checked_at is None or self.clock() - self.checked_at > self.ttl_s
            if (refresh or due) and not self.refreshing:
                self.refreshing = True
                self._thread = threading.Thread(
                    target=self._run, name="sync-stale-locks", daemon=True
                )
                self._thread.start()
            return {
                "locks": None if self.locks is None else dict(self.locks),
                "checked_at": _iso(self.checked_at),
                "refreshing": self.refreshing,
                "error": self.error,
            }

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def forget(self, lock_path: str) -> None:
        with self._lock:
            if not self.locks:
                return
            for repo in list(self.locks):
                remaining = [lock for lock in self.locks[repo] if lock != lock_path]
                if remaining:
                    self.locks[repo] = remaining
                else:
                    del self.locks[repo]

    def known(self, lock_path: str) -> bool:
        with self._lock:
            return any(lock_path in locks for locks in (self.locks or {}).values())

    def _run(self) -> None:
        try:
            result = self.fetch()
            error = None
        except Exception as exc:  # noqa: BLE001 - reported to the page
            result = None
            error = str(exc)
        with self._lock:
            if result is not None:
                self.locks = result
            self.error = error
            self.checked_at = self.clock()
            self.refreshing = False


def _fetch_stale_locks() -> dict[str, list[str]]:
    client = sync_daemon.SyncDaemonClient(timeout=STALE_LOCK_SCAN_TIMEOUT_S)
    return client.stale_locks()


STALE_LOCKS = StaleLockCache(fetch=_fetch_stale_locks)


def describe_stale_locks(
    locks: dict[str, list[str]] | None, *, now: float | None = None
) -> list[dict[str, Any]] | None:
    """One row per lock file, with its age as seen on disk now."""
    if locks is None:
        return None
    now = time.time() if now is None else now
    rows = []
    for repo, paths in sorted(locks.items()):
        for lock in paths:
            row: dict[str, Any] = {
                "repo": repo,
                "path": lock,
                "name": Path(lock).name,
                "exists": False,
                "age_s": None,
                "modified": None,
            }
            try:
                stat = Path(lock).lstat()
            except OSError:
                rows.append(row)
                continue
            row.update(
                exists=True,
                age_s=max(0, int(now - stat.st_mtime)),
                modified=_iso(stat.st_mtime),
            )
            rows.append(row)
    return rows


class LockMoveError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def lock_holders(path: Path) -> list[int] | None:
    """Process ids holding ``path`` open (``None`` when lsof cannot tell)."""
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        result = subprocess.run(
            [lsof, "-t", "--", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode not in (0, 1):
        return None
    pids = [int(line) for line in result.stdout.split() if line.strip().isdigit()]
    if result.returncode == 1 and pids:
        return None
    return pids


def _is_git_lock(path: Path) -> bool:
    """A lock file git leaves inside a git directory (index, HEAD, refs...)."""
    parts = path.parts
    if not path.name.endswith(".lock") or ".git" not in parts:
        return False
    return path.name in GIT_LOCK_NAMES or "refs" in parts[parts.index(".git") :]


def trash_dir() -> Path:
    return OPENBASE_BASE_DIR / "trash"


def move_lock_to_trash(
    lock_path: str,
    *,
    known: Callable[[str], bool],
    now: float | None = None,
    holders: Callable[[Path], list[int] | None] | None = None,
    trash: Path | None = None,
) -> dict[str, Any]:
    """Move a stale git lock into ``~/.openbase/trash`` (never deletes it).

    Refuses unless the daemon reported the lock as stale, it is a git lock
    file older than ``STALE_LOCK_AGE_S``, and no process holds it open.
    """
    if not lock_path or not os.path.isabs(lock_path):
        raise LockMoveError("Give the full path of the lock file.", 400)
    path = Path(lock_path)
    if not known(lock_path):
        raise LockMoveError(
            "Openbase Sync has not reported this file as a stale git lock.", 404
        )
    if not _is_git_lock(path):
        raise LockMoveError("This is not a git lock file.", 400)
    try:
        stat = path.lstat()
    except FileNotFoundError as exc:
        raise LockMoveError("The lock file is already gone.", 410) from exc
    if not path.is_file() or path.is_symlink():
        raise LockMoveError("This is not a regular lock file.", 400)
    now = time.time() if now is None else now
    age = now - stat.st_mtime
    if age < STALE_LOCK_AGE_S:
        raise LockMoveError(
            "This lock is less than 10 minutes old: a git command may still be "
            "running. Try again later."
        )
    pids = (holders or lock_holders)(path)
    if pids is None:
        raise LockMoveError(
            "Could not check whether a process holds this lock (lsof failed); "
            "left it in place."
        )
    if pids:
        raise LockMoveError(
            "A running process holds this lock (pid "
            + ", ".join(str(pid) for pid in pids)
            + "); left it in place."
        )
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = (trash or trash_dir()) / "git-locks" / stamp
    relative = str(path).lstrip("/").replace("/", "__")
    destination = base / relative
    counter = 1
    while destination.exists():
        destination = base / f"{relative}.{counter}"
        counter += 1
    base.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(destination))
    return {"moved": True, "from": str(path), "to": str(destination)}
