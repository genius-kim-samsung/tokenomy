"""토큰 → 비용(USD) 환산.

config/pricing.json의 match[]를 위에서부터 순회하며 model 문자열에
contains가 부분일치하는 첫 단가를 쓴다. 미일치 모델은 비용 미산정(priced=False).

단가는 공개 API 기준 기본값이다. 청구 단가가 다르면 tokenomy.config.json의
pricing_overrides로 모델별 단가를 덮어쓸 수 있다(코드 변경 불필요).

**날짜유효 단가(dated rates).** match 엔트리는 flat 단가 대신 `rates`([구간])를
가질 수 있다 — 공식 단가가 시점에 따라 바뀌는 모델(예: Sonnet 5 출시 프로모
~2026-08-31 후 표준)을 record.ts로 정확히 매긴다. 각 행은 자기 ts의 유효 단가로
한 번 계산돼 캐시되므로(단가표 변화 시에만 reprice), 경계 양쪽이 영구 정확하다.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tokenomy.parser import UsageRecord


@dataclass
class CostResult:
    cost_usd: float
    priced: bool
    provider: str | None


def load_pricing(path: str | Path | None = None) -> dict:
    if path is None:
        from tokenomy.paths import resource_path
        path = resource_path("config/pricing.json")
    return json.loads(Path(path).read_text(encoding="utf-8"))


_FINGERPRINT_FIELDS = ("contains", "provider", "input", "output", "cache_write", "cache_read")


def pricing_fingerprint(pricing: dict) -> str:
    """효과 단가(match[])의 안정적 해시. pricing.json + overrides가 적용된 dict를 받는다.

    이 값이 직전 적재 때와 달라지면 기존 messages.cost_usd가 stale → 전체 재계산 신호.
    match[] 순서는 first-match 규칙상 의미가 있으므로 순서를 보존해 해싱한다(정렬 안 함).
    각 항목은 단가 필드만 정규화해 무관한 키(주석 등) 변화엔 둔감하게 만든다.
    """
    norm = [
        {**{k: entry.get(k) for k in _FINGERPRINT_FIELDS}, "rates": entry.get("rates")}
        for entry in pricing.get("match", [])
    ]
    blob = json.dumps(norm, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def find_rate(model: str | None, pricing: dict) -> dict | None:
    """model 문자열에 contains가 부분일치하는 첫 단가 항목."""
    if not model:
        return None
    for entry in pricing.get("match", []):
        token = entry.get("contains")
        if token and token in model:
            return entry
    return None


def _is_version_boundary(model: str, contains: str) -> bool:
    """매칭된 contains 토큰 직후 문자가 숫자나 '.'이면 버전 경계 의심.

    부분일치가 새 버전을 그럴듯하게 틀리게 잡는 경우를 추정한다
    (예: 'gpt-5' 항목이 'gpt-5.5' 모델을 가로챔). 토큰 직후가 구분자('-')거나
    문자열 끝이면 안전으로 본다(예: 'opus' → 'claude-opus-4-8').
    """
    idx = model.find(contains)
    if idx < 0:
        return False
    nxt = model[idx + len(contains): idx + len(contains) + 1]
    return nxt.isdigit() or nxt == "."


# 1시간 캐시 생성은 input 단가의 2배로 과금된다(5분 캐시는 cache_write 단가).
CACHE_CREATE_1H_INPUT_MULTIPLIER = 2.0


def _parse_ts(ts: str | None) -> datetime | None:
    """ISO 타임스탬프(UTC 가정) → aware datetime. 실패/None은 None.

    parser.kst_day와 동형 규칙 — aggregate.parse_ts를 import하면 pricing→aggregate
    역방향 의존이 생기므로 여기 최소 심으로 둔다.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _effective_rate(entry: dict, ts: str | None) -> dict:
    """match 엔트리의 유효 단가를 고른다.

    - flat 필드(`input` 존재)면 그 엔트리를 그대로(원본 flat이거나 override로 flat 주입).
      pricing_overrides가 dated 모델에 full flat 단가를 주면 escape hatch로 우선한다.
    - 아니면 `rates`([구간])에서 record.ts로 선택 — 이른 구간부터, `until`(상한 배타)이
      없는 마지막이 개방구간. ts 미상/파싱실패는 개방구간(표준)으로 폴백.
    """
    if "input" in entry:
        return entry
    rates = entry.get("rates")
    if not rates:
        return entry
    t = _parse_ts(ts)
    for r in rates:
        until = r.get("until")
        if until is None:
            return r
        u = _parse_ts(until)
        if t is not None and u is not None and t < u:
            return r
    return rates[-1]


def compute_cost(record: UsageRecord, pricing: dict) -> CostResult:
    """UsageRecord의 비용을 계산. 캐시 단가를 5m/1h로 분리 반영."""
    rate = find_rate(record.model, pricing)
    if rate is None:
        return CostResult(cost_usd=0.0, priced=False, provider=None)

    # dated 엔트리는 record.ts로 유효 구간을 해석(flat은 그대로).
    eff = _effective_rate(rate, record.ts)

    # cache_creation 총량에서 1h 분량을 떼어, 1h는 input×2, 나머지(5m)는 cache_write 단가로.
    cache_1h = record.cache_creation_1h
    cache_5m = max(record.cache_creation - cache_1h, 0)

    cost = (
        record.input_tokens * eff["input"]
        + record.output_tokens * eff["output"]
        + cache_5m * eff["cache_write"]
        + cache_1h * eff["input"] * CACHE_CREATE_1H_INPUT_MULTIPLIER
        + record.cache_read * eff["cache_read"]
    ) / 1_000_000

    return CostResult(
        cost_usd=round(cost, 6),
        priced=True,
        provider=rate.get("provider"),
    )


_OVERRIDABLE = ("input", "output", "cache_write", "cache_read")


def apply_pricing_overrides(pricing: dict, overrides: dict | None) -> dict:
    """pricing_overrides({contains: {input/output/...[/provider]}})로 match[]를 보정한다.

    기존 contains 항목은 지정 단가 필드만 교체(미지정 필드 보존). 기존에 없는
    contains 키는 새 항목으로 만들어 match[] 앞에 prepend한다 — find_rate가
    위에서부터 첫 부분일치를 쓰므로, 더 구체적인 사용자 항목이 기존 거친 항목보다
    먼저 매칭된다(예: 'gpt-5.5'가 'gpt-5'를 앞선다). 누락 단가 필드는 0.0.
    """
    if not overrides:
        return pricing
    existing = {e.get("contains") for e in pricing.get("match", [])}
    for entry in pricing.get("match", []):
        ov = overrides.get(entry.get("contains"))
        if ov:
            for k in _OVERRIDABLE:
                if k in ov:
                    entry[k] = ov[k]
    new = [
        {"contains": contains, "provider": ov.get("provider"),
         "input": ov.get("input", 0.0), "output": ov.get("output", 0.0),
         "cache_write": ov.get("cache_write", 0.0), "cache_read": ov.get("cache_read", 0.0)}
        for contains, ov in overrides.items() if contains not in existing
    ]
    pricing["match"] = new + pricing.get("match", [])
    return pricing
