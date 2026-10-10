"""The container service log sink: pass-through, append, size cap, fail-soft."""

from __future__ import annotations

import io
import os
import stat
from pathlib import Path

from openbase_coder_cli.services.container_log_sink import (
    ServiceLogSink,
    main,
    trim_to_tail,
)


def _sink(path: Path, **kwargs):
    out = io.BytesIO()
    err = io.BytesIO()
    sink = ServiceLogSink(path, stdout=out, stderr=err, **kwargs)
    return sink, out, err


def test_lines_pass_through_and_append_to_the_file(tmp_path):
    path = tmp_path / "logs" / "livekit-agent.log"
    sink, out, err = _sink(path)
    sink.run(io.BytesIO(b"dispatch_timing stage=a\nstage=b\n"))
    sink.close()

    assert out.getvalue() == b"dispatch_timing stage=a\nstage=b\n"
    assert path.read_bytes() == b"dispatch_timing stage=a\nstage=b\n"
    assert err.getvalue() == b""


def test_existing_content_is_kept_and_appended_to(tmp_path):
    path = tmp_path / "svc.log"
    path.write_bytes(b"old\n")
    sink, out, _ = _sink(path)
    sink.write(b"new\n")
    sink.close()

    assert path.read_bytes() == b"old\nnew\n"
    assert out.getvalue() == b"new\n"


def test_file_is_trimmed_to_its_tail_once_over_the_cap(tmp_path):
    path = tmp_path / "svc.log"
    sink, out, _ = _sink(path, max_bytes=200)
    for i in range(100):
        sink.write(f"line {i:03d} {'x' * 10}\n".encode())
    sink.close()

    kept = path.read_bytes()
    assert len(kept) <= 200
    assert kept.endswith(b"line 099 xxxxxxxxxx\n")
    assert kept.startswith(b"line "), kept[:20]  # cut at a line boundary
    # stdout is never trimmed
    assert out.getvalue().count(b"\n") == 100


def test_trim_to_tail_keeps_whole_lines_and_the_inode(tmp_path):
    path = tmp_path / "svc.log"
    path.write_bytes(b"".join(f"row {i}\n".encode() for i in range(50)))
    inode = path.stat().st_ino

    size = trim_to_tail(path, keep_bytes=30)

    assert path.stat().st_ino == inode
    assert size == len(path.read_bytes()) <= 30
    assert path.read_bytes().startswith(b"row ")
    assert path.read_bytes().endswith(b"row 49\n")


def test_trim_to_tail_is_a_no_op_under_the_cap(tmp_path):
    path = tmp_path / "svc.log"
    path.write_bytes(b"short\n")
    assert trim_to_tail(path, keep_bytes=100) == 6
    assert path.read_bytes() == b"short\n"


def test_unwritable_file_is_reported_once_and_stream_continues(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    if os.access(blocked, os.W_OK):  # running as root: cannot provoke EACCES
        return
    try:
        sink, out, err = _sink(blocked / "svc.log")
        sink.write(b"one\n")
        sink.write(b"two\n")
        sink.close()
    finally:
        blocked.chmod(stat.S_IRWXU)

    assert out.getvalue() == b"one\ntwo\n"
    assert err.getvalue().count(b"[log-sink]") == 1
    assert b"output continues on stdout only" in err.getvalue()


def test_main_parses_the_path_and_cap(tmp_path, monkeypatch):
    path = tmp_path / "svc.log"
    stdin = io.TextIOWrapper(io.BytesIO(b"hello\n"))
    stdout = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", stdout)

    assert main([str(path), "--max-bytes", "64"]) == 0

    assert path.read_bytes() == b"hello\n"
    stdout.flush()
    assert stdout.buffer.getvalue() == b"hello\n"
