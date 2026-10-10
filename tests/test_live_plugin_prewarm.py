"""Fresh-process regression for LiveKit's main-thread plugin registration."""

import subprocess
import sys
import textwrap


def test_prewarm_registers_openai_before_background_readiness_probe():
    script = textwrap.dedent("""
        import socket
        import threading
        from types import SimpleNamespace

        def no_network(*args, **kwargs):
            raise AssertionError("This regression must not contact any service")
        socket.socket.connect = no_network

        from openbase_coder_cli.livekit_agent import livekit
        from openbase_coder_cli.livekit_agent.live_voice import import_live_model

        livekit.install_vad_backlog_patch = lambda: None
        livekit.silero.VAD.load = lambda: "vad"
        livekit.LIVE_VOICE_READINESS_PREWARM = True
        errors = []
        models = []
        def probe():
            try:
                models.append(import_live_model())
            except Exception as exc:
                errors.append(str(exc))
        def start():
            worker = threading.Thread(target=probe)
            worker.start()
            worker.join(timeout=10)
            assert not worker.is_alive()
        livekit._live_voice_readiness_refresher = SimpleNamespace(start=start)
        proc = SimpleNamespace(userdata={})
        livekit.prewarm(proc)
        assert proc.userdata["vad"] is not None
        assert not errors, errors
        assert models[0].__name__ == "GPTLiveModel"
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
