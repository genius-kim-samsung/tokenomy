"""DB → 화면용 dict 조립. 라우트(app.py)와 집계(aggregate.py)를 분리한다."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from tokenomy.aggregate import (
    KST, DIM_COLUMNS, DateGroup, DaySessionRow, FolderGroup, burndown,
    by_day_session, by_dimension, by_project, by_session, codex_burndown,
    daily_series, insights, period_bounds, session_detail, sidechain_split,
    stacked_trend,
)
from tokenomy.budget import budget_from_config, budget_start_kst, load_config, user_label

_SORT_KEYS = {
    "cost": lambda x: x.cost,
    "sessions": lambda x: x.sessions,
    "cache": lambda x: x.cache_ratio,
}

# 통합 추세 스택 영역 — provider별 (라벨, 선 색, 채움 색[반투명]).
# 스택 순서 = 등록 순서(아래→위). 신규 provider는 여기 한 줄만 추가하면 밴드가 자동 생성된다.
_TREND_STYLE: dict[str, tuple[str, str, str]] = {
    "claude": ("Claude", "#cc785c", "rgba(204,120,92,0.5)"),   # 코랄(기존 누적선 색 유지)
    "codex": ("Codex", "#5db8a6", "rgba(93,184,166,0.5)"),     # teal(DESIGN.md accent-teal)
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
    bs = budget_start_kst(config)

    claude_bd = burndown(conn, budget, now, "claude", budget_start=bs)
    codex_bd = codex_burndown(conn, budget, now, budget_start=bs)
    month_total = round(claude_bd.spent + codex_bd.spent, 4)

    projects = by_project(conn, None, now)
    projects.sort(key=_SORT_KEYS.get(sort, _SORT_KEYS["cost"]), reverse=True)
    projects = projects[:10]
    sessions = by_session(conn, None, now, limit_n=10)
    # 효율 코치/추세는 전 AI 합산·달력 월 기준 유지(설계). Burndown 인자는 claude 카드 재사용.
    coach = insights(conn, claude_bd, now, None)
    daily = daily_series(conn, None, now, budget_start=bs)

    # 통합 추세: provider별 누적을 스택 밴드로. 데이터 있는 provider만 등록 순서대로.
    trend_providers = [p for p in _TREND_STYLE if _provider_has_data(conn, p)]
    bands = stacked_trend(
        [(p, daily_series(conn, p, now, budget_start=bs)) for p in trend_providers]
    )
    trend_series = [
        {"label": _TREND_STYLE[b["provider"]][0],
         "color": _TREND_STYLE[b["provider"]][1],
         "fill": _TREND_STYLE[b["provider"]][2],
         "top": b["top"], "cum": b["cum"]}
        for b in bands
    ]
    trend_totals = bands[-1]["top"] if bands else [None for _ in daily]

    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    has_data = last is not None and last["t"] is not None

    return {
        "active_nav": "dashboard", "sort": sort,
        "user_label": user_label(config),
        "budget_configured": budget.total > 0,
        "budget_start": config.get("budget_start"),
        "month": now.strftime("%Y-%m"),
        "claude_bd": claude_bd, "codex_bd": codex_bd, "month_total": month_total,
        "claude_has_data": _provider_has_data(conn, "claude"),
        "codex_has_data": _provider_has_data(conn, "codex"),
        "projects": projects, "sessions": sessions, "insights": coach,
        "daily_labels": [p.day for p in daily],
        "trend_series": trend_series,
        "trend_totals": trend_totals,
        # 추세 기준 = 통합 월 예산(Claude+Codex). 페이스선 0→limit(말일에 예산 도달),
        # 가로선 = 예산 천장. 둘이 말일에서 수렴. 분모는 clamp된 기간 일수(len(daily)).
        "daily_pace": [round(budget.total / len(daily) * (i + 1), 4) if budget.total else 0.0
                       for i, _ in enumerate(daily)],
        "daily_budget": [budget.total if budget.total else 0.0 for _ in daily],
        "last_ts": last["t"] if has_data else None,
        "has_data": has_data,
    }


def session_context(conn, session_id: str) -> dict | None:
    detail = session_detail(conn, session_id)
    if detail is None:
        return None
    return {"detail": detail, "active_nav": "history"}


def _parse_date(value: str | None) -> datetime | None:
    """YYYY-MM-DD → KST 자정. 빈/오류 → None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=KST)
    except ValueError:
        return None


def _resolve_range(anchor_kst: datetime, period: str, start: str | None, end: str | None):
    """조회 기간 [start, nxt)와 표시 메타를 해석한다.

    우선순위: 유효한 사용자 지정(start≤end) > period(week/month) + anchor.
    반환: (start_dt, nxt_dt, label, period, custom)
    """
    s = _parse_date(start)
    e = _parse_date(end)
    if s and e and s <= e:
        nxt = e + timedelta(days=1)
        label = f"{s.strftime('%Y-%m-%d')} ~ {e.strftime('%Y-%m-%d')}"
        return s, nxt, label, period, True
    period = period if period in ("week", "month") else "month"
    start_dt, nxt_dt, label = period_bounds(period, anchor_kst)
    return start_dt, nxt_dt, label, period, False


DIM_LABELS = {"model": "모델", "skill": "스킬", "branch": "브랜치"}
_NULL_BUCKET = {"model": "(unknown)", "skill": "(미귀속)", "branch": "(브랜치 없음)"}


def dimension_context(conn, anchor_kst: datetime, provider: str, *,
                      dim: str = "model", now_kst: datetime | None = None,
                      period: str = "month", start: str | None = None,
                      end: str | None = None) -> dict:
    """차원별(모델/스킬/브랜치) 사용/비용 + 서브에이전트 비중. 주/월 또는 사용자 지정 구간."""
    dim = dim if dim in DIM_COLUMNS else "model"
    now = now_kst or datetime.now(KST)
    config = load_config()
    s, nxt, label, period, custom = _resolve_range(anchor_kst, period, start, end)
    rows = by_dimension(conn, provider or None, s, nxt, dim)
    total = round(sum(r.cost for r in rows), 4)
    null_label = _NULL_BUCKET[dim]
    table = [
        {"key": (r.key if r.key not in (None, "") else null_label), "cost": r.cost,
         "share": round(r.cost / total * 100, 1) if total else 0.0,
         "sessions": r.sessions, "cache_ratio": r.cache_ratio,
         "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
         "cache_creation": r.cache_creation, "cache_read": r.cache_read}
        for r in rows
    ]
    split = sidechain_split(conn, provider or None, s, nxt)
    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    return {
        "active_nav": "analysis", "user_label": user_label(config),
        "provider": provider, "dim": dim, "dim_label": DIM_LABELS[dim],
        "claude_only": dim in ("skill", "branch"), "split": split,
        "rows": table, "count": len(table), "total": total,
        "period": period, "custom": custom, "period_label": label,
        "anchor": anchor_kst.strftime("%Y-%m-%d"),
        "start": start or "", "end": end or "",
        "prev_anchor": (s - timedelta(days=1)).strftime("%Y-%m-%d"),
        "next_anchor": nxt.strftime("%Y-%m-%d"),
        "has_next": nxt <= now,
        "month": now.strftime("%Y-%m"),
        "last_ts": last["t"] if last and last["t"] else None,
    }


_WEEKDAY = "월화수목금토일"


_PREVIEW_N = 3   # 접힘 그룹 헤더에 보여줄 대표 작업요약 개수


def _folder_name(project: str | None) -> str:
    """폴더 그룹 키 — 프로젝트 경로 전체(없으면 (unknown))."""
    return project or "(unknown)"


def _preview(rows: list[DaySessionRow]) -> str:
    """비용 상위 작업요약 최대 N개를 ', '로 연결. 요약 전무하면 '(요약 없음)'."""
    tops = sorted(rows, key=lambda r: r.cost, reverse=True)
    names = [r.summary for r in tops if r.summary][:_PREVIEW_N]
    return ", ".join(names) if names else "(요약 없음)"


def build_date_tree(rows: list[DaySessionRow], sort: str) -> list[DateGroup]:
    """DaySessionRow 리스트 → 날짜→폴더→세션 2단 트리.

    폴더·세션 내부는 항상 비용 내림차순. 날짜 그룹 순서만 sort에 반응
    (date_desc 기본 / date_asc / day_cost=일 소계 큰 날 위로).
    캐시%는 토큰량 가중평균(Σcache_read / Σcache_den).
    """
    by_date: dict[str, dict[str, list[DaySessionRow]]] = {}
    for r in rows:
        by_date.setdefault(r.date, {}).setdefault(_folder_name(r.project), []).append(r)

    dgroups: list[DateGroup] = []
    for d, folders in by_date.items():
        fgroups: list[FolderGroup] = []
        for proj, frows in folders.items():
            frows.sort(key=lambda r: r.cost, reverse=True)
            den = sum(r.cache_den for r in frows)
            cr = sum(r.cache_read for r in frows)
            fgroups.append(FolderGroup(
                project=proj,
                cost=round(sum(r.cost for r in frows), 4),
                msgs=sum(r.msgs for r in frows),
                cache_ratio=round(cr / den, 4) if den else 0.0,
                preview=_preview(frows),
                rows=frows,
            ))
        fgroups.sort(key=lambda f: f.cost, reverse=True)
        all_rows = [r for f in fgroups for r in f.rows]
        den = sum(r.cache_den for r in all_rows)
        cr = sum(r.cache_read for r in all_rows)
        wd = _WEEKDAY[date.fromisoformat(d).weekday()]
        dgroups.append(DateGroup(
            date=d, weekday=wd,
            cost=round(sum(f.cost for f in fgroups), 4),
            msgs=sum(f.msgs for f in fgroups),
            cache_ratio=round(cr / den, 4) if den else 0.0,
            preview=_preview(all_rows),
            folders=fgroups,
        ))

    if sort == "date_asc":
        dgroups.sort(key=lambda g: g.date)
    elif sort == "day_cost":
        dgroups.sort(key=lambda g: g.cost, reverse=True)
    else:   # date_desc(기본)
        dgroups.sort(key=lambda g: g.date, reverse=True)
    return dgroups


def history_context(conn, anchor_kst: datetime, provider: str, sort: str,
                    now_kst: datetime | None = None, *,
                    period: str = "month", start: str | None = None,
                    end: str | None = None) -> dict:
    """내역 — 날짜→폴더→세션 트리. 주/월 기간 또는 사용자 지정 [start, end]."""
    now = now_kst or datetime.now(KST)
    config = load_config()
    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    s, nxt, label, period, custom = _resolve_range(anchor_kst, period, start, end)
    rows = by_day_session(conn, provider or None, start=s, nxt=nxt)
    tree = build_date_tree(rows, sort)
    return {
        "active_nav": "history",
        "user_label": user_label(config),
        "provider": provider, "sort": sort,
        "period": period, "custom": custom,
        "anchor": anchor_kst.strftime("%Y-%m-%d"),
        "start": start or "", "end": end or "",
        "month": now.strftime("%Y-%m"),
        "last_ts": last["t"] if last and last["t"] else None,
        "period_label": label,
        "prev_anchor": (s - timedelta(days=1)).strftime("%Y-%m-%d"),
        "next_anchor": nxt.strftime("%Y-%m-%d"),
        "has_next": nxt <= now,
        "tree": tree,
        "count": len(rows),
        "total": round(sum(r.cost for r in rows), 4),
    }
