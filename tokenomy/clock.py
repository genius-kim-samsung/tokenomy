"""시간·달력 어휘 저층 leaf(의존성 0) — KST 경계·영업일 산술·ts 파싱.

aggregate(로컬)·official_aggregate(공식)가 모두 아래로 import한다(domain.py와 같은 층).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST)


def month_bounds(now_kst: datetime) -> tuple[datetime, datetime]:
    start = now_kst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        nxt = start.replace(year=start.year + 1, month=1)
    else:
        nxt = start.replace(month=start.month + 1)
    return start, nxt


def business_days_between(start: date, end: date) -> int:
    """반열린 구간 [start, end)의 영업일(주말 제외) 수.

    주말 = 토(weekday 5)·일(6). end ≤ start면 0(음수 누적 없음).
    사내 대다수가 주말 근무하지 않으므로 D-day 추세는 영업일로 센다.
    공휴일/연차 제외는 후속(TODOS).
    """
    if end <= start:
        return 0
    n = 0
    d = start
    while d < end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def add_business_days(start: date, n: int) -> date:
    """start 다음날부터 영업일을 세어 n번째 영업일의 날짜. n=0이면 start 그대로.

    소진 예측일 산출용 — '오늘 이후 n 영업일 더 버틴다'의 도착 날짜.
    """
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def period_bounds(period: str, anchor_kst: datetime) -> tuple[datetime, datetime, str]:
    """기간 [start, nxt) 경계와 표시 라벨. period ∈ {day, week, month}.

    anchor가 속한 일/주/월을 KST 기준으로 반환. 주는 월요일 시작.
    화이트리스트 밖 period는 월간으로 폴백(라우트에서도 검증하지만 이중 안전).
    """
    a = anchor_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        nxt = a + timedelta(days=1)
        return a, nxt, f"{a.strftime('%Y-%m-%d')} ({'월화수목금토일'[a.weekday()]})"
    if period == "week":
        start = a - timedelta(days=a.weekday())   # 월요일(weekday: 월=0)
        nxt = start + timedelta(days=7)
        end = nxt - timedelta(days=1)
        end_fmt = "%Y-%m-%d" if end.year != start.year else "%m-%d"
        return start, nxt, f"{start.strftime('%Y-%m-%d')} ~ {end.strftime(end_fmt)}"
    start, nxt = month_bounds(a)                   # month (기본/폴백)
    return start, nxt, start.strftime("%Y-%m")


def _trailing_window_bounds(now_kst: datetime, weeks: int) -> tuple[datetime, datetime]:
    """오늘 포함 최근 weeks×7일 창 [start, nxt) — KST 자정 경계.

    start_date = today − (weeks×7 − 1)이라 창은 정확히 weeks×7 달력일(=영업일 5×weeks).
    정수 주(週)로 잡아 평일/주말 구성비 왜곡을 없앤다(ADR 0004 후속: 소비속도=트레일링 창).
    """
    days = max(int(weeks), 1) * 7
    today0 = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    return today0 - timedelta(days=days - 1), today0 + timedelta(days=1)
