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


def runtime_path() -> Path:
    """실행 중 인스턴스의 런타임 상태(port/pid). data/ 아래라 .gitignore에 이미 포함."""
    return data_dir() / "data" / "runtime.json"


def resource_path(rel: str) -> Path:
    """번들된 읽기전용 리소스. frozen이면 _MEIPASS, 소스면 repo 루트 기준."""
    base = Path(getattr(sys, "_MEIPASS", _REPO_ROOT))
    return base / rel


# 공식 사용량 취득용 로컬 OAuth 크레덴셜 위치(읽기 전용). 존재 여부 감지에만 쓴다.
CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"
CODEX_AUTH = Path.home() / ".codex" / "auth.json"
GEMINI_CREDS = Path.home() / ".gemini" / "oauth_creds.json"


def creds_present(provider: str) -> bool:
    """provider의 로컬 크레덴셜 파일이 존재하면 True(내용 검증은 안 함)."""
    import tokenomy.paths as _self
    _creds = {"claude": _self.CLAUDE_CREDS, "codex": _self.CODEX_AUTH,
              "gemini": _self.GEMINI_CREDS}
    p = _creds.get(provider)
    return bool(p and p.exists())


def mini_view_available(platform: str | None = None) -> bool:
    """미니 뷰(상주 모드 안의 프레임리스·항상위·절대좌표 글랜스 창)를 이 플랫폼에서 쓸 수 있는가.

    **Windows 전용**(ADR 0013). 미니 뷰는 frameless·on_top·저장된 코너 좌표 배치에 의존하는데,
    Ubuntu 24.04 기본 디스플레이 서버인 Wayland(GNOME 46)는 설계상 앱이 자기 창의 절대 좌표를
    지정하지 못하고 일반 toplevel의 항상위도 막아 핵심 속성이 깨진다. 이 함수가 미니뷰 버튼·전환·
    복원 경로의 단일 게이트(launcher + 웹 사이드바)다. 트레이·큰 창은 Wayland-clean이라 게이트 밖.

    platform=None이면 현재 sys.platform을 본다(테스트는 명시 인자로 분기 검증)."""
    plat = platform if platform is not None else sys.platform
    return plat == "win32"
