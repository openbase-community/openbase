import json

from openbase_coder_cli import livekit_voice_route


def test_route_json_is_reparsed_only_when_the_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "route.json"
    path.write_text(json.dumps({"dispatcher_thread_id": "a"}), encoding="utf-8")
    parses = []
    real_loads = json.loads

    def counting_loads(text, *args, **kwargs):
        parses.append(text)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(livekit_voice_route.json, "loads", counting_loads)
    first = livekit_voice_route._read_json(path)
    first["dispatcher_thread_id"] = "mutated by caller"
    assert livekit_voice_route._read_json(path) == {"dispatcher_thread_id": "a"}
    assert len(parses) == 1

    path.write_text(json.dumps({"dispatcher_thread_id": "bb"}), encoding="utf-8")
    assert livekit_voice_route._read_json(path) == {"dispatcher_thread_id": "bb"}
    assert len(parses) == 2
    assert livekit_voice_route._read_json(tmp_path / "missing.json") is None
