import os
from datetime import datetime, timedelta

from tokenomy.aggregate import KST
from tokenomy.db import connect
from tokenomy.freshness import freshness, record_ingest, WARN_AGE_DAYS


def _seed(root, age_days, now):
    p = root / "p" / "s.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}\n", encoding="utf-8")
    t = (now - timedelta(days=age_days)).timestamp()
    os.utime(p, (t, t))
    return p


def test_record_and_hours_since(tmp_path):
    conn = connect(":memory:")
    now = datetime(2026, 6, 12, 10, 0, 0, tzinfo=KST)
    record_ingest(conn, now)
    fr = freshness(conn, tmp_path, now + timedelta(hours=3))
    assert fr.last_ingest_ts == now.isoformat()
    assert abs(fr.hours_since_ingest - 3) < 0.01


def test_old_raw_triggers_warn(tmp_path):
    conn = connect(":memory:")
    now = datetime(2026, 6, 12, 12, 0, 0, tzinfo=KST)
    _seed(tmp_path / "projects", 27, now)
    fr = freshness(conn, tmp_path / "projects", now)
    assert fr.oldest_raw_age_days >= WARN_AGE_DAYS
    assert fr.level == "warn"


def test_recent_raw_is_ok(tmp_path):
    conn = connect(":memory:")
    now = datetime(2026, 6, 12, 12, 0, 0, tzinfo=KST)
    _seed(tmp_path / "projects", 2, now)
    fr = freshness(conn, tmp_path / "projects", now)
    assert fr.level == "ok"
