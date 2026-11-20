"""Core data types shared by parsers, detectors, correlator and reporters."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class Severity(enum.IntEnum):
    """Ordered severity level — a higher numeric value means more severe."""

    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @classmethod
    def from_name(cls, name: str) -> Severity:
        """Resolve a severity from its config-file name (case-insensitive)."""
        try:
            return cls[str(name).strip().upper()]
        except KeyError as exc:
            raise ValueError(f"Unknown severity level: {name!r}") from exc


class EventKind(enum.Enum):
    """Normalized categories that every parsed log line fits into."""

    HTTP_REQUEST = "http_request"   # one entry from a web access log
    SSH_AUTH = "ssh_auth"           # sshd authentication attempt
    SUDO = "sudo"                   # sudo command invocation
    OTHER = "other"                 # parsed line with no security meaning
    MALFORMED = "malformed"         # line no parser could understand


@dataclass(frozen=True)
class LogEvent:
    """One normalized log line — the shared vocabulary of the whole pipeline.

    Parsers fill only the fields relevant to their format; the rest stay
    ``None``. ``kind == MALFORMED`` marks lines that matched no parser, so
    they flow through the pipeline and get reported instead of dropped.
    """

    kind: EventKind
    raw: str
    source_file: str
    line_no: int
    timestamp: datetime | None = None
    ip: str | None = None
    # HTTP request fields
    method: str | None = None
    path: str | None = None
    status: int | None = None
    # SSH authentication fields
    user: str | None = None
    auth_success: bool | None = None
    invalid_user: bool = False
    # sudo fields
    sudo_command: str | None = None


@dataclass(frozen=True)
class Evidence:
    """A log line backing a finding, with its location for auditability."""

    source_file: str
    line_no: int
    raw: str

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.source_file, "line": self.line_no, "raw": self.raw}


@dataclass
class Finding:
    """A detected security-relevant observation.

    ``finding_id`` is assigned during reporting (``F-001`` …); ``related``
    holds ids of other findings, used by cross-file correlation.
    """

    rule_id: str
    severity: Severity
    title: str
    description: str
    ip: str | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    evidence: list[Evidence] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    finding_id: str = ""

    @property
    def source_files(self) -> list[str]:
        """Distinct files this finding's evidence was drawn from."""
        return sorted({e.source_file for e in self.evidence})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.finding_id,
            "rule_id": self.rule_id,
            "severity": self.severity.name,
            "title": self.title,
            "description": self.description,
            "ip": self.ip,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "evidence": [e.to_dict() for e in self.evidence],
            "related": self.related,
        }
