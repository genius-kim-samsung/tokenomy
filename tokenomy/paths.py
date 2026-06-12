"""경로 중앙 해석.

두 부류를 구분한다:
- 데이터(쓰기): config/DB/archive — `data_dir()` 아래. exe면 ~/.tokenomy/,
  소스 실행이면 repo 루트(기존 호환), env TOKENOMY_DATA로 전체 오버라이드.
- 리소스(읽기): pricing.json·웹 템플릿/static — `resource_path()`. PyInstaller
  onefile이면 _MEIPASS, 소스면 repo 루트.

입력 로그(~/.claude, ~/.codex)는 대상이 아니다 — 각 parser가 홈에서 직접 읽는다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent  # tokenomy/의 부모 = repo 루트


def data_dir() -> Path:
    env = os.environ.get("TOKENOMY_DATA")
    if env:
        base = Path(env).expanduser()
    elif getattr(sys, "frozen", False):       # PyInstaller exe
        base = Path.home() / ".tokenomy"
    else:                                      # 소스/개발 실행 → repo 루트(기존 호환)
        base = _REPO_ROOT
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    return data_dir() / "data" / "tokenomy.db"


def archive_root() -> Path:
    return data_dir() / "data" / "archive"


def config_path() -> Path:
    return data_dir() / "config" / "tokenomy.config.json"


def resource_path(rel: str) -> Path:
    """번들된 읽기전용 리소스. frozen이면 _MEIPASS, 소스면 repo 루트 기준."""
    base = Path(getattr(sys, "_MEIPASS", _REPO_ROOT))
    return base / rel
