"""DB → 화면용 dict 조립. 라우트(app.py)와 집계(aggregate.py)를 분리한다."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from tokenomy.aggregate import (
    KST, DIM_COLUMNS, PROVIDERS, DateGroup, DaySessionRow, FolderGroup,
    _provider_where, by_day_session, by_dimension, by_project, by_session,
    combined_forecast, daily_series, pool_history, pool_daily_history, pool_snapshots_by_day,
    official_period_glance,
    insights, month_bounds, month_spend, official_view, parse_ts, period_bounds,
    pricing_coverage, session_detail, sidechain_split, stacked_trend,
    token_composition,
)
from tokenomy.config import (
    credit_to_usd, forecast_settings, load_config, official_fetch_settings, tracked_providers, user_label,
)
from tokenomy.db import get_fetch_state, get_meta
from tokenomy.freshness import LAST_INGEST_KEY
from tokenomy.pricing import apply_pricing_overrides, load_pricing

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


def _remediation(provider: str, status: str | None) -> str | None:
    """fetch 상태 코드에 따른 사용자 안내 문자열을 반환한다. 정상/없음이면 None."""
    if status == "auth_error":
        return ("Codex CLI를 1회 실행해 토큰을 갱신하세요"
                if provider == "codex" else "재로그인이 필요합니다")
    if status == "http_error":
        return "취득 실패 — 잠시 후 다시 시도하세요"
    return None


def _provider_has_data(conn, provider: str) -> bool:
    row = conn.execute(
        "SELECT MAX(ts) t FROM messages WHERE provider=?", (provider,)
    ).fetchone()
    return row is not None and row["t"] is not None


def _filter_options(active: list[str]) -> list[dict]:
    """화면별 provider 필터(전체/<AI>) 항목. 활성 집합에서 파생 — claude/codex 하드코딩 없음."""
    return [{"key": p, "label": _PROVIDER_META.get(p, {}).get("label", p.title())} for p in active]


def _active_provider(provider: str, active: list[str]) -> str:
    """요청 provider를 활성 집합으로 검증. 활성에 없으면 ""(전체)로 폴백."""
    return provider if provider in active else ""


def sidebar_freshness(conn) -> str | None:
    """사이드바 '수집: N분 전'용 마지막 수집 실행 시각(ISO). 없으면 None.

    최신 메시지 ts(MAX(ts))가 아니라 record_ingest가 남긴 시각이다 — 갱신 카드의
    '마지막 갱신 시각'과 대칭을 맞춰, '방금 수집했다'가 정확히 반영되게 한다.
    """
    return get_meta(conn, LAST_INGEST_KEY)


# ── 공식 사용량 provider 카드 조립(ADR 0002) ───────────────────────────────────
# provider 액센트 색은 _TREND_STYLE과 동일 팔레트(추세 범례와 일관). 카드 크롬에만 쓰고
# 게이지 fill엔 절대 쓰지 않는다 — fill 색은 임계(utilization) 전용.
_PROVIDER_META = {
    "claude": {"label": "Claude", "accent": "#cc785c"},   # 코랄(brand)
    "codex": {"label": "Codex", "accent": "#5db8a6"},      # teal(상태색 회피)
}


def _forecast_hero(fc) -> dict | None:
    """CombinedForecast → 최상단 히어로 표시 dict. fc가 None이면 None(히어로 숨김).

    level: surplus(여유) / shortfall(부족, 소진예상일) / exhausted(이미 소진) / insufficient(추세 데이터 부족).
    """
    if fc is None:
        return None
    labels = " + ".join(
        _PROVIDER_META.get(p, {"label": p.title()})["label"] for p in fc.providers
    )
    base = {
        "providers_label": labels,
        "used": fc.used_usd, "limit": fc.limit_usd, "remaining": fc.remaining_usd,
        "daily_rate": fc.daily_rate_usd,
        "pct_now": round(fc.used_usd / fc.limit_usd * 100) if fc.limit_usd else 0,
        "exhaust_date": fc.exhaust_date.strftime("%m-%d") if fc.exhaust_date else None,
    }
    if fc.is_exhausted:
        base["level"] = "exhausted"
    elif fc.daily_rate_usd is None:
        base["level"] = "insufficient"
    elif fc.projected_remaining_usd is not None and fc.projected_remaining_usd < 0:
        base["level"] = "shortfall"
        base["shortfall_abs"] = round(-fc.projected_remaining_usd, 2)
    else:
        base["level"] = "surplus"
        base["surplus"] = round(fc.projected_remaining_usd, 2)
    return base


def forecast_chart_data(fc, daily, now_kst: datetime) -> dict:
    """통합 추세 차트용 — 통합 한도(수평선) + 월말까지 예상 used 라인.

    line은 daily와 같은 길이. 오늘 이전은 None, 오늘 인덱스는 공식 used로 앵커하고
    이후 calendar day를 돌며 영업일마다 daily_rate씩 누적한다(주말엔 그대로). 그래서 라인이
    한도선과 만나는 지점이 히어로의 소진 예상일과 일치한다. fc 없음/rate 없음/소진이면 line은 None.
    """
    if fc is None:
        return {"limit": None, "line": None}
    if fc.daily_rate_usd is None or fc.is_exhausted:
        return {"limit": fc.limit_usd, "line": None}
    n = len(daily)
    line = [None] * n
    today_idx = now_kst.day - 1
    if 0 <= today_idx < n:
        line[today_idx] = fc.used_usd
        bd = 0
        for i in range(today_idx + 1, n):
            if date(now_kst.year, now_kst.month, daily[i].day).weekday() < 5:
                bd += 1
            line[i] = round(fc.used_usd + fc.daily_rate_usd * bd, 4)
    return {"limit": fc.limit_usd, "line": line}


def pool_history_to_daily(segments, days, now_kst: datetime) -> list:
    """통합 풀 과거 세그먼트(aggregate.pool_history) → days 정렬 per-day used 배열(전망 차트 실선용, ADR 0007).

    각 날(현재 월·KST)의 마지막 스냅샷 used_usd를 해당 day 인덱스에 넣는다. 데이터 없는 날은
    None(차트가 spanGaps:false로 잇지 않음). 다른 월·미래 점은 무시한다. 세그먼트 경계(리셋/갭)는
    day 해상도로 환원되며 데이터 없는 날의 None으로 끊긴다(월 경계 리셋은 대개 차트 범위 밖).
    """
    idx_of = {d: i for i, d in enumerate(days)}
    out: list = [None] * len(days)
    for seg in segments:
        for p in seg:
            dt = parse_ts(p["ts"])
            if dt is None:
                continue
            dt = dt.astimezone(KST)
            if dt.year == now_kst.year and dt.month == now_kst.month:
                i = idx_of.get(dt.day)
                if i is not None:
                    out[i] = p["used_usd"]
    return out


def _gauge_level(util: float | None) -> str:
    """utilization(%) → 임계 클래스. 100%가 사실상 상한이라 경계를 당겨 적색을 살린다.

    <75 ok · 75~90 warn · ≥90 exceeds. (ADR 0002)
    """
    u = util or 0.0
    if u >= 90:
        return "exceeds"
    if u >= 75:
        return "warn"
    return "ok"


def _fresh_label(stale_minutes: int | None) -> str | None:
    """취득 신선도 문자열. None이면 표시하지 않는다."""
    if stale_minutes is None:
        return None
    if stale_minutes < 1:
        return "방금"
    if stale_minutes < 60:
        return f"{stale_minutes}분 전"
    if stale_minutes < 1440:
        return f"{stale_minutes // 60}시간 전"
    return f"{stale_minutes // 1440}일 전"


def _sparkline_points(series, w: float = 120.0, h: float = 28.0) -> str | None:
    """일별 누적 시계열(DayPoint) → SVG polyline points. 유효 점 2개 미만이면 None."""
    vals = [p.cumulative_cost for p in series if p.cumulative_cost is not None]
    if len(vals) < 2:
        return None
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    pad = 2.0
    iw, ih = w - 2 * pad, h - 2 * pad
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = pad + (i / (n - 1)) * iw
        y = pad + ih - ((v - lo) / span) * ih
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def _gauge_caption(used_usd: float | None, limit_usd: float | None, *,
                   used_native: float | None = None, limit_native: float | None = None,
                   native_unit: str = "usd") -> str | None:
    """USD 한도가 있으면 '$used / $limit'. 없으면(rate-window %) None(게이지 %가 대신 말함).

    credit 버킷(Codex)은 USD가 환산값이라 원본 크레딧을 괄호로 병기한다
    ('$42.96 / $235 (크레딧 1,074 / 5,875)'). 환산 단가가 바뀌어도 원본이 정본임을 드러낸다.
    """
    if used_usd is None or not limit_usd:
        return None
    cap = f"${used_usd:,.2f} / ${limit_usd:,.0f}"
    if native_unit == "credit" and used_native is not None and limit_native:
        cap += f" (크레딧 {used_native:,.0f} / {limit_native:,.0f})"
    return cap


def _reset_with_countdown(resets_at: str, now_kst: datetime) -> str:
    """rate-limit 창(5시간·주간 등)의 리셋을 'KST 분 단위 시각 + 잔여 시간'으로 표기.

    예: '리셋 2026-06-21 11:20 · 2시간 35분 후', '리셋 2026-06-25 09:00 · 3일 23시간 후'.
    rate 창은 날짜만 보여주면 정보가 빈약하므로 분까지 찍고, 남은 시간을 일/시/분으로 병기한다.
    잔여가 하루 이상이면 분은 노이즈라 일·시까지만, 하루 미만이면 시·분으로 좁혀 보여준다.
    resets_at(UTC ISO)은 parse_ts로 KST 변환. 파싱 실패/이미 지난 경우는 시각만 보여준다.
    """
    dt = parse_ts(resets_at)
    if dt is None:
        return f"리셋 {resets_at[:16].replace('T', ' ')}"
    stamp = dt.strftime("%Y-%m-%d %H:%M")
    mins = int((dt - now_kst).total_seconds() // 60)
    if mins <= 0:
        return f"리셋 {stamp}"
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    if d:
        rel = f"{d}일 {h}시간 후" if h else f"{d}일 후"
    elif h:
        rel = f"{h}시간 {m}분 후" if m else f"{h}시간 후"
    else:
        rel = f"{m}분 후"
    return f"리셋 {stamp} · {rel}"


def _reset_remaining_coarse(resets_at: str, now_kst: datetime) -> str | None:
    """rate-limit 창 리셋까지 남은 시간을 **최대 단위 1개**로(미니 글랜스용).

    "3일"·"2시간"·"40분"·"곧"(1분 미만). floor라 표시값보다 실제 잔여가 더 길 수 있다
    (한 줄 공간에 맞춘 거친 카운트다운 — 정밀 시각·잔여는 큰 창의 _reset_with_countdown).
    파싱 실패면 None(만료 버킷은 호출 전에 이미 걸러진다).
    """
    dt = parse_ts(resets_at)
    if dt is None:
        return None
    mins = int((dt - now_kst).total_seconds() // 60)
    if mins <= 0:
        return "곧"
    if mins < 60:
        return f"{mins}분"
    if mins < 1440:
        return f"{mins // 60}시간"
    return f"{mins // 1440}일"


def _bucket_gauge(b: dict, view, now_kst: datetime) -> dict:
    """공식 버킷 dict → 게이지 표시 모델. active 버킷이면 렌즈로 고스트(예측) 채움."""
    util = b["utilization"] or 0.0
    used, limit = b["used_usd"], b["limit_usd"]
    # 리셋/만료 날짜는 모두 sub 한 자리에 표시(정렬 일관). event_credit은 '만료', 그 외는 '리셋'.
    # rate 창(5시간·주간 등)은 날짜만으론 무의미 — 분 단위 시각 + 잔여 시간을 병기한다.
    sub = None
    # reset_in = 미니 전용 거친 카운트다운(rate_window만 — 월간·이벤트는 None).
    reset_in = None
    if b["resets_at"]:
        if b["bucket_kind"] == "rate_window":
            sub = _reset_with_countdown(b["resets_at"], now_kst)
            reset_in = _reset_remaining_coarse(b["resets_at"], now_kst)
        else:
            d = b["resets_at"][:10]
            sub = f"만료 {d}" if b["bucket_kind"] == "event_credit" else f"리셋 {d}"

    # 고스트(예측 렌즈) — active 버킷 + 렌즈 있을 때만. 현재→리셋시 예상 위치를 옅게 연장.
    # 고스트는 색만으론 의미가 안 와닿으므로 forecast 텍스트를 함께 단다("이 속도면…").
    ghost_pct = None
    ghost_warn = False
    forecast = None
    lens = view.lens
    if (lens and lens.daily_rate_usd and limit and used is not None
            and b["bucket_key"] == view.active_key):
        days = lens.days_left_to_reset or 0
        projected = used + lens.daily_rate_usd * days
        proj_util = projected / limit * 100 if limit else 0.0
        if proj_util > util:
            ghost_pct = round(min(proj_util, 100), 1)
            ghost_warn = bool(lens.dday_warning) or proj_util >= 90
            if lens.exhaust_date and lens.dday_warning:
                forecast = f"⚠ {lens.exhaust_date:%m-%d} 소진 예상"
            else:
                surplus = round(limit - projected, 2)
                forecast = f"이 속도면 리셋 시 ${surplus:,.0f} 여유"

    return {
        "label": b["label"],
        "util": round(util),
        "fill_pct": round(min(util, 100), 1),
        "level": _gauge_level(util),
        "estimated": False,
        "caption": _gauge_caption(used, limit, used_native=b["used_native"],
                                  limit_native=b["limit_native"], native_unit=b["native_unit"]),
        "sub": sub,
        "reset_in": reset_in,
        "ghost_pct": ghost_pct,
        "ghost_warn": ghost_warn,
        "forecast": forecast,
        "exhausted": util >= 100,
    }


def _provider_card(conn, provider: str, view, fetch_state, now_kst: datetime) -> dict:
    """OfficialView + fetch 상태 → 카드 1개 표시 모델.

    공식 버킷이 있으면 게이지(만료 버킷 제외)를 보여준다. fetch 실패면 스탈+경고.
    공식 데이터가 전혀 없으면 사용량 전용 폴백(로컬 추정 + 스파크라인).
    """
    meta = _PROVIDER_META.get(provider, {"label": provider.title(), "accent": "#6c6a64"})
    fs_status = fetch_state["last_status"] if fetch_state else None
    note = _remediation(provider, fs_status)
    has_official = view.status == "ok" and bool(view.buckets)

    gauges: list[dict] = []
    fallback = None
    if has_official:
        for b in view.buckets:
            # 만료(resets_at 과거) 버킷은 더 이상 actionable이 아니므로 숨긴다.
            r = parse_ts(b["resets_at"]) if b["resets_at"] else None
            if r is not None and r < now_kst:
                continue
            gauges.append(_bucket_gauge(b, view, now_kst))
        status = "error" if fs_status in ("auth_error", "http_error") else "ok"
    else:
        status = "no_data"
        fallback = {
            "estimate_usd": month_spend(conn, provider, now_kst),
            "spark": _sparkline_points(daily_series(conn, provider, now_kst)),
        }

    # 공식 기간 소비 글랜스(ADR 0011) — USD 풀 있는 provider만(스코프 게이트).
    # rate-window-only(개인 구독제)·공식 미취득은 pool_limit_usd None → 글랜스 줄 숨김.
    glance = official_period_glance(conn, provider, now_kst) if view.pool_limit_usd is not None else None

    return {
        "provider": provider,
        "label": meta["label"],
        "accent": meta["accent"],
        "status": status,
        "fresh": _fresh_label(view.stale_minutes),   # JS 미실행 시 서버 초기 텍스트(폴백)
        "fetched_at": view.fetched_at,                # 절대 ISO — JS rel-time tick의 기준
        "note": note,
        "gauges": gauges,
        "fallback": fallback,
        "glance": glance,
    }


def official_cards(conn, config: dict, now_kst: datetime | None = None) -> list[dict]:
    """대시보드 공식 사용량 그리드용 provider 카드 리스트.

    표시 대상 = **활성 AI**(tracked_providers)뿐. 끈 provider는 공식 스냅샷·로컬 데이터가
    있어도 카드를 띄우지 않는다(데이터는 보존, 표시만 숨김 — ADR 0005). 활성 0개면 빈 리스트.
    순서는 PROVIDERS 고정(claude→codex→…). 새 provider는 PROVIDERS·_PROVIDER_META만 늘리면 된다.
    """
    now = now_kst or datetime.now(KST)
    ctu = credit_to_usd(config)
    weeks = forecast_settings(config)["rate_window_weeks"]
    active = set(tracked_providers(config))
    cards: list[dict] = []
    for p in PROVIDERS:
        if p not in active:
            continue
        view = official_view(conn, p, now, ctu, weeks)
        cards.append(_provider_card(conn, p, view, get_fetch_state(conn, p), now))
    return cards


def official_section_context(conn, config: dict, now_kst: datetime | None = None) -> dict:
    """'AI별 사용량' 섹션 조각(partial)용 컨텍스트 — provider 카드 + 자동 갱신 간격(분).

    수동 HX 갱신(POST /official/refresh)과 자동 폴링(GET /official/section)이 공유한다.
    """
    now = now_kst or datetime.now(KST)
    return {
        "official_cards": official_cards(conn, config, now),
        "official_interval": official_fetch_settings(config)["min_interval_minutes"],
    }


def mini_view_context(conn, config: dict, now_kst: datetime | None = None) -> dict:
    """미니 뷰(상주 동반 글랜스 창, ADR 0008)용 컨텍스트 — 활성 AI별 압축 게이지 행.

    official_cards를 재사용하되 **official-only 불변식**을 강제한다: 공식 데이터가 없는
    카드(status no_data)는 큰 창처럼 로컬 추정으로 폴백하지 않고 no_official 플래그만 둔다
    (미니 뷰는 ingest와 무관 — 잔여 면에 한도 맥락 없는 로컬 추정은 부적합).
    provider당 모든 게이지를 그대로 노출한다(Codex 월간+주간, Claude 5h+7d 등).
    """
    now = now_kst or datetime.now(KST)
    cards = []
    for c in official_cards(conn, config, now):
        no_official = c["status"] == "no_data"
        cards.append({
            "provider": c["provider"], "label": c["label"], "accent": c["accent"],
            "status": c["status"], "no_official": no_official,
            "fresh": c["fresh"], "fetched_at": c["fetched_at"], "note": c["note"],
            "gauges": [] if no_official else c["gauges"],   # official-only: 폴백 게이지 없음
            "glance": c["glance"],                          # 공식 기간 소비(ADR 0011), official-only
        })
    return {
        "cards": cards,
        "interval": official_fetch_settings(config)["min_interval_minutes"],
    }


def overview_context(conn, sort: str, now_kst: datetime | None = None) -> dict:
    now = now_kst or datetime.now(KST)
    config = load_config()
    # 활성 AI(ADR 0005) — 화면의 "전체"는 곧 활성 합산이다(DB 전체가 아니다).
    active = tracked_providers(config)

    month_total = month_spend(conn, None, now, providers=active)

    projects = by_project(conn, None, now, providers=active)
    projects.sort(key=_SORT_KEYS.get(sort, _SORT_KEYS["cost"]), reverse=True)
    projects = projects[:10]
    sessions = by_session(conn, None, now, limit_n=10, providers=active)

    pricing = apply_pricing_overrides(load_pricing(), config.get("pricing_overrides"))
    cov = pricing_coverage(conn, pricing, providers=active)
    coach = insights(conn, now, None, cov=cov, providers=active)
    daily = daily_series(conn, None, now, providers=active)
    # 통합 월말 전망 — 활성(ADR 0005) provider의 공식 뷰로 통합 풀 구성.
    # 화면의 "전체"가 곧 활성 합산이므로 forecast 풀도 활성만 본다(combined_forecast가 한도 보유분만 필터).
    ctu = credit_to_usd(config)
    weeks = forecast_settings(config)["rate_window_weeks"]
    forecast_views = [official_view(conn, p, now, ctu, weeks) for p in active]
    forecast_obj = combined_forecast(conn, forecast_views, now, weeks)
    forecast = _forecast_hero(forecast_obj)
    fc_chart = forecast_chart_data(forecast_obj, daily, now)
    # 공식 사용량 스냅샷 이력 → 전망 차트 실제 과거 실선(ADR 0007). 활성 USD 풀만.
    # max_gap = 자동 갱신 간격 ×3(그보다 긴 공백은 수집 단절로 보아 선을 끊는다).
    interval = official_fetch_settings(config)["min_interval_minutes"]
    pool_hist = pool_history(conn, active, max_gap_minutes=interval * 3)
    forecast_actual = pool_history_to_daily(pool_hist, [p.day for p in daily], now)

    # 추세 밴드 = 활성 ∩ 데이터 있는 provider. stacked_trend·차트 JS는 무변경(N밴드 generic).
    trend_providers = [p for p in _TREND_STYLE if p in active and _provider_has_data(conn, p)]
    bands = stacked_trend(
        [(p, daily_series(conn, p, now)) for p in trend_providers]
    )
    trend_series = [
        {"label": _TREND_STYLE[b["provider"]][0],
         "color": _TREND_STYLE[b["provider"]][1],
         "fill": _TREND_STYLE[b["provider"]][2],
         "top": b["top"], "cum": b["cum"]}
        for b in bands
    ]
    trend_totals = bands[-1]["top"] if bands else [None for _ in daily]

    # has_data·last_ts도 활성 기준 — 끈 provider만 데이터 있으면 빈 상태로 안내(일관).
    where, params = _provider_where(None, active)
    last = conn.execute("SELECT MAX(ts) t FROM messages" + where, params).fetchone()
    has_data = last is not None and last["t"] is not None
    token_comp = token_composition(conn, None, *month_bounds(now), providers=active)

    # 라벨 적응(ADR 0005): 활성 ≥2면 "통합/전 AI 합산", 1개면 수식어 떼고 provider명.
    combined = len(active) >= 2
    solo_label = (_PROVIDER_META.get(active[0], {}).get("label", active[0].title())
                  if len(active) == 1 else None)

    return {
        "active_nav": "dashboard", "sort": sort,
        "user_label": user_label(config),
        "tracked": active,
        "combined": combined, "solo_label": solo_label, "active_empty": not active,
        "month": now.strftime("%Y-%m"),
        "month_total": month_total,
        "forecast": forecast,
        "forecast_limit": fc_chart["limit"],
        "forecast_line": fc_chart["line"],
        "forecast_actual": forecast_actual,
        "official_cards": official_cards(conn, config, now),
        "official_interval": official_fetch_settings(config)["min_interval_minutes"],
        "projects": projects, "sessions": sessions, "insights": coach,
        "daily_labels": [p.day for p in daily],
        "trend_series": trend_series,
        "trend_totals": trend_totals,
        "last_ts": last["t"] if has_data else None,
        "last_ingest_at": sidebar_freshness(conn),
        "token_comp": token_comp,
        "has_data": has_data,
    }


def session_context(conn, session_id: str) -> dict | None:
    detail = session_detail(conn, session_id)
    if detail is None:
        return None
    return {"detail": detail, "active_nav": "history",
            "last_ingest_at": sidebar_freshness(conn)}


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
    active = tracked_providers(config)
    provider = _active_provider(provider, active)   # 비활성 provider 요청 → 전체 폴백
    s, nxt, label, period, custom = _resolve_range(anchor_kst, period, start, end)
    pfilter = None if provider else active          # "" → 활성 합산, 단일 → 그 provider
    rows = by_dimension(conn, provider or None, s, nxt, dim, providers=pfilter)
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
    split = sidechain_split(conn, provider or None, s, nxt, providers=pfilter)
    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    return {
        "active_nav": "analysis", "user_label": user_label(config),
        "provider": provider, "dim": dim, "dim_label": DIM_LABELS[dim],
        "filter_providers": _filter_options(active), "show_filter": len(active) >= 2,
        "active_empty": not active,
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
        "last_ingest_at": sidebar_freshness(conn),
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


def _human_tokens(n: int) -> str:
    """토큰 수를 K/M/B 단위 문자열로(예: 1_500_000 → '1.5M')."""
    for unit, div in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(n)


def _share_pct(x: float) -> str:
    """비중(0~1)을 퍼센트 문자열로. 0 초과 1% 미만은 '<1%'."""
    p = x * 100
    return "<1%" if 0 < p < 1 else f"{p:.0f}%"


def settings_provider_toggles(config: dict) -> list[dict]:
    """설정 '활성 AI' 카드 토글 표시모델 — provider별 {key, label, on}.

    on = 활성(tracked_providers) 포함 여부. **fetch 상태칩·신선도·remediation은 두지 않는다** —
    그 상태는 대시보드 'AI별 사용량' 카드가 이미 보여주므로(ADR 0005) 설정은 구성(on/off)만 담당한다.
    PROVIDERS 순회 — claude/codex 하드코딩 없음(3번째 AI 확장 대비).
    """
    active = set(tracked_providers(config))
    return [{"key": p, "label": _PROVIDER_META.get(p, {}).get("label", p.title()),
             "on": p in active} for p in PROVIDERS]


def coverage_card_context(conn, pricing: dict) -> dict:
    """settings 단가 커버리지 카드용 컨텍스트.

    pricing 항목(match[]) 기준 역방향 그룹핑(항목 → 매칭 모델들) + 미식별 별도 묶음.
    거친 매칭은 한 그룹에 모델이 여러 행으로 나타나 자연히 드러난다.
    pricing은 호출부(settings_get)가 overrides 적용해 주입한다(테스트 격리 용이).
    표시·집계는 실제 사용(토큰>0) 모델만 본다 — synthetic·phantom 같은 0토큰 노이즈는 숨겨
    건강 상태 한 줄과 펼친 표가 어긋나지 않게 한다.
    """
    cov = pricing_coverage(conn, pricing)
    used = [m for m in cov.models if m.tokens > 0]

    def _row(m):
        return {"model": m.model, "status": m.status,
                "tokens_h": _human_tokens(m.tokens), "share": _share_pct(m.token_share)}

    order = [e.get("contains") for e in pricing.get("match", [])]
    grouped: dict[str, list] = {}
    for m in used:
        if m.matched_contains is not None:
            grouped.setdefault(m.matched_contains, []).append(m)
    groups = []
    for contains in order:
        ms = grouped.get(contains)
        if not ms:
            continue
        rate = next((e for e in pricing["match"] if e.get("contains") == contains), {})
        groups.append({
            "contains": contains,
            "rate": f"${rate.get('input', 0):g}/${rate.get('output', 0):g}",
            "rows": [_row(m) for m in ms],
        })

    unpriced_rows = [_row(m) for m in used if m.status == "unpriced"]
    suspects = [m.model for m in used if m.status == "suspect"]
    n_unpriced = sum(1 for m in used if m.status == "unpriced")
    n_suspect = sum(1 for m in used if m.status == "suspect")

    if n_unpriced:
        status = ("warn", f"미식별 {n_unpriced}종")
    elif n_suspect:
        status = ("info", f"확인 필요 {n_suspect}종")
    else:
        status = ("ok", "모든 모델 단가 식별됨")

    return {
        "coverage_groups": groups,
        "coverage_unpriced": unpriced_rows,
        "coverage_suspects": suspects,
        "coverage_status": status,   # (level, label)
        "coverage_has_detail": bool(groups or unpriced_rows),
    }


def history_context(conn, anchor_kst: datetime, provider: str, sort: str,
                    now_kst: datetime | None = None, *,
                    period: str = "month", start: str | None = None,
                    end: str | None = None) -> dict:
    """내역 — 날짜→폴더→세션 트리. 주/월 기간 또는 사용자 지정 [start, end]."""
    now = now_kst or datetime.now(KST)
    config = load_config()
    active = tracked_providers(config)
    provider = _active_provider(provider, active)   # 비활성 provider 요청 → 전체 폴백
    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    s, nxt, label, period, custom = _resolve_range(anchor_kst, period, start, end)
    # provider 지정 → 단일, "" → 활성 합산(providers=active)
    rows = by_day_session(conn, provider or None, start=s, nxt=nxt,
                          providers=(None if provider else active))
    tree = build_date_tree(rows, sort)
    return {
        "active_nav": "history",
        "user_label": user_label(config),
        "provider": provider, "sort": sort,
        "filter_providers": _filter_options(active), "show_filter": len(active) >= 2,
        "active_empty": not active,
        "period": period, "custom": custom,
        "anchor": anchor_kst.strftime("%Y-%m-%d"),
        "start": start or "", "end": end or "",
        "month": now.strftime("%Y-%m"),
        "last_ts": last["t"] if last and last["t"] else None,
        "last_ingest_at": sidebar_freshness(conn),
        "period_label": label,
        "prev_anchor": (s - timedelta(days=1)).strftime("%Y-%m-%d"),
        "next_anchor": nxt.strftime("%Y-%m-%d"),
        "has_next": nxt <= now,
        "tree": tree,
        "count": len(rows),
        "total": round(sum(r.cost for r in rows), 4),
    }


def official_history_context(conn, anchor_kst: datetime, provider: str = "", *,
                             period: str = "month", start: str | None = None,
                             end: str | None = None, now_kst: datetime | None = None) -> dict:
    """공식 사용 이력(ADR 0010) — 통합 풀의 누적 선 + 일별 소비 막대 + 일별 표.

    데이터는 공식 사용량 스냅샷 이력(ADR 0007)의 새 표현 — 신규 취득 없음. 소진형
    풀만(rate-window 제외) 보여주며, 소진형 풀이 없으면 has_pool=False(빈 상태).
    """
    now = now_kst or datetime.now(KST)
    config = load_config()
    active = tracked_providers(config)
    provider = _active_provider(provider, active)   # 비활성 provider 요청 → 전체 폴백
    s, nxt, label, period, custom = _resolve_range(anchor_kst, period, start, end)

    # 소진형 풀 판정 + 한도선 값 — combined_forecast가 None이면 소진형 풀 없음(빈 상태).
    ctu = credit_to_usd(config)
    weeks = forecast_settings(config)["rate_window_weeks"]
    fobj = combined_forecast(conn, [official_view(conn, p, now, ctu, weeks) for p in active], now, weeks)
    has_pool = fobj is not None
    pool_limit = fobj.limit_usd if fobj else None
    pool_providers = fobj.providers if fobj else active
    view_providers = [provider] if provider else pool_providers

    interval = official_fetch_settings(config)["min_interval_minutes"]
    # provider 순서 = _TREND_STYLE(스택/막대/드릴다운 일관) 우선, 나머지는 뒤로.
    prov_list = ([p for p in _TREND_STYLE if p in view_providers]
                 + [p for p in view_providers if p not in _TREND_STYLE])
    segments = pool_history(conn, view_providers, max_gap_minutes=interval * 3)
    daily = pool_daily_history(conn, view_providers, start=s, nxt=nxt)
    detail_by_day = pool_snapshots_by_day(conn, prov_list, start=s, nxt=nxt)   # 일 소비 재구성(드릴다운)
    prov_label = {p: (_TREND_STYLE[p][0] if p in _TREND_STYLE else p.title()) for p in prov_list}
    for dets in detail_by_day.values():       # provider 라벨 주입(템플릿 단순화)
        for pd in dets:
            pd["label"] = prov_label.get(pd["provider"], pd["provider"])

    # 말일 누적/잔여 = 그 날 끝 시점의 통합 풀 used(누적 선과 동일 출처)·한도 잔여(표 컬럼).
    end_cum: dict = {}
    for seg in segments:
        for pt in seg:
            dt = parse_ts(pt["ts"])
            if dt is not None:
                end_cum[dt.astimezone(KST).date()] = pt["used_usd"]   # 오름차순이라 last-wins
    table = []
    for r in daily:
        cum = end_cum.get(r["date"])
        table.append({**r, "ymd": r["date"].strftime("%Y-%m-%d"), "md": r["date"].strftime("%m-%d"),
                      "end_cumulative_usd": cum,
                      "remaining_usd": (round(pool_limit - cum, 4) if (pool_limit and cum is not None) else None),
                      "detail": detail_by_day.get(r["date"])})   # 펼침 시 스냅샷 재구성(없으면 None)

    # 차트용 배열 — 2단 패널(상: 누적 선, 하: provider 스택 막대)이 같은 날짜축 공유.
    chart_labels = [r["date"].strftime("%m-%d") for r in daily]
    cum_data = [end_cum.get(r["date"]) for r in daily]   # 누적 선(갱신 없는 날 None=끊김)
    bar_series = [{
        "key": p,
        "label": _TREND_STYLE[p][0] if p in _TREND_STYLE else p.title(),
        "color": _TREND_STYLE[p][1] if p in _TREND_STYLE else "#8a8a8a",
        "data": [r["per_provider"].get(p) for r in daily],   # provider 일별 소비(미커버 None)
    } for p in prov_list]

    return {
        "active_nav": "official_history",
        "user_label": user_label(config),
        "provider": provider,
        "filter_providers": _filter_options(active), "show_filter": len(active) >= 2,
        "active_empty": not active,
        "has_pool": has_pool, "pool_limit": pool_limit,
        "segments": segments, "daily": daily, "table": table,
        "chart_labels": chart_labels, "cum_data": cum_data, "bar_series": bar_series,
        "period": period, "custom": custom, "period_label": label,
        "anchor": anchor_kst.strftime("%Y-%m-%d"),
        "start": start or "", "end": end or "",
        "prev_anchor": (s - timedelta(days=1)).strftime("%Y-%m-%d"),
        "next_anchor": nxt.strftime("%Y-%m-%d"),
        "has_next": nxt <= now,
        "interval": interval,
        "last_ingest_at": sidebar_freshness(conn),
    }
