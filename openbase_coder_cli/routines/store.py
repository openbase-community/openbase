from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from openbase_coder_cli.paths import ROUTINES_JSON_PATH

from .models import RoutineRecord, routine_record_from_json

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

ROUTINES_SCHEMA_VERSION = 1
DEFAULT_LEGACY_STATE_PATH = Path.home() / ".super-agents" / "state.json"
T = TypeVar("T")


class RoutineStateError(ValueError):
    pass


@dataclass(slots=True)
class RoutineState:
    schema_version: int = ROUTINES_SCHEMA_VERSION
    routines: dict[str, RoutineRecord] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": ROUTINES_SCHEMA_VERSION,
            "routines": {
                name: routine.to_json() for name, routine in self.routines.items()
            },
        }


class RoutineStore:
    def __init__(
        self,
        path: Path | None = None,
        *,
        legacy_state_path: Path | None = None,
    ) -> None:
        self.path = path or ROUTINES_JSON_PATH
        self.legacy_state_path = legacy_state_path or DEFAULT_LEGACY_STATE_PATH

    def read(self) -> RoutineState:
        with _file_lock(self.path):
            self._migrate_legacy_state_locked()
            return _read_state(self.path)

    def update(self, callback: Callable[[RoutineState], T]) -> T:
        with _file_lock(self.path):
            self._migrate_legacy_state_locked()
            state = _read_state(self.path)
            result = callback(state)
            _write_json_atomic(self.path, state.to_json())
            return result

    def _migrate_legacy_state_locked(self) -> None:
        if self.path.exists():
            return
        with _file_lock(self.legacy_state_path):
            legacy = _read_json_object(self.legacy_state_path, missing_ok=True)
            raw_routines = legacy.get("routines")
            routines = _parse_routines(raw_routines)
            _write_json_atomic(
                self.path,
                RoutineState(routines=routines).to_json(),
            )
            if "routines" in legacy:
                legacy.pop("routines", None)
                _write_json_atomic(self.legacy_state_path, legacy)


def _read_state(path: Path) -> RoutineState:
    payload = _read_json_object(path)
    raw_version = payload.get("schema_version", ROUTINES_SCHEMA_VERSION)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise RoutineStateError(f"{path.name} has an invalid schema_version.")
    if raw_version > ROUTINES_SCHEMA_VERSION:
        raise RoutineStateError(
            f"{path.name} schema {raw_version} was written by a newer Openbase Coder; update the CLI."
        )
    return RoutineState(
        schema_version=ROUTINES_SCHEMA_VERSION,
        routines=_parse_routines(payload.get("routines")),
    )


def _parse_routines(value: Any) -> dict[str, RoutineRecord]:
    if not isinstance(value, dict):
        return {}
    routines: dict[str, RoutineRecord] = {}
    for name, raw in value.items():
        routine = routine_record_from_json(
            raw, name_fallback=str(name), require_prompt_time=True
        )
        if routine is not None:
            routines[routine.name] = routine
    return routines


def _read_json_object(path: Path, *, missing_ok: bool = False) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise RoutineStateError(f"{path.name} does not exist.") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutineStateError(f"Could not read {path.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RoutineStateError(f"{path.name} must contain a JSON object.")
    return payload


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as tmp:
            tmp.write(body)
            tmp_name = tmp.name
        os.replace(tmp_name, path)
    except Exception:
        if tmp_name:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
        raise


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
