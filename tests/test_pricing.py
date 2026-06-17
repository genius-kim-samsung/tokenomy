from tokenomy.parser import UsageRecord
from tokenomy.pricing import (
    CostResult,
    _is_version_boundary,
    apply_pricing_overrides,
    compute_cost,
    find_rate,
    load_pricing,
)

PRICING = {
    "match": [
        {"contains": "opus", "provider": "claude", "input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
        {"contains": "sonnet", "provider": "claude", "input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    ]
}


def _rec(model, **kw):
    return UsageRecord(
        provider="claude",
        session_id="s",
        cwd="/p",
        ts="t",
        model=model,
        input_tokens=kw.get("input_tokens", 0),
        output_tokens=kw.get("output_tokens", 0),
        cache_creation=kw.get("cache_creation", 0),
        cache_read=kw.get("cache_read", 0),
        cache_creation_1h=kw.get("cache_creation_1h", 0),
    )


def test_input_only_opus():
    r = compute_cost(_rec("claude-opus-4-8", input_tokens=1_000_000), PRICING)
    assert r.priced is True
    assert r.cost_usd == 15.0
    assert r.provider == "claude"


def test_output_cost_higher_than_input():
    r = compute_cost(_rec("claude-opus-4-8", output_tokens=1_000_000), PRICING)
    assert r.cost_usd == 75.0


def test_cache_read_is_cheap():
    # cache_read 1M = $1.50 on opus, far cheaper than fresh input $15
    r = compute_cost(_rec("claude-opus-4-8", cache_read=1_000_000), PRICING)
    assert r.cost_usd == 1.50


def test_cache_write_cost():
    r = compute_cost(_rec("claude-opus-4-8", cache_creation=1_000_000), PRICING)
    assert r.cost_usd == 18.75


def test_cache_creation_1h_costs_double_input():
    # 1시간 캐시 생성 단가 = input 단가 × 2. opus input=15 → 1M 1h = $30
    r = compute_cost(
        _rec("claude-opus-4-8", cache_creation=1_000_000, cache_creation_1h=1_000_000), PRICING
    )
    assert r.cost_usd == 30.0


def test_mixed_cache_creation_5m_and_1h():
    # 1M 중 1h=400k(→ 400k×30/M = 12.0), 5m=600k(→ 600k×18.75/M = 11.25) = 23.25
    r = compute_cost(
        _rec("claude-opus-4-8", cache_creation=1_000_000, cache_creation_1h=400_000), PRICING
    )
    assert r.cost_usd == 23.25


def test_combined_cost():
    r = compute_cost(
        _rec("claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=1_000_000,
             cache_creation=1_000_000, cache_read=1_000_000),
        PRICING,
    )
    # 3 + 15 + 3.75 + 0.30
    assert r.cost_usd == 22.05


def test_unknown_model_not_priced():
    r = compute_cost(_rec("gpt-foo", input_tokens=1_000_000), PRICING)
    assert r.priced is False
    assert r.cost_usd == 0.0
    assert r.provider is None


def test_none_model_not_priced():
    r = compute_cost(_rec(None, input_tokens=1_000_000), PRICING)
    assert r.priced is False


def test_find_rate_substring_match():
    assert find_rate("claude-sonnet-4-6", PRICING)["provider"] == "claude"
    assert find_rate("xxopusxx", PRICING)["input"] == 15.0
    assert find_rate("nope", PRICING) is None


def test_load_pricing_real_config():
    pricing = load_pricing("config/pricing.json")
    assert "match" in pricing
    # opus must be priced in the shipped config
    assert find_rate("claude-opus-4-8", pricing) is not None


def test_apply_overrides_replaces_rate_fields():
    pricing = {"match": [
        {"contains": "opus", "provider": "claude", "input": 15.0, "output": 75.0,
         "cache_write": 18.75, "cache_read": 1.50},
    ]}
    out = apply_pricing_overrides(pricing, {"opus": {"input": 9.0, "output": 36.0}})
    rate = out["match"][0]
    assert rate["input"] == 9.0
    assert rate["output"] == 36.0
    assert rate["cache_read"] == 1.50   # 미지정 필드는 보존


def test_apply_overrides_empty_is_noop():
    pricing = {"match": [{"contains": "opus", "input": 15.0}]}
    out = apply_pricing_overrides(pricing, {})
    assert out["match"][0]["input"] == 15.0


def test_apply_overrides_none_is_noop():
    # 사용자가 config에서 pricing_overrides: null 로 둔 경우
    pricing = {"match": [{"contains": "opus", "input": 15.0}]}
    out = apply_pricing_overrides(pricing, None)
    assert out["match"][0]["input"] == 15.0


def test_version_boundary_suspect_when_digit_or_dot_follows():
    # contains 토큰 직후가 숫자/'.'이면 다음 버전 의심
    assert _is_version_boundary("gpt-5.5", "gpt-5") is True
    assert _is_version_boundary("gpt-4o", "gpt-4") is False   # 'o'는 숫자/'.' 아님
    assert _is_version_boundary("gpt-4.1", "gpt-4") is True


def test_version_boundary_safe_when_separator_or_end_follows():
    assert _is_version_boundary("claude-opus-4-8", "opus") is False  # 직후 '-'
    assert _is_version_boundary("gpt-5", "gpt-5") is False           # 직후 없음(끝)
    assert _is_version_boundary("anything", "missing") is False      # 토큰 부재
