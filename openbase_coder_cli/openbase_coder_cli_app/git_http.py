"""Read-only git smart-HTTP endpoints for the code-sync reconciler.

Wraps ``git http-backend`` (CGI) for ``GET .../info/refs`` and
``POST .../git-upload-pack`` only — receive-pack is never routed, so peers
can fetch from this machine but can never push to it. Repositories are
addressed as ``/api/sync/git/<folder_id>/<repo_relpath>/...`` and must live
inside a configured synced folder; path traversal is rejected.

Auth matches the rest of the local API: a cloud JWT pinned to the machine
owner (the reconciler sends it via git's ``http.extraHeader``).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from django.http import HttpResponse, JsonResponse
from rest_framework import exceptions

from openbase_coder_cli.config.authentication import JWTAuthentication
from openbase_coder_cli.sync_config import folder_for_id

INFO_REFS_SUFFIX = "info/refs"
UPLOAD_PACK_SUFFIX = "git-upload-pack"
UPLOAD_PACK_SERVICE = "git-upload-pack"
GIT_TIMEOUT_SECONDS = 120


def _authenticate(request) -> JsonResponse | None:
    """Owner-pinned JWT auth (same class as the DRF views). None when ok."""
    try:
        result = JWTAuthentication().authenticate(request)
    except exceptions.AuthenticationFailed as exc:
        return JsonResponse({"detail": str(exc)}, status=401)
    if result is None:
        return JsonResponse({"detail": "Authentication required."}, status=401)
    return None


def _resolve_repo_dir(folder_id: str, repo_relpath: str) -> Path | None:
    """Filesystem repo dir for a folder/repo pair, or None when invalid."""
    folder = folder_for_id(folder_id)
    if folder is None:
        return None
    folder_root = folder.absolute_path().resolve()
    candidate = (folder_root / repo_relpath).resolve() if repo_relpath else folder_root
    try:
        candidate.relative_to(folder_root)
    except ValueError:
        return None
    if not (candidate / ".git").exists():
        return None
    return candidate


def _absolute_git_dir(repo_dir: Path) -> Path | None:
    """The repo's real git dir (follows worktree ``.git`` pointer files)."""
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--absolute-git-dir"],
        capture_output=True,
        text=True,
        check=False,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return Path(value) if value else None


def _split_repo_request(subpath: str) -> tuple[str, str] | None:
    """Split ``<repo_relpath>/<endpoint>`` into (repo_relpath, endpoint)."""
    normalized = subpath.strip("/")
    for suffix in (INFO_REFS_SUFFIX, UPLOAD_PACK_SUFFIX):
        if normalized == suffix:
            return "", suffix
        if normalized.endswith("/" + suffix):
            return normalized[: -(len(suffix) + 1)], suffix
    return None


def git_http_backend(request, folder_id: str, subpath: str):
    """Serve one git smart-HTTP request via the git http-backend CGI."""
    auth_error = _authenticate(request)
    if auth_error is not None:
        return auth_error

    split = _split_repo_request(subpath)
    if split is None:
        return JsonResponse({"detail": "Unsupported git endpoint."}, status=404)
    repo_relpath, endpoint = split

    if ".." in Path(repo_relpath).parts or Path(repo_relpath).is_absolute():
        return JsonResponse({"detail": "Invalid repository path."}, status=400)

    if endpoint == INFO_REFS_SUFFIX:
        if request.method != "GET":
            return JsonResponse({"detail": "Method not allowed."}, status=405)
        if request.GET.get("service") != UPLOAD_PACK_SERVICE:
            # Read-only: git-receive-pack (push) is disabled by design.
            return JsonResponse(
                {"detail": "Only git-upload-pack is served."}, status=403
            )
    else:
        if request.method != "POST":
            return JsonResponse({"detail": "Method not allowed."}, status=405)

    repo_dir = _resolve_repo_dir(folder_id, repo_relpath)
    if repo_dir is None:
        return JsonResponse({"detail": "Unknown repository."}, status=404)
    git_dir = _absolute_git_dir(repo_dir)
    if git_dir is None:
        return JsonResponse({"detail": "Unknown repository."}, status=404)

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_PROJECT_ROOT": str(git_dir),
        "GIT_HTTP_EXPORT_ALL": "1",
        "PATH_INFO": f"/{endpoint}",
        "REQUEST_METHOD": request.method,
        "QUERY_STRING": request.META.get("QUERY_STRING", ""),
        "CONTENT_TYPE": request.META.get("CONTENT_TYPE", ""),
        "CONTENT_LENGTH": str(len(request.body)),
        "REMOTE_ADDR": request.META.get("REMOTE_ADDR", ""),
        "REMOTE_USER": "openbase-coder",
    }
    # git clients gzip large upload-pack request bodies; the CGI needs the
    # encoding to inflate them.
    content_encoding = request.META.get("HTTP_CONTENT_ENCODING", "")
    if content_encoding:
        env["HTTP_CONTENT_ENCODING"] = content_encoding
    try:
        result = subprocess.run(
            ["git", "http-backend"],
            input=request.body,
            env=env,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return JsonResponse({"detail": f"git http-backend failed: {exc}"}, status=502)

    return _cgi_to_response(result.stdout)


def _cgi_to_response(cgi_output: bytes) -> HttpResponse:
    """Convert raw CGI output (headers, blank line, body) to a response."""
    header_blob, separator, body = cgi_output.partition(b"\r\n\r\n")
    if not separator:
        header_blob, separator, body = cgi_output.partition(b"\n\n")
    if not separator:
        return JsonResponse(
            {"detail": "git http-backend returned no CGI headers."}, status=502
        )

    status_code = 200
    content_type = "application/octet-stream"
    extra_headers: dict[str, str] = {}
    for raw_line in header_blob.split(b"\n"):
        line = raw_line.strip().decode("latin-1")
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        name = name.strip().lower()
        value = value.strip()
        if name == "status":
            try:
                status_code = int(value.split()[0])
            except (ValueError, IndexError):
                pass
        elif name == "content-type":
            content_type = value
        elif name not in ("content-length",):
            extra_headers[name] = value

    response = HttpResponse(body, content_type=content_type, status=status_code)
    for name, value in extra_headers.items():
        response[name] = value
    return response
