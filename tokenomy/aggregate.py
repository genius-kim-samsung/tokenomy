"""집계 — 번다운, 프로젝트별 비용, 효율 신호.

월 경계는 KST 기준. transcript ts는 UTC(ISO8601)라 KST로 변환해 버킷팅한다.
PoC는 메시지를 Python에서 필터(데이터 규모 작음); 규모가 커지면 SQL 집계로 이전.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from tokenomy.budget import Budget

KST = timezone(timedelta(hours=9))

# 합산/탭바가 도는 provider 목록. 3번째 AI 추가 시 여기 + Budget 필드 + 파서 + 단가만 보강.
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


def effective_month_start(now_kst: datetime, budget_start: datetime | None) -> datetime:
    """이번 달 기간 시작 — budget_start가 이번 달 안이면 그 날짜로 clamp, 아니면 1일.

    budget_start가 과거·미래 달이면 무시(달력 월 1일). 일회성 도입일이 첫 달만
    영향을 주도록 한다.
    """
    start, nxt = month_bounds(now_kst)
    if budget_start and start <= budget_start < nxt:
        return _midnight(budget_start)
    return start


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
class Burndown:
    provider: str
    limit: float
    spent: float
    pct: float
    days_in_month: int
    day_of_month: int
    days_left: int
    daily_avg: float
    projected_month: float
    exhaust_day: int | None
    on_track: bool
    unpriced_count: int
    status: str  # "ok" | "warn" | "exceeds"


@dataclass
class ProjectRow:
    project: str | None
    cost: float
    sessions: int
    cache_ratio: float


def _range_rows(conn, provider: str | None, start: datetime, nxt: datetime) -> list:
    cols = ("SELECT ts, cost_usd, priced, session_id, project, "
            "input_tokens, cache_creation, cache_read, web_search FROM messages")
    if provider is None:
        rows = conn.execute(cols).fetchall()          # 전 AI 합산
    else:
        rows = conn.execute(cols + " WHERE provider=?", (provider,)).fetchall()
    out = []
    for r in rows:
        dt = parse_ts(r["ts"])
        if dt and start <= dt < nxt:
            d = dict(r)
            d["project"] = normalize_project(d["project"])  # 워크트리 → 부모 repo로 합산
            out.append(d)
    return out


def _month_rows(conn, provider: str | None, now_kst: datetime) -> list:
    start, nxt = month_bounds(now_kst)
    return _range_rows(conn, provider, start, nxt)


def _compute_burndown(provider: str, spent: float, limit: float,
                      unpriced: int, now_kst: datetime, *,
                      period_start: datetime | None = None,
                      period_end: datetime | None = None) -> Burndown:
    """집계된 (spent, limit, unpriced)로 Burndown을 산출하는 순수 함수.

    period_start/end 미지정 시 now_kst의 달력 월을 기간으로 쓴다(하위호환). 지정 시
    그 기간 [start, end)를 기준으로 경과일·예상치를 계산한다(예: 도입일 clamp).
    provider별 burndown과 통합 combined_burndown이 공유한다.
    """
    if period_start is None or period_end is None:
        period_start, period_end = month_bounds(now_kst)
    days_in_month = (period_end - period_start).days
    day_of_month = (_midnight(now_kst) - period_start).days + 1
    days_left = days_in_month - day_of_month
    daily_avg = spent / day_of_month if day_of_month else 0.0
    projected = daily_avg * days_in_month
    pct = (spent / limit) if limit > 0 else 0.0

    exhaust_day: int | None = None
    if daily_avg > 0 and limit > 0:
        d = limit / daily_avg
        if d <= days_in_month:
            exhaust_day = int(d) if d == int(d) else int(d) + 1  # ceil

    on_track = (projected <= limit) if limit > 0 else True

    if limit > 0 and spent >= limit:
        status = "exceeds"
    elif limit > 0 and projected > limit:
        status = "warn"
    else:
        status = "ok"

    return Burndown(
        provider=provider, limit=limit, spent=round(spent, 4), pct=round(pct, 4),
        days_in_month=days_in_month, day_of_month=day_of_month, days_left=days_left,
        daily_avg=round(daily_avg, 4), projected_month=round(projected, 4),
        exhaust_day=exhaust_day, on_track=on_track, unpriced_count=unpriced,
        status=status,
    )


def burndown(conn, budget: Budget, now_kst: datetime, provider: str = "claude",
             *, budget_start: datetime | None = None) -> Burndown:
    period_start = effective_month_start(now_kst, budget_start)
    _, period_end = month_bounds(now_kst)
    rows = _range_rows(conn, provider, period_start, period_end)
    spent = sum((r["cost_usd"] or 0) for r in rows)
    unpriced = sum(1 for r in rows if not r["priced"])
    limit = budget.limit_for(provider)
    return _compute_burndown(provider, spent, limit, unpriced, now_kst,
                             period_start=period_start, period_end=period_end)


def combined_burndown(cards: list[Burndown], now_kst: datetime) -> Burndown:
    """provider별 Burndown 리스트 → 통합 Burndown.

    한도(limit>0)가 있는 provider만 spent·limit·unpriced를 합산해 분자/분모 범위를
    일치시킨다(예: claude 한도만 있으면 codex 지출은 통합 바에서 제외). 한도 있는
    provider가 하나도 없으면 limit=0(사용량만, spent=전체 합산)으로 둔다.
    호출부는 PROVIDERS 전체를 넘기므로 cards는 비어있지 않다(빈 리스트는 한도 0·지출 0).
    """
    capped = [c for c in cards if c.limit > 0]
    if capped:
        spent = sum(c.spent for c in capped)
        limit = sum(c.limit for c in capped)
        unpriced = sum(c.unpriced_count for c in capped)
    else:
        spent = sum(c.spent for c in cards)
        limit = 0.0
        unpriced = sum(c.unpriced_count for c in cards)
    return _compute_burndown("전체", spent, limit, unpriced, now_kst)


@dataclass
class CodexBurndown:
    """Codex 주간 누적(carryover) 번다운.

    분모 limit_to_date = weekly_limit(W) × weeks_elapsed(N).
    분자 spent = effective_start ~ 이번 달 누적 지출. remaining = 이번 주 가용(이월 포함).
    월이 바뀌면 분자·분모 모두 리셋(이월 소멸). 주간 모델이라 예상 월말은 내지 않는다.
    """
    provider: str           # "codex"
    weekly_limit: float     # W = 월한도 ÷ 4
    weeks_elapsed: int      # N (이번 달 충전 횟수)
    limit_to_date: float    # W × N
    spent: float            # 이번 달 누적 지출(effective_start~)
    remaining: float        # 이번 주 가용 = limit_to_date − spent
    pct: float
    status: str             # "ok" | "exceeds"
    unpriced_count: int
    week_spent: float       # 이번 주(월요일~)만의 지출(표시용)


def codex_burndown(conn, budget: Budget, now_kst: datetime,
                   *, budget_start: datetime | None = None) -> CodexBurndown:
    """Codex 주간 누적(carryover) 번다운을 산출한다.

    effective_start(도입일 or 달력 월 1일)부터 이번 달 말까지의 누적 지출과
    weekly_limit × weeks_elapsed를 비교한다. 이월 모델이라 일별 예상치는 제공하지 않는다.
    """
    month_start, month_end = month_bounds(now_kst)
    eff = effective_month_start(now_kst, budget_start)
    weekly = budget.weekly_codex_limit()
    weeks = week_count(eff, now_kst)
    limit_to_date = round(weekly * weeks, 4)

    rows = _range_rows(conn, "codex", eff, month_end)
    spent = round(sum((r["cost_usd"] or 0) for r in rows), 4)
    unpriced = sum(1 for r in rows if not r["priced"])
    remaining = round(limit_to_date - spent, 4)
    pct = round(spent / limit_to_date, 4) if limit_to_date > 0 else 0.0

    week_start = max(_midnight(now_kst) - timedelta(days=now_kst.weekday()), eff)
    week_rows = _range_rows(conn, "codex", week_start, month_end)
    week_spent = round(sum((r["cost_usd"] or 0) for r in week_rows), 4)

    status = "exceeds" if (limit_to_date > 0 and spent >= limit_to_date) else "ok"

    return CodexBurndown(
        provider="codex", weekly_limit=round(weekly, 4), weeks_elapsed=weeks,
        limit_to_date=limit_to_date, spent=spent, remaining=remaining, pct=pct,
        status=status, unpriced_count=unpriced, week_spent=week_spent,
    )


def by_project(conn, provider: str | None, now_kst: datetime, limit_n: int | None = None,
               *, start: datetime | None = None, nxt: datetime | None = None) -> list[ProjectRow]:
    assert (start is None) == (nxt is None), "start/nxt는 함께 지정해야 한다"
    rows = _range_rows(conn, provider, start, nxt) if (start and nxt) else _month_rows(conn, provider, now_kst)
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
) -> list[SessionRow]:
    """세션별 비용·효율 + 라벨/작업요약. start/nxt 미지정 시 이번 달 기준.

    label = 수동 귀속 라벨, summary = Claude Code aiTitle 캐시(sessions.summary).
    order="cost"(비용순) | "recent"(last_ts 최신순). project가 주어지면 그 프로젝트만.
    """
    assert (start is None) == (nxt is None), "start/nxt는 함께 지정해야 한다"
    rows = _range_rows(conn, provider, start, nxt) if (start and nxt) else _month_rows(conn, provider, now_kst)
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


def by_day_session(conn, provider: str | None, *, start: datetime, nxt: datetime) -> list[DaySessionRow]:
    """(KST날짜 × 세션) 단위 행. 기간 [start, nxt) 내 메시지를 날짜+세션으로 버킷팅한다.

    is_continued: 세션 최초 등장일(전체 messages의 MIN(ts))보다 이 행 날짜가 이후인가.
                  조회 범위가 아닌 전체에서 구해야 지난달 시작→이번달 이어짐을 오판하지 않는다.
    cache_miss:   is_continued AND cache_ratio < INSIGHT_CACHE_READ_MIN(첫 등장일은 절대 제외).
    """
    rows = _range_rows(conn, provider, start, nxt)

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


def token_composition(conn, provider: str | None, start, nxt) -> TokenComposition:
    """기간 [start, nxt) 내 input/output/cache_creation/cache_read 합계와 비중(%)을 반환.

    _range_rows는 output_tokens를 select하지 않아 재사용하지 않고 자체 SELECT한다.
    비중은 0~100 퍼센트값(round(x/total*100,1)) — cache_ratio(0~1)와 단위가 다르다.
    """
    sql = "SELECT ts, input_tokens, output_tokens, cache_creation, cache_read FROM messages"
    if provider is None:
        rows = conn.execute(sql).fetchall()
    else:
        rows = conn.execute(sql + " WHERE provider=?", (provider,)).fetchall()
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
                 dim: str = "model") -> list[DimensionRow]:
    """기간 [start, nxt) 내 차원(dim) 단위 합계. 비용 내림차순.

    dim은 DIM_COLUMNS 화이트리스트 키. 빈 문자열/NULL 키는 None 버킷(미귀속)으로 접는다.
    """
    col = DIM_COLUMNS.get(dim, "model")
    sql = (f"SELECT ts, {col} AS key, cost_usd, session_id, input_tokens, output_tokens, "
           "cache_creation, cache_read FROM messages")
    if provider is None:
        rows = conn.execute(sql).fetchall()
    else:
        rows = conn.execute(sql + " WHERE provider=?", (provider,)).fetchall()
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


def sidechain_split(conn, provider: str | None, start: datetime, nxt: datetime) -> SidechainSplit:
    """기간 [start, nxt) 내 is_sidechain 기준 부모 vs 서브에이전트 비용·토큰 분리."""
    sql = ("SELECT ts, is_sidechain, cost_usd, input_tokens, output_tokens, "
           "cache_creation, cache_read FROM messages")
    if provider is None:
        rows = conn.execute(sql).fetchall()
    else:
        rows = conn.execute(sql + " WHERE provider=?", (provider,)).fetchall()
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


def insights(conn, bd: "Burndown", now_kst: datetime, provider: str | None) -> list[Insight]:
    rows = _month_rows(conn, provider, now_kst)
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
        for r in by_day_session(conn, provider, start=month_start, nxt=month_nxt)
        if r.cache_miss
    }
    if rebuild_sessions:
        cards.append(Insight(
            "info",
            f"캐시 재구축 {len(rebuild_sessions)}개 세션 — 이어지는 작업에서 컨텍스트 재빌드(세션 유지로 개선 여지)",
        ))
    if bd.unpriced_count:
        cards.append(Insight("warn", f"단가 미식별 {bd.unpriced_count}건 — 비용 누락 가능"))
    if bd.limit > 0 and bd.projected_month > bd.limit:
        cards.append(Insight("warn", f"현 추세 월말 ${bd.projected_month:.0f} 예상 — 한도 초과 가능"))

    if not cards:
        cards.append(Insight("info", "특이 신호 없음"))
    return cards


@dataclass
class DayPoint:
    day: int
    cumulative_cost: float | None   # 미래(오늘 이후) 구간은 None → 차트에서 선이 끊김


def daily_series(conn, provider: str | None, now_kst: datetime,
                 *, budget_start: datetime | None = None) -> list[DayPoint]:
    """일별 누적 비용 시계열. 기간 [effective_month_start, 말일].

    실제 누적값은 오늘까지만 채우고 이후 날은 None(미래 구간 — 차트에서 선이 끊김).
    budget_start로 도입일을 clamp한다(번다운 카드와 동일). 미지정 시 달력 월 1일(하위호환).
    """
    period_start = effective_month_start(now_kst, budget_start)
    _, period_end = month_bounds(now_kst)
    last_day = (period_end - timedelta(days=1)).day
    rows = _range_rows(conn, provider, period_start, period_end)
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
        (동일 now_kst/budget_start로 만든 daily_series라 보장됨).
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
