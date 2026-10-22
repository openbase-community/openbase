"""Contact categorization for the messaging skills. All data here is synthetic."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
import pytest  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli.messaging_contacts import service, store  # noqa: E402
from openbase_coder_cli.messaging_contacts.identity import (  # noqa: E402
    imessage_identity,
    merge_people,
    normalize_phone,
    person_key,
    whatsapp_identity,
)
from openbase_coder_cli.messaging_contacts.sources import (  # noqa: E402
    MessagingSourceError,
    Source,
    detect_sources,
    scan_source,
    source_from_config,
)
from openbase_coder_cli.openbase_coder_cli_app import approvals  # noqa: E402
from openbase_coder_cli.openbase_coder_cli_app import (  # noqa: E402
    messaging_contacts_api as api,
)

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
RECENT = (NOW - timedelta(days=10)).isoformat()
OLD = (NOW - timedelta(days=400)).isoformat()

WA_ROWS = [
    {
        "id": "15550000001@s.whatsapp.net",
        "contact_name": "Ada Example",
        "phone_number": "15550000001",
        "is_group": 0,
        "last_message_at": RECENT,
        "read_allowed": 1,
        "send_allowed": 0,
    },
    {
        "id": "123456789@lid",
        "push_name": "Ada",
        "phone_number": "+1 555 000 0001",
        "is_group": 0,
        "last_inbound_message_at": RECENT,
    },
    {
        "id": "15550000002@s.whatsapp.net",
        "display_name": "Old Timer",
        "is_group": 0,
        "last_message_at": OLD,
    },
    {
        "id": "99999@g.us",
        "display_name": "Example Group",
        "is_group": 1,
        "last_message_at": RECENT,
    },
]
IM_HANDLES = [
    {
        "id": "handle:+15550000001",
        "kind": "handle",
        "display_name": "Ada E.",
        "normalized_id": "+15550000001",
        "last_message_at": RECENT,
    },
    {
        "id": "handle:bob@example.test",
        "kind": "handle",
        "display_name": "Bob Example",
        "normalized_id": "bob@example.test",
        "last_outbound_message_at": RECENT,
    },
]
IM_CHATS = [
    {
        "id": "chat:42",
        "kind": "chat",
        "display_name": "Family chat",
        "is_group": 1,
        "participant_count": 3,
        "last_message_at": RECENT,
    },
]


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        store, "store_path", lambda: tmp_path / "messaging-contacts.json"
    )


def fake_runner(source: Source, args: list[str]):
    command = args[0]
    offset = int(args[args.index("--offset") + 1])
    if offset:
        return []
    if source.kind == "whatsapp":
        return {"contacts": WA_ROWS} if command == "contacts" else []
    return IM_HANDLES if command == "contacts" else IM_CHATS


def fake_scan(source: Source):
    return scan_source(source, runner=fake_runner)


def _fixed_clock(monkeypatch, moment=NOW):
    monkeypatch.setattr(store, "utc_now", lambda: moment)


def _two_sources(monkeypatch):
    sources = [
        Source(id="whatsapp", kind="whatsapp", command=("whatsapp-local",)),
        Source(id="imessage", kind="imessage", command=("imessage-local",)),
    ]
    monkeypatch.setattr(service, "sources", lambda: sources)
    monkeypatch.setattr(service, "scan_source", fake_scan)


def test_phone_normalization_handles_jids_and_formatting():
    assert normalize_phone("15550000001@s.whatsapp.net") == "15550000001"
    assert normalize_phone("+1 (555) 000-0001") == "15550000001"
    assert normalize_phone("bob@example.test") is None
    assert normalize_phone("12345") is None


def test_identities_merge_across_sources_by_phone_but_not_groups():
    identities = [
        whatsapp_identity(row, source_id="whatsapp", mergeable=True) for row in WA_ROWS
    ]
    identities += [
        imessage_identity(row, source_id="imessage", mergeable=True)
        for row in IM_HANDLES + IM_CHATS
    ]
    people = {
        person.key: person for person in merge_people([i for i in identities if i])
    }
    ada = people["phone:15550000001"]
    assert sorted(i.source_id for i in ada.identities) == [
        "imessage",
        "whatsapp",
        "whatsapp",
    ]
    assert ada.display_name == "Ada Example"
    assert ada.kinds == ["imessage", "whatsapp"]
    assert "email:bob@example.test" in people
    assert "whatsapp:99999@g.us" in people and people["whatsapp:99999@g.us"].is_group
    assert "imessage:chat:42" in people and people["imessage:chat:42"].is_group


def test_sources_that_opt_out_of_merging_keep_their_own_keys():
    row = whatsapp_identity(WA_ROWS[0], source_id="whatsapp-second", mergeable=False)
    assert person_key(row) == "whatsapp-second:15550000001@s.whatsapp.net"


def test_source_config_and_detection(monkeypatch):
    source = source_from_config(
        {
            "kind": "whatsapp",
            "command": "ssh-wrapped whatsapp-local",
            "ssh_target": "archive-host",
            "merge": False,
            "id": "wa2",
        }
    )
    assert (
        source.command == ("ssh-wrapped", "whatsapp-local")
        and source.ssh_target == "archive-host"
        and not source.merge
    )
    monkeypatch.setattr(
        "openbase_coder_cli.messaging_contacts.sources.shutil.which",
        lambda name: (
            "/usr/local/bin/imessage-local" if name == "imessage-local" else None
        ),
    )
    detected = detect_sources({"WHATSAPP_CLI_SSH_TARGET": "archive-host"})
    assert [(s.kind, s.ssh_target) for s in detected] == [
        ("whatsapp", "archive-host"),
        ("imessage", None),
    ]
    assert detect_sources({}) and detect_sources({})[0].kind == "imessage"


def test_scan_pages_through_imessage_handles_and_conversations():
    calls: list[list[str]] = []

    def runner(source, args):
        calls.append(args)
        return fake_runner(source, args)

    identities = scan_source(
        Source(id="imessage", kind="imessage", command=("imessage-local",)),
        runner=runner,
    )
    assert [c[0] for c in calls] == ["contacts", "conversations"]
    assert all("--limit" in c and "--offset" in c for c in calls)
    assert sorted(i.contact_id for i in identities) == [
        "chat:42",
        "handle:+15550000001",
        "handle:bob@example.test",
    ]


def test_snapshot_lists_only_recent_people_and_tracks_progress(monkeypatch):
    _fixed_clock(monkeypatch)
    _two_sources(monkeypatch)
    snap = service.snapshot(refresh=True)
    keys = {row["key"] for row in snap["people"]}
    assert "phone:15550000002" not in keys, (
        "contacts without messages in the last year are excluded"
    )
    assert snap["progress"] == {
        "total": 4,
        "categorized": 0,
        "pending": 4,
        "skipped": 0,
    }
    assert set(snap["queue"]) == keys
    assert [c["id"] for c in snap["categories"]] == [
        "family",
        "friends",
        "work",
        "services",
        "unknown",
    ]
    assert snap["errors"] == []

    service.assign(["phone:15550000001"], "family")
    service.skip(["email:bob@example.test"], hours=24)
    snap = service.snapshot()
    assert snap["progress"] == {
        "total": 4,
        "categorized": 1,
        "pending": 3,
        "skipped": 1,
    }
    assert set(snap["queue"]) == {"whatsapp:99999@g.us", "imessage:chat:42"}
    ada = next(row for row in snap["people"] if row["key"] == "phone:15550000001")
    assert ada["category"]["label"] == "Family" and ada["skipped"] is False

    # The skip expires and the contact re-enters the queue.
    _fixed_clock(monkeypatch, NOW + timedelta(hours=25))
    assert "email:bob@example.test" in service.snapshot(cache_only=True)["queue"]


def test_new_contacts_enter_the_queue_on_refresh(monkeypatch):
    _fixed_clock(monkeypatch)
    _two_sources(monkeypatch)
    service.snapshot(refresh=True)
    service.assign(
        [
            "phone:15550000001",
            "email:bob@example.test",
            "whatsapp:99999@g.us",
            "imessage:chat:42",
        ],
        "friends",
    )
    assert service.snapshot()["progress"]["pending"] == 0

    newcomer = {
        "id": "15550000003@s.whatsapp.net",
        "contact_name": "New Person",
        "is_group": 0,
        "last_message_at": RECENT,
    }
    monkeypatch.setattr(
        service,
        "scan_source",
        lambda source: scan_source(
            source,
            runner=lambda s, a: (
                {"contacts": WA_ROWS + [newcomer]}
                if s.kind == "whatsapp" and a[0] == "contacts" and a[-1] == "0"
                else fake_runner(s, a)
            ),
        ),
    )
    snap = service.snapshot(refresh=True)
    assert snap["queue"] == ["phone:15550000003"]


def test_refresh_reports_a_source_error_without_losing_other_sources(monkeypatch):
    _fixed_clock(monkeypatch)
    _two_sources(monkeypatch)

    def scan(source):
        if source.kind == "whatsapp":
            raise MessagingSourceError("WhatsApp: command exited with status 1")
        return fake_scan(source)

    monkeypatch.setattr(service, "scan_source", scan)
    snap = service.snapshot(refresh=True)
    assert snap["errors"] == [
        {"source_id": "whatsapp", "message": "WhatsApp: command exited with status 1"}
    ]
    assert {row["key"] for row in snap["people"]} == {
        "phone:15550000001",
        "email:bob@example.test",
        "imessage:chat:42",
    }


def test_category_crud_unassigns_people_on_delete(monkeypatch):
    _fixed_clock(monkeypatch)
    _two_sources(monkeypatch)
    service.snapshot(refresh=True)
    created = service.save_category("Neighbors", "ask")
    assert created["id"] == "neighbors"
    assert service.save_category("Neighbors", "block")["id"] == "neighbors-2"
    service.assign(["phone:15550000001"], "neighbors")
    assert service.delete_category("neighbors") == {"id": "neighbors", "unassigned": 1}
    assert "phone:15550000001" in service.snapshot()["queue"]
    renamed = service.save_category("Close friends", "ask", "friends")
    assert renamed == {"id": "friends", "label": "Close friends", "policy": "ask"}


def test_approval_gate_labels_contacts_and_blocks_by_policy(monkeypatch):
    _fixed_clock(monkeypatch)
    _two_sources(monkeypatch)
    service.snapshot(refresh=True)
    assert service.approval_gate(
        "whatsapp-cli", {"contact_id": "15550000001@s.whatsapp.net"}
    ) == {
        "category": "Uncategorized",
        "policy": "ask",
        "person_key": "phone:15550000001",
    }
    service.assign(["phone:15550000001"], "unknown")
    # The iMessage identity of the same person inherits the category.
    assert (
        service.approval_gate("imessage", {"contact_id": "handle:+15550000001"})[
            "policy"
        ]
        == "block"
    )
    assert service.approval_gate("gmail", {"contact_id": "x"}) is None
    assert (
        service.approval_gate("whatsapp-cli", {"contact_id": "unseen@s.whatsapp.net"})
        is None
    )


def test_skill_approval_request_is_declined_for_blocked_contacts(monkeypatch, tmp_path):
    _fixed_clock(monkeypatch)
    _two_sources(monkeypatch)
    service.snapshot(refresh=True)
    service.assign(["phone:15550000001"], "unknown")
    created: dict = {}

    def create(**kwargs):
        created.update(kwargs)
        return {"id": "skill-1", "params": kwargs}

    answered: list[tuple[str, str]] = []
    monkeypatch.setattr(approvals, "create_skill_approval_request", create)
    monkeypatch.setattr(
        approvals,
        "answer_skill_approval_request",
        lambda request_id, decision: answered.append((request_id, decision)),
    )
    request = APIRequestFactory().post(
        "/api/skill-approval-requests/",
        data=json.dumps(
            {
                "skill": "whatsapp-cli",
                "action": "send-whatsapp-message",
                "description": "Queue a WhatsApp message",
                "details": {"contact_id": "15550000001@s.whatsapp.net"},
            }
        ),
        content_type="application/json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = approvals.skill_approval_requests(request)
    assert response.status_code == 201
    assert created["details"]["contact_category"] == "Unknown or spam"
    assert answered == [("skill-1", "decline")]
    assert response.data["request"]["auto_declined"] is True


def test_api_assign_skip_and_lookup(monkeypatch):
    _fixed_clock(monkeypatch)
    _two_sources(monkeypatch)
    factory = APIRequestFactory()

    def call(view, method, path, data=None, **kwargs):
        request = getattr(factory, method)(
            path,
            data=json.dumps(data) if data is not None else None,
            content_type="application/json",
        )
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
        return view(request, **kwargs)

    listing = call(api.messaging_contacts, "get", "/api/messaging-contacts/?refresh=1")
    assert listing.status_code == 200 and listing.data["progress"]["total"] == 4
    assigned = call(
        api.messaging_contacts_assign,
        "post",
        "/api/messaging-contacts/assign/",
        {"keys": ["phone:15550000001", "imessage:chat:42"], "category": "work"},
    )
    assert assigned.status_code == 200 and assigned.data["progress"]["categorized"] == 2
    bad = call(
        api.messaging_contacts_assign,
        "post",
        "/api/messaging-contacts/assign/",
        {"keys": ["phone:15550000001"], "category": "nope"},
    )
    assert bad.status_code == 400
    skipped = call(
        api.messaging_contacts_skip,
        "post",
        "/api/messaging-contacts/skip/",
        {"keys": ["email:bob@example.test"]},
    )
    assert skipped.status_code == 200 and skipped.data["progress"]["skipped"] == 1
    lookup = call(
        api.messaging_contact_lookup,
        "get",
        "/api/messaging-contacts/lookup/?kind=imessage&contact_id=handle%3A%2B15550000001",
    )
    assert (
        lookup.status_code == 200 and lookup.data["contact"]["category"]["id"] == "work"
    )
    summary = call(
        api.messaging_contacts_summary, "get", "/api/messaging-contacts/summary/"
    )
    assert summary.data["progress"] == {
        "total": 4,
        "categorized": 2,
        "pending": 2,
        "skipped": 1,
    }
    deleted = call(
        api.messaging_contact_category_detail,
        "delete",
        "/api/messaging-contacts/categories/work/",
        category_id="work",
    )
    assert deleted.status_code == 200 and deleted.data["unassigned"] == 2
