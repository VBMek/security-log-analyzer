# loganalyzer — security log analysis tool

A command-line tool that reads one or more log files, detects suspicious
activity through configurable rules, correlates findings across files, and
prints an actionable report.

## Quick start

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .            # runtime dependency: PyYAML only

# analyze the bundled example logs
python -m loganalyzer examples/webserver.log examples/auth.log
# …or the installed console script
loganalyzer examples/webserver.log examples/auth.log
```

## Usage

```
loganalyzer [--rules rules.yaml] [--format text|json] [--output FILE]
            [--fail-on SEVERITY] FILE [FILE ...]
```

| Option | Meaning |
|---|---|
| `FILE…` | one or more log files (access-log and syslog formats are auto-detected per line) |
| `--rules PATH` | YAML rules file; defaults to `./rules.yaml` when present, else built-in defaults |
| `--format` | `text` (default) or `json` for SIEM/pipeline ingestion |
| `--output` | write the report to a file instead of stdout |
| `--fail-on` | exit `1` when any finding reaches the given severity (`info|low|medium|high|critical`) — useful in CI |

Exit codes: `0` clean, `1` `--fail-on` threshold reached, `2` usage/config error.

## Detection rules

All rules live in `rules.yaml` (deep-merged over built-in defaults) and can be
tuned or disabled without touching code:

| Rule | What it catches | Severity |
|---|---|---|
| `brute_force` | ≥ N auth failures per IP in a window — web logins **and** SSH; a success after the failures escalates | HIGH → CRITICAL |
| `sql_injection` | SQLi signatures in the (decoded) request URI; structural patterns only, so `O'Brien` is not flagged | HIGH |
| `path_traversal` | `../`, `..\`, `..;`, sensitive paths (`/etc/passwd`…) | HIGH |
| `directory_scan` | ≥ N distinct probed/erroring paths per IP in a window | MEDIUM |
| `request_rate_abuse` | > N requests per IP in a short window; HTTP 429s highlighted | MEDIUM |
| `ssh_invalid_user` | ≥ N distinct nonexistent usernames — account enumeration | MEDIUM |
| `suspicious_sudo` | sudo commands matching sensitive patterns (`/etc/shadow`, shells, account tools) | HIGH |
| `malformed_line` | lines matching no known format | LOW |
| `correlated_activity` | same IP triggers rules in ≥ 2 files within the correlation window | max of children |

## Architecture

```
files ──▶ parsers.py ──▶ LogEvent stream ──▶ detectors.py ──▶ correlate.py ──▶ report.py
           (per-format)     (normalized)      (rules.yaml)     (IP + window)
```

* **Normalized events** — both log formats map to one `LogEvent` type, so
  detectors and correlation are format-agnostic; a new format = one parser.
* **Streaming detectors** — `feed()`/`finalize()` API with per-IP sliding
  window state, sized for continuous high-volume processing.
* **Cross-file correlation** — syslog timestamps get `correlation.default_year`
  / `default_utc_offset` from the config so both sources share one timeline.

## Development & QA

```bash
pip install -r requirements-dev.txt
pytest            # unit + end-to-end tests
ruff check .      # lint
mypy              # type-check (strict for loganalyzer/)
```

## AI usage

This tool was developed with AI assistance (Devin CLI), as invited by the
task description. The conversation transcript is attached to the submission
separately.
