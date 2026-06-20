"""공식 사용량 라이브 취득 — 유일한 아웃바운드 네트워크 모듈(옵트인·비차단).

각 CLI가 로컬에 보관한 OAuth 토큰을 읽기 전용으로 사용해 공식 사용량 API를 단발 호출한다.
토큰 직접 refresh 금지(읽기만). 실패는 예외를 삼켜 fetch_state에 기록하고 마지막 스냅샷을 유지한다.
PII(access_token/account_id/email/user_id)는 절대 DB에 저장하지 않는다 — 헤더에 쓰고 버린다.
사용량 수치만 official_buckets에 적재(파서가 PII 미추출).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from tokenomy import paths
from tokenomy.paths import CLAUDE_CREDS, CODEX_AUTH
from tokenomy.aggregate import parse_ts
from tokenomy.budget import credit_to_usd, official_fetch_settings, tracked_providers
from tokenomy.db import get_fetch_state, insert_official_buckets, upsert_fetch_state
from tokenomy.official_parser import parse_claude, parse_codex

# 공식 사용량 API 엔드포인트
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

# 요청 헤더 상수
_CLAUDE_UA = "claude-code/2.1.179"
_CODEX_UA = "codex_cli_rs"
_CLAUDE_BETA = "oauth-2025-04-20"
_TIMEOUT = 3  # 초(최대 3s, 백오프 없음)


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


# ---------------------------------------------------------------------------
# 취득 결과 + 헬퍼 + 메인 취득 함수
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    """한 provider 취득 결과(표시·로깅용). DB 적재는 fetch_provider가 직접 한다."""
    provider: str
    status: str            # 'ok'|'throttled'|'auth_error'|'http_error'|'disabled'
    note: str | None = None
    bucket_count: int = 0


def _auth_note(provider: str) -> str:
    """provider별 인증 오류 안내 메시지."""
    return ("Codex CLI를 1회 실행해 토큰을 갱신하세요"
            if provider == "codex" else "재로그인이 필요합니다")


def _throttled(state, now_kst, min_interval_minutes: int) -> bool:
    """직전 시도가 min_interval 윈도우 안이면 True(우리 호출 빈도만 제어)."""
    if state is None or state["last_attempt_at"] is None:
        return False
    last = parse_ts(state["last_attempt_at"])
    if last is None:
        return False
    return (now_kst - last).total_seconds() < min_interval_minutes * 60


def _http_get_json(url: str, headers: dict, urlopen) -> dict:
    """HTTP GET → JSON dict. 실패 시 예외를 그대로 전파(호출자가 분류)."""
    req = urllib.request.Request(url, headers=headers)
    with urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_provider(provider: str, *, now_kst, config, conn,
                   urlopen=urllib.request.urlopen) -> FetchResult:
    """공식 사용량 1회 취득(비차단·단발). 결과 FetchResult.

    게이트 순서: env-skip → tracked_providers 미포함 → 크레덴셜 부재 → throttle → GET(≤3s) → 파서 → 적재.
    실패(AuthError/HTTP/네트워크/파싱)는 예외를 삼켜 state에 기록하고 마지막 스냅샷을 유지한다.
    urlopen은 테스트에서 stub 주입(기본 urllib.request.urlopen).
    """
    # 1) 게이트 — env-skip / 미선택 provider / 크레덴셜 부재는 시도 없이 반환
    settings = official_fetch_settings(config)
    if os.environ.get("TOKENOMY_SKIP_OFFICIAL_FETCH"):
        return FetchResult(provider, "disabled", "skip(env)")
    if provider not in tracked_providers(config):
        return FetchResult(provider, "disabled")
    if not paths.creds_present(provider):
        # 선언했지만 로그인 안 된 상태 — 거짓 auth_error를 남기지 않고 조용히 skip
        return FetchResult(provider, "disabled", "creds_absent")

    # 2) throttle — 직전 시도가 윈도우 안이면 state를 갱신하지 않고 반환
    state = get_fetch_state(conn, provider)
    if _throttled(state, now_kst, settings["min_interval_minutes"]):
        return FetchResult(provider, "throttled")

    # 3) 토큰읽기 + 네트워크 + 파싱 — 실패는 여기서만 catch
    ts = now_kst.isoformat()
    try:
        if provider == "claude":
            tok = _read_claude_token(CLAUDE_CREDS)   # 모듈 상수를 명시 전달(패치 가능)
            headers = {"Authorization": f"Bearer {tok}",
                       "anthropic-beta": _CLAUDE_BETA, "User-Agent": _CLAUDE_UA}
            raw = _http_get_json(CLAUDE_USAGE_URL, headers, urlopen)
            buckets = parse_claude(raw, credit_to_usd=credit_to_usd(config))
        else:
            tok, acct = _read_codex_auth(CODEX_AUTH)   # 모듈 상수를 명시 전달(패치 가능)
            headers = {"Authorization": f"Bearer {tok}",
                       "ChatGPT-Account-Id": acct, "User-Agent": _CODEX_UA}
            raw = _http_get_json(CODEX_USAGE_URL, headers, urlopen)
            buckets = parse_codex(raw, credit_to_usd=credit_to_usd(config))
    except AuthError as e:
        upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=None,
                           last_status="auth_error", last_error=str(e)[:200])
        return FetchResult(provider, "auth_error", _auth_note(provider))
    except urllib.error.HTTPError as e:
        # HTTPError는 URLError의 하위클래스 — 반드시 먼저 잡아야 401/5xx 분기가 동작한다
        if e.code == 401:
            upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=None,
                               last_status="auth_error", last_error="HTTP 401")
            return FetchResult(provider, "auth_error", _auth_note(provider))
        upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=None,
                           last_status="http_error", last_error=f"HTTP {e.code}")
        return FetchResult(provider, "http_error")
    except Exception as e:
        # URLError, TimeoutError/socket.timeout, JSON 파싱 등 — 백오프 없이 포기
        upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=None,
                           last_status="http_error", last_error=str(e)[:200])
        return FetchResult(provider, "http_error")

    # 4) 적재 + 성공 state 기록 — try 밖(버그 가려짐 방지)
    n = insert_official_buckets(conn, provider=provider, fetched_at=ts,
                                buckets=buckets, created_at=ts)
    upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=ts,
                       last_status="ok", last_error=None)
    return FetchResult(provider, "ok", bucket_count=n)
