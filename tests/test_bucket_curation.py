"""버킷 큐레이션 레이어 — 코드네임 버킷 3축 해석(ADR 0016).

resolve_bucket_curation(순수, 우선순위 override>catalog>모양 기본값) + load_bucket_catalog(배포
카탈로그 I/O) + bucket_overrides(로컬) + bucket_curation_resolver(팩토리)를 검증한다.
"""
import json

from tokenomy.config import (
    bucket_curation_resolver,
    bucket_overrides,
    load_bucket_catalog,
    load_config,
    resolve_bucket_curation,
)


# --- resolve_bucket_curation: 모양 기본값(레이어 없음) ---


def test_shape_default_pools_monthly_and_codex():
    # 안정 월 한도 키는 카탈로그 없이도 풀에 포함(pooled=True).
    assert resolve_bucket_curation("claude", "spend", "monthly_limit")["pooled"] is True
    assert resolve_bucket_curation("codex", "individual_limit", "codex_monthly")["pooled"] is True


def test_shape_default_excludes_event_promo_rate():
    # 회전 코드네임 달러 크레딧·프로모·rate_window는 기본 풀 제외(opt-in).
    assert resolve_bucket_curation("claude", "amber_ladder", "event_credit")["pooled"] is False
    assert resolve_bucket_curation("claude", "omelette_promotional", "promo")["pooled"] is False
    assert resolve_bucket_curation("claude", "five_hour", "rate_window")["pooled"] is False


def test_shape_default_visible_and_no_label():
    # 미인식 버킷 기본 = 표시(hidden=False) + parser 라벨 유지(label=None).
    c = resolve_bucket_curation("claude", "amber_ladder", "event_credit")
    assert c["hidden"] is False
    assert c["label"] is None


# --- resolve_bucket_curation: 카탈로그 레이어 ---


def test_catalog_hides_and_excludes():
    cat = {"claude:amber_ladder": {"hidden": True}}
    c = resolve_bucket_curation("claude", "amber_ladder", "event_credit", catalog=cat)
    assert c["hidden"] is True
    assert c["pooled"] is False   # 모양 기본값 유지(미지정 축)


def test_catalog_opts_credit_into_pool():
    cat = {"claude:cinder_cove": {"pooled": True}}
    c = resolve_bucket_curation("claude", "cinder_cove", "event_credit", catalog=cat)
    assert c["pooled"] is True


def test_catalog_relabels():
    cat = {"claude:omelette_promotional": {"label": "Claude Design"}}
    c = resolve_bucket_curation("claude", "omelette_promotional", "promo", catalog=cat)
    assert c["label"] == "Claude Design"


def test_catalog_unknown_key_falls_to_shape_default():
    cat = {"claude:other": {"hidden": True}}
    c = resolve_bucket_curation("claude", "amber_ladder", "event_credit", catalog=cat)
    assert c == {"hidden": False, "pooled": False, "label": None}


# --- resolve_bucket_curation: 우선순위(override > catalog > 모양) ---


def test_override_beats_catalog_per_axis():
    cat = {"claude:x": {"hidden": True, "pooled": True, "label": "Cat"}}
    ov = {"claude:x": {"hidden": False, "label": "Mine"}}   # pooled 미지정 → 카탈로그 유지
    c = resolve_bucket_curation("claude", "x", "event_credit", catalog=cat, overrides=ov)
    assert c == {"hidden": False, "pooled": True, "label": "Mine"}


def test_override_alone_over_shape_default():
    ov = {"claude:amber_ladder": {"hidden": True}}
    c = resolve_bucket_curation("claude", "amber_ladder", "event_credit", overrides=ov)
    assert c["hidden"] is True
    assert c["pooled"] is False


def test_provider_namespaced_key():
    # 키는 provider:raw_key — 다른 provider의 같은 raw_key는 영향 없음.
    cat = {"codex:amber_ladder": {"hidden": True}}
    c = resolve_bucket_curation("claude", "amber_ladder", "event_credit", catalog=cat)
    assert c["hidden"] is False


# --- load_bucket_catalog: 배포 카탈로그 로더 ---


def test_load_bucket_catalog_missing_returns_empty(tmp_path):
    assert load_bucket_catalog(tmp_path / "nope.json") == {}


def test_load_bucket_catalog_reads_wrapped(tmp_path):
    p = tmp_path / "cat.json"
    p.write_text(json.dumps({"_meta": {"note": "x"},
                             "buckets": {"claude:amber_ladder": {"hidden": True}}}),
                 encoding="utf-8")
    assert load_bucket_catalog(p) == {"claude:amber_ladder": {"hidden": True}}


def test_load_bucket_catalog_reads_flat(tmp_path):
    p = tmp_path / "cat.json"
    p.write_text(json.dumps({"claude:amber_ladder": {"hidden": True}}), encoding="utf-8")
    assert load_bucket_catalog(p) == {"claude:amber_ladder": {"hidden": True}}


def test_load_bucket_catalog_malformed_returns_empty(tmp_path):
    p = tmp_path / "cat.json"
    p.write_text("{ not json", encoding="utf-8")
    assert load_bucket_catalog(p) == {}


def test_load_bucket_catalog_non_dict_returns_empty(tmp_path):
    p = tmp_path / "cat.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert load_bucket_catalog(p) == {}


def test_load_bucket_catalog_default_path_uses_resource_path():
    # 인자 없으면 resource_path(config/bucket_catalog.json)을 읽는다(배포 번들).
    from tokenomy.paths import resource_path
    cat = load_bucket_catalog()
    assert isinstance(cat, dict)
    # 시드된 배포 카탈로그가 amber_ladder를 숨김으로 큐레이션한다.
    assert resource_path("config/bucket_catalog.json").exists()


# --- bucket_overrides: 로컬 오버라이드 리더 ---


def test_bucket_overrides_default_empty():
    assert bucket_overrides({}) == {}


def test_bucket_overrides_reads_dict():
    ov = {"claude:x": {"hidden": True}}
    assert bucket_overrides({"bucket_overrides": ov}) == ov


def test_bucket_overrides_non_dict_falls_back():
    assert bucket_overrides({"bucket_overrides": "nope"}) == {}
    assert bucket_overrides({"bucket_overrides": None}) == {}


def test_load_config_default_has_empty_bucket_overrides(tmp_path):
    cfg = load_config(tmp_path / "nope.json")
    assert cfg["bucket_overrides"] == {}


# --- bucket_curation_resolver: 카탈로그+오버라이드 묶음 ---


def test_resolver_applies_override_over_catalog(tmp_path, monkeypatch):
    # 팩토리는 배포 카탈로그(load_bucket_catalog)와 config의 bucket_overrides를 묶는다.
    import tokenomy.config as c
    monkeypatch.setattr(c, "load_bucket_catalog",
                        lambda: {"claude:x": {"hidden": True, "label": "Cat"}})
    resolve = bucket_curation_resolver({"bucket_overrides": {"claude:x": {"label": "Mine"}}})
    out = resolve("claude", "x", "event_credit")
    assert out["hidden"] is True       # 카탈로그
    assert out["label"] == "Mine"      # 오버라이드 우선


def test_resolver_shape_default_when_no_layers(monkeypatch):
    import tokenomy.config as c
    monkeypatch.setattr(c, "load_bucket_catalog", lambda: {})
    resolve = bucket_curation_resolver({})
    assert resolve("claude", "spend", "monthly_limit")["pooled"] is True
    assert resolve("claude", "amber_ladder", "event_credit")["pooled"] is False


# --- 배포 카탈로그 시드 내용 + exe 번들(spec datas) ---


def test_shipped_catalog_seeds_amber_and_omelette():
    # 배포 카탈로그가 경식님 두 케이스를 큐레이션한다: amber_ladder 숨김, omelette → "Claude Design".
    cat = load_bucket_catalog()
    assert cat.get("claude:amber_ladder", {}).get("hidden") is True
    assert cat.get("claude:omelette_promotional", {}).get("label") == "Claude Design"


def test_shipped_catalog_pools_cinder_cove():
    # cinder_cove는 실제로 닳는 $1000 크레딧(유령 천장 아님) — 배포 카탈로그가 풀에 opt-in한다.
    # event_credit 모양 기본값은 pooled=False라, 카탈로그가 없으면 실소비가 통합 풀/전망에서 빠진다.
    cat = load_bucket_catalog()
    assert cat.get("claude:cinder_cove", {}).get("pooled") is True
    resolved = resolve_bucket_curation("claude", "cinder_cove", "event_credit", catalog=cat)
    assert resolved["pooled"] is True
    assert resolved["hidden"] is False    # 게이지는 계속 보임(숨김 아님)


def test_spec_bundles_bucket_catalog():
    # exe 번들 누락 방지(ADR 0016) — tokenomy.spec datas에 bucket_catalog.json이 있어야 한다.
    spec = open("tokenomy.spec", encoding="utf-8").read()
    assert "config/bucket_catalog.json" in spec
