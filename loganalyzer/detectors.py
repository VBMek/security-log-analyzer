"""Detection rules.

Each detector consumes the normalized event stream through ``feed()`` and
produces findings in ``finalize()``. Keeping per-source-IP state instead of
whole files mirrors how the tool would run in a continuous pipeline: events
in, alerts out — and memory stays proportional to per-IP activity, not file
size.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote_plus, urlsplit

from .models import EventKind, Evidence, Finding, LogEvent, Severity

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _evidence(event: LogEvent) -> Evidence:
    """Wrap an event as finding evidence carrying its file location."""
    return Evidence(
        source_file=event.source_file, line_no=event.line_no, raw=event.raw
    )


def _ts(event: LogEvent) -> datetime:
    """Event timestamp; events lacking one sort to the very bottom."""
    return event.timestamp or datetime.min.replace(tzinfo=UTC)


def _cap_events(events: list[LogEvent], head: int = 3, tail: int = 2) -> list[LogEvent]:
    """Reduce an event list to its first ``head`` and last ``tail`` entries."""
    if len(events) <= head + tail:
        return events
    return events[:head] + events[-tail:]


def _decoded_path(uri: str) -> str:
    """Path component of a request URI, percent-decoded (``+`` becomes space)."""
    return unquote_plus(urlsplit(uri).path)


def _prune_old(events: list[LogEvent], keep_seconds: int) -> list[LogEvent]:
    """Drop events older than ``keep_seconds`` behind the newest one.

    Bounds per-IP memory during long follow-mode runs; keeping 2x the
    detection window preserves enough context for windowed matching.
    """
    if not events:
        return events
    cutoff = max(_ts(e) for e in events) - timedelta(seconds=keep_seconds)
    return [e for e in events if _ts(e) >= cutoff]


def _densest_window(times: list[datetime], window_seconds: int) -> tuple[int, int]:
    """Largest number of timestamps inside any ``window_seconds`` window.

    Two-pointer slide over ``times`` (must be sorted ascending);
    returns ``(count, start_index)`` of the densest window.
    """
    best_count, best_start, left = 0, 0, 0
    for right, current in enumerate(times):
        while (current - times[left]).total_seconds() > window_seconds:
            left += 1
        if right - left + 1 > best_count:
            best_count, best_start = right - left + 1, left
    return best_count, best_start


def _span(events: list[LogEvent]) -> tuple[datetime | None, datetime | None]:
    """(earliest, latest) timestamps of the given events."""
    if not events:
        return None, None
    return _ts(min(events, key=_ts)), _ts(max(events, key=_ts))


class BaseDetector:
    """Common interface: ``feed()`` every event, ``finalize()`` to collect."""

    rule_id = "base"

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        self.severity = Severity.from_name(str(options.get("severity", "MEDIUM")))

    def feed(self, event: LogEvent) -> None:
        raise NotImplementedError

    def finalize(self) -> list[Finding]:
        return []


# ---------------------------------------------------------------------------
# Authentication attacks
# ---------------------------------------------------------------------------


class BruteForceDetector(BaseDetector):
    """Repeated authentication failures from one source IP.

    Covers both HTTP login endpoints and SSH authentication. When a
    *successful* login from the same IP lands inside the window after the
    failures, severity escalates to ``success_severity`` — the attack
    probably worked.
    """

    rule_id = "brute_force"

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.failure_threshold = int(options.get("failure_threshold", 4))
        self.window = int(options.get("window_seconds", 300))
        self.web_failure_statuses = {
            int(s) for s in options.get("web_failure_statuses", [401, 403])
        }
        self.web_success_statuses = {
            int(s) for s in options.get("web_success_statuses", [200, 302])
        }
        # Lowercased: some stacks (IIS, some frameworks) treat paths
        # case-insensitively, and "/LOGIN" must not evade detection.
        self.login_paths = {
            str(p).rstrip("/").lower()
            for p in options.get("login_paths", ["/login"])
        }
        self.count_invalid_users = bool(options.get("count_invalid_users", False))
        self.success_severity = Severity.from_name(
            str(options.get("success_severity", "CRITICAL"))
        )
        self._failures: dict[str, list[LogEvent]] = defaultdict(list)
        self._successes: dict[str, list[LogEvent]] = defaultdict(list)

    def _is_login_path(self, uri: str | None) -> bool:
        if not uri:
            return False
        # Compare only the path component; query strings are irrelevant here.
        return (_decoded_path(uri).rstrip("/").lower() or "/") in self.login_paths

    def feed(self, event: LogEvent) -> None:
        if event.ip is None or event.timestamp is None:
            return
        if event.kind is EventKind.HTTP_REQUEST and self._is_login_path(event.path):
            if event.status in self.web_failure_statuses:
                self._failures[event.ip].append(event)
            elif event.status in self.web_success_statuses:
                self._successes[event.ip].append(event)
        elif event.kind is EventKind.SSH_AUTH and event.auth_success is not None:
            if event.auth_success:
                self._successes[event.ip].append(event)
            elif self.count_invalid_users or not event.invalid_user:
                self._failures[event.ip].append(event)
            # invalid-user failures otherwise stay with ssh_invalid_user

    def finalize(self) -> list[Finding]:
        findings: list[Finding] = []
        for ip in list(self._failures):
            self._failures[ip] = _prune_old(self._failures[ip], self.window * 2)
            self._successes[ip] = _prune_old(
                self._successes.get(ip, []), self.window * 2
            )
        for ip, fails in self._failures.items():
            fails.sort(key=_ts)
            count, start = _densest_window([_ts(e) for e in fails], self.window)
            if count < self.failure_threshold:
                continue
            burst = fails[start : start + count]
            first, last = _ts(burst[0]), _ts(burst[-1])
            # A success within the window after the failures => escalation.
            deadline = last + timedelta(seconds=self.window)
            succeeded = sorted(
                (
                    s
                    for s in self._successes.get(ip, [])
                    if s.timestamp is not None and first <= s.timestamp <= deadline
                ),
                key=_ts,
            )
            users = sorted({e.user for e in [*burst, *succeeded] if e.user})
            desc = (
                f"{count} failed authentication attempts within {self.window}s"
                + (f" against user(s): {', '.join(users)}." if users else ".")
            )
            title = "Brute-force authentication attempts"
            if succeeded:
                title = "Brute-force attack followed by successful login"
                desc += (
                    " A successful login from the same IP followed"
                    " — credentials likely compromised."
                )
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=self.success_severity if succeeded else self.severity,
                    title=title,
                    description=desc,
                    ip=ip,
                    first_seen=first,
                    last_seen=_ts(succeeded[-1]) if succeeded else last,
                    evidence=[_evidence(e) for e in _cap_events(burst)]
                    + [_evidence(s) for s in succeeded],
                )
            )
        return findings


class SshInvalidUserDetector(BaseDetector):
    """SSH logins for many distinct nonexistent usernames — enumeration."""

    rule_id = "ssh_invalid_user"

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.threshold = int(options.get("distinct_users", 3))
        self.window = int(options.get("window_seconds", 300))
        self._attempts: dict[str, list[LogEvent]] = defaultdict(list)

    def feed(self, event: LogEvent) -> None:
        if (
            event.kind is EventKind.SSH_AUTH
            and event.invalid_user
            and event.ip
            and event.timestamp
        ):
            self._attempts[event.ip].append(event)

    def finalize(self) -> list[Finding]:
        findings: list[Finding] = []
        for ip in self._attempts:
            self._attempts[ip] = _prune_old(self._attempts[ip], self.window * 2)
        for ip, events in self._attempts.items():
            ordered = sorted(events, key=_ts)
            best = _densest_distinct(ordered, self.window, lambda e: e.user or "")
            users = sorted({e.user or "" for e in best})
            if len(users) < self.threshold:
                continue
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=self.severity,
                    title="SSH invalid-user enumeration",
                    description=(
                        f"{len(users)} distinct nonexistent usernames tried "
                        f"within {self.window}s: {', '.join(users)}."
                    ),
                    ip=ip,
                    first_seen=_ts(best[0]),
                    last_seen=_ts(best[-1]),
                    evidence=[_evidence(e) for e in _cap_events(best)],
                )
            )
        return findings


class SuspiciousSudoDetector(BaseDetector):
    """sudo invocations matching sensitive command patterns.

    Reading credential stores, spawning shells, or changing accounts via sudo
    are typical privilege-escalation / post-compromise moves; routine service
    administration (e.g. ``systemctl restart nginx``) stays quiet.
    """

    rule_id = "suspicious_sudo"

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.patterns = [
            re.compile(p) for p in options.get("sensitive_patterns", [])
        ]
        self._matches: list[tuple[LogEvent, str]] = []

    def feed(self, event: LogEvent) -> None:
        if event.kind is not EventKind.SUDO or not event.sudo_command:
            return
        hit = next(
            (p for p in self.patterns if p.search(event.sudo_command)), None
        )
        if hit:
            self._matches.append((event, hit.pattern))

    def finalize(self) -> list[Finding]:
        return [
            Finding(
                rule_id=self.rule_id,
                severity=self.severity,
                title="Suspicious sudo command",
                description=(
                    f"User '{event.user}' ran sudo {event.sudo_command!r}"
                    f" (matched /{pattern}/)."
                ),
                first_seen=event.timestamp,
                last_seen=event.timestamp,
                evidence=[_evidence(event)],
            )
            for event, pattern in self._matches
        ]


# ---------------------------------------------------------------------------
# Web attacks
# ---------------------------------------------------------------------------


class _UriPatternDetector(BaseDetector):
    """Shared base: flag HTTP requests whose decoded URI matches patterns."""

    rule_id = "uri_pattern"
    title = "Suspicious request"

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.patterns = [re.compile(p) for p in options.get("patterns", [])]
        self._hits: dict[str, list[LogEvent]] = defaultdict(list)

    def feed(self, event: LogEvent) -> None:
        if (
            event.kind is not EventKind.HTTP_REQUEST
            or event.ip is None
            or not event.path
        ):
            return
        # Match on the decoded URI so encoded payloads cannot slip through.
        decoded = unquote_plus(event.path)
        if any(p.search(decoded) for p in self.patterns):
            self._hits[event.ip].append(event)

    def finalize(self) -> list[Finding]:
        findings: list[Finding] = []
        for ip, events in self._hits.items():
            first, last = _span(events)
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=self.severity,
                    title=self.title,
                    description=(
                        f"{len(events)} request(s) matched {self.title.lower()}"
                        " signatures."
                    ),
                    ip=ip,
                    first_seen=first,
                    last_seen=last,
                    evidence=[_evidence(e) for e in _cap_events(events)],
                )
            )
        return findings


class SqlInjectionDetector(_UriPatternDetector):
    """SQL-injection signatures in request URIs.

    Patterns deliberately target SQL *structure* — keywords, stacked
    statements, comment sequences — rather than lone quotes, so inputs like
    ``O'Brien`` do not produce false positives.
    """

    rule_id = "sql_injection"
    title = "SQL injection attempt"


class PathTraversalDetector(_UriPatternDetector):
    """Directory-traversal sequences and sensitive system paths in URIs."""

    rule_id = "path_traversal"
    title = "Path traversal attempt"


def _densest_distinct(
    ordered: list[LogEvent], window_seconds: int, key: Any
) -> list[LogEvent]:
    """Window (<= ``window_seconds``) containing the most distinct ``key``s.

    ``ordered`` must be sorted by timestamp. Per-IP windows are small, so
    recomputing the distinct set per step is cheap enough.
    """
    best: list[LogEvent] = []
    left = 0
    for right, event in enumerate(ordered):
        while (_ts(event) - _ts(ordered[left])).total_seconds() > window_seconds:
            left += 1
        candidate = ordered[left : right + 1]
        if len({key(e) for e in candidate}) > len({key(e) for e in best}):
            best = candidate
    return best


class DirectoryScanDetector(BaseDetector):
    """Probing of many distinct paths — directory/endpoint enumeration.

    A request counts as a probe when it was rejected (4xx class configured in
    ``error_statuses``) or targeted a well-known sensitive path
    (``probe_paths``). ``distinct_paths`` distinct probes inside the window
    raise a finding.
    """

    rule_id = "directory_scan"

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.threshold = int(options.get("distinct_paths", 4))
        self.window = int(options.get("window_seconds", 60))
        self.error_statuses = {
            int(s) for s in options.get("error_statuses", [400, 401, 403, 404])
        }
        self.probe_paths = {
            self._norm(str(p)) for p in options.get("probe_paths", [])
        }
        self._probes: dict[str, list[LogEvent]] = defaultdict(list)

    @staticmethod
    def _norm(uri: str) -> str:
        """Normalized path for matching against ``probe_paths``."""
        return _decoded_path(uri).lower().rstrip("/") or "/"

    @staticmethod
    def _path_key(event: LogEvent) -> str:
        """Distinctness key: decoded path, query excluded, ``/`` folded.

        ``/admin`` and ``/admin/`` address the same resource, so they count
        once — otherwise a trailing-slash redirect would inflate the probe
        count. Case is preserved: some servers treat ``/Admin`` and
        ``/admin`` as different resources, so merging them could undercount.
        """
        return _decoded_path(event.path or "").rstrip("/") or "/"

    def feed(self, event: LogEvent) -> None:
        if (
            event.kind is not EventKind.HTTP_REQUEST
            or event.ip is None
            or event.timestamp is None
            or not event.path
        ):
            return
        if (
            event.status in self.error_statuses
            or self._norm(event.path) in self.probe_paths
        ):
            self._probes[event.ip].append(event)

    def finalize(self) -> list[Finding]:
        findings: list[Finding] = []
        for ip in self._probes:
            self._probes[ip] = _prune_old(self._probes[ip], self.window * 2)
        for ip, events in self._probes.items():
            best = _densest_distinct(
                sorted(events, key=_ts), self.window, self._path_key
            )
            paths = {self._path_key(e) for e in best}
            if len(paths) < self.threshold:
                continue
            shown = ", ".join(sorted(paths)[:6])
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=self.severity,
                    title="Directory / endpoint scanning",
                    description=(
                        f"{len(paths)} distinct paths probed within "
                        f"{self.window}s: {shown}"
                        + (" …" if len(paths) > 6 else "")
                    ),
                    ip=ip,
                    first_seen=_ts(best[0]),
                    last_seen=_ts(best[-1]),
                    evidence=[_evidence(e) for e in _cap_events(best)],
                )
            )
        return findings


class RequestRateAbuseDetector(BaseDetector):
    """More than ``max_requests`` HTTP requests per IP inside the window.

    HTTP 429 responses inside the burst are highlighted — the server was
    already telling the client to slow down.
    """

    rule_id = "request_rate_abuse"

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.max_requests = int(options.get("max_requests", 4))
        self.window = int(options.get("window_seconds", 5))
        self._requests: dict[str, list[LogEvent]] = defaultdict(list)

    def feed(self, event: LogEvent) -> None:
        if (
            event.kind is EventKind.HTTP_REQUEST
            and event.ip
            and event.timestamp
        ):
            self._requests[event.ip].append(event)

    def finalize(self) -> list[Finding]:
        findings: list[Finding] = []
        for ip in self._requests:
            self._requests[ip] = _prune_old(self._requests[ip], self.window * 2)
        for ip, events in self._requests.items():
            ordered = sorted(events, key=_ts)
            count, start = _densest_window(
                [_ts(e) for e in ordered], self.window
            )
            if count <= self.max_requests:
                continue
            burst = ordered[start : start + count]
            throttled = [e for e in burst if e.status == 429]
            desc = (
                f"{count} requests within {self.window}s "
                f"(limit {self.max_requests})."
            )
            if throttled:
                desc += (
                    f" Server already rate-limited {len(throttled)}"
                    " response(s) (HTTP 429)."
                )
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    severity=self.severity,
                    title="HTTP request rate abuse",
                    description=desc,
                    ip=ip,
                    first_seen=_ts(burst[0]),
                    last_seen=_ts(burst[-1]),
                    evidence=[_evidence(e) for e in _cap_events(burst)],
                )
            )
        return findings


# ---------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------


class MalformedLineDetector(BaseDetector):
    """Lines no parser could understand — corruption or tampering signal."""

    rule_id = "malformed_line"

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self._lines: list[LogEvent] = []

    def feed(self, event: LogEvent) -> None:
        if event.kind is EventKind.MALFORMED:
            self._lines.append(event)

    def finalize(self) -> list[Finding]:
        return [
            Finding(
                rule_id=self.rule_id,
                severity=self.severity,
                title="Malformed log line",
                description=(
                    f"Line {event.line_no} in {event.source_file}"
                    " could not be parsed."
                ),
                evidence=[_evidence(event)],
            )
            for event in self._lines
        ]


# Registry: rule id -> detector implementation.
DETECTOR_TYPES: dict[str, type[BaseDetector]] = {
    "brute_force": BruteForceDetector,
    "sql_injection": SqlInjectionDetector,
    "path_traversal": PathTraversalDetector,
    "directory_scan": DirectoryScanDetector,
    "request_rate_abuse": RequestRateAbuseDetector,
    "ssh_invalid_user": SshInvalidUserDetector,
    "suspicious_sudo": SuspiciousSudoDetector,
    "malformed_line": MalformedLineDetector,
}


def build_detectors(config: Any) -> list[BaseDetector]:
    """Instantiate every detector enabled in the configuration."""
    return [
        cls(config.detector_options(rule_id))
        for rule_id, cls in DETECTOR_TYPES.items()
        if config.detector_enabled(rule_id)
    ]
