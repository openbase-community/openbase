from types import SimpleNamespace

from openbase_coder_cli import memory_status
from openbase_coder_cli.workspace_memory import Memory


def test_healthy_status_does_not_enumerate_processes(monkeypatch):
    monkeypatch.setattr(memory_status, "memory_snapshot", lambda: Memory(4000, 2000))
    def unexpected(**kwargs):
        raise AssertionError("Healthy poll must not enumerate processes")
    monkeypatch.setattr(memory_status.psutil, "process_iter", unexpected)
    assert memory_status.memory_status_payload() == {
        "used_bytes": 2000, "total_bytes": 4000, "percent": 50.0, "top_processes": []
    }


def test_pressure_reports_bounded_names_and_rss_only(monkeypatch):
    monkeypatch.setattr(memory_status, "memory_snapshot", lambda: Memory(4000, 100))
    processes = [SimpleNamespace(info={"name": "python" + str(n), "memory_info": SimpleNamespace(rss=n)}) for n in range(5)]
    processes.append(SimpleNamespace(info={"name": None, "memory_info": None}))
    monkeypatch.setattr(memory_status.psutil, "process_iter", lambda **kw: processes)
    payload = memory_status.memory_status_payload()
    assert payload["percent"] == 97.5
    assert payload["used_bytes"] == 3900
    assert payload["top_processes"] == [{"name": "python" + str(n), "rss_bytes": n} for n in (4, 3, 2)]
