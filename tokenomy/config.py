"""설정 모델.

사용자 config(tokenomy.config.json)에서 앱 설정을 읽는다.
config가 없으면 기본값으로 동작한다. example 파일은 템플릿일 뿐
자동 로드하지 않는다(사용자가 복사해서 tokenomy.config.json을 만든다).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from tokenomy.paths import creds_present


def _default_label() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or "me"


def _config_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get("TOKENOMY_CONFIG")
    if env:
        return Path(env)
    from tokenomy.paths import config_path
    return config_path()


def load_config(path: str | Path | None = None) -> dict:
    base = {"user_label": _default_label(),
            "tracked_providers": None,           # None → 첫 호출 시 크레덴셜로 시드
            "credit_to_usd": 0.04,
            "official_fetch": {"min_interval_minutes": 10},
            "pricing_overrides": {}}
    p = _config_path(path)
    if not p.exists():
        return base
    loaded = json.loads(p.read_text(encoding="utf-8"))
    base.update(loaded)                       # top-level 키 덮어쓰기
    return base


def save_config(config: dict, path: str | Path | None = None) -> None:
    p = _config_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def user_label(config: dict) -> str:
    return config.get("user_label") or _default_label()


def credit_to_usd(config: dict) -> float:
    """크레딧→USD 환산 단가(크레딧 단위가격, 고정 청구 상수). 모델 무관 단일 상수.

    빈값·음수·비숫자는 모두 기본 0.04로 폴백한다(오설정으로 환산이 깨지지 않게).
    토큰 cost 경로(pricing.json)와 분리 — 여기서만 official 버킷 크레딧 환산에 쓴다.
    """
    raw = config.get("credit_to_usd")
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return 0.04
    return f if f > 0 else 0.04


def official_fetch_settings(config: dict) -> dict:
    """공식 사용량 갱신 설정. min_interval_minutes는 '자동 갱신 간격' — 창이 열린 동안
    갱신을 자동 폴링하는 주기이자 자동 호출의 최소 간격(수동 갱신은 무시한다).
    on/off·provider 게이트는 tracked_providers가 담당한다. 누락·오설정은 기본 10분으로 폴백한다."""
    raw = config.get("official_fetch") or {}
    try:
        mi = int(raw.get("min_interval_minutes", 10))
    except (TypeError, ValueError):
        mi = 10
    return {"min_interval_minutes": mi if mi > 0 else 10}


def tracked_providers(config: dict) -> list[str]:
    """사용자가 앱에서 보기로 켠 provider(=활성 AI) 목록.

    config['tracked_providers']가 리스트면 PROVIDERS 순서로 정규화(알 수 없는 값 제거).
    이때 **빈 리스트는 빈 집합으로 그대로 보존**한다 — "전부 끄기"는 사용자가 명시한
    영속 상태이므로 재시드하지 않는다(다시 켜기 전까지 전 화면에서 모두 숨김).
    키 자체가 없거나 None(미설정)이면 크레덴셜 파일이 있는 provider로 시드한다
    (무설정 첫 실행이 대개 정답). 전부 끄기를 원하면 UI에서 모두 해제하면 된다.
    """
    from tokenomy.aggregate import PROVIDERS
    raw = config.get("tracked_providers")
    if isinstance(raw, list):                          # 명시 설정(빈 리스트 포함) → 그대로 정규화
        return [p for p in PROVIDERS if p in raw]       # [] → [] 영속(재시드 안 함)
    # None(미설정) → 크레덴셜 파일 존재 기반 시드. 공식 취득 전체 차단은 TOKENOMY_SKIP_OFFICIAL_FETCH.
    return [p for p in PROVIDERS if creds_present(p)]
