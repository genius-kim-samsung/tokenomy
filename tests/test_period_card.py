"""기간별 사용량 카드(ADR 0017) — 총지출+share 통합 카드. views 계층 조립 검증.

모드 게이트(ADR 0015): 엔터=공식 통합 풀(스냅샷 이력 델타+라이브 pool_used),
구독/로컬=로컬 JSONL 달력 기간 합. 페이스 신호=이전 기간 *전체* 대비 ▲▼% + 기준값
병기(ADR 0018이 0017 §4 same-span을 supersede — 오늘 vs 어제 하루·이번주 vs 지난주·
이번달 vs 지난달, 기준값 prev_usd/prev_label을 화면에 함께 보여준다).
"""
from __future__ import annotations

from datetime import datetime

from tokenomy.clock import KST
from tokenomy.db import connect, insert_official_buckets
from tokenomy.official_parser import OfficialBucket

NOW = datetime(2026, 6, 10, 15, 0, tzinfo=KST)   # 수 15:00 — 주 시작=월 06-08


def _ps(usd, state="complete"):
    from tokenomy.official_aggregate import PeriodSpend
    return PeriodSpend(usd=usd, state=state)


def test_pace_up_down_flat_none():
    """페이스 = 현재 vs 이전 기간 변화율. 이전 없음/0이면 None(비교 불가)."""
    from tokenomy.web.views import _pace
    assert _pace(120.0, 100.0) == {"dir": "up", "pct": 20}
    assert _pace(80.0, 100.0) == {"dir": "down", "pct": 20}
    assert _pace(100.2, 100.0) == {"dir": "flat", "pct": 0}   # <0.5% → flat
    assert _pace(5.0, None) is None       # 이전 데이터 없음
    assert _pace(5.0, 0.0) is None        # 이전 0 → 비율 불가
    assert _pace(None, 100.0) is None     # 현재 없음


def test_share_text_local_note_tail():
    """구독/로컬 모드 공유 문구 — 헤더에 (API 단가 환산) 꼬리표, 한도% 없음."""
    from tokenomy.web.views import ShareRow, build_share_text
    rows = [ShareRow(label="Claude", today=_ps(3.0), week=_ps(14.0),
                     month_usd=112.0, util_pct=None)]
    text = build_share_text(rows, "2026-06-24", note="API 단가 환산")
    assert text.splitlines()[0] == "AI 사용량 (2026-06-24, KST) · API 단가 환산"
    assert "(한도" not in text   # 구독은 한도 없음 → 한도% 생략
    assert "· Claude 오늘 $3.0 · 이번주 $14.0 · 이번달 $112.0" in text


def test_share_text_note_default_unchanged():
    """note 미지정(엔터 기본)이면 헤더 꼬리표 없음 — 기존 동작 보존."""
    from tokenomy.web.views import ShareRow, build_share_text
    rows = [ShareRow(label="Claude", today=_ps(3.0), week=_ps(14.0),
                     month_usd=112.0, util_pct=28)]
    text = build_share_text(rows, "2026-06-24")
    assert text.splitlines()[0] == "AI 사용량 (2026-06-24, KST)"


# ── period_card_context: 모드 게이트 + 페이스 ─────────────────────────────────


def _msg(conn, ts, cost, provider="claude"):
    conn.execute(
        "INSERT INTO messages(dedup_key,provider,session_id,ts,cost_usd,priced) "
        "VALUES(?,?,?,?,?,1)", (f"{ts}-{cost}-{provider}", provider, "s", ts, cost))
    conn.commit()


def _snap(conn, provider, dt, used, *, limit=200.0):
    insert_official_buckets(
        conn, provider=provider, fetched_at=dt.isoformat(), created_at=dt.isoformat(),
        buckets=[OfficialBucket(
            bucket_key="monthly", raw_key="spend", bucket_kind="monthly_limit",
            label="월", native_unit="usd", used_native=used, limit_native=limit,
            remaining_native=limit - used, used_usd=used, limit_usd=limit,
            remaining_usd=limit - used, utilization=used / limit * 100, resets_at=None)])


def _snap_credit(conn, dt, monthly_used, credit_used, *, provider="claude",
                 monthly_limit=1000.0, credit_limit=1000.0, credit_raw="cinder"):
    """월간(주기형) + 만료형 크레딧 버킷을 한 스냅샷에 적재(ADR 0024 테스트용)."""
    from datetime import datetime as _dt
    insert_official_buckets(
        conn, provider=provider, fetched_at=dt.isoformat(), created_at=dt.isoformat(),
        buckets=[
            OfficialBucket(bucket_key="monthly", raw_key="spend", bucket_kind="monthly_limit",
                           label="월", native_unit="usd", used_native=monthly_used,
                           limit_native=monthly_limit, remaining_native=monthly_limit - monthly_used,
                           used_usd=monthly_used, limit_usd=monthly_limit,
                           remaining_usd=monthly_limit - monthly_used,
                           utilization=monthly_used / monthly_limit * 100, resets_at=None),
            OfficialBucket(bucket_key="event", raw_key=credit_raw, bucket_kind="event_credit",
                           label="이벤트", native_unit="usd", used_native=credit_used,
                           limit_native=credit_limit, remaining_native=credit_limit - credit_used,
                           used_usd=credit_used, limit_usd=credit_limit,
                           remaining_usd=credit_limit - credit_used,
                           utilization=credit_used / credit_limit * 100,
                           resets_at=_dt(2026, 9, 10, tzinfo=KST))])


# 만료형 크레딧을 풀에 opt-in하는 엔터 config(ADR 0016 오버라이드 + ADR 0024).
_ENT_CREDIT_CFG = {"tracked_providers": ["claude"], "account_mode": "enterprise",
                   "bucket_overrides": {"claude:cinder": {"pooled": True}}}


def _periods(ctx):
    return {p["key"]: p for p in ctx["periods"]}


def test_period_card_local_mode_with_pace():
    """구독/로컬: 오늘/이번주/이번달=로컬 달력 합 + 이전 *전체* 기간 페이스·기준값, share 꼬리표."""
    from tokenomy.web.views import period_card_context
    conn = connect(":memory:")
    # NOW=6/10(수) 15:00, 주 시작=월 6/8, 달 시작 6/1.
    _msg(conn, "2026-06-10T01:00:00Z", 5.0)   # KST 6/10 10:00 (오늘)
    _msg(conn, "2026-06-09T01:00:00Z", 3.0)   # KST 6/09 10:00 (이번주·어제 전체)
    _msg(conn, "2026-06-03T01:00:00Z", 2.0)   # KST 6/03 10:00 (이번달·지난주[6/1~6/7] 전체)
    _msg(conn, "2026-05-05T01:00:00Z", 4.0)   # KST 5/05 10:00 (지난달[5월] 전체)
    ctx = period_card_context(conn, {"tracked_providers": ["claude"],
                                     "account_mode": "subscription"}, NOW)
    assert ctx["mode"] == "local" and ctx["has_data"] is True
    p = _periods(ctx)
    assert p["오늘"]["usd"] == 5.0 and p["이번주"]["usd"] == 8.0 and p["이번달"]["usd"] == 10.0
    # 기준값 = 이전 *전체* 기간 합(어제 전체 3·지난주 전체 2·지난달 전체 4) — 화면 병기용.
    assert (p["오늘"]["prev_usd"], p["오늘"]["prev_label"]) == (3.0, "어제")
    assert (p["이번주"]["prev_usd"], p["이번주"]["prev_label"]) == (2.0, "지난주")
    assert (p["이번달"]["prev_usd"], p["이번달"]["prev_label"]) == (4.0, "지난달")
    assert p["오늘"]["pace"] == {"dir": "up", "pct": 67}      # 5 vs 3
    assert p["이번주"]["pace"] == {"dir": "up", "pct": 300}   # 8 vs 2
    assert p["이번달"]["pace"] == {"dir": "up", "pct": 150}   # 10 vs 4
    assert "API 단가 환산" in ctx["share_text"]
    assert "(한도" not in ctx["share_text"]
    assert "이 기기" in ctx["source_label"]


def test_period_card_local_pace_uses_full_previous_period():
    """로컬 기준값은 어제 *하루 전체*(same-span 컷오프 무시) — ADR 0018 핵심 차이.

    NOW=6/10 15:00. 어제 저녁(15:00 이후) 소비도 어제 전체에 포함돼 same-span(아침만)과
    값이 갈린다: same-span이면 어제=3(아침만)→up이지만, 전체면 어제=10→down.
    """
    from tokenomy.web.views import period_card_context
    conn = connect(":memory:")
    _msg(conn, "2026-06-10T01:00:00Z", 5.0)   # KST 6/10 10:00 (오늘)
    _msg(conn, "2026-06-09T01:00:00Z", 3.0)   # KST 6/09 10:00 (어제 아침)
    _msg(conn, "2026-06-09T13:00:00Z", 7.0)   # KST 6/09 22:00 (어제 저녁 — same-span 컷오프 밖)
    p = _periods(period_card_context(conn, {"tracked_providers": ["claude"],
                                            "account_mode": "subscription"}, NOW))
    assert p["오늘"]["usd"] == 5.0
    assert p["오늘"]["prev_usd"] == 10.0          # 어제 전체 = 3 + 7 (저녁 포함)
    assert p["오늘"]["pace"] == {"dir": "down", "pct": 50}   # 5 vs 10


def test_period_card_local_prev_zero_shows_baseline_no_pace():
    """이전 기간 소비 0이면 기준값은 0.0으로 표시(로컬은 미관측 개념 없음)·페이스만 None."""
    from tokenomy.web.views import period_card_context
    conn = connect(":memory:")
    _msg(conn, "2026-06-10T01:00:00Z", 5.0)   # 오늘만 — 어제/지난주/지난달 없음
    p = _periods(period_card_context(conn, {"tracked_providers": ["claude"],
                                            "account_mode": "subscription"}, NOW))
    assert p["오늘"]["prev_usd"] == 0.0          # 어제 0 — 기준값은 표시(꼬리 안 생략)
    assert p["오늘"]["pace"] is None             # 0 대비 비율 불가 → 페이스만 숨김


def test_period_card_local_no_data_nudge():
    """로컬·메시지 없음 → has_data False(카드는 '데이터 없음' 너지), share 없음."""
    from tokenomy.web.views import period_card_context
    conn = connect(":memory:")
    ctx = period_card_context(conn, {"tracked_providers": ["claude"]}, NOW)
    assert ctx["mode"] == "local"
    assert ctx["has_data"] is False
    assert ctx["share_text"] is None


def test_period_card_none_when_no_active():
    """활성 AI 0개면 카드 없음(None) — 섹션이 '표시할 AI 없음'으로 안내."""
    from tokenomy.web.views import period_card_context
    ctx = period_card_context(connect(":memory:"), {"tracked_providers": []}, NOW)
    assert ctx is None


def test_period_card_official_mode_month_and_share():
    """엔터: 이번달=공식 통합 풀 라이브 used, share는 공식(한도% 포함)."""
    from tokenomy.web.views import period_card_context
    conn = connect(":memory:")
    _snap(conn, "claude", datetime(2026, 6, 10, 12, 0, tzinfo=KST), 40.0)
    ctx = period_card_context(conn, {"tracked_providers": ["claude"],
                                     "account_mode": "enterprise"}, NOW)
    assert ctx["mode"] == "official"
    p = _periods(ctx)
    assert p["이번달"]["usd"] == 40.0   # 공식 pool_used
    # 스냅샷 하나뿐 → 이전 전체 기간 경계 미관측 → 기준값 None(꼬리·페이스 숨김).
    assert p["오늘"]["prev_usd"] is None and p["오늘"]["pace"] is None
    assert p["오늘"]["prev_label"] == "어제"
    assert "한도" in ctx["share_text"]
    assert "공식" in ctx["source_label"]


def test_period_card_official_month_excludes_pre_month_credit():
    """엔터 이번달 = 흐름(주기형 라이브 + 만료형 월초 델타), 크레딧 지난달 누적 제외 — ADR 0024."""
    from tokenomy.web.views import period_card_context
    conn = connect(":memory:")
    _snap_credit(conn, datetime(2026, 6, 1, 0, 0, tzinfo=KST), 10.0, 900.0)
    _snap_credit(conn, datetime(2026, 6, 10, 12, 0, tzinfo=KST), 10.0, 950.0)
    p = _periods(period_card_context(conn, _ENT_CREDIT_CFG, NOW))
    assert p["이번달"]["usd"] == 60.0 and p["이번달"]["state"] == "complete"


def test_period_card_official_month_partial_when_no_baseline():
    """월초 경계 스냅샷 없으면 만료형 몫 미집계 → 이번달=주기형만 + state=partial((B) 분업)."""
    from tokenomy.web.views import period_card_context
    conn = connect(":memory:")
    _snap_credit(conn, datetime(2026, 6, 10, 12, 0, tzinfo=KST), 10.0, 950.0)  # 6/1 baseline 없음
    p = _periods(period_card_context(conn, _ENT_CREDIT_CFG, NOW))
    assert p["이번달"]["usd"] == 10.0 and p["이번달"]["state"] == "partial"


def test_share_context_month_is_flow_util_is_cumulative():
    """공유 문구: 이번달=흐름(만료형 월초 델타), 한도%=위치(라이브 누적) — ADR 0024.

    크레딧 지난달 누적 $900, 이번달 델타 50. 이번달=월간라이브10+50=60(≠누적960).
    한도%=누적 960/2000=48%(위치라 만료 임박을 계속 알린다).
    """
    from tokenomy.web.views import share_context
    conn = connect(":memory:")
    _snap_credit(conn, datetime(2026, 6, 1, 0, 0, tzinfo=KST), 10.0, 900.0)
    _snap_credit(conn, datetime(2026, 6, 10, 12, 0, tzinfo=KST), 10.0, 950.0)
    ctx = share_context(conn, _ENT_CREDIT_CFG, NOW)
    assert ctx["pool"].month_usd == 60.0        # 흐름(주기형 라이브 + 만료형 델타)
    assert "한도 48%" in ctx["text"]            # 위치(누적) 96/200... 960/2000=48%


def test_period_card_official_today_pace():
    """엔터: 오늘 페이스 = 오늘 글랜스 vs 어제 *하루 전체*(공식 스냅샷 델타 합, ADR 0018).

    어제 전체[6/9 00:00~6/10 00:00] 소비 = (16-10)+(20-16) = 10(저녁·야간 델타 포함).
    same-span(어제 15:00까지=6)이 아니라 하루 전체와 견준다.
    """
    from tokenomy.web.views import period_card_context
    conn = connect(":memory:")
    _snap(conn, "claude", datetime(2026, 6, 9, 0, 0, tzinfo=KST), 10.0)
    _snap(conn, "claude", datetime(2026, 6, 9, 15, 0, tzinfo=KST), 16.0)   # 어제 15:00
    _snap(conn, "claude", datetime(2026, 6, 10, 0, 0, tzinfo=KST), 20.0)
    _snap(conn, "claude", datetime(2026, 6, 10, 12, 0, tzinfo=KST), 25.0)  # 오늘
    ctx = period_card_context(conn, {"tracked_providers": ["claude"],
                                     "account_mode": "enterprise"}, NOW)
    p = _periods(ctx)
    assert p["오늘"]["usd"] == 9.0                          # (20-16)+(25-20)
    assert p["오늘"]["prev_usd"] == 10.0                    # 어제 하루 전체
    assert p["오늘"]["pace"] == {"dir": "down", "pct": 10}  # 9 vs 10
