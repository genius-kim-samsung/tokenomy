"""예산/설정 모델.

사용자 config(tokenomy.config.json)에서 provider별 월 예산을 읽는다.
- claude: Claude Code 월 예산 USD
- codex:  Codex CLI 월 예산 USD
config가 없으면 예산 0(추적 전용 모드)으로 동작한다. example 파일은 템플릿일 뿐
자동 로드하지 않는다(사용자가 복사해서 tokenomy.config.json을 만든다).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))


@dataclass
class Budget:
    claude: float
    codex: float

    @property
    def total(self) -> float:
        return self.claude + self.codex

    def limit_for(self, provider: str) -> float:
        return self.claude if provider == "claude" else self.codex

    def weekly_codex_limit(self) -> float:
        """Codex 주간 한도 = 월 한도 ÷ 4 (조직 예산 정책)."""
        return self.codex / 4


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
            "budget": {"claude": 0.0, "codex": 0.0},
            "budget_start": None,
            "pricing_overrides": {}}
    p = _config_path(path)
    if not p.exists():
        return base
    loaded = json.loads(p.read_text(encoding="utf-8"))
    base.update(loaded)                       # top-level 키 덮어쓰기
    base.setdefault("budget", {})
    base["budget"].setdefault("claude", 0.0)  # budget 하위 키 보강
    base["budget"].setdefault("codex", 0.0)
    return base


def save_config(config: dict, path: str | Path | None = None) -> None:
    p = _config_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def budget_from_config(config: dict) -> Budget:
    b = config.get("budget") or {}
    return Budget(claude=float(b.get("claude") or 0), codex=float(b.get("codex") or 0))


def user_label(config: dict) -> str:
    return config.get("user_label") or _default_label()


def budget_start_kst(config: dict) -> datetime | None:
    """config['budget_start']('YYYY-MM-DD')를 KST 자정 datetime으로 파싱.

    빈 문자열·None·형식 오류는 모두 None(미설정)으로 취급한다(하위호환).
    """
    raw = config.get("budget_start")
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=KST)
    except (ValueError, TypeError):
        return None
