"""DB → 화면용 dict 조립. 라우트(app.py)와 집계(aggregate.py)를 분리한다."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from tokenomy.aggregate import (
    KST, PROVIDERS, DayGroup, DaySessionRow, burndown, by_day_session, by_month,
    by_project, by_session, by_week, combined_burndown, daily_series, insights,
    month_bounds, period_bounds, session_detail,
)
from tokenomy.budget import budget_from_config, load_config, user_label

_SORT_KEYS = {
    "cost": lambda x: x.cost,
    "sessions": lambda x: x.sessions,
    "cache": lambda x: x.cache_ratio,
}


def _provider_has_data(conn, provider: str) -> bool:
    row = conn.execute(
        "SELECT MAX(ts) t FROM messages WHERE provider=?", (provider,)
    ).fetchone()
    return row is not None and row["t"] is not None


def overview_context(conn, sort: str, now_kst: datetime | None = None) -> dict:
    now = now_kst or datetime.now(KST)
    config = load_config()
    budget = budget_from_config(config)

    cards = [
        {"provider": p, "name": p.capitalize(),
         "bd": burndown(conn, budget, now, p),
         "has_data": _provider_has_data(conn, p)}
        for p in PROVIDERS
    ]
    # 통합 바(combined)는 한도 있는 provider만 합산(분자/분모 일치)하지만, 아래
    # 프로젝트·세션·추세는 전 AI 합산이다(의도된 설계 — 통합 바엔 "(한도 설정한 AI 합산)"
    # 라벨을 단다). 둘 다 한도 거는 일반 케이스에선 일치한다.
    combined = combined_burndown([c["bd"] for c in cards], now)

    projects = by_project(conn, None, now)
    projects.sort(key=_SORT_KEYS.get(sort, _SORT_KEYS["cost"]), reverse=True)
    projects = projects[:10]   # Top 10 롤업 (전체 목록은 각 provider 상세 탭)
    sessions = by_session(conn, None, now, limit_n=10)
    coach = insights(conn, combined, now, None)
    daily = daily_series(conn, None, now)
    pace = [round(combined.limit / combined.days_in_month * p.day, 4)
            if combined.limit else 0.0 for p in daily]

    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    has_data = last is not None and last["t"] is not None

    return {
        "active_nav": "dashboard", "sort": sort,
        "user_label": user_label(config),
        # combined.limit>0 == budget.total>0 (Budget가 PROVIDERS와 동일) — 통합 바에
        # 직결되도록 combined 기준 사용.
        "budget_configured": combined.limit > 0,
        "month": now.strftime("%Y-%m"),
        "combined": combined, "cards": cards,
        "projects": projects, "sessions": sessions, "insights": coach,
        "daily_labels": [p.day for p in daily],
        "daily_actual": [p.cumulative_cost for p in daily],
        "daily_pace": pace,
        "last_ts": last["t"] if has_data else None,
        "has_data": has_data,
    }


def session_context(conn, session_id: str) -> dict | None:
    detail = session_detail(conn, session_id)
    if detail is None:
        return None
    return {"detail": detail}


_GROUPED_SORTS = ("date_desc", "date_asc", "day_cost")
_WEEKDAY = "월화수목금토일"


def _group_by_date(rows: list[DaySessionRow]) -> list[DayGroup]:
    """DaySessionRow 리스트 → 날짜별 DayGroup. 그룹 내부 행은 비용 내림차순."""
    by: dict = {}
    for r in rows:
        by.setdefault(r.date, []).append(r)
    out = []
    for d, rs in by.items():
        rs.sort(key=lambda x: x.cost, reverse=True)
        wd = _WEEKDAY[date.fromisoformat(d).weekday()]
        out.append(DayGroup(date=d, weekday=wd,
                            subtotal=round(sum(x.cost for x in rs), 4), rows=rs))
    return out


def _history_nav_month(anchor_kst: datetime, now_kst: datetime) -> dict:
    """세션/폴더/일/주 보기 공통 — 월 단위 기간 메타."""
    start, nxt = month_bounds(anchor_kst)
    return {
        "period_label": start.strftime("%Y-%m"),
        "prev_anchor": (start - timedelta(days=1)).strftime("%Y-%m-%d"),
        "next_anchor": nxt.strftime("%Y-%m-%d"),
        "has_next": nxt <= now_kst,
        "_start": start, "_nxt": nxt,
    }


def history_context(conn, view: str, anchor_kst: datetime, provider: str,
                    sort: str, project: str = "", now_kst: datetime | None = None) -> dict:
    """내역 — view ∈ {session, folder, day, week, month} 디스패치.

    공통 메타(active_nav/provider/anchor/last_ts)에 view별 행 데이터를 합쳐 반환한다.
    """
    now = now_kst or datetime.now(KST)
    config = load_config()
    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    base = {
        "active_nav": "history", "view": view,
        "user_label": user_label(config),
        "provider": provider, "sort": sort, "project": project,
        "anchor": anchor_kst.strftime("%Y-%m-%d"),
        "month": now.strftime("%Y-%m"),
        "last_ts": last["t"] if last and last["t"] else None,
        # view별로 아래에서 덮어쓰는 기본값
        "is_grouped": False, "groups": [], "flat_rows": [], "rows": [],
        "count": 0, "total": 0.0,
    }

    if view == "month":
        rows = by_month(conn, provider or None, anchor_kst.year)
        if sort == "oldest":
            rows = sorted(rows, key=lambda m: m.month)
        elif sort == "cost":
            rows = sorted(rows, key=lambda m: m.cost, reverse=True)
        # recent(기본)은 by_month가 이미 최신순
        base.update({
            "rows": rows, "count": len(rows),
            "total": round(sum(m.cost for m in rows), 4),
            "period_label": str(anchor_kst.year),
            "prev_anchor": f"{anchor_kst.year - 1}-01-01",
            "next_anchor": f"{anchor_kst.year + 1}-01-01",
            "has_next": anchor_kst.year < now.year,
        })
        return base

    nav = _history_nav_month(anchor_kst, now)
    start, nxt = nav.pop("_start"), nav.pop("_nxt")
    base.update(nav)

    if view == "week":
        rows = by_week(conn, provider or None, anchor_kst)
        if sort == "oldest":
            rows = sorted(rows, key=lambda w: w.week_start)
        elif sort == "cost":
            rows = sorted(rows, key=lambda w: w.cost, reverse=True)
        base.update({"rows": rows, "count": len(rows),
                     "total": round(sum(w.cost for w in rows), 4)})
        return base

    if view == "folder":
        rows = by_project(conn, provider or None, now, start=start, nxt=nxt)
        rows.sort(key=_SORT_KEYS.get(sort, _SORT_KEYS["cost"]), reverse=True)
        base.update({"rows": rows, "count": len(rows),
                     "total": round(sum(p.cost for p in rows), 4)})
        return base

    if view == "session":
        order = sort if sort in ("cost", "recent") else "cost"
        rows = by_session(conn, provider or None, now, start=start, nxt=nxt,
                          order=order, project=project or None)
        base.update({"rows": rows, "count": len(rows),
                     "total": round(sum(s.cost for s in rows), 4)})
        return base

    # view == "day" (기존 동작 이식)
    rows = by_day_session(conn, provider or None, start=start, nxt=nxt)
    total = round(sum(r.cost for r in rows), 4)
    is_grouped = sort in _GROUPED_SORTS
    groups: list = []
    flat_rows: list = []
    if is_grouped:
        groups = _group_by_date(rows)
        if sort == "date_asc":
            groups.sort(key=lambda g: g.date)
        elif sort == "day_cost":
            groups.sort(key=lambda g: g.subtotal, reverse=True)
        else:
            groups.sort(key=lambda g: g.date, reverse=True)
    elif sort == "cache":
        flat_rows = sorted(rows, key=lambda r: r.cache_ratio)
    else:
        flat_rows = sorted(rows, key=lambda r: r.cost, reverse=True)
    base.update({"is_grouped": is_grouped, "groups": groups, "flat_rows": flat_rows,
                 "count": len(rows), "total": total})
    return base
