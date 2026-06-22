"""집계 — 번다운, 프로젝트별 비용, 효율 신호.

월 경계는 KST 기준. transcript ts는 UTC(ISO8601)라 KST로 변환해 버킷팅한다.
PoC는 메시지를 Python에서 필터(데이터 규모 작음); 규모가 커지면 SQL 집계로 이전.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from tokenomy.pricing import find_rate, _is_version_boundary

KST = timezone(timedelta(hours=9))

# 합산/탭바가 도는 provider 목록. 3번째 AI 추가 시 여기 + 파서 + 단가만 보강.
PROVIDERS = ("claude", "codex")

# 효율 코치 휴리스틱 임계값 — 실데이터 캘리브레이션 전 튜닝값(단정 금지, 신호로만 사용)
INSIGHT_CACHE_READ_MIN = 0.30   # 월 cache_read 비율이 이 미만이면 경고
INSIGHT_WEB_SEARCH_MAX = 50     # 월 web_search 합이 이 초과면 정보 카드

# 워크트리 cwd를 부모 프로젝트로 접는 패턴.
# `<repo>/.claude/worktrees/<branch>[/...]`의 마커부터 끝까지 제거 → `<repo>`.
# slash/backslash 모두 매칭 → Claude(역슬래시)·Codex(슬래시) cwd에 공통 적용.
_WORKTREE_RE = re.compile(r"[/\\]\.claude[/\\]worktrees[/\\].*$", re.IGNORECASE)


def normalize_project(project: str | None) -> str | None:
    """워크트리 작업 디렉토리를 부모 프로젝트 경로로 정규화한다.

    격리 워크트리는 `<repo>/.claude/worktrees/<branch>`에 만들어진다. 그 cwd를
    그대로 두면 브랜치명이 독립 프로젝트처럼 잡혀 비용이 부모와 분리된다. 마커
    `.claude/worktrees/` 이후를 전부 잘라 부모 repo로 합산한다(하위 디렉토리 포함).
    provider 무관(파서가 cwd를 동일 컬럼에 적재) · 패턴이 없으면 원본 그대로.
    """
    if not project:
        return project
    return _WORKTREE_RE.sub("", project, count=1) or project


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


def _midnight(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


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


def week_count(effective_start: datetime, now_kst: datetime) -> int:
    """effective_start가 속한 주(1주차)부터 now가 속한 주까지의 주 수(월요일 경계).

    Codex 주간 한도 충전 횟수 N. 각 주 시작(월요일)마다 +1, effective_start의 주를 1로 센다.
    """
    eff_mon = _midnight(effective_start) - timedelta(days=effective_start.weekday())
    now_mon = _midnight(now_kst) - timedelta(days=now_kst.weekday())
    return (now_mon - eff_mon).days // 7 + 1


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


@dataclass
class ProjectRow:
    project: str | None
    cost: float
    sessions: int
    cache_ratio: float


def _provider_where(provider: str | None, providers: list[str] | None) -> tuple[str, list]:
    """provider 필터 (where_sql, params) 생성. 단일 provider 우선, 다음 활성 집합 providers.

    - provider 지정(not None) → `WHERE provider=?`(기존 단일 동작).
    - provider=None, providers 지정 → `WHERE provider IN (...)`(활성 합산).
      단 빈 집합(providers=[])은 `WHERE 0`(빈 결과) — 활성 0개가 전체로 새지 않게 한다.
    - 둘 다 None → 필터 없음(DB 전체, 하위호환).
    where_sql은 앞에 공백을 포함해 SELECT 뒤에 그대로 이어붙일 수 있다.
    """
    if provider is not None:
        return " WHERE provider=?", [provider]
    if providers is not None:
        if not providers:
            return " WHERE 0", []
        qs = ",".join("?" * len(providers))
        return f" WHERE provider IN ({qs})", list(providers)
    return "", []


def _range_rows(conn, provider: str | None, start: datetime, nxt: datetime,
                *, providers: list[str] | None = None) -> list:
    cols = ("SELECT ts, cost_usd, priced, session_id, project, "
            "input_tokens, cache_creation, cache_read, web_search FROM messages")
    where, params = _provider_where(provider, providers)
    rows = conn.execute(cols + where, params).fetchall()
    out = []
    for r in rows:
        dt = parse_ts(r["ts"])
        if dt and start <= dt < nxt:
            d = dict(r)
            d["project"] = normalize_project(d["project"])  # 워크트리 → 부모 repo로 합산
            out.append(d)
    return out


def _month_rows(conn, provider: str | None, now_kst: datetime,
                *, providers: list[str] | None = None) -> list:
    start, nxt = month_bounds(now_kst)
    return _range_rows(conn, provider, start, nxt, providers=providers)


def month_spend(conn, provider: str | None, now_kst: datetime,
                *, providers: list[str] | None = None) -> float:
    """provider(또는 None=전체/providers=활성 합산)의 이번 달(KST) cost_usd 합. 번다운 없이 총지출만."""
    return round(sum((r["cost_usd"] or 0) for r in _month_rows(conn, provider, now_kst, providers=providers)), 4)


def codex_weekly_window(conn) -> tuple[datetime, datetime] | None:
    """사용 이력 기준 가장 최근 7일 윈도우 [start, end).

    메시지 ts(KST)를 오름차순 순회하며, 현재 윈도우(start)에서 7일 이상 벗어난 첫
    메시지마다 그 시점으로 재앵커한다. 연속 사용이면 7일마다 타일링되고, 7일+ 유휴면
    다음 사용일이 새 앵커가 된다(유휴 기간은 윈도우를 소비하지 않음). end = start + 7일.
    Codex 사용이 전혀 없으면 None. 공식 누적 스냅샷은 cadence가 희소해 앵커 관측에
    부적합하므로 로컬 메시지 ts를 1차 근거로 쓴다.
    """
    rows = conn.execute(
        "SELECT ts FROM messages WHERE provider='codex' ORDER BY ts ASC"
    ).fetchall()
    ws: datetime | None = None
    for r in rows:
        dt = parse_ts(r["ts"])
        if dt is None:
            continue
        if ws is None or dt >= ws + timedelta(days=7):
            ws = dt
    if ws is None:
        return None
    return ws, ws + timedelta(days=7)


# 공식 미러 패널의 버킷 표시 순서(공식 앱 미러).
_BUCKET_ORDER = {"monthly_limit": 0, "codex_monthly": 0, "event_credit": 1, "promo": 2, "rate_window": 3}


@dataclass
class OfficialLens:
    """예측 렌즈 — 활성 버킷의 소비 속도/소진예상/리셋 D-day."""
    bucket_key: str
    daily_rate_usd: float | None    # USD/영업일. 유효 차분 1개 미만이면 None
    exhaust_date: date | None
    days_left_to_reset: int | None  # 현재 주기 리셋까지 영업일
    dday_warning: bool


@dataclass
class OfficialView:
    """공식 미러 패널 1개(provider별) — 버킷 + 주기 USD + 예측 렌즈 + 상태."""
    provider: str
    buckets: list[dict]                 # 표시용(버킷 행 dict, 표시 순서)
    active_key: str | None
    lens: OfficialLens | None
    period_used_usd: float | None       # 월간(공식). 카드 게이지용. 없으면 None
    period_limit_usd: float | None
    pool_used_usd: float | None         # 통합 전망 풀 기여 = 월간+포함크레딧 등 USD 한도 버킷 used 합(ADR 0004)
    pool_limit_usd: float | None        # 동 limit 합. USD 한도 버킷이 없으면 None
    weekly_used_usd: float | None       # Codex 주간(로컬 추정). Claude=None
    weekly_limit_usd: float | None
    weekly_estimated: bool
    weekly_window_end: date | None
    fetched_at: str | None
    stale_minutes: int | None
    status: str                         # "ok" | "no_data" | fetch_state.last_status
    note: str | None


def _row_to_bucket_dict(r) -> dict:
    """official_buckets 행 → 표시용 dict(resets_at은 ISO 문자열)."""
    d = dict(r)
    return {
        "bucket_key": d["bucket_key"], "raw_key": d["raw_key"], "bucket_kind": d["bucket_kind"],
        "label": d["label"], "native_unit": d["native_unit"],
        "used_native": d["used_native"], "limit_native": d["limit_native"],
        "remaining_native": d["remaining_native"],
        "used_usd": d["used_usd"], "limit_usd": d["limit_usd"], "remaining_usd": d["remaining_usd"],
        "utilization": d["utilization"], "resets_at": d["resets_at"],
    }


def _lens_from_series(conn, provider: str, bucket_key: str, now_kst: datetime,
                      limit_usd: float | None, used_usd: float | None,
                      reset_date: date | None, weeks: int = 2) -> OfficialLens | None:
    """로컬 일일 소비속도(local_daily_rate)로 소진예상·리셋 D-day 산출.

    rate = 트레일링 창(오늘 포함 weeks×7일) 로컬 소비 ÷ 그 창의 영업일. 소비가 없으면 daily_rate=None.
    bucket_key는 반환값 식별용으로만 사용(rate 계산에 미사용).
    """
    rate = local_daily_rate(conn, provider, now_kst, weeks)

    exhaust_date: date | None = None
    if rate and limit_usd and used_usd is not None and limit_usd > used_usd:
        need = math.ceil((limit_usd - used_usd) / rate)
        exhaust_date = add_business_days(now_kst.date(), need)

    days_left = business_days_between(now_kst.date(), reset_date) if reset_date else None
    dday = bool((exhaust_date is not None and reset_date is not None and exhaust_date < reset_date)
                or (limit_usd and used_usd is not None and used_usd / limit_usd >= 0.80))
    return OfficialLens(bucket_key=bucket_key, daily_rate_usd=rate, exhaust_date=exhaust_date,
                        days_left_to_reset=days_left, dday_warning=dday)


def official_view(conn, provider: str, now_kst: datetime,
                  credit_to_usd: float, weeks: int = 2) -> OfficialView:
    """공식 미러 패널 컨텍스트. 최신 스냅샷(공식) + 로컬 주간 윈도우(Codex)를 합친다.

    - period_used/limit = 월간 버킷(공식 ground truth). 없으면 None.
    - Codex weekly_used = 로컬 CLI 첫-사용 7일 윈도우 합(추정), weekly_limit = 공식 월÷4(있을 때만). 예산 폴백 없음.
      유휴 상태(마지막 사용 7일+ 경과로 윈도우가 닫힌 경우) weekly_used=0, weekly_window_end=None.
    - Claude 월 버킷 resets_at None은 다음 달 경계(KST)로 채운다.
    - 활성 버킷 선정(1차): 후보(monthly_limit/event_credit/codex_monthly 중 stale 제외)의
      series 최근 두 스냅샷 used 양의 차분이 가장 큰 버킷 — 동률은 tie-break(event<monthly).
      (2차) 양의 차분이 없으면 remaining>0 첫 버킷; 없으면 첫 후보. promo/rate_window는 항상 제외.
    - 예측 렌즈(lens)는 로컬 rate 기반으로 provider 공통 제공. 로컬 소비가 없으면 daily_rate=None.
    """
    from tokenomy.db import latest_official_snapshot, get_fetch_state, official_bucket_series

    rows = latest_official_snapshot(conn, provider)
    fetched_at = rows[0]["fetched_at"] if rows else None
    _, next_month = month_bounds(now_kst)

    buckets = [_row_to_bucket_dict(r) for r in rows]
    # Claude 월 버킷 resets_at 보강(다음 달 경계)
    for b in buckets:
        if b["bucket_kind"] in ("monthly_limit", "codex_monthly") and not b["resets_at"]:
            b["resets_at"] = next_month.isoformat()
    buckets.sort(key=lambda b: _BUCKET_ORDER.get(b["bucket_kind"], 9))

    monthly = next((b for b in buckets if b["bucket_kind"] in ("monthly_limit", "codex_monthly")), None)
    period_used = monthly["used_usd"] if monthly else None
    period_limit = monthly["limit_usd"] if monthly else None

    # staleness(분)
    stale_minutes = None
    if fetched_at:
        dt = parse_ts(fetched_at)
        if dt is not None:
            stale_minutes = max(0, int((now_kst - dt).total_seconds() // 60))

    # 상태 (Fix 2: last_status가 DB-null인 경우 "no_data"로 보정)
    if rows:
        status = "ok"
    else:
        st = get_fetch_state(conn, provider)
        status = (st["last_status"] if st else None) or "no_data"

    # Codex 주간(로컬 추정)
    weekly_used = weekly_limit = None
    weekly_estimated = False
    weekly_end: date | None = None
    if provider == "codex":
        win = codex_weekly_window(conn)
        if win is not None:
            ws, we = win
            if now_kst >= we:
                # 윈도우 닫힘(유휴): 마지막 사용 7일+ 경과 → 현재 주간 데이터 없음
                weekly_used = 0.0
                weekly_estimated = True
                weekly_end = None
            else:
                # 현재 윈도우 활성
                wrows = _range_rows(conn, "codex", ws, we)
                weekly_used = round(sum((r["cost_usd"] or 0) for r in wrows), 4)
                weekly_estimated = True
                weekly_end = we.date()
        # 주간 한도 = 공식 월 한도 ÷ 4(있을 때만). 예산 폴백 없음.
        if period_limit:
            weekly_limit = round(period_limit / 4, 4)

    # 활성 버킷 + 렌즈
    # stale 제외: resets_at이 설정됐고 이미 과거면 후보에서 뺀다
    active_key = None
    lens = None
    tie_order = {"event_credit": 0, "monthly_limit": 1, "codex_monthly": 1}
    candidates = [
        b for b in buckets
        if b["bucket_kind"] in ("monthly_limit", "event_credit", "codex_monthly")
        and not (b["resets_at"] and parse_ts(b["resets_at"]) is not None
                 and parse_ts(b["resets_at"]) < now_kst)
    ]
    # 통합 전망 풀 기여 = candidates(stale 제외 USD 한도 버킷) 중 limit_usd 있는 것의 합.
    # 카드 게이지(period_*)는 월간만 보지만, 전망은 포함 크레딧 등 실제 닳는 버킷까지 합산(ADR 0004).
    pool_cands = [b for b in candidates if b["limit_usd"] is not None]
    pool_used = round(sum(b["used_usd"] or 0.0 for b in pool_cands), 4) if pool_cands else None
    pool_limit = round(sum(b["limit_usd"] for b in pool_cands), 4) if pool_cands else None
    if candidates:
        # 1차: series 최근 두 스냅샷의 양의 used 차분이 가장 큰 버킷
        def _recent_diff(b: dict) -> float:
            series = official_bucket_series(conn, provider, b["bucket_key"])
            pts = [(r["fetched_at"], r["used_usd"]) for r in series if r["used_usd"] is not None]
            if len(pts) < 2:
                return 0.0
            diff = pts[-1][1] - pts[-2][1]
            return diff if diff > 0 else 0.0

        diffs = {b["bucket_key"]: _recent_diff(b) for b in candidates}
        max_diff = max(diffs.values())
        if max_diff > 0:
            # 최대 차분 후보 중 tie-break 순서가 가장 낮은 것
            best = min(
                (b for b in candidates if diffs[b["bucket_key"]] == max_diff),
                key=lambda b: tie_order.get(b["bucket_kind"], 9),
            )
            active = best
        else:
            # 2차: tie-break 정렬 후 remaining>0 첫 버킷, 없으면 첫 후보
            sorted_cands = sorted(candidates, key=lambda b: tie_order.get(b["bucket_kind"], 9))
            active = next((b for b in sorted_cands if (b["remaining_usd"] or 0) > 0), sorted_cands[0])

        active_key = active["bucket_key"]
        reset_date = parse_ts(active["resets_at"]).date() if active["resets_at"] else None
        # 렌즈는 로컬 rate라 provider 공통(Claude/Codex 모두 적용).
        lens = _lens_from_series(conn, provider, active_key, now_kst,
                                 active["limit_usd"], active["used_usd"], reset_date, weeks)

    note = None if rows else "공식 미취득 — 로컬 추정(USD)"
    return OfficialView(
        provider=provider, buckets=buckets, active_key=active_key, lens=lens,
        period_used_usd=period_used, period_limit_usd=period_limit,
        pool_used_usd=pool_used, pool_limit_usd=pool_limit,
        weekly_used_usd=weekly_used, weekly_limit_usd=weekly_limit,
        weekly_estimated=weekly_estimated, weekly_window_end=weekly_end,
        fetched_at=fetched_at, stale_minutes=stale_minutes, status=status, note=note,
    )


@dataclass
class CombinedForecast:
    """통합 풀 월말 전망 — 한도 있는 provider 합산 + 로컬 소비속도 직선 연장.

    위치(used/limit)는 공식 ground-truth, 기울기(daily_rate)는 로컬 JSONL 추정.
    지평은 달력 월말(KST). projected_remaining 양수=여유 / 음수=부족.
    """
    providers: list[str]
    used_usd: float
    limit_usd: float
    remaining_usd: float
    daily_rate_usd: float | None
    bdays_remaining: int
    projected_used_usd: float | None
    projected_remaining_usd: float | None
    exhaust_date: date | None
    is_exhausted: bool
    per_provider: list[dict]
    month_end: date


def _trailing_window_bounds(now_kst: datetime, weeks: int) -> tuple[datetime, datetime]:
    """오늘 포함 최근 weeks×7일 창 [start, nxt) — KST 자정 경계.

    start_date = today − (weeks×7 − 1)이라 창은 정확히 weeks×7 달력일(=영업일 5×weeks).
    정수 주(週)로 잡아 평일/주말 구성비 왜곡을 없앤다(ADR 0004 후속: 소비속도=트레일링 창).
    """
    days = max(int(weeks), 1) * 7
    today0 = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    return today0 - timedelta(days=days - 1), today0 + timedelta(days=1)


def _earliest_message_date(conn, provider: str | None, providers: list[str] | None):
    """모집단(provider/providers)의 최초 메시지 KST 날짜. 없으면 None."""
    where, params = _provider_where(provider, providers)
    dts = [parse_ts(r["ts"]) for r in conn.execute("SELECT ts FROM messages" + where, params).fetchall()]
    dts = [d for d in dts if d is not None]
    return min(dts).date() if dts else None


def _trailing_business_days(conn, now_kst: datetime, weeks: int, *,
                            provider: str | None = None,
                            providers: list[str] | None = None) -> int:
    """트레일링 창의 영업일 수(적응형 분모).

    분모 = business_days_between(max(창시작, 모집단 최초메시지일), 오늘+1). 기성 사용자
    (최초메시지 ≤ 창시작)는 풀 창(=5×weeks, 유휴 영업일 포함해 정상 희석), 신규/복귀는
    존재 이전 일수를 제외해 과소추정(거짓 "여유")을 막는다. 모집단에 메시지가 전혀 없으면 0.
    earliest-msg는 spend와 같은 provider 모집단으로 조회(분자/분모 모집단 일치).
    """
    start, _ = _trailing_window_bounds(now_kst, weeks)
    earliest = _earliest_message_date(conn, provider, providers)
    if earliest is None:
        return 0
    return business_days_between(max(start.date(), earliest), now_kst.date() + timedelta(days=1))


def trailing_window_spend(conn, provider: str | None, now_kst: datetime, weeks: int,
                          *, providers: list[str] | None = None) -> float:
    """트레일링 창(오늘 포함 weeks×7일)의 로컬 cost_usd 합. 번다운 없이 총소비."""
    start, nxt = _trailing_window_bounds(now_kst, weeks)
    return round(sum((r["cost_usd"] or 0) for r in _range_rows(conn, provider, start, nxt, providers=providers)), 4)


def local_daily_rate(conn, provider: str, now_kst: datetime, weeks: int = 2) -> float | None:
    """provider의 트레일링 창(오늘 포함 weeks×7일) 로컬 소비 ÷ 그 창의 영업일(USD/영업일).

    소비속도는 청구 리셋과 무관한 행동 속성이라 트레일링 창으로 추정한다(ADR 0004 후속:
    월초 누적 → 트레일링). 분모는 적응형(_trailing_business_days). 소비/창이 없으면 None.
    """
    elapsed = _trailing_business_days(conn, now_kst, weeks, provider=provider)
    spend = trailing_window_spend(conn, provider, now_kst, weeks)
    return round(spend / elapsed, 4) if (elapsed > 0 and spend > 0) else None


def combined_forecast(conn, views: list[OfficialView], now_kst: datetime,
                      weeks: int = 2) -> CombinedForecast | None:
    """공식 USD/크레딧 한도가 있는 provider를 한 풀로 합쳐 달력 월말 예상 잔여를 낸다.

    풀 = pool_limit_usd가 있는 view들. 없으면 None(히어로 숨김).
    used/limit = 공식 pool_*의 합(현재 위치 — 월간+포함크레딧 등 USD 한도 버킷 합산, ADR 0004).
    예상 used = used + daily_rate × (오늘 이후 월말까지 남은 영업일). 음수 잔여면 소진 예상일 산출.
    daily_rate = 풀 provider 트레일링 창(오늘 포함 weeks×7일) 로컬 소비 합 ÷ 그 창의 영업일(적응형).
    이미 소진(used≥limit)이거나 로컬 소비가 없으면(daily_rate None) 전망은 생략(None)한다.

    한계(ADR 0004): 포함 크레딧은 리셋 주기가 월간과 다를 수 있으나(예: 분기 만료),
    v1.x는 달력 월말 지평으로 함께 본다 — "이번 달 이 속도면 가용 예산을 다 쓰나?" 질문에 답하기 위함.
    """
    pool = [v for v in views if v.pool_limit_usd]
    if not pool:
        return None

    used = round(sum(v.pool_used_usd or 0.0 for v in pool), 4)
    limit = round(sum(v.pool_limit_usd for v in pool), 4)
    remaining = round(limit - used, 4)

    _, next_month = month_bounds(now_kst)
    month_end = (next_month - timedelta(days=1)).date()
    pool_providers = [v.provider for v in pool]
    elapsed = _trailing_business_days(conn, now_kst, weeks, providers=pool_providers)
    spend = trailing_window_spend(conn, None, now_kst, weeks, providers=pool_providers)
    daily_rate = round(spend / elapsed, 4) if (elapsed > 0 and spend > 0) else None
    bdays_remaining = business_days_between(now_kst.date() + timedelta(days=1), next_month.date())

    is_exhausted = used >= limit
    projected_used = projected_remaining = None
    exhaust_date = None
    if daily_rate is not None and not is_exhausted:
        projected_used = round(used + daily_rate * bdays_remaining, 4)
        projected_remaining = round(limit - projected_used, 4)
        if projected_used > limit and daily_rate > 0:
            need = math.ceil((limit - used) / daily_rate)
            exhaust_date = add_business_days(now_kst.date(), need)

    return CombinedForecast(
        providers=[v.provider for v in pool],
        used_usd=used, limit_usd=limit, remaining_usd=remaining,
        daily_rate_usd=daily_rate, bdays_remaining=bdays_remaining,
        projected_used_usd=projected_used, projected_remaining_usd=projected_remaining,
        exhaust_date=exhaust_date, is_exhausted=is_exhausted,
        per_provider=[{"provider": v.provider, "used_usd": v.pool_used_usd or 0.0,
                       "limit_usd": v.pool_limit_usd} for v in pool],
        month_end=month_end,
    )


def by_project(conn, provider: str | None, now_kst: datetime, limit_n: int | None = None,
               *, start: datetime | None = None, nxt: datetime | None = None,
               providers: list[str] | None = None) -> list[ProjectRow]:
    assert (start is None) == (nxt is None), "start/nxt는 함께 지정해야 한다"
    rows = (_range_rows(conn, provider, start, nxt, providers=providers) if (start and nxt)
            else _month_rows(conn, provider, now_kst, providers=providers))
    agg: dict = {}
    for r in rows:
        key = r["project"] or "(unknown)"
        a = agg.setdefault(key, {"cost": 0.0, "sessions": set(), "cr": 0, "den": 0})
        a["cost"] += r["cost_usd"] or 0
        a["sessions"].add(r["session_id"])
        a["cr"] += r["cache_read"] or 0
        a["den"] += (r["input_tokens"] or 0) + (r["cache_creation"] or 0) + (r["cache_read"] or 0)

    out = [
        ProjectRow(
            project=k, cost=round(a["cost"], 4), sessions=len(a["sessions"]),
            cache_ratio=round(a["cr"] / a["den"], 4) if a["den"] else 0.0,
        )
        for k, a in agg.items()
    ]
    out.sort(key=lambda x: x.cost, reverse=True)
    return out[:limit_n] if limit_n else out


@dataclass
class DaySessionRow:
    """한 행 = (KST 날짜 × 세션). 같은 세션이 N일 걸치면 N행."""
    date: str               # "2026-06-13" (KST)
    session_id: str
    provider: str | None
    summary: str | None     # 작업요약(aiTitle 캐시)
    project: str | None
    label: str | None       # 수동 귀속 라벨
    cost: float
    msgs: int
    cache_ratio: float
    cache_read: int         # 그룹 가중평균 분자(원시 cache_read 합)
    cache_den: int          # 그룹 가중평균 분모(input + cache_creation + cache_read)
    is_continued: bool      # 세션 최초등장일보다 이후 날짜인가 → ↩
    cache_miss: bool        # is_continued AND cache_ratio < 임계 → ⚠


@dataclass
class FolderGroup:
    """날짜 안의 폴더(프로젝트) 묶음. views.build_date_tree가 생성."""
    project: str            # 표시용 폴더명((unknown) 포함)
    cost: float
    msgs: int
    cache_ratio: float      # 가중평균 = Σcache_read / Σcache_den
    preview: str            # 접힘 시 노출할 대표 작업요약
    rows: list[DaySessionRow]   # 세션 행(비용 내림차순)


@dataclass
class DateGroup:
    """날짜 묶음(최상위). folders는 비용 내림차순."""
    date: str               # "2026-06-13" (KST)
    weekday: str            # '금'
    cost: float
    msgs: int
    cache_ratio: float      # 가중평균
    preview: str
    folders: list[FolderGroup]


@dataclass
class SessionRow:
    session_id: str
    project: str | None
    provider: str | None   # 세션 provider(sessions.provider) — combined 탭에서 AI 구분
    label: str | None      # 수동 귀속 라벨(sessions.label)
    summary: str | None    # Claude Code aiTitle 캐시(sessions.summary)
    cost: float
    first_ts: str | None
    last_ts: str | None
    msgs: int
    cache_ratio: float


def by_session(
    conn,
    provider: str | None,
    now_kst: datetime,
    limit_n: int | None = None,
    project: str | None = None,
    order: str = "cost",
    *,
    start: datetime | None = None,
    nxt: datetime | None = None,
    providers: list[str] | None = None,
) -> list[SessionRow]:
    """세션별 비용·효율 + 라벨/작업요약. start/nxt 미지정 시 이번 달 기준.

    label = 수동 귀속 라벨, summary = Claude Code aiTitle 캐시(sessions.summary).
    order="cost"(비용순) | "recent"(last_ts 최신순). project가 주어지면 그 프로젝트만.
    """
    assert (start is None) == (nxt is None), "start/nxt는 함께 지정해야 한다"
    rows = (_range_rows(conn, provider, start, nxt, providers=providers) if (start and nxt)
            else _month_rows(conn, provider, now_kst, providers=providers))
    meta = {
        r["session_id"]: (r["label"], r["summary"], r["provider"], r["user_turns"])
        for r in conn.execute(
            "SELECT session_id, label, summary, provider, user_turns FROM sessions"
        ).fetchall()
    }
    agg: dict = {}
    for r in rows:
        if project is not None and (r["project"] or "(unknown)") != project:
            continue
        sid = r["session_id"]
        a = agg.setdefault(
            sid,
            {"project": r["project"], "cost": 0.0,
             "first": r["ts"], "last": r["ts"], "cr": 0, "den": 0},
        )
        a["cost"] += r["cost_usd"] or 0
        if r["ts"] and (a["first"] is None or r["ts"] < a["first"]):
            a["first"] = r["ts"]
        if r["ts"] and (a["last"] is None or r["ts"] > a["last"]):
            a["last"] = r["ts"]
        a["cr"] += r["cache_read"] or 0
        a["den"] += (r["input_tokens"] or 0) + (r["cache_creation"] or 0) + (r["cache_read"] or 0)

    out = []
    for sid, a in agg.items():
        m = meta.get(sid, (None, None, None, None))
        out.append(SessionRow(
            session_id=sid, project=a["project"],
            provider=m[2],
            label=m[0],
            summary=m[1],
            cost=round(a["cost"], 4), first_ts=a["first"], last_ts=a["last"],
            msgs=(m[3] or 0),
            cache_ratio=round(a["cr"] / a["den"], 4) if a["den"] else 0.0,
        ))
    if order == "recent":
        out.sort(key=lambda x: x.last_ts or "", reverse=True)
    else:
        out.sort(key=lambda x: x.cost, reverse=True)
    return out[:limit_n] if limit_n else out


def by_day_session(conn, provider: str | None, *, start: datetime, nxt: datetime,
                   providers: list[str] | None = None) -> list[DaySessionRow]:
    """(KST날짜 × 세션) 단위 행. 기간 [start, nxt) 내 메시지를 날짜+세션으로 버킷팅한다.

    is_continued: 세션 최초 등장일(전체 messages의 MIN(ts))보다 이 행 날짜가 이후인가.
                  조회 범위가 아닌 전체에서 구해야 지난달 시작→이번달 이어짐을 오판하지 않는다.
    cache_miss:   is_continued AND cache_ratio < INSIGHT_CACHE_READ_MIN(첫 등장일은 절대 제외).
    """
    rows = _range_rows(conn, provider, start, nxt, providers=providers)

    # 세션별 최초 등장일(전체 기준, KST 날짜 문자열)
    first_day: dict[str, str] = {}
    # provider 필터 없음 — 세션 전체의 최초 등장일 기준이어야 월 경계 이어짐을 오판하지 않음
    for r in conn.execute("SELECT session_id, MIN(ts) m FROM messages GROUP BY session_id").fetchall():
        dt = parse_ts(r["m"])
        if dt:
            first_day[r["session_id"]] = dt.date().isoformat()

    meta = {
        r["session_id"]: (r["label"], r["summary"], r["provider"])
        for r in conn.execute("SELECT session_id, label, summary, provider FROM sessions").fetchall()
    }

    day_turns = {
        (r["session_id"], r["day"]): r["turns"]
        for r in conn.execute("SELECT session_id, day, turns FROM session_day_turns").fetchall()
    }

    agg: dict = {}
    for r in rows:
        dt = parse_ts(r["ts"])
        if not dt:
            continue
        date = dt.date().isoformat()
        key = (date, r["session_id"])
        a = agg.setdefault(
            key,
            {"project": r["project"], "cost": 0.0, "cr": 0, "den": 0},
        )
        a["cost"] += r["cost_usd"] or 0
        a["cr"] += r["cache_read"] or 0
        a["den"] += (r["input_tokens"] or 0) + (r["cache_creation"] or 0) + (r["cache_read"] or 0)

    out: list[DaySessionRow] = []
    for (date, sid), a in agg.items():
        cache_ratio = (a["cr"] / a["den"]) if a["den"] else 0.0
        is_continued = first_day.get(sid, date) < date
        cache_miss = is_continued and cache_ratio < INSIGHT_CACHE_READ_MIN
        label, summary, sprov = meta.get(sid, (None, None, None))
        # msgs = 그 날짜의 사용자 턴 수(session_day_turns). 멀티데이 세션도 날짜별 정확 카운트.
        out.append(DaySessionRow(
            date=date, session_id=sid, provider=sprov,
            summary=summary, project=a["project"], label=label,
            cost=round(a["cost"], 4), msgs=day_turns.get((sid, date), 0),
            cache_ratio=round(cache_ratio, 4),
            cache_read=a["cr"], cache_den=a["den"],
            is_continued=is_continued, cache_miss=cache_miss,
        ))
    out.sort(key=lambda x: (x.date, x.session_id), reverse=True)
    return out


# 차원 키 → messages 컬럼. 사용자 입력은 이 dict의 '키'로만 받고, SQL엔 '값'(컬럼명)만 넣는다.
DIM_COLUMNS = {"model": "model", "skill": "attribution_skill", "branch": "git_branch"}


@dataclass
class TokenComposition:
    """기간 내 토큰 4종 합계 + 비중(토큰량 기준, 0~100 퍼센트값). 비용은 담지 않는다(바에 비용 오해 방지)."""
    input_tokens: int
    output_tokens: int
    cache_creation: int
    cache_read: int
    total: int
    input_pct: float
    output_pct: float
    cache_creation_pct: float
    cache_read_pct: float


def token_composition(conn, provider: str | None, start, nxt,
                      *, providers: list[str] | None = None) -> TokenComposition:
    """기간 [start, nxt) 내 input/output/cache_creation/cache_read 합계와 비중(%)을 반환.

    _range_rows는 output_tokens를 select하지 않아 재사용하지 않고 자체 SELECT한다.
    비중은 0~100 퍼센트값(round(x/total*100,1)) — cache_ratio(0~1)와 단위가 다르다.
    """
    sql = "SELECT ts, input_tokens, output_tokens, cache_creation, cache_read FROM messages"
    where, params = _provider_where(provider, providers)
    rows = conn.execute(sql + where, params).fetchall()
    it = ot = cc = cr = 0
    for r in rows:
        dt = parse_ts(r["ts"])
        if not (dt and start <= dt < nxt):
            continue
        it += r["input_tokens"] or 0
        ot += r["output_tokens"] or 0
        cc += r["cache_creation"] or 0
        cr += r["cache_read"] or 0
    total = it + ot + cc + cr

    def pct(x: int) -> float:
        return round(x / total * 100, 1) if total else 0.0

    return TokenComposition(
        input_tokens=it, output_tokens=ot, cache_creation=cc, cache_read=cr,
        total=total, input_pct=pct(it), output_pct=pct(ot),
        cache_creation_pct=pct(cc), cache_read_pct=pct(cr),
    )


@dataclass
class DimensionRow:
    key: str | None
    cost: float
    sessions: int
    input_tokens: int
    output_tokens: int
    cache_creation: int
    cache_read: int
    cache_ratio: float


def by_dimension(conn, provider: str | None, start: datetime, nxt: datetime,
                 dim: str = "model", *, providers: list[str] | None = None) -> list[DimensionRow]:
    """기간 [start, nxt) 내 차원(dim) 단위 합계. 비용 내림차순.

    dim은 DIM_COLUMNS 화이트리스트 키. 빈 문자열/NULL 키는 None 버킷(미귀속)으로 접는다.
    """
    col = DIM_COLUMNS.get(dim, "model")
    sql = (f"SELECT ts, {col} AS key, cost_usd, session_id, input_tokens, output_tokens, "
           "cache_creation, cache_read FROM messages")
    where, params = _provider_where(provider, providers)
    rows = conn.execute(sql + where, params).fetchall()
    agg: dict = {}
    for r in rows:
        dt = parse_ts(r["ts"])
        if not (dt and start <= dt < nxt):
            continue
        key = r["key"]
        if key == "":
            key = None
        a = agg.setdefault(key, {"cost": 0.0, "sessions": set(), "it": 0, "ot": 0, "cc": 0, "cr": 0})
        a["cost"] += r["cost_usd"] or 0
        a["sessions"].add(r["session_id"])
        a["it"] += r["input_tokens"] or 0
        a["ot"] += r["output_tokens"] or 0
        a["cc"] += r["cache_creation"] or 0
        a["cr"] += r["cache_read"] or 0
    out = [
        DimensionRow(
            key=k, cost=round(a["cost"], 4), sessions=len(a["sessions"]),
            input_tokens=a["it"], output_tokens=a["ot"],
            cache_creation=a["cc"], cache_read=a["cr"],
            cache_ratio=round(a["cr"] / (a["it"] + a["cc"] + a["cr"]), 4) if (a["it"] + a["cc"] + a["cr"]) else 0.0,
        )
        for k, a in agg.items()
    ]
    out.sort(key=lambda x: x.cost, reverse=True)
    return out


@dataclass
class ModelUsageRow:
    model: str | None
    cost: float
    sessions: int
    input_tokens: int
    output_tokens: int
    cache_creation: int
    cache_read: int
    cache_ratio: float


def by_model(conn, provider: str | None, start: datetime, nxt: datetime) -> list[ModelUsageRow]:
    """기간 [start, nxt) 내 모델 단위 합계(=by_dimension(dim='model')). 비용 내림차순."""
    return [
        ModelUsageRow(
            model=r.key, cost=r.cost, sessions=r.sessions,
            input_tokens=r.input_tokens, output_tokens=r.output_tokens,
            cache_creation=r.cache_creation, cache_read=r.cache_read, cache_ratio=r.cache_ratio,
        )
        for r in by_dimension(conn, provider, start, nxt, "model")
    ]


@dataclass
class SidechainSplit:
    parent_cost: float
    sub_cost: float
    total_cost: float
    sub_share: float        # 서브에이전트 비중 % (= sub/total*100)
    parent_tokens: int
    sub_tokens: int


def sidechain_split(conn, provider: str | None, start: datetime, nxt: datetime,
                    *, providers: list[str] | None = None) -> SidechainSplit:
    """기간 [start, nxt) 내 is_sidechain 기준 부모 vs 서브에이전트 비용·토큰 분리."""
    sql = ("SELECT ts, is_sidechain, cost_usd, input_tokens, output_tokens, "
           "cache_creation, cache_read FROM messages")
    where, params = _provider_where(provider, providers)
    rows = conn.execute(sql + where, params).fetchall()
    pc = sc = 0.0
    pt = st = 0
    for r in rows:
        dt = parse_ts(r["ts"])
        if not (dt and start <= dt < nxt):
            continue
        tok = (r["input_tokens"] or 0) + (r["output_tokens"] or 0) \
            + (r["cache_creation"] or 0) + (r["cache_read"] or 0)
        if r["is_sidechain"]:
            sc += r["cost_usd"] or 0
            st += tok
        else:
            pc += r["cost_usd"] or 0
            pt += tok
    total = pc + sc
    return SidechainSplit(
        parent_cost=round(pc, 4), sub_cost=round(sc, 4), total_cost=round(total, 4),
        sub_share=round(sc / total * 100, 1) if total else 0.0,
        parent_tokens=pt, sub_tokens=st,
    )


@dataclass
class ModelRow:
    model: str | None
    cost: float
    input_tokens: int
    output_tokens: int
    cache_creation: int
    cache_read: int


@dataclass
class SessionDetail:
    session_id: str
    project: str | None
    provider: str | None
    label: str | None
    first_ts: str | None
    last_ts: str | None
    cost: float
    msgs: int
    web_search: int
    web_fetch: int
    models: list[ModelRow]


@dataclass
class Insight:
    level: str  # "info" | "warn"
    text: str


def insights(conn, now_kst: datetime, provider: str | None,
             cov: "CoverageReport | None" = None,
             *, providers: list[str] | None = None) -> list[Insight]:
    """효율 코치 카드. bd 인자 제거 — 예산 초과 카드 없음. unpriced는 rows에서 직접 계산."""
    rows = _month_rows(conn, provider, now_kst, providers=providers)
    cr = sum(r["cache_read"] or 0 for r in rows)
    den = sum((r["input_tokens"] or 0) + (r["cache_creation"] or 0) + (r["cache_read"] or 0) for r in rows)
    cache_ratio = (cr / den) if den else 1.0
    web_search = sum(r["web_search"] or 0 for r in rows)

    cards: list[Insight] = []
    if den and cache_ratio < INSIGHT_CACHE_READ_MIN:
        cards.append(Insight("warn", f"캐시 활용 {cache_ratio * 100:.0f}% — 컨텍스트 재구축 낭비 가능성"))
    if web_search > INSIGHT_WEB_SEARCH_MAX:
        cards.append(Insight("info", f"web_search {web_search}회 — 비용 영향 점검 권장"))
    # 캐시 재구축: 이어지는 세션인데 캐시를 못 읽은(cache_miss) 고유 세션 수.
    # by_day_session이 첫 등장일을 제외(is_continued)하므로 오해 없음. 달력 월 기준.
    month_start, month_nxt = month_bounds(now_kst)
    rebuild_sessions = {
        r.session_id
        for r in by_day_session(conn, provider, start=month_start, nxt=month_nxt, providers=providers)
        if r.cache_miss
    }
    if rebuild_sessions:
        cards.append(Insight(
            "info",
            f"캐시 재구축 {len(rebuild_sessions)}개 세션 — 이어지는 작업에서 컨텍스트 재빌드(세션 유지로 개선 여지)",
        ))
    if cov is not None and cov.unpriced_count:
        pct = cov.unpriced_token_share * 100
        cards.append(Insight(
            "warn",
            f"단가 미식별 {cov.unpriced_count}종(토큰 {pct:.0f}%) — 비용 누락, 설정에서 확인",
        ))
    elif cov is None:
        unpriced = sum(1 for r in rows if not r["priced"])
        if unpriced:
            cards.append(Insight("warn", f"단가 미식별 {unpriced}건 — 비용 누락 가능"))

    if not cards:
        cards.append(Insight("info", "특이 신호 없음"))
    return cards


@dataclass
class CoverageModel:
    provider: str
    model: str | None
    matched_contains: str | None   # 매칭된 pricing 항목의 contains. None이면 미식별
    status: str                    # "ok" | "suspect" | "unpriced"
    tokens: int
    token_share: float


@dataclass
class CoverageReport:
    models: list[CoverageModel]    # 토큰 내림차순
    total_tokens: int
    unpriced_count: int            # status=="unpriced" 모델 종 수(메시지 건수 아님)
    unpriced_token_share: float
    suspect_count: int
    coarse_contains: list[str]     # 2개 이상 distinct 모델이 매칭된 contains


def pricing_coverage(conn, pricing: dict, *, providers: list[str] | None = None) -> CoverageReport:
    """distinct (provider, model)별 토큰 집계 + 단가 매칭 진단(읽기 전용).

    - find_rate로 매칭, 매칭 항목의 contains 보존. rate None → unpriced.
    - 버전경계 의심(_is_version_boundary) → suspect, 그 외 ok.
    - coarse_contains: 같은 contains에 매칭된 distinct 모델이 2개 이상인 항목.
    - providers(활성 집합) 지정 시 그 provider만 진단(끈 AI는 설정 진단에도 안 나옴). 빈 집합은 빈 리포트.
    """
    where, params = _provider_where(None, providers)
    rows = conn.execute(
        "SELECT provider, model, "
        "SUM(input_tokens+output_tokens+cache_creation+cache_read) AS toks "
        "FROM messages" + where + " GROUP BY provider, model", params
    ).fetchall()
    total = sum((r["toks"] or 0) for r in rows)
    models: list[CoverageModel] = []
    contains_models: dict[str, set] = {}
    for r in rows:
        model = r["model"]
        toks = r["toks"] or 0
        rate = find_rate(model, pricing)
        if rate is None:
            matched, status = None, "unpriced"
        else:
            matched = rate.get("contains")
            status = "suspect" if _is_version_boundary(model or "", matched or "") else "ok"
            contains_models.setdefault(matched, set()).add(model)
        models.append(CoverageModel(
            provider=r["provider"], model=model, matched_contains=matched,
            status=status, tokens=toks,
            token_share=(toks / total) if total else 0.0,
        ))
    models.sort(key=lambda m: m.tokens, reverse=True)
    unpriced = [m for m in models if m.status == "unpriced"]
    return CoverageReport(
        models=models,
        total_tokens=total,
        unpriced_count=len(unpriced),
        unpriced_token_share=(sum(m.tokens for m in unpriced) / total) if total else 0.0,
        suspect_count=sum(1 for m in models if m.status == "suspect"),
        coarse_contains=sorted(c for c, ms in contains_models.items() if len(ms) >= 2),
    )


@dataclass
class DayPoint:
    day: int
    cumulative_cost: float | None   # 미래(오늘 이후) 구간은 None → 차트에서 선이 끊김


def daily_series(conn, provider: str | None, now_kst: datetime,
                 *, providers: list[str] | None = None) -> list[DayPoint]:
    """일별 누적 비용 시계열. 기간 [달력 월 1일, 말일].

    실제 누적값은 오늘까지만 채우고 이후 날은 None(미래 구간 — 차트에서 선이 끊김).
    달력 월 기준(1일 시작, 고정). 레거시 budget_start clamp는 없다.
    """
    period_start, period_end = month_bounds(now_kst)
    last_day = (period_end - timedelta(days=1)).day
    rows = _range_rows(conn, provider, period_start, period_end, providers=providers)
    per_day: dict = {}
    for r in rows:
        dt = parse_ts(r["ts"])
        if dt:
            per_day[dt.day] = per_day.get(dt.day, 0.0) + (r["cost_usd"] or 0)
    out: list[DayPoint] = []
    cumulative = 0.0
    for d in range(period_start.day, last_day + 1):
        if d <= now_kst.day:
            cumulative += per_day.get(d, 0.0)
            out.append(DayPoint(day=d, cumulative_cost=round(cumulative, 4)))
        else:
            out.append(DayPoint(day=d, cumulative_cost=None))
    return out


def stacked_trend(
    per_provider: list[tuple[str, list[DayPoint]]],
) -> list[dict]:
    """provider별 누적 시계열을 스택 밴드 경계값으로 변환.

    per_provider: [(provider, [DayPoint, …]), …] — 모든 리스트가 같은 길이·날짜 정렬
        (동일 now_kst로 만든 daily_series라 보장됨; 달력 월 기준 고정).
    반환: [{"provider": str, "cum": [float|None], "top": [float|None]}, …]
        - cum = 그 provider의 원본 누적(툴팁 표시·% 분모용)
        - top = 아래 밴드까지 더한 running sum(차트 fill 경계용)
        - 어떤 날 cum 또는 아래 밴드 top이 None이면 그 날 top도 None(미래 끊김 전파).
    """
    out: list[dict] = []
    running: list[float | None] | None = None   # 직전(아래) 밴드의 top 배열
    for provider, points in per_provider:
        cum = [p.cumulative_cost for p in points]
        if running is None:
            top = [round(c, 4) if c is not None else None for c in cum]
        else:
            top = [
                None if c is None or r is None else round(r + c, 4)
                for c, r in zip(cum, running)
            ]
        out.append({"provider": provider, "cum": cum, "top": top})
        running = top
    return out


def session_detail(conn, session_id: str) -> SessionDetail | None:
    totals = conn.execute(
        "SELECT COUNT(*) rows, SUM(cost_usd) cost, SUM(web_search) ws, "
        "SUM(web_fetch) wf, MIN(ts) first_ts, MAX(ts) last_ts, MAX(provider) provider "
        "FROM messages WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if not totals or not totals["rows"]:
        return None

    meta = conn.execute(
        "SELECT project, provider, label, user_turns FROM sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()

    model_rows = conn.execute(
        "SELECT model, SUM(cost_usd) cost, SUM(input_tokens) it, SUM(output_tokens) ot, "
        "SUM(cache_creation) cc, SUM(cache_read) cr "
        "FROM messages WHERE session_id=? GROUP BY model ORDER BY cost DESC",
        (session_id,),
    ).fetchall()

    return SessionDetail(
        session_id=session_id,
        project=normalize_project(meta["project"]) if meta else None,
        provider=(meta["provider"] if meta else None) or totals["provider"],
        label=meta["label"] if meta else None,
        first_ts=totals["first_ts"], last_ts=totals["last_ts"],
        cost=round(totals["cost"] or 0, 4),
        msgs=(meta["user_turns"] if meta and meta["user_turns"] is not None else 0),
        web_search=totals["ws"] or 0, web_fetch=totals["wf"] or 0,
        models=[
            ModelRow(
                model=m["model"], cost=round(m["cost"] or 0, 4),
                input_tokens=m["it"] or 0, output_tokens=m["ot"] or 0,
                cache_creation=m["cc"] or 0, cache_read=m["cr"] or 0,
            )
            for m in model_rows
        ],
    )
