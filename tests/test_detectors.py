"""Detector tests: one class per rule, including edge and FP cases."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from loganalyzer.config import DEFAULT_RULES
from loganalyzer.detectors import (
    BaseDetector,
    BruteForceDetector,
    DirectoryScanDetector,
    MalformedLineDetector,
    PathTraversalDetector,
    RequestRateAbuseDetector,
    SqlInjectionDetector,
    SshInvalidUserDetector,
    SuspiciousSudoDetector,
)
from loganalyzer.models import Finding, LogEvent, Severity

from .conftest import auth, web


def _options(rule_id: str, **overrides: Any) -> dict[str, Any]:
    opts = dict(DEFAULT_RULES["detectors"][rule_id])
    opts.update(overrides)
    return opts


def _run(detector: BaseDetector, events: Iterable[LogEvent]) -> list[Finding]:
    for event in events:
        detector.feed(event)
    return detector.finalize()


def _web_requests(ip: str, path: str, status: int, seconds: range) -> list[LogEvent]:
    """Build one access-log event per second-step for the given times."""
    return [
        web(
            f'{ip} - - [03/Jul/2025:10:00:{s:02d} +0000] '
            f'"GET {path} HTTP/1.1" {status} 100',
            line_no=s,
        )
        for s in seconds
    ]


def _ssh_failures(ip: str, seconds: range, user: str = "admin") -> list[LogEvent]:
    return [
        auth(
            f"Jul  3 10:00:{s:02d} server sshd[1]: Failed password for "
            f"{user} from {ip} port {1000 + s} ssh2",
            line_no=s,
        )
        for s in seconds
    ]


class TestBruteForce:
    def test_failures_only_is_high(self) -> None:
        det = BruteForceDetector(_options("brute_force"))
        findings = _run(det, _ssh_failures("10.0.0.50", range(3, 7)))
        assert len(findings) == 1
        assert findings[0].severity is Severity.HIGH
        assert findings[0].ip == "10.0.0.50"

    def test_success_after_failures_escalates_to_critical(self) -> None:
        det = BruteForceDetector(_options("brute_force"))
        events = [
            *_ssh_failures("10.0.0.50", range(3, 7)),
            auth(
                "Jul  3 10:00:07 server sshd[1]: Accepted password for admin "
                "from 10.0.0.50 port 52345 ssh2"
            ),
        ]
        findings = _run(det, events)
        assert len(findings) == 1
        assert findings[0].severity is Severity.CRITICAL
        assert "successful" in findings[0].title.lower()

    def test_below_threshold_is_quiet(self) -> None:
        det = BruteForceDetector(_options("brute_force"))
        assert _run(det, _ssh_failures("10.0.0.50", range(3, 6))) == []

    def test_http_login_failures_detected(self) -> None:
        det = BruteForceDetector(_options("brute_force"))
        events = [
            web(
                f'10.0.0.50 - - [03/Jul/2025:10:00:0{s} +0000] '
                '"POST /login HTTP/1.1" 401 54',
                line_no=s,
            )
            for s in range(3, 7)
        ] + [
            web(
                '10.0.0.50 - - [03/Jul/2025:10:00:07 +0000] '
                '"POST /login HTTP/1.1" 200 3842'
            )
        ]
        findings = _run(det, events)
        assert len(findings) == 1
        assert findings[0].severity is Severity.CRITICAL

    def test_non_login_paths_ignored(self) -> None:
        det = BruteForceDetector(_options("brute_force"))
        events = _web_requests("1.2.3.4", "/products", 401, range(0, 5))
        assert _run(det, events) == []

    def test_invalid_users_excluded_by_default(self) -> None:
        det = BruteForceDetector(_options("brute_force"))
        events = [
            auth(
                f"Jul  3 10:00:{s:02d} server sshd[1]: Failed password for "
                f"invalid user user{s} from 203.0.113.5 port {1000+s} ssh2"
            )
            for s in range(9, 13)
        ]
        assert _run(det, events) == []

    def test_invalid_users_counted_when_enabled(self) -> None:
        det = BruteForceDetector(
            _options("brute_force", count_invalid_users=True)
        )
        events = [
            auth(
                f"Jul  3 10:00:{s:02d} server sshd[1]: Failed password for "
                f"invalid user user{s} from 203.0.113.5 port {1000+s} ssh2"
            )
            for s in range(9, 13)
        ]
        assert len(_run(det, events)) == 1

    def test_success_before_failures_does_not_escalate(self) -> None:
        det = BruteForceDetector(_options("brute_force"))
        events = [
            auth(
                "Jul  3 09:59:00 server sshd[1]: Accepted password for admin "
                "from 10.0.0.50 port 1 ssh2"
            ),
            *_ssh_failures("10.0.0.50", range(3, 7)),
        ]
        findings = _run(det, events)
        assert findings[0].severity is Severity.HIGH


class TestSqlInjection:
    def _feed(self, path: str, ip: str = "10.0.0.88") -> LogEvent:
        return web(
            f'{ip} - - [03/Jul/2025:10:00:14 +0000] '
            f'"GET {path} HTTP/1.1" 200 54'
        )

    def test_union_select_detected(self) -> None:
        det = SqlInjectionDetector(_options("sql_injection"))
        findings = _run(
            det, [self._feed("/search?q=' UNION SELECT * FROM users--")]
        )
        assert len(findings) == 1
        assert findings[0].severity is Severity.HIGH

    def test_stacked_drop_table_detected(self) -> None:
        det = SqlInjectionDetector(_options("sql_injection"))
        findings = _run(det, [self._feed("/search?q=1; DROP TABLE users--")])
        assert len(findings) == 1

    def test_encoded_payload_detected(self) -> None:
        det = SqlInjectionDetector(_options("sql_injection"))
        findings = _run(
            det,
            [self._feed("/search?q=%27%20UNION%20SELECT%20*%20FROM%20users--")],
        )
        assert len(findings) == 1

    def test_obrien_false_positive_guard(self) -> None:
        det = SqlInjectionDetector(_options("sql_injection"))
        # A lone apostrophe inside a name must NOT raise a finding.
        findings = _run(
            det, [self._feed("/search?q=O'Brien", ip="192.168.1.14")]
        )
        assert findings == []

    def test_benign_search_ignored(self) -> None:
        det = SqlInjectionDetector(_options("sql_injection"))
        assert _run(det, [self._feed("/search?q=laptop")]) == []


class TestPathTraversal:
    def _feed(self, path: str, ip: str = "203.0.113.5") -> LogEvent:
        return web(
            f'{ip} - - [03/Jul/2025:10:00:12 +0000] '
            f'"GET {path} HTTP/1.1" 400 0'
        )

    def test_dotdot_and_etc_passwd_detected(self) -> None:
        det = PathTraversalDetector(_options("path_traversal"))
        findings = _run(det, [self._feed("/admin/../../../etc/passwd")])
        assert len(findings) == 1
        assert findings[0].severity is Severity.HIGH

    def test_encoded_traversal_detected(self) -> None:
        det = PathTraversalDetector(_options("path_traversal"))
        findings = _run(det, [self._feed("/%2e%2e/%2e%2e/etc/passwd")])
        assert len(findings) == 1

    def test_normal_path_ignored(self) -> None:
        det = PathTraversalDetector(_options("path_traversal"))
        assert _run(det, [self._feed("/products")]) == []


class TestDirectoryScan:
    def test_enumeration_detected(self) -> None:
        det = DirectoryScanDetector(_options("directory_scan"))
        paths = ["/admin", "/phpmyadmin", "/wp-admin", "/.env", "/config.php"]
        events = [
            web(
                f'172.16.0.20 - - [03/Jul/2025:10:00:18 +0000] '
                f'"GET {p} HTTP/1.1" 404 0',
                line_no=i,
            )
            for i, p in enumerate(paths)
        ]
        findings = _run(det, events)
        assert len(findings) == 1
        assert findings[0].severity is Severity.MEDIUM

    def test_below_threshold_is_quiet(self) -> None:
        det = DirectoryScanDetector(_options("directory_scan"))
        events = [
            web(
                f'172.16.0.20 - - [03/Jul/2025:10:00:18 +0000] '
                f'"GET {p} HTTP/1.1" 404 0',
                line_no=i,
            )
            for i, p in enumerate(["/a", "/b", "/c"])
        ]
        assert _run(det, events) == []

    def test_successful_browsing_ignored(self) -> None:
        det = DirectoryScanDetector(_options("directory_scan"))
        # 200s on ordinary paths are not probes.
        events = _web_requests("1.2.3.4", "/page", 200, range(0, 6))
        assert _run(det, events) == []


class TestRequestRateAbuse:
    def test_burst_detected(self) -> None:
        det = RequestRateAbuseDetector(_options("request_rate_abuse"))
        events = _web_requests("10.0.0.99", "/api/users", 200, range(23, 28))
        findings = _run(det, events)
        assert len(findings) == 1
        assert findings[0].severity is Severity.MEDIUM

    def test_at_limit_is_quiet(self) -> None:
        det = RequestRateAbuseDetector(_options("request_rate_abuse"))
        events = _web_requests("10.0.0.99", "/api/users", 200, range(23, 27))
        assert _run(det, events) == []

    def test_rate_limited_responses_noted(self) -> None:
        det = RequestRateAbuseDetector(_options("request_rate_abuse"))
        events = _web_requests("10.0.0.99", "/api/users", 429, range(23, 28))
        findings = _run(det, events)
        assert "429" in findings[0].description


class TestSshInvalidUser:
    def test_enumeration_detected(self) -> None:
        det = SshInvalidUserDetector(_options("ssh_invalid_user"))
        # Three distinct nonexistent usernames from one IP.
        events = [
            auth(
                "Jul  3 10:00:09 server sshd[1]: Failed password for "
                "invalid user test from 203.0.113.5 port 44123 ssh2"
            ),
            auth(
                "Jul  3 10:00:10 server sshd[1]: Failed password for "
                "invalid user root from 203.0.113.5 port 44124 ssh2"
            ),
            auth(
                "Jul  3 10:00:11 server sshd[1]: Failed password for "
                "invalid user ubuntu from 203.0.113.5 port 44125 ssh2"
            ),
        ]
        findings = _run(det, events)
        assert len(findings) == 1
        assert findings[0].severity is Severity.MEDIUM
        assert "ubuntu" in findings[0].description

    def test_same_user_repeated_is_quiet(self) -> None:
        det = SshInvalidUserDetector(_options("ssh_invalid_user"))
        events = [
            auth(
                f"Jul  3 10:00:0{s} server sshd[1]: Failed password for "
                f"invalid user test from 203.0.113.5 port {1000+s} ssh2"
            )
            for s in (9, 10, 11)
        ]
        # distinct_users=3 not met — same username each time
        assert _run(det, events) == []


class TestSuspiciousSudo:
    def test_shadow_read_flagged(self) -> None:
        det = SuspiciousSudoDetector(_options("suspicious_sudo"))
        events = [
            auth(
                "Jul  3 10:00:15 server sudo: johndoe : TTY=pts/0 ; "
                "PWD=/home/johndoe ; USER=root ; COMMAND=/bin/cat /etc/shadow"
            )
        ]
        findings = _run(det, events)
        assert len(findings) == 1
        assert findings[0].severity is Severity.HIGH

    def test_routine_restart_ignored(self) -> None:
        det = SuspiciousSudoDetector(_options("suspicious_sudo"))
        events = [
            auth(
                "Jul  3 10:00:20 server sudo: deploy : TTY=pts/1 ; "
                "PWD=/var/www ; USER=root ; "
                "COMMAND=/bin/systemctl restart nginx"
            )
        ]
        assert _run(det, events) == []


class TestMalformedLine:
    def test_malformed_event_becomes_finding(self) -> None:
        det = MalformedLineDetector(_options("malformed_line"))
        event = web("[MALFORMED ENTRY - system restart", line_no=26)
        findings = _run(det, [event])
        assert len(findings) == 1
        assert findings[0].severity is Severity.LOW
        assert findings[0].evidence[0].line_no == 26
