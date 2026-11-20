"""Follow-mode tests: spawn the CLI and append lines to a watched file.

These are true end-to-end checks: the tool runs as a subprocess, we append
to the log, and assert the new finding streams out on stdout.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

CLEAN_LINE = (
    '1.2.3.4 - - [03/Jul/2025:10:00:01 +0000] "GET /index.html HTTP/1.1" 200 1\n'
)
ATTACK_LINE = (
    '9.9.9.9 - - [03/Jul/2025:10:30:00 +0000] '
    '"GET /../../etc/passwd HTTP/1.1" 400 0\n'
)


def _spawn(*args: str) -> subprocess.Popen[bytes]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return subprocess.Popen(
        [sys.executable, "-m", "loganalyzer", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=REPO,
        env=env,
    )


def _read_until(proc: subprocess.Popen[bytes], needle: str, timeout: float = 20.0) -> str:
    """Read stdout until `needle` appears; fail loudly with what we got.

    Uses raw ``os.read`` on the pipe fd — ``select`` on a buffered
    ``TextIOWrapper`` deadlocks because the wrapper prefetches data the
    fd no longer reports as readable.
    """
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            buf += os.read(fd, 65536) or b""
            break
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            if needle.encode() in buf:
                return buf.decode("utf-8", errors="replace")
    raise AssertionError(
        f"never saw {needle!r} — output was:\n{buf.decode('utf-8', errors='replace')}"
    )


class TestFollowMode:
    def test_appended_attack_line_alerts(self, tmp_path: Path) -> None:
        log = tmp_path / "live.log"
        log.write_text(CLEAN_LINE)
        proc = _spawn("--follow", "--poll-seconds", "0.1", str(log))
        try:
            _read_until(proc, "watch: following")
            with log.open("a", encoding="utf-8") as fh:
                fh.write(ATTACK_LINE)
            out = _read_until(proc, "path_traversal")
            assert "9.9.9.9" in out
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_initial_findings_not_repeated(self, tmp_path: Path) -> None:
        # File already contains an attack before follow starts: it must
        # appear once in the initial report, not again as an alert.
        log = tmp_path / "live.log"
        log.write_text(CLEAN_LINE + ATTACK_LINE)
        proc = _spawn("--follow", "--poll-seconds", "0.1", str(log))
        try:
            out = _read_until(proc, "watch: following")
            assert out.count("path_traversal") == 1
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_fail_on_exits_one_when_reached(self, tmp_path: Path) -> None:
        log = tmp_path / "live.log"
        log.write_text(CLEAN_LINE)
        proc = _spawn(
            "--follow", "--poll-seconds", "0.1", "--fail-on", "high", str(log)
        )
        try:
            _read_until(proc, "watch: following")
            with log.open("a", encoding="utf-8") as fh:
                fh.write(ATTACK_LINE)   # path_traversal => HIGH
            assert proc.wait(timeout=20) == 1
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
