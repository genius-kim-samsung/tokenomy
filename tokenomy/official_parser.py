"""공식 사용량 응답 → 정규화 버킷(순수, 모양 불문, USD 환산).

네트워크/DB 의존 없음 — raw dict만 받아 [OfficialBucket]을 반환한다. enterprise
달러 버킷·개인 구독 % 창을 모두 처리한다. 코드네임(cinder_cove 등)은 회전하므로
키 이름이 아니라 dict 모양으로 분류한다(five_hour/seven_day*만 안정 키로 매칭).

프라이버시: 사용량 수치만 추출. email/user_id/account_id 등 PII는 건드리지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

# 개인 구독 rate-limit 창의 안정 키(코드네임 아님 — 이름 매칭 허용).
_RATE_WINDOW_PREFIXES = ("five_hour", "seven_day")

# seven_day_<model> 접미사 → 표기명(공식 앱 라벨과 동형). 미지/회전 접미사는 타이틀케이스 폴백.
_WINDOW_MODEL_NAMES = {"opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku"}


def _rate_window_label(key: str) -> str:
    """Claude rate-limit 창 키 → 서술 라벨.

    주간 창은 공식 앱과 동형으로 라벨링한다. 단 five_hour는 공식 앱이 "현재 세션"으로
    부르지만, 우리는 창 길이가 곧 드러나는 "5시간 한도"로 표기한다(리셋 시각·잔여를
    분 단위로 병기하므로 "세션"보다 창 길이가 더 actionable하다 — views._reset_with_countdown).
    five_hour/seven_day는 안정 키라 이름 매칭하고, seven_day_<model>의 접미사는
    모델 슬러그로 보아 표기명으로 환산하되 미지/회전 접미사는 타이틀케이스로 폴백한다.
    """
    if key.startswith("five_hour"):
        return "5시간 한도"
    if key == "seven_day":
        return "주간 · 모든 모델"
    if key.startswith("seven_day_"):
        slug = key[len("seven_day_"):]
        name = _WINDOW_MODEL_NAMES.get(slug, slug.replace("_", " ").title())
        return f"주간 · {name} 전용"
    if key.startswith("seven_day"):
        return "주간"
    return "이용률 창"


# Codex rate-limit 창의 안정 키 → 표기. five_hour/seven_day처럼 회전 코드네임이 아니다.
# Claude five_hour와 라벨을 통일한다(같은 5시간 창인데 표현이 갈리지 않게).
_CODEX_WINDOW_LABELS = {"primary_window": "5시간 한도", "secondary_window": "주간 한도"}


def _codex_rate_window_label(key: str, window_seconds) -> str:
    """Codex rate-limit 창 → 서술 라벨.

    공식 앱(Codex CLI/ChatGPT)은 5시간 창을 "현재 세션"이라 부르지만, Claude의 5시간 창
    라벨(_rate_window_label의 "5시간 한도")과 통일해 창 길이가 드러나는 "5시간 한도"·"주간
    한도"로 표기한다. primary_window/secondary_window는 안정 키라 이름 매칭하고, 미지/회전
    키는 응답의 limit_window_seconds로 창 길이를 도출(≤6h→5시간 한도, ≥6d→주간 한도)해
    폴백한다. 둘 다 실패하면 "이용률 창"(코드 라벨일 뿐 — CONTEXT.md의 rate-window 참조).
    """
    label = _CODEX_WINDOW_LABELS.get(key)
    if label:
        return label
    secs = _to_float(window_seconds)
    if secs is not None:
        if secs <= 6 * 3600:
            return "5시간 한도"
        if secs >= 6 * 86400:
            return "주간 한도"
    return "이용률 창"


@dataclass
class OfficialBucket:
    """공식 앱 막대 1개에 대응하는 정규화 버킷. USD는 환산 결과를 함께 보관한다."""
    bucket_key: str          # 안정 논리 id: 'monthly'|'event'|'promo'|'rate_window'
    raw_key: str             # 원 API 키(코드네임/창 이름) — series 보조 분리키
    bucket_kind: str         # 'monthly_limit'|'event_credit'|'promo'|'rate_window'|'codex_monthly'
    label: str               # 서술형 라벨(코드네임 비의존)
    native_unit: str         # 'usd'|'credit'|'percent'
    used_native: float | None
    limit_native: float | None
    remaining_native: float | None
    used_usd: float | None
    limit_usd: float | None
    remaining_usd: float | None
    utilization: float       # 0~100
    resets_at: datetime | None


def _parse_iso(value) -> datetime | None:
    """ISO8601 문자열 → datetime(타임존 보존). 실패 시 None."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_unix(value) -> datetime | None:
    """unix epoch(초) → KST datetime. 실패 시 None."""
    try:
        return datetime.fromtimestamp(float(value), tz=KST)
    except (TypeError, ValueError, OSError):
        return None


def _to_float(value) -> float | None:
    """문자열/숫자 → float. None/실패 시 None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_claude(raw: dict, *, credit_to_usd: float) -> list[OfficialBucket]:
    """Claude enterprise/개인 응답을 버킷으로. native_unit은 enterprise=usd, 개인=percent."""
    out: list[OfficialBucket] = []

    # 1) 월 사용 한도 = spend(used/limit amount_minor) — extra_usage와 같은 버킷.
    spend = raw.get("spend")
    if isinstance(spend, dict) and isinstance(spend.get("limit"), dict):
        lim = spend["limit"]
        used = spend.get("used") or {}  # null used → 0 USD(의도된 폴백)
        exp = lim.get("exponent", 0) or 0
        limit_usd = (lim.get("amount_minor") or 0) / (10 ** exp)
        used_usd = (used.get("amount_minor") or 0) / (10 ** exp)
        util = (used_usd / limit_usd * 100) if limit_usd > 0 else 0.0
        out.append(OfficialBucket(
            bucket_key="monthly", raw_key="spend", bucket_kind="monthly_limit",
            label="사용 한도(Enterprise)", native_unit="usd",
            used_native=used_usd, limit_native=limit_usd,
            remaining_native=round(limit_usd - used_usd, 6),
            used_usd=used_usd, limit_usd=limit_usd,
            remaining_usd=round(limit_usd - used_usd, 6),
            utilization=round(util, 4), resets_at=None,   # 월 경계는 집계에서 계산
        ))

    # 2) 코드네임 dict들 — 모양으로 분류(키 이름 무시).
    for key, val in raw.items():
        if key in ("spend", "extra_usage", "limits") or not isinstance(val, dict):
            continue
        if key.startswith(_RATE_WINDOW_PREFIXES):
            # 개인 구독 % 창
            win_util = val.get("utilization")
            if win_util is None:
                continue
            out.append(OfficialBucket(
                bucket_key="rate_window", raw_key=key, bucket_kind="rate_window",
                label=_rate_window_label(key), native_unit="percent",
                used_native=None, limit_native=None, remaining_native=None,
                used_usd=None, limit_usd=None, remaining_usd=None,
                utilization=round(float(win_util), 4), resets_at=_parse_iso(val.get("resets_at")),
            ))
        elif val.get("used_dollars") is not None and val.get("limit_dollars") is not None:
            # 이벤트 크레딧(일회성, 자체 만료)
            used = float(val["used_dollars"]); limit = float(val["limit_dollars"])
            rem = val.get("remaining_dollars")
            rem = float(rem) if rem is not None else round(limit - used, 6)
            resets = _parse_iso(val.get("resets_at"))
            # 만료일은 라벨이 아니라 sub('만료 YYYY-MM-DD')에 표시 — 다른 게이지의 리셋 위치와 정렬(views).
            out.append(OfficialBucket(
                bucket_key="event", raw_key=key, bucket_kind="event_credit",
                label="일회성 크레딧", native_unit="usd",
                used_native=used, limit_native=limit, remaining_native=rem,
                used_usd=used, limit_usd=limit, remaining_usd=rem,
                utilization=round(used / limit * 100, 4) if limit > 0 else 0.0,
                resets_at=resets,
            ))
        elif val.get("utilization"):   # 0/None이면 생략
            # 별도 프로모션(달러 null, util만)
            out.append(OfficialBucket(
                bucket_key="promo", raw_key=key, bucket_kind="promo",
                label="별도/프로모션", native_unit="percent",
                used_native=None, limit_native=None, remaining_native=None,
                used_usd=None, limit_usd=None, remaining_usd=None,
                utilization=round(float(val["utilization"]), 4),
                resets_at=_parse_iso(val.get("resets_at")),
            ))
    return out


def parse_codex(raw: dict, *, credit_to_usd: float) -> list[OfficialBucket]:
    """Codex enterprise(크레딧→USD 환산)/개인(% 창) 응답을 버킷으로."""
    out: list[OfficialBucket] = []

    sc = raw.get("spend_control")
    indiv = sc.get("individual_limit") if isinstance(sc, dict) else None
    if isinstance(indiv, dict):
        used = _to_float(indiv.get("used")) or 0.0
        limit = _to_float(indiv.get("limit")) or 0.0
        rem = _to_float(indiv.get("remaining"))
        if rem is None:
            rem = round(limit - used, 6)
        out.append(OfficialBucket(
            bucket_key="monthly", raw_key="individual_limit", bucket_kind="codex_monthly",
            label="월간 크레딧 한도", native_unit="credit",
            used_native=used, limit_native=limit, remaining_native=rem,
            used_usd=round(used * credit_to_usd, 6),
            limit_usd=round(limit * credit_to_usd, 6),
            remaining_usd=round(rem * credit_to_usd, 6),  # API의 자체 remaining(부동소수점상 limit-used과 정확히 일치하지 않을 수 있음, 의도적)
            utilization=float(indiv.get("used_percent") or 0),
            resets_at=_parse_unix(indiv.get("reset_at")),
        ))

    rl = raw.get("rate_limit")
    if isinstance(rl, dict):
        for key, val in rl.items():
            if not isinstance(val, dict) or val.get("used_percent") is None:
                continue
            out.append(OfficialBucket(
                bucket_key="rate_window", raw_key=key, bucket_kind="rate_window",
                label=_codex_rate_window_label(key, val.get("limit_window_seconds")),
                native_unit="percent",
                used_native=None, limit_native=None, remaining_native=None,
                used_usd=None, limit_usd=None, remaining_usd=None,
                utilization=round(float(val["used_percent"]), 4),
                resets_at=_parse_unix(val.get("reset_at")),
            ))
    return out
