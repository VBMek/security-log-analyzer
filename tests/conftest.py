"""Shared pytest fixtures and helpers for building log events."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest

from loganalyzer.config import DEFAULT_RULES, RulesConfig
from loganalyzer.models import LogEvent
from loganalyzer.parsers import parse_line

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
DEFAULT_TZ = UTC


@pytest.fixture()
def rules() -> RulesConfig:
    """The built-in default configuration."""
    return RulesConfig(dict(DEFAULT_RULES))


def web(
    line: str, source: str = "webserver.log", line_no: int = 1
) -> LogEvent:
    """Parse a synthetic access-log line (fails loudly if it doesn't parse)."""
    event = parse_line(
        line, source_file=source, line_no=line_no,
        default_year=2025, default_tz=DEFAULT_TZ,
    )
    return event


def auth(
    line: str, source: str = "auth.log", line_no: int = 1
) -> LogEvent:
    """Parse a synthetic syslog line (fails loudly if it doesn't parse)."""
    event = parse_line(
        line, source_file=source, line_no=line_no,
        default_year=2025, default_tz=DEFAULT_TZ,
    )
    return event
