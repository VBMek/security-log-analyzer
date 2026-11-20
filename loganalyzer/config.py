"""Rule configuration: built-in defaults merged with an optional YAML file.

The analyzer works out of the box via ``DEFAULT_RULES``. When a rules file is
given (``--rules``) or ``rules.yaml`` exists in the working directory, its
values are deep-merged over the defaults, so partial overrides are allowed —
a user can change one threshold without copying the whole file.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import UTC, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

import yaml

from .models import Severity


class ConfigError(ValueError):
    """Raised when the rules configuration is missing or invalid."""


# Built-in defaults — mirrored by the shipped rules.yaml, which exists to be
# read and tuned; any key omitted there keeps the value defined here.
DEFAULT_RULES: dict[str, Any] = {
    "correlation": {
        # Max gap between the same IP's activity in different files for the
        # findings to be merged into one correlated incident.
        "window_seconds": 300,
        # Syslog timestamps carry neither year nor timezone; these are applied.
        "default_year": 2025,
        "default_utc_offset": "+00:00",
    },
    "detectors": {
        "brute_force": {
            "enabled": True,
            "severity": "HIGH",
            "success_severity": "CRITICAL",
            "failure_threshold": 4,
            "window_seconds": 300,
            "web_failure_statuses": [401, 403],
            "web_success_statuses": [200, 302],
            "login_paths": ["/login", "/signin", "/auth", "/wp-login.php"],
            # Invalid-user SSH failures are handled by ssh_invalid_user so the
            # two signal types stay distinct; set true to also count them here.
            "count_invalid_users": False,
        },
        "sql_injection": {
            "enabled": True,
            "severity": "HIGH",
            # Patterns match SQL *structure* — keywords, stacked statements,
            # comment sequences — never a lone quote, so inputs such as
            # "O'Brien" do not produce false positives.
            "patterns": [
                r"(?i)\bunion\b[\s\S]*\bselect\b",
                r"(?i)\b(select|insert|delete|update)\b[\s\S]*\b(from|into)\b",
                r"(?i)\b(drop|alter|truncate)\s+table\b",
                r"(?i);\s*(drop|delete|select|insert|update|exec)\b",
                r"(?i)\b(or|and)\b\s+[\w'\"]+\s*=\s*[\w'\"]+",
                r"('--|\"--|/\*|\*/)",
                # Time-based blind SQLi. The `\s*\(` requires function-call
                # syntax, so plain words ("sleep pills", "benchmark") stay clean.
                r"(?i)\b(sleep|benchmark|pg_sleep|dbms_lock\.sleep"
                r"|dbms_pipe\.receive_message)\s*\(",
                r"(?i)\bwaitfor\s+(delay|time)\b",
            ],
        },
        "path_traversal": {
            "enabled": True,
            "severity": "HIGH",
            "patterns": [
                r"\.\.[\\/]",   # ../ or ..\
                r"\.\.;",       # Tomcat-style ..; bypass
                r"/(etc/(passwd|shadow|hosts)|proc/|root/|boot\.ini|winnt|windows)",
            ],
        },
        "directory_scan": {
            "enabled": True,
            "severity": "MEDIUM",
            "distinct_paths": 4,
            "window_seconds": 60,
            "error_statuses": [400, 401, 403, 404],
            # Well-known sensitive targets — counted even when not errored.
            "probe_paths": [
                "/admin", "/administrator", "/wp-admin", "/wp-login.php",
                "/phpmyadmin", "/.env", "/.git", "/config.php", "/backup",
                "/server-status",
            ],
        },
        "request_rate_abuse": {
            "enabled": True,
            "severity": "MEDIUM",
            "max_requests": 4,
            "window_seconds": 5,
        },
        "ssh_invalid_user": {
            "enabled": True,
            "severity": "MEDIUM",
            "distinct_users": 3,
            "window_seconds": 300,
        },
        "suspicious_sudo": {
            "enabled": True,
            "severity": "HIGH",
            "sensitive_patterns": [
                r"/etc/(shadow|gshadow|sudoers|passwd)",
                r"\b(useradd|userdel|usermod|visudo|passwd)\b",
                r"\bchmod\s+(777|u\+s|4755)",
                r"\b(bash|sh|zsh|dash|su)\s*$|\b(bash|sh|zsh)\s+-[a-zA-Z]*i",
                r"\b(dd|nc|ncat|netcat|curl|wget)\b.*(/etc/|/root/)",
            ],
        },
        "malformed_line": {
            "enabled": True,
            "severity": "LOW",
        },
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` over ``base`` recursively; scalars and lists replace."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _parse_utc_offset(text: str) -> timezone:
    """Parse ``+HH:MM`` / ``-HH:MM`` / ``Z`` into a fixed timezone."""
    cleaned = text.strip().upper()
    if cleaned == "Z":
        return UTC
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", cleaned)
    if not match:
        raise ConfigError(
            f"Invalid UTC offset {text!r} (expected e.g. '+02:00' or 'Z')"
        )
    sign = 1 if match.group(1) == "+" else -1
    delta = timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))
    return timezone(sign * delta)


@dataclass(frozen=True)
class RulesConfig:
    """Typed accessors over the merged rules dictionary."""

    raw: dict[str, Any]

    def detector_options(self, rule_id: str) -> dict[str, Any]:
        """Full option mapping for one detector (empty dict if absent)."""
        detectors = self.raw.get("detectors", {})
        options = detectors.get(rule_id, {}) if isinstance(detectors, dict) else {}
        if not isinstance(options, dict):
            raise ConfigError(f"detectors.{rule_id} must be a mapping")
        return dict(options)

    def detector_enabled(self, rule_id: str) -> bool:
        return bool(self.detector_options(rule_id).get("enabled", True))

    def _correlation(self) -> dict[str, Any]:
        section = self.raw.get("correlation", {})
        if not isinstance(section, dict):
            raise ConfigError("'correlation' section must be a mapping")
        return section

    @property
    def correlation_window_seconds(self) -> int:
        return int(self._correlation().get("window_seconds", 300))

    @property
    def default_year(self) -> int:
        return int(self._correlation().get("default_year", 2025))

    @property
    def default_tz(self) -> tzinfo:
        return _parse_utc_offset(
            str(self._correlation().get("default_utc_offset", "+00:00"))
        )

    def validate(self) -> None:
        """Fail fast on unknown severity names or broken regexes."""
        detectors = self.raw.get("detectors", {})
        if not isinstance(detectors, dict):
            raise ConfigError("'detectors' section must be a mapping")
        for name, options in detectors.items():
            if not isinstance(options, dict):
                raise ConfigError(f"detectors.{name} must be a mapping")
            for key in ("severity", "success_severity"):
                if key in options:
                    try:
                        Severity.from_name(str(options[key]))
                    except ValueError as exc:
                        raise ConfigError(f"detectors.{name}.{key}: {exc}") from exc
            for key in ("patterns", "sensitive_patterns"):
                for pattern in options.get(key, []):
                    try:
                        re.compile(str(pattern))
                    except re.error as exc:
                        raise ConfigError(
                            f"detectors.{name}.{key}: bad regex {pattern!r}: {exc}"
                        ) from exc


def load_rules(path: Path | None = None) -> RulesConfig:
    """Load the effective configuration.

    Resolution order: explicit ``--rules`` path (missing file is an error) →
    ``./rules.yaml`` when present → built-in defaults only.
    """
    raw = copy.deepcopy(DEFAULT_RULES)
    resolved = path
    if resolved is None and Path("rules.yaml").is_file():
        resolved = Path("rules.yaml")
    if resolved is not None:
        if not resolved.is_file():
            raise ConfigError(f"Rules file not found: {resolved}")
        try:
            loaded = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"Cannot parse rules file {resolved}: {exc}") from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"Rules file {resolved} must contain a YAML mapping")
        raw = _deep_merge(raw, loaded)
    config = RulesConfig(raw)
    config.validate()
    return config
