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
from tokenomy.aggregate import official_view, parse_ts
from tokenomy.config import (
    account_mode, credit_to_usd, official_fetch_settings, seed_account_mode, tracked_providers,
)
from tokenomy.db import (
    get_fetch_state, insert_official_buckets, insert_official_raw, upsert_fetch_state,
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


def _http_get_text(url: str, headers: dict, urlopen) -> tuple[str, int]:
    """HTTP GET → (응답 본문 텍스트, 상태코드). 실패 시 예외를 그대로 전파(호출자가 분류).

    본문을 **텍스트로** 돌려줘 호출자가 파싱 전에 raw를 포착할 수 있게 한다(ADR 0014).
    """
    req = urllib.request.Request(url, headers=headers)
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
                   urlopen=urllib.request.urlopen, manual=False) -> FetchResult:
    """공식 사용량 1회 취득(비차단·단발). 결과 FetchResult.

    게이트 순서: env-skip → tracked_providers 미포함 → 크레덴셜 부재 → throttle → GET(≤3s) → 파서 → 적재.
    manual=True(사용자가 직접 누른 갱신)면 throttle 게이트를 건너뛴다 — 명시적 의사 우선.
    자동(起動·폴링)은 manual=False라 throttle('자동 갱신 간격')을 거친다.
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

    # 2) throttle — 직전 시도가 윈도우 안이면 state를 갱신하지 않고 반환.
    #    manual(수동 갱신)은 사용자 명시 의사라 throttle을 건너뛴다.
    state = get_fetch_state(conn, provider)
    if not manual and _throttled(state, now_kst, settings["min_interval_minutes"]):
        return FetchResult(provider, "throttled")

    # 3) 토큰읽기 + 네트워크(GET) — 실패 분류. raw 텍스트를 받아 파싱 전에 포착(ADR 0014).
    ts = now_kst.isoformat()
    try:
        if provider == "claude":
            tok = _read_claude_token(CLAUDE_CREDS)   # 모듈 상수를 명시 전달(패치 가능)
            headers = {"Authorization": f"Bearer {tok}",
                       "anthropic-beta": _CLAUDE_BETA, "User-Agent": _CLAUDE_UA}
            raw_text, http_code = _http_get_text(CLAUDE_USAGE_URL, headers, urlopen)
        else:
            tok, acct = _read_codex_auth(CODEX_AUTH)   # 모듈 상수를 명시 전달(패치 가능)
            headers = {"Authorization": f"Bearer {tok}",
                       "ChatGPT-Account-Id": acct, "User-Agent": _CODEX_UA}
            raw_text, http_code = _http_get_text(CODEX_USAGE_URL, headers, urlopen)
    except AuthError as e:
        # 크레덴셜/로컬 문제 — 네트워크 응답 자체가 없어 포착할 raw도 없다
        upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=None,
                           last_status="auth_error", last_error=str(e)[:200])
        return FetchResult(provider, "auth_error", _auth_note(provider))
    except urllib.error.HTTPError as e:
        # HTTPError는 URLError의 하위클래스 — 반드시 먼저 잡아야 401/5xx 분기가 동작한다.
        # 에러 바디가 있으면 포착(4xx/5xx의 실제 원인이 거기 있다).
        _capture_raw(conn, provider, ts, "http_error", e.code, _read_err_body(e))
        if e.code == 401:
            upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=None,
                               last_status="auth_error", last_error="HTTP 401")
            return FetchResult(provider, "auth_error", _auth_note(provider))
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
        buckets = (parse_claude if provider == "claude" else parse_codex)(
            raw, credit_to_usd=credit_to_usd(config))
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
        has_budget = any(
            official_view(conn, p, now_kst, cu).pool_limit_usd is not None
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
