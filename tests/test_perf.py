"""Scale regression: planted attacks must be detected in ~120k lines.

Uses scripts/gen_access_log.py's ``build_events`` so this test exercises
exactly the same data as manual scale runs: ~120k normal lines, nine
attack bursts, four near-boundary decoys, three malformed lines.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from loganalyzer.cli import main
from scripts.gen_access_log import build_events

NORMAL_LINES = 120_000  # plus ~22k poller/attack/malformed lines on top

# Every planted attack as (rule_id, source_ip) — see attacks() in the
# generator; the three malformed lines carry no IP.
EXPECTED = sorted([
    ("brute_force", "198.51.100.10"),      # success after fails -> CRITICAL
    ("brute_force", "198.51.100.11"),      # GET /LOGIN — case-insensitive
    ("sql_injection", "198.51.100.12"),
    ("path_traversal", "198.51.100.14"),
    ("directory_scan", "198.51.100.13"),
    ("request_rate_abuse", "198.51.100.13"),
    ("directory_scan", "198.51.100.15"),   # slash/case evasion still caught
    ("request_rate_abuse", "198.51.100.15"),
    ("request_rate_abuse", "198.51.100.16"),
    ("malformed_line", None),
    ("malformed_line", None),
    ("malformed_line", None),
])

# Near-boundary decoys that must produce no findings at all.
DECOY_IPS = {"198.51.100.17", "198.51.100.18", "198.51.100.20", "10.200.0.1"}


@pytest.mark.slow
def test_planted_attacks_detected_at_scale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    events = build_events(NORMAL_LINES, seed=42)
    log = tmp_path / "scale.log"
    log.write_text("".join(f"{raw}\n" for _, raw in events))

    start = time.perf_counter()
    rc = main(["--format", "json", str(log)])
    elapsed = time.perf_counter() - start

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    # no line silently dropped; the three planted malformed lines surface
    assert payload["summary"]["parsed_events"] == len(events) - 3
    assert payload["summary"]["malformed_events"] == 3

    # exact match — every attack found, nothing extra from normal traffic
    got = sorted((f["rule_id"], f["ip"]) for f in payload["findings"])
    assert got == EXPECTED

    brute = {
        f["ip"]: f["severity"]
        for f in payload["findings"]
        if f["rule_id"] == "brute_force"
    }
    assert brute["198.51.100.10"] == "CRITICAL"   # escalation survived
    assert brute["198.51.100.11"] == "HIGH"

    assert not {f["ip"] for f in payload["findings"]} & DECOY_IPS

    # gross-regression guard, not a perf target (~8s on a laptop)
    print(
        f"\nanalyzed {len(events)} lines in {elapsed:.1f}s "
        f"({len(events) / elapsed:,.0f} lines/s)"
    )
    assert elapsed < 60
