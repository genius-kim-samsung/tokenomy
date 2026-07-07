from tokenomy.parser import UsageRecord
from tokenomy.pricing import (
    CostResult,
    _effective_rate,
    _is_version_boundary,
    apply_pricing_overrides,
    compute_cost,
    find_rate,
    load_pricing,
    pricing_fingerprint,
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
        ts=kw.get("ts", "t"),
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


def test_shipped_config_prices_current_models_correctly():
    # 배포 config가 현행 모델을 공식 단가로 매기고, 전용 항목 덕에 suspect가 아님
    p = load_pricing("config/pricing.json")
    opus = find_rate("claude-opus-4-8", p)
    assert (opus["input"], opus["output"]) == (5.0, 25.0)
    fable = find_rate("claude-fable-5", p)
    assert (fable["input"], fable["output"]) == (10.0, 50.0)
    g55 = find_rate("gpt-5.5", p)
    assert (g55["input"], g55["output"], g55["cache_read"]) == (5.0, 30.0, 0.5)
    # gpt-5.5 전용 항목이 generic 'gpt-5'보다 먼저 매칭 → 버전경계 의심 아님
    assert _is_version_boundary("gpt-5.5", g55["contains"]) is False


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


def test_apply_overrides_adds_new_model_prepended():
    # 기존에 없는 contains 키 → 새 항목으로 추가, 기존보다 앞(prepend)
    pricing = {"match": [
        {"contains": "gpt-5", "provider": "codex", "input": 1.25, "output": 10.0,
         "cache_write": 0.0, "cache_read": 0.125},
    ]}
    out = apply_pricing_overrides(pricing, {
        "gpt-5.5": {"provider": "codex", "input": 2.0, "output": 12.0, "cache_read": 0.2},
    })
    assert out["match"][0]["contains"] == "gpt-5.5"   # prepend
    # 새 항목이 먼저 매칭되어 gpt-5.5가 정확 단가로 잡힌다
    assert find_rate("gpt-5.5", out)["input"] == 2.0
    # 누락 필드는 0.0
    assert out["match"][0]["cache_write"] == 0.0


def test_apply_overrides_new_model_priced_via_compute_cost():
    pricing = {"match": [
        {"contains": "opus", "provider": "claude", "input": 15.0, "output": 75.0,
         "cache_write": 18.75, "cache_read": 1.50},
    ]}
    out = apply_pricing_overrides(pricing, {
        "gpt-5.5": {"provider": "codex", "input": 2.0, "output": 12.0},
    })
    r = compute_cost(_rec("gpt-5.5", output_tokens=1_000_000), out)
    assert r.priced is True
    assert r.cost_usd == 12.0
    assert r.provider == "codex"


def test_fingerprint_stable_for_same_pricing():
    assert pricing_fingerprint(PRICING) == pricing_fingerprint(PRICING)
    # 동일 내용의 새 dict도 같은 해시
    import copy
    assert pricing_fingerprint(PRICING) == pricing_fingerprint(copy.deepcopy(PRICING))


def test_fingerprint_changes_when_rate_changes():
    import copy
    a = pricing_fingerprint(PRICING)
    p2 = copy.deepcopy(PRICING)
    p2["match"][0]["input"] = 5.0          # opus 단가 변경
    assert pricing_fingerprint(p2) != a


def test_fingerprint_changes_when_order_changes():
    # first-match 규칙상 순서가 바뀌면 매칭이 달라질 수 있으므로 해시도 달라야 한다
    import copy
    p2 = copy.deepcopy(PRICING)
    p2["match"].reverse()
    assert pricing_fingerprint(p2) != pricing_fingerprint(PRICING)


def test_fingerprint_ignores_irrelevant_keys():
    # 단가와 무관한 키(주석 등) 변화엔 둔감해야 한다
    import copy
    p2 = copy.deepcopy(PRICING)
    p2["match"][0]["note"] = "주석 추가"
    p2["_meta"] = {"note": "무관"}
    assert pricing_fingerprint(p2) == pricing_fingerprint(PRICING)


def test_fingerprint_reflects_applied_overrides():
    # overrides 적용 결과가 단가를 바꾸면 핑거프린트도 달라진다
    import copy
    base = copy.deepcopy(PRICING)
    a = pricing_fingerprint(base)
    out = apply_pricing_overrides(copy.deepcopy(PRICING), {"opus": {"input": 5.0}})
    assert pricing_fingerprint(out) != a


# --- 날짜유효 단가(dated rates) — Sonnet 5 프로모($2/$10)→표준($3/$15) 전환 ---

DATED = {
    "match": [
        {"contains": "sonnet-5", "provider": "claude", "rates": [
            {"until": "2026-09-01T00:00:00Z", "input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2},
            {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.3},
        ]},
        {"contains": "sonnet", "provider": "claude", "input": 3.0, "output": 15.0,
         "cache_write": 3.75, "cache_read": 0.30},
    ]
}


def test_effective_rate_flat_entry_returned_asis():
    # 날짜구간이 없는 flat 엔트리는 ts와 무관하게 그대로 반환
    flat = DATED["match"][1]
    assert _effective_rate(flat, "2026-08-15T00:00:00Z") is flat
    assert _effective_rate(flat, None) is flat


def test_effective_rate_picks_promo_before_cutover():
    entry = DATED["match"][0]
    r = _effective_rate(entry, "2026-08-15T12:00:00Z")
    assert (r["input"], r["output"]) == (2.0, 10.0)


def test_effective_rate_picks_standard_after_cutover():
    entry = DATED["match"][0]
    r = _effective_rate(entry, "2026-09-15T12:00:00Z")
    assert (r["input"], r["output"]) == (3.0, 15.0)


def test_effective_rate_boundary_is_exclusive():
    # until은 상한 배타(exclusive) — 정각 2026-09-01T00:00Z는 표준
    entry = DATED["match"][0]
    assert _effective_rate(entry, "2026-09-01T00:00:00Z")["input"] == 3.0
    # 1초 전은 프로모
    assert _effective_rate(entry, "2026-08-31T23:59:59Z")["input"] == 2.0


def test_effective_rate_none_ts_defaults_standard():
    # ts 미상 → 개방구간(표준)으로 폴백
    entry = DATED["match"][0]
    assert _effective_rate(entry, None)["input"] == 3.0
    assert _effective_rate(entry, "unparseable")["input"] == 3.0


def test_compute_cost_sonnet5_promo_before_cutover():
    r = compute_cost(_rec("claude-sonnet-5", output_tokens=1_000_000, ts="2026-08-15T12:00:00Z"), DATED)
    assert r.priced is True
    assert r.cost_usd == 10.0   # 프로모 출력 $10/MTok


def test_compute_cost_sonnet5_standard_after_cutover():
    r = compute_cost(_rec("claude-sonnet-5", output_tokens=1_000_000, ts="2026-09-15T12:00:00Z"), DATED)
    assert r.cost_usd == 15.0   # 표준 출력 $15/MTok


def test_compute_cost_sonnet5_promo_1h_cache_tracks_input():
    # 1h 캐시 = 유효 input×2. 프로모 input $2 → 1h 1M = $4
    r = compute_cost(
        _rec("claude-sonnet-5", cache_creation=1_000_000, cache_creation_1h=1_000_000,
             ts="2026-08-15T00:00:00Z"), DATED)
    assert r.cost_usd == 4.0


def test_compute_cost_sonnet4_uses_flat_sonnet_entry():
    # sonnet-4-6은 dated sonnet-5가 아니라 flat 'sonnet' catch-all로 매겨진다(ts 무관)
    r = compute_cost(_rec("claude-sonnet-4-6", output_tokens=1_000_000, ts="2026-08-15T00:00:00Z"), DATED)
    assert r.cost_usd == 15.0


def test_find_rate_prefers_sonnet5_over_generic_sonnet():
    assert find_rate("claude-sonnet-5", DATED)["contains"] == "sonnet-5"
    assert find_rate("claude-sonnet-4-6", DATED)["contains"] == "sonnet"


def test_fingerprint_changes_when_dated_entry_added():
    import copy
    base = {"match": [DATED["match"][1]]}   # flat sonnet만
    a = pricing_fingerprint(base)
    assert pricing_fingerprint(DATED) != a   # dated 엔트리 추가 → 재계산 신호


def test_fingerprint_changes_when_a_dated_rate_changes():
    import copy
    p2 = copy.deepcopy(DATED)
    p2["match"][0]["rates"][0]["input"] = 2.5   # 프로모 단가 변경
    assert pricing_fingerprint(p2) != pricing_fingerprint(DATED)


def test_full_flat_override_replaces_dated_entry():
    # dated 엔트리에 full flat override → escape hatch가 우선(ts 무관 flat)
    import copy
    out = apply_pricing_overrides(copy.deepcopy(DATED), {
        "sonnet-5": {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.1},
    })
    entry = find_rate("claude-sonnet-5", out)
    assert _effective_rate(entry, "2026-08-15T00:00:00Z")["input"] == 1.0
    r = compute_cost(_rec("claude-sonnet-5", output_tokens=1_000_000, ts="2026-08-15T00:00:00Z"), out)
    assert r.cost_usd == 5.0


def test_partial_override_on_dated_entry_ignored_not_crash():
    # 부분 override(일부 필드만)는 dated를 대체하지 않는다 — 4필드 다 차야 flat 전환.
    # 옛 '"input" in entry' 가드면 KeyError(output 부재)였다.
    import copy
    out = apply_pricing_overrides(copy.deepcopy(DATED), {"sonnet-5": {"input": 1.0}})
    entry = find_rate("claude-sonnet-5", out)
    r = compute_cost(_rec("claude-sonnet-5", output_tokens=1_000_000, ts="2026-08-15T00:00:00Z"), out)
    assert r.cost_usd == 10.0   # 프로모 구간 단가 유지(부분 override 무시)


def test_shipped_config_sonnet5_promo_and_standard():
    p = load_pricing("config/pricing.json")
    entry = find_rate("claude-sonnet-5", p)
    assert entry is not None and entry.get("contains") == "sonnet-5"
    promo = compute_cost(_rec("claude-sonnet-5", input_tokens=1_000_000, ts="2026-08-15T00:00:00Z"), p)
    assert promo.cost_usd == 2.0
    std = compute_cost(_rec("claude-sonnet-5", input_tokens=1_000_000, ts="2026-09-15T00:00:00Z"), p)
    assert std.cost_usd == 3.0
