"""Month-frozen, append-only ledger for Bob's lifetime stats.

One row per (metric, month). Rules, enforced here and covered by tests:

* A row may be recomputed only while it is not frozen.
* A month freezes once ``now >= month_end + 3 days`` (month_end = first instant
  of the next month, UTC).
* A frozen row is never modified. Recording a different value (or query) for a
  frozen row raises :class:`FrozenRowError`; collectors log that and exit
  non-zero instead of rewriting history.
* A definition change gets a new metric name (e.g. ``prs_merged_public_v2``).
* Lifetime = sum over all months of a metric.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

FIELDS = ["metric", "month", "value", "frozen", "source", "query", "computed_at"]
FREEZE_GRACE = timedelta(days=3)
LEDGER_PATH = Path(__file__).parent / "data" / "ledger.csv"
DRIFT_LOG = Path(__file__).parent / "data" / "drift.log"

# Exit code collectors use when a frozen row would have changed (drift logged).
EXIT_FROZEN_DRIFT = 3

SOURCE_PUBLIC = "github-search"
SOURCE_PRIVATE = "brain-host"

METRICS: dict[str, dict] = {
    "prs_merged_public": {
        "scope": "public",
        "source": SOURCE_PUBLIC,
        "first_month": "2024-11",
        "label": "Merged public PRs",
        "definition": "PRs authored by TimeToBuildBob in public repos, by merge month (GitHub issue search).",
    },
    "prs_opened_public": {
        "scope": "public",
        "source": SOURCE_PUBLIC,
        "first_month": "2024-11",
        "label": "Opened public PRs",
        "definition": "PRs authored by TimeToBuildBob in public repos, by creation month (any final state).",
    },
    "issues_opened_public": {
        "scope": "public",
        "source": SOURCE_PUBLIC,
        "first_month": "2024-11",
        "label": "Opened public issues",
        "definition": "Issues authored by TimeToBuildBob in public repos, by creation month.",
    },
    "commits_public_default_branch": {
        "scope": "public",
        "source": SOURCE_PUBLIC,
        "first_month": "2024-11",
        "label": "Public commits (default branches)",
        "definition": (
            "Commits authored by TimeToBuildBob in public repos by committer date (GitHub commit search). "
            "Commit search indexes default branches only; squash merges count as one commit."
        ),
    },
    "brain_commits": {
        "scope": "private",
        "source": SOURCE_PRIVATE,
        "first_month": "2025-08",
        "label": "Brain repo commits (private)",
        "definition": (
            "Commits on master of Bob's private brain repo whose author name is 'Bob' "
            "(bob@superuserlabs.org, plus a few early/misconfigured Bob identities), by committer date. "
            "Starts 2025-08: earlier Bob work was committed under Erik's identity and cannot be separated. "
            "Excludes Erik, other agents, and test-fixture identities (Test <test@test.com> etc.)."
        ),
    },
    "sessions": {
        "scope": "private",
        "source": SOURCE_PRIVATE,
        "first_month": "2026-06",
        "label": "Agent sessions (private, approximate)",
        "definition": (
            "Bob agent sessions that ran to a result record (state/sessions/*.result.json): "
            "agent_id 'bob-*', status 'completed' with reason clean_exit/nonzero_exit/timeout and "
            "duration >= 60s, by completed_at month, deduplicated by session_id. Excludes "
            "lock/coordination skips and sub-minute startup failures (never did work), watchdog "
            "stubs, and '<synthetic>'/test/mock/unknown models. "
            "Starts 2026-06: result records begin 2026-05-20, so 2026-05 is incomplete and omitted."
        ),
    },
}

PUBLIC_METRICS = [m for m, spec in METRICS.items() if spec["scope"] == "public"]
PRIVATE_METRICS = [m for m, spec in METRICS.items() if spec["scope"] == "private"]


class FrozenRowError(Exception):
    """A recompute would change a frozen row."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_month(month: str) -> tuple[int, int]:
    year, mon = month.split("-")
    y, m = int(year), int(mon)
    if not 1 <= m <= 12 or len(month) != 7:
        raise ValueError(f"bad month: {month!r}")
    return y, m


def month_start(month: str) -> datetime:
    y, m = parse_month(month)
    return datetime(y, m, 1, tzinfo=timezone.utc)


def next_month(month: str) -> str:
    y, m = parse_month(month)
    return f"{y + 1:04d}-01" if m == 12 else f"{y:04d}-{m + 1:02d}"


def month_end(month: str) -> datetime:
    """First instant of the following month (exclusive end)."""
    return month_start(next_month(month))


def last_day(month: str) -> str:
    return (month_end(month) - timedelta(days=1)).strftime("%Y-%m-%d")


def freeze_at(month: str) -> datetime:
    return month_end(month) + FREEZE_GRACE


def is_freezable(month: str, now: datetime) -> bool:
    return now >= freeze_at(month)


def month_of(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m")


def months_range(first: str, last: str) -> list[str]:
    out, cur = [], first
    while cur <= last:
        out.append(cur)
        cur = next_month(cur)
    return out


def iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Row:
    metric: str
    month: str
    value: int
    frozen: bool
    source: str
    query: str
    computed_at: str

    def to_csv(self) -> dict[str, str]:
        return {
            "metric": self.metric,
            "month": self.month,
            "value": str(self.value),
            "frozen": "true" if self.frozen else "false",
            "source": self.source,
            "query": self.query,
            "computed_at": self.computed_at,
        }


class Ledger:
    def __init__(self, rows: list[Row] | None = None):
        self.rows: dict[tuple[str, str], Row] = {}
        for r in rows or []:
            key = (r.metric, r.month)
            if key in self.rows:
                raise ValueError(f"duplicate ledger row: {key}")
            self.rows[key] = r

    @classmethod
    def load(cls, path: Path | None = None) -> Ledger:
        path = path or LEDGER_PATH
        if not path.exists():
            return cls()
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != FIELDS:
                raise ValueError(f"unexpected ledger header: {reader.fieldnames}")
            rows = []
            for d in reader:
                if d["frozen"] not in ("true", "false"):
                    raise ValueError(f"bad frozen flag in row: {d}")
                parse_month(d["month"])
                rows.append(
                    Row(
                        metric=d["metric"],
                        month=d["month"],
                        value=int(d["value"]),
                        frozen=d["frozen"] == "true",
                        source=d["source"],
                        query=d["query"],
                        computed_at=d["computed_at"],
                    )
                )
        return cls(rows)

    def save(self, path: Path | None = None) -> None:
        path = path or LEDGER_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".csv.tmp")
        with tmp.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
            w.writeheader()
            for key in sorted(self.rows):
                w.writerow(self.rows[key].to_csv())
        tmp.replace(path)

    def get(self, metric: str, month: str) -> Row | None:
        return self.rows.get((metric, month))

    def needs_compute(self, metric: str, month: str) -> bool:
        """True unless the row exists and is frozen."""
        row = self.get(metric, month)
        return row is None or not row.frozen

    def record(
        self,
        metric: str,
        month: str,
        value: int,
        source: str,
        query: str,
        now: datetime,
    ) -> str:
        """Record a computed value. Returns 'inserted'|'updated'|'froze'|'unchanged'.

        Raises FrozenRowError if the row is frozen and value/query differ.
        """
        if metric not in METRICS:
            raise KeyError(f"unknown metric {metric!r}; definition changes need a registered new name")
        if METRICS[metric]["source"] != source:
            raise PermissionError(f"{metric} is owned by source {METRICS[metric]['source']!r}, not {source!r}")
        if month > month_of(now):
            raise ValueError(f"cannot record future month {month} at {iso(now)}")
        value = int(value)
        row = self.get(metric, month)
        freeze = is_freezable(month, now)
        if row is not None and row.frozen:
            if row.value != value or row.query != query:
                raise FrozenRowError(
                    f"{metric} {month} is frozen at {row.value} (query {row.query!r}); "
                    f"recompute gave {value} (query {query!r}) — not modified"
                )
            return "unchanged"
        if row is None:
            self.rows[(metric, month)] = Row(metric, month, value, freeze, source, query, iso(now))
            return "inserted"
        status = "unchanged"
        if row.value != value or row.query != query:
            row.value, row.query, row.computed_at = value, query, iso(now)
            status = "updated"
        if freeze:
            row.frozen = True
            status = "froze" if status == "unchanged" else status
        return status

    def months(self, metric: str) -> list[Row]:
        return [self.rows[k] for k in sorted(self.rows) if k[0] == metric]

    def lifetime(self, metric: str) -> int:
        return sum(r.value for r in self.months(metric))


def log_drift(message: str, now: datetime, path: Path | None = None) -> None:
    path = path or DRIFT_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(f"{iso(now)} {message}\n")
