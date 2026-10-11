"""Size-capped file mirror for a supervised container service's output.

The container entrypoint supervises every Openbase service itself and pipes
their output to PID 1's stdout. In a Maritime workspace that stdout is the
VM's serial console, which nothing keeps: after Gabe's buggy staging call of
2026-10-09 not one line of the LiveKit agent's per-utterance log survived.
launchd and systemd installs keep ``<logs>/<service>.log`` instead
(``services/launchd.py``, ``services/systemd.py``), so this sink gives the
container the same file: every line is passed through to stdout unchanged
(the entrypoint still prefixes it for the console) and appended to the file,
which is trimmed back to its tail once it passes the cap.

The sink must never take the service down with it: a file that cannot be
written is reported once on stderr and the stream keeps flowing to stdout.

Usage (from the entrypoint): ``python -m openbase_coder_cli.services.container_log_sink <path>``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import BinaryIO

# A service log is trimmed on every start, so it only grows unbounded when
# the supervisor respawns a failing runner faster than anyone restarts it
# (2026-10-07: 12,080 identical tracebacks, 14.6 MB). Each runner start caps
# its own log at this size (``services/launchd.py``), and the container sink
# applies the same cap continuously. Stdlib only: this module runs as a
# pipeline stage for every supervised service and must import under any
# Python, with or without the CLI's dependencies.
SERVICE_LOG_CAP_BYTES = 4 * 1024 * 1024

# Once the file passes the cap it is cut back to this many trailing bytes, so
# a chatty service is trimmed every couple of megabytes instead of every line.
KEEP_FRACTION = 0.5


def trim_to_tail(path: Path, *, keep_bytes: int) -> int:
    """Keep only the last ``keep_bytes`` of ``path``, starting at a line boundary.

    Returns the new size. The file is rewritten in place (same inode) so an
    ``O_APPEND`` writer holding it open keeps appending after the kept tail.
    """
    with path.open("r+b") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        if size <= keep_bytes:
            return size
        handle.seek(size - keep_bytes)
        tail = handle.read()
        newline = tail.find(b"\n")
        if 0 <= newline < len(tail) - 1:
            tail = tail[newline + 1 :]
        handle.seek(0)
        handle.write(tail)
        handle.truncate()
        return len(tail)


class ServiceLogSink:
    """Mirror a byte stream into ``path`` with a size cap, passing it through."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = SERVICE_LOG_CAP_BYTES,
        stdout: BinaryIO | None = None,
        stderr: BinaryIO | None = None,
    ) -> None:
        self._path = path
        self._max_bytes = max(1, int(max_bytes))
        self._keep_bytes = max(1, int(self._max_bytes * KEEP_FRACTION))
        self._stdout = stdout if stdout is not None else sys.stdout.buffer
        self._stderr = stderr if stderr is not None else sys.stderr.buffer
        self._file: BinaryIO | None = None
        self._size = 0
        self._disabled = False
        self._open()

    def _open(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self._path.open("ab")
            self._size = self._path.stat().st_size
        except OSError as exc:
            self._disable(f"cannot open: {exc}")

    def _disable(self, reason: str) -> None:
        if not self._disabled:
            self._disabled = True
            self._stderr.write(
                f"[log-sink] {self._path}: {reason}; output continues on stdout only.\n".encode()
            )
            self._stderr.flush()
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def write(self, line: bytes) -> None:
        self._stdout.write(line)
        self._stdout.flush()
        if self._file is None:
            return
        try:
            self._file.write(line)
            self._file.flush()
            self._size += len(line)
            if self._size > self._max_bytes:
                self._size = trim_to_tail(self._path, keep_bytes=self._keep_bytes)
        except OSError as exc:
            self._disable(f"cannot write: {exc}")

    def run(self, stdin: BinaryIO) -> None:
        for line in stdin:
            self.write(line)

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path, help="log file to append to")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=SERVICE_LOG_CAP_BYTES,
        help="trim the file back to its tail once it exceeds this size",
    )
    args = parser.parse_args(argv)
    sink = ServiceLogSink(args.path, max_bytes=args.max_bytes)
    try:
        sink.run(sys.stdin.buffer)
    finally:
        sink.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
