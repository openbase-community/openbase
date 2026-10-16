"""Run Super Agents operations in django-cli instead of the calling process.

A Super Agent turn on the Claude Code backend is an asyncio task inside the
process that owns the ``ClaudeAgentSdkClient``. When a voice Dispatcher starts
agents, that process was the LiveKit job: the room closing after a hangup
exited the job and took every unfinished agent turn with it (Maritime
workspace 28, 2026-10-16: Casper's turn died "interrupted: owning process
exited mid-turn"). Hanging up must never stop agents, so the voice job's
in-process ``super_agents`` MCP server forwards its operations here, to the
long-lived django-cli service that already owns typed turns.

Both sides share this module: :class:`RemoteSuperAgentsClient` is the
forwarding client (it satisfies ``super_agents.backend_clients.SuperAgentsClient``)
and :func:`invoke_super_agents_method` runs one forwarded call against the
process-local client inside django-cli. The wire format is JSON with
dataclass arguments tagged by type.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import Any

from super_agents.app_models import LabelQueryInput, QueueCancelInput
from super_agents.backend_clients import SuperAgentsClient

INVOKE_PATH = "/api/super-agents/invoke/"
REQUEST_TIMEOUT_SECONDS = 60.0

_DATACLASS_TYPES: dict[str, type] = {
    "LabelQueryInput": LabelQueryInput,
    "QueueCancelInput": QueueCancelInput,
}
_TYPE_TAG = "__super_agents_type__"
_FIELDS_KEY = "fields"

# Every public coroutine of the generic client contract, and nothing else:
# a forwarded name outside this set is rejected before any lookup happens.
REMOTE_METHODS: frozenset[str] = frozenset(
    name
    for name, member in inspect.getmembers(SuperAgentsClient)
    if not name.startswith("_") and inspect.iscoroutinefunction(member)
)
READ_ONLY_METHODS: frozenset[str] = frozenset(
    {
        "status",
        "read_by_label",
        "sessions",
        "tags",
        "active",
        "recent",
        "compact_status",
        "resolve_label",
        "progress_by_label",
    }
)


class RemoteSuperAgentsError(RuntimeError):
    """django-cli ran the operation and reported a failure."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


class RemoteSuperAgentsUnavailable(RuntimeError):
    """django-cli could not be reached, so no operation ran."""


def encode_argument(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            _TYPE_TAG: type(value).__name__,
            _FIELDS_KEY: {
                key: encode_argument(item)
                for key, item in dataclasses.asdict(value).items()
                if item is not None
            },
        }
    if isinstance(value, dict):
        return {str(key): encode_argument(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_argument(item) for item in value]
    return value


def decode_argument(value: Any) -> Any:
    if isinstance(value, dict):
        tag = value.get(_TYPE_TAG)
        if isinstance(tag, str):
            dataclass_type = _DATACLASS_TYPES.get(tag)
            if dataclass_type is None:
                raise ValueError(f"Unsupported Super Agents argument type {tag!r}.")
            fields = value.get(_FIELDS_KEY) or {}
            if not isinstance(fields, dict):
                raise ValueError(f"Malformed {tag} argument.")
            return dataclass_type(
                **{key: decode_argument(item) for key, item in fields.items()}
            )
        return {key: decode_argument(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_argument(item) for item in value]
    return value


def encode_request(
    backend: str | None, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    return {
        "backend": backend,
        "method": method,
        "args": [encode_argument(arg) for arg in args],
        "kwargs": {key: encode_argument(value) for key, value in kwargs.items()},
    }


async def invoke_super_agents_method(client: Any, payload: dict[str, Any]) -> Any:
    """Run one forwarded operation on ``client``; raise ValueError on bad input."""
    method = payload.get("method")
    if not isinstance(method, str) or method not in REMOTE_METHODS:
        raise ValueError(f"Unsupported Super Agents method {method!r}.")
    requested_backend = payload.get("backend")
    local_backend = getattr(client, "backend", None)
    if requested_backend and local_backend and requested_backend != local_backend:
        raise ValueError(
            f"This server's Super Agents client runs backend {local_backend!r}, "
            f"not {requested_backend!r}."
        )
    raw_args = payload.get("args") or []
    raw_kwargs = payload.get("kwargs") or {}
    if not isinstance(raw_args, list) or not isinstance(raw_kwargs, dict):
        raise ValueError("Malformed Super Agents invocation.")
    args = [decode_argument(arg) for arg in raw_args]
    kwargs = {str(key): decode_argument(value) for key, value in raw_kwargs.items()}
    return await getattr(client, method)(*args, **kwargs)


class RemoteSuperAgentsClient:
    """A ``SuperAgentsClient`` whose operations run inside django-cli.

    ``local`` is the process-local client for the same backend and store. It
    answers the few non-coroutine lookups the MCP layer makes on a client
    (``backend``, ``store``, ``_resolve_session`` for delegation receipts);
    every operation of the contract is forwarded, so no turn is ever owned
    by the caller's process.
    """

    def __init__(
        self,
        local: Any,
        *,
        base_url: str | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        transport: Any = None,
    ):
        self._local = local
        self._base_url = base_url
        self._timeout = timeout
        self._transport = transport
        self.backend = getattr(local, "backend", None)
        self.store = getattr(local, "store", None)

    def _resolve_session(self, query: Any) -> Any:
        return self._local._resolve_session(query)

    def __getattr__(self, name: str) -> Any:
        # Only reached for names not set on the instance.
        if name in REMOTE_METHODS:

            async def call(*args: Any, **kwargs: Any) -> Any:
                return await self._invoke(name, args, kwargs)

            call.__name__ = name
            return call
        raise AttributeError(name)

    async def _invoke(
        self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        import httpx

        from openbase_coder_cli.cli.local_server import (
            LocalInstallationAuth,
            local_server_url,
        )

        base_url = self._base_url or local_server_url()
        payload = encode_request(self.backend, method, args, kwargs)
        try:
            async with httpx.AsyncClient(
                base_url=base_url,
                auth=LocalInstallationAuth(),
                timeout=self._timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as http:
                response = await http.post(INVOKE_PATH, json=payload)
        except httpx.HTTPError as exc:
            raise RemoteSuperAgentsUnavailable(
                "The Openbase local API is not reachable, so the Super Agents "
                f"operation {method} did not run: {exc}"
            ) from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if (
            response.status_code >= 400
            or not isinstance(body, dict)
            or "result" not in body
        ):
            if not isinstance(body, dict):
                body = {}
            message = str(body.get("error") or f"HTTP {response.status_code}")
            code = body.get("code")
            if body.get("type") == "ValueError":
                raise ValueError(message)
            raise RemoteSuperAgentsError(
                message, code=code if isinstance(code, str) else None
            )
        return body["result"]
