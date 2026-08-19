"""Fail-closed, per-approval voice authorization for Openbase Coder."""

from .broker import VoiceLockdownBroker, get_voice_lockdown_broker

__all__ = ["VoiceLockdownBroker", "get_voice_lockdown_broker"]
