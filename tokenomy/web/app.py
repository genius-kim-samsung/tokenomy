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
_HISTORY_VIEWS = ("session", "folder", "day", "week", "month")
_VIEW_SORTS = {
    "session": ("cost", "recent"),
    "folder": ("cost", "sessions", "cache"),
    "day": ("date_desc", "date_asc", "day_cost", "cost", "cache"),
    "week": ("recent", "oldest", "cost"),
    "month": ("recent", "oldest", "cost"),
}
_VIEW_DEFAULT_SORT = {"session": "cost", "folder": "cost", "day": "date_desc",
                      "week": "recent", "month": "recent"}


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
            request, "session.html", {"detail": None}, status_code=404
        )
    return templates.TemplateResponse(request, "session.html", ctx)


@app.get("/projects")
def projects_redirect():
    return RedirectResponse("/history?view=folder", status_code=301)


@app.get("/sessions")
def sessions_redirect():
    return RedirectResponse("/history?view=session", status_code=301)


@app.get("/history")
def history_view(request: Request, view: str = "session", anchor: str | None = None,
                 provider: str = "", sort: str | None = None, project: str | None = None,
                 partial: str | None = None, notice: str | None = None):
    view = view if view in _HISTORY_VIEWS else "session"
    provider = provider if provider in PROVIDERS else ""
    allowed = _VIEW_SORTS[view]
    sort = sort if sort in allowed else _VIEW_DEFAULT_SORT[view]
    conn = connect()
    # htmx 요청(HX-Request) 또는 명시적 partial=1 → 셸 없이 조각만 렌더.
    # 단 htmx 히스토리 복원 요청(HX-History-Restore-Request)은 페이지 셸 전체가 필요 →
    # 조각을 주면 복원 시 body가 행 조각으로 덮여 깨진다. 이 경우는 전체 페이지로.
    hx_partial = (request.headers.get("HX-Request") == "true"
                  and request.headers.get("HX-History-Restore-Request") != "true")
    is_partial = partial == "1" or hx_partial
    update_tag = None if is_partial else check_update(conn)  # 부분갱신은 셸 미렌더 → 조회 불필요
    ctx = history_context(conn, view, _parse_anchor(anchor), provider, sort, project or "")
    template = "_history_rows.html" if is_partial else "history.html"
    return templates.TemplateResponse(
        request, template,
        {**ctx, "notice": notice, "update_tag": update_tag},
    )


@app.get("/models")
def models_view(request: Request, anchor: str | None = None, provider: str = "",
                notice: str | None = None):
    provider = provider if provider in PROVIDERS else ""
    conn = connect()
    update_tag = check_update(conn)
    ctx = models_context(conn, _parse_anchor(anchor), provider)
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
