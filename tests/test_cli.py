"""End-to-end CLI tests on the bundled example logs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loganalyzer.cli import main

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
WEB = str(EXAMPLES / "webserver.log")
AUTH = str(EXAMPLES / "auth.log")


class TestEndToEnd:
    def test_expected_findings_on_examples(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([WEB, AUTH]) == 0
        out = capsys.readouterr().out
        # every mandated detection shows up in the report
        for rule in (
            "brute_force", "sql_injection", "path_traversal",
            "directory_scan", "request_rate_abuse", "ssh_invalid_user",
            "suspicious_sudo", "malformed_line", "correlated_activity",
        ):
            assert rule in out, f"missing rule in report: {rule}"
        # the two multi-file attackers are correlated
        assert "10.0.0.50" in out
        assert "203.0.113.5" in out

    def test_json_output_is_parseable(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--format", "json", WEB, AUTH]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"]["files"] == [WEB, AUTH]
        assert payload["summary"]["malformed_events"] == 1
        rule_ids = {f["rule_id"] for f in payload["findings"]}
        assert "brute_force" in rule_ids
        assert "correlated_activity" in rule_ids
        # brute force escalated: success after failures => CRITICAL
        brute = [f for f in payload["findings"] if f["rule_id"] == "brute_force"]
        assert any(f["severity"] == "CRITICAL" for f in brute)

    def test_single_file_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([AUTH]) == 0
        out = capsys.readouterr().out
        assert "ssh_invalid_user" in out
        # no cross-file correlation possible with one file
        assert "correlated_activity" not in out

    def test_output_to_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "report.txt"
        assert main(["--output", str(target), WEB]) == 0
        assert "SECURITY LOG ANALYSIS REPORT" in target.read_text()
        assert capsys.readouterr().out == ""   # nothing on stdout

    def test_fail_on_exit_codes(self) -> None:
        assert main(["--fail-on", "critical", WEB, AUTH]) == 1
        assert main(["--fail-on", "low", WEB, AUTH]) == 1

    def test_fail_on_clean_input(self, tmp_path: Path) -> None:
        clean = tmp_path / "clean.log"
        clean.write_text(
            '1.2.3.4 - - [03/Jul/2025:10:00:01 +0000] '
            '"GET /index.html HTTP/1.1" 200 1234\n'
        )
        assert main(["--fail-on", "low", str(clean)]) == 0

    def test_missing_input_file(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["/nonexistent/nope.log"]) == 2
        assert "not found" in capsys.readouterr().err

    def test_missing_rules_file(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--rules", "/nonexistent/x.yaml", WEB]) == 2
        assert "not found" in capsys.readouterr().err

    def test_rules_override_changes_results(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rules = tmp_path / "rules.yaml"
        rules.write_text(
            "detectors:\n  request_rate_abuse:\n    enabled: false\n"
        )
        assert main(["--rules", str(rules), WEB]) == 0
        assert "request_rate_abuse" not in capsys.readouterr().out
