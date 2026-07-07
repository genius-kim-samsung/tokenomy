"""공식 사용량 집계 — 공식 스냅샷(official_buckets) 기반 뷰·렌즈·풀 이력·통합 전망.

로컬 롤업(aggregate.py)과 상호 호출 없는 별개 깊은 모듈이다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from tokenomy.clock import (
    KST, _trailing_window_bounds, add_business_days, business_days_between,
    month_bounds, parse_ts,
)
from tokenomy.domain import is_pooled_kind

# 공식 미러 패널의 버킷 표시 순서(공식 앱 미러).
_BUCKET_ORDER = {"monthly_limit": 0, "codex_monthly": 0, "event_credit": 1, "promo": 2, "rate_window": 3}


@dataclass
class OfficialLens:
    """예측 렌즈 — 활성 버킷의 소비 속도/소진예상/리셋 D-day."""
    bucket_key: str
    daily_rate_usd: float | None    # USD/영업일. 유효 차분 1개 미만이면 None
    exhaust_date: date | None
    days_left_to_reset: int | None  # 현재 주기 리셋까지 영업일
    dday_warning: bool


@dataclass
class OfficialView:
    """공식 미러 패널 1개(provider별) — 버킷 + 주기 USD + 예측 렌즈 + 상태."""
    provider: str
    buckets: list[dict]                 # 표시용(버킷 행 dict, 표시 순서)
    active_key: str | None
    lens: OfficialLens | None
    period_used_usd: float | None       # 월간(공식). 카드 게이지용. 없으면 None
    period_limit_usd: float | None
    pool_used_usd: float | None         # 통합 전망 풀 기여 = 월간+포함크레딧 등 USD 한도 버킷 used 합(ADR 0004)
    pool_limit_usd: float | None        # 동 limit 합. USD 한도 버킷이 없으면 None
    fetched_at: str | None
    stale_minutes: int | None
    status: str                         # "ok" | "no_data" | fetch_state.last_status
    note: str | None


def _row_to_bucket_dict(r) -> dict:
    """official_buckets 행 → 표시용 dict(resets_at은 ISO 문자열)."""
    d = dict(r)
    return {
        "bucket_key": d["bucket_key"], "raw_key": d["raw_key"], "bucket_kind": d["bucket_kind"],
        "label": d["label"], "native_unit": d["native_unit"],
        "used_native": d["used_native"], "limit_native": d["limit_native"],
        "remaining_native": d["remaining_native"],
        "used_usd": d["used_usd"], "limit_usd": d["limit_usd"], "remaining_usd": d["remaining_usd"],
        "utilization": d["utilization"], "resets_at": d["resets_at"],
    }


def _lens_from_series(conn, provider: str, bucket_key: str, now_kst: datetime,
                      limit_usd: float | None, used_usd: float | None,
                      reset_date: date | None, weeks: int = 2,
                      *, is_pooled=None, max_gap_minutes: int | None = None) -> OfficialLens | None:
    """공식 일일 소비속도(official_daily_rate)로 소진예상·리셋 D-day 산출(ADR 0015 D3).

    rate = 공식 스냅샷 기반 기울기((a)트레일링 델타 → (b)월초누적 폴백). 로컬 기울기 폐기 —
    카드 고스트도 히어로(combined_forecast)와 같은 엔진으로 공식 계정 전체 속도를 쓴다.
    공식 소비가 없으면 daily_rate=None. provider 풀에 USD 한도 버킷이 둘 이상이면(예: Claude
    월간+이벤트) rate는 풀 합산 기울기 — active 버킷이 그 풀의 주 소진처라 근사로 타당하다.
    bucket_key는 반환값 식별용으로만 사용(rate 계산에 미사용).
    """
    rate = official_daily_rate(conn, [provider], now_kst, weeks,
                               is_pooled=is_pooled, max_gap_minutes=max_gap_minutes)

    exhaust_date: date | None = None
    if rate and limit_usd and used_usd is not None and limit_usd > used_usd:
        need = math.ceil((limit_usd - used_usd) / rate)
        exhaust_date = add_business_days(now_kst.date(), need)

    days_left = business_days_between(now_kst.date(), reset_date) if reset_date else None
    dday = bool((exhaust_date is not None and reset_date is not None and exhaust_date < reset_date)
                or (limit_usd and used_usd is not None and used_usd / limit_usd >= 0.80))
    return OfficialLens(bucket_key=bucket_key, daily_rate_usd=rate, exhaust_date=exhaust_date,
                        days_left_to_reset=days_left, dday_warning=dday)


def official_view(conn, provider: str, now_kst: datetime,
                  credit_to_usd: float, weeks: int = 2, *, is_pooled=None,
                  max_gap_minutes: int | None = None) -> OfficialView:
    """공식 미러 패널 컨텍스트. 최신 스냅샷(공식)에서 버킷·풀·예측 렌즈를 조립한다.

    - period_used/limit = 월간 버킷(공식 ground truth). 없으면 None.
    - Claude 월 버킷 resets_at None은 다음 달 경계(KST)로 채운다.
    - 활성 버킷 선정(1차): 후보(풀 멤버 & stale 제외)의 series 최근 두 스냅샷 used 양의 차분이
      가장 큰 버킷 — 동률은 tie-break(event<monthly). (2차) 양의 차분이 없으면 remaining>0 첫
      버킷; 없으면 첫 후보. 풀 멤버십은 큐레이션(is_pooled, ADR 0016) — 기본은 안정 월 한도만
      이라 코드네임 크레딧은 후보·풀에서 빠진다(buckets 표시 목록에는 남음). promo/rate_window 제외.
    - 예측 렌즈(lens) rate는 공식 기울기(official_daily_rate, ADR 0015 D3). 공식 소비가 없으면 daily_rate=None.
    """
    from tokenomy.db import latest_official_snapshot, get_fetch_state, official_bucket_series

    rows = latest_official_snapshot(conn, provider)
    fetched_at = rows[0]["fetched_at"] if rows else None
    _, next_month = month_bounds(now_kst)

    buckets = [_row_to_bucket_dict(r) for r in rows]
    # Claude 월 버킷 resets_at 보강(다음 달 경계)
    for b in buckets:
        if b["bucket_kind"] in ("monthly_limit", "codex_monthly") and not b["resets_at"]:
            b["resets_at"] = next_month.isoformat()
    buckets.sort(key=lambda b: _BUCKET_ORDER.get(b["bucket_kind"], 9))

    monthly = next((b for b in buckets if b["bucket_kind"] in ("monthly_limit", "codex_monthly")), None)
    period_used = monthly["used_usd"] if monthly else None
    period_limit = monthly["limit_usd"] if monthly else None

    # staleness(분)
    stale_minutes = None
    if fetched_at:
        dt = parse_ts(fetched_at)
        if dt is not None:
            stale_minutes = max(0, int((now_kst - dt).total_seconds() // 60))

    # 상태 (Fix 2: last_status가 DB-null인 경우 "no_data"로 보정)
    if rows:
        status = "ok"
    else:
        st = get_fetch_state(conn, provider)
        status = (st["last_status"] if st else None) or "no_data"

    # 활성 버킷 + 렌즈
    # stale 제외: resets_at이 설정됐고 이미 과거면 후보에서 뺀다
    active_key = None
    lens = None
    ip = _resolve_is_pooled(is_pooled)
    tie_order = {"event_credit": 0, "monthly_limit": 1, "codex_monthly": 1}
    candidates = [
        b for b in buckets
        if ip(provider, b["raw_key"], b["bucket_kind"])
        and not (b["resets_at"] and parse_ts(b["resets_at"]) is not None
                 and parse_ts(b["resets_at"]) < now_kst)
    ]
    # 통합 전망 풀 기여 = candidates(stale 제외 USD 한도 버킷) 중 limit_usd 있는 것의 합.
    # 카드 게이지(period_*)는 월간만 보지만, 전망은 포함 크레딧 등 실제 닳는 버킷까지 합산(ADR 0004).
    pool_cands = [b for b in candidates if b["limit_usd"] is not None]
    pool_used = round(sum(b["used_usd"] or 0.0 for b in pool_cands), 4) if pool_cands else None
    pool_limit = round(sum(b["limit_usd"] for b in pool_cands), 4) if pool_cands else None
    if candidates:
        # 1차: series 최근 두 스냅샷의 양의 used 차분이 가장 큰 버킷
        def _recent_diff(b: dict) -> float:
            series = official_bucket_series(conn, provider, b["bucket_key"])
            pts = [(r["fetched_at"], r["used_usd"]) for r in series if r["used_usd"] is not None]
            if len(pts) < 2:
                return 0.0
            diff = pts[-1][1] - pts[-2][1]
            return diff if diff > 0 else 0.0

        diffs = {b["bucket_key"]: _recent_diff(b) for b in candidates}
        max_diff = max(diffs.values())
        if max_diff > 0:
            # 최대 차분 후보 중 tie-break 순서가 가장 낮은 것
            best = min(
                (b for b in candidates if diffs[b["bucket_key"]] == max_diff),
                key=lambda b: tie_order.get(b["bucket_kind"], 9),
            )
            active = best
        else:
            # 2차: tie-break 정렬 후 remaining>0 첫 버킷, 없으면 첫 후보
            sorted_cands = sorted(candidates, key=lambda b: tie_order.get(b["bucket_kind"], 9))
            active = next((b for b in sorted_cands if (b["remaining_usd"] or 0) > 0), sorted_cands[0])

        active_key = active["bucket_key"]
        reset_date = parse_ts(active["resets_at"]).date() if active["resets_at"] else None
        # 렌즈 rate=공식(official_daily_rate, ADR 0015 D3). pool_used=이 provider 풀 누적(=(b)월초누적 분자).
        lens = _lens_from_series(conn, provider, active_key, now_kst,
                                 active["limit_usd"], active["used_usd"], reset_date, weeks,
                                 is_pooled=is_pooled, max_gap_minutes=max_gap_minutes)

    note = None if rows else "공식 미취득 — 로컬 추정(USD)"
    return OfficialView(
        provider=provider, buckets=buckets, active_key=active_key, lens=lens,
        period_used_usd=period_used, period_limit_usd=period_limit,
        pool_used_usd=pool_used, pool_limit_usd=pool_limit,
        fetched_at=fetched_at, stale_minutes=stale_minutes, status=status, note=note,
    )


@dataclass
class CombinedForecast:
    """통합 풀 월말 전망 — 한도 있는 provider 합산 + 로컬 소비속도 직선 연장.

    위치(used/limit)는 공식 ground-truth, 기울기(daily_rate)는 로컬 JSONL 추정.
    지평은 달력 월말(KST). projected_remaining 양수=여유 / 음수=부족.
    """
    providers: list[str]
    used_usd: float
    limit_usd: float
    remaining_usd: float
    daily_rate_usd: float | None
    bdays_remaining: int
    projected_used_usd: float | None
    projected_remaining_usd: float | None
    exhaust_date: date | None
    is_exhausted: bool
    per_provider: list[dict]
    month_end: date
    this_month_used_usd: float | None = None   # 이번달 흐름(주기형 라이브+만료형 델타, ADR 0024). 히어로 헤드라인용
    this_month_partial: bool = False           # 만료형 이번달 몫 미집계(월초 경계 미관측)


def _pool_used_latest(conn, providers: list[str], *, is_pooled=None) -> float | None:
    """각 provider 최신 스냅샷의 풀 used 합(시각 무관). 기여 provider 없으면 None.

    위치(official_view의 pool_used = latest_official_snapshot)와 같은 "현재 위치" 의미 —
    주기형 이번달 used는 곧 이 라이브 위치라 시각 게이트 없이 최신값을 쓴다(ADR 0024).
    """
    total = None
    for p in providers:
        hist = pool_used_history(conn, p, is_pooled=is_pooled)
        if hist:
            total = (total or 0.0) + hist[-1][1]
    return total


def _earliest_pool_snapshot(conn, providers: list[str], *, is_pooled=None) -> datetime | None:
    """풀 기여 provider들의 최초 공식 스냅샷 시각. 없으면 None."""
    earliest = None
    for p in providers:
        hist = pool_used_history(conn, p, is_pooled=is_pooled)
        if hist and (earliest is None or hist[0][0] < earliest):
            earliest = hist[0][0]
    return earliest


def _official_trailing_rate(conn, providers: list[str], now_kst: datetime,
                            weeks: int, *, is_pooled=None) -> float | None:
    """(a) 공식 스냅샷 델타 트레일링 기울기(USD/영업일) — 이력이 충분할 때만(ADR 0015 D3).

    윈도우 시작 **이전** 스냅샷이 있어야(=베이스라인 존재) 트레일링 델타가 깨끗하다 — 없으면
    None(이력 부족 → 호출자가 (b)로 강등). pool_daily_history(리셋 보정 델타)를 윈도우에서
    합산 ÷ 윈도우 영업일. 소비 0이면 None.
    """
    start, nxt = _trailing_window_bounds(now_kst, weeks)
    earliest = _earliest_pool_snapshot(conn, providers, is_pooled=is_pooled)
    if earliest is None or earliest >= start:
        return None                                 # 윈도우 시작 전 베이스 없음 → 이력 부족
    rows = pool_daily_history(conn, providers, start=start, nxt=nxt, is_pooled=is_pooled)
    consumed = sum(r["used_usd"] for r in rows if r["covered"] and r["used_usd"] is not None)
    bdays = business_days_between(start.date(), now_kst.date() + timedelta(days=1))
    return round(consumed / bdays, 4) if (bdays > 0 and consumed > 0) else None


def _official_mtd_rate(now_kst: datetime, pool_used: float | None) -> float | None:
    """(b) 월초누적 평균 기울기 = pool_used / 월초~오늘 영업일. used 없거나 0이면 None."""
    if pool_used is None or pool_used <= 0:
        return None
    month_start, _ = month_bounds(now_kst)
    bdays = business_days_between(month_start.date(), now_kst.date() + timedelta(days=1))
    return round(pool_used / bdays, 4) if bdays > 0 else None


def official_daily_rate(conn, providers: list[str], now_kst: datetime, weeks: int,
                        *, is_pooled=None, max_gap_minutes: int | None = None) -> float | None:
    """엔터프라이즈 전망 기울기(USD/영업일) — **공식만**(로컬 기울기 폐기, ADR 0015 D3).

    (a) 공식 스냅샷 델타 트레일링 우선 → 이력 부족 시 (b) 월초누적 평균 → 둘 다 불가면 None(위치만).
    다중 기기 사용자에서 한 기기 로컬 속도만 보던 과소추정과 '공식 위에 로컬을 얹는' 하이브리드 혼란을
    없앤다 — 위치(used)도 기울기도 모두 공식 계정 전체다. 풀 멤버십은 큐레이션(is_pooled, ADR 0016).
    (b) 월초누적 분자 = **이번달 흐름**(this_month_spend: 주기형 라이브 + 만료형 월초 델타, ADR 0024)
    — 옛 raw pool_used는 만료형 크레딧의 지난달 누적을 이번달로 과다계상해 기울기를 부풀렸다.
    """
    r = _official_trailing_rate(conn, providers, now_kst, weeks, is_pooled=is_pooled)
    if r is not None:
        return r
    flow, _partial = this_month_spend(conn, providers, now_kst,
                                      max_gap_minutes=max_gap_minutes, is_pooled=is_pooled)
    return _official_mtd_rate(now_kst, flow)


def forecast_month_line(used_usd: float, daily_rate_usd: float,
                        now_kst: datetime) -> dict[int, float]:
    """오늘~월말 각 달력일의 투영 used(월간 KST) — key=일(1..말일), 값=그 날까지 예상 used.

    투영 규칙의 **정본 1곳**: 오늘=used anchor, 이후 영업일마다 daily_rate 누적(주말 flat).
    오늘 이전 날은 넣지 않는다(차트가 None으로 끊음). 히어로 월말값(combined_forecast)과
    차트 라인(forecast_chart_data)이 이 한 walk를 공유해 구성상 일치한다 — 영업일/주말 규칙을
    바꿔도 한 곳만 고치면 된다. rate None·소진 여부 판단은 호출자 몫(여긴 순수 산술).
    """
    _, next_month = month_bounds(now_kst)
    month_end_day = (next_month - timedelta(days=1)).day
    out: dict[int, float] = {now_kst.day: round(used_usd, 4)}
    bd = 0
    for day in range(now_kst.day + 1, month_end_day + 1):
        if date(now_kst.year, now_kst.month, day).weekday() < 5:
            bd += 1
        out[day] = round(used_usd + daily_rate_usd * bd, 4)
    return out


def combined_forecast(conn, views: list[OfficialView], now_kst: datetime,
                      weeks: int = 2, *, is_pooled=None,
                      max_gap_minutes: int | None = None) -> CombinedForecast | None:
    """공식 USD/크레딧 한도가 있는 provider를 한 풀로 합쳐 달력 월말 예상 잔여를 낸다.

    풀 = pool_limit_usd가 있는 view들. 없으면 None(히어로 숨김).
    used/limit = 공식 pool_*의 합(현재 위치 — 월간+포함크레딧 등 USD 한도 버킷 합산, ADR 0004).
    예상 used = used + daily_rate × (오늘 이후 월말까지 남은 영업일). 음수 잔여면 소진 예상일 산출.
    daily_rate = **공식** 기울기(ADR 0015 D3·official_daily_rate): (a)스냅샷 델타 트레일링 →
    (b)월초누적 평균 폴백 → 둘 다 불가면 None. 위치도 기울기도 모두 공식 계정 전체(로컬 기울기 폐기).
    이미 소진(used≥limit)이거나 공식 기울기가 없으면(daily_rate None) 전망은 생략(None)한다.

    한계(ADR 0004): 포함 크레딧은 리셋 주기가 월간과 다를 수 있으나(예: 분기 만료),
    v1.x는 달력 월말 지평으로 함께 본다 — "이번 달 이 속도면 가용 예산을 다 쓰나?" 질문에 답하기 위함.
    """
    pool = [v for v in views if v.pool_limit_usd]
    if not pool:
        return None

    used = round(sum(v.pool_used_usd or 0.0 for v in pool), 4)
    limit = round(sum(v.pool_limit_usd for v in pool), 4)
    remaining = round(limit - used, 4)

    _, next_month = month_bounds(now_kst)
    month_end = (next_month - timedelta(days=1)).date()
    pool_providers = [v.provider for v in pool]
    # 기울기 = 공식만(ADR 0015 D3). (a)스냅샷 델타 트레일링 → (b)월초누적 폴백 → 없으면 None.
    daily_rate = official_daily_rate(conn, pool_providers, now_kst, weeks,
                                     is_pooled=is_pooled, max_gap_minutes=max_gap_minutes)
    # 헤드라인용 이번달 흐름(위치=used는 라이브 누적, 흐름=this_month은 만료형 델타로 분리, ADR 0024).
    tm_used, tm_partial = this_month_spend(conn, pool_providers, now_kst,
                                           max_gap_minutes=max_gap_minutes, is_pooled=is_pooled)
    bdays_remaining = business_days_between(now_kst.date() + timedelta(days=1), next_month.date())

    is_exhausted = used >= limit
    projected_used = projected_remaining = None
    exhaust_date = None
    if daily_rate is not None and not is_exhausted:
        # 투영 정본은 forecast_month_line(차트 라인과 동일 walk) — 월말값이 예상 used.
        projected_used = forecast_month_line(used, daily_rate, now_kst)[month_end.day]
        projected_remaining = round(limit - projected_used, 4)
        if projected_used > limit and daily_rate > 0:
            need = math.ceil((limit - used) / daily_rate)
            exhaust_date = add_business_days(now_kst.date(), need)

    return CombinedForecast(
        providers=[v.provider for v in pool],
        used_usd=used, limit_usd=limit, remaining_usd=remaining,
        daily_rate_usd=daily_rate, bdays_remaining=bdays_remaining,
        projected_used_usd=projected_used, projected_remaining_usd=projected_remaining,
        exhaust_date=exhaust_date, is_exhausted=is_exhausted,
        per_provider=[{"provider": v.provider, "used_usd": v.pool_used_usd or 0.0,
                       "limit_usd": v.pool_limit_usd} for v in pool],
        month_end=month_end,
        this_month_used_usd=tm_used, this_month_partial=tm_partial,
    )


def _default_is_pooled(provider: str, raw_key: str, bucket_kind: str) -> bool:
    """큐레이션 미주입 시 풀 멤버십 모양 기본값 — 안정 월 한도 키만 풀(event_credit 제외)."""
    return is_pooled_kind(bucket_kind)


def _resolve_is_pooled(is_pooled):
    """is_pooled predicate 정규화 — None이면 모양 기본값(_default_is_pooled)."""
    return is_pooled if is_pooled is not None else _default_is_pooled


def pool_used_history(conn, provider: str, *, is_pooled=None) -> list[tuple[datetime, float]]:
    """provider의 스냅샷별 통합 풀 used(USD) 시계열 [(fetched_at dt, used_usd 합)] 오름차순.

    각 공식 스냅샷(fetched_at)에서 풀 멤버(is_pooled True & limit_usd 존재)의 used_usd를
    합산한다 — official_view의 풀 집계(pool_*)와 동형이되 최신 스냅샷이 아니라 전 이력에 적용.
    풀 멤버십은 큐레이션(ADR 0016) — is_pooled None이면 모양 기본값(안정 월 한도만, 코드네임
    크레딧 제외). USD 한도 버킷이 하나도 없는 스냅샷(개인 구독 rate_window만 등)은 제외한다
    (공식 사용량 스냅샷 이력은 소진형 한도에만 의미, ADR 0007). 시각 파싱 실패 스냅샷도 제외.
    """
    ip = _resolve_is_pooled(is_pooled)
    rows = conn.execute(
        "SELECT fetched_at, raw_key, bucket_kind, used_usd, limit_usd FROM official_buckets "
        "WHERE provider=? ORDER BY fetched_at ASC, id ASC",
        (provider,),
    ).fetchall()
    by_snap: dict[str, float] = {}
    for r in rows:
        if ip(provider, r["raw_key"], r["bucket_kind"]) and r["limit_usd"] is not None:
            by_snap[r["fetched_at"]] = by_snap.get(r["fetched_at"], 0.0) + (r["used_usd"] or 0.0)
    out: list[tuple[datetime, float]] = []
    for ft, used in by_snap.items():
        dt = parse_ts(ft)
        if dt is not None:
            out.append((dt, round(used, 4)))
    out.sort(key=lambda p: p[0])
    return out


# 누적 used 진동 노이즈(공식 API 반올림/집계 흔들림, 예 44.88↔44.87)를 청구 리셋으로
# 오판하지 않기 위한 임계. 진짜 리셋은 누적이 직전의 절반 미만으로 급락한다 — 그 미만
# 하락만 리셋으로 보고, 그 이상(미세 하락 포함)은 정상 변동으로 처리한다(ADR 0007/0010).
_RESET_RATIO = 0.5


def _is_reset(prev_v: float | None, v: float) -> bool:
    """누적값이 직전의 절반 미만으로 급락하면 청구 리셋(노이즈 진동은 제외)."""
    return prev_v is not None and v < prev_v * _RESET_RATIO


def _consumption_delta(prev_v: float | None, v: float) -> tuple[float, bool]:
    """인접 누적값 → (일 소비 델타, 리셋 여부).

    첫 표본=누적 전체. 리셋(급락)=post-reset 누적값(새 주기 사용분). 그 외(정상 증가 또는
    노이즈/소폭 감소)=부호 그대로의 차이 — 미세 진동(±0.01)은 같은 구간에서 자연 상계된다.
    """
    if prev_v is None:
        return v, False
    if _is_reset(prev_v, v):
        return v, True
    return v - prev_v, False


def _segment_points(points: list, max_gap_minutes: int | None) -> list:
    """오름차순 [(dt, val)]을 연속 세그먼트로 분할한다(공식 스냅샷 이력 정직성, ADR 0007).

    경계: ① 리셋 — val이 직전보다 작으면(누적이 떨어짐 = 청구 리셋) 끊는다. ② 갭 —
    max_gap_minutes가 주어지고 직전 점과의 간격이 그보다 크면(수집 공백) 끊는다.
    빈 구간은 잇지 않는다 — 호출자가 세그먼트별로 그려 갭/리셋을 가로지르지 않게 한다.
    max_gap_minutes=None이면 갭으로는 끊지 않고 리셋으로만 끊는다.
    """
    segments: list = []
    cur: list = []
    prev_dt = None
    prev_val = None
    for dt, val in points:
        if cur:
            reset = _is_reset(prev_val, val)
            gap = (max_gap_minutes is not None
                   and (dt - prev_dt).total_seconds() > max_gap_minutes * 60)
            if reset or gap:
                segments.append(cur)
                cur = []
        cur.append((dt, val))
        prev_dt, prev_val = dt, val
    if cur:
        segments.append(cur)
    return segments


def pool_history(conn, providers: list[str], *, max_gap_minutes: int | None = None,
                 is_pooled=None) -> list:
    """활성 providers의 통합 풀 used(USD) 과거 곡선을 세그먼트 리스트로 반환(ADR 0007).

    각 세그먼트=연속 구간 [{"ts": iso, "used_usd": float}, ...] 오름차순. 전망 차트가
    세그먼트별로 그려 갭/리셋을 가로지르지 않게 한다. USD 풀 기여가 있는 provider만
    합산한다(rate_window-only는 pool_used_history에서 이미 빠짐).

    여러 provider는 서로 다른 시각에 찍히므로 합집합 타임라인에서 각 provider의 가장
    최근 값(≤T)을 forward-fill해 합산한다. forward-fill은 다음을 넘지 않는다:
    ① 어느 provider의 마지막 표본이 max_gap보다 오래됐으면(수집 공백) 그 시점 합산을 생략(끊김),
    ② 어느 provider가 새 세그먼트를 시작하면(리셋/공백 복귀) 그 시점에서 세그먼트를 끊는다.
    max_gap_minutes=None이면 갭으로 끊지 않고 리셋으로만 끊는다.
    """
    contributing = []
    for p in providers:
        hist = pool_used_history(conn, p, is_pooled=is_pooled)
        if hist:
            contributing.append(hist)
    if not contributing:
        return []

    boundaries = set()       # provider별 세그먼트 시작 시각(리셋/공백 복귀 경계)
    union_times: set = set()
    for hist in contributing:
        for seg in _segment_points(hist, max_gap_minutes):
            boundaries.add(seg[0][0])
        for dt, _v in hist:
            union_times.add(dt)
    times = sorted(union_times)
    max_gap_sec = max_gap_minutes * 60 if max_gap_minutes is not None else None

    def _value_at(hist, T):
        """hist에서 T 이하 가장 최근 값. 없거나 max_gap보다 오래되면 None(forward-fill 끊김)."""
        latest = None
        for dt, val in hist:        # hist 오름차순
            if dt <= T:
                latest = (dt, val)
            else:
                break
        if latest is None:
            return None
        if max_gap_sec is not None and (T - latest[0]).total_seconds() > max_gap_sec:
            return None
        return latest[1]

    segments: list = []
    cur: list = []
    for T in times:
        vals = [_value_at(h, T) for h in contributing]
        if any(v is None for v in vals):
            if cur:
                segments.append(cur)
                cur = []
            continue
        if cur and T in boundaries:
            segments.append(cur)
            cur = []
        cur.append({"ts": T.isoformat(), "used_usd": round(sum(vals), 4)})
    if cur:
        segments.append(cur)
    return segments


def _provider_span_spend(hist: list, start: datetime, end: datetime,
                         gap_sec: float | None, *, require_end_boundary: bool = True) -> float | None:
    """한 provider 누적 시계열에서 [start, end] 소비 = consumption_delta 합. 경계 게이트 적용.

    baseline = start 이하 최근 표본(없으면 추적 시작 이전 → None). baseline이 start에서
    gap_sec보다 오래거나(leading gap), 구간 마지막 표본이 end에서 gap_sec보다 오래면
    (trailing gap) None — 경계가 깨끗이 관측됐을 때만 값. 리셋(누적 하락)은 consumption_delta가
    post-reset 값으로 계상해 월 경계 이전 구간도 성립한다.
    require_end_boundary=False면 trailing gap을 무시한다 — end가 "지금"인 진행 중 창(이번달
    소비, ADR 0024)엔 최신 표본이 곧 현재라 tail이 없어 leading gate만 필요하다.
    """
    if not hist:
        return None
    baseline_dt = baseline_v = None
    i, n = 0, len(hist)
    while i < n and hist[i][0] <= start:
        baseline_dt, baseline_v = hist[i]
        i += 1
    if baseline_dt is None:
        return None   # 추적 시작 이전(start 이하 표본 없음)
    if gap_sec is not None and (start - baseline_dt).total_seconds() > gap_sec:
        return None   # start 경계 미관측(leading gap → 부풀림)
    prev = baseline_v
    last_dt = None
    spend = 0.0
    while i < n and hist[i][0] <= end:
        dt, v = hist[i]
        delta, _reset = _consumption_delta(prev, v)
        spend += delta
        prev = v
        last_dt = dt
        i += 1
    if last_dt is None:
        return None   # 구간 내 표본 없음 → end 미관측
    if require_end_boundary and gap_sec is not None and (end - last_dt).total_seconds() > gap_sec:
        return None   # end 경계 미관측(trailing gap → 소비 일부 누락)
    return round(spend, 4)


def official_span_spend(conn, providers: list[str], start: datetime, end: datetime,
                        *, max_gap_minutes: int | None, is_pooled=None,
                        require_end_boundary: bool = True) -> float | None:
    """[start, end] 구간의 통합 풀 공식 소비(USD) — 페이스 신호의 '이전 동일 구간'용(ADR 0017).

    각 provider 스냅샷 이력에서 구간 소비를 consumption_delta 합으로 내고(리셋은 post-reset
    계상 → 월 경계 성립) 풀로 합산한다. 양 경계가 max_gap 내 관측됐을 때만 값 — 어느 provider라도
    경계가 미관측(추적 시작 이전·leading/trailing gap)이면 None(불충분 → 페이스 숨김, 숫자만).
    풀 멤버십은 큐레이션(is_pooled, ADR 0016) — pool_used_history와 동일 스코프.
    """
    if not providers or end <= start:
        return None
    gap_sec = max_gap_minutes * 60 if max_gap_minutes is not None else None
    total = 0.0
    for p in providers:
        s = _provider_span_spend(pool_used_history(conn, p, is_pooled=is_pooled),
                                 start, end, gap_sec, require_end_boundary=require_end_boundary)
        if s is None:
            return None
        total += s
    return round(total, 4)


def this_month_spend(conn, providers: list[str], now_kst: datetime, *,
                     max_gap_minutes: int | None = None, is_pooled=None):
    """이번달 소비(흐름, USD) + 만료형 부분관측 플래그(ADR 0024).

    "지금 얼마 남았나(위치)"가 아니라 "이번 달에 얼마 흘렀나(흐름)"를 낸다.
    [주기형 버킷]=라이브 used(월 리셋이라 누적이 곧 이번달 — 월초 경계 스냅샷 불필요),
    [만료형 버킷]=월초~now 델타(만료일까지 누적돼 라이브 used엔 지난달분이 섞이므로).
    판별은 bucket_kind(주기형=is_pooled_kind). 반환 (usd, expiring_partial):
    expiring_partial=만료형이 풀에 있으나 월초 경계 미관측이라 그 몫이 빠졌는가(카드/공유 캐비엇).
    풀 기여가 전혀 없으면 (None, False).
    """
    ip = _resolve_is_pooled(is_pooled)
    is_periodic = lambda p, rk, bk: ip(p, rk, bk) and is_pooled_kind(bk)         # noqa: E731
    is_expiring = lambda p, rk, bk: ip(p, rk, bk) and not is_pooled_kind(bk)     # noqa: E731
    providers = list(providers)

    # 주기형 이번달 = 라이브 위치(최신 스냅샷) — 월 리셋이라 누적이 곧 이번달. 위치(pool_used)와 동형.
    periodic = _pool_used_latest(conn, providers, is_pooled=is_periodic)
    # 만료형 버킷 있는 provider만(빈 hist가 official_span_spend를 None으로 오판하지 않게).
    exp_providers = [p for p in providers if pool_used_history(conn, p, is_pooled=is_expiring)]
    if periodic is None and not exp_providers:
        return None, False

    total = periodic or 0.0
    partial = False
    if exp_providers:
        month_start, _ = month_bounds(now_kst)
        # 만료형 이번달 델타 = 월초 baseline 대비 소비. trailing gate 없음(end=now=최신 표본).
        expiring = official_span_spend(conn, exp_providers, month_start, now_kst,
                                       max_gap_minutes=max_gap_minutes, is_pooled=is_expiring,
                                       require_end_boundary=False)
        if expiring is not None:
            total += expiring
        else:
            partial = True   # 만료형 있으나 월초 경계 미관측 → 그 몫 미집계
    return round(total, 4), partial


def pool_daily_history(conn, providers: list[str], *, start: datetime, nxt: datetime,
                       is_pooled=None) -> list:
    """[start, nxt) 구간의 날짜별 통합 풀 소비 델타 + 커버리지(ADR 0010).

    각 행 = {date, covered, used_usd, per_provider}. 일별 소비 = 각 provider 누적
    시계열의 인접 누적차 합(첫 표본은 기준 0에서의 누적, 리셋=누적 하락은 post-reset
    값만 계상). 표본 있는 날만 covered=True로 돌려준다.
    """
    start_d = start.astimezone(KST).date()
    nxt_d = nxt.astimezone(KST).date()
    per_prov: dict[str, dict] = {}
    for p in providers:
        daily: dict = {}
        prev = None
        for dt, v in pool_used_history(conn, p, is_pooled=is_pooled):   # (dt, 누적 USD) 오름차순, 소진형 버킷만
            cons, _ = _consumption_delta(prev, v)
            d = dt.astimezone(KST).date()
            daily[d] = daily.get(d, 0.0) + cons
            prev = v
        per_prov[p] = daily

    rows = []
    d = start_d
    while d < nxt_d:
        pp = {p: round(daily[d], 6) for p, daily in per_prov.items() if d in daily}
        if pp:
            rows.append({"date": d, "covered": True,
                         "used_usd": round(sum(pp.values()), 6), "per_provider": pp})
        else:   # 표본 없는 날 — 수집 공백(0 아님, ADR 0007)
            rows.append({"date": d, "covered": False, "used_usd": None, "per_provider": {}})
        d += timedelta(days=1)
    return rows


def pool_hourly_history(conn, providers: list[str], *, day_start: datetime,
                        is_pooled=None) -> list:
    """단일 날짜 [day_start, +1d)의 시각(0~23)별 통합 풀 소비 델타 + 커버리지(ADR 0019).

    `pool_daily_history`의 시간 버전 — 같은 델타 공식(첫 표본=기준 0 누적, 리셋=post-reset,
    노이즈 진동 상계)을 24개 시각 빈으로 분해한다. baseline은 당일 첫 표본 직전 스냅샷
    (전날이어도)을 경계 넘어 carry해 첫 시각 소비를 정확히 잡는다(전체 이력을 돌며 prev를
    갱신하되 당일 표본만 집계). 각 행 = {hour, covered, used_usd, per_provider}. 표본 있는
    시각만 covered=True. 누적선(end_cumulative)은 뷰가 pool_history에서 시각별 last-wins로 뽑는다.
    """
    day_end = day_start + timedelta(days=1)
    per_prov: dict[str, dict] = {}
    for p in providers:
        hourly: dict = {}
        prev = None
        for dt, v in pool_used_history(conn, p, is_pooled=is_pooled):   # (dt, 누적 USD) 오름차순
            cons, _ = _consumption_delta(prev, v)
            kst = dt.astimezone(KST)
            if day_start <= kst < day_end:
                hourly[kst.hour] = hourly.get(kst.hour, 0.0) + cons
            prev = v
        per_prov[p] = hourly

    rows = []
    for h in range(24):
        pp = {p: round(hourly[h], 6) for p, hourly in per_prov.items() if h in hourly}
        if pp:
            rows.append({"hour": h, "covered": True,
                         "used_usd": round(sum(pp.values()), 6), "per_provider": pp})
        else:   # 표본 없는 시각 — 수집 공백(0 아님, ADR 0007/0019)
            rows.append({"hour": h, "covered": False, "used_usd": None, "per_provider": {}})
    return rows


def pool_snapshots_by_day(conn, providers: list[str], *,
                          start: datetime, nxt: datetime, is_pooled=None) -> dict:
    """[start, nxt) 각 날짜의 일 소비를 만든 스냅샷 세부 재구성(ADR 0010 드릴다운).

    `dict[date, list[provider_detail]]` — 표본 있는 날만 키. provider_detail은
    `pool_daily_history`와 **같은 델타 공식**(첫 표본=누적 전체, 리셋=post-reset 값)을
    스냅샷 단위로 분해한다. 그래서 detail의 델타 합 = 그 날 per_provider 일 소비와 일치 →
    "왜 이 숫자인지"를 자기 설명한다. 각 detail:
      provider / first_ever(직전 기준 없음=추적 시작) / baseline({ts,used_usd}|None) /
      gap_days(직전 기준과 당일 첫 표본 사이 일수; ≥2면 갭 흡수) /
      snapshots[{ts,used_usd,delta,reset}] / total_delta.
    기준(baseline)은 당일 첫 표본 직전 스냅샷 — start 이전이어도 무방(경계 day의 기준 보존).
    """
    start_d = start.astimezone(KST).date()
    nxt_d = nxt.astimezone(KST).date()
    by_day: dict = {}
    for p in providers:
        prev_dt = None
        prev_v = None
        for dt, v in pool_used_history(conn, p, is_pooled=is_pooled):   # (dt, 누적 USD) 오름차순, 소진형 버킷만
            d = dt.astimezone(KST).date()
            delta, reset = _consumption_delta(prev_v, v)
            if start_d <= d < nxt_d:
                entry = by_day.setdefault(d, {})
                if p not in entry:
                    first_ever = prev_v is None
                    entry[p] = {
                        "provider": p, "first_ever": first_ever,
                        "baseline": (None if first_ever
                                     else {"ts": prev_dt.isoformat(), "used_usd": round(prev_v, 4)}),
                        "gap_days": (0 if first_ever
                                     else (d - prev_dt.astimezone(KST).date()).days),
                        "snapshots": [], "total_delta": 0.0,
                    }
                pd = entry[p]
                pd["snapshots"].append({"ts": dt.isoformat(), "used_usd": round(v, 4),
                                        "delta": round(delta, 4), "reset": reset})
                pd["total_delta"] = round(pd["total_delta"] + delta, 6)
            prev_dt, prev_v = dt, v
    # provider 순서는 인자 순서 보존(뷰가 _PROVIDER_STYLE 순으로 정렬해 전달)
    return {d: [entry[p] for p in providers if p in entry] for d, entry in by_day.items()}


@dataclass
class PeriodSpend:
    """한 기간(오늘·이번주)의 공식 소비 글랜스(ADR 0011, CONTEXT.md '공식 기간 소비')."""
    usd: float | None                   # 기간 소비 USD. state="none"이면 None
    state: str                          # "complete" | "partial" | "none"
    observed_from: str | None = None    # today partial: 첫 관측 시각 ISO("09:00부터")
    covered_days: int | None = None     # 기간 내 표본 있는 날 수
    total_days: int | None = None       # 기간 총 날 수


@dataclass
class ProviderGlance:
    """provider별 오늘·이번주 글랜스 쌍(ADR 0011)."""
    today: PeriodSpend
    week: PeriodSpend


def _glance_detail(snaps_for_day: list, provider: str) -> dict | None:
    """pool_snapshots_by_day의 하루 detail 리스트에서 provider 항목을 고른다."""
    for d in snaps_for_day:
        if d["provider"] == provider:
            return d
    return None


def _period_spend(rows: list, snaps: dict, provider: str, *, is_today: bool) -> PeriodSpend:
    """pool_daily_history 행 + pool_snapshots_by_day로 한 기간의 PeriodSpend 산출.

    합 = covered 행 used_usd 합(이번주는 주중 갭이 있어도 미관측분이 다음 표본 날에 합산되어
    총량 보존). 상태는 기간 **첫 covered 날의 baseline 신뢰도**로 갈린다: 직전 baseline
    없음(first_ever)이거나 직전이 2일+ 전(gap_days≥2)이면 partial(△ — 갭 흡수로 부풀 수
    있음), 깨끗하면 complete. covered 0이면 none(관측 자체 없음 — "$0"과 구분).
    """
    total_days = len(rows)
    covered = [r for r in rows if r["covered"]]
    if not covered:
        return PeriodSpend(usd=None, state="none", observed_from=None,
                           covered_days=0, total_days=total_days)
    usd = round(sum(r["used_usd"] for r in covered), 4)
    detail = _glance_detail(snaps.get(covered[0]["date"], []), provider)
    state = "complete"
    observed_from = None
    if detail is not None and (detail["first_ever"] or detail["gap_days"] >= 2):
        state = "partial"
        if is_today and detail["snapshots"]:
            observed_from = detail["snapshots"][0]["ts"]
    return PeriodSpend(usd=usd, state=state, observed_from=observed_from,
                       covered_days=len(covered), total_days=total_days)


def official_period_glance(conn, provider: str, now_kst: datetime, *, is_pooled=None) -> ProviderGlance:
    """공식 기간 소비(오늘·이번주) 글랜스(ADR 0011, CONTEXT.md 동명 용어).

    pool_daily_history(소비 숫자)와 pool_snapshots_by_day(갭/추적시작 신호)를 재사용한다 —
    새 델타 경로 없음, "사용 이력(공식)" 화면과 같은 경로라 어긋나지 않는다. 오늘=KST 달력일
    0시~지금, 이번주=월요일 0시 KST~지금. USD 풀 스코프 게이트는 호출자(_provider_card)가
    view.pool_limit_usd로 처리한다(여긴 데이터만).
    """
    now = now_kst.astimezone(KST)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_d = today_start.date()
    week_start = today_start - timedelta(days=today_start.weekday())   # 월요일 0시 KST
    nxt = today_start + timedelta(days=1)

    week_rows = pool_daily_history(conn, [provider], start=week_start, nxt=nxt, is_pooled=is_pooled)
    snaps = pool_snapshots_by_day(conn, [provider], start=week_start, nxt=nxt, is_pooled=is_pooled)
    today_rows = [r for r in week_rows if r["date"] == today_d]

    return ProviderGlance(
        today=_period_spend(today_rows, snaps, provider, is_today=True),
        week=_period_spend(week_rows, snaps, provider, is_today=False),
    )
