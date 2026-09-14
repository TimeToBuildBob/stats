#!/usr/bin/env python3
"""Collect Bob's private aggregates (brain repo) for data/ledger.csv, in two halves.

``--emit`` runs ON Bob's host (GitHub Actions cannot read the private brain repo):
it computes every month the ledger still accepts and writes a repository_dispatch
body. Only monthly integer totals leave the host: no repo names, titles, paths or
content.

``--apply`` runs in the collect workflow: it validates the dispatched payload
(private metrics, non-negative integers, sane months, nothing else) and records it
under the ledger's freeze rules. Frozen rows are never modified; a drift is logged
and the exit code is 3. Only private metric rows are touched.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import ledger as L

# Author names/emails Bob has committed under on the brain repo's master.
# Matched with --perl-regexp against "Name <email>"; name "Bob" covers
# bob@superuserlabs.org (canonical), bob@example.com (2025-10 misconfig),
# bob@gptme.org and one "Bob <test@test.com>". Erik, Alice and test-fixture
# identities (Test <test@test.com>, Test User, AI Assistant) are excluded.
BOB_AUTHOR_REGEX = "^Bob <"
BRAIN_REF = "master"

SESSION_REASONS = {"clean_exit", "nonzero_exit", "timeout"}
SESSION_MIN_SECONDS = 60
BAD_MODEL = re.compile(r"<synthetic>|\b(test|mock|unknown|fake|dummy)\b", re.IGNORECASE)


def brain_commits(brain: Path, month: str) -> tuple[int, str]:
    since = L.iso(L.month_start(month))
    until = L.iso(L.month_end(month) - timedelta(seconds=1))
    args = [
        "rev-list",
        "--count",
        "--perl-regexp",
        f"--author={BOB_AUTHOR_REGEX}",
        f"--since={since}",
        f"--until={until}",
        BRAIN_REF,
    ]
    out = subprocess.run(["git", "-C", str(brain), *args], check=True, capture_output=True, text=True)
    query = "git -C <brain> " + " ".join(a if " " not in a else f"'{a}'" for a in args)
    return int(out.stdout.strip()), query


def session_counts(brain: Path) -> dict[str, int]:
    """Monthly counts of real sessions from state/sessions/*.result.json."""
    seen: dict[str, str] = {}
    for p in glob.glob(str(brain / "state" / "sessions" / "*.result.json")):
        try:
            with open(p) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if not include_session(d):
            continue
        sid = d.get("session_id") or Path(p).name
        seen.setdefault(sid, d["completed_at"][:7])
    counts: dict[str, int] = defaultdict(int)
    for month in seen.values():
        counts[month] += 1
    return counts


def include_session(d: dict) -> bool:
    if not str(d.get("agent_id") or "").startswith("bob-"):
        return False
    if d.get("status") != "completed" or d.get("reason") not in SESSION_REASONS:
        return False
    if not d.get("completed_at"):
        return False
    if (d.get("duration_seconds") or 0) < SESSION_MIN_SECONDS:
        return False
    model = d.get("model")
    return model is None or not BAD_MODEL.search(str(model))


SESSIONS_QUERY = (
    "state/sessions/*.result.json where agent_id~^bob- and status=completed and "
    "reason in (clean_exit,nonzero_exit,timeout) and duration_seconds>=60 and "
    "model !~ <synthetic>|\\b(test|mock|unknown|fake|dummy)\\b; month(completed_at UTC); dedup session_id"
)


EVENT_TYPE = "private-aggregates"
MAX_QUERY_LEN = 1000


def compute_rows(brain: Path, led: L.Ledger, now, verify_frozen: bool = False) -> list[dict]:
    """Compute private rows for every month the ledger still accepts (missing or open)."""
    rows = []
    sessions = None
    for metric in L.PRIVATE_METRICS:
        for month in L.months_range(L.METRICS[metric]["first_month"], L.month_of(now)):
            if not led.needs_compute(metric, month) and not verify_frozen:
                continue
            if metric == "brain_commits":
                value, query = brain_commits(brain, month)
            elif metric == "sessions":
                if sessions is None:
                    sessions = session_counts(brain)
                value, query = sessions.get(month, 0), SESSIONS_QUERY
            else:  # pragma: no cover
                raise KeyError(metric)
            rows.append({"metric": metric, "month": month, "value": value, "query": query})
    return rows


def validate_rows(payload: object, now) -> list[dict]:
    """Validate a dispatched payload. Only private metrics with integer totals are accepted."""
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ValueError("payload must be an object with a 'rows' list")
    rows = []
    for r in payload["rows"]:
        if not isinstance(r, dict) or set(r) != {"metric", "month", "value", "query"}:
            raise ValueError(f"bad row shape: {r!r}")
        if r["metric"] not in L.PRIVATE_METRICS:
            raise ValueError(f"not a private metric: {r['metric']!r}")
        if not isinstance(r["month"], str):
            raise ValueError(f"bad month: {r['month']!r}")
        L.parse_month(r["month"])
        if r["month"] < L.METRICS[r["metric"]]["first_month"] or r["month"] > L.month_of(now):
            raise ValueError(f"month out of range for {r['metric']}: {r['month']}")
        if type(r["value"]) is not int or r["value"] < 0:
            raise ValueError(f"value must be a non-negative integer: {r['value']!r}")
        if not isinstance(r["query"], str) or len(r["query"]) > MAX_QUERY_LEN:
            raise ValueError("query must be a string of reasonable length")
        rows.append(r)
    return rows


def apply_rows(led: L.Ledger, rows: list[dict], now) -> int:
    """Record rows under the ledger's freeze rules. Returns the number of drift refusals."""
    drift = 0
    for r in rows:
        try:
            status = led.record(r["metric"], r["month"], r["value"], L.SOURCE_PRIVATE, r["query"], now)
        except L.FrozenRowError as e:
            L.log_drift(str(e), now)
            print(f"DRIFT: {e}", file=sys.stderr)
            drift += 1
            continue
        print(f"{r['metric']} {r['month']} = {r['value']} ({status})")
    return drift


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--emit", type=Path, help="(host) compute open months and write a repository_dispatch body")
    mode.add_argument("--apply", type=Path, help="(Actions) apply a dispatched client_payload to the ledger")
    ap.add_argument("--brain", type=Path, help="path to Bob's brain repo (required with --emit)")
    ap.add_argument("--verify-frozen", action="store_true", help="also recompute frozen months (drift is refused on apply)")
    args = ap.parse_args(argv)

    now = L.utcnow()
    led = L.Ledger.load()

    if args.emit:
        if not args.brain or not (args.brain / ".git").exists():
            print(f"--emit needs --brain pointing at a git repo, got {args.brain}", file=sys.stderr)
            return 2
        rows = compute_rows(args.brain.resolve(), led, now, args.verify_frozen)
        body = {"event_type": EVENT_TYPE, "client_payload": {"computed_at": L.iso(now), "rows": rows}}
        args.emit.write_text(json.dumps(body, indent=1) + "\n")
        for r in rows:
            print(f"{r['metric']} {r['month']} = {r['value']}")
        return 0

    rows = validate_rows(json.loads(args.apply.read_text()), now)
    drift = apply_rows(led, rows, now)
    led.save()
    return L.EXIT_FROZEN_DRIFT if drift else 0


if __name__ == "__main__":
    sys.exit(main())
