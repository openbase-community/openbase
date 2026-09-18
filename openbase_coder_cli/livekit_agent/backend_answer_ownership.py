"""A cancelled LiveKit consumer does not cancel its shielded backend turn."""


def preserve_backend_answer_on_cancel(stream, record, ledger) -> bool:
    if not getattr(stream, "_backend_committed", False):
        return False
    client = getattr(stream, "_backend_voice_client", None)
    turn_id = getattr(client, "pending_voice_answer_turn_id", None)
    if not turn_id:
        return False
    return ledger.mark_answer_owed(record, turn_id=turn_id, client=client)
