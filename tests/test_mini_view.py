"""미니 뷰(상주 동반 글랜스 창, ADR 0008) 컨텍스트 조립 단위 테스트.

핵심 불변식: 미니 뷰는 **공식 스냅샷만** 읽는다(official-only). 공식 데이터가 없으면
큰 창 카드처럼 로컬 추정으로 폴백하지 않고 'no_official' 안내만 둔다 — ingest 무관.
표시는 활성 AI별 **모든 게이지**를 압축 행으로(Codex 월간(+개인 구독제 rate-window), Claude 5h+7d 등).
"""
from datetime import datetime

from tokenomy.clock import KST
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


def test_codex_no_weekly_estimate_gauge():
    # 추정 주간 게이지 제거(ADR 0012) — 미니도 공식 수치만. 로컬 사용이 있어도 안 만든다.
    conn = _conn()
    _seed(conn, "codex", [_bucket(bucket_kind="codex_monthly", label="월간",
                                  native_unit="credit", utilization=20.0,
                                  used_usd=40.0, limit_usd=200.0, remaining_usd=160.0)])
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,cost_usd,priced) "
                 "VALUES ('a','codex','s1','2026-06-20T10:00:00Z',3.0,1)")
    conn.commit()
    card = _card(mini_view_context(conn, {"tracked_providers": ["codex"]}, NOW), "codex")
    labels = [g["label"] for g in card["gauges"]]
    assert labels == ["월간"]                       # 월간만 — 주간 추정 게이지 없음
    assert "이번 주" not in labels


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


def _seed_day(conn, provider, day, used):
    """USD 버킷 1개를 2026-06-<day> 12:00 KST 스냅샷으로 적재(글랜스 이력용)."""
    dt = datetime(2026, 6, day, 12, 0, tzinfo=KST)
    insert_official_buckets(
        conn, provider=provider, fetched_at=dt.isoformat(), created_at=dt.isoformat(),
        buckets=[_bucket(used_usd=used, used_native=used,
                         remaining_usd=100.0 - used, remaining_native=100.0 - used,
                         utilization=used)])


def test_mini_card_carries_period_glance_with_partial_marker():
    """미니 카드도 공식 기간 소비 글랜스를 갖는다(ADR 0011) — 갭이면 today partial(△ 신호)."""
    conn = _conn()
    _seed_day(conn, "claude", 18, 20.0)   # 목 baseline
    _seed_day(conn, "claude", 21, 50.0)   # 일(오늘=NOW), 19·20 갭 → today partial
    card = _card(mini_view_context(conn, {"tracked_providers": ["claude"]}, NOW), "claude")
    assert card["glance"] is not None
    assert card["glance"].today.state == "partial"
    assert card["glance"].today.usd == 30.0           # 50-20
    assert card["glance"].today.observed_from is not None


def test_mini_view_fans_out_official_view_once_per_provider(monkeypatch):
    """미니 조립(카드+공유문구)도 outlook 팬아웃 1회를 공유한다 — provider당 official_view 1회(후보 4)."""
    import tokenomy.forecast as forecast_module
    import tokenomy.web.views as views_module
    calls: list[str] = []
    real = forecast_module.official_view

    def counting(conn, provider, *a, **kw):
        calls.append(provider)
        return real(conn, provider, *a, **kw)

    monkeypatch.setattr(forecast_module, "official_view", counting)
    if hasattr(views_module, "official_view"):      # 리팩터 전 직접 팬아웃 경로도 계수
        monkeypatch.setattr(views_module, "official_view", counting)

    conn = _conn()
    mini_view_context(conn, {"tracked_providers": ["claude", "codex"]}, NOW)
    assert sorted(calls) == ["claude", "codex"]
