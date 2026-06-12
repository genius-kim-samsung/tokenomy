"""집계 — 번다운, 프로젝트별 비용, 효율 신호.

월 경계는 KST 기준. transcript ts는 UTC(ISO8601)라 KST로 변환해 버킷팅한다.
PoC는 메시지를 Python에서 필터(데이터 규모 작음); 규모가 커지면 SQL 집계로 이전.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from tokenomy.budget import Budget

KST = timezone(timedelta(hours=9))

# 합산/탭바가 도는 provider 목록. 3번째 AI 추가 시 여기 + Budget 필드 + 파서 + 단가만 보강.
PROVIDERS = ("claude", "codex")

# 효율 코치 휴리스틱 임계값 — 실데이터 캘리브레이션 전 튜닝값(단정 금지, 신호로만 사용)
INSIGHT_CACHE_READ_MIN = 0.30   # 월 cache_read 비율이 이 미만이면 경고
INSIGHT_WEB_SEARCH_MAX = 50     # 월 web_search 합이 이 초과면 정보 카드


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


def _month_rows(conn, provider: str | None, now_kst: datetime) -> list:
    start, nxt = month_bounds(now_kst)
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
            out.append(r)
    return out


def _compute_burndown(provider: str, spent: float, limit: float,
                      unpriced: int, now_kst: datetime) -> Burndown:
    """집계된 (spent, limit, unpriced)로 Burndown을 산출하는 순수 함수.
    provider별 burndown과 통합 combined_burndown이 공유한다."""
    start, nxt = month_bounds(now_kst)
    days_in_month = (nxt - start).days
    day_of_month = now_kst.day
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


def burndown(conn, budget: Budget, now_kst: datetime, provider: str = "claude") -> Burndown:
    rows = _month_rows(conn, provider, now_kst)
    spent = sum((r["cost_usd"] or 0) for r in rows)
    unpriced = sum(1 for r in rows if not r["priced"])
    limit = budget.limit_for(provider)
    return _compute_burndown(provider, spent, limit, unpriced, now_kst)


def combined_burndown(cards: list[Burndown], now_kst: datetime) -> Burndown:
    """provider별 Burndown 리스트 → 통합 Burndown.

    한도(limit>0)가 있는 provider만 spent·limit·unpriced를 합산해 분자/분모 범위를
    일치시킨다(예: claude 한도만 있으면 codex 지출은 통합 바에서 제외). 한도 있는
    provider가 하나도 없으면 limit=0(사용량만, spent=전체 합산)으로 둔다.
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


def by_project(conn, provider: str | None, now_kst: datetime, limit_n: int | None = None) -> list[ProjectRow]:
    rows = _month_rows(conn, provider, now_kst)
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
class SessionRow:
    session_id: str
    project: str | None
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
) -> list[SessionRow]:
    """이번 달 세션별 비용·효율 + 라벨/작업요약.

    label = 수동 귀속 라벨, summary = Claude Code aiTitle 캐시(sessions.summary).
    order="cost"(비용순) | "recent"(last_ts 최신순). project가 주어지면 그 프로젝트만.
    """
    rows = _month_rows(conn, provider, now_kst)
    meta = {
        r["session_id"]: (r["label"], r["summary"])
        for r in conn.execute("SELECT session_id, label, summary FROM sessions").fetchall()
    }
    agg: dict = {}
    for r in rows:
        if project is not None and (r["project"] or "(unknown)") != project:
            continue
        sid = r["session_id"]
        a = agg.setdefault(
            sid,
            {"project": r["project"], "cost": 0.0, "msgs": 0,
             "first": r["ts"], "last": r["ts"], "cr": 0, "den": 0},
        )
        a["cost"] += r["cost_usd"] or 0
        a["msgs"] += 1
        if r["ts"] and (a["first"] is None or r["ts"] < a["first"]):
            a["first"] = r["ts"]
        if r["ts"] and (a["last"] is None or r["ts"] > a["last"]):
            a["last"] = r["ts"]
        a["cr"] += r["cache_read"] or 0
        a["den"] += (r["input_tokens"] or 0) + (r["cache_creation"] or 0) + (r["cache_read"] or 0)

    out = [
        SessionRow(
            session_id=sid, project=a["project"],
            label=meta.get(sid, (None, None))[0],
            summary=meta.get(sid, (None, None))[1],
            cost=round(a["cost"], 4), first_ts=a["first"], last_ts=a["last"],
            msgs=a["msgs"], cache_ratio=round(a["cr"] / a["den"], 4) if a["den"] else 0.0,
        )
        for sid, a in agg.items()
    ]
    if order == "recent":
        out.sort(key=lambda x: x.last_ts or "", reverse=True)
    else:
        out.sort(key=lambda x: x.cost, reverse=True)
    return out[:limit_n] if limit_n else out


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
    cumulative_cost: float


def daily_series(conn, provider: str | None, now_kst: datetime) -> list[DayPoint]:
    rows = _month_rows(conn, provider, now_kst)
    per_day: dict = {}
    for r in rows:
        dt = parse_ts(r["ts"])
        if dt:
            per_day[dt.day] = per_day.get(dt.day, 0.0) + (r["cost_usd"] or 0)
    out: list[DayPoint] = []
    cumulative = 0.0
    for d in range(1, now_kst.day + 1):
        cumulative += per_day.get(d, 0.0)
        out.append(DayPoint(day=d, cumulative_cost=round(cumulative, 4)))
    return out


def session_detail(conn, session_id: str) -> SessionDetail | None:
    totals = conn.execute(
        "SELECT COUNT(*) msgs, SUM(cost_usd) cost, SUM(web_search) ws, "
        "SUM(web_fetch) wf, MIN(ts) first_ts, MAX(ts) last_ts, MAX(provider) provider "
        "FROM messages WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if not totals or not totals["msgs"]:
        return None

    meta = conn.execute(
        "SELECT project, provider, label FROM sessions WHERE session_id=?",
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
        project=meta["project"] if meta else None,
        provider=(meta["provider"] if meta else None) or totals["provider"],
        label=meta["label"] if meta else None,
        first_ts=totals["first_ts"], last_ts=totals["last_ts"],
        cost=round(totals["cost"] or 0, 4), msgs=totals["msgs"],
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
