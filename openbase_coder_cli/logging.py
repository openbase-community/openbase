import logging
import time
from typing import Any

# Prefix that marks a machine-readable voice/dispatch timing event. Every such
# line is correlated after the fact against the iOS AuthDiagnostics timeline to
# reconstruct an iOS-vs-desktop view of a voice session (see
# voice-tests session timeline tooling).
DISPATCH_TIMING_PREFIX = "dispatch_timing "
# Key appended to each timing line carrying the emit-time wall clock in epoch
# milliseconds. The formatter below emits only the bare message (no asctime),
# so without this stamp a dispatch_timing line has no absolute time and cannot
# be placed on a cross-device timeline.
WALL_CLOCK_KEY = "wall_ms"


class DispatchTimingClockFilter(logging.Filter):
    """Stamp every ``dispatch_timing`` line with an epoch-millisecond wall clock.

    The voice pipeline emits dozens of ``dispatch_timing stage=... k=v`` lines
    from many call sites (audio frames, STT interim/final transcripts, VAD,
    connection-state changes, utterance dispatch). To align them with the iOS
    side — whose AuthDiagnostics entries each carry an ISO8601 wall clock — the
    desktop lines need an absolute timestamp too. Doing it here, at emit time,
    stamps every existing and future timing line from one place instead of
    editing each call site, and captures the true event time (filters run when
    the record is emitted, before any formatting).

    The appended text is a literal with no ``%`` placeholders, so ``record.args``
    still line up with the original ``record.msg`` format string. The stamp is
    idempotent: a record that already carries ``wall_ms=`` is left untouched, so
    the filter is safe to attach to more than one handler.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.msg
        if (
            isinstance(msg, str)
            and msg.startswith(DISPATCH_TIMING_PREFIX)
            and f" {WALL_CLOCK_KEY}=" not in msg
        ):
            record.msg = f"{msg} {WALL_CLOCK_KEY}={int(time.time() * 1000)}"
        return True


class EmojiFormatter(logging.Formatter):
    """Custom formatter that adds emojis to log levels."""

    FORMATS = {
        logging.DEBUG: "%(message)s",
        logging.INFO: "%(message)s",
        logging.WARNING: "⚠️  %(message)s",
        logging.ERROR: "❌ %(message)s",
        logging.CRITICAL: "🚨 %(message)s",
    }

    def format(self, record: Any) -> str:
        format_str = self.FORMATS.get(record.levelno, self.FORMATS[logging.INFO])
        formatter = logging.Formatter(format_str)
        return formatter.format(record)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure logging with emoji formatting."""
    handler = logging.StreamHandler()
    handler.setFormatter(EmojiFormatter())
    # Stamp timing lines with an epoch-ms wall clock at the handler boundary so
    # every dispatch_timing event carries an absolute, cross-device timestamp.
    handler.addFilter(DispatchTimingClockFilter())

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove any existing handlers
    root_logger.handlers = []
    root_logger.addHandler(handler)
