"""토큰 → 비용(USD) 환산.

config/pricing.json의 match[]를 위에서부터 순회하며 model 문자열에
contains가 부분일치하는 첫 단가를 쓴다. 미일치 모델은 비용 미산정(priced=False).

단가는 공개 API 기준 기본값이다. 청구 단가가 다르면 tokenomy.config.json의
pricing_overrides로 모델별 단가를 덮어쓸 수 있다(코드 변경 불필요).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
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


def compute_cost(record: UsageRecord, pricing: dict) -> CostResult:
    """UsageRecord의 비용을 계산. 캐시 단가를 5m/1h로 분리 반영."""
    rate = find_rate(record.model, pricing)
    if rate is None:
        return CostResult(cost_usd=0.0, priced=False, provider=None)

    # cache_creation 총량에서 1h 분량을 떼어, 1h는 input×2, 나머지(5m)는 cache_write 단가로.
    cache_1h = record.cache_creation_1h
    cache_5m = max(record.cache_creation - cache_1h, 0)

    cost = (
        record.input_tokens * rate["input"]
        + record.output_tokens * rate["output"]
        + cache_5m * rate["cache_write"]
        + cache_1h * rate["input"] * CACHE_CREATE_1H_INPUT_MULTIPLIER
        + record.cache_read * rate["cache_read"]
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
