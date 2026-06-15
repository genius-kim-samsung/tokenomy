"""FastAPI 라우트 (얇게 — 라우팅+입력검증만). 데이터 조립은 views.py."""
from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tokenomy import __version__
from tokenomy.aggregate import KST, PROVIDERS, parse_ts
from tokenomy.budget import budget_from_config, load_config, save_config
from tokenomy.cli import cmd_ingest
from tokenomy.db import connect
from tokenomy.paths import resource_path
from tokenomy.update import check_update
from tokenomy.web.views import (
    history_context, models_context, overview_context, session_context,
)

_BASE = resource_path("tokenomy/web")
templates = Jinja2Templates(directory=str(_BASE / "templates"))
templates.env.globals["app_version"] = __version__


def _kstfmt(ts):
    dt = parse_ts(ts)
    return dt.strftime("%m-%d %H:%M") if dt else (ts or "")


templates.env.filters["kstfmt"] = _kstfmt

app = FastAPI(title="Tokenomy")
app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")

_SORTS = ("cost", "sessions", "cache")
_HISTORY_SORTS = ("date_desc", "date_asc", "day_cost")
_PERIODS = ("week", "month")


def _parse_anchor(value: str | None) -> datetime:
    """YYYY-MM-DD → KST datetime. 빈값/파싱실패 → 오늘(KST)."""
    if value:
        try:
            return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=KST)
        except ValueError:
            pass
    return datetime.now(KST)


@app.get("/")
def dashboard(request: Request, sort: str = "cost", notice: str | None = None):
    sort = sort if sort in _SORTS else "cost"
    conn = connect()
    update_tag = check_update(conn)
    ctx = overview_context(conn, sort)
    return templates.TemplateResponse(
        request, "overview.html",
        {**ctx, "notice": notice, "update_tag": update_tag},
    )


@app.get("/session/{session_id}")
def session_view(request: Request, session_id: str):
    conn = connect()
    ctx = session_context(conn, session_id)
    if ctx is None:
        return templates.TemplateResponse(
            request, "session.html", {"detail": None, "active_nav": "history"}, status_code=404
        )
    return templates.TemplateResponse(request, "session.html", ctx)


@app.get("/projects")
def projects_redirect():
    return RedirectResponse("/history", status_code=301)


@app.get("/sessions")
def sessions_redirect():
    return RedirectResponse("/history", status_code=301)


@app.get("/history")
def history_view(request: Request, anchor: str | None = None, provider: str = "",
                 sort: str | None = None, period: str | None = None,
                 start: str | None = None, end: str | None = None,
                 partial: str | None = None, notice: str | None = None):
    provider = provider if provider in PROVIDERS else ""
    sort = sort if sort in _HISTORY_SORTS else "date_desc"
    period = period if period in _PERIODS else "month"
    conn = connect()
    # htmx 요청(HX-Request)/명시적 partial=1 → 셸 없이 조각만. 단 히스토리 복원 요청은 전체 페이지.
    hx_partial = (request.headers.get("HX-Request") == "true"
                  and request.headers.get("HX-History-Restore-Request") != "true")
    is_partial = partial == "1" or hx_partial
    update_tag = None if is_partial else check_update(conn)
    ctx = history_context(conn, _parse_anchor(anchor), provider, sort,
                          period=period, start=start, end=end)
    template = "_history_body.html" if is_partial else "history.html"
    return templates.TemplateResponse(
        request, template, {**ctx, "notice": notice, "update_tag": update_tag},
    )


@app.get("/models")
def models_view(request: Request, anchor: str | None = None, provider: str = "",
                period: str | None = None, start: str | None = None,
                end: str | None = None, notice: str | None = None):
    provider = provider if provider in PROVIDERS else ""
    period = period if period in _PERIODS else "month"
    conn = connect()
    update_tag = check_update(conn)
    ctx = models_context(conn, _parse_anchor(anchor), provider,
                         period=period, start=start, end=end)
    return templates.TemplateResponse(
        request, "models.html",
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
    conn = connect()
    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    return templates.TemplateResponse(
        request, "settings.html",
        {"claude": budget.claude, "codex": budget.codex,
         "budget_start": config.get("budget_start") or "",
         "active_nav": "settings", "update_tag": check_update(conn),
         "last_ts": last["t"] if last and last["t"] else None},
    )


def _to_float(value: str | None) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0
    except ValueError:
        return 0.0


def _valid_date_or_none(value: str | None) -> str | None:
    """'YYYY-MM-DD'면 그대로, 아니면 None. 잘못된 입력으로 config가 깨지지 않게 한다."""
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return None


@app.post("/settings")
def settings_post(claude: str = Form(""), codex: str = Form(""),
                  budget_start: str = Form("")):
    config = load_config()
    config["budget"]["claude"] = _to_float(claude)
    config["budget"]["codex"] = _to_float(codex)
    config["budget_start"] = _valid_date_or_none(budget_start)
    save_config(config)
    return RedirectResponse("/", status_code=303)
