"""공식 집계(official_aggregate.py) 테스트 — official_view·통합 전망·풀 이력·글랜스."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import tokenomy.official_aggregate
from tokenomy.clock import KST, parse_ts
from tokenomy.db import connect, insert_official_buckets
from tokenomy.official_aggregate import (
    OfficialView, _segment_points, combined_forecast, forecast_month_line,
    official_daily_rate, official_period_glance, official_view,
    pool_daily_history, pool_history, pool_hourly_history, pool_snapshots_by_day,
    pool_used_history, this_month_spend,
)
from tokenomy.official_parser import OfficialBucket
from tokenomy.web.views import _forecast_hero, pool_history_to_daily

# June 2026 has 30 days
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=KST)  # day 10 of 30


def _insert(conn, ts, cost, project="/p", session="s", cache_read=0, input_t=0,
            priced=1, provider="claude", model="claude-opus-4-8"):
    conn.execute(
        "INSERT INTO messages(dedup_key,provider,session_id,project,ts,model,"
        "input_tokens,cache_creation,cache_read,cost_usd,priced) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (f"{ts}-{cost}-{session}-{project}-{model}", provider, session, project, ts,
         model, input_t, 0, cache_read, cost, priced),
    )
    conn.commit()


# --- official_view (Claude 버킷 + Codex 월간 + 예측 렌즈) ---


def _ob(key, kind, used_usd, limit_usd, raw="r", unit="usd", util=0.0, resets=None):
    return OfficialBucket(
        bucket_key=key, raw_key=raw, bucket_kind=kind, label=key, native_unit=unit,
        used_native=used_usd, limit_native=limit_usd,
        remaining_native=(limit_usd - used_usd) if limit_usd else None,
        used_usd=used_usd, limit_usd=limit_usd,
        remaining_usd=(limit_usd - used_usd) if limit_usd else None,
        utilization=util, resets_at=resets,
    )


def test_official_view_no_data_status():
    conn = connect(":memory:")
    v = official_view(conn, "claude", NOW, 0.04)
    assert isinstance(v, OfficialView)
    assert v.status == "no_data"
    assert v.buckets == []


def test_official_view_claude_monthly_period():
    conn = connect(":memory:")
    insert_official_buckets(
        conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
        buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                 _ob("event", "event_credit", 125.0, 500.0, raw="cinder")],
        created_at="2026-06-10T09:00:00+09:00",
    )
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.status == "ok"
    assert v.period_used_usd == 30.0 and v.period_limit_usd == 100.0
    assert {b["bucket_key"] for b in v.buckets} == {"monthly", "event"}
    # 월 버킷 resets_at은 다음 달 경계로 채워짐
    monthly = next(b for b in v.buckets if b["bucket_key"] == "monthly")
    assert monthly["resets_at"].startswith("2026-07-01")


def test_official_view_codex_monthly_only_no_weekly():
    # 추정 주간 게이지 제거(ADR 0012) — 로컬 Codex 사용이 있어도 월간 버킷만 노출.
    conn = connect(":memory:")
    _insert(conn, "2026-06-09T01:00:00Z", 12.0, provider="codex", session="a")
    insert_official_buckets(
        conn, provider="codex", fetched_at="2026-06-10T09:00:00+09:00",
        buckets=[_ob("monthly", "codex_monthly", 20.0, 80.0, raw="individual_limit",
                     unit="credit", util=25.0)],
        created_at="2026-06-10T09:00:00+09:00",
    )
    now = datetime(2026, 6, 11, 12, 0, tzinfo=KST)
    v = official_view(conn, "codex", now, 0.04)
    assert v.period_used_usd == 20.0 and v.period_limit_usd == 80.0  # 월간(공식)
    assert not hasattr(v, "weekly_used_usd")    # 주간 필드 제거됨
    assert {b["bucket_key"] for b in v.buckets} == {"monthly"}


def test_official_view_lens_uses_official_rate():
    # 카드 고스트도 공식 기울기(ADR 0015 D3) — 단일 스냅샷 → 공식 월초누적, 로컬 거액은 무시.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 40.0, 100.0, raw="spend")],
                            created_at="x")
    _insert(conn, "2026-06-05T01:00:00Z", 999.0, provider="claude", session="a")  # 로컬 — 무시돼야
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.lens is not None
    assert v.lens.daily_rate_usd == 5.0   # 공식 월초누적 40/8영업일(6/1~6/10), 로컬 999 무시
    assert v.active_key == "monthly"


def test_official_view_codex_lens_uses_official_rate():
    # Codex 카드 고스트도 공식 기울기 — 로컬은 무시.
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T01:00:00Z", 999.0, provider="codex", session="a")  # 로컬 — 무시
    insert_official_buckets(conn, provider="codex", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "codex_monthly", 40.0, 80.0, raw="individual_limit",
                                         unit="credit", util=50.0)],
                            created_at="x")
    v = official_view(conn, "codex", NOW, 0.04)
    assert v.lens is not None
    assert v.lens.daily_rate_usd == 5.0   # 공식 월초누적 40/8


def test_lens_none_rate_without_official_used():
    # 공식 used=0 → (a)트레일링 불가·(b)월초누적 0 → 고스트 기울기 None(로컬 있어도 무시).
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 0.0, 100.0, raw="spend")],
                            created_at="x")
    _insert(conn, "2026-06-05T01:00:00Z", 100.0, provider="claude", session="a")  # 로컬 — 무시
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.lens is not None
    assert v.lens.daily_rate_usd is None


def test_official_view_active_bucket_largest_diff():
    conn = connect(":memory:")
    # Claude: event + monthly 둘 다 활성. monthly의 최근 차분이 더 커서 active=monthly(이벤트가 tie-break 우선임에도)
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-09T09:00:00+09:00",
                            buckets=[_ob("event", "event_credit", 100.0, 500.0, raw="cinder"),
                                     _ob("monthly", "monthly_limit", 10.0, 100.0, raw="spend")],
                            created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("event", "event_credit", 102.0, 500.0, raw="cinder"),   # +2
                                     _ob("monthly", "monthly_limit", 40.0, 100.0, raw="spend")],  # +30
                            created_at="x")
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.active_key == "monthly"   # 최근 차분 30 > 2 → tie-break(event 우선) 무시하고 monthly


# ─── Task 3: 예산 분리 신규 테스트 ─────────────────────────────────────────────


def _conn_with_official_codex_monthly(limit_usd: float):
    """공식 Codex 월간 버킷이 있는 in-memory DB. official_view 테스트용."""
    conn = connect(":memory:")
    insert_official_buckets(
        conn, provider="codex", fetched_at="2026-06-10T09:00:00+09:00",
        buckets=[_ob("monthly", "codex_monthly", 20.0, limit_usd, raw="individual_limit",
                     unit="credit", util=0.0)],
        created_at="2026-06-10T09:00:00+09:00",
    )
    return conn


def test_official_view_no_budget_arg():
    conn = _conn_with_official_codex_monthly(limit_usd=80)
    ov = official_view(conn, "codex", NOW, 0.04)
    assert ov.period_limit_usd == 80.0   # 공식 월간 한도(수동 예산 인자 없음)


# --- 투영 프리미티브(forecast_month_line) — hero·chart 투영 규칙의 정본 1곳 ---


def test_forecast_month_line_anchors_today_and_accumulates_business_days():
    # NOW=2026-06-10(수). 오늘=used anchor, 이후 영업일마다 rate 누적, 주말 flat, 월말=used+rate*영업일.
    line = forecast_month_line(40.0, 10.0, NOW)
    assert line[10] == 40.0                      # 오늘(6/10) = used anchor
    assert line[11] == 50.0                      # 6/11(목) +1영업일
    assert line[12] == 60.0                      # 6/12(금) +2
    assert line[13] == 60.0                      # 6/13(토) 주말 flat
    assert line[14] == 60.0                      # 6/14(일) flat
    assert line[15] == 70.0                      # 6/15(월) +3
    assert 9 not in line                         # 오늘 이전은 없음(차트가 None 처리)
    assert line[30] == 180.0                     # 월말(6/30) = 40 + 10*14영업일


# --- 통합 월말 전망(combined_forecast) ---


def _fc_views(conn, now):
    return [official_view(conn, "claude", now, 0.04),
            official_view(conn, "codex", now, 0.04)]


def test_combined_forecast_empty_pool_none():
    # 공식 한도 전무 → None(히어로 숨김)
    conn = connect(":memory:")
    assert combined_forecast(conn, _fc_views(conn, NOW), NOW) is None


def test_combined_forecast_pool_sums_used_and_limit():
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend")],
                            created_at="x")
    insert_official_buckets(conn, provider="codex", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "codex_monthly", 20.0, 80.0,
                                         raw="individual_limit", unit="credit", util=25.0)],
                            created_at="x")
    fc = combined_forecast(conn, _fc_views(conn, NOW), NOW)
    assert set(fc.providers) == {"claude", "codex"}
    assert fc.used_usd == 50.0 and fc.limit_usd == 180.0
    assert fc.remaining_usd == 130.0


def test_combined_forecast_surplus():
    # 엔터 기울기=공식(ADR 0015). 단일 스냅샷 → 월초누적 used40/8영업일=5/일 → 110<200 여유.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 40.0, 200.0, raw="spend")],
                            created_at="x")
    fc = combined_forecast(conn, _fc_views(conn, NOW), NOW)
    assert fc.providers == ["claude"]
    assert fc.daily_rate_usd == 5.0                 # 공식 월초누적: 40 / 8영업일(6/1~6/10)
    assert fc.bdays_remaining == 14
    assert fc.projected_used_usd == 110.0           # 40 + 5*14
    assert fc.projected_remaining_usd == 90.0       # 200 - 110 → 여유
    assert fc.exhaust_date is None
    assert fc.is_exhausted is False


def test_combined_forecast_shortfall_with_exhaust_date():
    # 공식 월초누적 used80/8영업일=10/일 → 80+10*14=220>100 부족, 소진 6/12.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 80.0, 100.0, raw="spend")],
                            created_at="x")
    fc = combined_forecast(conn, _fc_views(conn, NOW), NOW)
    assert fc.daily_rate_usd == 10.0
    assert fc.projected_used_usd == 220.0           # 80 + 10*14
    assert fc.projected_remaining_usd == -120.0     # 부족
    assert fc.exhaust_date == date(2026, 6, 12)     # ceil((100-80)/10)=2 영업일 후


def test_combined_forecast_insufficient_when_no_official_used():
    # 공식 used=0 → (a)트레일링 불가·(b)월초누적 0 → 기울기 None(위치만, 로컬 기울기 폐기).
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 0.0, 200.0, raw="spend")],
                            created_at="x")
    # 로컬 소비가 있어도 엔터 기울기엔 영향 없음(로컬 기울기 폐기, ADR 0015 D3).
    _insert(conn, "2026-06-05T01:00:00Z", 100.0, provider="claude", session="a")
    fc = combined_forecast(conn, _fc_views(conn, NOW), NOW)
    assert fc.daily_rate_usd is None                # 공식 소비 0 → 기울기 없음
    assert fc.projected_remaining_usd is None
    assert fc.remaining_usd == 200.0


def test_combined_forecast_official_slope_prefers_trailing_delta():
    # 윈도우 시작 전 베이스(5/20 used50) + 윈도우 내(6/10 used150) → 트레일링 델타 100/10영업일=10.
    # 월초누적이면 150/8≈18.75라 달라 — (a)트레일링이 (b)월초누적보다 우선임을 보인다.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-05-20T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 50.0, 500.0, raw="spend")],
                            created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 150.0, 500.0, raw="spend")],
                            created_at="x")
    fc = combined_forecast(conn, _fc_views(conn, NOW), NOW)
    assert fc.daily_rate_usd == 10.0                # 트레일링 100/10, 월초누적(150/8) 아님


def test_combined_forecast_official_slope_ignores_local_spend():
    # 엔터 기울기는 로컬 소비를 보지 않는다(로컬 기울기 폐기, ADR 0015 D3). 공식 월초누적만.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 40.0, 200.0, raw="spend")],
                            created_at="x")
    _insert(conn, "2026-06-05T01:00:00Z", 999.0, provider="claude")  # 로컬 거액 — 기울기에 영향 없어야
    fc = combined_forecast(conn, _fc_views(conn, NOW), NOW)
    # 공식 월초누적 40/8=5. 로컬 999가 섞이면 깨진다.
    assert fc.daily_rate_usd == 5.0


def test_lens_uses_official_trailing_delta():
    # 카드 고스트도 (a)공식 트레일링 우선: 윈도우 전 베이스(5/20 used50)+윈도우 내(6/10 used150)
    # → 100/10영업일=10. 로컬 거액은 무시(공식만, ADR 0015 D3).
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-05-20T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 50.0, 500.0, raw="spend")],
                            created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 150.0, 500.0, raw="spend")],
                            created_at="x")
    _insert(conn, "2026-06-05T01:00:00Z", 999.0, provider="claude")  # 로컬 — 무시
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.lens.daily_rate_usd == 10.0   # 공식 트레일링 100/10, 로컬 무시


def test_combined_forecast_already_exhausted():
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 100.0, 100.0, raw="spend")],
                            created_at="x")
    _insert(conn, "2026-06-05T01:00:00Z", 50.0, provider="claude", session="a")
    fc = combined_forecast(conn, _fc_views(conn, NOW), NOW)
    assert fc.is_exhausted is True
    assert fc.projected_used_usd is None            # 소진이면 전망 생략


# --- 풀 집계: 월간 + 포함 크레딧 합산(ADR 0004 갱신) ---


def test_official_view_pool_sums_monthly_and_credit():
    # 전망 풀 기여 = 월간 + opt-in 크레딧 합(ADR 0016: 크레딧은 큐레이션으로 풀 포함). 카드 게이지(period_*)는 월간만.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 500.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    v = official_view(conn, "claude", NOW, 0.04, is_pooled=_pool_with("cinder"))
    assert v.period_used_usd == 30.0 and v.period_limit_usd == 100.0   # 카드 게이지=월간만
    assert v.pool_used_usd == 155.0 and v.pool_limit_usd == 600.0      # 풀=월간+opt-in 크레딧


def test_official_view_pool_excludes_expired_credit():
    # 만료(resets_at 과거) 크레딧은 풀에서 제외(candidates stale 기준 공유).
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 500.0, raw="cinder",
                                         resets=datetime(2026, 5, 1, tzinfo=KST))],  # 과거 → 제외
                            created_at="x")
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.pool_used_usd == 30.0 and v.pool_limit_usd == 100.0


def test_official_view_pool_none_without_usd_limit():
    # USD 한도 버킷이 없으면(개인 구독 rate_window 등) 풀 기여 없음.
    conn = connect(":memory:")
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.pool_used_usd is None and v.pool_limit_usd is None


# ─── 버킷 큐레이션: event_credit opt-in 풀(ADR 0016) ─────────────────────────────


# 테스트용 is_pooled: 안정 키 + (opt-in한 raw_key) 풀 포함.
def _pool_with(*opted):
    return lambda p, rk, bk: bk in ("monthly_limit", "codex_monthly") or rk in opted


def test_official_view_event_credit_excluded_from_pool_by_default():
    # ADR 0016: 회전 코드네임 달러 크레딧은 기본 풀 제외(opt-in). 풀=월간만.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 25000.0, raw="amber_ladder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.pool_used_usd == 30.0 and v.pool_limit_usd == 100.0   # $25k 유령 천장 제외


def test_official_view_event_credit_pooled_when_opted_in():
    # is_pooled가 opt-in하면 진짜 크레딧을 풀에 합산.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 500.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    v = official_view(conn, "claude", NOW, 0.04, is_pooled=_pool_with("cinder"))
    assert v.pool_used_usd == 155.0 and v.pool_limit_usd == 600.0


def test_official_view_excluded_credit_still_shown_as_bucket():
    # 풀 제외돼도 게이지(buckets)에는 남는다(발견 신호). hidden은 views가 처리.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 25000.0, raw="amber_ladder")],
                            created_at="x")
    v = official_view(conn, "claude", NOW, 0.04)
    assert {b["bucket_key"] for b in v.buckets} == {"monthly", "event"}


def test_official_view_excluded_credit_not_active():
    # 풀 제외 크레딧은 active 버킷(렌즈 구동) 후보에서도 빠진다 — 차분이 더 커도 월간이 active.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-09T09:00:00+09:00",
                            buckets=[_ob("event", "event_credit", 100.0, 25000.0, raw="amber_ladder"),
                                     _ob("monthly", "monthly_limit", 10.0, 100.0, raw="spend")],
                            created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("event", "event_credit", 180.0, 25000.0, raw="amber_ladder"),  # +80
                                     _ob("monthly", "monthly_limit", 40.0, 100.0, raw="spend")],         # +30
                            created_at="x")
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.active_key == "monthly"   # event 차분(80)이 더 커도 풀 제외라 active 후보 아님


def test_pool_used_history_excludes_event_credit_by_default():
    # ADR 0016: 기본 풀 제외 → 누적 시계열은 월간만.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 25000.0, raw="amber_ladder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    assert pool_used_history(conn, "claude") == [(parse_ts("2026-06-10T09:00:00+09:00"), 30.0)]


def test_pool_used_history_includes_event_credit_when_opted_in():
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 500.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    hist = pool_used_history(conn, "claude", is_pooled=_pool_with("cinder"))
    assert hist == [(parse_ts("2026-06-10T09:00:00+09:00"), 155.0)]


def test_combined_forecast_includes_event_credit():
    # 통합 풀이 Claude opt-in 크레딧(실제 닳는 버킷)을 합산(ADR 0016). 오버리지($0/$100)만 보던 회귀 방지.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 0.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 500.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    insert_official_buckets(conn, provider="codex", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "codex_monthly", 20.0, 80.0,
                                         raw="individual_limit", unit="credit", util=25.0)],
                            created_at="x")
    ip = _pool_with("cinder")
    views = [official_view(conn, "claude", NOW, 0.04, is_pooled=ip),
             official_view(conn, "codex", NOW, 0.04, is_pooled=ip)]
    fc = combined_forecast(conn, views, NOW, is_pooled=ip)
    assert fc.used_usd == 145.0      # 0 + 125 + 20
    assert fc.limit_usd == 680.0     # 100 + 500 + 80
    assert fc.remaining_usd == 535.0
    # per_provider도 풀(월간+크레딧) 합산을 반영
    claude = next(p for p in fc.per_provider if p["provider"] == "claude")
    assert claude["used_usd"] == 125.0 and claude["limit_usd"] == 600.0


# ─── 이번달 소비(흐름): this_month_spend — 주기형 라이브 + 만료형 월초 델타(ADR 0024) ───


def test_this_month_spend_periodic_only_uses_live_used():
    # 순수 주기형(monthly-only): 라이브 used가 곧 이번달(월 리셋). 만료형 없음 → partial=False.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 40.0, 100.0, raw="spend")],
                            created_at="x")
    usd, partial = this_month_spend(conn, ["claude"], NOW, max_gap_minutes=30)
    assert usd == 40.0 and partial is False


def test_this_month_spend_expiring_uses_month_delta_not_cumulative():
    # 만료형 크레딧(9/10 만료)이 지난달부터 누적 $900. 월초 baseline이 있으면 이번달은
    # 델타(950-900=50)만 계상 — 누적 $950이 이번달로 새지 않는다. 주기형 라이브 40 + 50 = 90.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-01T00:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 10.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 900.0, 1000.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 40.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 950.0, 1000.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    usd, partial = this_month_spend(conn, ["claude"], NOW, max_gap_minutes=30,
                                    is_pooled=_pool_with("cinder"))
    assert usd == 90.0 and partial is False


def test_this_month_spend_expiring_no_baseline_is_partial():
    # 월초 경계 스냅샷 없음(6/10 스냅샷만) → 만료형 이번달 델타 계산 불가 → 주기형(40)만, partial=True.
    # 주기형은 늘 온전(경계 스냅샷 불필요)이라 데이터가 통째로 사라지지 않는다((B) 버킷별 분업).
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 40.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 950.0, 1000.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    usd, partial = this_month_spend(conn, ["claude"], NOW, max_gap_minutes=30,
                                    is_pooled=_pool_with("cinder"))
    assert usd == 40.0 and partial is True


def test_official_daily_rate_mtd_uses_month_flow_not_cumulative():
    # 트레일링 창 이전 베이스 없음 → MTD 폴백. 분자는 이번달 흐름(주기형10 + 만료형델타50=60),
    # raw 누적(monthly10 + cinder950=960) 아님. rate=60/8영업일=7.5(≠960/8=120).
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-01T00:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 10.0, 1000.0, raw="spend"),
                                     _ob("event", "event_credit", 900.0, 1000.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 10.0, 1000.0, raw="spend"),
                                     _ob("event", "event_credit", 950.0, 1000.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    rate = official_daily_rate(conn, ["claude"], NOW, 2, is_pooled=_pool_with("cinder"),
                               max_gap_minutes=30)
    assert rate == 7.5


def test_combined_forecast_position_cumulative_but_month_flow_delta():
    # 위치(used/limit/remaining)=라이브 누적 불변; this_month_used_usd=이번달 흐름(만료형은 델타).
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-01T00:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 10.0, 1000.0, raw="spend"),
                                     _ob("event", "event_credit", 900.0, 1000.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 10.0, 1000.0, raw="spend"),
                                     _ob("event", "event_credit", 950.0, 1000.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    ip = _pool_with("cinder")
    views = [official_view(conn, "claude", NOW, 0.04, is_pooled=ip, max_gap_minutes=30)]
    fc = combined_forecast(conn, views, NOW, is_pooled=ip, max_gap_minutes=30)
    # 위치 = 라이브 누적(공식 정본): 10 + 950 = 960 / 1000 + 1000 = 2000
    assert fc.used_usd == 960.0 and fc.limit_usd == 2000.0 and fc.remaining_usd == 1040.0
    # 흐름 = 이번달: 주기형 라이브 10 + 만료형 델타 50 = 60(누적 960이 이번달로 새지 않음)
    assert fc.this_month_used_usd == 60.0 and fc.this_month_partial is False


def test_forecast_hero_shows_this_month_flow_and_live_remaining():
    # 히어로: 헤드라인 숫자=이번달 흐름(60), 잔여=라이브 누적(1040) — ADR 0024.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-01T00:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 10.0, 1000.0, raw="spend"),
                                     _ob("event", "event_credit", 900.0, 1000.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 10.0, 1000.0, raw="spend"),
                                     _ob("event", "event_credit", 950.0, 1000.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    ip = _pool_with("cinder")
    views = [official_view(conn, "claude", NOW, 0.04, is_pooled=ip, max_gap_minutes=30)]
    fc = combined_forecast(conn, views, NOW, is_pooled=ip, max_gap_minutes=30)
    hero = _forecast_hero(fc)
    assert hero["this_month_used"] == 60.0      # 헤드라인 = 흐름
    assert hero["this_month_partial"] is False
    assert hero["remaining"] == 1040.0          # 잔여 = 라이브 누적(위치)


# --- 공식 사용량 스냅샷 이력: 통합 풀 used 시계열(ADR 0007) ---


def test_pool_used_history_empty():
    conn = connect(":memory:")
    assert pool_used_history(conn, "claude") == []


def test_pool_used_history_ascending_per_snapshot():
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend")],
                            created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:10:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 42.0, 100.0, raw="spend")],
                            created_at="x")
    hist = pool_used_history(conn, "claude")
    assert [u for _, u in hist] == [30.0, 42.0]
    assert hist[0][0] == parse_ts("2026-06-10T09:00:00+09:00")
    assert hist[0][0] < hist[1][0]


def test_pool_used_history_sums_usd_buckets_excludes_rate_window():
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 500.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST)),
                                     _ob("rate_window", "rate_window", None, None, raw="five_hour",
                                         unit="percent", util=40.0)],
                            created_at="x")
    # opt-in 크레딧(cinder)은 합산, rate_window는 USD 한도 없어 제외(ADR 0016/0007).
    hist = pool_used_history(conn, "claude", is_pooled=_pool_with("cinder"))
    assert hist == [(parse_ts("2026-06-10T09:00:00+09:00"), 155.0)]  # 30 + 125, rate_window 제외


def test_pool_used_history_excludes_snapshot_without_usd_limit():
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("rate_window", "rate_window", None, None, raw="five_hour",
                                         unit="percent", util=40.0)],
                            created_at="x")
    assert pool_used_history(conn, "claude") == []


# --- 세그먼트 분할(_segment_points): 리셋·갭에서 선 끊기(ADR 0007) ---


def _t(minute):
    return datetime(2026, 6, 10, 9, 0, tzinfo=KST) + timedelta(minutes=minute)


def test_segment_points_empty():
    assert _segment_points([], max_gap_minutes=30) == []


def test_segment_points_single():
    p = [(_t(0), 10.0)]
    assert _segment_points(p, max_gap_minutes=30) == [p]


def test_segment_points_monotonic_one_segment():
    p = [(_t(0), 10.0), (_t(10), 20.0), (_t(20), 25.0)]
    assert _segment_points(p, max_gap_minutes=30) == [p]


def test_segment_points_splits_on_reset_drop():
    p = [(_t(0), 80.0), (_t(10), 90.0), (_t(20), 5.0), (_t(30), 12.0)]  # 90→5 리셋
    assert _segment_points(p, max_gap_minutes=30) == [
        [(_t(0), 80.0), (_t(10), 90.0)], [(_t(20), 5.0), (_t(30), 12.0)]]


def test_segment_points_splits_on_time_gap():
    p = [(_t(0), 10.0), (_t(10), 20.0), (_t(50), 30.0)]  # 10→50 = 40분 > 30
    assert _segment_points(p, max_gap_minutes=30) == [
        [(_t(0), 10.0), (_t(10), 20.0)], [(_t(50), 30.0)]]


def test_segment_points_no_gap_break_within_threshold():
    p = [(_t(0), 10.0), (_t(25), 20.0)]  # 25 ≤ 30
    assert _segment_points(p, max_gap_minutes=30) == [p]


def test_segment_points_none_gap_only_reset_breaks():
    p = [(_t(0), 10.0), (_t(500), 20.0), (_t(510), 5.0)]  # max_gap None → 갭 무시, 리셋만
    assert _segment_points(p, max_gap_minutes=None) == [
        [(_t(0), 10.0), (_t(500), 20.0)], [(_t(510), 5.0)]]


def test_segment_points_noise_dip_stays_one_segment():
    """미세 하락(누적값 진동 노이즈)은 리셋이 아니라 한 세그먼트 유지(ADR 0007)."""
    p = [(_t(0), 44.88), (_t(10), 44.87), (_t(20), 44.88), (_t(30), 44.87)]
    assert _segment_points(p, max_gap_minutes=60) == [p]


def test_segment_points_drop_above_half_not_reset():
    """절반 이상 남은 하락은 리셋 아님 — 청구 리셋은 절반 미만 급락으로만 판정."""
    p = [(_t(0), 100.0), (_t(10), 60.0)]   # 40% 하락(60 > 50)
    assert _segment_points(p, max_gap_minutes=60) == [p]


# --- 통합 풀 과거 곡선(pool_history): forward-fill 합산 + 갭/리셋 끊기(ADR 0007) ---


def _seed_pool(conn, provider, kind, raw, samples, limit=100.0, unit="usd"):
    for minute, used in samples:
        insert_official_buckets(
            conn, provider=provider, fetched_at=_t(minute).isoformat(),
            buckets=[_ob("monthly", kind, used, limit, raw=raw, unit=unit)], created_at="x")


def test_pool_history_empty():
    conn = connect(":memory:")
    assert pool_history(conn, ["claude", "codex"], max_gap_minutes=30) == []


def test_pool_history_single_provider_segments_on_reset():
    conn = connect(":memory:")
    _seed_pool(conn, "claude", "monthly_limit", "spend", [(0, 80.0), (10, 90.0), (20, 5.0)])
    segs = pool_history(conn, ["claude"], max_gap_minutes=30)
    assert [[p["used_usd"] for p in s] for s in segs] == [[80.0, 90.0], [5.0]]
    assert segs[0][0]["ts"] == _t(0).isoformat()


def test_pool_history_two_providers_summed_forward_fill():
    conn = connect(":memory:")
    _seed_pool(conn, "claude", "monthly_limit", "spend", [(0, 30.0), (10, 40.0)])
    _seed_pool(conn, "codex", "codex_monthly", "individual_limit", [(0, 20.0), (10, 25.0)],
               limit=80.0, unit="credit")
    segs = pool_history(conn, ["claude", "codex"], max_gap_minutes=30)
    assert len(segs) == 1
    assert [p["used_usd"] for p in segs[0]] == [50.0, 65.0]  # 30+20, 40+25


def test_pool_history_breaks_on_provider_reset():
    conn = connect(":memory:")
    _seed_pool(conn, "claude", "monthly_limit", "spend", [(0, 30.0), (10, 40.0), (20, 50.0)])
    _seed_pool(conn, "codex", "codex_monthly", "individual_limit", [(0, 20.0), (10, 25.0), (20, 2.0)],
               limit=80.0, unit="credit")  # codex 리셋 t20: 25→2
    segs = pool_history(conn, ["claude", "codex"], max_gap_minutes=30)
    assert [[p["used_usd"] for p in s] for s in segs] == [[50.0, 65.0], [52.0]]


# --- pool_history_to_daily: 과거 곡선을 전망 차트 일-인덱스에 매핑(ADR 0007) ---


def test_pool_history_to_daily_maps_by_day_last_wins():
    now = datetime(2026, 6, 10, 12, 0, tzinfo=KST)
    days = list(range(1, 31))  # 1..30
    segs = [[
        {"ts": datetime(2026, 6, 3, 9, 0, tzinfo=KST).isoformat(), "used_usd": 10.0},
        {"ts": datetime(2026, 6, 3, 15, 0, tzinfo=KST).isoformat(), "used_usd": 12.0},  # 같은 날 → 나중 값
        {"ts": datetime(2026, 6, 5, 9, 0, tzinfo=KST).isoformat(), "used_usd": 20.0},
    ]]
    out = pool_history_to_daily(segs, days, now)
    assert out[2] == 12.0    # day 3 → index 2
    assert out[4] == 20.0    # day 5 → index 4
    assert out[0] is None and out[3] is None  # 데이터 없는 날 = None(끊김)


def test_pool_history_to_daily_ignores_other_months():
    now = datetime(2026, 6, 10, 12, 0, tzinfo=KST)
    days = list(range(1, 31))
    segs = [[{"ts": datetime(2026, 5, 20, 9, 0, tzinfo=KST).isoformat(), "used_usd": 99.0}]]  # 5월 → 무시
    assert all(v is None for v in pool_history_to_daily(segs, days, now))


def test_pool_history_to_daily_empty():
    now = datetime(2026, 6, 10, 12, 0, tzinfo=KST)
    days = list(range(1, 31))
    assert pool_history_to_daily([], days, now) == [None] * 30


# --- pool_daily_history: 날짜별 통합 풀 소비 델타 + 커버리지(ADR 0010) ---

_JUNE_START = datetime(2026, 6, 1, tzinfo=KST)
_JULY_START = datetime(2026, 7, 1, tzinfo=KST)


def _seed_days(conn, provider, kind, raw, day_used, limit=100.0, unit="usd"):
    """(day, used_usd) 표본을 2026-06-<day> 12:00 KST 스냅샷으로 적재."""
    for day, used in day_used:
        dt = datetime(2026, 6, day, 12, 0, tzinfo=KST)
        insert_official_buckets(
            conn, provider=provider, fetched_at=dt.isoformat(),
            buckets=[_ob("monthly", kind, used, limit, raw=raw, unit=unit)], created_at="x")


def test_pool_daily_history_basic_deltas():
    """일별 소비 = 인접 누적차. 첫 표본은 기준 0에서의 누적(=그 값)."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend", [(3, 10.0), (4, 25.0), (5, 40.0)])
    rows = pool_daily_history(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    covered = {r["date"]: r["used_usd"] for r in rows if r["covered"]}
    assert covered == {date(2026, 6, 3): 10.0, date(2026, 6, 4): 15.0, date(2026, 6, 5): 15.0}


def test_pool_daily_history_reset_counts_post_reset_only():
    """리셋(누적 하락)은 음수/거대 막대를 만들지 않고 post-reset 값만 계상한다."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend", [(3, 80.0), (4, 90.0), (5, 5.0)])
    rows = pool_daily_history(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    covered = {r["date"]: r["used_usd"] for r in rows if r["covered"]}
    assert covered[date(2026, 6, 3)] == 80.0
    assert covered[date(2026, 6, 4)] == 10.0
    assert covered[date(2026, 6, 5)] == 5.0   # 리셋: 90→5, -85이나 95가 아니라 5


def test_pool_daily_history_noise_dip_offsets_not_reset():
    """누적값 미세 진동(44.88↔44.87)은 리셋 오판 없이 상계되어 그날 소비≈0(ADR 0010)."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend",
               [(3, 23, 0, 44.88),                                  # 전날 마지막(기준)
                (4, 0, 0, 44.88), (4, 1, 0, 44.87), (4, 2, 0, 44.88)])  # 진동(노이즈)
    rows = pool_daily_history(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    covered = {r["date"]: r["used_usd"] for r in rows if r["covered"]}
    assert covered[date(2026, 6, 4)] == pytest.approx(0.0, abs=1e-9)  # 거대값(44.87) 아님


def test_pool_daily_history_gap_lumps_and_marks_uncovered():
    """갭 가로지른 소비는 첫 post-gap 날에 합산, 표본 없는 날은 covered=False·used=None(0 아님)."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend", [(3, 10.0), (6, 40.0)])  # day4,5 표본 없음
    rows = pool_daily_history(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    by_d = {r["date"]: r for r in rows}
    assert by_d[date(2026, 6, 3)]["used_usd"] == 10.0
    assert by_d[date(2026, 6, 6)]["used_usd"] == 30.0    # day4,5,6 소비가 day6에 lump
    assert by_d[date(2026, 6, 4)]["covered"] is False and by_d[date(2026, 6, 4)]["used_usd"] is None
    assert by_d[date(2026, 6, 1)]["covered"] is False    # 첫 표본 이전도 미커버
    assert len(rows) == 30                                # 구간 모든 날이 행으로(막대 x축)


def test_pool_daily_history_per_provider_breakdown_sums():
    """provider별 일별 델타가 분해로 노출되고, 통합 델타는 그 합과 정확히 일치(스택 무결성)."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend", [(3, 10.0), (4, 30.0)])
    _seed_days(conn, "codex", "codex_monthly", "individual_limit", [(3, 5.0), (4, 11.0)],
               limit=80.0, unit="credit")
    rows = pool_daily_history(conn, ["claude", "codex"], start=_JUNE_START, nxt=_JULY_START)
    by_d = {r["date"]: r for r in rows}
    assert by_d[date(2026, 6, 3)]["per_provider"] == {"claude": 10.0, "codex": 5.0}
    assert by_d[date(2026, 6, 4)]["per_provider"] == {"claude": 20.0, "codex": 6.0}  # 30-10, 11-5
    for r in rows:
        if r["covered"]:
            assert r["used_usd"] == round(sum(r["per_provider"].values()), 6)


def test_pool_daily_history_excludes_rate_window_and_empty_pool():
    """rate_window-only provider는 소진형 풀에 0 기여, 빈 풀은 전부 미커버(라우트 숨김 근거)."""
    conn = connect(":memory:")
    dt = datetime(2026, 6, 3, 12, 0, tzinfo=KST)
    insert_official_buckets(   # rate_window만(limit_usd None) — 소진형 아님
        conn, provider="claude", fetched_at=dt.isoformat(),
        buckets=[_ob("rate_window", "rate_window", None, None, raw="five_hour",
                     unit="percent", util=50.0)], created_at="x")
    rows = pool_daily_history(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    assert all(not r["covered"] for r in rows)
    empty = pool_daily_history(connect(":memory:"), ["claude", "codex"],
                               start=_JUNE_START, nxt=_JULY_START)
    assert all(not r["covered"] for r in empty)


# --- pool_hourly_history: 단일 날짜의 시간(0~23)별 통합 풀 소비 델타(ADR 0019) ---

_JUNE3 = datetime(2026, 6, 3, tzinfo=KST)


def test_pool_hourly_history_basic_deltas():
    """시간별 소비 = 인접 누적차. 첫 표본은 기준 0에서의 누적(=그 값). 24개 시각 행."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend",
               [(3, 9, 0, 10.0), (3, 10, 0, 25.0), (3, 14, 0, 40.0)])
    rows = pool_hourly_history(conn, ["claude"], day_start=_JUNE3)
    covered = {r["hour"]: r["used_usd"] for r in rows if r["covered"]}
    assert covered == {9: 10.0, 10: 15.0, 14: 15.0}
    assert len(rows) == 24


def test_pool_hourly_history_baseline_carries_from_prior_day():
    """당일 첫 시각 소비는 전날 마지막 표본 기준 델타 — 자정에 0으로 리셋되지 않는다."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend",
               [(2, 23, 0, 30.0), (3, 9, 0, 35.0), (3, 10, 0, 38.0)])
    rows = pool_hourly_history(conn, ["claude"], day_start=_JUNE3)
    covered = {r["hour"]: r["used_usd"] for r in rows if r["covered"]}
    assert covered == {9: 5.0, 10: 3.0}   # 35-30, 38-35 — 전날 $30 기준


def test_pool_hourly_history_reset_counts_post_reset_only():
    """하루 안의 리셋(누적 하락)은 음수/거대 막대 없이 post-reset 값만 계상."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend",
               [(3, 9, 0, 80.0), (3, 10, 0, 90.0), (3, 11, 0, 5.0)])
    rows = pool_hourly_history(conn, ["claude"], day_start=_JUNE3)
    covered = {r["hour"]: r["used_usd"] for r in rows if r["covered"]}
    assert covered == {9: 80.0, 10: 10.0, 11: 5.0}   # 리셋 90→5는 5(=-85/95 아님)


def test_pool_hourly_history_gap_lumps_and_marks_uncovered():
    """갭 가로지른 소비는 첫 post-gap 시각에 합산, 표본 없는 시각은 covered=False·used=None."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend",
               [(3, 9, 0, 10.0), (3, 14, 0, 40.0)])   # 10~13시 표본 없음
    rows = pool_hourly_history(conn, ["claude"], day_start=_JUNE3)
    by_h = {r["hour"]: r for r in rows}
    assert by_h[9]["used_usd"] == 10.0
    assert by_h[14]["used_usd"] == 30.0               # 10~14시 소비가 14시에 lump
    assert by_h[10]["covered"] is False and by_h[10]["used_usd"] is None
    assert by_h[0]["covered"] is False                # 첫 표본 이전도 미커버


def test_pool_hourly_history_per_provider_breakdown_sums():
    """provider별 시간 델타 분해 + 통합 델타 = 그 합(스택 무결성)."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend", [(3, 9, 0, 10.0), (3, 10, 0, 30.0)])
    _seed_snap(conn, "codex", "codex_monthly", "individual_limit",
               [(3, 9, 0, 5.0), (3, 10, 0, 11.0)], limit=80.0, unit="credit")
    rows = pool_hourly_history(conn, ["claude", "codex"], day_start=_JUNE3)
    by_h = {r["hour"]: r for r in rows}
    assert by_h[9]["per_provider"] == {"claude": 10.0, "codex": 5.0}
    assert by_h[10]["per_provider"] == {"claude": 20.0, "codex": 6.0}   # 30-10, 11-5
    for r in rows:
        if r["covered"]:
            assert r["used_usd"] == round(sum(r["per_provider"].values()), 6)


def test_pool_hourly_history_excludes_rate_window_and_empty_pool():
    """rate_window-only는 소진형 풀에 0 기여, 빈 풀은 전부 미커버(라우트 숨김 근거)."""
    conn = connect(":memory:")
    dt = datetime(2026, 6, 3, 9, 0, tzinfo=KST)
    insert_official_buckets(
        conn, provider="claude", fetched_at=dt.isoformat(),
        buckets=[_ob("rate_window", "rate_window", None, None, raw="five_hour",
                     unit="percent", util=50.0)], created_at="x")
    rows = pool_hourly_history(conn, ["claude"], day_start=_JUNE3)
    assert all(not r["covered"] for r in rows)
    empty = pool_hourly_history(connect(":memory:"), ["claude", "codex"], day_start=_JUNE3)
    assert all(not r["covered"] for r in empty) and len(empty) == 24


# --- pool_snapshots_by_day: 일 소비 재구성 드릴다운(ADR 0010) ---


def _seed_snap(conn, provider, kind, raw, samples, limit=100.0, unit="usd"):
    """(day, hour, minute, used_usd) 표본을 2026-06 KST 스냅샷으로 적재."""
    for day, hour, minute, used in samples:
        dt = datetime(2026, 6, day, hour, minute, tzinfo=KST)
        insert_official_buckets(
            conn, provider=provider, fetched_at=dt.isoformat(),
            buckets=[_ob("monthly", kind, used, limit, raw=raw, unit=unit)], created_at="x")


def test_pool_snapshots_by_day_first_ever_two_snaps():
    """추적 첫날: 직전 기준 없음(first_ever), 첫 표본 델타=누적값 전체, 합=일 소비."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend", [(3, 9, 0, 10.0), (3, 14, 0, 18.0)])
    by_day = pool_snapshots_by_day(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    detail = by_day[date(2026, 6, 3)]
    assert len(detail) == 1
    pd = detail[0]
    assert pd["provider"] == "claude"
    assert pd["first_ever"] is True and pd["baseline"] is None and pd["gap_days"] == 0
    assert [(s["delta"], s["reset"]) for s in pd["snapshots"]] == [(10.0, False), (8.0, False)]
    assert pd["total_delta"] == 18.0


def test_pool_snapshots_by_day_baseline_from_previous_day():
    """연속일: 직전 기준 = 어제 마지막 표본, 당일 첫 델타 = 당일값 - 기준, gap_days=1."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend",
               [(3, 23, 0, 10.0), (4, 9, 0, 25.0), (4, 18, 0, 30.0)])
    by_day = pool_snapshots_by_day(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    pd = by_day[date(2026, 6, 4)][0]
    assert pd["first_ever"] is False and pd["gap_days"] == 1
    assert pd["baseline"]["used_usd"] == 10.0
    assert pd["baseline"]["ts"] == datetime(2026, 6, 3, 23, 0, tzinfo=KST).isoformat()
    assert [s["delta"] for s in pd["snapshots"]] == [15.0, 5.0]   # 25-10, 30-25
    assert pd["total_delta"] == 20.0


def test_pool_snapshots_by_day_gap_lumps_into_first_post_gap_day():
    """3일 갭: 기준이 3일 전, gap_days=3 → 그 사이 소비가 이 날 델타에 합산됨을 노출."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend", [(3, 12, 0, 10.0), (6, 12, 0, 52.0)])
    by_day = pool_snapshots_by_day(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    assert date(2026, 6, 4) not in by_day and date(2026, 6, 5) not in by_day  # 표본 없는 날 키 없음
    pd = by_day[date(2026, 6, 6)][0]
    assert pd["gap_days"] == 3 and pd["baseline"]["used_usd"] == 10.0
    assert pd["snapshots"][0]["delta"] == 42.0   # 52-10, 4·5일치 흡수


def test_pool_snapshots_by_day_reset_flag():
    """리셋(누적 하락): 해당 표본 reset=True, 델타=post-reset 값(음수/거대값 아님)."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend", [(3, 9, 0, 90.0), (4, 9, 0, 5.0)])
    by_day = pool_snapshots_by_day(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    pd = by_day[date(2026, 6, 4)][0]
    assert pd["snapshots"][0]["reset"] is True and pd["snapshots"][0]["delta"] == 5.0


def test_pool_snapshots_by_day_noise_dip_not_reset():
    """미세 하락(노이즈)은 reset=False·델타는 부호 그대로 음수(post-reset 거대값 아님, ADR 0010)."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend",
               [(3, 23, 0, 44.88), (4, 9, 0, 44.87)])   # 0.01 하락 = 노이즈
    by_day = pool_snapshots_by_day(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    pd = by_day[date(2026, 6, 4)][0]
    assert pd["snapshots"][0]["reset"] is False
    assert pd["snapshots"][0]["delta"] == pytest.approx(-0.01, abs=1e-6)


def test_pool_snapshots_by_day_multi_provider_ordered():
    """여러 provider는 인자 순서대로 리스트에 분해돼, 합산 일 소비를 provider별로 설명한다."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend", [(3, 9, 0, 10.0), (4, 9, 0, 30.0)])
    _seed_snap(conn, "codex", "codex_monthly", "individual_limit", [(3, 10, 0, 5.0), (4, 10, 0, 11.0)],
               limit=80.0, unit="credit")
    by_day = pool_snapshots_by_day(conn, ["claude", "codex"], start=_JUNE_START, nxt=_JULY_START)
    detail = by_day[date(2026, 6, 4)]
    assert [pd["provider"] for pd in detail] == ["claude", "codex"]
    assert {pd["provider"]: pd["total_delta"] for pd in detail} == {"claude": 20.0, "codex": 6.0}


def test_pool_snapshots_by_day_reconciles_with_daily_history():
    """불변식: 각 날 detail의 per-provider total_delta 합 == pool_daily_history의 일 소비."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend",
               [(3, 9, 0, 10.0), (3, 20, 0, 14.0), (6, 9, 0, 40.0)])  # 갭(4,5) + 같은날 2표본
    _seed_snap(conn, "codex", "codex_monthly", "individual_limit",
               [(3, 9, 0, 5.0), (4, 9, 0, 9.0)], limit=80.0, unit="credit")
    by_day = pool_snapshots_by_day(conn, ["claude", "codex"], start=_JUNE_START, nxt=_JULY_START)
    daily = {r["date"]: r["used_usd"] for r in
             pool_daily_history(conn, ["claude", "codex"], start=_JUNE_START, nxt=_JULY_START)
             if r["covered"]}
    for d, detail in by_day.items():
        assert round(sum(pd["total_delta"] for pd in detail), 6) == daily[d]


def test_pool_snapshots_by_day_baseline_can_predate_range_start():
    """구간 첫날의 기준은 start 이전 표본일 수 있다(경계에서 델타 보존)."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend", [(3, 9, 0, 40.0), (5, 9, 0, 55.0)])
    start = datetime(2026, 6, 4, tzinfo=KST)   # day3 표본은 구간 밖, 기준으로만 쓰임
    by_day = pool_snapshots_by_day(conn, ["claude"], start=start, nxt=_JULY_START)
    assert date(2026, 6, 3) not in by_day
    pd = by_day[date(2026, 6, 5)][0]
    assert pd["first_ever"] is False and pd["baseline"]["used_usd"] == 40.0
    assert pd["snapshots"][0]["delta"] == 15.0   # 55-40


# --- official_period_glance: 공식 오늘·이번주 소비 글랜스(ADR 0011) ---

_NOW_WED = datetime(2026, 6, 10, 15, 0, tzinfo=KST)   # 수 15:00 — 주 시작=월 06-08


def test_period_glance_today_complete():
    """어제 baseline + 오늘 표본 → today.usd=오늘 델타, state=complete."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend", [(9, 20.0), (10, 30.0)])
    g = official_period_glance(conn, "claude", _NOW_WED)
    assert g.today.usd == 10.0           # 30-20
    assert g.today.state == "complete"


def test_period_glance_week_sums_covered_days():
    """이번주 = 월~오늘 covered 합. 주말 baseline(일) 있으면 complete."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend",
               [(7, 10.0), (8, 15.0), (9, 22.0), (10, 30.0)])   # 일 baseline + 월·화·수
    g = official_period_glance(conn, "claude", _NOW_WED)
    assert g.week.usd == 20.0            # (15-10)+(22-15)+(30-22)
    assert g.week.state == "complete"
    assert g.week.covered_days == 3 and g.week.total_days == 3   # 월·화·수


def test_period_glance_today_none_when_no_sample_today():
    """오늘 표본이 없으면 today.state=none, usd=None('$0'과 구분)."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend", [(8, 10.0), (9, 18.0)])  # 오늘(10) 없음
    g = official_period_glance(conn, "claude", _NOW_WED)
    assert g.today.state == "none"
    assert g.today.usd is None


def test_period_glance_today_partial_on_gap():
    """오늘 직전 baseline이 3일 전(gap_days≥2)이면 today partial + observed_from."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend", [(7, 10.0), (10, 40.0)])   # 8,9 갭
    g = official_period_glance(conn, "claude", _NOW_WED)
    assert g.today.state == "partial"
    assert g.today.usd == 30.0       # 40-10 (8,9,10 lump)
    assert g.today.observed_from == datetime(2026, 6, 10, 12, 0, tzinfo=KST).isoformat()


def test_period_glance_week_gap_robust_sum_preserved():
    """주중 하루 갭이어도 주 합계는 정확(총량 보존), covered_days<total_days로 노출."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend",
               [(7, 10.0), (8, 15.0), (10, 30.0)])   # 화(9) 갭
    g = official_period_glance(conn, "claude", _NOW_WED)
    assert g.week.usd == 20.0        # 30-10, 합 보존
    assert g.week.state == "complete"
    assert g.week.covered_days == 2 and g.week.total_days == 3


def test_period_glance_week_partial_when_first_ever():
    """주 시작 전 baseline이 전무(첫 표본이 이번주)면 week partial(추적 시작 — 이전 미분리)."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend", [(8, 10.0), (9, 18.0), (10, 30.0)])
    g = official_period_glance(conn, "claude", _NOW_WED)
    assert g.week.state == "partial"
    assert g.week.usd == 30.0        # 추적 시작분 포함


def test_period_glance_handles_reset_within_week():
    """기간 내 리셋(누적 하락)은 post-reset만 계상 — 거대/음수 막대 없음."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend",
               [(7, 80.0), (8, 90.0), (9, 5.0), (10, 12.0)])   # 화(9) 리셋 90→5
    g = official_period_glance(conn, "claude", _NOW_WED)
    assert g.week.usd == 22.0        # (90-80)+5+(12-5)
    assert g.today.usd == 7.0        # 12-5


# ── 기간별 사용량 카드 기반(ADR 0017) — 공식 이전 동일구간(official_span_spend) ──


def _seed_span_snap(conn, provider, dt, used, *, limit=200.0, kind="monthly_limit", raw="spend"):
    """단일 USD 풀 버킷 스냅샷을 임의 시각 dt(KST)로 적재(공식 이전 구간 테스트용)."""
    insert_official_buckets(conn, provider=provider, fetched_at=dt.isoformat(),
                            buckets=[_ob("monthly", kind, used, limit, raw=raw)], created_at="x")


def _Dt(day, hour):
    return datetime(2026, 6, day, hour, 0, tzinfo=KST)


def test_official_span_spend_boundary_diff():
    """경계가 max_gap 내 관측되면 [start, end] 소비 = 누적차(풀 합산)."""
    from tokenomy.official_aggregate import official_span_spend
    conn = connect(":memory:")
    _seed_span_snap(conn, "claude", _Dt(9, 0), 10.0)
    _seed_span_snap(conn, "claude", _Dt(9, 12), 16.0)
    _seed_span_snap(conn, "codex", _Dt(9, 0), 5.0)
    _seed_span_snap(conn, "codex", _Dt(9, 12), 8.0)
    spend = official_span_spend(conn, ["claude", "codex"], _Dt(9, 0), _Dt(9, 12),
                                max_gap_minutes=180)
    assert spend == 9.0    # (16-10)+(8-5)


def test_official_span_spend_reset_counts_post_reset():
    """구간 내 리셋(누적 하락)은 post-reset만 계상 — 월 경계 이전 구간도 성립."""
    from tokenomy.official_aggregate import official_span_spend
    conn = connect(":memory:")
    _seed_span_snap(conn, "claude", _Dt(9, 0), 180.0)    # 직전 주기 말(baseline)
    _seed_span_snap(conn, "claude", _Dt(9, 1), 5.0)      # 리셋 후 새 주기
    _seed_span_snap(conn, "claude", _Dt(9, 12), 20.0)
    spend = official_span_spend(conn, ["claude"], _Dt(9, 0), _Dt(9, 12), max_gap_minutes=180)
    assert spend == 20.0    # 5(post-reset) + (20-5)


def test_official_span_spend_none_when_start_gap():
    """start 직전 baseline이 max_gap보다 오래면(경계 미관측) None — leading-gap 부풀림 차단."""
    from tokenomy.official_aggregate import official_span_spend
    conn = connect(":memory:")
    _seed_span_snap(conn, "claude", _Dt(7, 12), 10.0)    # start 36h 전
    _seed_span_snap(conn, "claude", _Dt(9, 12), 16.0)
    assert official_span_spend(conn, ["claude"], _Dt(9, 0), _Dt(9, 12),
                               max_gap_minutes=180) is None


def test_official_span_spend_none_before_tracking():
    """start 이전 표본이 전무(추적 시작 이전)면 None."""
    from tokenomy.official_aggregate import official_span_spend
    conn = connect(":memory:")
    _seed_span_snap(conn, "claude", _Dt(9, 6), 12.0)     # 첫 표본이 start 이후
    assert official_span_spend(conn, ["claude"], _Dt(9, 0), _Dt(9, 12),
                               max_gap_minutes=180) is None


def test_official_span_spend_none_when_end_gap():
    """구간 마지막 표본이 end에서 max_gap보다 오래면(end 미관측) None — 일부 소비 누락."""
    from tokenomy.official_aggregate import official_span_spend
    conn = connect(":memory:")
    _seed_span_snap(conn, "claude", _Dt(9, 0), 10.0)
    _seed_span_snap(conn, "claude", _Dt(9, 2), 12.0)     # end 10h 전이 마지막
    assert official_span_spend(conn, ["claude"], _Dt(9, 0), _Dt(9, 12),
                               max_gap_minutes=180) is None


def test_official_aggregate_has_no_import_of_local_aggregate():
    # 분할 불변식: official_aggregate(공식)는 aggregate(로컬 롤업)를 import하지 않는다.
    # module-level·함수-지역 어느 쪽이든 위반 — 소스 문자열로 둘 다 막는다.
    src = Path(tokenomy.official_aggregate.__file__).read_text(encoding="utf-8")
    assert "from tokenomy.aggregate" not in src
    assert "import tokenomy.aggregate" not in src
    assert "from tokenomy import aggregate" not in src
