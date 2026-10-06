from __future__ import annotations

import json

import pytest

from openbase_coder_cli import dispatcher_config
from openbase_coder_cli.voice_models import (
    DEFAULT_VOICE_MODEL_ID,
    GPT_LIVE_VOICE_MODEL_ID,
    PIPELINE_VOICE_MODEL_ID,
    VOICE_ENGINE_LIVE,
    VOICE_ENGINE_PIPELINE,
    VOICE_MODEL_ENV_KEY,
    normalize_voice_model_id,
    voice_engine_for_model,
    voice_model_options_payload,
)


def test_default_voice_model_is_gpt_live():
    assert DEFAULT_VOICE_MODEL_ID == GPT_LIVE_VOICE_MODEL_ID
    assert voice_engine_for_model(None) == VOICE_ENGINE_LIVE
    defaults = [o for o in voice_model_options_payload() if o["is_default"]]
    assert [o["id"] for o in defaults] == [GPT_LIVE_VOICE_MODEL_ID]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("gpt-live-1", GPT_LIVE_VOICE_MODEL_ID),
        (" GPT-Live ", GPT_LIVE_VOICE_MODEL_ID),
        ("live", GPT_LIVE_VOICE_MODEL_ID),
        ("pipeline", PIPELINE_VOICE_MODEL_ID),
        ("classic", PIPELINE_VOICE_MODEL_ID),
    ],
)
def test_normalize_voice_model_id(raw, expected):
    assert normalize_voice_model_id(raw) == expected


def test_normalize_voice_model_id_rejects_unknown():
    with pytest.raises(ValueError, match="Voice model must be one of"):
        normalize_voice_model_id("gemini-3.8-live")


def test_pipeline_model_maps_to_pipeline_engine():
    assert voice_engine_for_model(PIPELINE_VOICE_MODEL_ID) == VOICE_ENGINE_PIPELINE


def test_dispatcher_config_voice_model_round_trip(tmp_path, monkeypatch):
    monkeypatch.delenv(VOICE_MODEL_ENV_KEY, raising=False)
    config_path = tmp_path / "dispatcher-config.json"
    assert dispatcher_config.selected_voice_model_id(config_path) == GPT_LIVE_VOICE_MODEL_ID
    assert dispatcher_config.selected_voice_engine(config_path) == VOICE_ENGINE_LIVE

    result = dispatcher_config.set_voice_model("pipeline", config_path)
    assert result == {"model": PIPELINE_VOICE_MODEL_ID, "engine": VOICE_ENGINE_PIPELINE}
    assert dispatcher_config.selected_voice_engine(config_path) == VOICE_ENGINE_PIPELINE
    payload = json.loads(config_path.read_text())
    assert payload[dispatcher_config.VOICE_MODEL_KEY] == PIPELINE_VOICE_MODEL_ID
    assert payload[dispatcher_config.SCHEMA_VERSION_KEY] == (
        dispatcher_config.DISPATCHER_CONFIG_SCHEMA_VERSION
    )

    with pytest.raises(ValueError):
        dispatcher_config.set_voice_model("nope", config_path)


def test_dispatcher_config_voice_model_env_override_and_bad_value(tmp_path, monkeypatch):
    config_path = tmp_path / "dispatcher-config.json"
    monkeypatch.setenv(VOICE_MODEL_ENV_KEY, "pipeline")
    assert dispatcher_config.selected_voice_model_id(config_path) == PIPELINE_VOICE_MODEL_ID
    config_path.write_text(json.dumps({dispatcher_config.VOICE_MODEL_KEY: "bogus"}))
    assert dispatcher_config.selected_voice_model_id(config_path) == DEFAULT_VOICE_MODEL_ID
