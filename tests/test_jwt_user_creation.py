"""Regression tests for JWT first-contact user creation.

A device's first contact fires several authenticated API requests at once;
all of them race through ``_get_or_create_user`` and previously the losers
surfaced ``IntegrityError`` 500s (seen live as a burst of
``UNIQUE constraint failed: auth_user.username``).
"""

from __future__ import annotations

import os
from contextlib import nullcontext

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.db import IntegrityError  # noqa: E402

from openbase_coder_cli.config import authentication  # noqa: E402


class _FakeManager:
    """User manager where a concurrent winner appears mid-create."""

    def __init__(self, *, race: bool):
        self.race = race
        self.rows: dict[str, object] = {}
        self.create_calls = 0

    def filter(self, *, username):
        rows = self.rows

        class _QuerySet:
            @staticmethod
            def first():
                return rows.get(username)

        return _QuerySet()

    def create_user(self, *, username, email, password):
        self.create_calls += 1
        if self.race:
            # Another request won the insert between our existence check and
            # this create; the database rejects the duplicate.
            self.rows[username] = f"winner:{username}"
            raise IntegrityError("UNIQUE constraint failed: auth_user.username")
        user = f"created:{username}"
        self.rows[username] = user
        return user


def _patched(monkeypatch, manager):
    class _FakeUser:
        objects = manager

    monkeypatch.setattr(authentication, "get_user_model", lambda: _FakeUser)
    monkeypatch.setattr(authentication.transaction, "atomic", lambda: nullcontext())
    return _FakeUser


def test_creates_user_on_first_contact(monkeypatch):
    manager = _FakeManager(race=False)
    _patched(monkeypatch, manager)
    assert authentication._get_or_create_user(sub="abc") == "created:abc"
    assert manager.create_calls == 1


def test_returns_existing_user_without_creating(monkeypatch):
    manager = _FakeManager(race=False)
    manager.rows["abc"] = "existing:abc"
    _patched(monkeypatch, manager)
    assert authentication._get_or_create_user(sub="abc") == "existing:abc"
    assert manager.create_calls == 0


def test_concurrent_create_race_returns_winner(monkeypatch):
    manager = _FakeManager(race=True)
    _patched(monkeypatch, manager)
    assert authentication._get_or_create_user(sub="abc") == "winner:abc"
    assert manager.create_calls == 1


def test_integrity_error_without_winner_reraises(monkeypatch):
    manager = _FakeManager(race=True)
    _patched(monkeypatch, manager)
    original_create = manager.create_user

    def create_without_winner(**kwargs):
        try:
            return original_create(**kwargs)
        finally:
            manager.rows.clear()

    monkeypatch.setattr(manager, "create_user", create_without_winner)
    with pytest.raises(IntegrityError):
        authentication._get_or_create_user(sub="abc")
