"""Cross-file correlation: one source IP active in several files.

An IP that triggers rules in two or more different log files within the
configured correlation window is a stronger signal than any single finding —
it points at a coordinated, multi-vector attack.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from .models import Evidence, Finding


def _file_windows(group: list[Finding]) -> dict[str, tuple[datetime, datetime]]:
    """Per-file (earliest, latest) activity across the group's findings."""
    windows: dict[str, tuple[datetime, datetime]] = {}
    for finding in group:
        if finding.first_seen is None:
            continue
        hi = finding.last_seen or finding.first_seen
        for source in finding.source_files:
            if source in windows:
                lo, prev_hi = windows[source]
                windows[source] = (min(lo, finding.first_seen), max(prev_hi, hi))
            else:
                windows[source] = (finding.first_seen, hi)
    return windows


def correlate_findings(
    findings: list[Finding], *, window_seconds: int
) -> list[Finding]:
    """Emit ``correlated_activity`` findings for multi-file attacker IPs.

    ``findings`` must already carry ``finding_id``s so the correlated
    finding can reference its children via ``related``.
    """
    by_ip: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        if finding.ip is not None:
            by_ip[finding.ip].append(finding)

    window = timedelta(seconds=window_seconds)
    correlated: list[Finding] = []
    for ip, group in by_ip.items():
        windows = _file_windows(group)
        if len(windows) < 2:
            continue  # activity confined to a single file — nothing to correlate

        # Correlate when at least two files' activity windows overlap or are
        # separated by at most `window`.
        items = sorted(windows.items())
        close = any(
            a_lo <= b_hi + window and b_lo <= a_hi + window
            for i, (_, (a_lo, a_hi)) in enumerate(items)
            for _, (b_lo, b_hi) in items[i + 1 :]
        )
        if not close:
            continue

        rules = sorted({f.rule_id for f in group})
        # Representative evidence: one line per child finding, de-duplicated
        # and completed so every involved file appears at least once.
        evidence: list[Evidence] = []
        covered: set[str] = set()
        for f in group:
            for e in f.evidence[:1]:
                if (e.source_file, e.line_no) not in {
                    (x.source_file, x.line_no) for x in evidence
                }:
                    evidence.append(e)
                covered.add(e.source_file)
        for f in group:
            for e in f.evidence:
                if e.source_file not in covered:
                    evidence.append(e)
                    covered.add(e.source_file)
                    break
        correlated.append(
            Finding(
                rule_id="correlated_activity",
                severity=max(f.severity for f in group),
                title="Correlated activity across multiple log files",
                description=(
                    f"{ip} triggered [{', '.join(rules)}] in "
                    f"{len(windows)} files ({', '.join(sorted(windows))}) "
                    f"within a {window_seconds}s window"
                    " — likely a coordinated attack."
                ),
                ip=ip,
                first_seen=min(lo for lo, _ in windows.values()),
                last_seen=max(hi for _, hi in windows.values()),
                evidence=evidence[:6],
                related=[f.finding_id for f in group if f.finding_id],
            )
        )
    return correlated
