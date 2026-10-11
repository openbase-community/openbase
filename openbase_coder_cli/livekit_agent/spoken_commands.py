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
