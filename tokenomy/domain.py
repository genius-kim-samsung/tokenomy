"""버킷 어휘 저층 — 공식 사용량 도메인의 공유 상수 + pooled 술어.

config(설정)·aggregate(집계)가 **모두 아래로** import하는 leaf다(의존성 0, 역방향
import 0). "어떤 provider를 추적하나 / 어떤 bucket_kind가 풀(주기형 월 한도)이냐"의
답을 한 곳에 둔다 — 옛날엔 이 상수들이 aggregate에 살아 config가 함수-지역 import로
올려다보며 계층을 역전시켰다(그 back-edge를 이 모듈이 없앤다).

bucket_kind 자체의 **생산**은 official_parser(raw dict → OfficialBucket)가 소유하고,
정렬/타이브레이크 규칙(_BUCKET_ORDER·tie_order)은 aggregate 도메인에 남는다 — 이 모듈은
어휘(상수)와 풀 멤버십 술어만 담는 얇고 깊은 leaf다.
"""
from __future__ import annotations

# 합산/탭바가 도는 provider 목록. 4번째 AI 추가 시 여기 + 파서 + 단가만 보강.
PROVIDERS = ("claude", "codex", "gemini")

# 공식 quota(official_fetch) 지원 provider 목록 — 이들만 PROVIDER_SPEC 필수. gemini는
# 로컬 전용(공식 quota는 이번 범위 밖)이라 PROVIDERS에는 있지만 여기엔 없다.
# (OFFICIAL_PROVIDERS ⊆ PROVIDERS.)
OFFICIAL_PROVIDERS = ("claude", "codex")

# 풀 기본 멤버십 = 안정 월 한도 키만(주기형). 만료형 크레딧(event_credit)·promo·
# rate_window는 카탈로그/오버라이드 pooled=True로만 옵트인(ADR 0016·0024).
POOL_DEFAULT_KINDS = ("monthly_limit", "codex_monthly")


def is_pooled_kind(bucket_kind: str) -> bool:
    """bucket_kind가 풀 기본 멤버(주기형 월 한도)인가 — 큐레이션 미주입 시 모양 기본값.

    주기형(월 한도)이면 True, 만료형(크레딧/promo)·rate_window면 False. 큐레이션
    오버라이드는 이 기본값 위에 축별로 얹힌다(config.resolve_bucket_curation).
    """
    return bucket_kind in POOL_DEFAULT_KINDS
