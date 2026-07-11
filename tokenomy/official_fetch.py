"""공식 사용량 라이브 취득 — 유일한 아웃바운드 네트워크 모듈(옵트인·비차단).

각 CLI가 로컬에 보관한 OAuth 토큰을 사용해 공식 사용량 API를 단발 호출한다(사용량 조회 GET 자체는
읽기 전용). 단, **Claude(ADR 0021)·Codex(ADR 0022) 두 access token 모두** 만료 임박 시 조건부
능동 refresh + 토큰 파일 atomic write-back을 수행한다(refresh_claude_token / refresh_codex_token).
실패는 예외를 삼켜 fetch_state에 기록하고 마지막 스냅샷을 유지한다.
PII(access_token/account_id/email/user_id)는 절대 DB에 저장하지 않는다 — 헤더에 쓰고 버린다.
사용량 수치만 official_buckets에 적재(파서가 PII 미추출).
"""
from __future__ import annotations

import base64
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from tokenomy import paths
from tokenomy.atomicio import atomic_write_json
from tokenomy.paths import CLAUDE_CREDS, CODEX_AUTH, GEMINI_CREDS
from tokenomy.clock import parse_ts
from tokenomy.official_aggregate import official_view
from tokenomy.config import (
    account_mode, bucket_curation_resolver, credit_to_usd, official_fetch_settings,
    seed_account_mode, tracked_providers,
)
from tokenomy.db import (
    get_fetch_state, insert_official_buckets, insert_official_raw, last_provider_activity_ts,
    upsert_fetch_state,
)
from tokenomy.official_parser import parse_claude, parse_codex

# 공식 사용량 API 엔드포인트
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

# 요청 헤더 상수
_CLAUDE_UA = "claude-code/2.1.179"
_CODEX_UA = "codex_cli_rs"
_CLAUDE_BETA = "oauth-2025-04-20"
_TIMEOUT = 3  # 초(최대 3s, 백오프 없음)

# OAuth refresh(능동 갱신) — Claude(ADR 0021)·Codex(ADR 0022). console.*는 Cloudflare 403이라 api.* 사용.
_CLAUDE_REFRESH_URL = "https://api.anthropic.com/v1/oauth/token"
_CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_CODEX_REFRESH_URL = "https://auth.openai.com/oauth/token"
_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_GEMINI_REFRESH_URL = "https://oauth2.googleapis.com/token"
# gemini-cli installed-app 공개 client(소스에 공개 — 조사 Q4). Google refresh_token은 비회전·장수명.
_GEMINI_CLIENT_ID = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
_GEMINI_CLIENT_SECRET = "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"
_GEMINI_UA = "gemini-cli/0.50.0"
_GEMINI_ENDPOINT = "https://cloudcode-pa.googleapis.com/v1internal"
_REFRESH_TIMEOUT = 10  # 초(백오프 없음)
_PREEMPT_MS = 5 * 60 * 1000  # 만료 5분 전이면 선제 갱신

# 토큰 read→refresh→write 구간 직렬화(같은 프로세스 내 진입점 3곳의 self-race 방지).
_token_lock = threading.Lock()


class AuthError(Exception):
    """크레덴셜 파일 누락·스키마 드리프트·토큰/account_id 부재."""


# raw 응답을 디버그 보관하기 전에 가리는 PII 키(deny-list, ADR 0014).
# Codex 응답이 최상위에 user_id/account_id/email를 담는다 — 사용량 수치는 보존하고 이것만 마스킹.
_PII_KEYS = frozenset({"user_id", "account_id", "email"})


def scrub_pii(obj):
    """deny-list 키의 값을 재귀적으로 '[redacted]'로 치환한 **새 객체**를 반환(원본 불변).

    dict/list를 따라 내려가며 _PII_KEYS에 든 키만 가린다. 키 이름 매칭이라 모양이
    바뀌어도(코드네임 회전) 사용량 수치는 그대로 남고 알려진 PII만 사라진다.
    """
    if isinstance(obj, dict):
        return {k: ("[redacted]" if k in _PII_KEYS else scrub_pii(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_pii(v) for v in obj]
    return obj


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


def _read_gemini_token(path: Path = GEMINI_CREDS) -> str:
    """~/.gemini/oauth_creds.json → access_token(읽기 전용)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tok = data["access_token"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise AuthError(f"gemini credentials: {e}") from e
    if not tok:
        raise AuthError("gemini access_token empty")
    return tok


def _jwt_exp_ms(token: str) -> int | None:
    """JWT의 `exp` 클레임(초)을 ms로 반환(ADR 0022). 서명 검증은 안 한다 — 발급자가 아니라
    "곧 만료?"만 판정하므로 페이로드(가운데 세그먼트)만 base64url 디코드한다.

    Codex `auth.json`엔 Claude의 평문 `expiresAt` 같은 만료 필드가 없어 access_token JWT에서
    읽는다. 토큰이 JWT가 아니거나 exp가 없으면 None(상위가 선제 갱신을 건너뛰고 401 반응형에 맡김).
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)               # base64 패딩 복원
        exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
        return int(exp) * 1000
    except (IndexError, ValueError, KeyError, TypeError):
        return None


def _atomic_write_json(path: Path, data) -> bool:
    """data(dict)를 path에 원자적·0600으로 기록(ADR 0021/0022 토큰 write-back 공용).

    실제 쓰기는 공용 `atomicio.atomic_write_json`(고유 temp→원자 replace·PermissionError
    재시도)에 위임하되, 토큰이 담긴 파일이라 평문 권한 노출을 막으려 0600으로 생성한다
    (POSIX 권한; Windows는 상위 디렉터리 ACL 상속). 성공 True, 실패(OSError) 시 False를
    반환하며 **원본은 건드리지 않는다**(무손상 폴백 — raise를 bool로 감싸는 셸).
    """
    try:
        atomic_write_json(path, data, perms=0o600)
    except OSError:
        return False
    return True


def _iso_z(now_ms: int) -> str:
    """ms epoch → RFC3339 UTC(Z) 문자열. Codex `auth.json`의 `last_refresh` 형식에 맞춘다."""
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000000000Z")


def _json_body(payload: dict) -> bytes:
    """기본 refresh body 인코더 — JSON(Claude/Codex, 기존 동작 그대로)."""
    return json.dumps(payload).encode("utf-8")


def _form_body(payload: dict) -> bytes:
    """form-urlencoded refresh body 인코더 — Google OAuth 토큰 엔드포인트(gemini)."""
    return urllib.parse.urlencode(payload).encode("utf-8")


def _refresh_oauth(path: Path, *, now_ms: int, urlopen, read_refresh_token: Callable,
                   apply_response: Callable, refresh_url: str, client_id: str,
                   headers: dict, body_extra: dict, body_encoder: Callable = _json_body) -> str | None:
    """OAuth refresh 공통 엔진 — refresh token으로 새 access token을 받아 atomic write-back(ADR 0021/0022).

    골격: 파일 읽기 → refresh_token 추출·가드 → POST(json body, _REFRESH_TIMEOUT) → access_token
    파싱 → write-back 적용 → _atomic_write_json → 새 access token 반환. 모든 실패는 None이고 **원본
    파일은 절대 건드리지 않는다**(무손상 폴백). provider 차분(refresh_token 위치·응답 반영·URL·
    client_id·헤더·body scope)은 콜백/인자로 주입한다. rotation으로 새 refresh_token이 오면
    apply_response가 반드시 기록한다(안 쓰면 다음 갱신이 죽은 토큰으로 실패).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rt = read_refresh_token(data)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not rt:
        return None

    body = body_encoder({"grant_type": "refresh_token", "refresh_token": rt,
                         "client_id": client_id, **body_extra})
    req = urllib.request.Request(refresh_url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=_REFRESH_TIMEOUT) as resp:
            text = resp.read().decode("utf-8")
    except Exception:
        return None  # 비200/네트워크/타임아웃 — 원본 불변

    try:
        r = json.loads(text)
        at = r["access_token"]
    except (ValueError, KeyError, TypeError):
        return None
    if not at:
        return None

    apply_response(data, r, int(now_ms))
    if not _atomic_write_json(path, data):
        return None
    return at


def _claude_read_refresh_token(data) -> str | None:
    """Claude 크레덴셜에서 refresh token 추출(claudeAiOauth.refreshToken)."""
    return data["claudeAiOauth"]["refreshToken"]


def _claude_apply_response(data, resp, now_ms: int) -> None:
    """Claude refresh 응답 반영 — 토큰 3종만 갱신, 기존 키 보존."""
    o = data["claudeAiOauth"]
    o["accessToken"] = resp["access_token"]
    nrt = resp.get("refresh_token")
    if nrt:
        o["refreshToken"] = nrt
    exp_in = resp.get("expires_in")
    if exp_in:
        o["expiresAt"] = now_ms + int(exp_in) * 1000


def refresh_claude_token(path: Path = CLAUDE_CREDS, *, now_ms: int,
                         urlopen=urllib.request.urlopen) -> str | None:
    """refresh token으로 새 access token을 받아 .credentials.json에 atomic write-back(ADR 0021).

    성공 시 새 accessToken을 반환하고, 실패(파일/네트워크/스키마)는 None을 반환하며 **원본 파일을
    절대 건드리지 않는다**(무손상 폴백). 토큰은 파일에만 쓰고 DB에는 저장하지 않는다.
    rotation으로 새 refresh_token이 오면 반드시 기록한다(안 쓰면 다음 갱신이 죽은 토큰으로 실패).
    """
    return _refresh_oauth(
        path, now_ms=now_ms, urlopen=urlopen,
        read_refresh_token=_claude_read_refresh_token, apply_response=_claude_apply_response,
        refresh_url=_CLAUDE_REFRESH_URL, client_id=_CLAUDE_CLIENT_ID,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": _CLAUDE_UA, "anthropic-beta": _CLAUDE_BETA},
        body_extra={})


def _codex_read_refresh_token(data) -> str | None:
    """Codex auth에서 refresh token 추출 — API-key 모드(auth_mode != chatgpt)면 None(갱신 대상 아님)."""
    if data.get("auth_mode") not in (None, "chatgpt"):
        return None
    return data["tokens"]["refresh_token"]


def _codex_apply_response(data, resp, now_ms: int) -> None:
    """Codex refresh 응답 반영 — access/refresh/id 토큰만 갱신, account_id 등 보존·last_refresh 갱신."""
    toks = data["tokens"]
    toks["access_token"] = resp["access_token"]
    nrt = resp.get("refresh_token")
    if nrt:
        toks["refresh_token"] = nrt
    nid = resp.get("id_token")
    if nid:
        toks["id_token"] = nid
    data["last_refresh"] = _iso_z(now_ms)


def refresh_codex_token(path: Path = CODEX_AUTH, *, now_ms: int,
                        urlopen=urllib.request.urlopen) -> str | None:
    """Codex refresh token으로 새 access token을 받아 auth.json에 atomic write-back(ADR 0022).

    성공 시 새 access_token을 반환하고, 실패(파일/네트워크/스키마)나 비-OAuth 모드는 None을 반환하며
    **원본 파일을 절대 건드리지 않는다**(무손상 폴백). rotation으로 매번 새 refresh_token이 오므로
    반드시 기록한다(누락 시 다음 갱신이 죽은 토큰으로 실패). account_id는 같은 로그인에서 불변이라
    보존(재유도 안 함). API-key 모드(`auth_mode != chatgpt`)나 refresh_token 부재면 갱신할 OAuth
    토큰이 없어 None. 토큰은 파일에만 쓰고 DB에는 저장하지 않는다.
    """
    return _refresh_oauth(
        path, now_ms=now_ms, urlopen=urlopen,
        read_refresh_token=_codex_read_refresh_token, apply_response=_codex_apply_response,
        refresh_url=_CODEX_REFRESH_URL, client_id=_CODEX_CLIENT_ID,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": _CODEX_UA},
        body_extra={"scope": "openid profile email"})


def _gemini_read_refresh_token(data) -> str | None:
    """Gemini creds에서 refresh token 추출(oauth_creds.json top-level refresh_token)."""
    return data.get("refresh_token")


def _gemini_apply_response(data, resp, now_ms: int) -> None:
    """Gemini refresh 응답 반영 — access_token·expiry_date 갱신, refresh_token은 오면 기록(비회전이라 보통 불변)."""
    data["access_token"] = resp["access_token"]
    nrt = resp.get("refresh_token")
    if nrt:
        data["refresh_token"] = nrt
    exp_in = resp.get("expires_in")
    if exp_in:
        data["expiry_date"] = now_ms + int(exp_in) * 1000    # ms epoch(Claude expiresAt과 동형)


def refresh_gemini_token(path: Path = GEMINI_CREDS, *, now_ms: int,
                         urlopen=urllib.request.urlopen) -> str | None:
    """refresh token으로 새 access token을 받아 oauth_creds.json에 atomic write-back(ADR 0021/0022 gemini 확장).

    Google 토큰 엔드포인트는 **form-urlencoded + client_secret**이라 body_encoder=_form_body로 위임한다.
    성공 시 새 access_token 반환, 실패(파일/네트워크/스키마)나 refresh_token 부재는 None + 원본 불변.
    Google refresh_token은 비회전·장수명이라 write-back 부담이 Codex(회전)보다 작다.
    """
    return _refresh_oauth(
        path, now_ms=now_ms, urlopen=urlopen,
        read_refresh_token=_gemini_read_refresh_token, apply_response=_gemini_apply_response,
        refresh_url=_GEMINI_REFRESH_URL, client_id=_GEMINI_CLIENT_ID,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        body_extra={"client_secret": _GEMINI_CLIENT_SECRET},
        body_encoder=_form_body)


def _auto_refresh_allowed(config, conn, now, mode: str, provider: str = "claude",
                          *, token_expired: bool = False) -> bool:
    """자동 갱신 안전망(ADR 0021/0022). off=불허, always=허용, auto=최근 *해당 provider* 활동 없을 때만 허용.

    auto는 이 기기의 마지막 provider 메시지 ts가 safety_hours 이내면 '그 CLI를 쓰는 기기'로 보고
    skip한다(실행 중 CLI의 메모리 토큰과 충돌 회피). 활동 없음/파싱 불가는 허용(=CLI 안 쓰는 기기로
    간주). provider별 활동 판정이라 claude·codex가 한 기기에서 섞여도 각각 옳게 게이팅된다(ADR 0022).

    단, token_expired=True(이미 만료 확인)면 auto에서도 활동 판정을 생략하고 허용한다(ADR 0021 개정)
    — 죽은 토큰은 실행 중 CLI도 못 쓰므로 rotation 충돌로 보호할 대상이 없고, 이 우회가 없으면
    마지막 CLI 사용 후 TTL(8h)~safety_hours(24h) 사이에 갱신 불가 블랙아웃이 생긴다.
    """
    if mode == "off":
        return False
    if mode == "always":
        return True
    if token_expired:
        return True
    last = last_provider_activity_ts(conn, provider)
    if last is None:
        return True
    dt = parse_ts(last)
    if dt is None:
        return True
    hours = official_fetch_settings(config)["auto_refresh_safety_hours"]
    return (now - dt).total_seconds() >= hours * 3600


def _claude_expiry_ms(path: Path) -> int | None:
    """Claude 크레덴셜의 평문 expiresAt(ms). 파일/스키마 오류면 None(선제 갱신 skip)."""
    try:
        return int(json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]["expiresAt"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _codex_expiry_ms(path: Path) -> int | None:
    """Codex access token JWT의 exp(ms). 평문 만료 필드가 없어 JWT에서 읽는다(_jwt_exp_ms). 실패면 None."""
    try:
        return _jwt_exp_ms(json.loads(path.read_text(encoding="utf-8"))["tokens"]["access_token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _gemini_expiry_ms(path: Path) -> int | None:
    """Gemini creds의 expiry_date(ms epoch, 직접 읽기 — JWT 디코드 불필요). 오류면 None."""
    try:
        return int(json.loads(path.read_text(encoding="utf-8"))["expiry_date"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _ensure_fresh(config, conn, *, now, path: Path, urlopen, provider: str,
                  read_expiry_ms: Callable, refresh: Callable, read_final: Callable):
    """선제 갱신 공통 골격 — 모드 게이트 → 만료 판정 → _PREEMPT_MS·안전망 → 락 안 double-check
    → refresh → 최종 파일 읽기 반환(ADR 0021/0022).

    만료 5분 이내(_PREEMPT_MS) + 안전망 허용이면 락 안에서 double-check 후 refresh한다. 이미
    만료된 토큰(exp ≤ now)은 안전망의 활동 판정을 우회한다(token_expired — ADR 0021 개정). 어떤
    경우든 마지막엔 read_final(path)로 파일의 현재(혹은 갱신된) 값을 읽어 돌려준다(갱신 실패해도
    기존 토큰으로 진행 → 상위가 401 폴백). provider 차분은 read_expiry_ms(만료 판정)·refresh·
    read_final(최종 읽기)·provider(안전망 판정)로 주입한다.
    """
    mode = official_fetch_settings(config)["auto_refresh_token"]
    if mode != "off":
        now_ms = int(now.timestamp() * 1000)
        exp = read_expiry_ms(path)
        if exp is not None and exp - now_ms < _PREEMPT_MS and _auto_refresh_allowed(
                config, conn, now, mode, provider, token_expired=exp <= now_ms):
            with _token_lock:
                exp2 = read_expiry_ms(path)           # double-check — 다른 스레드가 그새 갱신했으면 skip
                if exp2 is not None and exp2 - int(now.timestamp() * 1000) < _PREEMPT_MS:
                    refresh(path, now_ms=int(now.timestamp() * 1000), urlopen=urlopen)
    return read_final(path)


def ensure_fresh_claude_token(config, conn, *, now, path: Path = CLAUDE_CREDS,
                              urlopen=urllib.request.urlopen) -> str:
    """필요 시 Claude access token을 선제 갱신하고 현재(혹은 갱신된) accessToken을 반환(ADR 0021).

    만료 5분 이내 + 안전망 허용이면 락 안에서 double-check 후 refresh한다. 어떤 경우든 마지막엔
    파일의 현재 accessToken을 읽어 돌려준다(갱신 실패해도 기존 토큰으로 진행 → 상위가 401 폴백).
    """
    return _ensure_fresh(config, conn, now=now, path=path, urlopen=urlopen, provider="claude",
                         read_expiry_ms=_claude_expiry_ms, refresh=refresh_claude_token,
                         read_final=_read_claude_token)


def ensure_fresh_gemini_token(config, conn, *, now, path: Path = GEMINI_CREDS,
                              urlopen=urllib.request.urlopen) -> str:
    """필요 시 Gemini access token을 선제 갱신하고 현재(혹은 갱신된) access_token을 반환(ADR 0021/0022 gemini 확장).

    만료 5분 이내 + 안전망 허용이면 락 안 double-check 후 refresh(Claude str-토큰 경로 미러).
    만료 판정은 expiry_date(ms) 직접. 갱신 실패해도 기존 토큰으로 진행 → 상위가 401 폴백.
    """
    return _ensure_fresh(config, conn, now=now, path=path, urlopen=urlopen, provider="gemini",
                         read_expiry_ms=_gemini_expiry_ms, refresh=refresh_gemini_token,
                         read_final=_read_gemini_token)


def ensure_fresh_codex_token(config, conn, *, now, path: Path = CODEX_AUTH,
                             urlopen=urllib.request.urlopen) -> tuple[str, str]:
    """필요 시 Codex access token을 선제 갱신하고 (access_token, account_id)를 반환(ADR 0022).

    Codex엔 평문 만료 필드가 없어 access_token JWT의 exp로 판정한다(`_jwt_exp_ms`). 만료 5분 이내
    + 안전망 허용이면 락 안에서 double-check 후 refresh한다. 어떤 경우든 마지막엔 파일의 현재
    (혹은 갱신된) 토큰·account_id를 읽어 돌려준다(갱신 실패해도 기존 토큰으로 진행 → 상위가 401 폴백).
    """
    return _ensure_fresh(config, conn, now=now, path=path, urlopen=urlopen, provider="codex",
                         read_expiry_ms=_codex_expiry_ms, refresh=refresh_codex_token,
                         read_final=_read_codex_auth)


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


@dataclass(frozen=True)
class ProviderSpec:
    """provider별 취득 지식을 한 곳에 모은 명세(레지스트리 값).

    provider 분기(엔드포인트·인증 안내·헤더 조립·토큰 refresh·파서)를 fetch_provider
    밖으로 밀어낸다 — fetch_provider는 provider를 몰라도 spec에 위임만 한다. headers는 ensure_fresh_*
    호출 + 토큰 읽기 + 헤더 조립을 전부 은닉해 토큰 묶음 모양(claude=str, codex=(token, account_id))이
    spec 밖으로 새지 않게 한다. refresh는 함수명과 크레덴셜 경로(CLAUDE_CREDS/CODEX_AUTH) 모두
    호출 시점에 모듈 전역을 읽는 늦은 바인딩 lambda — 테스트가 어느 쪽을 갈아끼워도 반영된다
    (headers 콜백의 전역 참조와 대칭).
    """
    usage_url: str
    auth_note: str                 # 인증 오류 안내(옛 _auth_note)
    headers: Callable              # (config, conn, *, now, urlopen) -> dict
    refresh: Callable              # (*, now_ms, urlopen) -> str | None
    expiry_ms: Callable            # () -> int | None — 크레덴셜 파일의 만료 시각(ms), 판정 불가면 None
    parse: Callable                # (raw, *, credit_to_usd) -> buckets
    fetch: Callable                # (spec, headers, *, urlopen) -> (raw_text, http_code). 기본=단발 GET


def _claude_headers(config, conn, *, now, urlopen) -> dict:
    """Claude usage 요청 헤더 — ensure_fresh(선제 갱신) 후 Bearer + anthropic-beta + UA."""
    tok = ensure_fresh_claude_token(config, conn, now=now, path=CLAUDE_CREDS, urlopen=urlopen)
    return {"Authorization": f"Bearer {tok}",
            "anthropic-beta": _CLAUDE_BETA, "User-Agent": _CLAUDE_UA}


def _codex_headers(config, conn, *, now, urlopen) -> dict:
    """Codex usage 요청 헤더 — ensure_fresh(선제 갱신) 후 Bearer + ChatGPT-Account-Id + UA."""
    tok, acct = ensure_fresh_codex_token(config, conn, now=now, path=CODEX_AUTH, urlopen=urlopen)
    return {"Authorization": f"Bearer {tok}",
            "ChatGPT-Account-Id": acct, "User-Agent": _CODEX_UA}


def _default_get_fetch(spec, headers, *, urlopen) -> tuple[str, int]:
    """기본 취득 — 단발 GET(Claude/Codex). spec.usage_url로 요청."""
    return _http_get_text(spec.usage_url, headers, urlopen)


def _gemini_headers(config, conn, *, now, urlopen) -> dict:
    """Gemini 요청 헤더 — ensure_fresh(선제 갱신) 후 Bearer + JSON Content-Type + UA(2-step 공용)."""
    tok = ensure_fresh_gemini_token(config, conn, now=now, path=GEMINI_CREDS, urlopen=urlopen)
    return {"Authorization": f"Bearer {tok}",
            "Content-Type": "application/json", "User-Agent": _GEMINI_UA}


def _gemini_fetch(spec, headers, *, urlopen) -> tuple[str, int]:
    """2-step 취득(ADR 0027 결정 1) — loadCodeAssist(project 발견) → retrieveUserQuota.

    project 조달(ADR 0027 결정 4): GOOGLE_CLOUD_PROJECT env → loadCodeAssist 응답 cloudaicompanionProject.
    둘 다 없으면 AuthError(안내 유도). loadCodeAssist 응답(project 포함)은 버리고 retrieveUserQuota
    응답만 (raw_text, code)로 반환한다(파싱·포착 대상). 어느 단계든 HTTPError는 그대로 전파돼
    fetch_provider의 401 재시도가 처리한다.
    """
    env_project = os.environ.get("GOOGLE_CLOUD_PROJECT") or None
    lca_body: dict = {"metadata": {"ideType": "IDE_UNSPECIFIED",
                                   "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}}
    if env_project:
        lca_body["cloudaicompanionProject"] = env_project
        lca_body["metadata"]["duetProject"] = env_project
    lca_text, _ = _http_post_json(f"{_GEMINI_ENDPOINT}:loadCodeAssist", lca_body, headers, urlopen)
    project = env_project
    try:
        project = json.loads(lca_text).get("cloudaicompanionProject") or env_project
    except (ValueError, TypeError):
        pass
    if not project:
        raise AuthError("gemini project 미확인 — GOOGLE_CLOUD_PROJECT 설정이 필요합니다")
    return _http_post_json(f"{_GEMINI_ENDPOINT}:retrieveUserQuota", {"project": project}, headers, urlopen)


# provider 레지스트리 — 새 AI는 여기 spec 1개 추가(+파서·단가). refresh는 모듈 전역 이름을 경유하는
# lambda라 테스트의 monkeypatch(of.refresh_claude_token 등)가 반영된다(늦은 바인딩).
PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "claude": ProviderSpec(
        usage_url=CLAUDE_USAGE_URL,
        auth_note="재로그인이 필요합니다", headers=_claude_headers,
        refresh=lambda *, now_ms, urlopen: refresh_claude_token(
            CLAUDE_CREDS, now_ms=now_ms, urlopen=urlopen),
        expiry_ms=lambda: _claude_expiry_ms(CLAUDE_CREDS),
        parse=lambda raw, *, credit_to_usd: parse_claude(raw, credit_to_usd=credit_to_usd),
        fetch=_default_get_fetch,
    ),
    "codex": ProviderSpec(
        usage_url=CODEX_USAGE_URL,
        auth_note="Codex CLI를 1회 실행해 토큰을 갱신하세요", headers=_codex_headers,
        refresh=lambda *, now_ms, urlopen: refresh_codex_token(
            CODEX_AUTH, now_ms=now_ms, urlopen=urlopen),
        expiry_ms=lambda: _codex_expiry_ms(CODEX_AUTH),
        parse=lambda raw, *, credit_to_usd: parse_codex(raw, credit_to_usd=credit_to_usd),
        fetch=_default_get_fetch,
    ),
}


def _throttled(state, now_kst, min_interval_minutes: int) -> bool:
    """직전 시도가 min_interval 윈도우 안이면 True(우리 호출 빈도만 제어)."""
    if state is None or state["last_attempt_at"] is None:
        return False
    last = parse_ts(state["last_attempt_at"])
    if last is None:
        return False
    return (now_kst - last).total_seconds() < min_interval_minutes * 60


def _http_get_text(url: str, headers: dict, urlopen) -> tuple[str, int]:
    """HTTP GET → (응답 본문 텍스트, 상태코드). 실패 시 예외를 그대로 전파(호출자가 분류).

    본문을 **텍스트로** 돌려줘 호출자가 파싱 전에 raw를 포착할 수 있게 한다(ADR 0014).
    """
    req = urllib.request.Request(url, headers=headers)
    with urlopen(req, timeout=_TIMEOUT) as resp:
        text = resp.read().decode("utf-8")
        code = getattr(resp, "status", None) or getattr(resp, "code", None) or 200
        return text, code


def _http_post_json(url: str, body: dict, headers: dict, urlopen) -> tuple[str, int]:
    """HTTP POST(JSON body) → (응답 본문 텍스트, 상태코드). 실패 시 예외 전파(호출자가 분류).

    _http_get_text의 POST 짝 — gemini 2-step용. 본문을 텍스트로 돌려줘 raw 포착(ADR 0014)을 잇는다.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urlopen(req, timeout=_TIMEOUT) as resp:
        text = resp.read().decode("utf-8")
        code = getattr(resp, "status", None) or getattr(resp, "code", None) or 200
        return text, code


# 디버그 raw 포착(ADR 0014) — 본문 크기 상한과 스크럽/적재 헬퍼.
_RAW_CAP = 8192   # 저장 본문 8KB cap(HTML 에러 바디·대형 응답 방어)


def _scrub_and_cap(text: str | None) -> str:
    """본문을 PII 스크럽(JSON이면 deny-list 재귀) 후 8KB로 자른다.

    JSON이면 파싱→스크럽→compact 재직렬화(가독은 페이지에서 pretty). 비-JSON(에러 HTML
    등)은 키 매칭이 불가해 텍스트 그대로 cap만 한다. 빈 본문은 ''(호출자가 미포착 판단).
    """
    if not text:
        return ""
    try:
        out = json.dumps(scrub_pii(json.loads(text)),
                         ensure_ascii=False, separators=(",", ":"))
    except (ValueError, TypeError):
        out = text
    return out[:_RAW_CAP]


def _read_err_body(e) -> str:
    """HTTPError 본문을 안전하게 읽는다(fp=None이면 '')."""
    try:
        return e.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _capture_raw(conn, provider: str, fetched_at: str, status: str,
                 http_code: int | None, text: str | None) -> None:
    """스크럽+cap된 raw를 official_raw에 적재(빈 본문은 미포착). 포착 실패는 본 흐름 불간섭."""
    scrubbed = _scrub_and_cap(text)
    if not scrubbed:
        return   # 바디 없는 실패(네트워크/빈 401)는 fetch_state.last_error가 대신한다
    try:
        insert_official_raw(conn, provider=provider, fetched_at=fetched_at,
                            status=status, http_code=http_code,
                            raw_text=scrubbed, created_at=fetched_at)
    except Exception:
        pass


def fetch_provider(provider: str, *, now_kst, config, conn,
                   urlopen=urllib.request.urlopen, manual=False,
                   _retried: bool = False) -> FetchResult:
    """공식 사용량 1회 취득(비차단·단발). 결과 FetchResult.

    게이트 순서: env-skip → 미등록 provider → tracked_providers 미포함 → 크레덴셜 부재 → throttle →
    GET(≤3s) → 파서 → 적재. provider 지식은 PROVIDER_SPECS에 수렴 — 여기선 spec에 위임만 한다.
    manual=True(사용자가 직접 누른 갱신)면 throttle 게이트를 건너뛴다 — 명시적 의사 우선.
    자동(起動·폴링)은 manual=False라 throttle('자동 갱신 간격')을 거친다.
    실패(AuthError/HTTP/네트워크/파싱)는 예외를 삼켜 state에 기록하고 마지막 스냅샷을 유지한다.
    urlopen은 테스트에서 stub 주입(기본 urllib.request.urlopen).
    """
    # 1) 게이트 — env-skip / 미등록 provider / 미선택 provider / 크레덴셜 부재는 시도 없이 반환
    settings = official_fetch_settings(config)
    if os.environ.get("TOKENOMY_SKIP_OFFICIAL_FETCH"):
        return FetchResult(provider, "disabled", "skip(env)")
    spec = PROVIDER_SPECS.get(provider)
    if spec is None:
        # 레지스트리에 없는 provider — fail-loud(tracked/creds 게이트보다 앞, state 미기록)
        return FetchResult(provider, "disabled", "unknown provider")
    if provider not in tracked_providers(config):
        return FetchResult(provider, "disabled")
    if not paths.creds_present(provider):
        # 선언했지만 로그인 안 된 상태 — 거짓 auth_error를 남기지 않고 조용히 skip
        return FetchResult(provider, "disabled", "creds_absent")

    # 2) throttle — 직전 시도가 윈도우 안이면 state를 갱신하지 않고 반환.
    #    manual(수동 갱신)은 사용자 명시 의사라 throttle을 건너뛴다.
    state = get_fetch_state(conn, provider)
    if not manual and _throttled(state, now_kst, settings["min_interval_minutes"]):
        return FetchResult(provider, "throttled")

    # 3) 토큰읽기 + 네트워크(GET) — 실패 분류. raw 텍스트를 받아 파싱 전에 포착(ADR 0014).
    #    provider 분기 소멸: spec.headers가 토큰 선제갱신·읽기·헤더 조립을 은닉, spec.usage_url로 GET.
    ts = now_kst.isoformat()
    try:
        headers = spec.headers(config, conn, now=now_kst, urlopen=urlopen)
        raw_text, http_code = spec.fetch(spec, headers, urlopen=urlopen)
    except AuthError as e:
        # 크레덴셜/로컬 문제 — 네트워크 응답 자체가 없어 포착할 raw도 없다
        upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=None,
                           last_status="auth_error", last_error=str(e)[:200])
        return FetchResult(provider, "auth_error", spec.auth_note)
    except urllib.error.HTTPError as e:
        # HTTPError는 URLError의 하위클래스 — 반드시 먼저 잡아야 401/5xx 분기가 동작한다.
        # 에러 바디가 있으면 포착(4xx/5xx의 실제 원인이 거기 있다).
        _capture_raw(conn, provider, ts, "http_error", e.code, _read_err_body(e))
        if e.code == 401:
            settings = official_fetch_settings(config)
            if not _retried and settings["auto_refresh_token"] != "off":
                now_ms = int(now_kst.timestamp() * 1000)
                exp = spec.expiry_ms()                    # 파일 만료 재확인 — 죽은 토큰만 안전망 우회(ADR 0021 개정)
                if _auto_refresh_allowed(config, conn, now_kst,
                                         settings["auto_refresh_token"], provider,
                                         token_expired=exp is not None and exp <= now_ms):
                    with _token_lock:                     # spec.refresh로 디스패치(ADR 0021/0022)
                        new = spec.refresh(now_ms=now_ms, urlopen=urlopen)
                    if new:                               # 갱신 성공 → 딱 1회 재시도
                        return fetch_provider(provider, now_kst=now_kst, config=config,
                                              conn=conn, urlopen=urlopen, manual=manual,
                                              _retried=True)
            upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=None,
                               last_status="auth_error", last_error="HTTP 401")
            return FetchResult(provider, "auth_error", spec.auth_note)
        upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=None,
                           last_status="http_error", last_error=f"HTTP {e.code}")
        return FetchResult(provider, "http_error")
    except Exception as e:
        # URLError, TimeoutError/socket.timeout 등 — 응답 본문이 없어 포착할 raw도 없다
        upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=None,
                           last_status="http_error", last_error=str(e)[:200])
        return FetchResult(provider, "http_error")

    # 4) 파싱(HTTP 200) — 파서가 던지거나 JSON이 깨져도 raw 증거를 보존한다(parse_error 포착).
    try:
        raw = json.loads(raw_text)
        buckets = spec.parse(raw, credit_to_usd=credit_to_usd(config))
    except Exception as e:
        _capture_raw(conn, provider, ts, "parse_error", http_code, raw_text)
        upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=None,
                           last_status="http_error", last_error=str(e)[:200])
        return FetchResult(provider, "http_error")

    # 5) 적재 + 성공 state 기록 — try 밖(버그 가려짐 방지). raw도 포착(스크럽).
    _capture_raw(conn, provider, ts, "ok", http_code, raw_text)
    n = insert_official_buckets(conn, provider=provider, fetched_at=ts,
                                buckets=buckets, created_at=ts)
    upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=ts,
                       last_status="ok", last_error=None)
    return FetchResult(provider, "ok", bucket_count=n)


def refresh_tracked(config, *, now_kst, conn, manual=False, providers=None):
    """providers(없으면 tracked 전체)를 1회 갱신(자동 폴링·起動 hx-load·수동 갱신 공용).

    각 provider를 fetch_provider로 호출하고 결과 FetchResult 리스트를 반환한다.
    개별 fetch가 실패를 자체적으로 삼키지만(state 기록), 안전망으로 여기서도 예외를
    삼켜 한 provider가 터져도 나머지를 막지 않는다(비차단).
    providers를 명시하면 카드별 개별 갱신(예: ["claude"])에 쓰고, 없으면 전체 갱신이다.
    manual은 fetch_provider로 전달 — 수동 갱신이면 throttle을 건너뛴다.
    """
    targets = providers if providers is not None else tracked_providers(config)
    results = []
    for p in targets:
        try:
            results.append(fetch_provider(p, now_kst=now_kst, config=config,
                                          conn=conn, manual=manual))
        except Exception:
            pass
    _maybe_seed_account_mode(config, now_kst=now_kst, conn=conn, results=results)
    return results


def _maybe_seed_account_mode(config, *, now_kst, conn, results) -> None:
    """account_mode 미설정이고 이번 사이클에 공식 취득 성공(ok)이 있으면 데이터로 자동 시드(ADR 0015).

    판별자는 **tracked 전체**의 OfficialView.pool_limit_usd(USD 예산 한도 버킷 유무) — 하나라도
    있으면 enterprise, 아니면 subscription. official_view는 영속 스냅샷(latest_official_snapshot)을
    읽으므로 한 provider가 이번 사이클에 실패해도 과거 성공분으로 견고하게 판별된다.
    'ok가 하나라도'를 게이트로 둬 콜드 실패 사이클(데이터 0)에 subscription을 오시드하지 않는다.
    명시값은 seed_account_mode가 존중하므로 사용자 토글을 덮어쓰지 않는다(sticky). 시드 영속은
    save_config(기본 경로)로 일어난다. **비차단** — 시드 중 어떤 예외도 갱신 자체를 막지 않는다.
    """
    if account_mode(config) is not None:
        return
    if not any(getattr(r, "status", None) == "ok" for r in results):
        return
    try:
        cu = credit_to_usd(config)
        curation = bucket_curation_resolver(config)
        is_pooled = lambda p, rk, bk: curation(p, rk, bk)["pooled"]   # 풀 멤버십(ADR 0016)
        has_budget = any(
            official_view(conn, p, now_kst, cu, is_pooled=is_pooled).pool_limit_usd is not None
            for p in tracked_providers(config)
        )
        seed_account_mode(config, has_usd_budget=has_budget)
    except Exception:
        pass


def background_poll_loop(config, *, conn_factory, now_fn, stop_event, sleep_fn,
                         refresh_fn=refresh_tracked):
    """상주 모드 백그라운드 공식 갱신 폴 루프(ADR 0007).

    stop_event가 set될 때까지 자동 갱신 간격(min_interval_minutes)마다
    refresh_tracked(manual=False)를 호출해 공식 스냅샷 이력을 누적한다. 창 숨김과 무관하게
    돈다 — ADR 0006의 "숨김 중 주기작업 거부"를 **공식 갱신에 한해** 보완한 것이며, 로컬
    수집(ingest)은 건드리지 않는다(복원 시에만, ADR 0006). background_poll가 꺼져 있으면
    한 번도 폴하지 않고 즉시 반환한다.

    DB 연결은 이 스레드 안에서 conn_factory()로 만든다(sqlite는 스레드 격리). 개별 폴의
    예외는 삼켜 루프가 죽지 않게 한다(비차단). now_fn/sleep_fn/refresh_fn은 테스트 주입용.
    """
    settings = official_fetch_settings(config)
    if not settings["background_poll"]:
        return
    interval_sec = settings["min_interval_minutes"] * 60
    conn = conn_factory()
    try:
        while not stop_event.is_set():
            try:
                refresh_fn(config, now_kst=now_fn(), conn=conn, manual=False)
            except Exception:
                pass
            sleep_fn(interval_sec)
    finally:
        try:
            conn.close()
        except Exception:
            pass
