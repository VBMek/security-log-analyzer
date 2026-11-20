"""Log line parsers.

Each supported format has a dedicated parser producing a normalized
``LogEvent``. ``parse_line`` tries every parser and falls back to a
``MALFORMED`` event, so unparsable input is reported rather than dropped.
Adding a new format means adding one parser — detectors stay unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from .models import EventKind, LogEvent

# Apache/nginx "combined"-style access log:
#   192.168.1.10 - - [03/Jul/2025:10:00:01 +0000] "GET /index.html HTTP/1.1" 200 1234
# The quoted request is captured whole: attack payloads (e.g. SQL injection
# strings) contain unencoded spaces, so the URI part may not be \S+.
ACCESS_LOG_RE = re.compile(
    r'^(?P<ip>\S+)\s+\S+\s+\S+\s+'
    r'\[(?P<ts>[^\]]+)\]\s+'
    r'"(?P<request>[^"]*)"\s+'
    r'(?P<status>\d{3})\s+(?P<size>\d+|-)\s*$'
)
ACCESS_TS_FORMAT = "%d/%b/%Y:%H:%M:%S %z"

# Syslog envelope: `Jul  3 10:00:03 server sshd[1234]: <message>`
SYSLOG_RE = re.compile(
    r'^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})\s+'
    r'(?P<host>\S+)\s+(?P<service>[A-Za-z_][\w./-]*(?:\[\d+\])?):\s*(?P<msg>.*)$'
)

# sshd message shapes seen in auth.log
SSHD_FAILED_RE = re.compile(
    r'^Failed password for (invalid user )?(?P<user>\S+) '
    r'from (?P<ip>\S+) port (?P<port>\d+)'
)
SSHD_ACCEPTED_RE = re.compile(
    r'^Accepted \S+ for (?P<user>\S+) from (?P<ip>\S+) port (?P<port>\d+)'
)
SSH_CONNECTION_RE = re.compile(
    r'^(?:Connection (?:closed|reset)|Disconnected) (?:by|from) (?P<ip>\S+)'
)

# sudo log: `user : TTY=pts/0 ; PWD=/home/u ; USER=root ; COMMAND=/bin/cat …`
SUDO_RE = re.compile(
    r'^(?P<user>\S+)\s*:\s*TTY=(?P<tty>[^;]*)\s*;\s*PWD=(?P<pwd>[^;]*)\s*;'
    r'\s*USER=(?P<runas>[^;]*)\s*;\s*COMMAND=(?P<cmd>.*)$'
)


def parse_access_line(
    line: str, *, source_file: str, line_no: int
) -> LogEvent | None:
    """Parse one access-log line; ``None`` when it does not match the format."""
    match = ACCESS_LOG_RE.match(line)
    if not match:
        return None
    try:
        timestamp = datetime.strptime(match["ts"], ACCESS_TS_FORMAT)
    except ValueError:
        return None   # shaped like an access line but with a broken timestamp

    # Split the quoted request into METHOD <uri…> HTTP/x.y — the URI may
    # itself contain spaces, so it is "everything between the two".
    method: str | None = None
    uri: str | None = None
    request = match["request"].strip()
    if request:
        parts = request.split(" ")
        method = parts[0]
        tail = parts[1:]
        if tail and tail[-1].startswith("HTTP/"):
            tail = tail[:-1]
        uri = " ".join(tail) or None

    return LogEvent(
        kind=EventKind.HTTP_REQUEST,
        raw=line,
        source_file=source_file,
        line_no=line_no,
        timestamp=timestamp,
        ip=match["ip"],
        method=method,
        path=uri,
        status=int(match["status"]),
    )


def parse_syslog_line(
    line: str,
    *,
    source_file: str,
    line_no: int,
    default_year: int,
    default_tz: tzinfo,
) -> LogEvent | None:
    """Parse one syslog line; ``None`` when it does not match the format.

    Syslog timestamps lack year and timezone, so ``default_year`` and
    ``default_tz`` from the configuration are applied.
    """
    match = SYSLOG_RE.match(line)
    if not match:
        return None
    try:
        timestamp = datetime.strptime(
            f"{default_year} {match['mon']} {match['day']} {match['time']}",
            "%Y %b %d %H:%M:%S",
        ).replace(tzinfo=default_tz)
    except ValueError:
        return None

    # Fields common to every event produced from this line.
    base: dict[str, Any] = {
        "raw": line,
        "source_file": source_file,
        "line_no": line_no,
        "timestamp": timestamp,
    }
    service = match["service"].split("[")[0]
    msg = match["msg"]

    if service == "sshd":
        failed = SSHD_FAILED_RE.match(msg)
        if failed:
            return LogEvent(
                kind=EventKind.SSH_AUTH, user=failed["user"],
                auth_success=False, invalid_user=failed.group(1) is not None,
                ip=failed["ip"], **base,
            )
        accepted = SSHD_ACCEPTED_RE.match(msg)
        if accepted:
            return LogEvent(
                kind=EventKind.SSH_AUTH, user=accepted["user"],
                auth_success=True, ip=accepted["ip"], **base,
            )
        conn = SSH_CONNECTION_RE.match(msg)
        return LogEvent(
            kind=EventKind.OTHER, ip=conn["ip"] if conn else None, **base,
        )

    if service == "sudo":
        sudo = SUDO_RE.match(msg)
        return LogEvent(
            kind=EventKind.SUDO if sudo else EventKind.OTHER,
            user=sudo["user"] if sudo else None,
            sudo_command=sudo["cmd"].strip() if sudo else None,
            **base,
        )

    return LogEvent(kind=EventKind.OTHER, **base)


def parse_line(
    line: str,
    *,
    source_file: str,
    line_no: int,
    default_year: int,
    default_tz: tzinfo,
) -> LogEvent:
    """Parse one line with the first matching parser; MALFORMED if none fits."""
    event = parse_access_line(line, source_file=source_file, line_no=line_no)
    if event is None:
        event = parse_syslog_line(
            line, source_file=source_file, line_no=line_no,
            default_year=default_year, default_tz=default_tz,
        )
    if event is None:
        event = LogEvent(
            kind=EventKind.MALFORMED,
            raw=line,
            source_file=source_file,
            line_no=line_no,
        )
    return event


def parse_file(
    path: Path, *, default_year: int, default_tz: tzinfo
) -> Iterator[LogEvent]:
    """Yield one ``LogEvent`` per non-empty line, streaming the file.

    Non-UTF-8 bytes are replaced rather than crashing the run — resilience
    matters more than fidelity for log analysis.
    """
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue   # ignore blank lines entirely
            yield parse_line(
                line.rstrip("\n"),
                source_file=str(path),
                line_no=line_no,
                default_year=default_year,
                default_tz=default_tz,
            )
