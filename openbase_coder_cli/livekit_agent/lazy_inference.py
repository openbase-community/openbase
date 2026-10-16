"""Keep the container's turn-detector weights cold until text needs inference.

LiveKit 1.8 registers and initializes inference runners before any call,
including GPT Live calls that never use this model. Retain its IPC service
and model behavior while deferring the expensive ONNX/tokenizer allocation.
"""

from __future__ import annotations

import threading

from livekit.agents.inference_runner import _InferenceRunner
from livekit.plugins.turn_detector.english import _EUORunnerEn
from livekit.plugins.turn_detector.multilingual import _EUORunnerMultilingual

from openbase_coder_cli.workspace_memory import is_container

MODEL_IDLE_SECONDS = 300.0


class LazyMultilingualRunner(_EUORunnerMultilingual):
    def initialize(self) -> None:
        self._runner = None
        self._lock = threading.Lock()
        self._timer = None
        self._generation = 0

    def _release_if_idle(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            # ONNX and tokenizer allocations are owned by this runner. Drop
            # them after five quiet minutes instead of keeping a call's peak
            # allocation for the entire lifetime of the workspace.
            self._runner = None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def run(self, data: bytes) -> bytes | None:
        with self._lock:
            self._generation += 1
            if self._timer is not None:
                self._timer.cancel()
            if self._runner is None:
                runner = _EUORunnerMultilingual()
                runner.initialize()
                self._runner = runner
            try:
                return self._runner.run(data)
            finally:
                self._timer = threading.Timer(
                    MODEL_IDLE_SECONDS, self._release_if_idle, (self._generation,)
                )
                self._timer.daemon = True
                self._timer.start()


def configure_container_inference() -> None:
    if not is_container():
        return
    # Importing the plugin also registers its English model. Openbase uses
    # only multilingual, so that second model never serves a request here.
    _InferenceRunner.registered_runners.pop(_EUORunnerEn.INFERENCE_METHOD, None)
    if _EUORunnerMultilingual.INFERENCE_METHOD in _InferenceRunner.registered_runners:
        _InferenceRunner.registered_runners[_EUORunnerMultilingual.INFERENCE_METHOD] = (
            LazyMultilingualRunner
        )
