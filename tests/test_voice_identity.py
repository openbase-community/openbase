"""The Cartesia <-> GPT-Live voice pairing and the per-install voice identity."""

from __future__ import annotations

import pytest

from openbase_coder_cli import dispatcher_config
from openbase_coder_cli.cartesia_voice_catalog import (
    CARTESIA_VOICE_CATALOG,
    DEFAULT_GPT_LIVE_VOICE,
    FEMININE,
    GPT_LIVE_VOICE_CATALOG,
    MASCULINE,
    cartesia_voice_catalog_payload,
    cartesia_voice_for_gpt_live_voice,
    cartesia_voice_for_id,
    gpt_live_voice_for_cartesia_voice,
    gpt_live_voice_for_id,
)
from openbase_coder_cli.tts_providers import (
    DEFAULT_CARTESIA_VOICE_ID,
    KOKORO_PROVIDER_ID,
    OPENBASE_CLOUD_TTS_PROVIDER_ID,
    get_tts_provider,
)
from openbase_coder_cli.voice_identity import (
    IDENTITY_SOURCE_DISPATCHER_VOICE,
    IDENTITY_SOURCE_LIVE_VOICE_OVERRIDE,
    current_voice_identity,
)

JACQUELINE = "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"
DANIEL = "47c38ca4-5f35-497b-b1a3-415245fb35e1"
BLAKE = "a167e0f3-df7e-4d52-a9c3-f949145efdab"
ACCEPTED_GPT_LIVE_VOICES = {
    "alloy", "ash", "ballad", "beacon", "cedar", "cinder", "coral",
    "echo", "marin", "sage", "shimmer", "stone", "verse", "vesper",
}


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    config_path = tmp_path / "dispatcher-config.json"
    monkeypatch.setattr(dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", config_path)
    monkeypatch.delenv("LIVEKIT_LIVE_VOICE_VOICE", raising=False)
    monkeypatch.delenv("CARTESIA_VOICE_ID", raising=False)
    monkeypatch.delenv("LIVEKIT_VOICE_MODEL", raising=False)
    return config_path


# --- catalog integrity -----------------------------------------------------


def test_gpt_live_catalog_lists_exactly_the_accepted_voices():
    assert {voice.id for voice in GPT_LIVE_VOICE_CATALOG} == ACCEPTED_GPT_LIVE_VOICES
    assert len(GPT_LIVE_VOICE_CATALOG) == len(ACCEPTED_GPT_LIVE_VOICES)
    assert {voice.gender for voice in GPT_LIVE_VOICE_CATALOG} == {FEMININE, MASCULINE}


def test_every_offered_cartesia_voice_pairs_with_a_same_gender_gpt_live_voice():
    for voice in CARTESIA_VOICE_CATALOG:
        live_voice = gpt_live_voice_for_id(voice.gpt_live_voice)
        assert live_voice is not None, voice.name
        assert voice.gender in {FEMININE, MASCULINE}, voice.name
        assert live_voice.gender == voice.gender, voice.name


def test_every_gpt_live_voice_maps_back_to_a_same_gender_offered_cartesia_voice():
    for live_voice in GPT_LIVE_VOICE_CATALOG:
        match = cartesia_voice_for_id(live_voice.cartesia_voice_id)
        assert match is not None, live_voice.id
        assert match.gender == live_voice.gender, live_voice.id


def test_reverse_pick_round_trips_for_every_voice_some_cartesia_voice_pairs_with():
    paired = {voice.gpt_live_voice for voice in CARTESIA_VOICE_CATALOG}
    for live_voice in GPT_LIVE_VOICE_CATALOG:
        if live_voice.id in paired:
            assert cartesia_voice_for_gpt_live_voice(live_voice.id).gpt_live_voice == (
                live_voice.id
            )


def test_default_pair_is_jacqueline_and_marin():
    assert DEFAULT_CARTESIA_VOICE_ID == JACQUELINE
    assert DEFAULT_GPT_LIVE_VOICE == "marin"
    assert gpt_live_voice_for_cartesia_voice(JACQUELINE) == "marin"
    assert cartesia_voice_for_gpt_live_voice("marin").id == JACQUELINE


def test_unknown_voices_fall_back_to_the_default_pair():
    assert gpt_live_voice_for_cartesia_voice("custom-voice") == "marin"
    assert gpt_live_voice_for_cartesia_voice(None) == "marin"
    assert cartesia_voice_for_gpt_live_voice("onyx").id == JACQUELINE
    assert cartesia_voice_for_gpt_live_voice(" Cedar ").id == BLAKE


def test_catalog_payload_carries_the_pair_for_clients():
    payload = cartesia_voice_catalog_payload()
    assert payload[0]["name"] == "Jacqueline"
    assert payload[0]["gpt_live_voice"] == "marin"
    cloud_voice = get_tts_provider(OPENBASE_CLOUD_TTS_PROVIDER_ID).voice_for_id(DANIEL)
    assert cloud_voice.gpt_live_voice == "beacon"


# --- the per-install identity ----------------------------------------------


def test_default_identity_is_jacqueline_with_marin():
    identity = current_voice_identity()
    assert identity.voice_id == JACQUELINE
    assert identity.voice_name == "Jacqueline"
    assert identity.gpt_live_voice == "marin"
    assert identity.source == IDENTITY_SOURCE_DISPATCHER_VOICE


def test_chosen_voice_drives_both_engines(isolated_config):
    dispatcher_config.set_dispatcher_voice(DANIEL, isolated_config)
    identity = current_voice_identity()
    assert identity.voice_id == DANIEL
    assert identity.voice_name == "Daniel"
    assert identity.gpt_live_voice == "beacon"


def test_pinned_live_voice_anchors_announcements_on_its_cartesia_match(monkeypatch):
    monkeypatch.setenv("LIVEKIT_LIVE_VOICE_VOICE", "cedar")
    identity = current_voice_identity()
    assert identity.voice_id == BLAKE
    assert identity.gpt_live_voice == "cedar"
    assert identity.source == IDENTITY_SOURCE_LIVE_VOICE_OVERRIDE


def test_pinned_live_voice_equal_to_the_pair_keeps_the_chosen_voice(
    monkeypatch, isolated_config
):
    dispatcher_config.set_dispatcher_voice(DANIEL, isolated_config)
    monkeypatch.setenv("LIVEKIT_LIVE_VOICE_VOICE", "beacon")
    identity = current_voice_identity()
    assert identity.voice_id == DANIEL
    assert identity.source == IDENTITY_SOURCE_DISPATCHER_VOICE


def test_pinned_live_voice_is_ignored_on_the_pipeline_engine(monkeypatch, isolated_config):
    dispatcher_config.set_voice_model("pipeline", isolated_config)
    monkeypatch.setenv("LIVEKIT_LIVE_VOICE_VOICE", "cedar")
    identity = current_voice_identity()
    assert identity.voice_id == JACQUELINE
    assert identity.source == IDENTITY_SOURCE_DISPATCHER_VOICE


def test_unsupported_pinned_live_voice_is_ignored(monkeypatch):
    monkeypatch.setenv("LIVEKIT_LIVE_VOICE_VOICE", "onyx")
    identity = current_voice_identity()
    assert identity.voice_id == JACQUELINE
    assert identity.gpt_live_voice == "marin"


def test_local_voice_without_a_pair_keeps_gender(monkeypatch):
    monkeypatch.setattr(
        dispatcher_config,
        "dispatcher_voice",
        lambda path=None: {"id": "am_adam", "name": "Adam", "provider": KOKORO_PROVIDER_ID},
    )
    monkeypatch.setattr(
        "openbase_coder_cli.voice_identity.dispatcher_voice",
        dispatcher_config.dispatcher_voice,
    )
    identity = current_voice_identity()
    assert identity.voice_id == "am_adam"
    assert identity.gpt_live_voice == "cedar"
    assert gpt_live_voice_for_id(identity.gpt_live_voice).gender == MASCULINE
