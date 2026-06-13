"""DB → 화면용 dict 조립. 라우트(app.py)와 집계(aggregate.py)를 분리한다."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from tokenomy.aggregate import (
    KST, PROVIDERS, DayGroup, DaySessionRow, burndown, by_day_session, by_project,
    by_session, combined_burndown, daily_series, insights, month_bounds, period_bounds,
    session_detail,
)
from tokenomy.budget import budget_from_config, load_config, user_label

_SORT_KEYS = {
    "cost": lambda x: x.cost,
    "sessions": lambda x: x.sessions,
    "cache": lambda x: x.cache_ratio,
}


def dashboard_context(conn, provider: str, sort: str, now_kst: datetime | None = None) -> dict:
    now = now_kst or datetime.now(KST)
    config = load_config()
    budget = budget_from_config(config)

    bd = burndown(conn, budget, now, provider)
    projects = by_project(conn, provider, now)
    projects.sort(key=_SORT_KEYS.get(sort, _SORT_KEYS["cost"]), reverse=True)
    projects = projects[:10]   # AI별 상세도 Top 10 미리보기, 전체는 /projects
    sessions = by_session(conn, provider, now, limit_n=10)
    cards = insights(conn, bd, now, provider)
    daily = daily_series(conn, provider, now)

    # 예산 페이스 라인(한도 ÷ 월일수 × day) — Chart.js 비교선
    pace = [round(bd.limit / bd.days_in_month * p.day, 4) if bd.limit else 0.0 for p in daily]

    last = conn.execute(
        "SELECT MAX(ts) t FROM messages WHERE provider=?", (provider,)
    ).fetchone()
    has_data = last is not None and last["t"] is not None

    return {
        "provider": provider, "sort": sort,
        "active_tab": provider,
        "user_label": user_label(config),
        "budget_configured": budget.total > 0,
        "month": now.strftime("%Y-%m"),
        "burndown": bd, "projects": projects, "sessions": sessions,
        "insights": cards,
        "daily_labels": [p.day for p in daily],
        "daily_actual": [p.cumulative_cost for p in daily],
        "daily_pace": pace,
        "last_ts": last["t"] if has_data else None,
        "has_data": has_data,
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
        "active_tab": "overview", "sort": sort,
        "user_label": user_label(config),
        # combined.limit>0 == budget.total>0 (Budget가 PROVIDERS와 동일) — 통합 바에
        # 직결되도록 combined 기준 사용. dashboard_context는 budget.total>0로 동치.
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


def projects_context(conn, period: str, anchor_kst: datetime, provider: str,
                     sort: str, now_kst: datetime | None = None) -> dict:
    """전체 프로젝트 목록(/projects). 기간 [start,nxt)로 집계 후 sort 키로 재정렬."""
    now = now_kst or datetime.now(KST)
    config = load_config()
    start, nxt, label = period_bounds(period, anchor_kst)
    rows = by_project(conn, provider or None, now, start=start, nxt=nxt)
    rows.sort(key=_SORT_KEYS.get(sort, _SORT_KEYS["cost"]), reverse=True)
    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    return {
        "active_tab": provider or "overview",
        "user_label": user_label(config),
        "period": period, "period_label": label,
        "anchor": anchor_kst.strftime("%Y-%m-%d"),
        "provider": provider, "sort": sort,
        "rows": rows, "count": len(rows),
        "total": round(sum(r.cost for r in rows), 4),
        "prev_anchor": (start - timedelta(days=1)).strftime("%Y-%m-%d"),
        "next_anchor": nxt.strftime("%Y-%m-%d"),
        "has_next": nxt <= now,                 # 현재/미래 기간이면 다음 숨김
        "month": now.strftime("%Y-%m"),         # _tabs.html 헤더용
        "last_ts": last["t"] if last and last["t"] else None,
    }


def sessions_context(conn, period: str, anchor_kst: datetime, provider: str,
                     order: str, project: str, now_kst: datetime | None = None) -> dict:
    """전체 세션 목록(/sessions). order=cost|recent, project 드릴다운 필터."""
    now = now_kst or datetime.now(KST)
    config = load_config()
    start, nxt, label = period_bounds(period, anchor_kst)
    rows = by_session(conn, provider or None, now, start=start, nxt=nxt,
                      order=order, project=project or None)
    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    return {
        "active_tab": provider or "overview",
        "user_label": user_label(config),
        "period": period, "period_label": label,
        "anchor": anchor_kst.strftime("%Y-%m-%d"),
        "provider": provider, "order": order, "project": project,
        "rows": rows, "count": len(rows),
        "total": round(sum(r.cost for r in rows), 4),
        "prev_anchor": (start - timedelta(days=1)).strftime("%Y-%m-%d"),
        "next_anchor": nxt.strftime("%Y-%m-%d"),
        "has_next": nxt <= now,
        "month": now.strftime("%Y-%m"),
        "last_ts": last["t"] if last and last["t"] else None,
    }


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


def history_context(conn, anchor_kst: datetime, provider: str,
                    sort: str, now_kst: datetime | None = None) -> dict:
    """내역(/history). 월 고정. sort에 따라 그룹(date_desc/date_asc/day_cost) 또는
    평면(cost/cache)으로 조립한다. 평면은 날짜 그룹을 깨고 단일 정렬 리스트.
    cache 정렬은 캐시 효율이 낮은(개선 여지 큰) 세션을 먼저 보이도록 오름차순이다."""
    now = now_kst or datetime.now(KST)
    config = load_config()
    start, nxt = month_bounds(anchor_kst)
    rows = by_day_session(conn, provider or None, start=start, nxt=nxt)
    total = round(sum(r.cost for r in rows), 4)
    count = len(rows)

    is_grouped = sort in _GROUPED_SORTS
    groups: list = []
    flat_rows: list = []
    if is_grouped:
        groups = _group_by_date(rows)
        if sort == "date_asc":
            groups.sort(key=lambda g: g.date)
        elif sort == "day_cost":
            groups.sort(key=lambda g: g.subtotal, reverse=True)
        else:  # date_desc (기본)
            groups.sort(key=lambda g: g.date, reverse=True)
    # 평면 정렬은 안정 정렬 — 동률은 by_day_session의 (date, session_id) 내림차순이 유지된다.
    elif sort == "cache":
        flat_rows = sorted(rows, key=lambda r: r.cache_ratio)            # 낮은 순
    else:  # cost
        flat_rows = sorted(rows, key=lambda r: r.cost, reverse=True)

    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    return {
        "active_tab": provider or "overview",
        "user_label": user_label(config),
        "provider": provider, "sort": sort,
        "is_grouped": is_grouped, "groups": groups, "flat_rows": flat_rows,
        "count": count, "total": total,
        "period_label": start.strftime("%Y-%m"),
        "anchor": anchor_kst.strftime("%Y-%m-%d"),
        "prev_anchor": (start - timedelta(days=1)).strftime("%Y-%m-%d"),
        "next_anchor": nxt.strftime("%Y-%m-%d"),
        "has_next": nxt <= now,
        "month": now.strftime("%Y-%m"),
        "last_ts": last["t"] if last and last["t"] else None,
    }
