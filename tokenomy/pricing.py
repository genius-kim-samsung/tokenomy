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


def load_pricing(path: str | Path = "config/pricing.json") -> dict:
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
    """pricing_overrides({contains: {input/output/...}})로 match[] 단가를 덮어쓴다.

    contains 키가 일치하는 항목의 지정된 단가 필드만 교체한다(미지정 필드 보존).
    """
    if not overrides:
        return pricing
    for entry in pricing.get("match", []):
        ov = overrides.get(entry.get("contains"))
        if ov:
            for k in _OVERRIDABLE:
                if k in ov:
                    entry[k] = ov[k]
    return pricing
