"""``--follow`` mode: stream new findings as lines are appended.

Like ``tail -f`` for alerts: a normal analysis pass runs first, then each
file stays open and appended lines are parsed and fed through the same
detectors. ``finalize()`` is called after every batch and a signature
dedup suppresses findings already reported — while a severity change
(e.g. HIGH -> CRITICAL escalation) still re-fires as a new alert.
Log rotation is detected via inode/size changes and the file is reopened.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import IO, Any

from .config import RulesConfig
from .correlate import correlate_findings
from .detectors import build_detectors
from .models import EventKind, Finding, LogEvent, Severity
from .parsers import parse_line
from .report import ReportStats, render_json, render_text, sort_findings


class _LineFollower:
    """One tailed file: buffers partial lines, detects rotation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = path.open("r", encoding="utf-8", errors="replace")
        self.pending = ""

    def read_available(self) -> list[str]:
        """All complete (newline-terminated) lines available right now."""
        lines: list[str] = []
        while True:
            chunk = self.handle.readline()
            if not chunk:
                return lines
            data = self.pending + chunk
            if data.endswith("\n"):
                lines.append(data[:-1])
                self.pending = ""
            else:
                self.pending = data

    def rotated(self) -> bool:
        """True when the file was replaced (new inode) or truncated."""
        try:
            current = self.path.stat()
            opened = os.fstat(self.handle.fileno())
        except OSError:
            return False
        return (
            current.st_ino != opened.st_ino
            or current.st_size < self.handle.tell()
        )

    def reopen(self) -> None:
        self.handle.close()
        self.handle = self.path.open("r", encoding="utf-8", errors="replace")
        self.pending = ""
        print(f"watch: {self.path} rotated — reopened", file=sys.stderr, flush=True)

    def close(self) -> None:
        self.handle.close()


def _signature(finding: Finding) -> tuple[Any, ...]:
    """Stable identity used for alert dedup.

    Includes severity, so an escalating incident (same IPs/times, higher
    severity) legitimately re-fires; includes the first evidence location
    so distinct same-rule findings (e.g. two malformed lines) stay separate.
    """
    first = finding.evidence[0] if finding.evidence else None
    return (
        finding.rule_id,
        finding.ip,
        finding.first_seen.isoformat() if finding.first_seen else None,
        finding.severity.name,
        (first.source_file, first.line_no) if first else None,
    )


def _alert_text(finding: Finding) -> str:
    """Compact multi-line alert for streaming output."""
    first = finding.evidence[0] if finding.evidence else None
    where = f"{first.source_file}:{first.line_no}" if first else "—"
    ip = f" — {finding.ip}" if finding.ip else ""
    return (
        f"[{finding.finding_id}] {finding.severity.name}"
        f" — {finding.rule_id}{ip}\n"
        f"    {finding.title}. {finding.description}\n"
        f"    at {where}"
    )


def follow_logs(args: Any, config: RulesConfig) -> int:
    """Run the analyzer continuously; returns the process exit code."""
    followers = {p: _LineFollower(p) for p in args.files}
    line_no = {p: 0 for p in args.files}
    detectors = build_detectors(config)
    id_map: dict[tuple[Any, ...], str] = {}
    reported: set[tuple[Any, ...]] = set()
    next_id = 1

    def drain_events() -> list[LogEvent]:
        """Parse every complete line appended since the last drain."""
        events: list[LogEvent] = []
        for path, follower in followers.items():
            for line in follower.read_available():
                line_no[path] += 1
                if not line.strip():
                    continue
                events.append(
                    parse_line(
                        line,
                        source_file=str(path),
                        line_no=line_no[path],
                        default_year=config.default_year,
                        default_tz=config.default_tz,
                    )
                )
        return events

    def stabilize_ids(findings: list[Finding]) -> None:
        """Give each finding a stable F-### id keyed by its signature."""
        nonlocal next_id
        for finding in findings:
            sig = _signature(finding)
            if sig in id_map:
                finding.finding_id = id_map[sig]
            else:
                id_map[sig] = finding.finding_id = f"F-{next_id:03d}"
                next_id += 1

    def refresh() -> tuple[list[LogEvent], list[Finding]]:
        """Drain input, re-run detection, return (events, NEW findings)."""
        events = drain_events()
        for event in events:
            for detector in detectors:
                detector.feed(event)
        base = sort_findings(
            [f for d in detectors for f in d.finalize()]
        )
        stabilize_ids(base)  # correlation needs children to have ids first
        correlated = correlate_findings(
            base, window_seconds=config.correlation_window_seconds
        )
        stabilize_ids(correlated)
        new = [
            f
            for f in (*base, *correlated)
            if _signature(f) not in reported
        ]
        for f in new:
            reported.add(_signature(f))
        return events, sort_findings(new)

    def emit(alert: str) -> None:
        """Write one alert to stdout or the --output file (append mode)."""
        if out is not None:
            out.write(alert + "\n")
            out.flush()
        else:
            print(alert, flush=True)

    # -- phase 1: analyze current file contents, print the normal report --
    events, initial = refresh()
    stats = ReportStats(
        files=[str(p) for p in args.files],
        total_lines=sum(line_no.values()),
        parsed_events=sum(
            1 for e in events if e.kind is not EventKind.MALFORMED
        ),
        malformed_events=sum(
            1 for e in events if e.kind is EventKind.MALFORMED
        ),
    )
    rendered = (
        render_json(initial, stats)
        if args.format == "json"
        else render_text(initial, stats)
    )
    out: IO[str] | None = None
    if args.output:
        try:
            args.output.write_text(rendered, encoding="utf-8")
            out = args.output.open("a", encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot write report: {exc}", file=sys.stderr)
            for follower in followers.values():
                follower.close()
            return 2
    else:
        print(rendered, end="", flush=True)

    fail_on = Severity.from_name(args.fail_on) if args.fail_on else None
    if fail_on and any(f.severity >= fail_on for f in initial):
        for follower in followers.values():
            follower.close()
        return 1

    emit(f"watch: following {len(followers)} file(s) — Ctrl+C to stop")

    # -- phase 2: poll for appended lines, emit new findings as alerts --
    # New findings stream as compact alerts (text) or JSON lines (json).
    try:
        while True:
            for follower in followers.values():
                if follower.rotated():
                    follower.reopen()
            _, fresh = refresh()
            for finding in fresh:
                if args.format == "json":
                    emit(json.dumps(finding.to_dict()))
                else:
                    emit(_alert_text(finding))
                if fail_on and finding.severity >= fail_on:
                    emit(
                        f"watch: --fail-on {fail_on.name.lower()} "
                        "reached — exiting"
                    )
                    return 1
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        emit("watch: stopped by user")
        return 130
    finally:
        for follower in followers.values():
            follower.close()
        if out is not None:
            out.close()
