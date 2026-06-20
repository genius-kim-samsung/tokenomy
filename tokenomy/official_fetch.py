"""공식 사용량 라이브 취득 — 유일한 아웃바운드 네트워크 모듈(옵트인·비차단).

각 CLI가 로컬에 보관한 OAuth 토큰을 읽기 전용으로 사용해 공식 사용량 API를 단발 호출한다.
토큰 직접 refresh 금지(읽기만). 실패는 예외를 삼켜 fetch_state에 기록하고 마지막 스냅샷을 유지한다.
PII(access_token/account_id/email/user_id)는 절대 DB에 저장하지 않는다 — 헤더에 쓰고 버린다.
사용량 수치만 official_buckets에 적재(파서가 PII 미추출).
"""
from __future__ import annotations

import json
from pathlib import Path

CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"
CODEX_AUTH = Path.home() / ".codex" / "auth.json"


class AuthError(Exception):
    """크레덴셜 파일 누락·스키마 드리프트·토큰/account_id 부재."""


def _read_claude_token(path: Path = CLAUDE_CREDS) -> str:
    """~/.claude/.credentials.json → claudeAiOauth.accessToken(읽기 전용)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tok = data["claudeAiOauth"]["accessToken"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise AuthError(f"claude credentials: {e}") from e
    if not tok:
        raise AuthError("claude accessToken empty")
    return tok


def _read_codex_auth(path: Path = CODEX_AUTH) -> tuple[str, str]:
    """~/.codex/auth.json → (tokens.access_token, tokens.account_id)(읽기 전용)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tok = data["tokens"]["access_token"]
        acct = data["tokens"]["account_id"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise AuthError(f"codex auth: {e}") from e
    if not tok or not acct:
        raise AuthError("codex token/account_id empty")
    return tok, acct
