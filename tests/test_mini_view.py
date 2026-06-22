"""미니 뷰(상주 동반 글랜스 창, ADR 0008) 컨텍스트 조립 단위 테스트.

핵심 불변식: 미니 뷰는 **공식 스냅샷만** 읽는다(official-only). 공식 데이터가 없으면
큰 창 카드처럼 로컬 추정으로 폴백하지 않고 'no_official' 안내만 둔다 — ingest 무관.
표시는 활성 AI별 **모든 게이지**를 압축 행으로(Codex 월간+주간, Claude 5h+7d 등).
"""
from datetime import datetime

from tokenomy.aggregate import KST
from tokenomy.db import connect, insert_official_buckets
from tokenomy.official_parser import OfficialBucket
from tokenomy.web.views import mini_view_context

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=KST)


def _conn():
    return connect(":memory:")


def _bucket(**kw) -> OfficialBucket:
    base = dict(
        bucket_key="monthly", raw_key="spend", bucket_kind="monthly_limit",
        label="월 사용 한도", native_unit="usd",
        used_native=30.0, limit_native=100.0, remaining_native=70.0,
        used_usd=30.0, limit_usd=100.0, remaining_usd=70.0,
        utilization=30.0, resets_at=None,
    )
    base.update(kw)
    return OfficialBucket(**base)


def _seed(conn, provider, buckets):
    insert_official_buckets(conn, provider=provider, fetched_at=NOW.isoformat(),
                            buckets=buckets, created_at=NOW.isoformat())


def _card(ctx, provider):
    return next(c for c in ctx["cards"] if c["provider"] == provider)


def test_official_ok_card_has_gauges_and_no_fallback():
    conn = _conn()
    _seed(conn, "claude", [_bucket(utilization=80.0, used_usd=80.0, remaining_usd=20.0)])
    ctx = mini_view_context(conn, {"tracked_providers": ["claude"]}, NOW)
    card = _card(ctx, "claude")
    assert card["status"] == "ok"
    assert card["no_official"] is False
    assert card["gauges"] and card["gauges"][0]["label"] == "월 사용 한도"
    assert card.get("fallback") is None          # official-only: 로컬 추정 폴백 없음


def test_no_official_omits_local_fallback():
    # 로컬 메시지만 있고 공식 스냅샷이 없으면 — 큰 창 카드는 로컬 추정으로 폴백하지만
    # 미니 뷰는 official-only라 추정 없이 no_official 안내만 둔다.
    conn = _conn()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,cost_usd,priced) "
                 "VALUES ('a','claude','s','2026-06-12T10:00:00Z',7.0,1)")
    conn.commit()
    card = _card(mini_view_context(conn, {"tracked_providers": ["claude"]}, NOW), "claude")
    assert card["no_official"] is True
    assert card["gauges"] == []
    assert card.get("fallback") is None          # 불변식: 로컬 추정 미표시


def test_all_gauges_shown_codex_monthly_plus_weekly():
    # provider당 모든 게이지(모든 버킷) 노출 — Codex는 월간 + 주간 추정 둘 다.
    conn = _conn()
    _seed(conn, "codex", [_bucket(bucket_kind="codex_monthly", label="월간 크레딧 한도",
                                  native_unit="credit", utilization=20.0,
                                  used_usd=40.0, limit_usd=200.0, remaining_usd=160.0)])
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,cost_usd,priced) "
                 "VALUES ('a','codex','s1','2026-06-20T10:00:00Z',3.0,1)")
    conn.commit()
    card = _card(mini_view_context(conn, {"tracked_providers": ["codex"]}, NOW), "codex")
    labels = [g["label"] for g in card["gauges"]]
    assert "월간 크레딧 한도" in labels and "이번 주" in labels   # 두 게이지 모두


def test_inactive_provider_omitted():
    conn = _conn()
    _seed(conn, "claude", [_bucket()])
    _seed(conn, "codex", [_bucket(bucket_kind="codex_monthly", label="월간 크레딧 한도")])
    ctx = mini_view_context(conn, {"tracked_providers": ["claude"]}, NOW)
    assert [c["provider"] for c in ctx["cards"]] == ["claude"]


def test_empty_active_yields_no_cards():
    conn = _conn()
    _seed(conn, "claude", [_bucket()])
    ctx = mini_view_context(conn, {"tracked_providers": []}, NOW)
    assert ctx["cards"] == []


def test_interval_reflects_config():
    conn = _conn()
    ctx = mini_view_context(conn, {"tracked_providers": [], "official_fetch": {"min_interval_minutes": 15}}, NOW)
    assert ctx["interval"] == 15
