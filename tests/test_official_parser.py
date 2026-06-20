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
    e = kinds["event_credit"]
    assert e.used_usd == 125.0 and e.limit_usd == 500.0
    assert e.bucket_key == "event" and e.raw_key == "cinder_cove"
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
    assert isinstance(b.resets_at, datetime)


def test_codex_personal_rate_windows():
    buckets = parse_codex(_load("codex_personal.json"), credit_to_usd=0.04)
    kinds = {b.raw_key for b in buckets}
    assert kinds == {"primary_window", "secondary_window"}
    for b in buckets:
        assert b.bucket_kind == "rate_window" and b.native_unit == "percent"
        assert b.used_usd is None


def test_no_pii_extracted():
    buckets = parse_codex(_load("codex_enterprise.json"), credit_to_usd=0.04)
    blob = repr(buckets)
    assert "redacted" not in blob       # email/user_id/account_id 미추출
