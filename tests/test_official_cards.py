"""공식 사용량 provider 카드 조립(views.official_cards) 단위 테스트 — ADR 0002."""
from datetime import datetime, timedelta

from tokenomy.aggregate import DayPoint
from tokenomy.clock import KST
from tokenomy.db import connect, insert_official_buckets, upsert_fetch_state
from tokenomy.official_parser import OfficialBucket
from tokenomy.web.views import (
    _fresh_label, _gauge_level, _sparkline_points, official_cards,
)
from tokenomy.official_aggregate import CombinedForecast
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
    assert card.get("fallback") is None
    g = card["gauges"][0]
    assert g["label"] == "월 사용 한도"
    assert g["level"] == "warn"          # 80% → warn
    assert g["caption"] == "$80.0 / $100"


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


# ── 카드: 추정 주간 게이지 제거(ADR 0012) — 공식 카드는 공식 수치만 ──────────────
def test_codex_card_has_no_weekly_estimate_gauge():
    conn = _conn()
    _seed(conn, "codex", [_bucket(bucket_kind="codex_monthly", label="월간",
                                  native_unit="credit", utilization=20.0,
                                  used_usd=40.0, limit_usd=200.0, remaining_usd=160.0)])
    # 로컬 Codex 사용이 있어도 월÷4 추정 게이지를 만들지 않는다(로컬 used를 공식 한도에 섞지 않음).
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,cost_usd,priced) "
                 "VALUES ('a','codex','s1','2026-06-20T10:00:00Z',3.0,1)")
    conn.commit()
    card = _card(official_cards(conn, {"tracked_providers": ["codex"]}, NOW), "codex")
    labels = [g["label"] for g in card["gauges"]]
    assert labels == ["월간"]                      # 월간 단일 게이지뿐(추정 주간 게이지 없음)
    assert "이번 주" not in labels


# ── 카드: 공식 없음 → 로컬 폴백 없이 깨끗한 no_data(ADR 0015 D8) ────────────────
def test_card_no_official_omits_local_fallback():
    conn = _conn()
    for k, ts, c in [("a", "2026-06-10T10:00:00Z", 5.0), ("b", "2026-06-12T10:00:00Z", 7.0)]:
        conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,cost_usd,priced) "
                     f"VALUES ('{k}','claude','s','{ts}',{c},1)")
    conn.commit()
    card = _card(official_cards(conn, {"tracked_providers": ["claude"]}, NOW), "claude")
    assert card["status"] == "no_data"
    assert card["gauges"] == []
    assert card.get("fallback") is None   # 로컬 추정$·스파크 폴백 미생성(공식만)


# ── 고스트(예측) + forecast 텍스트 — active 버킷 2스냅샷 ───────────────────────
def test_active_bucket_ghost_and_forecast():
    # opt-in한 코드네임 크레딧(ADR 0016)이 풀·active·고스트를 구동. (기본은 풀 제외라 opt-in 필요)
    # 기울기=공식(ADR 0015 D3, ADR 0024). 만료형 크레딧이라 이번달 흐름은 월초 baseline 대비 델타로
    # 잡는다 — 월초(6/1) 스냅샷을 두어(상주 운영) 크레딧의 지난달 누적이 이번달로 새지 않게 한다.
    conn = _conn()
    conn.execute(    # 로컬 소비(무시돼야 — 공식 기울기로 전환)
        "INSERT INTO messages(dedup_key,provider,session_id,project,ts,model,"
        "input_tokens,cache_creation,cache_read,cost_usd,priced) "
        "VALUES('k','claude','s','/p','2026-06-15T01:00:00Z','claude-opus-4-8',0,0,0,450.0,1)")
    conn.commit()
    reset = NOW + timedelta(days=10)
    month_start = datetime(2026, 6, 1, 0, 0, tzinfo=KST)   # 월초 baseline(만료형 이번달 델타 기준)
    old = (NOW - timedelta(days=5)).isoformat()
    new = NOW.isoformat()
    for fa, used, util in [(month_start.isoformat(), 600.0, 60.0),
                           (old, 700.0, 70.0), (new, 820.0, 82.0)]:
        insert_official_buckets(
            conn, provider="claude", fetched_at=fa, created_at=fa,
            buckets=[_bucket(bucket_key="event", bucket_kind="event_credit", raw_key="cinder_cove",
                             label="포함된 크레딧", used_usd=used, limit_usd=1000.0,
                             remaining_usd=1000.0 - used, utilization=util, resets_at=reset)])
    cfg = {"tracked_providers": ["claude"],
           "bucket_overrides": {"claude:cinder_cove": {"pooled": True}}}
    card = _card(official_cards(conn, cfg, NOW), "claude")
    g = card["gauges"][0]
    assert g["ghost_pct"] is not None and g["ghost_pct"] > g["fill_pct"]
    assert g["ghost_warn"] is True                  # 82% 소진 → dday(≥0.80) → 빨간 고스트
    assert "소진 예상" in (g["forecast"] or "")   # 고스트 의미를 텍스트로 명시(여유/부족 통일)


# ── 버킷 큐레이션: hidden(게이지 숨김) · label(라벨 교체) (ADR 0016) ──────────────
def test_card_hides_bucket_via_override():
    # 오버라이드 hidden=True → 해당 버킷은 게이지에서 사라진다(유령 천장 제거).
    conn = _conn()
    _seed(conn, "claude", [
        _bucket(),   # 월 사용 한도(raw_key="spend")
        _bucket(bucket_key="event", bucket_kind="event_credit", raw_key="amber_ladder",
                label="이벤트", used_usd=125.0, limit_usd=25000.0, remaining_usd=24875.0,
                utilization=0.5, resets_at=NOW + timedelta(days=30)),
    ])
    cfg = {"tracked_providers": ["claude"],
           "bucket_overrides": {"claude:amber_ladder": {"hidden": True}}}
    card = _card(official_cards(conn, cfg, NOW), "claude")
    labels = [g["label"] for g in card["gauges"]]
    assert "이벤트" not in labels and "월 사용 한도" in labels


def test_card_relabels_bucket_via_override():
    # 오버라이드 label → 게이지 라벨 교체(omelette_promotional → "Claude Design").
    conn = _conn()
    _seed(conn, "claude", [
        _bucket(bucket_key="promo", bucket_kind="promo", raw_key="omelette_promotional",
                label="별도/프로모션", native_unit="percent",
                used_usd=None, limit_usd=None, remaining_usd=None, utilization=40.0),
    ])
    cfg = {"tracked_providers": ["claude"],
           "bucket_overrides": {"claude:omelette_promotional": {"label": "Claude Design"}}}
    card = _card(official_cards(conn, cfg, NOW), "claude")
    assert card["gauges"][0]["label"] == "Claude Design"


def test_card_gauge_exposes_raw_key():
    # 디버그 발견 루프(ADR 0016 결정 6): 게이지가 raw_key(코드네임)를 실어 디버그 모드에서 노출 가능.
    conn = _conn()
    _seed(conn, "claude", [_bucket(bucket_key="event", bucket_kind="event_credit",
                                   raw_key="amber_ladder", label="이벤트",
                                   used_usd=125.0, limit_usd=25000.0, remaining_usd=24875.0,
                                   utilization=0.5)])
    # amber_ladder는 배포 카탈로그가 hidden → 노출 확인용으로 표시 강제(override hidden=False).
    cfg = {"tracked_providers": ["claude"],
           "bucket_overrides": {"claude:amber_ladder": {"hidden": False}}}
    card = _card(official_cards(conn, cfg, NOW), "claude")
    assert card["gauges"][0]["raw_key"] == "amber_ladder"


def test_card_shipped_catalog_hides_amber_and_labels_omelette():
    # 배포 카탈로그(config 오버라이드 없음)만으로 amber_ladder 숨김 + omelette "Claude Design".
    conn = _conn()
    _seed(conn, "claude", [
        _bucket(),   # 월 사용 한도
        _bucket(bucket_key="event", bucket_kind="event_credit", raw_key="amber_ladder",
                label="이벤트", used_usd=125.0, limit_usd=25000.0, remaining_usd=24875.0,
                utilization=0.5, resets_at=NOW + timedelta(days=30)),
        _bucket(bucket_key="promo", bucket_kind="promo", raw_key="omelette_promotional",
                label="별도/프로모션", native_unit="percent",
                used_usd=None, limit_usd=None, remaining_usd=None, utilization=40.0),
    ])
    card = _card(official_cards(conn, {"tracked_providers": ["claude"]}, NOW), "claude")
    labels = [g["label"] for g in card["gauges"]]
    assert "이벤트" not in labels                    # amber_ladder 숨김
    assert "Claude Design" in labels                 # omelette 재라벨
    assert "월 사용 한도" in labels


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


# ── 미니 카운트다운(reset_in): rate_window만, 최대 단위 1개(floor) ──────────────────
def _gauge(conn, label):
    card = _card(official_cards(conn, {"tracked_providers": ["claude"]}, NOW), "claude")
    return next(x for x in card["gauges"] if x["label"] == label)


def test_rate_window_reset_in_coarse_hours():
    conn = _conn()
    reset = NOW + timedelta(hours=2, minutes=35)
    _seed(conn, "claude", [_bucket(
        bucket_key="rate_window", raw_key="five_hour", bucket_kind="rate_window",
        label="5시간", native_unit="percent", used_usd=None, limit_usd=None,
        remaining_usd=None, utilization=42.0, resets_at=reset)])
    assert _gauge(conn, "5시간")["reset_in"] == "2시간"   # 2h35m → 최대 단위 1개(floor)


def test_rate_window_reset_in_minutes_under_hour():
    conn = _conn()
    reset = NOW + timedelta(minutes=40)
    _seed(conn, "claude", [_bucket(
        bucket_key="rate_window", raw_key="five_hour", bucket_kind="rate_window",
        label="5시간", native_unit="percent", used_usd=None, limit_usd=None,
        remaining_usd=None, utilization=10.0, resets_at=reset)])
    assert _gauge(conn, "5시간")["reset_in"] == "40분"


def test_rate_window_reset_in_days():
    conn = _conn()
    reset = NOW + timedelta(days=3, hours=23, minutes=12)
    _seed(conn, "claude", [_bucket(
        bucket_key="rate_window", raw_key="seven_day_opus", bucket_kind="rate_window",
        label="7일(Opus)", native_unit="percent", used_usd=None, limit_usd=None,
        remaining_usd=None, utilization=30.0, resets_at=reset)])
    assert _gauge(conn, "7일(Opus)")["reset_in"] == "3일"   # 일 단위만(시 생략)


def test_monthly_bucket_has_no_reset_in():
    # 월간은 카운트다운 불필요(rate_window 아님) — reset_in None.
    conn = _conn()
    _seed(conn, "claude", [_bucket(utilization=30.0)])   # monthly_limit, 다음 달 경계로 resets_at 채워짐
    assert _gauge(conn, "월 사용 한도")["reset_in"] is None


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


# ── 공식 기간 소비 글랜스(ADR 0011) ───────────────────────────────────────────
def _seed_day(conn, provider, day, used):
    """USD 버킷 1개를 2026-06-<day> 12:00 KST 스냅샷으로 적재(글랜스 이력용)."""
    dt = datetime(2026, 6, day, 12, 0, tzinfo=KST)
    insert_official_buckets(
        conn, provider=provider, fetched_at=dt.isoformat(), created_at=dt.isoformat(),
        buckets=[_bucket(used_usd=used, used_native=used,
                         remaining_usd=100.0 - used, remaining_native=100.0 - used,
                         utilization=used)])


def test_card_has_period_glance_for_usd_pool():
    """USD 풀 provider 카드에 공식 기간 소비 글랜스(오늘·이번주)가 붙는다."""
    conn = _conn()
    _seed_day(conn, "claude", 9, 20.0)    # 어제 baseline
    _seed_day(conn, "claude", 10, 30.0)   # 오늘(NOW6=6/10)
    card = _card(official_cards(conn, {"tracked_providers": ["claude"]}, NOW6), "claude")
    assert card["glance"] is not None
    assert card["glance"].today.usd == 10.0           # 30-20
    assert card["glance"].today.state == "complete"


def test_card_no_glance_for_rate_window_only():
    """rate-window-only(개인 구독제)는 USD 풀 없음 → 글랜스 None(줄 숨김, 스코프 게이트)."""
    conn = _conn()
    _seed(conn, "claude", [_bucket(
        bucket_key="rate_window", raw_key="five_hour", bucket_kind="rate_window",
        label="5시간 한도", native_unit="percent",
        used_usd=None, limit_usd=None, remaining_usd=None,
        used_native=50.0, limit_native=100.0, remaining_native=50.0, utilization=50.0)])
    card = _card(official_cards(conn, {"tracked_providers": ["claude"]}, NOW), "claude")
    assert card["glance"] is None


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


def test_forecast_chart_last_point_equals_hero_projected_used():
    # 정합 불변식(후보 1의 핵심): 차트 라인의 월말값 == 히어로 projected_used.
    # 둘 다 aggregate.forecast_month_line 한 walk를 공유하므로 구성상 일치한다.
    fc = _fc()
    out = forecast_chart_data(fc, _daily_june(), NOW6)
    last = next(v for v in reversed(out["line"]) if v is not None)
    assert last == fc.projected_used_usd


# ── 카드 forecast 텍스트: 여유 또는 소진 예상 ──────────────────────────────────
def test_card_forecast_text_surplus_or_exhaust():
    conn = _conn()
    _seed(conn, "claude", [_bucket(used_usd=30.0, limit_usd=100.0, utilization=30.0)])
    # 기울기=공식 월초누적(30/8≈3.75/일 → 작은 rate → 리셋 시 여유). 아래 로컬은 무시됨.
    conn.execute(
        "INSERT INTO messages(dedup_key,provider,session_id,project,ts,model,"
        "input_tokens,cache_creation,cache_read,cost_usd,priced) "
        "VALUES('k','claude','s','/p','2026-06-09T01:00:00Z','claude-opus-4-8',0,0,0,8.0,1)")
    conn.commit()
    cards = official_cards(conn, {"tracked_providers": ["claude"]}, now_kst=NOW6)
    card = _card(cards, "claude")
    fcs = [g.get("forecast") for g in card["gauges"] if g.get("forecast")]
    assert fcs, "active 버킷에 forecast 텍스트가 있어야 한다"
    assert ("여유" in fcs[0]) or ("소진 예상" in fcs[0])


# ── 활성 게이트(ADR 0005): 비활성 provider는 공식·로컬 데이터가 있어도 카드 없음 ──
def test_inactive_provider_with_data_is_omitted():
    conn = _conn()
    _seed(conn, "claude", [_bucket()])
    _seed(conn, "codex", [_bucket(bucket_kind="codex_monthly", label="월간 크레딧 한도")])
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,cost_usd,priced) "
                 "VALUES ('a','codex','s1','2026-06-20T10:00:00Z',3.0,1)")
    conn.commit()
    # codex는 공식 스냅샷 + 로컬 데이터 둘 다 있지만 활성이 아니므로 카드를 띄우지 않는다.
    cards = official_cards(conn, {"tracked_providers": ["claude"]}, NOW)
    assert [c["provider"] for c in cards] == ["claude"]


def test_empty_active_yields_no_cards():
    conn = _conn()
    _seed(conn, "claude", [_bucket()])
    assert official_cards(conn, {"tracked_providers": []}, NOW) == []
