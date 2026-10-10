from openbase_coder_cli.thread_sync.models import ThreadStatus
from openbase_coder_cli.thread_sync.thread_payloads import _session_from_thread


def test_queued_followup_never_precedes_current_prompt_in_history():
    thread = {
        "threadId": "phone",
        "backend": "openbase_cloud",
        "status": "running",
        "activeTurnId": "original",
        "turns": [
            {
                "turnId": "question",
                "status": "queued",
                "createdAt": "2026-10-10T04:16:48Z",
                "prompt": "What is the code?",
            },
            {
                "turnId": "original",
                "status": "running",
                "createdAt": "2026-10-10T04:13:50Z",
                "updatedAt": "2026-10-10T04:17:00Z",
                "prompt": "Create a private repository",
            },
        ],
    }
    state = _session_from_thread(thread, include_turns=True)
    assert state.run_history == []
    assert state.current_run.message == "Create a private repository"


def test_history_order_uses_acceptance_time_not_completion_or_late_updates():
    state = _session_from_thread(
        {
            "threadId": "phone",
            "turns": [
                {
                    "id": "question",
                    "status": "completed",
                    "createdAt": "2026-10-10T04:16:48Z",
                    "finishedAt": "2026-10-10T04:18:00Z",
                },
                {
                    "id": "original",
                    "status": "cancelled",
                    "createdAt": "2026-10-10T04:13:50Z",
                    "updatedAt": "2026-10-10T04:20:00Z",
                },
            ],
        },
        include_turns=True,
    )
    assert [turn.run_id for turn in state.run_history] == ["original", "question"]


def test_background_response_preserves_code_and_does_not_claim_idle():
    state = _session_from_thread(
        {
            "threadId": "phone",
            "backend": "openbase_cloud",
            "status": "running",
            "activeTurnId": "login",
            "turns": [
                {
                    "turnId": "login",
                    "status": "running",
                    "responseFinishedAt": "2026-10-10T04:16:10Z",
                    "items": [
                        {"type": "agentMessage", "text": "Device code: EXAMPLE"},
                        {"type": "agentMessage", "text": "Approve on your phone."},
                    ],
                    "lastUsefulMessage": "Approve on your phone.",
                }
            ],
        },
        include_turns=True,
    )
    assert state.status == ThreadStatus.running
    assert state.current_run.response_finished_at is not None
    assert (
        state.current_run.accumulated_output
        == "Device code: EXAMPLE\n\nApprove on your phone."
    )
