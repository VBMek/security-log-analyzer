"""Configuration tests: defaults, overrides, validation, file resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from loganalyzer.config import ConfigError, RulesConfig, load_rules
from loganalyzer.detectors import build_detectors


class TestLoadRules:
    def test_defaults_when_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # run from an empty directory so ./rules.yaml is not picked up
        monkeypatch.chdir(tmp_path)
        config = load_rules(None)
        assert config.detector_enabled("brute_force")
        assert config.correlation_window_seconds == 300

    def test_local_rules_yaml_auto_loaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "rules.yaml").write_text(
            "detectors:\n  request_rate_abuse:\n    max_requests: 99\n"
        )
        config = load_rules(None)
        assert (
            config.detector_options("request_rate_abuse")["max_requests"] == 99
        )
        # other defaults survive the deep-merge
        assert (
            config.detector_options("request_rate_abuse")["window_seconds"] == 5
        )

    def test_explicit_missing_file_is_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_rules(tmp_path / "does-not-exist.yaml")

    def test_disable_detector(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text(
            "detectors:\n  sql_injection:\n    enabled: false\n"
        )
        config = load_rules(rules_file)
        rule_ids = [d.rule_id for d in build_detectors(config)]
        assert "sql_injection" not in rule_ids
        assert "brute_force" in rule_ids

    def test_threshold_override(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text(
            "detectors:\n  brute_force:\n    failure_threshold: 2\n"
        )
        config = load_rules(rules_file)
        assert config.detector_options("brute_force")["failure_threshold"] == 2

    def test_invalid_severity_rejected(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text(
            "detectors:\n  path_traversal:\n    severity: WEIRD\n"
        )
        with pytest.raises(ConfigError, match="severity"):
            load_rules(rules_file)

    def test_invalid_regex_rejected(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text(
            "detectors:\n  sql_injection:\n    patterns: ['(unclosed']\n"
        )
        with pytest.raises(ConfigError, match="regex"):
            load_rules(rules_file)

    def test_non_mapping_file_rejected(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text("- just\n- a\n- list\n")
        with pytest.raises(ConfigError, match="mapping"):
            load_rules(rules_file)

    def test_shipped_rules_yaml_loads(self) -> None:
        # The repo-root rules.yaml must always be valid.
        config = load_rules(Path("rules.yaml"))
        assert isinstance(config, RulesConfig)

    def test_default_tz_parsing(self) -> None:
        config = RulesConfig(
            {"correlation": {"default_utc_offset": "+02:00"}}
        )
        offset = config.default_tz.utcoffset(None)
        assert offset is not None and offset.total_seconds() == 7200
        bad = RulesConfig({"correlation": {"default_utc_offset": "mars"}})
        with pytest.raises(ConfigError):
            _ = bad.default_tz


class TestYamlTypes:
    def test_shipped_yaml_is_a_mapping_with_detectors(self) -> None:
        raw = yaml.safe_load(Path("rules.yaml").read_text())
        assert isinstance(raw, dict)
        assert "detectors" in raw and "correlation" in raw
