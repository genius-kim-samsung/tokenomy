"""버킷 어휘 저층 모듈(domain.py) — 공유 상수 + pooled 술어.

domain.py는 config·aggregate가 모두 아래로 import하는 leaf다(역방향 import 0).
버킷 kind가 풀(주기형 월 한도)인지 만료형(크레딧/promo/rate_window)인지의
판정을 단일 소유한다(ADR 0016·0024).
"""
from pathlib import Path

import tokenomy.config
from tokenomy.domain import PROVIDERS, POOL_DEFAULT_KINDS, is_pooled_kind


def test_providers_is_claude_and_codex():
    assert PROVIDERS == ("claude", "codex")


def test_pool_default_kinds_are_periodic_monthly_limits():
    # 풀 기본값 = 안정 월 한도 키만(회전 코드네임 달러 크레딧은 opt-in 제외).
    assert POOL_DEFAULT_KINDS == ("monthly_limit", "codex_monthly")


def test_is_pooled_kind_true_for_periodic_monthly_limits():
    assert is_pooled_kind("monthly_limit") is True
    assert is_pooled_kind("codex_monthly") is True


def test_is_pooled_kind_false_for_expiring_and_rate_window():
    # 만료형 크레딧·promo·rate_window는 풀 기본값에서 빠진다(opt-in만).
    assert is_pooled_kind("event_credit") is False
    assert is_pooled_kind("promo") is False
    assert is_pooled_kind("rate_window") is False


def test_is_pooled_kind_false_for_unknown_kind():
    assert is_pooled_kind("something_new") is False


def test_config_has_no_back_edge_to_aggregate():
    # 계층 규약: config(설정 leaf)는 aggregate(상위 도메인)를 import하지 않는다.
    # module-level·함수-지역 어느 쪽이든 back-edge — 소스 문자열로 둘 다 막는다.
    src = Path(tokenomy.config.__file__).read_text(encoding="utf-8")
    assert "from tokenomy.aggregate" not in src
    assert "import tokenomy.aggregate" not in src
