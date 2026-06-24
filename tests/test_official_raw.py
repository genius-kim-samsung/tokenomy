"""공식 raw 포착(official_raw) — db 적재/조회/7일 prune (ADR 0014)."""
from __future__ import annotations

from tokenomy.db import (
    connect, insert_official_raw, get_official_raw, list_official_raw,
)
from tokenomy.official_fetch import scrub_pii


# ---------------------------------------------------------------------------
# PII 스크럽(deny-list 재귀) — ADR 0014
# ---------------------------------------------------------------------------

def test_scrub_redacts_top_level_pii():
    obj = {"user_id": "u1", "account_id": "a1", "email": "x@y.z",
           "plan_type": "business", "used": 5}
    out = scrub_pii(obj)
    assert out["user_id"] == "[redacted]"
    assert out["account_id"] == "[redacted]"
    assert out["email"] == "[redacted]"
    assert out["plan_type"] == "business"   # 비PII는 보존
    assert out["used"] == 5


def test_scrub_recurses_nested_dict_and_list():
    obj = {"a": {"email": "e", "keep": 1},
           "list": [{"user_id": "u"}, {"v": 2}]}
    out = scrub_pii(obj)
    assert out["a"]["email"] == "[redacted]"
    assert out["a"]["keep"] == 1
    assert out["list"][0]["user_id"] == "[redacted]"
    assert out["list"][1]["v"] == 2


def test_scrub_does_not_mutate_input():
    obj = {"email": "e"}
    scrub_pii(obj)
    assert obj["email"] == "e"   # 원본 불변


def test_scrub_passthrough_scalars():
    assert scrub_pii(5) == 5
    assert scrub_pii(None) is None
    assert scrub_pii("s") == "s"


def test_insert_and_get_official_raw():
    conn = connect(":memory:")
    ts = "2026-06-24T10:00:00+09:00"
    insert_official_raw(conn, provider="claude", fetched_at=ts, status="ok",
                        http_code=200, raw_text='{"a":1}', created_at=ts)
    row = get_official_raw(conn, "claude", ts)
    assert row is not None
    assert row["provider"] == "claude"
    assert row["status"] == "ok"
    assert row["http_code"] == 200
    assert row["raw_text"] == '{"a":1}'
    assert row["byte_len"] == len('{"a":1}'.encode("utf-8"))


def test_get_official_raw_missing_returns_none():
    conn = connect(":memory:")
    assert get_official_raw(conn, "claude", "2026-06-24T10:00:00+09:00") is None


def test_insert_official_raw_idempotent_replace():
    """같은 (provider, fetched_at) 재취득은 덮어쓰기(부분 스냅샷·중복 방지)."""
    conn = connect(":memory:")
    ts = "2026-06-24T10:00:00+09:00"
    insert_official_raw(conn, provider="claude", fetched_at=ts, status="ok",
                        http_code=200, raw_text='{"a":1}', created_at=ts)
    insert_official_raw(conn, provider="claude", fetched_at=ts, status="ok",
                        http_code=200, raw_text='{"a":2}', created_at=ts)
    rows = list_official_raw(conn, "claude")
    assert len(rows) == 1
    assert rows[0]["raw_text"] == '{"a":2}'


def test_prune_drops_rows_older_than_7_days():
    """insert 시 fetched_at 기준 7일 지난 raw는 자동 삭제(롤링)."""
    conn = connect(":memory:")
    old = "2026-06-10T10:00:00+09:00"
    insert_official_raw(conn, provider="claude", fetched_at=old, status="ok",
                        http_code=200, raw_text="{}", created_at=old)
    new = "2026-06-24T10:00:00+09:00"  # old +14일
    insert_official_raw(conn, provider="claude", fetched_at=new, status="ok",
                        http_code=200, raw_text="{}", created_at=new)
    rows = list_official_raw(conn, "claude")
    assert len(rows) == 1
    assert rows[0]["fetched_at"] == new


def test_prune_keeps_within_7_days():
    conn = connect(":memory:")
    a = "2026-06-20T10:00:00+09:00"
    insert_official_raw(conn, provider="claude", fetched_at=a, status="ok",
                        http_code=200, raw_text="{}", created_at=a)
    b = "2026-06-24T10:00:00+09:00"  # a +4일(7일 이내)
    insert_official_raw(conn, provider="claude", fetched_at=b, status="ok",
                        http_code=200, raw_text="{}", created_at=b)
    assert len(list_official_raw(conn, "claude")) == 2


def test_list_official_raw_desc_and_provider_scoped():
    conn = connect(":memory:")
    t1 = "2026-06-24T10:00:00+09:00"
    t2 = "2026-06-24T11:00:00+09:00"
    insert_official_raw(conn, provider="claude", fetched_at=t1, status="ok",
                        http_code=200, raw_text="{}", created_at=t1)
    insert_official_raw(conn, provider="claude", fetched_at=t2, status="ok",
                        http_code=200, raw_text="{}", created_at=t2)
    insert_official_raw(conn, provider="codex", fetched_at=t1, status="ok",
                        http_code=200, raw_text="{}", created_at=t1)
    rows = list_official_raw(conn, "claude")
    assert [r["fetched_at"] for r in rows] == [t2, t1]   # 최신순
    assert all(r["provider"] == "claude" for r in rows)


def test_insert_official_raw_allows_null_http_code():
    """파싱 실패/네트워크 에러는 http_code가 없을 수 있다(None 허용)."""
    conn = connect(":memory:")
    ts = "2026-06-24T10:00:00+09:00"
    insert_official_raw(conn, provider="codex", fetched_at=ts, status="http_error",
                        http_code=None, raw_text="<html>err</html>", created_at=ts)
    row = get_official_raw(conn, "codex", ts)
    assert row["status"] == "http_error"
    assert row["http_code"] is None


# ---------------------------------------------------------------------------
# views.official_raw_context — raw 페이지 데이터 조립(ADR 0014)
# ---------------------------------------------------------------------------

from tokenomy.db import insert_official_buckets   # noqa: E402
from tokenomy.official_parser import OfficialBucket   # noqa: E402
from tokenomy.web.views import official_raw_context   # noqa: E402


def _seed_raw(conn, provider, ts, raw='{"a":1}', status="ok", http_code=200):
    insert_official_raw(conn, provider=provider, fetched_at=ts, status=status,
                        http_code=http_code, raw_text=raw, created_at=ts)


def test_official_raw_context_empty_when_no_raw():
    conn = connect(":memory:")
    ctx = official_raw_context(conn, {})
    assert ctx["providers"] == []
    assert ctx["selected_provider"] is None


def test_official_raw_context_selects_latest_by_default():
    conn = connect(":memory:")
    _seed_raw(conn, "claude", "2026-06-24T10:00:00+09:00", raw='{"x":1}')
    _seed_raw(conn, "claude", "2026-06-24T11:00:00+09:00", raw='{"x":2}')
    ctx = official_raw_context(conn, {})
    assert ctx["selected_provider"] == "claude"
    assert ctx["selected_fetched_at"] == "2026-06-24T11:00:00+09:00"
    assert '"x": 2' in ctx["raw_pretty"]            # pretty-print된 최신
    assert len(ctx["snapshots"]) == 2               # 7일 피커


def test_official_raw_context_specific_fetched_at():
    conn = connect(":memory:")
    _seed_raw(conn, "claude", "2026-06-24T10:00:00+09:00", raw='{"x":1}')
    _seed_raw(conn, "claude", "2026-06-24T11:00:00+09:00", raw='{"x":2}')
    ctx = official_raw_context(conn, {}, provider="claude",
                               fetched_at="2026-06-24T10:00:00+09:00")
    assert ctx["selected_fetched_at"] == "2026-06-24T10:00:00+09:00"
    assert '"x": 1' in ctx["raw_pretty"]


def test_official_raw_context_includes_buckets_and_meta():
    conn = connect(":memory:")
    ts = "2026-06-24T10:00:00+09:00"
    _seed_raw(conn, "claude", ts, raw='{"x":1}', status="ok")
    b = OfficialBucket(bucket_key="monthly", raw_key="spend", bucket_kind="monthly_limit",
                       label="월간", native_unit="usd", used_native=30.0, limit_native=100.0,
                       remaining_native=70.0, used_usd=30.0, limit_usd=100.0,
                       remaining_usd=70.0, utilization=30.0, resets_at=None)
    insert_official_buckets(conn, provider="claude", fetched_at=ts, buckets=[b], created_at=ts)
    ctx = official_raw_context(conn, {}, provider="claude", fetched_at=ts)
    assert ctx["meta"]["status"] == "ok" and ctx["meta"]["http_code"] == 200
    assert len(ctx["buckets"]) == 1 and ctx["buckets"][0]["label"] == "월간"


def test_official_raw_context_non_json_raw_passthrough():
    conn = connect(":memory:")
    ts = "2026-06-24T10:00:00+09:00"
    _seed_raw(conn, "claude", ts, raw="<html>err</html>", status="parse_error", http_code=200)
    ctx = official_raw_context(conn, {}, provider="claude", fetched_at=ts)
    assert "<html>err</html>" in ctx["raw_pretty"]   # 비-JSON은 그대로
    assert ctx["meta"]["status"] == "parse_error"


def test_official_raw_context_unknown_provider_falls_back_to_first():
    conn = connect(":memory:")
    _seed_raw(conn, "codex", "2026-06-24T10:00:00+09:00")
    ctx = official_raw_context(conn, {}, provider="bogus")
    assert ctx["selected_provider"] == "codex"
