"""FastAPI 라우트 (얇게 — 라우팅+입력검증만). 데이터 조립은 views.py."""
from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tokenomy.aggregate import KST, PROVIDERS, parse_ts
from tokenomy.budget import budget_from_config, load_config, save_config
from tokenomy.cli import cmd_ingest
from tokenomy.db import connect
from tokenomy.paths import resource_path
from tokenomy.update import check_update
from tokenomy.web.views import (
    dashboard_context, history_context, overview_context, projects_context, sessions_context,
    session_context,
)

_BASE = resource_path("tokenomy/web")
templates = Jinja2Templates(directory=str(_BASE / "templates"))


def _kstfmt(ts):
    dt = parse_ts(ts)
    return dt.strftime("%m-%d %H:%M") if dt else (ts or "")


templates.env.filters["kstfmt"] = _kstfmt

app = FastAPI(title="Tokenomy")
app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")

_SORTS = ("cost", "sessions", "cache")
_PERIODS = ("day", "week", "month")
_ORDERS = ("cost", "recent")
_HISTORY_SORTS = ("date_desc", "date_asc", "day_cost", "cost", "cache")


def _parse_anchor(value: str | None) -> datetime:
    """YYYY-MM-DD → KST datetime. 빈값/파싱실패 → 오늘(KST)."""
    if value:
        try:
            return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=KST)
        except ValueError:
            pass
    return datetime.now(KST)


@app.get("/")
def dashboard(request: Request, provider: str | None = None, sort: str = "cost",
              notice: str | None = None):
    sort = sort if sort in _SORTS else "cost"
    conn = connect()
    update_tag = check_update(conn)
    if provider in PROVIDERS:
        ctx = dashboard_context(conn, provider, sort)
        template = "dashboard.html"
    else:
        ctx = overview_context(conn, sort)
        template = "overview.html"
    return templates.TemplateResponse(
        request, template,
        {**ctx, "notice": notice, "update_tag": update_tag},
    )


@app.get("/session/{session_id}")
def session_view(request: Request, session_id: str):
    conn = connect()
    ctx = session_context(conn, session_id)
    if ctx is None:
        return templates.TemplateResponse(
            request, "session.html", {"detail": None}, status_code=404
        )
    return templates.TemplateResponse(request, "session.html", ctx)


@app.get("/projects")
def projects_view(request: Request, period: str = "month", anchor: str | None = None,
                  provider: str = "", sort: str = "cost", notice: str | None = None):
    period = period if period in _PERIODS else "month"
    provider = provider if provider in PROVIDERS else ""
    sort = sort if sort in _SORTS else "cost"
    conn = connect()
    update_tag = check_update(conn)
    ctx = projects_context(conn, period, _parse_anchor(anchor), provider, sort)
    return templates.TemplateResponse(
        request, "projects.html",
        {**ctx, "notice": notice, "update_tag": update_tag},
    )


@app.get("/sessions")
def sessions_view(request: Request, period: str = "month", anchor: str | None = None,
                  provider: str = "", order: str = "cost",
                  project: str | None = None, notice: str | None = None):
    period = period if period in _PERIODS else "month"
    provider = provider if provider in PROVIDERS else ""
    order = order if order in _ORDERS else "cost"
    conn = connect()
    update_tag = check_update(conn)
    ctx = sessions_context(conn, period, _parse_anchor(anchor), provider, order, project or "")
    return templates.TemplateResponse(
        request, "sessions.html",
        {**ctx, "notice": notice, "update_tag": update_tag},
    )


@app.get("/history")
def history_view(request: Request, anchor: str | None = None, provider: str = "",
                 sort: str = "date_desc", partial: str | None = None,
                 notice: str | None = None):
    provider = provider if provider in PROVIDERS else ""
    sort = sort if sort in _HISTORY_SORTS else "date_desc"
    conn = connect()
    update_tag = None if partial == "1" else check_update(conn)  # 부분갱신은 셸 미렌더 → 조회 불필요
    ctx = history_context(conn, _parse_anchor(anchor), provider, sort)
    template = "_history_rows.html" if partial == "1" else "history.html"
    return templates.TemplateResponse(
        request, template,
        {**ctx, "notice": notice, "update_tag": update_tag},
    )


@app.post("/ingest")
def do_ingest():
    conn = connect()
    try:
        cmd_ingest(conn)
    except Exception:
        return RedirectResponse("/?notice=ingest-failed", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.get("/settings")
def settings_get(request: Request):
    config = load_config()
    budget = budget_from_config(config)
    return templates.TemplateResponse(
        request, "settings.html",
        {"claude": budget.claude, "codex": budget.codex},
    )


def _to_float(value: str | None) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0
    except ValueError:
        return 0.0


@app.post("/settings")
def settings_post(claude: str = Form(""), codex: str = Form("")):
    config = load_config()
    config["budget"]["claude"] = _to_float(claude)
    config["budget"]["codex"] = _to_float(codex)
    save_config(config)
    return RedirectResponse("/", status_code=303)
