"""DB → 화면용 dict 조립. 라우트(app.py)와 집계(aggregate.py)를 분리한다."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from tokenomy.aggregate import (
    KST, DIM_COLUMNS, PROVIDERS, DateGroup, DaySessionRow, FolderGroup, PeriodSpend,
    last_message_ts, by_day_session, by_dimension, by_project, by_session,
    forecast_month_line, daily_series, pool_history, pool_daily_history, pool_hourly_history,
    pool_snapshots_by_day,
    official_period_glance,
    insights, month_bounds, month_spend, official_span_spend, official_view, parse_ts,
    period_bounds, pricing_coverage, range_spend, session_detail, sidechain_split, this_month_spend,
    stacked_trend, token_composition,
)
from tokenomy.config import (
    account_mode, bucket_curation_resolver, load_config,
    official_fetch_settings, onboarding_pending, tracked_providers, user_label,
)
from tokenomy.db import (
    get_fetch_state, get_meta, get_official_raw, list_official_raw, official_buckets_at,
)
from tokenomy.forecast import outlook, forecast_params
from tokenomy.freshness import LAST_INGEST_KEY
from tokenomy.pricing import apply_pricing_overrides, load_pricing
from tokenomy.web import control

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
    return last_message_ts(conn, provider) is not None


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
        # 헤드라인 숫자 = 이번달 흐름(위치=used와 분리, ADR 0024). partial이면 만료형 몫 미집계.
        "this_month_used": fc.this_month_used_usd,
        "this_month_partial": fc.this_month_partial,
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
    # 투영 라인은 aggregate.forecast_month_line 정본에서 읽어 daily 축(일 번호)에 매핑만 한다(순수 표현).
    # hero(combined_forecast)와 같은 walk라 라인이 한도선과 만나는 지점이 소진 예상일과 구성상 일치.
    day_used = forecast_month_line(fc.used_usd, fc.daily_rate_usd, now_kst)
    line = [day_used.get(d.day) for d in daily]
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
    cap = f"${used_usd:,.1f} / ${limit_usd:,.0f}"
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


def _curation_for(config: dict):
    """config(배포 카탈로그 + 로컬 오버라이드)에서 큐레이션 해석기와 풀 predicate를 빌드(ADR 0016).

    반환 (curation, is_pooled): curation은 `(provider,raw_key,bucket_kind) -> {hidden,pooled,label}`
    (표시용 hidden/label), is_pooled는 풀 멤버십 predicate(aggregate 풀 함수 주입용). 같은
    해석기에서 파생해 **표시·풀 일관**(pooled 불변식)을 보장한다.
    """
    curation = bucket_curation_resolver(config)

    def is_pooled(provider: str, raw_key: str, bucket_kind: str) -> bool:
        return curation(provider, raw_key, bucket_kind)["pooled"]
    return curation, is_pooled


def _bucket_gauge(b: dict, view, now_kst: datetime, *, curation=None) -> dict:
    """공식 버킷 dict → 게이지 표시 모델. active 버킷이면 렌즈로 고스트(예측) 채움.

    curation(ADR 0016) 있으면 라벨을 큐레이션 라벨로 교체(예: omelette_promotional → 'Claude Design').
    """
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

    label = b["label"]
    if curation is not None:
        label = curation(view.provider, b["raw_key"], b["bucket_kind"])["label"] or b["label"]

    return {
        "label": label,
        "raw_key": b["raw_key"],   # 디버그 발견 루프(ADR 0016 결정 6) — 디버그 모드 게이지에 코드네임 노출
        "util": round(util),
        "fill_pct": round(min(util, 100), 1),
        "level": _gauge_level(util),
        "caption": _gauge_caption(used, limit, used_native=b["used_native"],
                                  limit_native=b["limit_native"], native_unit=b["native_unit"]),
        "sub": sub,
        "reset_in": reset_in,
        "ghost_pct": ghost_pct,
        "ghost_warn": ghost_warn,
        "forecast": forecast,
        "exhausted": util >= 100,
    }


def _provider_card(conn, provider: str, view, fetch_state, now_kst: datetime,
                   *, curation=None, is_pooled=None) -> dict:
    """OfficialView + fetch 상태 → 카드 1개 표시 모델.

    공식 버킷이 있으면 게이지(만료·hidden 버킷 제외)를 보여준다. fetch 실패면 스탈+경고.
    공식 데이터가 전혀 없으면 로컬 추정으로 폴백하지 않고 깨끗한 no_data(공식만, ADR 0015 D8) —
    템플릿이 '공식 사용량 미취득' 빈 상태를 띄우고 헤더 ↻로 갱신을 유도한다. curation(ADR 0016)이
    hidden인 버킷(유령 천장 등)은 게이지에서 제외하고, 라벨은 _bucket_gauge가 큐레이션으로 교체한다.
    """
    meta = _PROVIDER_META.get(provider, {"label": provider.title(), "accent": "#6c6a64"})
    fs_status = fetch_state["last_status"] if fetch_state else None
    note = _remediation(provider, fs_status)
    has_official = view.status == "ok" and bool(view.buckets)

    gauges: list[dict] = []
    if has_official:
        for b in view.buckets:
            # 만료(resets_at 과거) 버킷은 더 이상 actionable이 아니므로 숨긴다.
            r = parse_ts(b["resets_at"]) if b["resets_at"] else None
            if r is not None and r < now_kst:
                continue
            # 큐레이션 hidden 버킷(유령 천장 등, ADR 0016)도 게이지에서 제외.
            if curation is not None and curation(provider, b["raw_key"], b["bucket_kind"])["hidden"]:
                continue
            gauges.append(_bucket_gauge(b, view, now_kst, curation=curation))
        status = "error" if fs_status in ("auth_error", "http_error") else "ok"
    else:
        status = "no_data"

    # 공식 기간 소비 글랜스(ADR 0011) — USD 풀 있는 provider만(스코프 게이트).
    # rate-window-only(개인 구독제)·공식 미취득은 pool_limit_usd None → 글랜스 줄 숨김.
    glance = (official_period_glance(conn, provider, now_kst, is_pooled=is_pooled)
              if view.pool_limit_usd is not None else None)

    return {
        "provider": provider,
        "label": meta["label"],
        "accent": meta["accent"],
        "status": status,
        "fresh": _fresh_label(view.stale_minutes),   # JS 미실행 시 서버 초기 텍스트(폴백)
        "fetched_at": view.fetched_at,                # 절대 ISO — JS rel-time tick의 기준
        "note": note,
        "gauges": gauges,
        "glance": glance,
    }


def official_cards(conn, config: dict, now_kst: datetime | None = None) -> list[dict]:
    """대시보드 공식 사용량 그리드용 provider 카드 리스트.

    표시 대상 = **활성 AI**(tracked_providers)뿐. 끈 provider는 공식 스냅샷·로컬 데이터가
    있어도 카드를 띄우지 않는다(데이터는 보존, 표시만 숨김 — ADR 0005). 활성 0개면 빈 리스트.
    순서는 PROVIDERS 고정(claude→codex→…). 새 provider는 PROVIDERS·_PROVIDER_META만 늘리면 된다.
    """
    now = now_kst or datetime.now(KST)
    fp = forecast_params(config)              # config 팬아웃 정본(ctu·weeks·max_gap·is_pooled)
    curation, _ = _curation_for(config)       # 표시용 hidden/label(숫자 팬아웃은 fp)
    active = set(fp.active)
    cards: list[dict] = []
    for p in PROVIDERS:
        if p not in active:
            continue
        view = official_view(conn, p, now, fp.ctu, fp.weeks, is_pooled=fp.is_pooled,
                             max_gap_minutes=fp.max_gap)
        cards.append(_provider_card(conn, p, view, get_fetch_state(conn, p), now,
                                    curation=curation, is_pooled=fp.is_pooled))
    return cards


# --- 공식 raw 디버그 페이지(official raw capture, ADR 0014) ---


def _pretty_json(text: str | None) -> str:
    """raw 텍스트를 보기 좋게 — JSON이면 indent=2, 아니면(에러 HTML 등) 원문 그대로."""
    if not text:
        return ""
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        return text


def official_raw_context(conn, config: dict, provider: str | None = None,
                         fetched_at: str | None = None) -> dict:
    """디버그 raw 페이지 데이터 — raw 원문 + 그 스냅샷의 파싱 버킷 + 메타 + 7일 피커.

    raw가 잡힌 provider만 탭으로 노출한다(없으면 빈 상태). 선택 fetched_at은 쿼리값이
    유효하면 그것, 아니면 해당 provider의 최신 스냅샷. 버킷은 official_buckets에 저장된
    값(=게이지를 실제로 구동한 ground truth)이라 'raw는 X인데 표시는 Y'를 바로 대조한다.
    """
    providers = [p for p in PROVIDERS if list_official_raw(conn, p)]
    empty = {"active_nav": "official_raw", "providers": providers,
             "selected_provider": None, "snapshots": [], "selected_fetched_at": None,
             "raw_pretty": None, "buckets": [], "meta": None}
    if not providers:
        return empty

    if provider not in providers:
        provider = providers[0]
    snaps = list_official_raw(conn, provider)            # 최신순

    chosen = get_official_raw(conn, provider, fetched_at) if fetched_at else None
    if chosen is None:
        chosen = snaps[0]                                # 기본 = 최신
    fa = chosen["fetched_at"]

    fs = get_fetch_state(conn, provider)
    return {
        "active_nav": "official_raw",
        "providers": providers,
        "selected_provider": provider,
        "snapshots": [{"fetched_at": r["fetched_at"], "status": r["status"],
                       "http_code": r["http_code"], "byte_len": r["byte_len"]}
                      for r in snaps],
        "selected_fetched_at": fa,
        "raw_pretty": _pretty_json(chosen["raw_text"]),
        "buckets": [dict(b) for b in official_buckets_at(conn, provider, fa)],
        "meta": {
            "status": chosen["status"], "http_code": chosen["http_code"],
            "byte_len": chosen["byte_len"], "created_at": chosen["created_at"],
            "fetch_state": dict(fs) if fs else None,
        },
    }


# --- 사용량 공유 문구(usage share snapshot, CONTEXT.md 동명 용어) ---


@dataclass
class ShareRow:
    """공유 문구 한 줄 = provider 1개의 공식 기간 소비 + 이번달 누적/한도%."""
    label: str
    today: PeriodSpend
    week: PeriodSpend
    month_usd: float | None      # 이번달 = 흐름(주기형 라이브 + 만료형 월초 델타, ADR 0024). 없으면 None
    util_pct: int | None         # 한도% = 위치(라이브 누적 pool_used/pool_limit). 없으면 None
    month_partial: bool = False  # 만료형 이번달 몫 미집계(월초 경계 미관측, ADR 0024)


@dataclass
class PoolGlance:
    """풀 합산 오늘/이번주/이번달 — partial 전염(any partial→partial, all none→none)."""
    today: PeriodSpend
    week: PeriodSpend
    month_usd: float | None
    month_partial: bool = False


def _round_spend(ps: PeriodSpend) -> PeriodSpend:
    """공유 산출물용 — usd를 내부 정밀도(4자리)로 정규화(ADR 0020 sum-exact-then-round).

    표시 1자리 반올림은 _usd가 담당 — 여기서 표시 정밀도로 미리 깎지 않는다(round-at-display).
    """
    return PeriodSpend(usd=None if ps.usd is None else round(ps.usd, 4), state=ps.state)


def _pool_period(spends: list[PeriodSpend]) -> PeriodSpend:
    """기간별 PeriodSpend 여럿을 풀로 합산. covered만 더하고 partial은 전염시킨다.

    정확값(내부 4자리)으로 합산하고 표시 반올림은 _usd에 맡긴다(ADR 0020). 줄은 각자
    독립 반올림되므로 표시된 줄 합과 표시 합계가 드물게 ≤0.1 어긋날 수 있다(합계가 정답).
    """
    covered = [s for s in spends if s.state != "none" and s.usd is not None]
    if not covered:
        return PeriodSpend(usd=None, state="none")
    usd = round(sum(s.usd for s in covered), 4)
    state = "partial" if any(s.state == "partial" for s in covered) else "complete"
    return PeriodSpend(usd=usd, state=state)


def pool_glance(rows: list[ShareRow]) -> PoolGlance:
    """활성 provider 행들을 풀 합산 글랜스로. 이번달은 월간 버킷 used 합(내부 4자리 정확값)."""
    months = [r.month_usd for r in rows if r.month_usd is not None]
    return PoolGlance(
        today=_pool_period([r.today for r in rows]),
        week=_pool_period([r.week for r in rows]),
        month_usd=round(sum(months), 4) if months else None,
        month_partial=any(r.month_partial for r in rows),
    )


def _pace(current: float | None, previous: float | None) -> dict | None:
    """페이스 신호 — 현재 구간 소비 vs 이전 동일 구간(same-span, ADR 0017).

    반환 {dir, pct}: dir="up"(더 씀)·"down"(덜 씀)·"flat"(±0.5% 미만). pct=|변화율| 반올림 정수.
    이전이 없거나(None)·0이면(비율 불가) None — 페이스 숨김(숫자만). 현재가 None이어도 None.
    """
    if current is None or previous is None or previous <= 0:
        return None
    delta = (current - previous) / previous * 100
    if abs(delta) < 0.5:
        return {"dir": "flat", "pct": 0}
    return {"dir": "up" if delta > 0 else "down", "pct": round(abs(delta))}


def _usd(v: float) -> str:
    return f"${v:,.1f}"


def _period_cell(key: str, ps: PeriodSpend) -> str:
    """오늘/이번주 한 칸. none이면 '데이터 없음', 아니면 금액(△·주석 없음 — 문구는 깨끗)."""
    if ps.state == "none" or ps.usd is None:
        return f"{key} 데이터 없음"
    return f"{key} {_usd(ps.usd)}"


def _month_cell(month_usd: float | None) -> str:
    if month_usd is None:
        return "이번달 데이터 없음"
    return f"이번달 {_usd(month_usd)}"


def build_share_text(rows: list[ShareRow], date_label: str, *, note: str | None = None) -> str:
    """사용량 공유 문구(CONTEXT.md) — 메신저 붙여넣기용 클립보드 텍스트.

    AI별 줄(오늘·이번주·이번달·한도%) + 합계 줄(한도% 없음). partial 경고는 카드가
    보내는 사람에게만 하고, 복사 문구 자체엔 △·주석을 넣지 않는다. none 기간은 '데이터 없음'.
    note(구독/로컬, ADR 0017)는 헤더 꼬리표로 출처를 고지한다(예: 'API 단가 환산').
    한도% 생략은 ShareRow.util_pct=None으로 자연 처리된다(구독은 한도 없음).
    """
    pool = pool_glance(rows)
    header = f"AI 사용량 ({date_label}, KST)"
    if note:
        header += f" · {note}"
    lines = [header]
    for r in rows:
        util = f" (한도 {r.util_pct}%)" if r.util_pct is not None else ""
        lines.append(
            f"· {r.label} {_period_cell('오늘', r.today)} · "
            f"{_period_cell('이번주', r.week)} · {_month_cell(r.month_usd)}{util}"
        )
    lines.append(
        f"합계 {_period_cell('오늘', pool.today)} · "
        f"{_period_cell('이번주', pool.week)} · {_month_cell(pool.month_usd)}"
    )
    return "\n".join(lines)


def share_context(conn, config: dict, now_kst: datetime | None = None) -> dict | None:
    """사용량 공유 문구 카드 컨텍스트 — USD 풀 provider만. 풀 없으면 None(카드 숨김).

    각 활성 provider의 공식 기간 소비(오늘·이번주, official_period_glance) + 이번달(월간 버킷
    used)·한도%(월간 버킷 util)로 ShareRow를 만들고, 풀 합산 글랜스 + 복사 문구를 함께 돌려준다.
    게이트는 view.pool_limit_usd(글랜스·전망 히어로와 동일 — 개인 구독제·온보딩은 None).
    """
    now = now_kst or datetime.now(KST)
    fp = forecast_params(config)              # config 팬아웃 정본(ctu·weeks·max_gap·is_pooled)
    active = set(fp.active)
    is_pooled = fp.is_pooled
    rows: list[ShareRow] = []
    pool_providers: list[str] = []
    for p in PROVIDERS:
        if p not in active:
            continue
        view = official_view(conn, p, now, fp.ctu, fp.weeks, is_pooled=is_pooled,
                             max_gap_minutes=fp.max_gap)
        if view.pool_limit_usd is None:
            continue
        pool_providers.append(p)
        meta = _PROVIDER_META.get(p, {"label": p.title()})
        g = official_period_glance(conn, p, now, is_pooled=is_pooled)
        # 한도%(util) = 위치(라이브 누적 pool_used/pool_limit) — 만료형 크레딧이 있어도 "지금 한도의
        # 몇 % 소진"이라 누적이 맞다(크레딧 만료 임박을 계속 알린다). ADR 0024.
        util = None
        if view.pool_used_usd is not None and view.pool_limit_usd:
            util = round(view.pool_used_usd / view.pool_limit_usd * 100)
        # 이번달 = 흐름(주기형 라이브 + 만료형 월초 델타) — 라이브 누적을 그대로 쓰면 만료형 크레딧의
        # 지난달분이 이번달로 샌다(ADR 0024). 만료형 경계 미관측 시 그 몫만 미집계(month_partial).
        month, month_partial = this_month_spend(conn, [p], now,
                                                max_gap_minutes=fp.max_gap, is_pooled=is_pooled)
        rows.append(ShareRow(label=meta["label"], today=_round_spend(g.today),
                             week=_round_spend(g.week), month_usd=month,
                             month_partial=month_partial, util_pct=util))
    if not rows:
        return None
    date_label = f"{now.astimezone(KST):%Y-%m-%d}"
    return {
        "pool": pool_glance(rows),
        "text": build_share_text(rows, date_label),
        "date_label": date_label,
        "providers": pool_providers,   # 풀 멤버 provider 키(기간별 카드 공식 페이스용, ADR 0017)
    }


# --- 기간별 사용량 카드(ADR 0017) — 총지출+share 통합, A군 모드 게이트(ADR 0015) ---


def _period_windows(now_kst: datetime) -> list[tuple]:
    """오늘/이번주/이번달 — (key, cur_start, prev_start, prev_end, prev_label). 현재 end는 항상 now.

    prev_*는 '이전 기간 *전체*'(ADR 0018이 0017 §4 same-span을 supersede): 어제 하루 전체·
    지난주 한 주 전체·지난달 한 달 전체. prev_end = 현재 구간 시작(= 직전 기간의 끝 경계)이라
    이전 *전체* 달력 구간을 가리키고, 사이클별 자기 합산이라 월 리셋을 빼기로 가로지르지 않는다.
    """
    now = now_kst.astimezone(KST)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())   # 월요일 0시 KST
    month_start = today_start.replace(day=1)
    prev_month_start = (month_start - timedelta(days=1)).replace(day=1)
    return [
        ("오늘", today_start, today_start - timedelta(days=1), today_start, "어제"),
        ("이번주", week_start, week_start - timedelta(days=7), week_start, "지난주"),
        ("이번달", month_start, prev_month_start, month_start, "지난달"),
    ]


def _local_share_rows(conn, active: list[str], now: datetime, windows: list[tuple]) -> list[ShareRow]:
    """활성 provider별 로컬 기간 합(오늘/이번주/이번달) → ShareRow(util_pct=None, 한도 없음)."""
    today_start, week_start, month_start = windows[0][1], windows[1][1], windows[2][1]
    rows: list[ShareRow] = []
    for p in PROVIDERS:
        if p not in active:
            continue
        meta = _PROVIDER_META.get(p, {"label": p.title()})
        rows.append(ShareRow(
            label=meta["label"],
            today=PeriodSpend(usd=range_spend(conn, p, today_start, now), state="complete"),
            week=PeriodSpend(usd=range_spend(conn, p, week_start, now), state="complete"),
            month_usd=range_spend(conn, p, month_start, now), util_pct=None))
    return rows


_LOCAL_SOURCE = "이 기기 · 추정"
_LOCAL_DISCLAIMER = "공개 API 단가 기준 추정 · 이 기기의 Claude Code와 Codex만 · 수집 시 갱신"


def period_card_context(conn, config: dict, now_kst: datetime | None = None) -> dict | None:
    """기간별 사용량 카드(ADR 0017·0018) — 오늘·이번주·이번달 동등 3칸 + 이전 기간 전체 페이스·기준값.

    A군 모드 게이트(ADR 0015): 엔터=공식 통합 풀(오늘/이번주=스냅샷 이력 델타, 이번달=라이브
    pool_used), 구독/로컬=로컬 JSONL 달력 기간 합(이 기기 추정). 각 기간에 **이전 기간 전체**(어제/
    지난주/지난달) 대비 ▲▼%와 그 **기준값**(prev_usd/prev_label)을 병기한다(ADR 0018). 페이스는
    비교 데이터 충분 시에만(공식은 경계 관측 게이트로 prev=None이면 꼬리·% 생략, 로컬은 이전 합>0이면
    %; 로컬 기준값은 미관측 개념이 없어 0 포함 항상 표시). 활성 0개·온보딩이면 None(카드 없음).
    로컬·메시지 없음이면 has_data=False(데이터 없음 너지).
    """
    now = now_kst or datetime.now(KST)
    active = tracked_providers(config)
    if not active or onboarding_pending(config):
        return None
    interval = official_fetch_settings(config)["min_interval_minutes"]
    _, is_pooled = _curation_for(config)
    windows = _period_windows(now)
    share = share_context(conn, config, now)
    # 공식 모드 = 공식 USD 풀 있음(share not None) + 개인구독제 아님(ADR 0015 _a_zone_official 동치).
    official = account_mode(config) != "subscription" and share is not None

    if official:
        pool, providers = share["pool"], share["providers"]
        max_gap = interval * 3   # 그보다 긴 공백은 수집 단절 — 경계 미관측으로 본다
        # 이번달 state=partial ⇐ 만료형 이번달 몫 미집계(월초 경계 미관측, ADR 0024) → 캐비엇.
        cur = {"오늘": pool.today, "이번주": pool.week,
               "이번달": PeriodSpend(usd=pool.month_usd,
                                   state="partial" if pool.month_partial else "complete")}
        periods = []
        for key, _cur_start, ps, pe, prev_label in windows:
            c = cur[key]
            # 이전 *전체* 기간(어제/지난주/지난달) 공식 소비 — 경계 미관측이면 None(꼬리·% 생략).
            prev = official_span_spend(conn, providers, ps, pe,
                                       max_gap_minutes=max_gap, is_pooled=is_pooled)
            pace = _pace(c.usd, prev) if c.state == "complete" else None
            # partial 캐비엇 방향이 기간마다 다르다(ADR 0024): 오늘/이번주=추적 공백→부풀 위험,
            # 이번달=만료형 크레딧 이번달 몫 미집계→축소. 툴팁을 방향에 맞춘다.
            note = ("크레딧 이번달 소비 미집계 — 월초 스냅샷이 쌓이면 반영" if key == "이번달"
                    else "추적 공백 — 부풀 수 있음")
            periods.append({"key": key, "usd": c.usd, "state": c.state, "pace": pace,
                            "prev_usd": prev, "prev_label": prev_label, "partial_note": note})
        return {
            "mode": "official", "source_label": "공식 · 계정 전체",
            "disclaimer": "공식 API 사용량 · 계정 전체(전 기기)",
            "has_data": True, "periods": periods,
            "partial_warning": pool.today.state == "partial" or pool.week.state == "partial",
            "share_text": share["text"], "date_label": share["date_label"],
        }

    # 로컬 모드(구독, 또는 엔터지만 공식 풀 없음 → 강등) — 이 기기·공개 단가 환산.
    date_label = f"{now.astimezone(KST):%Y-%m-%d}"
    if last_message_ts(conn, None, active) is None:
        return {"mode": "local", "source_label": _LOCAL_SOURCE, "disclaimer": _LOCAL_DISCLAIMER,
                "has_data": False, "periods": [], "partial_warning": False,
                "share_text": None, "date_label": date_label}
    rows = _local_share_rows(conn, active, now, windows)
    pool = pool_glance(rows)
    cur = {"오늘": pool.today, "이번주": pool.week,
           "이번달": PeriodSpend(usd=pool.month_usd, state="complete")}
    periods = []
    for key, _cur_start, ps, pe, prev_label in windows:
        c = cur[key]
        # 이전 *전체* 기간 합(어제/지난주/지난달). 로컬은 미관측 개념이 없어 기준값 항상 표시(0 포함),
        # 페이스만 이전 합>0일 때(_pace가 게이트).
        prev = range_spend(conn, None, ps, pe, providers=active)
        periods.append({"key": key, "usd": c.usd, "state": "complete", "pace": _pace(c.usd, prev),
                        "prev_usd": prev, "prev_label": prev_label})
    return {
        "mode": "local", "source_label": _LOCAL_SOURCE, "disclaimer": _LOCAL_DISCLAIMER,
        "has_data": True, "periods": periods, "partial_warning": False,
        "share_text": build_share_text(rows, date_label, note="API 단가 환산"),
        "date_label": date_label,
    }


def official_section_context(conn, config: dict, now_kst: datetime | None = None) -> dict:
    """'AI별 사용량' 섹션 조각(partial)용 컨텍스트 — provider 카드 + 자동 갱신 간격(분).

    수동 HX 갱신(POST /official/refresh)과 자동 폴링(GET /official/section)이 공유한다.
    """
    now = now_kst or datetime.now(KST)
    return {
        "official_cards": official_cards(conn, config, now),
        "official_interval": official_fetch_settings(config)["min_interval_minutes"],
        # 기간별 사용량 카드(ADR 0017) — 총지출+share 통합. 모드별 출처. 섹션 partial에 실어
        # 글랜스와 같은 주기로 갱신(엔터=라이브, 구독=수집 시만 — 카드 디스클레이머가 안내).
        "period_card": period_card_context(conn, config, now),
        # 개인구독제(ADR 0015 D4): rate-window 게이지를 '이용 한도(스로틀)'로 프레이밍하는 분기 신호.
        "account_mode": account_mode(config),
    }


def mini_view_context(conn, config: dict, now_kst: datetime | None = None) -> dict:
    """미니 뷰(상주 동반 글랜스 창, ADR 0008)용 컨텍스트 — 활성 AI별 압축 게이지 행.

    official_cards를 재사용하되 **official-only 불변식**을 강제한다: 공식 데이터가 없는
    카드(status no_data)는 큰 창처럼 로컬 추정으로 폴백하지 않고 no_official 플래그만 둔다
    (미니 뷰는 ingest와 무관 — 잔여 면에 한도 맥락 없는 로컬 추정은 부적합).
    provider당 모든 게이지를 그대로 노출한다(Codex 월간(+개인 구독제 rate-window), Claude 5h+7d 등).
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
        # 사용량 공유 문구(큰 창과 동일 산출물) — 미니 헤더 📋가 #mini-section의 .share-src를 읽는다.
        "share": share_context(conn, config, now),
    }


def _a_zone_official(config: dict, forecast_obj) -> bool:
    """A군(이번 달 총지출·전망 히어로·추세 오버레이)을 **공식**으로 렌더할지 판정(ADR 0015).

    공식 USD 풀이 있고(forecast_obj not None) 모드가 개인구독제가 아니면 공식(엔터프라이즈).
    - 엔터 + USD 풀 → 공식. 미설정 + USD 풀 → 공식(곧 enterprise로 시드될 상태).
    - 개인구독제는 USD 풀이 있어도(혼합 엣지) A군 로컬 유지 — 사용자 선택 존중(D6).
    - 엔터인데 공식 USD 풀이 없으면(forecast_obj None) 자동으로 로컬 강등(깨진 빈 히어로 방지, D6 안전장치).
    """
    return forecast_obj is not None and account_mode(config) != "subscription"


def overview_context(conn, sort: str, now_kst: datetime | None = None) -> dict:
    now = now_kst or datetime.now(KST)
    config = load_config()
    # 활성 AI(ADR 0005) — 화면의 "전체"는 곧 활성 합산이다(DB 전체가 아니다).
    active = tracked_providers(config)

    month_total = month_spend(conn, None, now, providers=active)

    # "폴더 사용량" 카드 — 사용량(USD) 내림차순 Top 10. by_project가 이미 cost-desc 정렬.
    # (비용/세션/캐시 토글 폐지로 정렬 키는 사용량 하나로 고정.)
    projects = by_project(conn, None, now, providers=active)[:10]
    sessions = by_session(conn, None, now, limit_n=10, providers=active)

    pricing = apply_pricing_overrides(load_pricing(), config.get("pricing_overrides"))
    cov = pricing_coverage(conn, pricing, providers=active)
    coach = insights(conn, now, None, cov=cov, providers=active)
    daily = daily_series(conn, None, now, providers=active)
    # 통합 월말 전망 — 조립 정본(outlook, forecast.py). 활성 AI 팬아웃·config 해석·투영이 인터페이스 뒤.
    # 화면의 "전체"=활성 합산이므로 forecast 풀도 활성만 본다(combined_forecast가 한도 보유분만 필터).
    fp = forecast_params(config)              # 실제선(pool_history)에 is_pooled·max_gap 재사용
    forecast_obj = outlook(conn, config, now)
    # A군 모드 게이트(ADR 0015): 공식(엔터) vs 로컬(개인구독제). 헤드라인·히어로·추세 오버레이를 가른다.
    a_official = _a_zone_official(config, forecast_obj)
    # 이번 달 총지출 헤드라인 — 엔터=공식 풀 used(계정 전체·실청구), 개인구독제/로컬=이 기기 추정.
    headline_usd = forecast_obj.used_usd if a_official else month_total
    forecast = _forecast_hero(forecast_obj) if a_official else None   # 개인구독제는 히어로 숨김
    interval = official_fetch_settings(config)["min_interval_minutes"]
    if a_official:
        fc_chart = forecast_chart_data(forecast_obj, daily, now)
        # 공식 사용량 스냅샷 이력 → 전망 차트 실제 과거 실선(ADR 0007). 활성 USD 풀만.
        # max_gap = 자동 갱신 간격 ×3(그보다 긴 공백은 수집 단절로 보아 선을 끊는다).
        pool_hist = pool_history(conn, active, max_gap_minutes=fp.max_gap, is_pooled=fp.is_pooled)
        forecast_actual = pool_history_to_daily(pool_hist, [p.day for p in daily], now)
    else:
        # 로컬 A군 — 한도·예상선·실제 공식선 없는 순수 로컬 추세(개인구독제엔 한도가 없다).
        fc_chart = {"limit": None, "line": None}
        forecast_actual = None

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
    last = last_message_ts(conn, None, active)
    has_data = last is not None
    token_comp = token_composition(conn, None, *month_bounds(now), providers=active)

    # 라벨 적응(ADR 0005): 활성 ≥2면 "통합/전 AI 합산", 1개면 수식어 떼고 provider명.
    combined = len(active) >= 2
    solo_label = (_PROVIDER_META.get(active[0], {}).get("label", active[0].title())
                  if len(active) == 1 else None)

    return {
        "active_nav": "dashboard", "sort": sort,
        "user_label": user_label(config),
        # 완전 신규(미설정 + 빈 시드) → 빈 껍데기 대신 시작 안내 카드로 대체(A 모집단).
        "onboarding": onboarding_pending(config),
        # 첫 수집이 백그라운드로 도는 중이면 "수집 중" 배너를 건다(창 우선 기동, ADR 0023).
        "ingesting": control.is_ingesting(),
        "tracked": active,
        "combined": combined, "solo_label": solo_label, "active_empty": not active,
        "month": now.strftime("%Y-%m"),
        "month_total": month_total,
        # A군 모드 게이트(ADR 0015) — 헤드라인 출처/존 라벨/디스클레이머 분기에 쓴다.
        "account_mode": account_mode(config),
        "a_official": a_official,
        "headline_usd": headline_usd,
        "headline_official": a_official,
        "forecast": forecast,
        "forecast_limit": fc_chart["limit"],
        "forecast_line": fc_chart["line"],
        "forecast_actual": forecast_actual,
        "official_cards": official_cards(conn, config, now),
        "official_interval": official_fetch_settings(config)["min_interval_minutes"],
        # 기간별 사용량 카드(ADR 0017) — 초기 로드 시 즉시 표시(이후 섹션 폴링이 갱신).
        "period_card": period_card_context(conn, config, now),
        "projects": projects, "sessions": sessions, "insights": coach,
        "daily_labels": [p.day for p in daily],
        "trend_series": trend_series,
        "trend_totals": trend_totals,
        "last_ts": last,
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
    period = period if period in ("day", "week", "month") else "month"
    start_dt, nxt_dt, label = period_bounds(period, anchor_kst)
    return start_dt, nxt_dt, label, period, False


DIM_LABELS = {"model": "모델", "skill": "스킬", "branch": "브랜치"}
_NULL_BUCKET = {"model": "(unknown)", "skill": "(미귀속)", "branch": "(브랜치 없음)"}


def dimension_context(conn, anchor_kst: datetime, provider: str, *,
                      dim: str = "model", now_kst: datetime | None = None,
                      period: str = "month", start: str | None = None,
                      end: str | None = None) -> dict:
    """기준별(모델/스킬/브랜치) 사용/비용 + 서브에이전트 비중. 주/월 또는 사용자 지정 구간."""
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
    last = last_message_ts(conn)
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
        "last_ts": last,
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
    """사용 이력(로컬) — 날짜→폴더→세션 트리. 주/월 기간 또는 사용자 지정 [start, end]."""
    now = now_kst or datetime.now(KST)
    config = load_config()
    active = tracked_providers(config)
    provider = _active_provider(provider, active)   # 비활성 provider 요청 → 전체 폴백
    last = last_message_ts(conn)
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
        "last_ts": last,
        "last_ingest_at": sidebar_freshness(conn),
        "period_label": label,
        "prev_anchor": (s - timedelta(days=1)).strftime("%Y-%m-%d"),
        "next_anchor": nxt.strftime("%Y-%m-%d"),
        "has_next": nxt <= now,
        "tree": tree,
        "count": len(rows),
        "total": round(sum(r.cost for r in rows), 4),
    }


def _cum_segments_and_endcum(reset_segments, bins, bin_of):
    """리셋-세그먼트를 bin-인덱스 배열 리스트 + bin별 말단 누적으로 매핑(ADR 0019).

    각 reset_segment → bins 길이 배열(점 없는 bin=null, 같은 bin은 오름차순 last-wins).
    템플릿이 세그먼트마다 dataset으로 그려 **리셋은 하드 끊김**(세그먼트 분리), **세그먼트 내
    null(수집 공백)은 점선 브리지**(`segment.borderDash`). `bin_of(dt)`가 bins 밖이면 None을
    반환해 건너뛴다(hourly에서 당일 밖 표본 제외). end_cum = bin→말단 누적(표 '누적' 컬럼).
    """
    idx = {b: i for i, b in enumerate(bins)}
    seg_arrays: list = []
    end_cum: dict = {}
    for seg in reset_segments:
        arr: list = [None] * len(bins)
        for pt in seg:
            dt = parse_ts(pt["ts"])
            if dt is None:
                continue
            b = bin_of(dt)
            if b in idx:
                arr[idx[b]] = pt["used_usd"]
                end_cum[b] = pt["used_usd"]   # 오름차순 + 후행 세그먼트 → last-wins
        if any(v is not None for v in arr):
            seg_arrays.append(arr)
    return seg_arrays, end_cum


def official_history_context(conn, anchor_kst: datetime, provider: str = "", *,
                             period: str = "month", start: str | None = None,
                             end: str | None = None, now_kst: datetime | None = None) -> dict:
    """사용 이력(공식) 화면(ADR 0010/0019) — 통합 풀 누적 선 + 소비 막대 + 표.

    데이터는 공식 사용량 스냅샷 이력(ADR 0007)의 새 표현 — 신규 취득 없음. 소진형 풀만
    (rate-window 제외) 보여주며, 소진형 풀이 없으면 has_pool=False(빈 상태). period=day는
    그날의 **시간대별(24칸)** 뷰(ADR 0019), 월/주는 일별 — 둘 다 누적선은 리셋-세그먼트로
    그려 갭은 점선·리셋은 끊김. 누적선 점 값은 절대 월누적이고 일 뷰는 한도선을 생략한다.
    """
    now = now_kst or datetime.now(KST)
    config = load_config()
    active = tracked_providers(config)
    provider = _active_provider(provider, active)   # 비활성 provider 요청 → 전체 폴백
    s, nxt, label, period, custom = _resolve_range(anchor_kst, period, start, end)
    is_hourly = (period == "day" and not custom)    # 시간대별 렌더는 일 토글 한정(ADR 0019)

    # 소진형 풀 판정 + 한도선 값 — outlook(전망 조립 정본)이 None이면 소진형 풀 없음(빈 상태).
    fp = forecast_params(config)              # 아래 pool_history 계열에 is_pooled 재사용
    fobj = outlook(conn, config, now)
    is_pooled = fp.is_pooled
    has_pool = fobj is not None
    pool_limit = fobj.limit_usd if fobj else None
    pool_providers = fobj.providers if fobj else active
    view_providers = [provider] if provider else pool_providers

    interval = official_fetch_settings(config)["min_interval_minutes"]
    # provider 순서 = _TREND_STYLE(스택/막대/드릴다운 일관) 우선, 나머지는 뒤로.
    prov_list = ([p for p in _TREND_STYLE if p in view_providers]
                 + [p for p in view_providers if p not in _TREND_STYLE])
    prov_label = {p: (_TREND_STYLE[p][0] if p in _TREND_STYLE else p.title()) for p in prov_list}
    # 누적선용 리셋-세그먼트 — 갭은 forward-fill(점선 브리지), 리셋만 분할(하드 끊김).
    reset_segments = pool_history(conn, view_providers, max_gap_minutes=None, is_pooled=is_pooled)

    def _bar_series(rows):
        return [{
            "key": p, "label": _TREND_STYLE[p][0] if p in _TREND_STYLE else p.title(),
            "color": _TREND_STYLE[p][1] if p in _TREND_STYLE else "#8a8a8a",
            "data": [r["per_provider"].get(p) for r in rows],   # provider별 소비(미커버 None)
        } for p in prov_list]

    def _remaining(cum):
        return round(pool_limit - cum, 4) if (pool_limit and cum is not None) else None

    if is_hourly:
        hourly = pool_hourly_history(conn, view_providers, day_start=s, is_pooled=is_pooled)
        bins = list(range(24))
        chart_labels = [f"{h:02d}" for h in bins]
        cum_segments, end_cum = _cum_segments_and_endcum(
            reset_segments, bins,
            lambda dt: (dt.astimezone(KST).hour if s <= dt.astimezone(KST) < nxt else None))
        # 표에서 미래 시간대 제외(빈 시작 시각 > now) — 과거 날짜는 24행 전부, 오늘은
        # 현재(부분 관측) 시간대까지. 차트 축은 24칸 유지(월/주 뷰와 같은 규칙).
        table = [{**r, "hlabel": f'{r["hour"]:02d}시',
                  "end_cumulative_usd": end_cum.get(r["hour"]),
                  "remaining_usd": _remaining(end_cum.get(r["hour"])), "detail": None}
                 for r in hourly if s + timedelta(hours=r["hour"]) <= now]
        bar_series = _bar_series(hourly)
        empty_day = all(not r["covered"] for r in hourly)
    else:
        daily = pool_daily_history(conn, view_providers, start=s, nxt=nxt, is_pooled=is_pooled)
        bins = [r["date"] for r in daily]
        chart_labels = [d.strftime("%m-%d") for d in bins]
        cum_segments, end_cum = _cum_segments_and_endcum(
            reset_segments, bins, lambda dt: dt.astimezone(KST).date())
        detail_by_day = pool_snapshots_by_day(conn, prov_list, start=s, nxt=nxt, is_pooled=is_pooled)   # 드릴다운
        for dets in detail_by_day.values():       # provider 라벨 주입(템플릿 단순화)
            for pd in dets:
                pd["label"] = prov_label.get(pd["provider"], pd["provider"])
        # 표에서 미래 날짜 제외 — 미래는 "수집 공백"이 아니라 미관측 대상(행 없음).
        # 차트 축은 기간 전체 유지(축은 라벨이지 관측 주장이 아님 — CONTEXT.md).
        today = now.astimezone(KST).date()
        table = [{**r, "ymd": r["date"].strftime("%Y-%m-%d"), "md": r["date"].strftime("%m-%d"),
                  "end_cumulative_usd": end_cum.get(r["date"]),
                  "remaining_usd": _remaining(end_cum.get(r["date"])),
                  "detail": detail_by_day.get(r["date"])} for r in daily
                 if r["date"] <= today]
        bar_series = _bar_series(daily)
        empty_day = False

    return {
        "active_nav": "official_history",
        "user_label": user_label(config),
        "provider": provider,
        "filter_providers": _filter_options(active), "show_filter": len(active) >= 2,
        "active_empty": not active,
        "has_pool": has_pool, "pool_limit": pool_limit,
        "is_hourly": is_hourly, "empty_day": empty_day,
        "table": table,
        "chart_labels": chart_labels, "cum_segments": cum_segments,
        "chart_limit": (None if is_hourly else pool_limit),   # 일 뷰는 한도선 생략(자동 줌)
        "bar_series": bar_series,
        "period": period, "custom": custom, "period_label": label,
        "anchor": anchor_kst.strftime("%Y-%m-%d"),
        "start": start or "", "end": end or "",
        "prev_anchor": (s - timedelta(days=1)).strftime("%Y-%m-%d"),
        "next_anchor": nxt.strftime("%Y-%m-%d"),
        "has_next": nxt <= now,
        "interval": interval,
        "last_ingest_at": sidebar_freshness(conn),
    }
