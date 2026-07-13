"""사용량 공유 문구(usage share snapshot) — 클립보드 복사 텍스트 조립.

CONTEXT.md '사용량 공유 문구': 공식·계정 전체 한정. AI별 오늘/이번주/이번달 + 한도% + 풀 합계.
partial(△)은 카드에서 보내는 사람에게만 경고하고 복사 문구 자체는 깨끗(△·주석 없음).
"""
from __future__ import annotations

from datetime import datetime

from tokenomy.clock import KST
from tokenomy.official_aggregate import PeriodSpend
from tokenomy.db import connect, insert_official_buckets
from tokenomy.official_parser import OfficialBucket
from tokenomy.web.views import (
    PoolGlance, ShareRow, UtilShareRow, build_share_text, pool_glance, share_context,
)

NOW6 = datetime(2026, 6, 10, 12, 0, tzinfo=KST)   # 6/10(수) — 차트/글랜스 픽스처와 정렬


def _ps(usd, state="complete"):
    return PeriodSpend(usd=usd, state=state)


def _seed_day(conn, provider, day, used, *, limit=100.0, kind="monthly_limit",
              unit="usd", raw="spend"):
    """USD 월간 버킷 1개를 2026-06-<day> 12:00 KST 스냅샷으로 적재."""
    dt = datetime(2026, 6, day, 12, 0, tzinfo=KST)
    insert_official_buckets(
        conn, provider=provider, fetched_at=dt.isoformat(), created_at=dt.isoformat(),
        buckets=[OfficialBucket(
            bucket_key="monthly", raw_key=raw, bucket_kind=kind, label="월 사용 한도",
            native_unit=unit, used_native=used, limit_native=limit, remaining_native=limit - used,
            used_usd=used, limit_usd=limit, remaining_usd=limit - used,
            utilization=used / limit * 100, resets_at=None)])


def test_share_text_two_providers_complete():
    """정상: AI별 줄(오늘·이번주·이번달·한도%) + 합계 줄(한도% 없음)."""
    rows = [
        ShareRow(label="Claude", today=_ps(3.10), week=_ps(14.00), month_usd=112.00, util_pct=28),
        ShareRow(label="Codex", today=_ps(1.10), week=_ps(4.50), month_usd=48.00, util_pct=12),
    ]
    text = build_share_text(rows, "2026-06-24")
    assert text == (
        "AI 사용량 (2026-06-24, KST)\n"
        "· Claude 오늘 $3.1 · 이번주 $14.0 · 이번달 $112.0 (한도 28%)\n"
        "· Codex 오늘 $1.1 · 이번주 $4.5 · 이번달 $48.0 (한도 12%)\n"
        "합계 오늘 $4.2 · 이번주 $18.5 · 이번달 $160.0"
    )


def test_share_text_partial_today_has_no_marker():
    """partial(△)이어도 복사 문구엔 △·주석 없이 숫자만(송신자 경고는 카드 몫)."""
    rows = [ShareRow(label="Claude", today=_ps(3.10, "partial"), week=_ps(14.00, "partial"),
                     month_usd=112.00, util_pct=28)]
    text = build_share_text(rows, "2026-06-24")
    assert "△" not in text
    assert "오늘 $3.1" in text and "이번주 $14.0" in text


def test_share_text_today_none_shows_data_missing():
    """오늘 none이면 그 칸은 '오늘 데이터 없음', 이번주/이번달은 그대로 공유."""
    rows = [ShareRow(label="Claude", today=_ps(None, "none"), week=_ps(14.00),
                     month_usd=112.00, util_pct=28)]
    text = build_share_text(rows, "2026-06-24")
    assert "· Claude 오늘 데이터 없음 · 이번주 $14.0 · 이번달 $112.0 (한도 28%)" in text


def test_share_text_pool_sums_only_covered_today():
    """한 provider의 오늘이 none이면 합계 오늘은 나머지 covered만 합산(총량 보존)."""
    rows = [
        ShareRow(label="Claude", today=_ps(None, "none"), week=_ps(14.00), month_usd=112.00, util_pct=28),
        ShareRow(label="Codex", today=_ps(1.10), week=_ps(4.50), month_usd=48.00, util_pct=12),
    ]
    text = build_share_text(rows, "2026-06-24")
    assert "합계 오늘 $1.1 · 이번주 $18.5 · 이번달 $160.0" in text


def test_pool_glance_partial_infects():
    """한 provider만 partial이어도 풀 today는 partial(전염) — 카드 △ 경고 근거."""
    rows = [
        ShareRow(label="Claude", today=_ps(3.10, "complete"), week=_ps(14.00), month_usd=112.0, util_pct=28),
        ShareRow(label="Codex", today=_ps(1.10, "partial"), week=_ps(4.50), month_usd=48.0, util_pct=12),
    ]
    pool = pool_glance(rows)
    assert pool.today.state == "partial"
    assert pool.today.usd == 4.20


def test_pool_glance_all_none_is_none():
    """모든 provider의 today가 none이면 풀 today도 none(usd=None)."""
    rows = [
        ShareRow(label="Claude", today=_ps(None, "none"), week=_ps(14.00), month_usd=112.0, util_pct=28),
        ShareRow(label="Codex", today=_ps(None, "none"), week=_ps(4.50), month_usd=48.0, util_pct=12),
    ]
    pool = pool_glance(rows)
    assert pool.today.state == "none" and pool.today.usd is None
    assert pool.month_usd == 160.0   # 이번달은 라이브라 살아있음


def test_share_context_builds_from_official_pool():
    """DB 공식 풀에서 ShareRow 조립 → 문구·풀 글랜스. 이번달=월간 버킷 used, 한도%=util."""
    conn = connect(":memory:")
    _seed_day(conn, "claude", 9, 20.0)    # 어제 baseline
    _seed_day(conn, "claude", 10, 30.0)   # 오늘(6/10): 오늘=30-20, 이번달=30, 한도%=30
    ctx = share_context(conn, {"tracked_providers": ["claude"]}, NOW6)
    assert ctx is not None
    assert "AI 사용량 (2026-06-10, KST)" in ctx["text"]
    assert "· Claude 오늘 $10.0" in ctx["text"]
    assert "이번달 $30.0 (한도 30%)" in ctx["text"]
    assert ctx["pool"].month_usd == 30.0


def _seed_gemini_rate_window(conn, utils):
    """gemini rate_window 버킷들(USD 없음, 클래스 util%)을 NOW6 스냅샷으로 적재. utils=[(라벨, util)]."""
    insert_official_buckets(
        conn, provider="gemini", fetched_at=NOW6.isoformat(), created_at=NOW6.isoformat(),
        buckets=[OfficialBucket(
            bucket_key="rate_window", raw_key=label.lower(), bucket_kind="rate_window",
            label=label, native_unit="percent", used_native=None, limit_native=None,
            remaining_native=None, used_usd=None, limit_usd=None, remaining_usd=None,
            utilization=util, resets_at=None) for label, util in utils])


def test_share_context_gemini_util_line_from_official():
    """DB 공식 스냅샷에서 gemini 이용률 줄 조립 — util 내림차순·정수 반올림, 풀 멤버는 USD만."""
    conn = connect(":memory:")
    _seed_day(conn, "claude", 9, 20.0)
    _seed_day(conn, "claude", 10, 30.0)
    _seed_gemini_rate_window(conn, [("Flash", 3.4), ("Pro", 12.3456), ("Flash-Lite", 0.0)])
    ctx = share_context(conn, {"tracked_providers": ["claude", "gemini"]}, NOW6)
    assert ctx is not None
    assert "· Gemini 이용률 Pro 12% · Flash 3% · Flash-Lite 0%" in ctx["text"]
    assert ctx["providers"] == ["claude"]      # 페이스용 풀 멤버는 USD 풀 provider만
    assert ctx["pool"].month_usd == 30.0       # 합계에 gemini 미기여


def test_share_context_gemini_only_still_none():
    """이용률 줄만으로는 카드를 만들지 않는다 — USD 풀 rows 없으면 여전히 None(게이트 불변)."""
    conn = connect(":memory:")
    _seed_gemini_rate_window(conn, [("Pro", 12.0)])
    assert share_context(conn, {"tracked_providers": ["gemini"]}, NOW6) is None


def test_share_context_none_without_usd_pool():
    """rate-window-only(개인 구독제)는 USD 풀 없음 → None(카드·복사 모두 숨김)."""
    conn = connect(":memory:")
    insert_official_buckets(
        conn, provider="claude", fetched_at=NOW6.isoformat(), created_at=NOW6.isoformat(),
        buckets=[OfficialBucket(
            bucket_key="rate_window", raw_key="five_hour", bucket_kind="rate_window",
            label="5시간", native_unit="percent", used_native=50.0, limit_native=100.0,
            remaining_native=50.0, used_usd=None, limit_usd=None, remaining_usd=None,
            utilization=50.0, resets_at=None)])
    assert share_context(conn, {"tracked_providers": ["claude"]}, NOW6) is None


def test_share_text_util_row_line_before_pool():
    """USD 풀 없는 provider(gemini)는 이용률 전용 줄 — AI 줄들 뒤·합계 앞, 합계엔 미기여."""
    rows = [ShareRow(label="Claude", today=_ps(3.10), week=_ps(14.00), month_usd=112.00, util_pct=28)]
    util_rows = [UtilShareRow(label="Gemini", utils=[("Pro", 12), ("Flash", 3)])]
    text = build_share_text(rows, "2026-06-24", util_rows=util_rows)
    assert text == (
        "AI 사용량 (2026-06-24, KST)\n"
        "· Claude 오늘 $3.1 · 이번주 $14.0 · 이번달 $112.0 (한도 28%)\n"
        "· Gemini 이용률 Pro 12% · Flash 3%\n"
        "합계 오늘 $3.1 · 이번주 $14.0 · 이번달 $112.0"
    )
