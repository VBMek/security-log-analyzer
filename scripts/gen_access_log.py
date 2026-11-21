#!/usr/bin/env python3
"""Generate a synthetic access log for scale/FP testing.

Normal traffic is generated from a seeded RNG; a fixed set of attack
bursts (and a few near-boundary decoys that must stay quiet) is planted
at deterministic timestamps. Usage:

    python scripts/gen_access_log.py -o scale.log -n 200000 --seed 42
"""

from __future__ import annotations

import argparse
import random
from datetime import UTC, datetime, timedelta

DAY = datetime(2025, 7, 3, tzinfo=UTC)

# Benign request surface. Deliberately excludes probe_paths (/admin, /.env,
# /wp-admin ...) so ordinary traffic cannot trigger directory_scan.
BENIGN_PATHS = [
    "/", "/index.html", "/products", "/about", "/contact", "/dashboard",
    "/search?q=laptop", "/search?q=shoes", "/search?q=O'Brien",
    "/api/users/me", "/api/items", "/cart", "/checkout", "/logout",
    "/static/app.css", "/static/app.js", "/static/logo.png",
    "/blog/hello-world", "/blog/release-notes", "/docs/api",
]
# Weighted statuses for normal traffic (kept low on 404s so random bursts
# of errored distinct paths stay below the directory_scan threshold).
STATUSES = [200] * 88 + [304] * 6 + [301] * 3 + [404] * 2 + [500]


def fmt(ts: datetime) -> str:
    return ts.strftime("%d/%b/%Y:%H:%M:%S +0000")


def line(ip: str, ts: datetime, method: str, path: str,
         status: int, size: int) -> tuple[datetime, str]:
    return ts, (
        f'{ip} - - [{fmt(ts)}] "{method} {path} HTTP/1.1" {status} {size}'
    )


def attacks() -> list[tuple[datetime, str]]:
    """Deterministic attack bursts; returns (expected findings, lines)."""
    ev: list[tuple[datetime, str]] = []
    t = DAY.replace(hour=3, minute=14)

    def add(ip: str, at: datetime, method: str, path: str,
            status: int, size: int = 100) -> None:
        ev.append(line(ip, at, method, path, status, size))

    # Brute force with success -> CRITICAL
    for i in range(5):
        add("198.51.100.10", t + timedelta(seconds=2 * i), "POST", "/login", 401)
    add("198.51.100.10", t + timedelta(seconds=10), "POST", "/login", 200)
    # Uppercase login path -> HIGH (case-insensitive matching)
    t = DAY.replace(hour=4, minute=22)
    for i in range(5):
        add("198.51.100.11", t + timedelta(seconds=3 * i), "GET", "/LOGIN", 401)
    # SQL injection incl. time-based -> HIGH
    t = DAY.replace(hour=5, minute=40)
    sqli = [
        "/search?q=' UNION SELECT * FROM users--",
        "/search?q=' OR '1'='1",
        "/search?q=1 AND SLEEP(5)",
        "/search?q=1; WAITFOR DELAY '0:0:5'",
    ]
    for i, p in enumerate(sqli):
        add("198.51.100.12", t + timedelta(seconds=2 * i), "GET", p, 200)
    # Fast directory scan -> directory_scan + request_rate_abuse
    t = DAY.replace(hour=7, minute=11)
    scan = ["/admin", "/wp-admin", "/phpmyadmin", "/.env",
            "/.git/config", "/config.php", "/backup", "/server-status"]
    for i, p in enumerate(scan):
        add("198.51.100.13", t + timedelta(milliseconds=800 * i),
            "GET", p, 404, 0)
    # Path traversal incl. encoded -> HIGH
    t = DAY.replace(hour=9, minute=33)
    for i, p in enumerate(
        ["/../../etc/passwd", "/%2e%2e/%2e%2e/etc/passwd", "/static/..%5c..%5cwin"]
    ):
        add("198.51.100.14", t + timedelta(seconds=i), "GET", p, 400, 0)
    # Slash/case evasion scan -> still flagged (4 distinct after folding)
    t = DAY.replace(hour=11, minute=5)
    for i, p in enumerate(["/admin", "/admin/", "/Admin", "/wp-admin", "/.env"]):
        add("198.51.100.15", t + timedelta(milliseconds=500 * i),
            "GET", p, 403, 0)
    # Rate abuse -> MEDIUM
    t = DAY.replace(hour=13, minute=20)
    for i in range(30):
        add("198.51.100.16", t + timedelta(milliseconds=150 * i),
            "GET", "/api/data", 200)
    # --- decoys: must NOT produce findings ---
    # exactly max_requests=4 inside the window (boundary)
    t = DAY.replace(hour=15, minute=45)
    for i in range(4):
        add("198.51.100.17", t + timedelta(seconds=i), "GET", "/api/ping", 200)
    # brute force one below threshold
    t = DAY.replace(hour=17, minute=30)
    for i in range(3):
        add("198.51.100.18", t + timedelta(seconds=2 * i), "POST", "/login", 401)
    # /admin + /admin/ + 2 typos -> 3 distinct after folding, quiet
    t = DAY.replace(hour=19, minute=0)
    for i, p in enumerate(["/admin", "/admin/", "/typo1", "/typo2"]):
        add("198.51.100.20", t + timedelta(seconds=2 * i), "GET", p, 404, 0)
    return ev


def build_events(n_lines: int, seed: int) -> list[tuple[datetime, str]]:
    """Build the whole log as sorted (timestamp, raw_line) events.

    Shared by the CLI below and tests/test_perf.py so scale tests exercise
    exactly the same planted attacks and decoys as manual runs.
    """
    rng = random.Random(seed)  # noqa: S311 - test data, not crypto

    events = attacks()
    # Legit monitoring poller: /health every 4s all day (dense but legal).
    t = DAY
    while t < DAY + timedelta(days=1):
        events.append(line("10.200.0.1", t, "GET", "/health", 200, 12))
        t += timedelta(seconds=4)

    # Normal traffic: ~1200 IPs, short sessions of 1-5 requests spaced
    # >=6s (stays under the 5s rate window) with ~15% 404/5xx noise.
    ips = [
        f"{rng.choice(['10', '172.16', '192.168', '85', '91', '104'])}"
        f".{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
        for _ in range(1200)
    ]
    produced = 0
    while produced < n_lines:
        ip = rng.choice(ips)
        t = DAY + timedelta(seconds=rng.uniform(0, 86399))
        for _ in range(rng.randint(1, 5)):
            if produced >= n_lines or t >= DAY + timedelta(days=1):
                break
            status = rng.choice(STATUSES)
            path = (
                rng.choice(["/typo", "/old-page", "/favicon.ico"])
                if status == 404
                else rng.choice(BENIGN_PATHS)
            )
            events.append(line(ip, t, rng.choice(["GET", "GET", "POST"]),
                               path, status, rng.randint(50, 90_000)))
            t += timedelta(seconds=rng.uniform(6, 40))
            produced += 1

    # A few malformed lines scattered through the day.
    for i, hour in enumerate((6, 14, 21)):
        events.append((
            DAY.replace(hour=hour, minute=7, second=13),
            f"[MALFORMED ENTRY - truncated write {i}",
        ))

    events.sort(key=lambda e: e[0])
    return events


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("-n", "--lines", type=int, default=200_000,
                    help="number of normal-traffic lines (default 200000)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    events = build_events(args.lines, args.seed)
    with open(args.output, "w") as fh:
        fh.writelines(raw + "\n" for _, raw in events)
    print(f"wrote {len(events)} lines to {args.output}")


if __name__ == "__main__":
    main()
