from openbase_coder_cli.livekit_agent.live_speech_gate import LiveSpeechGate


def test_echo_matching_is_independent_of_output_delta_boundaries():
    gate = LiveSpeechGate(clock=lambda: 10.0)
    gate.authorize()
    gate.agent_state_changed("speaking")
    # The plugin reconstructs these deltas as "Project ready".
    gate.agent_said("Pro")
    gate.agent_said("ject ready")
    gate.user_state_changed("speaking")
    gate.caller_heard("Project ready")
    assert gate.authorized, "the exact echoed phrase revoked its own audio"
