"""Command-line interface for the security log analyzer.

    loganalyzer [--rules rules.yaml] [--format text|json] [--output FILE]
                [--fail-on SEVERITY] FILE [FILE ...]
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import ConfigError, load_rules
from .correlate import correlate_findings
from .detectors import build_detectors
from .models import EventKind, LogEvent, Severity
from .parsers import parse_file
from .report import (
    ReportStats,
    assign_ids,
    render_json,
    render_text,
    sort_findings,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loganalyzer",
        description="Analyze log files for suspicious security activity.",
        epilog="Exit codes: 0 = nothing suspicious found (or all findings "
        "below --fail-on), 1 = suspicious findings detected, "
        "2 = usage/configuration/input error.",
    )
    parser.add_argument(
        "files", metavar="FILE", type=Path, nargs="+",
        help="log file(s) to analyze",
    )
    parser.add_argument(
        "--rules", type=Path, default=None, metavar="PATH",
        help="YAML rules file (default: ./rules.yaml if present, "
        "else built-in defaults)",
    )
    parser.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="report format (default: text)",
    )
    parser.add_argument(
        "--output", type=Path, default=None, metavar="PATH",
        help="write the report to a file instead of stdout",
    )
    parser.add_argument(
        "--fail-on", type=str.lower, metavar="SEVERITY",
        choices=[s.name.lower() for s in Severity],
        help="only findings at or above this severity trigger exit code 1 "
        "(info|low|medium|high|critical; default: any finding triggers it)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_rules(args.rules)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for path in args.files:
        if not path.is_file():
            print(f"error: log file not found: {path}", file=sys.stderr)
            return 2

    # 1) Parse every file into the normalized event stream.
    events: list[LogEvent] = []
    try:
        for path in args.files:
            events.extend(
                parse_file(
                    path,
                    default_year=config.default_year,
                    default_tz=config.default_tz,
                )
            )
    except OSError as exc:
        print(f"error: cannot read log file: {exc}", file=sys.stderr)
        return 2

    # 2) Feed the stream through every enabled detector.
    detectors = build_detectors(config)
    for event in events:
        for detector in detectors:
            detector.feed(event)
    findings = [f for d in detectors for f in d.finalize()]

    # 3) Correlate findings across files (needs stable ids first).
    findings = sort_findings(findings)
    assign_ids(findings)
    correlated = correlate_findings(
        findings, window_seconds=config.correlation_window_seconds
    )
    assign_ids(correlated, start=len(findings) + 1)
    findings = sort_findings([*findings, *correlated])

    # 4) Render.
    stats = ReportStats(
        files=[str(p) for p in args.files],
        total_lines=len(events),
        parsed_events=sum(1 for e in events if e.kind is not EventKind.MALFORMED),
        malformed_events=sum(1 for e in events if e.kind is EventKind.MALFORMED),
    )
    rendered = (
        render_json(findings, stats)
        if args.format == "json"
        else render_text(findings, stats)
    )
    if args.output:
        try:
            args.output.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot write report: {exc}", file=sys.stderr)
            return 2
    else:
        print(rendered, end="")

    # Exit-code contract for CI: any finding at or above the --fail-on
    # threshold (default: any finding at all) fails the run.
    if args.fail_on is not None:
        limit = Severity.from_name(args.fail_on)
        failed = any(f.severity >= limit for f in findings)
    else:
        failed = bool(findings)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
