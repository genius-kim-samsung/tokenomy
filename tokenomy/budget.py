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
            "official_fetch": {"min_interval_minutes": 5},
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
    """공식 사용량 자동 취득 설정. 현재는 throttle 간격만 — on/off·provider 게이트는
    tracked_providers가 담당한다. 누락·오설정은 기본 5분으로 폴백한다."""
    raw = config.get("official_fetch") or {}
    try:
        mi = int(raw.get("min_interval_minutes", 5))
    except (TypeError, ValueError):
        mi = 5
    return {"min_interval_minutes": mi if mi > 0 else 5}


def tracked_providers(config: dict) -> list[str]:
    """사용자가 쓴다고 선언한 provider 목록. 없거나 비면 크레덴셜 존재로 시드한다.

    config['tracked_providers']가 유효한 리스트면 PROVIDERS 순서로 정규화(알 수 없는 값 제거).
    비었거나 None이면 크레덴셜 파일이 있는 provider로 시드(무설정 첫 실행이 대개 정답).
    """
    from tokenomy.aggregate import PROVIDERS
    raw = config.get("tracked_providers")
    if isinstance(raw, list):
        sel = [p for p in PROVIDERS if p in raw]
        if sel:
            return sel
    # 빈 리스트·None → 크레덴셜 파일 존재 기반 시드(UI에서 전체 체크 해제 시 자동 복구).
    # 공식 취득 완전 비활성화는 TOKENOMY_SKIP_OFFICIAL_FETCH 환경변수 사용.
    return [p for p in PROVIDERS if creds_present(p)]
