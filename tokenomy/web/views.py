"""DB → 화면용 dict 조립. 라우트(app.py)와 집계(aggregate.py)를 분리한다."""
from __future__ import annotations

from datetime import datetime

from tokenomy.aggregate import (
    KST, burndown, by_project, by_session, daily_series, insights, session_detail,
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


def session_context(conn, session_id: str) -> dict | None:
    detail = session_detail(conn, session_id)
    if detail is None:
        return None
    return {"detail": detail}
