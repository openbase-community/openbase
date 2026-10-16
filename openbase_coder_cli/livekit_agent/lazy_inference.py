"""Keep the container's turn-detector weights cold until text needs inference.

LiveKit 1.8 registers and initializes inference runners before any call,
including GPT Live calls that never use this model. Retain its IPC service
and model behavior while deferring the expensive ONNX/tokenizer allocation.
"""

from __future__ import annotations

from livekit.agents.inference_runner import _InferenceRunner
from livekit.plugins.turn_detector.english import _EUORunnerEn
from livekit.plugins.turn_detector.multilingual import _EUORunnerMultilingual

from openbase_coder_cli.workspace_memory import is_container


class LazyMultilingualRunner(_EUORunnerMultilingual):
    def initialize(self) -> None:
        self._loaded = False

    def run(self, data: bytes) -> bytes | None:
        if not self._loaded:
            super().initialize()
            self._loaded = True
        return super().run(data)


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
