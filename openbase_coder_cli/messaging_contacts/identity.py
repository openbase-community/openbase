"""Normalize contact identities from each messaging source and merge people."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

WHATSAPP_PHONE_SUFFIX = "@s.whatsapp.net"
WHATSAPP_GROUP_SUFFIX = "@g.us"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class Identity:
    """One contact row from one source, reduced to what the review needs."""

    source_id: str
    kind: str
    contact_id: str
    display_name: str | None
    phone: str | None
    email: str | None
    is_group: bool
    last_message_at: str | None
    last_inbound_at: str | None
    last_outbound_at: str | None
    read_allowed: bool
    send_allowed: bool
    mergeable: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "kind": self.kind,
            "contact_id": self.contact_id,
            "display_name": self.display_name,
            "phone": self.phone,
            "email": self.email,
            "is_group": self.is_group,
            "last_message_at": self.last_message_at,
            "last_inbound_at": self.last_inbound_at,
            "last_outbound_at": self.last_outbound_at,
            "read_allowed": self.read_allowed,
            "send_allowed": self.send_allowed,
            "mergeable": self.mergeable,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Identity:
        return cls(
            source_id=str(payload.get("source_id") or ""),
            kind=str(payload.get("kind") or ""),
            contact_id=str(payload.get("contact_id") or ""),
            display_name=_text_or_none(payload.get("display_name")),
            phone=_text_or_none(payload.get("phone")),
            email=_text_or_none(payload.get("email")),
            is_group=bool(payload.get("is_group")),
            last_message_at=_text_or_none(payload.get("last_message_at")),
            last_inbound_at=_text_or_none(payload.get("last_inbound_at")),
            last_outbound_at=_text_or_none(payload.get("last_outbound_at")),
            read_allowed=bool(payload.get("read_allowed")),
            send_allowed=bool(payload.get("send_allowed")),
            mergeable=bool(payload.get("mergeable", True)),
        )


@dataclass
class Person:
    """Identities that clearly belong to one person (or one group chat)."""

    key: str
    identities: list[Identity] = field(default_factory=list)

    @property
    def display_name(self) -> str | None:
        for identity in self.identities:
            if identity.display_name:
                return identity.display_name
        return None

    @property
    def is_group(self) -> bool:
        return any(identity.is_group for identity in self.identities)

    @property
    def last_message_at(self) -> str | None:
        stamps = [i.last_message_at for i in self.identities if i.last_message_at]
        return max(stamps) if stamps else None

    @property
    def kinds(self) -> list[str]:
        return sorted({identity.kind for identity in self.identities})

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "is_group": self.is_group,
            "last_message_at": self.last_message_at,
            "kinds": self.kinds,
            "read_allowed": any(i.read_allowed for i in self.identities),
            "send_allowed": any(i.send_allowed for i in self.identities),
            "identities": [identity.to_json() for identity in self.identities],
        }


def normalize_phone(value: Any) -> str | None:
    """Reduce a phone-like value to its digits; None when it is not a phone."""
    text = str(value or "").strip()
    if not text or ("@" in text and not text.endswith(WHATSAPP_PHONE_SUFFIX)):
        return None
    if text.endswith(WHATSAPP_PHONE_SUFFIX):
        text = text[: -len(WHATSAPP_PHONE_SUFFIX)]
    digits = re.sub(r"\D", "", text)
    # Anything shorter is not a dialable number; it is a short code or junk.
    return digits if len(digits) >= 7 else None


def normalize_email(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if _EMAIL_RE.match(text) else None


def whatsapp_identity(
    row: dict[str, Any], *, source_id: str, mergeable: bool
) -> Identity | None:
    contact_id = _text_or_none(row.get("id"))
    if not contact_id:
        return None
    is_group = bool(row.get("is_group")) or contact_id.endswith(WHATSAPP_GROUP_SUFFIX)
    phone = (
        None
        if is_group
        else (normalize_phone(row.get("phone_number")) or normalize_phone(contact_id))
    )
    return Identity(
        source_id=source_id,
        kind="whatsapp",
        contact_id=contact_id,
        display_name=_first_text(
            row.get("contact_name"),
            row.get("display_name"),
            row.get("verified_name"),
            row.get("push_name"),
        ),
        phone=phone,
        email=None,
        is_group=is_group,
        last_message_at=_latest(
            row.get("last_message_at"),
            row.get("last_inbound_message_at"),
            row.get("last_outbound_message_at"),
        ),
        last_inbound_at=_text_or_none(row.get("last_inbound_message_at")),
        last_outbound_at=_text_or_none(row.get("last_outbound_message_at")),
        read_allowed=bool(row.get("read_allowed")),
        send_allowed=bool(row.get("send_allowed")),
        mergeable=mergeable,
    )


def imessage_identity(
    row: dict[str, Any], *, source_id: str, mergeable: bool
) -> Identity | None:
    contact_id = _text_or_none(row.get("id"))
    if not contact_id:
        return None
    is_group = bool(row.get("is_group")) or (
        row.get("kind") == "chat" and int(row.get("participant_count") or 0) > 1
    )
    handle = row.get("normalized_id") or row.get("chat_identifier") or contact_id
    return Identity(
        source_id=source_id,
        kind="imessage",
        contact_id=contact_id,
        display_name=_text_or_none(row.get("display_name")),
        phone=None if is_group else normalize_phone(handle),
        email=None if is_group else normalize_email(handle),
        is_group=is_group,
        last_message_at=_latest(
            row.get("last_message_at"),
            row.get("last_inbound_message_at"),
            row.get("last_outbound_message_at"),
        ),
        last_inbound_at=_text_or_none(row.get("last_inbound_message_at")),
        last_outbound_at=_text_or_none(row.get("last_outbound_message_at")),
        read_allowed=bool(row.get("read_allowed")),
        send_allowed=bool(row.get("send_allowed")),
        mergeable=mergeable,
    )


def person_key(identity: Identity) -> str:
    """Stable key shared by every identity that clearly is the same person.

    Phones and emails merge across sources; groups and identities from a
    source that opted out of merging stay on their own key.
    """
    if identity.mergeable and not identity.is_group:
        if identity.phone:
            return f"phone:{identity.phone}"
        if identity.email:
            return f"email:{identity.email}"
    return f"{identity.source_id}:{identity.contact_id}"


def merge_people(identities: list[Identity]) -> list[Person]:
    people: dict[str, Person] = {}
    for identity in identities:
        key = person_key(identity)
        person = people.get(key)
        if person is None:
            person = people[key] = Person(key=key)
        person.identities.append(identity)
    return sorted(
        people.values(), key=lambda p: (p.last_message_at or "", p.key), reverse=True
    )


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = _text_or_none(value)
        if text:
            return text
    return None


def _latest(*values: Any) -> str | None:
    stamps = [text for text in (_text_or_none(value) for value in values) if text]
    return max(stamps) if stamps else None


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
