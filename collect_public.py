#!/usr/bin/env python3
"""Collect Bob's public GitHub metrics into data/ledger.csv.

Uses the GitHub search API with GITHUB_TOKEN only. Only public metric rows are
ever touched; private (brain-host) rows pass through unchanged.

Frozen months are skipped (not re-queried) unless --verify-frozen is given, in
which case a changed frozen value is logged to data/drift.log and the script
exits non-zero without modifying the row.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import ledger as L

USER = "TimeToBuildBob"
API = "https://api.github.com"
# Search API allows 30 req/min authenticated; stay under it.
MIN_INTERVAL = 2.2
_last_call = 0.0


def query_for(metric: str, month: str) -> tuple[str, str]:
    """Return (endpoint, q) for a metric and month. Dates are inclusive, UTC."""
    a, b = f"{month}-01", L.last_day(month)
    qs = {
        "prs_merged_public": ("issues", f"author:{USER} is:pr is:merged is:public merged:{a}..{b}"),
        "prs_opened_public": ("issues", f"author:{USER} is:pr is:public created:{a}..{b}"),
        "issues_opened_public": ("issues", f"author:{USER} is:issue is:public created:{a}..{b}"),
        "commits_public_default_branch": ("commits", f"author:{USER} is:public committer-date:{a}..{b}"),
    }
    endpoint, q = qs[metric]
    if "is:public" not in q:  # the local token can see private repos
        raise AssertionError(f"query without is:public: {q}")
    return endpoint, q


def query_string(endpoint: str, q: str) -> str:
    return f"GET /search/{endpoint}?q={q}"


def search_count(endpoint: str, q: str, token: str, max_attempts: int = 8) -> int:
    global _last_call
    url = f"{API}/search/{endpoint}?" + urllib.parse.urlencode({"q": q, "per_page": 1})
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "TimeToBuildBob-stats",
    }
    for attempt in range(1, max_attempts + 1):
        wait = MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) or e.code >= 500:
                retry_after = e.headers.get("Retry-After")
                reset = e.headers.get("X-RateLimit-Reset")
                if retry_after:
                    delay = float(retry_after)
                elif reset and e.headers.get("X-RateLimit-Remaining") == "0":
                    delay = max(float(reset) - time.time(), 0) + 2
                else:
                    delay = min(60, 5 * attempt)
                print(f"  HTTP {e.code} on {q!r}; sleeping {delay:.0f}s (attempt {attempt})", file=sys.stderr)
                time.sleep(delay)
                continue
            raise
        if data.get("incomplete_results"):
            print(f"  incomplete_results for {q!r}; retrying (attempt {attempt})", file=sys.stderr)
            time.sleep(5 * attempt)
            continue
        return int(data["total_count"])
    raise RuntimeError(f"search failed after {max_attempts} attempts: {q}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verify-frozen", action="store_true", help="re-query frozen months and report drift")
    ap.add_argument("--metric", action="append", choices=L.PUBLIC_METRICS)
    args = ap.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 2

    now = L.utcnow()
    led = L.Ledger.load()
    drift = 0
    counts: dict[str, int] = {}
    for metric in args.metric or L.PUBLIC_METRICS:
        for month in L.months_range(L.METRICS[metric]["first_month"], L.month_of(now)):
            if not led.needs_compute(metric, month) and not args.verify_frozen:
                continue
            endpoint, q = query_for(metric, month)
            value = search_count(endpoint, q, token)
            try:
                status = led.record(metric, month, value, L.SOURCE_PUBLIC, query_string(endpoint, q), now)
            except L.FrozenRowError as e:
                L.log_drift(str(e), now)
                print(f"DRIFT: {e}", file=sys.stderr)
                drift += 1
                continue
            counts[status] = counts.get(status, 0) + 1
            print(f"{metric} {month} = {value} ({status})")
    led.save()
    print(f"done: {counts} drift={drift}")
    return L.EXIT_FROZEN_DRIFT if drift else 0


if __name__ == "__main__":
    sys.exit(main())
