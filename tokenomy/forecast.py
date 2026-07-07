"""전망(outlook) 조립 — 공식 뷰 팬아웃 + 통합 풀 전망을 좁은 인터페이스 뒤로 접는 모듈.

official_aggregate(순수 계산)·config(설정) 위에 앉는 무순환 조립층이다(둘 다 단방향 import, 역방향 0).
전망 레시피(`[official_view(...) for p in 활성AI] → combined_forecast`)와 그 config 팬아웃이
호출부마다 복붙되던 걸 여기 정본화한다 — 대시보드·전망 페이지·내비·미니가 `outlook(conn, config, now)`
하나만 부른다. 반환(Outlook)은 중간 산물을 버리지 않는다: params(config 해석)·views(공식 뷰
팬아웃)·combined(통합 전망)를 함께 실어, 조립부가 렌더 1회당 팬아웃 1회를 공유한다.
투영 규칙 자체는 official_aggregate.forecast_month_line(정본 1곳)에 산다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from tokenomy.official_aggregate import (
    CombinedForecast, OfficialView, combined_forecast, official_view,
)
from tokenomy.config import (
    bucket_curation_resolver, credit_to_usd, forecast_settings,
    official_fetch_settings, tracked_providers,
)


@dataclass
class FParams:
    """전망 조립에 필요한 config 해석 결과 묶음 — 팬아웃의 단일 소유자.

    active=활성 AI(ADR 0005), ctu=credit_to_usd(크레딧 환산), weeks=트레일링 창(주),
    max_gap=수집 공백 임계(분, 자동 갱신 간격×3), is_pooled=풀 멤버십 predicate(큐레이션, ADR 0016).
    """
    active: list[str]
    ctu: float
    weeks: int
    max_gap: int
    is_pooled: Callable[[str, str, str], bool]


def forecast_params(config: dict) -> FParams:
    """config에서 전망 팬아웃(active/ctu/weeks/max_gap/is_pooled)을 한 번에 해석한다.

    호출부(대시보드·공유·전망 페이지·내비·미니)가 5줄짜리 config 블록을 복붙하던 걸 대체한다.
    is_pooled는 큐레이션 해석기의 pooled 축(표시용 hidden/label과 같은 해석기에서 파생, ADR 0016).
    """
    resolver = bucket_curation_resolver(config)

    def is_pooled(provider: str, raw_key: str, bucket_kind: str) -> bool:
        return resolver(provider, raw_key, bucket_kind)["pooled"]

    return FParams(
        active=tracked_providers(config),
        ctu=credit_to_usd(config),
        weeks=forecast_settings(config)["rate_window_weeks"],
        max_gap=official_fetch_settings(config)["min_interval_minutes"] * 3,
        is_pooled=is_pooled,
    )


@dataclass
class Outlook:
    """전망 조립 결과 한 묶음 — config 해석(params)·공식 뷰 팬아웃(views)·통합 전망(combined).

    views는 활성 AI별 OfficialView(provider→뷰, 활성 순서) — 카드·공유문구가 재사용하는
    중간 산물이라 버리지 않는다. combined는 한도 있는 provider가 하나도 없으면 None(히어로 숨김).
    """
    params: FParams
    views: dict[str, OfficialView]
    combined: CombinedForecast | None


def outlook(conn, config: dict, now: datetime) -> Outlook:
    """통합 풀 월말 전망을 조립한다 — 활성 AI 공식 뷰 팬아웃 → combined_forecast.

    위치도 기울기도 공식 계정 전체(ADR 0015). 팬아웃·config 해석이 이 인터페이스 뒤로 숨고,
    호출부(조립부)는 Outlook 하나로 카드·공유문구·히어로를 재계산 없이 조립한다.
    """
    p = forecast_params(config)
    views = {pr: official_view(conn, pr, now, p.ctu, p.weeks,
                               is_pooled=p.is_pooled, max_gap_minutes=p.max_gap)
             for pr in p.active}
    combined = combined_forecast(conn, list(views.values()), now, p.weeks,
                                 is_pooled=p.is_pooled, max_gap_minutes=p.max_gap)
    return Outlook(params=p, views=views, combined=combined)
