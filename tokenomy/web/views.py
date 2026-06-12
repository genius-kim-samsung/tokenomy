"""DB → 화면용 dict 조립. 라우트(app.py)와 집계(aggregate.py)를 분리한다."""
from __future__ import annotations

from datetime import datetime

from tokenomy.aggregate import (
    KST, PROVIDERS, burndown, by_project, by_session, combined_burndown,
    daily_series, insights, session_detail,
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
