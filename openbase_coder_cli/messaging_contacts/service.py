"""The contact review: who needs a category, assignments, and lookups for the
approval gate. Reads the sources through their CLIs and caches the result."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from openbase_coder_cli.messaging_contacts import store as state
from openbase_coder_cli.messaging_contacts.identity import (
    Identity,
    Person,
    merge_people,
    person_key,
)
from openbase_coder_cli.messaging_contacts.sources import (
    MessagingSourceError,
    Source,
    configured_sources,
    scan_source,
)

ACTIVITY_WINDOW_DAYS = 365
INDEX_TTL = timedelta(minutes=15)
SKIP_HOURS = 24
# Approval requests name the skill; map that to the source kind they belong to.
SKILL_KINDS = {
    "whatsapp-cli": "whatsapp",
    "whatsapp": "whatsapp",
    "imessage": "imessage",
    "imessage-cli": "imessage",
}
UNCATEGORIZED_LABEL = "Uncategorized"

_CATEGORY_ID_RE = re.compile(r"[^a-z0-9]+")


def sources() -> list[Source]:
    return configured_sources(state.sources_config(state.read_store()))


def refresh_index() -> dict[str, Any]:
    """Re-read every source and cache the identities; errors are per source."""
    identities: list[Identity] = []
    errors: list[dict[str, str]] = []
    for source in sources():
        try:
            identities.extend(scan_source(source))
        except MessagingSourceError as exc:
            errors.append({"source_id": source.id, "message": str(exc)})
    payload = {
        "refreshed_at": state.iso_now(),
        "identities": [identity.to_json() for identity in identities],
        "errors": errors,
    }
    state.update_store(lambda store: store.__setitem__("index", payload))
    return payload


def snapshot(
    *,
    refresh: bool = False,
    cache_only: bool = False,
    window_days: int = ACTIVITY_WINDOW_DAYS,
) -> dict[str, Any]:
    """Everything the review GUI needs: people, categories, progress, sources.

    A stale cache is re-scanned unless ``cache_only`` is set (badges poll
    often and must not reach out to a remote host every few seconds).
    """
    store = state.read_store()
    index = state.index(store)
    if refresh or (not cache_only and _index_stale(index)):
        index = refresh_index()
        store = state.read_store()
    people = _active_people(index, window_days)
    assignments = state.people_state(store)
    category_by_id = {c["id"]: c for c in state.categories(store)}
    now_iso = state.iso_now()
    rows = [
        _person_row(person, assignments.get(person.key), category_by_id, now_iso)
        for person in people
    ]
    categorized = sum(1 for row in rows if row["category"])
    skipped = sum(1 for row in rows if not row["category"] and row["skipped"])
    queue = [row for row in rows if not row["category"] and not row["skipped"]]
    return {
        "people": rows,
        "queue": [row["key"] for row in queue],
        "categories": list(category_by_id.values()),
        "sources": [source.to_json() for source in sources()],
        "progress": {
            "total": len(rows),
            "categorized": categorized,
            "pending": len(rows) - categorized,
            "skipped": skipped,
        },
        "window_days": window_days,
        "refreshed_at": index.get("refreshed_at"),
        "errors": index.get("errors") or [],
    }


def summary() -> dict[str, Any]:
    """Counts only, for badges. Serves the cache, except that the very first
    call builds the index so the review prompt can appear before anyone has
    opened the Contacts page."""
    never_scanned = not state.index(state.read_store())
    snap = snapshot(refresh=never_scanned, cache_only=not never_scanned)
    return {
        "progress": snap["progress"],
        "sources": snap["sources"],
        "refreshed_at": snap["refreshed_at"],
        "errors": snap["errors"],
    }


def assign(keys: list[str], category_id: str | None) -> dict[str, Any]:
    """Set (or clear, with None) the category for each person key."""
    normalized_keys = _require_keys(keys)

    def mutate(store: dict[str, Any]) -> None:
        if category_id is not None and category_id not in {
            c["id"] for c in state.categories(store)
        }:
            raise ValueError(f"unknown category: {category_id}")
        people = store.setdefault("people", {})
        if not isinstance(people, dict):
            people = store["people"] = {}
        for key in normalized_keys:
            entry = people.get(key) if isinstance(people.get(key), dict) else {}
            people[key] = {
                **entry,
                "category": category_id,
                "updated_at": state.iso_now(),
                "skipped_until": None,
            }

    state.update_store(mutate)
    return {"keys": normalized_keys, "category": category_id}


def skip(keys: list[str], hours: float = SKIP_HOURS) -> dict[str, Any]:
    """Defer people for a while; they re-enter the queue afterwards."""
    normalized_keys = _require_keys(keys)
    until = (state.utc_now() + timedelta(hours=max(hours, 0))).isoformat()

    def mutate(store: dict[str, Any]) -> None:
        people = store.setdefault("people", {})
        if not isinstance(people, dict):
            people = store["people"] = {}
        for key in normalized_keys:
            entry = people.get(key) if isinstance(people.get(key), dict) else {}
            people[key] = {**entry, "skipped_until": until}

    state.update_store(mutate)
    return {"keys": normalized_keys, "skipped_until": until}


def list_categories() -> list[dict[str, Any]]:
    return state.categories(state.read_store())


def save_category(
    label: str, policy: str, category_id: str | None = None
) -> dict[str, Any]:
    """Create a category, or update the label/policy of an existing one."""
    clean_label = " ".join(str(label or "").split())
    if not clean_label:
        raise ValueError("category label is required")
    if policy not in state.CATEGORY_POLICIES:
        raise ValueError(f"policy must be one of: {', '.join(state.CATEGORY_POLICIES)}")
    result: dict[str, Any] = {}

    def mutate(store: dict[str, Any]) -> None:
        items = state.categories(store)
        if category_id:
            match = next((item for item in items if item["id"] == category_id), None)
            if match is None:
                raise ValueError(f"unknown category: {category_id}")
            match.update({"label": clean_label, "policy": policy})
            result.update(match)
        else:
            new_id = _unique_id(clean_label, {item["id"] for item in items})
            result.update({"id": new_id, "label": clean_label, "policy": policy})
            items.append(dict(result))
        store["categories"] = items

    state.update_store(mutate)
    return result


def delete_category(category_id: str) -> dict[str, Any]:
    """Remove a category; people assigned to it go back to the queue."""
    unassigned = 0

    def mutate(store: dict[str, Any]) -> None:
        nonlocal unassigned
        items = state.categories(store)
        if not any(item["id"] == category_id for item in items):
            raise ValueError(f"unknown category: {category_id}")
        store["categories"] = [item for item in items if item["id"] != category_id]
        people = store.get("people")
        if isinstance(people, dict):
            for entry in people.values():
                if isinstance(entry, dict) and entry.get("category") == category_id:
                    entry["category"] = None
                    unassigned += 1

    state.update_store(mutate)
    return {"id": category_id, "unassigned": unassigned}


def lookup_contact(kind: str, contact_id: str) -> dict[str, Any] | None:
    """Category state for one source contact, from the cached index only."""
    store = state.read_store()
    identities = _identities(state.index(store))
    match = next(
        (i for i in identities if i.kind == kind and i.contact_id == contact_id), None
    )
    if match is None:
        return None
    key = person_key(match)
    person = Person(key=key, identities=[i for i in identities if person_key(i) == key])
    category_by_id = {c["id"]: c for c in state.categories(store)}
    return _person_row(
        person, state.people_state(store).get(key), category_by_id, state.iso_now()
    )


def approval_gate(skill: str, details: dict[str, Any]) -> dict[str, Any] | None:
    """How the approval queue should treat a skill request about a contact.

    Returns ``{"category": label, "policy": "ask"|"block"}`` when the request
    names a contact from a known messaging skill, else None. Uncategorized
    contacts keep the normal flow but are labelled so the reviewer notices.
    """
    kind = SKILL_KINDS.get(str(skill or "").strip().lower())
    contact_id = (
        str(details.get("contact_id") or "").strip()
        if isinstance(details, dict)
        else ""
    )
    if not kind or not contact_id:
        return None
    row = lookup_contact(kind, contact_id)
    if row is None:
        return None
    category = row.get("category")
    if not category:
        return {
            "category": UNCATEGORIZED_LABEL,
            "policy": "ask",
            "person_key": row["key"],
        }
    return {
        "category": category["label"],
        "policy": category.get("policy", "ask"),
        "person_key": row["key"],
    }


def _active_people(index: dict[str, Any], window_days: int) -> list[Person]:
    cutoff = (state.utc_now() - timedelta(days=window_days)).isoformat()
    recent = [
        i
        for i in _identities(index)
        if i.last_message_at and i.last_message_at >= cutoff
    ]
    return merge_people(recent)


def _identities(index: dict[str, Any]) -> list[Identity]:
    raw = index.get("identities")
    if not isinstance(raw, list):
        return []
    return [Identity.from_json(item) for item in raw if isinstance(item, dict)]


def _index_stale(index: dict[str, Any]) -> bool:
    refreshed_at = index.get("refreshed_at")
    if not isinstance(refreshed_at, str):
        return True
    from super_agents.app_time import parse_iso_ms

    refreshed_ms = parse_iso_ms(refreshed_at)
    return (
        refreshed_ms <= 0
        or state.utc_now().timestamp() * 1000 - refreshed_ms
        > INDEX_TTL.total_seconds() * 1000
    )


def _person_row(
    person: Person,
    entry: dict[str, Any] | None,
    category_by_id: dict[str, dict[str, Any]],
    now_iso: str,
) -> dict[str, Any]:
    entry = entry or {}
    category = category_by_id.get(str(entry.get("category") or ""))
    skipped_until = entry.get("skipped_until")
    skipped = isinstance(skipped_until, str) and skipped_until > now_iso
    return {
        **person.to_json(),
        "category": category,
        "updated_at": entry.get("updated_at"),
        "skipped": skipped,
        "skipped_until": skipped_until if skipped else None,
    }


def _require_keys(keys: list[str]) -> list[str]:
    normalized = [str(key).strip() for key in keys if str(key or "").strip()]
    if not normalized:
        raise ValueError("at least one person key is required")
    return list(dict.fromkeys(normalized))


def _unique_id(label: str, taken: set[str]) -> str:
    base = _CATEGORY_ID_RE.sub("-", label.lower()).strip("-") or "category"
    candidate = base
    counter = 2
    while candidate in taken:
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate
