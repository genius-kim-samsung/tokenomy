"""시간·달력 어휘 leaf(clock.py) 테스트 — KST 경계·ts 파싱·영업일 산술·기간 경계."""
from __future__ import annotations

from datetime import date, datetime

from tokenomy.clock import (
    KST, add_business_days, business_days_between, month_bounds, parse_ts, period_bounds,
)

# June 2026 has 30 days
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=KST)  # day 10 of 30


def test_month_bounds_june():
    start, nxt = month_bounds(NOW)
    assert start.month == 6 and start.day == 1
    assert nxt.month == 7
    assert (nxt - start).days == 30


def test_parse_ts_utc_to_kst():
    dt = parse_ts("2026-06-05T00:00:00Z")
    assert dt.tzinfo == KST
    assert dt.hour == 9  # +9


# ─── period_bounds: 일/주/월 경계 + 라벨 ──────────────────────────────────────

_ANCHOR_SAT = datetime(2026, 6, 13, 15, 0, tzinfo=KST)  # 토요일 15:00 KST


def test_period_bounds_day():
    start, nxt, label = period_bounds("day", _ANCHOR_SAT)
    assert start == datetime(2026, 6, 13, 0, 0, tzinfo=KST)
    assert nxt == datetime(2026, 6, 14, 0, 0, tzinfo=KST)
    assert label == "2026-06-13 (토)"


def test_period_bounds_week_starts_monday():
    start, nxt, label = period_bounds("week", _ANCHOR_SAT)
    assert start == datetime(2026, 6, 8, 0, 0, tzinfo=KST)   # 월요일
    assert nxt == datetime(2026, 6, 15, 0, 0, tzinfo=KST)
    assert label == "2026-06-08 ~ 06-14"


def test_period_bounds_month():
    start, nxt, label = period_bounds("month", _ANCHOR_SAT)
    assert start == datetime(2026, 6, 1, 0, 0, tzinfo=KST)
    assert nxt == datetime(2026, 7, 1, 0, 0, tzinfo=KST)
    assert label == "2026-06"


def test_period_bounds_month_year_rollover():
    start, nxt, label = period_bounds("month", datetime(2026, 12, 20, tzinfo=KST))
    assert start == datetime(2026, 12, 1, 0, 0, tzinfo=KST)
    assert nxt == datetime(2027, 1, 1, 0, 0, tzinfo=KST)
    assert label == "2026-12"


def test_period_bounds_week_crosses_month():
    # 2026-07-01(수)가 속한 주 → 월요일 2026-06-29 시작
    start, nxt, label = period_bounds("week", datetime(2026, 7, 1, tzinfo=KST))
    assert start == datetime(2026, 6, 29, 0, 0, tzinfo=KST)
    assert nxt == datetime(2026, 7, 6, 0, 0, tzinfo=KST)
    assert label == "2026-06-29 ~ 07-05"


def test_period_bounds_week_year_rollover():
    # 2026-12-31(목)이 속한 주 → 월요일 2026-12-28, end 2027-01-03(다른 연도)
    start, nxt, label = period_bounds("week", datetime(2026, 12, 31, tzinfo=KST))
    assert start == datetime(2026, 12, 28, 0, 0, tzinfo=KST)
    assert nxt == datetime(2027, 1, 4, 0, 0, tzinfo=KST)
    assert label == "2026-12-28 ~ 2027-01-03"


# --- 영업일 헬퍼 business_days_between (주말 제외, 반열린 구간 [start, end)) -----


def test_business_days_between_full_week():
    # 6/15(월) ~ 6/22(월) [start, end) → 월~금 5영업일(다음 월요일 제외)
    assert business_days_between(date(2026, 6, 15), date(2026, 6, 22)) == 5


def test_business_days_between_single_weekday():
    # 6/15(월) ~ 6/16(화) → 월요일 1영업일
    assert business_days_between(date(2026, 6, 15), date(2026, 6, 16)) == 1


def test_business_days_between_skips_weekend():
    # 6/19(금) ~ 6/22(월) → 금요일만 1(토·일 제외)
    assert business_days_between(date(2026, 6, 19), date(2026, 6, 22)) == 1


def test_business_days_between_same_day_zero():
    assert business_days_between(date(2026, 6, 15), date(2026, 6, 15)) == 0


def test_business_days_between_weekend_only_zero():
    # 6/20(토) ~ 6/22(월) → 토·일만, 0영업일
    assert business_days_between(date(2026, 6, 20), date(2026, 6, 22)) == 0


def test_business_days_between_negative_range_zero():
    # end < start → 음수 누적 없이 0
    assert business_days_between(date(2026, 6, 22), date(2026, 6, 15)) == 0


def test_add_business_days_within_week():
    # 6/15(월) + 3영업일 = 화·수·목 → 6/18(목)
    assert add_business_days(date(2026, 6, 15), 3) == date(2026, 6, 18)


def test_add_business_days_skips_weekend():
    # 6/19(금) + 1영업일 = 토·일 건너뛴 6/22(월)
    assert add_business_days(date(2026, 6, 19), 1) == date(2026, 6, 22)


def test_add_business_days_zero_returns_same():
    assert add_business_days(date(2026, 6, 15), 0) == date(2026, 6, 15)
