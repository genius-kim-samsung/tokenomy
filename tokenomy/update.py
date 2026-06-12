"""인앱 업데이트 확인 — GitHub Releases 최신 태그 vs 현재 버전.

- 1일 1회만 네트워크 조회(meta.last_update_check)
- 실패/오프라인/타임아웃은 조용히 None(앱 동작 무영향)
- env TOKENOMY_SKIP_UPDATE_CHECK가 설정되면 항상 None(테스트/CI/오프라인)
- 의존성 추가 없음: urllib(stdlib), semver는 튜플 비교
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import date

from tokenomy import __version__

_REPO = "genius-kim-samsung/tokenomy"
RELEASES_API = f"https://api.github.com/repos/{_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{_REPO}/releases/latest"
_CHECK_KEY = "last_update_check"


def _parse_version(v: str) -> tuple[int, ...]:
    v = v.lstrip("vV").split("-")[0].split("+")[0]
    parts: list[int] = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


def is_newer(remote: str, current: str) -> bool:
    return _parse_version(remote) > _parse_version(current)


def _fetch_latest_tag(timeout: float = 3.0) -> str | None:
    try:
        req = urllib.request.Request(
            RELEASES_API,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "tokenomy"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("tag_name")
    except Exception:
        return None


def check_update(conn=None, today: date | None = None) -> str | None:
    """새 버전 태그(예 'v0.2.0')를 반환. 없거나 확인 불가/캐시면 None."""
    if os.environ.get("TOKENOMY_SKIP_UPDATE_CHECK"):
        return None
    today = today or date.today()
    if conn is not None:
        from tokenomy.db import get_meta, set_meta
        if get_meta(conn, _CHECK_KEY) == today.isoformat():
            return None  # 오늘 이미 확인함
        set_meta(conn, _CHECK_KEY, today.isoformat())
    tag = _fetch_latest_tag()
    if tag and is_newer(tag, __version__):
        return tag
    return None
