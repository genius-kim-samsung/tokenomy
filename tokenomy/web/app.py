"""FastAPI 라우트 (얇게 — 라우팅+입력검증만). 데이터 조립은 views.py."""
from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tokenomy import __version__
from tokenomy.aggregate import KST, DIM_COLUMNS, PROVIDERS, parse_ts, official_view, combined_forecast
from tokenomy.config import ACCOUNT_MODES, account_mode, credit_to_usd as _credit_to_usd, debug_mode, forecast_settings, load_config, official_fetch_settings, tracked_providers, save_config
from tokenomy.cli import cmd_ingest
from tokenomy.db import connect
from tokenomy.official_fetch import refresh_tracked
from tokenomy.paths import mini_view_available, resource_path
from tokenomy.pricing import apply_pricing_overrides, load_pricing
from tokenomy.update import check_update
from tokenomy.web import control
from tokenomy.web.views import (
    _curation_for, coverage_card_context, dimension_context, history_context, mini_view_context,
    official_history_context, official_raw_context, official_section_context,
    overview_context, session_context, settings_provider_toggles, sidebar_freshness,
)

_BASE = resource_path("tokenomy/web")


def _nav_context(request: Request) -> dict:
    """모든 템플릿에 내비 플래그 주입(ADR 0010). 사용 이력(공식) 링크는 소진형 풀이
    있을 때만 — 페이지의 has_pool과 동일 로직(combined_forecast is not None)이라 일관.
    실패 시 보수적으로 노출(빈 페이지가 죽은 숨김보다 안전)."""
    try:
        conn = connect()
        config = load_config()
        now = datetime.now(KST)
        active = tracked_providers(config)
        ctu = _credit_to_usd(config)
        weeks = forecast_settings(config)["rate_window_weeks"]
        _, is_pooled = _curation_for(config)
        fobj = combined_forecast(conn, [official_view(conn, p, now, ctu, weeks, is_pooled=is_pooled)
                                        for p in active], now, weeks, is_pooled=is_pooled)
        return {"show_official_history": fobj is not None, "debug_mode": debug_mode(config)}
    except Exception:
        return {"show_official_history": True, "debug_mode": False}


templates = Jinja2Templates(directory=str(_BASE / "templates"), context_processors=[_nav_context])
templates.env.globals["app_version"] = __version__
# 미니뷰 가용 플랫폼 게이트(ADR 0013) — 프로세스당 상수(플랫폼 불변)라 글로벌로 1회 평가.
# Linux(Wayland)에선 False → 사이드바 '미니뷰' 전환 버튼을 서버에서 아예 안 그린다.
templates.env.globals["mini_view_available"] = mini_view_available()


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


@app.get("/official-history")
def official_history_view(request: Request, anchor: str | None = None, provider: str = "",
                          period: str | None = None, start: str | None = None,
                          end: str | None = None, notice: str | None = None):
    """사용 이력(공식) 화면(ADR 0010) — 통합 풀 누적 선 + 일별 소비 막대 + 일별 표."""
    provider = provider if provider in PROVIDERS else ""
    period = period if period in _PERIODS else "month"
    conn = connect()
    update_tag = check_update(conn)
    ctx = official_history_context(conn, _parse_anchor(anchor), provider,
                                   period=period, start=start, end=end)
    return templates.TemplateResponse(
        request, "official_history.html", {**ctx, "notice": notice, "update_tag": update_tag},
    )


@app.get("/official/raw")
def official_raw_view(request: Request, provider: str = "", fetched_at: str = ""):
    """공식 raw 디버그 페이지(ADR 0014). 디버그 OFF면 404 — 완전한 숨김 패리티.

    포착 자체는 debug와 무관하게 항상 ON이므로, 켜는 즉시 지난 7일 raw가 이미 보인다.
    """
    config = load_config()
    if not debug_mode(config):
        raise HTTPException(status_code=404)
    conn = connect()
    ctx = official_raw_context(conn, config, provider=provider or None,
                               fetched_at=fetched_at or None)
    return templates.TemplateResponse(
        request, "official_raw.html", {**ctx, "update_tag": check_update(conn)},
    )


@app.post("/app/debug-toggle")
def debug_toggle(enabled: str = Form(None)):
    """디버그 모드 토글(ADR 0014). enabled 명시(1/0)면 그 값으로, 없으면 현재를 뒤집는다.

    사이드바 버전 7회 탭(JS)이 enabled=1로, 설정 화면 OFF 버튼이 enabled=0으로 호출한다.
    켜면 새로 열린 raw 페이지로, 끄면 설정으로 보낸다(맥락에 맞는 착지).
    """
    config = load_config()
    if enabled is None:
        new = not debug_mode(config)
    else:
        new = enabled in ("1", "true", "True", "on")
    config["debug_mode"] = new
    save_config(config)
    return RedirectResponse("/official/raw" if new else "/settings?saved=1", status_code=303)


@app.get("/models")
def models_redirect():
    return RedirectResponse("/analysis?dim=model", status_code=301)


@app.get("/analysis")
def analysis_view(request: Request, anchor: str | None = None, provider: str = "",
                  dim: str = "model", period: str | None = None,
                  start: str | None = None, end: str | None = None,
                  notice: str | None = None):
    dim = dim if dim in DIM_COLUMNS else "model"
    provider = provider if provider in PROVIDERS else ""
    period = period if period in _PERIODS else "month"
    conn = connect()
    update_tag = check_update(conn)
    ctx = dimension_context(conn, _parse_anchor(anchor), provider, dim=dim,
                            period=period, start=start, end=end)
    return templates.TemplateResponse(
        request, "analysis.html",
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


def _official_section_response(request: Request, conn, config, now):
    """'AI별 사용량' 섹션 조각(partial) 렌더 — 수동 HX 갱신·자동 폴링 공용."""
    return templates.TemplateResponse(
        request, "_official_section.html",
        official_section_context(conn, config, now))


@app.post("/official/refresh")
def official_refresh(request: Request, provider: str = Form("")):
    """수동 갱신 — throttle을 건너뛴다(manual). provider 지정 시 그 카드만, 아니면 전체.

    HX 요청이면 'AI별 사용량' 섹션을 부분교체로 돌려주고(JS 개입), 아니면 전체 리로드 폴백(303).
    """
    conn = connect()
    config = load_config()
    now = datetime.now(KST)
    targets = [provider] if provider in PROVIDERS else None
    refresh_tracked(config, now_kst=now, conn=conn, manual=True, providers=targets)
    if request.headers.get("HX-Request"):
        return _official_section_response(request, conn, config, now)
    return RedirectResponse("/", status_code=303)


@app.get("/official/section")
def official_section(request: Request):
    """자동 폴링(hx-trigger load·every) — 자동 갱신(throttle 적용) 후 섹션 조각 렌더."""
    conn = connect()
    config = load_config()
    now = datetime.now(KST)
    refresh_tracked(config, now_kst=now, conn=conn, manual=False)
    return _official_section_response(request, conn, config, now)


@app.get("/mini")
def mini_view(request: Request):
    """미니 뷰(상주 동반 글랜스 창, ADR 0008) 셸 — 사이드바 없는 독립 페이지.

    활성 AI별 압축 게이지 행(official-only). 갱신은 셸 안의 #mini-section이
    hx-trigger="load, every Nm"로 /mini/section을 자체 폴링한다(수집과 무관).
    """
    conn = connect()
    ctx = mini_view_context(conn, load_config(), datetime.now(KST))
    return templates.TemplateResponse(request, "mini.html", ctx)


@app.get("/mini/section")
def mini_section(request: Request):
    """미니 뷰 자동 폴링 — 자동 갱신(manual=False, throttle 적용) 후 압축 조각 렌더."""
    conn = connect()
    config = load_config()
    now = datetime.now(KST)
    refresh_tracked(config, now_kst=now, conn=conn, manual=False)
    return templates.TemplateResponse(
        request, "_mini_section.html", mini_view_context(conn, config, now))


@app.get("/settings")
def settings_get(request: Request, saved: int = 0):
    config = load_config()
    conn = connect()
    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    pricing = apply_pricing_overrides(load_pricing(), config.get("pricing_overrides"))
    ofs = official_fetch_settings(config)
    tracked = tracked_providers(config)
    return templates.TemplateResponse(
        request, "settings.html",
        {"tracked": tracked, "providers": list(PROVIDERS),
         "credit_to_usd": _credit_to_usd(config),
         "account_mode": account_mode(config),   # 계정 형태 토글 현재값(None=자동)
         "rate_window_weeks": forecast_settings(config)["rate_window_weeks"],
         "official_fetch": ofs,
         "provider_toggles": settings_provider_toggles(config),
         "saved": bool(saved),
         "active_nav": "settings", "update_tag": check_update(conn),
         "last_ts": last["t"] if last and last["t"] else None,
         "last_ingest_at": sidebar_freshness(conn),
         **coverage_card_context(conn, pricing)},
    )


def _to_float(value: str | None) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0
    except ValueError:
        return 0.0


@app.post("/settings")
async def settings_post(request: Request):
    # 동적 파싱 — track_<provider> 체크박스를 PROVIDERS 순회로 수집(claude/codex 하드코딩 제거,
    # 3번째 AI 추가 시 폼·파서 무수정). 전부 미체크 → 빈 집합 영속(Commit 1이 재시드 차단).
    form = await request.form()
    config = load_config()
    sel = [p for p in PROVIDERS if form.get(f"track_{p}")]
    config["tracked_providers"] = sel
    ctu = _to_float(form.get("credit_to_usd"))
    config["credit_to_usd"] = ctu if ctu > 0 else 0.04
    mi = int(_to_float(form.get("min_interval")))
    # background_poll: 체크박스(미체크면 폼에 키 부재 → False). 상주 백그라운드 폴 토글(ADR 0007).
    config["official_fetch"] = {"min_interval_minutes": mi if mi > 0 else 10,
                                "background_poll": bool(form.get("background_poll"))}
    # 소비속도 추정 기간(트레일링 창, 주) — getter로 정규화(1~8 clamp·오설정→기본 2) 후 저장.
    # 클램프 범위를 getter 단일 출처에 두려고 직접 min/max를 두지 않는다.
    rw = forecast_settings({"forecast_settings": {"rate_window_weeks": form.get("rate_window_weeks")}})
    config["forecast_settings"] = rw
    # 계정 형태(ADR 0015): enterprise|subscription 명시 저장(sticky). 빈 값="자동" → None으로
    # 비워 다음 공식 취득 때 데이터로 재시드되게 한다(seed_account_mode는 None일 때만 시드).
    mode = form.get("account_mode")
    config["account_mode"] = mode if mode in ACCOUNT_MODES else None
    # 레거시 키 정리(있으면 제거 — config를 깔끔하게 다시 쓴다)
    for k in ("budget", "budget_start"):
        config.pop(k, None)
    save_config(config)
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.get("/app/ping")
def app_ping():
    """단일 인스턴스 정체 확인 마커 — 런처가 기존 인스턴스인지 판별할 때 GET."""
    return {"app": "tokenomy"}


@app.post("/app/show")
def app_show():
    """재실행된 인스턴스가 보낸 창 복원 신호 — 등록된 콜백(_show_window) 호출."""
    control.request_show()
    return {"ok": True}


