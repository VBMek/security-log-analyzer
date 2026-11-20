"""Cross-file correlation tests (source IP + time window)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from loganalyzer.correlate import correlate_findings
from loganalyzer.models import Evidence, Finding, Severity

T0 = datetime(2025, 7, 3, 10, 0, 0, tzinfo=UTC)


def _finding(
    rule: str, ip: str | None, source: str, when: datetime,
    severity: Severity = Severity.HIGH, fid: str = "",
) -> Finding:
    return Finding(
        rule_id=rule,
        severity=severity,
        title="t",
        description="d",
        ip=ip,
        first_seen=when,
        last_seen=when,
        evidence=[Evidence(source_file=source, line_no=1, raw="x")],
        finding_id=fid,
    )


class TestCorrelate:
    def test_same_ip_two_files_within_window(self) -> None:
        findings = [
            _finding("brute_force", "10.0.0.50", "web.log", T0, fid="F-001"),
            _finding(
                "ssh_invalid_user", "10.0.0.50", "auth.log",
                T0 + timedelta(seconds=60), fid="F-002",
            ),
        ]
        out = correlate_findings(findings, window_seconds=300)
        assert len(out) == 1
        corr = out[0]
        assert corr.rule_id == "correlated_activity"
        assert corr.ip == "10.0.0.50"
        assert corr.severity is Severity.HIGH
        assert set(corr.related) == {"F-001", "F-002"}
        assert set(corr.source_files) == {"auth.log", "web.log"}

    def test_severity_is_max_of_children(self) -> None:
        findings = [
            _finding(
                "a", "1.2.3.4", "web.log", T0, severity=Severity.MEDIUM,
                fid="F-001",
            ),
            _finding(
                "b", "1.2.3.4", "auth.log", T0, severity=Severity.CRITICAL,
                fid="F-002",
            ),
        ]
        out = correlate_findings(findings, window_seconds=300)
        assert out[0].severity is Severity.CRITICAL

    def test_single_file_only_not_correlated(self) -> None:
        findings = [
            _finding("a", "1.2.3.4", "web.log", T0),
            _finding("b", "1.2.3.4", "web.log", T0 + timedelta(seconds=10)),
        ]
        assert correlate_findings(findings, window_seconds=300) == []

    def test_outside_window_not_correlated(self) -> None:
        findings = [
            _finding("a", "1.2.3.4", "web.log", T0),
            _finding(
                "b", "1.2.3.4", "auth.log", T0 + timedelta(seconds=600)
            ),
        ]
        assert correlate_findings(findings, window_seconds=300) == []

    def test_finding_without_ip_ignored(self) -> None:
        findings = [_finding("suspicious_sudo", None, "auth.log", T0)]
        assert correlate_findings(findings, window_seconds=300) == []

    def test_distinct_ips_not_merged(self) -> None:
        findings = [
            _finding("a", "1.1.1.1", "web.log", T0),
            _finding("b", "2.2.2.2", "auth.log", T0),
        ]
        assert correlate_findings(findings, window_seconds=300) == []
