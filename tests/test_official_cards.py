"""공식 사용량 provider 카드 조립(views.official_cards) 단위 테스트 — ADR 0002."""
from datetime import datetime, timedelta

from tokenomy.aggregate import KST, DayPoint
from tokenomy.db import connect, insert_official_buckets, upsert_fetch_state
from tokenomy.official_parser import OfficialBucket
from tokenomy.web.views import (
    _fresh_label, _gauge_level, _sparkline_points, official_cards,
)
from tokenomy.aggregate import CombinedForecast
from tokenomy.web.views import _forecast_hero, forecast_chart_data
from datetime import date as _date

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=KST)
NOW6 = datetime(2026, 6, 10, 12, 0, tzinfo=KST)   # 6/10(수): 차트 인덱스 9가 오늘


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


def _card(cards, provider):
    return next(c for c in cards if c["provider"] == provider)


# ── 임계 경계(녹<75 · 앰버75~90 · 적≥90) ──────────────────────────────────────
def test_gauge_level_thresholds():
    assert _gauge_level(0) == "ok"
    assert _gauge_level(74.9) == "ok"
    assert _gauge_level(75) == "warn"
    assert _gauge_level(89.9) == "warn"
    assert _gauge_level(90) == "exceeds"
    assert _gauge_level(100) == "exceeds"
    assert _gauge_level(None) == "ok"


def test_fresh_label():
    assert _fresh_label(None) is None
    assert _fresh_label(0) == "방금"
    assert _fresh_label(8) == "8분 전"
    assert _fresh_label(125) == "2시간 전"
    assert _fresh_label(2880) == "2일 전"


# ── 스파크라인 ────────────────────────────────────────────────────────────────
def test_sparkline_none_for_too_few_points():
    assert _sparkline_points([]) is None
    assert _sparkline_points([DayPoint(1, 5.0)]) is None


def test_sparkline_points_skip_future_none():
    s = [DayPoint(1, 1.0), DayPoint(2, 3.0), DayPoint(3, None)]
    pts = _sparkline_points(s)
    assert pts is not None
    assert len(pts.split(" ")) == 2   # 미래(None) 구간 제외


# ── 카드: 공식 OK ─────────────────────────────────────────────────────────────
def test_card_official_ok_gauge_levels():
    conn = _conn()
    _seed(conn, "claude", [_bucket(utilization=80.0, used_usd=80.0, remaining_usd=20.0)])
    card = _card(official_cards(conn, {"tracked_providers": ["claude"]}, NOW), "claude")
    assert card["status"] == "ok"
    assert card["fallback"] is None
    g = card["gauges"][0]
    assert g["label"] == "월 사용 한도"
    assert g["level"] == "warn"          # 80% → warn
    assert g["estimated"] is False
    assert g["caption"] == "$80.00 / $100"


def test_card_exhausted_label():
    conn = _conn()
    _seed(conn, "claude", [_bucket(utilization=100.0, used_usd=100.0, remaining_usd=0.0)])
    card = _card(official_cards(conn, {"tracked_providers": ["claude"]}, NOW), "claude")
    g = card["gauges"][0]
    assert g["level"] == "exceeds"
    assert g["exhausted"] is True


# ── 카드: fetch 실패 → 스탈 게이지 + 경고 노트 ─────────────────────────────────
def test_card_stale_gauge_on_fetch_error():
    conn = _conn()
    _seed(conn, "codex", [_bucket(bucket_kind="codex_monthly", label="월간 크레딧 한도",
                                  native_unit="credit", utilization=18.0,
                                  used_usd=42.0, limit_usd=235.0, remaining_usd=193.0)])
    upsert_fetch_state(conn, "codex", last_attempt_at=NOW.isoformat(),
                       last_success_at=None, last_status="auth_error", last_error="HTTP 401")
    card = _card(official_cards(conn, {"tracked_providers": ["codex"]}, NOW), "codex")
    assert card["status"] == "error"
    assert card["gauges"]                       # 직전 스냅샷 게이지 유지
    assert "Codex CLI" in (card["note"] or "")


# ── 카드: Codex 주간 추정 게이지(해치) ────────────────────────────────────────
def test_codex_card_has_weekly_estimate_gauge():
    conn = _conn()
    _seed(conn, "codex", [_bucket(bucket_kind="codex_monthly", label="월간 크레딧 한도",
                                  native_unit="credit", utilization=20.0,
                                  used_usd=40.0, limit_usd=200.0, remaining_usd=160.0)])
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,cost_usd,priced) "
                 "VALUES ('a','codex','s1','2026-06-20T10:00:00Z',3.0,1)")
    conn.commit()
    card = _card(official_cards(conn, {"tracked_providers": ["codex"]}, NOW), "codex")
    est = [g for g in card["gauges"] if g["estimated"]]
    assert est and est[0]["label"] == "이번 주"
    assert est[0]["caption"].endswith("/ $50")   # 200 ÷ 4 = 50 추정 한도


# ── 카드: 공식 없음 → 사용량 전용 폴백 ────────────────────────────────────────
def test_card_fallback_uses_local_estimate_and_spark():
    conn = _conn()
    for k, ts, c in [("a", "2026-06-10T10:00:00Z", 5.0), ("b", "2026-06-12T10:00:00Z", 7.0)]:
        conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,cost_usd,priced) "
                     f"VALUES ('{k}','claude','s','{ts}',{c},1)")
    conn.commit()
    card = _card(official_cards(conn, {"tracked_providers": ["claude"]}, NOW), "claude")
    assert card["status"] == "no_data"
    assert card["gauges"] == []
    assert card["fallback"]["estimate_usd"] == 12.0
    assert card["fallback"]["spark"] is not None


# ── 고스트(예측) + forecast 텍스트 — active 버킷 2스냅샷 ───────────────────────
def test_active_bucket_ghost_and_forecast():
    conn = _conn()
    # 로컬 소비 시드: rate = 450 / 15영업일(6/1~6/21) = 30/일
    # → 예상 used = 820 + 30×7(영업일 6/22~6/30) = 1030 > 1000 → 고스트
    # → exhaust = ceil(180/30) = 6영업일 후 = 6/29 < 7/1 → dday_warning=True
    conn.execute(
        "INSERT INTO messages(dedup_key,provider,session_id,project,ts,model,"
        "input_tokens,cache_creation,cache_read,cost_usd,priced) "
        "VALUES('k','claude','s','/p','2026-06-15T01:00:00Z','claude-opus-4-8',0,0,0,450.0,1)")
    conn.commit()
    reset = NOW + timedelta(days=10)
    old = (NOW - timedelta(days=5)).isoformat()
    new = NOW.isoformat()
    for fa, used, util in [(old, 700.0, 70.0), (new, 820.0, 82.0)]:
        insert_official_buckets(
            conn, provider="claude", fetched_at=fa, created_at=fa,
            buckets=[_bucket(bucket_key="event", bucket_kind="event_credit",
                             label="포함된 크레딧", used_usd=used, limit_usd=1000.0,
                             remaining_usd=1000.0 - used, utilization=util, resets_at=reset)])
    card = _card(official_cards(conn, {"tracked_providers": ["claude"]}, NOW), "claude")
    g = card["gauges"][0]
    assert g["ghost_pct"] is not None and g["ghost_pct"] > g["fill_pct"]
    assert g["ghost_warn"] is True                  # 82% + 리셋 전 소진 → 빨간 고스트
    assert "소진 예상" in (g["forecast"] or "")   # 고스트 의미를 텍스트로 명시(여유/부족 통일)


# ── 5시간 한도(개인 구독) — sub에 분 단위 리셋 시각 + 잔여 카운트다운 ─────────────
def test_five_hour_window_sub_has_time_and_countdown():
    conn = _conn()
    reset = NOW + timedelta(hours=2, minutes=35)   # 2026-06-21 14:35 KST
    _seed(conn, "claude", [_bucket(
        bucket_key="rate_window", raw_key="five_hour", bucket_kind="rate_window",
        label="5시간 한도", native_unit="percent",
        used_native=None, limit_native=None, remaining_native=None,
        used_usd=None, limit_usd=None, remaining_usd=None,
        utilization=42.0, resets_at=reset)])
    card = _card(official_cards(conn, {"tracked_providers": ["claude"]}, NOW), "claude")
    g = next(x for x in card["gauges"] if x["label"] == "5시간 한도")
    assert g["sub"] == "리셋 2026-06-21 14:35 · 2시간 35분 후"   # 날짜만으론 무의미 → 분+카운트다운


def test_five_hour_window_sub_minutes_only_when_under_hour():
    conn = _conn()
    reset = NOW + timedelta(minutes=40)            # 1시간 미만 → "분 후"만
    _seed(conn, "claude", [_bucket(
        bucket_key="rate_window", raw_key="five_hour", bucket_kind="rate_window",
        label="5시간 한도", native_unit="percent",
        used_usd=None, limit_usd=None, remaining_usd=None,
        utilization=10.0, resets_at=reset)])
    card = _card(official_cards(conn, {"tracked_providers": ["claude"]}, NOW), "claude")
    g = next(x for x in card["gauges"] if x["label"] == "5시간 한도")
    assert g["sub"] == "리셋 2026-06-21 12:40 · 40분 후"


def test_weekly_window_sub_has_time_and_day_countdown():
    # 주간 모델 창도 rate_window라 동일 처리 — 잔여가 하루 이상이면 '일·시'로 좁혀 표기
    conn = _conn()
    reset = NOW + timedelta(days=3, hours=23, minutes=12)   # 2026-06-25 11:12 KST
    _seed(conn, "claude", [_bucket(
        bucket_key="rate_window", raw_key="seven_day_opus", bucket_kind="rate_window",
        label="주간 · Opus 전용", native_unit="percent",
        used_usd=None, limit_usd=None, remaining_usd=None,
        utilization=30.0, resets_at=reset)])
    card = _card(official_cards(conn, {"tracked_providers": ["claude"]}, NOW), "claude")
    g = next(x for x in card["gauges"] if x["label"] == "주간 · Opus 전용")
    assert g["sub"] == "리셋 2026-06-25 11:12 · 3일 23시간 후"   # 분은 노이즈라 생략


# ── 사이드바 신선도: 마지막 수집 실행 시각(최신 메시지 ts 아님) ─────────────────
def test_sidebar_freshness_returns_last_ingest_time():
    from tokenomy.freshness import record_ingest
    from tokenomy.web.views import sidebar_freshness
    conn = _conn()
    record_ingest(conn, NOW)
    assert sidebar_freshness(conn) == NOW.isoformat()


def test_sidebar_freshness_none_when_never_ingested():
    from tokenomy.web.views import sidebar_freshness
    conn = _conn()
    assert sidebar_freshness(conn) is None


# ── 게이트: tracked도 아니고 데이터도 없으면 카드 없음 ─────────────────────────
def test_untracked_no_data_provider_omitted():
    conn = _conn()
    _seed(conn, "claude", [_bucket()])
    cards = official_cards(conn, {"tracked_providers": ["claude"]}, NOW)
    assert [c["provider"] for c in cards] == ["claude"]   # codex는 생략


# ── 통합 월말 전망 히어로 ─────────────────────────────────────────────────────
def _fc(**kw) -> CombinedForecast:
    base = dict(
        providers=["claude"], used_usd=40.0, limit_usd=200.0, remaining_usd=160.0,
        daily_rate_usd=10.0, bdays_remaining=14, projected_used_usd=180.0,
        projected_remaining_usd=20.0, exhaust_date=None, is_exhausted=False,
        per_provider=[{"provider": "claude", "used_usd": 40.0, "limit_usd": 200.0}],
        month_end=_date(2026, 6, 30),
    )
    base.update(kw)
    return CombinedForecast(**base)


def test_forecast_hero_none_passthrough():
    assert _forecast_hero(None) is None


def test_forecast_hero_surplus():
    h = _forecast_hero(_fc())
    assert h["level"] == "surplus"
    assert h["surplus"] == 20.0
    assert h["pct_now"] == 20          # 40/200
    assert h["providers_label"] == "Claude"


def test_forecast_hero_shortfall():
    h = _forecast_hero(_fc(limit_usd=100.0, remaining_usd=60.0, projected_used_usd=180.0,
                           projected_remaining_usd=-80.0, exhaust_date=_date(2026, 6, 18)))
    assert h["level"] == "shortfall"
    assert h["shortfall_abs"] == 80.0
    assert h["exhaust_date"] == "06-18"


def test_forecast_hero_exhausted():
    h = _forecast_hero(_fc(used_usd=100.0, limit_usd=100.0, remaining_usd=0.0, is_exhausted=True,
                           projected_used_usd=None, projected_remaining_usd=None))
    assert h["level"] == "exhausted"


def test_forecast_hero_insufficient():
    h = _forecast_hero(_fc(daily_rate_usd=None, projected_used_usd=None, projected_remaining_usd=None))
    assert h["level"] == "insufficient"
    assert h["remaining"] == 160.0


def test_forecast_hero_multi_provider_label():
    h = _forecast_hero(_fc(providers=["claude", "codex"]))
    assert h["providers_label"] == "Claude + Codex"


def _daily_june():
    # 6월(30일) 일별 점. day 번호만 쓰므로 누적값은 0으로 충분.
    return [DayPoint(day=d, cumulative_cost=0.0) for d in range(1, 31)]


def test_forecast_chart_none_when_no_forecast():
    assert forecast_chart_data(None, _daily_june(), NOW6) == {"limit": None, "line": None}


def test_forecast_chart_limit_only_when_rate_missing():
    fc = _fc(daily_rate_usd=None, projected_used_usd=None, projected_remaining_usd=None)
    out = forecast_chart_data(fc, _daily_june(), NOW6)
    assert out["limit"] == 200.0 and out["line"] is None


def test_forecast_chart_line_anchored_on_official_used():
    out = forecast_chart_data(_fc(), _daily_june(), NOW6)   # used40 rate10, NOW6=6/10
    line = out["line"]
    assert out["limit"] == 200.0
    assert line[8] is None                 # 6/9(인덱스8) — 오늘 이전 → None
    assert line[9] == 40.0                 # 6/10(인덱스9=오늘) = 공식 used
    assert line[29] == 180.0               # 6/30(인덱스29=월말) = used + rate*14


# ── 카드 forecast 텍스트: 여유 또는 소진 예상 ──────────────────────────────────
def test_card_forecast_text_surplus_or_exhaust():
    conn = _conn()
    _seed(conn, "claude", [_bucket(used_usd=30.0, limit_usd=100.0, utilization=30.0)])
    # 로컬 소비로 기울기 발생(작은 rate → 리셋 시 여유)
    conn.execute(
        "INSERT INTO messages(dedup_key,provider,session_id,project,ts,model,"
        "input_tokens,cache_creation,cache_read,cost_usd,priced) "
        "VALUES('k','claude','s','/p','2026-06-09T01:00:00Z','claude-opus-4-8',0,0,0,8.0,1)")
    conn.commit()
    cards = official_cards(conn, {}, now_kst=NOW6)
    card = _card(cards, "claude")
    fcs = [g.get("forecast") for g in card["gauges"] if g.get("forecast")]
    assert fcs, "active 버킷에 forecast 텍스트가 있어야 한다"
    assert ("여유" in fcs[0]) or ("소진 예상" in fcs[0])
