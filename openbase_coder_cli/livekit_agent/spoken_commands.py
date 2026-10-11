"""Recognition of spoken control commands in transcribed user speech."""

EXIT_TO_DISPATCH_PHRASE = "exit to dispatch"
EXIT_TO_DISPATCH_PHRASES = {
    EXIT_TO_DISPATCH_PHRASE,
    "to dispatch",
    "two dispatch",
}

# An exit command is a short imperative ("Please exit to dispatch now."), not a
# sentence that merely mentions dispatch. Longer utterances are real prompts
# and must never be swallowed by the exit short-circuit.
_MAX_EXIT_COMMAND_WORDS = 6


def _normalize_spoken_command(text: str) -> str:
    return " ".join(
        "".join(char.lower() if char.isalnum() else " " for char in text).split()
    )


def _is_exit_to_dispatch_command(text: str) -> bool:
    normalized = _normalize_spoken_command(text)
    if len(normalized.split()) > _MAX_EXIT_COMMAND_WORDS:
        return False
    return any(phrase in normalized for phrase in EXIT_TO_DISPATCH_PHRASES)


# A spoken stop (Gabe, 2026-10-11): the whole utterance is a stop word plus at
# most filler, so "don't stop the server" or "stop using tabs" never matches.
STOP_WORDS = {"stop", "cancel", "abort", "halt"}
_STOP_FILLER = {
    "wait",
    "hold",
    "on",
    "please",
    "that",
    "it",
    "now",
    "ok",
    "okay",
    "hey",
    "no",
    "right",
    "everything",
    "the",
    "work",
    "all",
    "just",
}
_MAX_STOP_COMMAND_WORDS = 5


def is_stop_command(text: str) -> bool:
    """True when the caller only said to stop ("wait, stop", "cancel that")."""
    words = _normalize_spoken_command(text).split()
    if not words or len(words) > _MAX_STOP_COMMAND_WORDS:
        return False
    if not any(word in STOP_WORDS for word in words):
        return False
    return all(word in STOP_WORDS or word in _STOP_FILLER for word in words)
