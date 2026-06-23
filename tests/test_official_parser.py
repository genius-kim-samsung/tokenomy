import json
from datetime import datetime
from pathlib import Path

from tokenomy.official_parser import OfficialBucket, parse_claude, parse_codex

FIX = Path(__file__).parent / "fixtures" / "official"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _by_kind(buckets):
    return {b.bucket_kind: b for b in buckets}


def test_claude_enterprise_buckets():
    buckets = parse_claude(_load("claude_enterprise.json"), credit_to_usd=0.04)
    kinds = _by_kind(buckets)
    # 월 사용 한도(spend) + 이벤트 크레딧 + 프로모션(util 0이면 생략 → 여기선 0.0이라 제외)
    assert len(buckets) == 2
    assert "monthly_limit" in kinds
    assert "event_credit" in kinds
    m = kinds["monthly_limit"]
    assert m.native_unit == "usd"
    assert m.used_usd == 30.0          # amount_minor 3000 / 10**2
    assert m.limit_usd == 100.0        # amount_minor 10000 / 10**2
    assert m.bucket_key == "monthly"
    assert m.label == "월간"            # 기간 기반 라벨(구 "사용 한도(Enterprise)")
    e = kinds["event_credit"]
    assert e.used_usd == 125.0 and e.limit_usd == 500.0
    assert e.bucket_key == "event" and e.raw_key == "cinder_cove"
    assert e.label == "이벤트"          # 구 "일회성 크레딧"
    assert isinstance(e.resets_at, datetime)


def test_claude_promo_zero_util_skipped():
    buckets = parse_claude(_load("claude_enterprise.json"), credit_to_usd=0.04)
    assert all(b.bucket_kind != "promo" for b in buckets)  # utilization 0.0 → 생략


def test_claude_rotated_codenames_same_classification():
    buckets = parse_claude(_load("claude_enterprise_rotated.json"), credit_to_usd=0.04)
    kinds = _by_kind(buckets)
    assert "monthly_limit" in kinds and "event_credit" in kinds
    assert kinds["event_credit"].raw_key == "maple_harbor"   # 코드네임 회전에도 분류 동일


def test_claude_personal_rate_windows():
    buckets = parse_claude(_load("claude_personal.json"), credit_to_usd=0.04)
    rw = [b for b in buckets if b.bucket_kind == "rate_window"]
    assert {b.raw_key for b in rw} == {"five_hour", "seven_day", "seven_day_opus"}
    for b in rw:
        assert b.native_unit == "percent"
        assert b.used_usd is None       # % 창은 USD 없음
        assert b.utilization > 0
    # 창별 서술 라벨 — 세 창이 구분되어야 한다(창 길이 표기 "5시간"·"7일(범위)")
    labels = {b.raw_key: b.label for b in rw}
    assert labels == {
        "five_hour": "5시간",
        "seven_day": "7일(All)",
        "seven_day_opus": "7일(Opus)",
    }


def test_claude_rate_window_label_variants():
    # 회전/미지 모델 접미사는 타이틀케이스로 폴백, 알려진 슬러그는 표기명 환산
    raw = {
        "five_hour": {"utilization": 5.0},
        "seven_day": {"utilization": 5.0},
        "seven_day_sonnet": {"utilization": 5.0},
        "seven_day_some_new_model": {"utilization": 5.0},
    }
    labels = {b.raw_key: b.label for b in parse_claude(raw, credit_to_usd=0.04)}
    assert labels["five_hour"] == "5시간"
    assert labels["seven_day"] == "7일(All)"
    assert labels["seven_day_sonnet"] == "7일(Sonnet)"
    assert labels["seven_day_some_new_model"] == "7일(Some New Model)"


def test_codex_enterprise_credit_to_usd():
    buckets = parse_codex(_load("codex_enterprise.json"), credit_to_usd=0.04)
    assert len(buckets) == 1
    b = buckets[0]
    assert b.bucket_kind == "codex_monthly" and b.bucket_key == "monthly"
    assert b.native_unit == "credit"
    assert b.used_native == 500.0 and b.limit_native == 2000.0
    assert b.used_usd == 20.0           # 500 * 0.04
    assert b.limit_usd == 80.0          # 2000 * 0.04
    assert b.utilization == 25
    assert b.label == "월간"            # 구 "월간 크레딧 한도"
    assert isinstance(b.resets_at, datetime)


def test_codex_personal_rate_windows():
    buckets = parse_codex(_load("codex_personal.json"), credit_to_usd=0.04)
    by_key = {b.raw_key: b for b in buckets}
    # Claude 5시간 창 라벨과 통일: 5시간 창=5시간, 7일 창=7일(All).
    assert {k: b.label for k, b in by_key.items()} == {
        "primary_window": "5시간", "secondary_window": "7일(All)"}
    for b in buckets:
        assert b.bucket_kind == "rate_window" and b.native_unit == "percent"
        assert b.used_usd is None
    # 실제 API 키 reset_at(unix) → resets_at(datetime)으로 채워져야 카드에 카운트다운이 뜬다.
    assert isinstance(by_key["primary_window"].resets_at, datetime)
    assert isinstance(by_key["secondary_window"].resets_at, datetime)


def test_codex_rate_window_label_variants():
    # 안정 키 매칭 우선, 미지 키는 limit_window_seconds로 길이 도출, 둘 다 없으면 폴백.
    raw = {"rate_limit": {
        "primary_window": {"used_percent": 5.0, "limit_window_seconds": 18000},
        "secondary_window": {"used_percent": 5.0, "limit_window_seconds": 604800},
        "mystery_short": {"used_percent": 5.0, "limit_window_seconds": 3600},      # ≤6h
        "mystery_long": {"used_percent": 5.0, "limit_window_seconds": 1209600},    # ≥6d(14일)
        "no_window": {"used_percent": 5.0},                                       # 폴백
    }}
    labels = {b.raw_key: b.label for b in parse_codex(raw, credit_to_usd=0.04)}
    assert labels["primary_window"] == "5시간"
    assert labels["secondary_window"] == "7일(All)"
    assert labels["mystery_short"] == "5시간"
    assert labels["mystery_long"] == "7일(All)"
    assert labels["no_window"] == "이용률 창"


def test_no_pii_extracted():
    buckets = parse_codex(_load("codex_enterprise.json"), credit_to_usd=0.04)
    blob = repr(buckets)
    assert "redacted" not in blob       # email/user_id/account_id 미추출
