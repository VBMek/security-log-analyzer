"""Report rendering: human-readable text and machine-readable JSON."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .models import Finding, Severity

# How many evidence lines are shown per finding in the text report.
MAX_EVIDENCE_LINES = 8


@dataclass(frozen=True)
class ReportStats:
    """Header inputs describing what was analyzed."""

    files: list[str]
    total_lines: int
    parsed_events: int
    malformed_events: int


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Highest severity first; ties broken by earliest activity, then rule."""
    no_ts = datetime.max.replace(tzinfo=UTC)
    return sorted(
        findings,
        key=lambda f: (-int(f.severity), f.first_seen or no_ts, f.rule_id),
    )


def assign_ids(findings: list[Finding], *, start: int = 1) -> None:
    """Assign stable human-friendly ids (``F-001``, ``F-002``, …)."""
    for number, finding in enumerate(findings, start):
        finding.finding_id = f"F-{number:03d}"


def _iso(ts: datetime | None) -> str:
    return ts.isoformat(sep=" ", timespec="seconds") if ts else "—"


def render_text(findings: list[Finding], stats: ReportStats) -> str:
    """Render the findings report as plain text for the terminal."""
    counts = {s: 0 for s in Severity}
    for finding in findings:
        counts[finding.severity] += 1
    summary = (
        "  ".join(
            f"{s.name}:{counts[s]}" for s in reversed(list(Severity)) if counts[s]
        )
        or "none"
    )

    lines = [
        "=" * 72,
        "  SECURITY LOG ANALYSIS REPORT",
        "=" * 72,
        f"Files analyzed  : {len(stats.files)} ({', '.join(stats.files)})",
        f"Lines processed : {stats.total_lines} "
        f"({stats.parsed_events} parsed, {stats.malformed_events} malformed)",
        f"Findings        : {len(findings)}",
        f"Severity summary: {summary}",
    ]
    if not findings:
        lines += ["", "No suspicious activity detected."]
        return "\n".join(lines) + "\n"

    for finding in findings:
        lines += [
            "-" * 72,
            f"[{finding.finding_id}] {finding.severity.name} — {finding.rule_id}",
            f"    {finding.title}",
            f"    {finding.description}",
        ]
        if finding.ip:
            lines.append(f"    Source IP : {finding.ip}")
        if finding.first_seen:
            span = _iso(finding.first_seen)
            if finding.last_seen and finding.last_seen != finding.first_seen:
                span += f"  →  {_iso(finding.last_seen)}"
            lines.append(f"    Window    : {span}")
        if finding.source_files:
            lines.append(f"    Sources   : {', '.join(finding.source_files)}")
        if finding.evidence:
            lines.append("    Evidence  :")
            for item in finding.evidence[:MAX_EVIDENCE_LINES]:
                lines.append(f"      {item.source_file}:{item.line_no}  {item.raw}")
            hidden = len(finding.evidence) - MAX_EVIDENCE_LINES
            if hidden > 0:
                lines.append(f"      … and {hidden} more")
        if finding.related:
            lines.append(f"    Related   : {', '.join(finding.related)}")
    return "\n".join(lines) + "\n"


def render_json(findings: list[Finding], stats: ReportStats) -> str:
    """Render the findings report as JSON for SIEM/pipeline ingestion."""
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity.name] = counts.get(finding.severity.name, 0) + 1
    payload: dict[str, Any] = {
        "summary": {
            "files": stats.files,
            "total_lines": stats.total_lines,
            "parsed_events": stats.parsed_events,
            "malformed_events": stats.malformed_events,
            "findings": len(findings),
            "by_severity": counts,
        },
        "findings": [f.to_dict() for f in findings],
    }
    return json.dumps(payload, indent=2) + "\n"
