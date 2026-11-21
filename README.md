# loganalyzer — security log analysis tool

A command-line tool that reads one or more log files, detects suspicious
activity through configurable rules, correlates findings across files, and
prints an actionable report.

## Quick start

Requires Python 3.11+.

```bash
# 1) create and activate a virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# 2) install the tool (runtime dependency: PyYAML only)
pip install -e .

# 3) analyze the bundled example logs
loganalyzer examples/webserver.log examples/auth.log
# or equivalently:
python -m loganalyzer examples/webserver.log examples/auth.log
```

Expected: a report with 12 findings — a CRITICAL multi-file brute force
from `10.0.0.50`, SQLi from `10.0.0.88`, traversal + SSH user-enumeration
from `203.0.113.5`, a fast directory scan from `172.16.0.20`, a suspicious
`sudo cat /etc/shadow`, and more — plus `correlated_activity` incidents
tying the two files together. Exit code is `1` because findings were
detected.

## Usage

```
loganalyzer [--rules rules.yaml] [--format text|json] [--output FILE]
            [--fail-on SEVERITY] [--follow] [--poll-seconds SEC]
            FILE [FILE ...]
```

| Option | Meaning |
|---|---|
| `FILE…` | one or more log files (access-log and syslog formats are auto-detected per line) |
| `--rules PATH` | YAML rules file; defaults to `./rules.yaml` when present, else built-in defaults |
| `--format` | `text` (default) or `json` for SIEM/pipeline ingestion |
| `--output` | write the report to a file instead of stdout |
| `--fail-on` | only findings at or above this severity (`info|low|medium|high|critical`) trigger exit `1` — default: any finding triggers it |
| `-f`, `--follow` | keep watching for appended lines and stream new findings live (like `tail -f`) |
| `--poll-seconds` | follow-mode poll interval, default `1.0` |

Exit codes for CI usage:

| Code | Meaning |
|---|---|
| `0` | analysis ran, nothing suspicious found (or all findings below `--fail-on`) |
| `1` | suspicious findings detected (in `--follow`: `--fail-on` reached) |
| `2` | usage / configuration / input error (bad rules, missing or unreadable files) |
| `130` | `--follow` stopped via Ctrl+C |

## Follow mode (live monitoring)

```bash
loganalyzer --follow /var/log/nginx/access.log /var/log/auth.log
# watch-only-alerting at a severity threshold:
loganalyzer --follow --fail-on high /var/log/nginx/access.log
```

The tool first runs a normal analysis on the current file contents, then
keeps each file open (`tail -f` style) and streams **new** findings as
compact alerts — already-reported findings are deduplicated by signature,
while a severity escalation (e.g. HIGH → CRITICAL) still re-fires. With
`--format json` each alert is emitted as one JSON object per line (JSONL).

Follow mode is rotation-aware (inode/size change → reopen) and buffers
partial lines, so a line is only analyzed once it is complete.

## Detection rules

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

## Configuring rules

All rule behavior lives in YAML — shipped as `rules.yaml` at the repo root
(auto-loaded when running from there; otherwise `--rules PATH`). The file
is **deep-merged** over built-in defaults, so you only write the keys you
want to change.

A minimal override file:

```yaml
detectors:
  request_rate_abuse:
    max_requests: 20        # only flag really aggressive clients
  malformed_line:
    enabled: false          # silence a whole rule family
correlation:
  window_seconds: 120       # tighter cross-file correlation window
```

Common adjustments, by example:

```yaml
detectors:
  sql_injection:
    # add a signature — patterns match the percent-decoded URI
    patterns:
      - '(?i)\bxp_cmdshell\b'
      - '(?i)\bload_file\s*\('

  path_traversal:
    patterns:
      - '\.env'              # also flag direct .env grabs as traversal

  directory_scan:
    probe_paths:
      - "/internal-only"     # treat internal path probing as scanning

  suspicious_sudo:
    sensitive_patterns:
      - '\b(crontab|at)\b'   # persistence via scheduled jobs

  brute_force:
    failure_threshold: 3     # stricter: 3 failures already alert
    count_invalid_users: true
```

Writing your own *pattern* is the main extension point:

* `sql_injection.patterns`, `path_traversal.patterns` — Python regexes
  applied to the percent-decoded request URI.
* `suspicious_sudo.sensitive_patterns` — regexes applied to the sudo
  `COMMAND=` string.
* `directory_scan.probe_paths` — literal paths (not regex), normalized.

Everything else (thresholds, windows, status codes, severities,
`enabled` flags) is scalar tuning. New detector *types* require a small
class in `loganalyzer/detectors.py` registered in `DETECTOR_TYPES`.

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

## Testing

```bash
pip install -r requirements-dev.txt

pytest              # whole suite (79 tests, ~15s incl. the scale test)
pytest -m "not slow"  # skip the ~10s scale test, 78 tests in ~1s
pytest -v           # one line per test
pytest tests/test_detectors.py          # one module
pytest -k obrien                        # tests matching a name
```

Suite layout — one module per concern, all under `tests/`:

| File | Covers |
|---|---|
| `test_parsers.py` | both log formats, timestamps, malformed lines |
| `test_detectors.py` | every rule incl. thresholds and FP guards (e.g. `O'Brien`) |
| `test_correlate.py` | IP + time-window cross-file correlation |
| `test_config.py` | rules.yaml loading, overrides, validation errors |
| `test_cli.py` | end-to-end runs, JSON output, exit codes |
| `test_follow.py` | live `--follow` alerting via subprocess |
| `test_perf.py` | scale regression: all planted attacks found in ~140k lines, zero FPs (marked `slow`) |

**Reading failures:** pytest prints the failing assert plus captured
output — e.g. `never saw 'watch: following' — output was: …` dumps the
child's stdout for follow tests. A green run ends with
`79 passed in ~15s`.

QA gates (all must pass before committing):

```bash
pytest            # tests
ruff check .      # lint
mypy              # strict type-check for loganalyzer/
```

## AI usage

This tool was developed with AI assistance (Devin CLI), as invited by the
task description. The conversation transcript is attached to the submission
separately.
