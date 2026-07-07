"""전망(outlook) 조립 모듈 — config 팬아웃 정본 + official_view 팬아웃+combined_forecast 접기."""
from datetime import datetime

from tokenomy.clock import KST
from tokenomy.official_aggregate import CombinedForecast, combined_forecast, official_view
from tokenomy.db import connect, insert_official_buckets
from tokenomy.official_parser import OfficialBucket
from tokenomy.forecast import forecast_params, outlook, FParams

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=KST)  # day 10 of 30


def _ob(key, kind, used, limit, *, raw="spend", unit="usd", util=None):
    return OfficialBucket(
        bucket_key=key, raw_key=raw, bucket_kind=kind, label=key, native_unit=unit,
        used_usd=used, limit_usd=limit, remaining_usd=(limit - used) if limit else None,
        used_native=used, limit_native=limit, remaining_native=(limit - used) if limit else None,
        utilization=util if util is not None else (used / limit * 100 if limit else None),
        resets_at=None)


def _seed(conn, provider, kind, used, limit, *, raw="spend", unit="usd"):
    insert_official_buckets(conn, provider=provider, fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", kind, used, limit, raw=raw, unit=unit)],
                            created_at="x")


# ── forecast_params — config 팬아웃 정본 1곳 ────────────────────────────────
def test_forecast_params_resolves_config_fanout():
    config = {
        "tracked_providers": ["claude", "codex"],
        "credit_to_usd": 0.05,
        "forecast_settings": {"rate_window_weeks": 3},
        "official_fetch": {"min_interval_minutes": 10},
    }
    p = forecast_params(config)
    assert isinstance(p, FParams)
    assert p.active == ["claude", "codex"]
    assert p.ctu == 0.05
    assert p.weeks == 3
    assert p.max_gap == 30                                   # min_interval 10 × 3
    # is_pooled: 안정 월 한도만 풀, 미큐레이션 회전 코드네임 크레딧은 모양 기본값=제외(ADR 0016).
    assert p.is_pooled("claude", "spend", "monthly_limit") is True
    assert p.is_pooled("claude", "zzz_unknown_credit", "event_credit") is False


# ── outlook — 전망 조립을 한 문(conn, config, now)으로 ──────────────────────
def test_outlook_none_when_no_official_limits():
    # 한도 있는 provider 전무 → None(히어로 숨김).
    conn = connect(":memory:")
    config = {"tracked_providers": ["claude", "codex"], "credit_to_usd": 0.04}
    assert outlook(conn, config, NOW) is None


def test_outlook_assembles_pool_forecast_from_config():
    # 대시보드가 손조립하던 팬아웃+combined_forecast를 config 한 문으로 — 동일 결과.
    conn = connect(":memory:")
    _seed(conn, "claude", "monthly_limit", 40.0, 200.0)     # 공식 월초누적 40/8영업일=5/일
    config = {"tracked_providers": ["claude", "codex"], "credit_to_usd": 0.04}
    fc = outlook(conn, config, NOW)
    assert isinstance(fc, CombinedForecast)
    assert fc.providers == ["claude"]
    assert fc.used_usd == 40.0 and fc.limit_usd == 200.0
    assert fc.daily_rate_usd == 5.0
    assert fc.projected_used_usd == 110.0                   # 40 + 5*14영업일
