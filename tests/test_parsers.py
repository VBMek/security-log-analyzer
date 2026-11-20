"""Parser tests: both formats, timestamps, and malformed-line handling."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

from loganalyzer.models import EventKind
from loganalyzer.parsers import parse_file

from .conftest import auth, web


class TestAccessLogParser:
    def test_parses_all_fields(self) -> None:
        event = web(
            '192.168.1.10 - - [03/Jul/2025:10:00:01 +0000] '
            '"GET /index.html HTTP/1.1" 200 1234'
        )
        assert event.kind is EventKind.HTTP_REQUEST
        assert event.ip == "192.168.1.10"
        assert event.method == "GET"
        assert event.path == "/index.html"
        assert event.status == 200
        assert event.timestamp is not None
        assert event.timestamp.year == 2025
        # timezone from the log is preserved
        assert event.timestamp.utcoffset() == UTC.utcoffset(None)

    def test_dash_size_is_accepted(self) -> None:
        event = web(
            '10.0.0.1 - - [03/Jul/2025:10:00:01 +0000] '
            '"GET /x HTTP/1.1" 404 -'
        )
        assert event.status == 404

    def test_query_string_kept_in_path(self) -> None:
        event = web(
            "10.0.0.1 - - [03/Jul/2025:10:00:01 +0000] "
            '"GET /search?q=laptop HTTP/1.1" 200 1'
        )
        assert event.path == "/search?q=laptop"

    def test_non_access_line_returns_none(self) -> None:
        event = web("[MALFORMED ENTRY - system restart")
        assert event.kind is EventKind.MALFORMED


class TestSyslogParser:
    def test_failed_password(self) -> None:
        event = auth(
            "Jul  3 10:00:03 server sshd[1234]: Failed password for admin "
            "from 10.0.0.50 port 52341 ssh2"
        )
        assert event.kind is EventKind.SSH_AUTH
        assert event.auth_success is False
        assert event.invalid_user is False
        assert event.user == "admin"
        assert event.ip == "10.0.0.50"

    def test_failed_password_invalid_user(self) -> None:
        event = auth(
            "Jul  3 10:00:09 server sshd[1235]: Failed password for invalid "
            "user test from 203.0.113.5 port 44123 ssh2"
        )
        assert event.auth_success is False
        assert event.invalid_user is True
        assert event.user == "test"

    def test_accepted_password_and_publickey(self) -> None:
        for method in ("password", "publickey"):
            event = auth(
                f"Jul  3 10:00:07 server sshd[1]: Accepted {method} for "
                "deploy from 192.168.1.100 port 39281 ssh2"
            )
            assert event.kind is EventKind.SSH_AUTH
            assert event.auth_success is True
            assert event.user == "deploy"

    def test_sudo_command(self) -> None:
        event = auth(
            "Jul  3 10:00:15 server sudo: johndoe : TTY=pts/0 ; "
            "PWD=/home/johndoe ; USER=root ; COMMAND=/bin/cat /etc/shadow"
        )
        assert event.kind is EventKind.SUDO
        assert event.user == "johndoe"
        assert event.sudo_command == "/bin/cat /etc/shadow"

    def test_connection_closed_is_other_with_ip(self) -> None:
        event = auth(
            "Jul  3 10:00:25 server sshd[1245]: Connection closed by "
            "10.0.0.50 port 52345 [preauth]"
        )
        assert event.kind is EventKind.OTHER
        assert event.ip == "10.0.0.50"

    def test_syslog_timestamp_gets_default_year_and_tz(self) -> None:
        event = auth(
            "Jul  3 08:30:00 host sshd[1]: Accepted password for u "
            "from 1.2.3.4 port 1 ssh2"
        )
        assert event.timestamp is not None
        assert event.timestamp.year == 2025
        assert event.timestamp.month == 7
        assert event.timestamp.day == 3
        assert event.timestamp.tzinfo == UTC

    def test_garbage_line_is_malformed(self) -> None:
        event = auth("this is not a syslog line at all {{{")
        assert event.kind is EventKind.MALFORMED
        assert event.timestamp is None


class TestParseFile:
    def test_example_files(self) -> None:
        web_events = list(
            parse_file(
                Path("examples/webserver.log"),
                default_year=2025, default_tz=UTC,
            )
        )
        auth_events = list(
            parse_file(
                Path("examples/auth.log"),
                default_year=2025, default_tz=UTC,
            )
        )
        assert len(web_events) == 33
        assert len(auth_events) == 12
        malformed = [e for e in web_events if e.kind is EventKind.MALFORMED]
        assert len(malformed) == 1
        assert malformed[0].line_no == 26
        assert "MALFORMED ENTRY" in malformed[0].raw

    def test_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "mini.log"
        f.write_text(
            "Jul  3 10:00:00 h sshd[1]: Accepted password for u "
            "from 1.2.3.4 port 1 ssh2\n\n\n"
        )
        assert len(list(parse_file(f, default_year=2025, default_tz=UTC))) == 1
