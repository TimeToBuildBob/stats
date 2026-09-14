from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import ledger as L


def dt(*a):
    return datetime(*a, tzinfo=timezone.utc)


PUB = L.SOURCE_PUBLIC
PRIV = L.SOURCE_PRIVATE


def test_freeze_boundary_is_month_end_plus_3_days():
    # August ends at 2026-09-01T00:00Z; freezes at 2026-09-04T00:00Z.
    assert L.freeze_at("2026-08") == dt(2026, 9, 4)
    assert not L.is_freezable("2026-08", dt(2026, 9, 3, 23, 59, 59))
    assert L.is_freezable("2026-08", dt(2026, 9, 4))
    # December rolls the year.
    assert L.freeze_at("2025-12") == dt(2026, 1, 4)


def test_recompute_allowed_before_freeze_then_freezes():
    led = L.Ledger()
    assert led.record("prs_merged_public", "2026-08", 400, PUB, "q", dt(2026, 8, 20)) == "inserted"
    assert led.record("prs_merged_public", "2026-08", 472, PUB, "q", dt(2026, 9, 3)) == "updated"
    row = led.get("prs_merged_public", "2026-08")
    assert row.value == 472 and not row.frozen
    assert led.record("prs_merged_public", "2026-08", 472, PUB, "q", dt(2026, 9, 4)) == "froze"
    assert led.get("prs_merged_public", "2026-08").frozen
    assert not led.needs_compute("prs_merged_public", "2026-08")


def test_last_recompute_at_freeze_time_may_still_change_value():
    led = L.Ledger()
    led.record("prs_merged_public", "2026-08", 470, PUB, "q", dt(2026, 9, 1))
    assert led.record("prs_merged_public", "2026-08", 472, PUB, "q", dt(2026, 9, 5)) == "updated"
    row = led.get("prs_merged_public", "2026-08")
    assert row.frozen and row.value == 472


def test_frozen_row_is_immutable():
    led = L.Ledger()
    led.record("prs_merged_public", "2025-01", 10, PUB, "q", dt(2026, 1, 1))
    before = L.Row(**vars(led.get("prs_merged_public", "2025-01")))
    assert before.frozen
    with pytest.raises(L.FrozenRowError):
        led.record("prs_merged_public", "2025-01", 11, PUB, "q", dt(2026, 2, 1))
    with pytest.raises(L.FrozenRowError):  # definition change on a frozen row
        led.record("prs_merged_public", "2025-01", 10, PUB, "q2", dt(2026, 2, 1))
    assert vars(led.get("prs_merged_public", "2025-01")) == vars(before)
    # Same value is a no-op, computed_at untouched.
    assert led.record("prs_merged_public", "2025-01", 10, PUB, "q", dt(2026, 3, 1)) == "unchanged"
    assert vars(led.get("prs_merged_public", "2025-01")) == vars(before)


def test_backfilled_past_month_inserted_frozen():
    led = L.Ledger()
    led.record("issues_opened_public", "2024-11", 3, PUB, "q", dt(2026, 9, 14))
    assert led.get("issues_opened_public", "2024-11").frozen


def test_lifetime_is_sum_over_months():
    led = L.Ledger()
    now = dt(2026, 9, 14)
    for month, v in [("2026-06", 5), ("2026-07", 7), ("2026-08", 11), ("2026-09", 2)]:
        led.record("prs_opened_public", month, v, PUB, "q", now)
    led.record("prs_merged_public", "2026-08", 100, PUB, "q", now)
    assert led.lifetime("prs_opened_public") == 25
    assert led.lifetime("prs_merged_public") == 100


def test_unknown_metric_future_month_and_wrong_source_rejected():
    led = L.Ledger()
    now = dt(2026, 9, 14)
    with pytest.raises(KeyError):
        led.record("prs_merged_public_v2", "2026-09", 1, PUB, "q", now)
    with pytest.raises(ValueError):
        led.record("prs_merged_public", "2026-10", 1, PUB, "q", now)
    with pytest.raises(PermissionError):  # public collector can't write private rows
        led.record("brain_commits", "2026-09", 1, PUB, "q", now)
    with pytest.raises(PermissionError):
        led.record("prs_merged_public", "2026-09", 1, PRIV, "q", now)


def test_save_load_roundtrip_and_idempotent_rerun(tmp_path: Path):
    path = tmp_path / "ledger.csv"
    now = dt(2026, 9, 14)
    led = L.Ledger()
    led.record("prs_merged_public", "2026-08", 472, PUB, "GET /search/issues?q=a b", now)
    led.record("prs_merged_public", "2026-09", 150, PUB, "GET /search/issues?q=a b", now)
    led.record("brain_commits", "2026-08", 15000, PRIV, "git rev-list", now)
    led.save(path)
    first = path.read_bytes()

    # Re-run later the same day with the same values: file is byte-identical.
    led2 = L.Ledger.load(path)
    later = now + timedelta(hours=6)
    for metric, month, v, src, q in [
        ("prs_merged_public", "2026-08", 472, PUB, "GET /search/issues?q=a b"),
        ("prs_merged_public", "2026-09", 150, PUB, "GET /search/issues?q=a b"),
    ]:
        assert led2.needs_compute(metric, month) == (month == "2026-09")
        if led2.needs_compute(metric, month):
            assert led2.record(metric, month, v, src, q, later) == "unchanged"
    led2.save(path)
    assert path.read_bytes() == first


def test_public_update_leaves_private_rows_untouched(tmp_path: Path):
    path = tmp_path / "ledger.csv"
    now = dt(2026, 9, 14)
    led = L.Ledger()
    led.record("brain_commits", "2026-09", 8000, PRIV, "git", now)
    led.record("sessions", "2026-09", 2000, PRIV, "sessions", now)
    led.record("prs_merged_public", "2026-09", 150, PUB, "q", now)
    led.save(path)
    private_before = [vars(r) for r in led.rows.values() if r.source == PRIV]

    led2 = L.Ledger.load(path)
    led2.record("prs_merged_public", "2026-09", 160, PUB, "q", now + timedelta(days=1))
    led2.save(path)
    led3 = L.Ledger.load(path)
    assert [vars(r) for r in led3.rows.values() if r.source == PRIV] == private_before
    assert led3.get("prs_merged_public", "2026-09").value == 160


def test_load_rejects_duplicates_and_bad_header(tmp_path: Path):
    path = tmp_path / "ledger.csv"
    row = "prs_merged_public,2026-08,1,true,github-search,q,2026-09-14T00:00:00Z\n"
    path.write_text(",".join(L.FIELDS) + "\n" + row + row)
    with pytest.raises(ValueError):
        L.Ledger.load(path)
    path.write_text("metric,month,value\n")
    with pytest.raises(ValueError):
        L.Ledger.load(path)


def test_collect_public_skips_frozen_and_logs_drift(tmp_path, monkeypatch):
    import collect_public as C

    path = tmp_path / "ledger.csv"
    drift = tmp_path / "drift.log"
    monkeypatch.setattr(L, "LEDGER_PATH", path)
    monkeypatch.setattr(L, "DRIFT_LOG", drift)
    monkeypatch.setattr(L, "utcnow", lambda: dt(2024, 12, 10))
    monkeypatch.setenv("GITHUB_TOKEN", "x")

    calls = []
    monkeypatch.setattr(C, "search_count", lambda ep, q, tok: calls.append(q) or 5)
    assert C.main(["--metric", "prs_merged_public"]) == 0
    led = L.Ledger.load(path)
    assert led.get("prs_merged_public", "2024-11").frozen  # 2024-12-10 >= 2024-12-04
    assert not led.get("prs_merged_public", "2024-12").frozen
    assert len(calls) == 2 and all("is:public" in q for q in calls)

    # Normal re-run only queries the open month.
    calls.clear()
    assert C.main(["--metric", "prs_merged_public"]) == 0
    assert len(calls) == 1 and "2024-12-01" in calls[0]

    # A verify run that sees a different value for a frozen month refuses + logs.
    monkeypatch.setattr(C, "search_count", lambda ep, q, tok: 9)
    assert C.main(["--metric", "prs_merged_public", "--verify-frozen"]) == L.EXIT_FROZEN_DRIFT
    assert L.Ledger.load(path).get("prs_merged_public", "2024-11").value == 5
    assert "2024-11 is frozen" in drift.read_text()


def test_session_filter():
    import collect_private as P

    base = {
        "agent_id": "bob-autonomous-claude-code",
        "status": "completed",
        "reason": "clean_exit",
        "completed_at": "2026-07-01T00:00:00+00:00",
        "duration_seconds": 600,
        "model": "opus",
    }
    assert P.include_session(base)
    assert P.include_session({**base, "model": None})
    assert not P.include_session({**base, "model": "<synthetic>"})
    assert not P.include_session({**base, "model": "test-model"})
    assert P.include_session({**base, "model": "grok-latest"})
    assert not P.include_session({**base, "reason": "lock_busy_skip"})
    assert not P.include_session({**base, "duration_seconds": 3, "reason": "nonzero_exit"})
    assert not P.include_session({**base, "agent_id": None})


def test_private_payload_validation():
    import collect_private as P

    now = dt(2026, 9, 14)
    ok = {"metric": "brain_commits", "month": "2026-09", "value": 10, "query": "git"}
    assert P.validate_rows({"rows": [ok]}, now) == [ok]
    bad = [
        {**ok, "metric": "prs_merged_public"},  # public metrics can't arrive via dispatch
        {**ok, "value": -1},
        {**ok, "value": True},
        {**ok, "value": "10"},
        {**ok, "month": "2026-10"},  # future
        {**ok, "month": "2025-07"},  # before first_month
        {**ok, "month": "2026-13"},
        {**ok, "extra": "repo-name"},  # only aggregates, no extra fields
    ]
    for row in bad:
        with pytest.raises(ValueError):
            P.validate_rows({"rows": [row]}, now)
    with pytest.raises(ValueError):
        P.validate_rows([ok], now)


def test_private_apply_freeze_and_emit_roundtrip(tmp_path, monkeypatch):
    import json

    import collect_private as P

    path = tmp_path / "ledger.csv"
    drift = tmp_path / "drift.log"
    monkeypatch.setattr(L, "LEDGER_PATH", path)
    monkeypatch.setattr(L, "DRIFT_LOG", drift)
    monkeypatch.setattr(L, "utcnow", lambda: dt(2026, 7, 10))
    (tmp_path / "brain" / ".git").mkdir(parents=True)
    monkeypatch.setattr(P, "brain_commits", lambda brain, month: (100, "git rev-list"))
    monkeypatch.setattr(P, "session_counts", lambda brain: {"2026-06": 7, "2026-07": 3})

    body = tmp_path / "body.json"
    assert P.main(["--emit", str(body), "--brain", str(tmp_path / "brain")]) == 0
    emitted = json.loads(body.read_text())
    assert emitted["event_type"] == "private-aggregates"
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(emitted["client_payload"]))
    assert P.main(["--apply", str(payload)]) == 0
    led = L.Ledger.load(path)
    assert led.get("sessions", "2026-06").frozen and led.get("sessions", "2026-06").value == 7
    assert not led.get("sessions", "2026-07").frozen
    assert led.lifetime("brain_commits") == 100 * len(L.months_range("2025-08", "2026-07"))

    # Next emit only carries open months.
    assert P.main(["--emit", str(body), "--brain", str(tmp_path / "brain")]) == 0
    assert {(r["metric"], r["month"]) for r in json.loads(body.read_text())["client_payload"]["rows"]} == {
        ("brain_commits", "2026-07"),
        ("sessions", "2026-07"),
    }

    # A payload changing a frozen month is refused, logged, and exits 3.
    payload.write_text(json.dumps({"rows": [{"metric": "sessions", "month": "2026-06", "value": 8, "query": P.SESSIONS_QUERY}]}))
    assert P.main(["--apply", str(payload)]) == L.EXIT_FROZEN_DRIFT
    assert L.Ledger.load(path).get("sessions", "2026-06").value == 7
    assert "2026-06 is frozen" in drift.read_text()
