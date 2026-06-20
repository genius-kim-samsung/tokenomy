# 공식 사용량 자동 취득 — Phase 2(라이브 취득) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 각 CLI가 로컬에 보관한 OAuth 토큰을 읽기 전용으로 사용해 공식 사용량 API를 단발 호출하고(옵트인·비차단), 파싱→스냅샷 적재→대시보드 표시까지 라이브로 연결한다.

**Architecture:** Phase 1이 만든 순수 파서(`official_parser`)·DB 계층(`official_buckets`/`official_fetch_state`)·집계(`official_view`)·표시 패널은 이미 완료(main d685237). Phase 2는 **유일한 아웃바운드 네트워크 모듈** `official_fetch.py` 하나를 추가하고, 트리거 3곳(웹 새로고침 버튼=1차 / 설정 토글 / ingest 비차단 훅=보조)에 배선한다. 네트워크는 표준 라이브러리 `urllib.request`만 사용하고, HTTP 전송을 호출부에서 주입(`urlopen=...`)해 fixture로 전 경로를 테스트한다.

**Tech Stack:** Python 3.11+ stdlib(`urllib.request`/`json`/`pathlib`/`threading`), FastAPI(기존), Jinja2(기존), pytest. **신규 런타임 의존성 없음.**

## Global Constraints

스펙(`docs/superpowers/specs/2026-06-20-official-usage-auto-fetch-design.md`) §2·§7·§12에서 그대로 가져온 구속 요건. 모든 태스크에 암묵 포함된다.

- **네트워크 옵트인** — `official_fetch.enabled` 기본 **false**(미설정·오설정이면 false). provider별 토글(`claude`/`codex`)도 별도 검사. `TOKENOMY_SKIP_OFFICIAL_FETCH` 환경변수 설정 시 항상 skip(오프라인/CI/테스트). 옵트인 off면 네트워크 호출 0.
- **HTTP 타임아웃 ≤ 3s/provider**(connect+read). **백오프 없음** — 단발 시도, 실패 즉시 포기, 마지막 스냅샷 유지. (실측 doc의 "지수 백오프"는 옛 권고 — 스펙 §7에서 **제거**됨. 재도입 금지.)
- **throttle** — `min_interval_minutes` 기본 **5**. `last_attempt_at`이 윈도우 미달이면 `throttled` 반환(마지막 스냅샷 유지). **throttled일 때 state를 갱신하지 않는다**(갱신하면 윈도우가 영원히 미끄러짐).
- **起動 비차단** — `launcher._safe_ingest`(launcher.py:51)가 서버 시작 전 `cmd_ingest`를 동기 호출하므로, fetch가 그 안에서 블록되면 안 된다. ingest 훅은 **데몬 스레드**로 분리(스레드는 자기 sqlite conn을 연다 — 스레드 간 conn 공유 금지).
- **토큰 읽기 전용, refresh 금지** — Claude `~/.claude/.credentials.json` → `claudeAiOauth.accessToken`. Codex `~/.codex/auth.json` → `tokens.access_token` + `tokens.account_id`.
- **엔드포인트/헤더** — Claude: `GET https://api.anthropic.com/api/oauth/usage`, `Authorization: Bearer <tok>`, `anthropic-beta: oauth-2025-04-20`, `User-Agent: claude-code/2.1.179`. Codex: `GET https://chatgpt.com/backend-api/wham/usage`, `Authorization: Bearer <tok>`, `ChatGPT-Account-Id: <account_id>`, `User-Agent: codex_cli_rs`.
- **에러 분류(마지막 스냅샷·last_success_at 보존)** — 401 → `auth_error`(Codex note "Codex CLI를 1회 실행"). 429/5xx/네트워크/TLS 인터셉트/JSON 파싱 실패 → `http_error`. 크레덴셜 파일 누락·스키마 드리프트·토큰/account_id 부재 → `auth_error`. 모든 실패는 `upsert_fetch_state`가 `last_success_at`을 COALESCE로 보존.
- **PII 저장 금지** — `access_token`/`account_id`/`email`/`user_id`는 **절대 DB에 적재하지 않는다**. 헤더에 쓰고 버린다. `official_buckets`에는 사용량 수치만(파서가 PII 미추출).
- **웹은 `127.0.0.1`만** — 새 라우트도 로컬 전용. `POST /official/refresh`는 **결과 무관 redirect**(백오프 없음).
- **코드 스타일** — docstring·주석 한국어. 모든 모듈 상단 `from __future__ import annotations`. stdlib 우선. 계층 분리 유지(라우트 app.py 얇게 ↔ views.py ↔ aggregate.py ↔ db.py).
- **베이스라인** — Phase 2 시작 시점 `378 passed, 2 failed`. 2건은 `test_launcher.py`의 포트 8765 환경 충돌(알려진 이슈, **회귀 아님**). 모든 태스크는 이 2건을 무시하고 나머지를 green으로 유지한다.

**실행 환경:** 워크트리 cwd `C:\projects\samsung\tokenomy\.claude\worktrees\feat+official-usage-auto-fetch\`. 파이썬은 메인 repo의 `.venv`를 워크트리에서 실행: `C:/projects/samsung/tokenomy/.venv/Scripts/python.exe`. 테스트는 `TOKENOMY_SKIP_UPDATE_CHECK=1` 환경에서.

---

## File Structure

| 파일 | 역할 | 변경 |
|---|---|---|
| `tokenomy/official_fetch.py` | 유일한 아웃바운드. 토큰 읽기·헤더·GET(타임아웃)·throttle·에러분류·파서→DB | **신규** |
| `tokenomy/budget.py` | `official_fetch_settings(config)` accessor(옵트인 설정 정규화) | 수정 |
| `tokenomy/web/app.py` | `POST /official/refresh` 라우트 + settings GET/POST 확장 | 수정 |
| `tokenomy/web/views.py` | `official_fetch_status(conn, config)` — 취득 상태 표면 + overview_context 배선 | 수정 |
| `tokenomy/web/templates/overview.html` | 공식 패널에 새로고침 버튼 + 취득 상태/안내 | 수정 |
| `tokenomy/web/templates/settings.html` | 공식 자동 취득 토글 카드 + 마지막 취득 상태 | 수정 |
| `tokenomy/cli.py` | `_official_fetch_worker` + `cmd_ingest` 비차단 훅 | 수정 |
| `config/tokenomy.config.example.json` | `official_fetch` 블록 | 수정 |
| `CLAUDE.md` / `README.md` | 아키텍처·게시·환경변수 갱신 | 수정 |
| `tests/test_official_fetch.py` | fetch 전 경로(stub transport) | **신규** |
| `tests/test_budget.py` / `test_web.py` / `test_cli.py` | accessor·라우트·설정·훅 | 수정 |

---

## Task 1: 공식 취득 설정 accessor + 예시 config

**Files:**
- Modify: `tokenomy/budget.py`(`load_config` base 기본값 + 신규 `official_fetch_settings`)
- Modify: `config/tokenomy.config.example.json`
- Test: `tests/test_budget.py`

**Interfaces:**
- Consumes: 없음(config dict).
- Produces:
  - `official_fetch_settings(config: dict) -> dict` — 키 `{"enabled": bool, "claude": bool, "codex": bool, "min_interval_minutes": int}`. 누락/오설정은 안전 기본값(enabled False, provider 토글 True, min_interval 5)으로 폴백. 음수 min_interval → 5.

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_budget.py` 끝에 추가:

```python
from tokenomy.budget import official_fetch_settings


def test_official_fetch_settings_defaults_optin_off():
    # 미설정: 옵트인 off, provider 토글 on, 5분 throttle
    s = official_fetch_settings({})
    assert s == {"enabled": False, "claude": True, "codex": True, "min_interval_minutes": 5}


def test_official_fetch_settings_reads_user_values():
    cfg = {"official_fetch": {"enabled": True, "claude": True, "codex": False,
                              "min_interval_minutes": 10}}
    s = official_fetch_settings(cfg)
    assert s["enabled"] is True
    assert s["codex"] is False
    assert s["min_interval_minutes"] == 10


def test_official_fetch_settings_bad_interval_falls_back():
    assert official_fetch_settings({"official_fetch": {"min_interval_minutes": "x"}})["min_interval_minutes"] == 5
    assert official_fetch_settings({"official_fetch": {"min_interval_minutes": -3}})["min_interval_minutes"] == 5


def test_official_fetch_settings_partial_keeps_defaults():
    # enabled만 준 부분 설정 — 나머지는 기본값으로 채워짐
    s = official_fetch_settings({"official_fetch": {"enabled": True}})
    assert s == {"enabled": True, "claude": True, "codex": True, "min_interval_minutes": 5}
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python.exe -m pytest tests/test_budget.py -q`
Expected: FAIL — `ImportError: cannot import name 'official_fetch_settings'`

- [ ] **Step 3: accessor 구현** — `tokenomy/budget.py` 끝(`credit_to_usd` 함수 아래)에 추가:

```python
def official_fetch_settings(config: dict) -> dict:
    """공식 사용량 자동 취득 설정(옵트인). 누락·오설정은 안전 기본값으로 폴백.

    기본: enabled False(네트워크 옵트인), provider 토글 True, min_interval_minutes 5.
    토큰/엔드포인트는 코드 상수 — 여기선 토글·throttle만 정규화한다.
    """
    raw = config.get("official_fetch") or {}

    def _flag(key: str, default: bool) -> bool:
        v = raw.get(key, default)
        return v if isinstance(v, bool) else default

    try:
        mi = int(raw.get("min_interval_minutes", 5))
    except (TypeError, ValueError):
        mi = 5
    if mi < 0:
        mi = 5
    return {
        "enabled": _flag("enabled", False),
        "claude": _flag("claude", True),
        "codex": _flag("codex", True),
        "min_interval_minutes": mi,
    }
```

- [ ] **Step 4: `load_config` base 기본값에 official_fetch 추가** — `tokenomy/budget.py`의 `load_config` 내 `base` 딕셔너리(현재 budget.py:52-56)를 수정:

```python
    base = {"user_label": _default_label(),
            "budget": {"claude": 0.0, "codex": 0.0},
            "budget_start": None,
            "credit_to_usd": 0.04,
            "official_fetch": {"enabled": False, "claude": True, "codex": True,
                               "min_interval_minutes": 5},
            "pricing_overrides": {}}
```

- [ ] **Step 5: 예시 config 갱신** — `config/tokenomy.config.example.json`를 다음으로 교체:

```json
{
  "user_label": "me",
  "budget": {
    "claude": 100,
    "codex": 50
  },
  "budget_start": null,
  "credit_to_usd": 0.04,
  "official_fetch": {
    "enabled": false,
    "claude": true,
    "codex": true,
    "min_interval_minutes": 5
  },
  "pricing_overrides": {}
}
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python.exe -m pytest tests/test_budget.py -q`
Expected: PASS(신규 4건 포함)

- [ ] **Step 7: 커밋**

```bash
git add tokenomy/budget.py config/tokenomy.config.example.json tests/test_budget.py
git commit -m "feat(official): 자동 취득 설정 accessor(official_fetch_settings) + 예시 config"
```

---

## Task 2: 크레덴셜 토큰 리더(파일 IO, 네트워크 없음)

**Files:**
- Create: `tokenomy/official_fetch.py`
- Test: `tests/test_official_fetch.py`

**Interfaces:**
- Consumes: 없음.
- Produces:
  - `class AuthError(Exception)` — 크레덴셜 파일 누락·스키마 드리프트·토큰 부재.
  - `_read_claude_token(path: Path = CLAUDE_CREDS) -> str` — 실패 시 `AuthError`.
  - `_read_codex_auth(path: Path = CODEX_AUTH) -> tuple[str, str]` — `(access_token, account_id)`, 실패 시 `AuthError`.
  - 모듈 상수: `CLAUDE_USAGE_URL`, `CODEX_USAGE_URL`, `CLAUDE_CREDS`, `CODEX_AUTH`.

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_official_fetch.py` 신규:

```python
"""공식 사용량 라이브 취득 — 토큰 리더 + fetch_provider 전 경로(stub transport)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tokenomy.official_fetch import (
    AuthError, _read_claude_token, _read_codex_auth,
)


def test_read_claude_token_ok(tmp_path):
    p = tmp_path / "creds.json"
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-abc"}}), encoding="utf-8")
    assert _read_claude_token(p) == "sk-abc"


def test_read_claude_token_missing_file(tmp_path):
    with pytest.raises(AuthError):
        _read_claude_token(tmp_path / "nope.json")


def test_read_claude_token_bad_schema(tmp_path):
    p = tmp_path / "creds.json"
    p.write_text(json.dumps({"other": {}}), encoding="utf-8")
    with pytest.raises(AuthError):
        _read_claude_token(p)


def test_read_claude_token_empty(tmp_path):
    p = tmp_path / "creds.json"
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": ""}}), encoding="utf-8")
    with pytest.raises(AuthError):
        _read_claude_token(p)


def test_read_codex_auth_ok(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"tokens": {"access_token": "jwt-x", "account_id": "acc-1"}}),
                 encoding="utf-8")
    assert _read_codex_auth(p) == ("jwt-x", "acc-1")


def test_read_codex_auth_missing_account_id(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"tokens": {"access_token": "jwt-x"}}), encoding="utf-8")
    with pytest.raises(AuthError):
        _read_codex_auth(p)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python.exe -m pytest tests/test_official_fetch.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tokenomy.official_fetch'`

- [ ] **Step 3: 모듈 + 리더 구현** — `tokenomy/official_fetch.py` 신규:

```python
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

from tokenomy.aggregate import parse_ts
from tokenomy.budget import credit_to_usd, official_fetch_settings
from tokenomy.db import get_fetch_state, insert_official_buckets, upsert_fetch_state
from tokenomy.official_parser import parse_claude, parse_codex

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"
CODEX_AUTH = Path.home() / ".codex" / "auth.json"

_CLAUDE_UA = "claude-code/2.1.179"
_CODEX_UA = "codex_cli_rs"
_CLAUDE_BETA = "oauth-2025-04-20"
_TIMEOUT = 3.0   # connect+read, provider당 상한(백오프 없음)


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
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python.exe -m pytest tests/test_official_fetch.py -q`
Expected: PASS(6건)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/official_fetch.py tests/test_official_fetch.py
git commit -m "feat(official): official_fetch 모듈 골격 + 읽기전용 크레덴셜 토큰 리더"
```

---

## Task 3: `fetch_provider` — 전 경로(옵트인·throttle·성공·에러)

**Files:**
- Modify: `tokenomy/official_fetch.py`
- Test: `tests/test_official_fetch.py`

**Interfaces:**
- Consumes: `AuthError`, `_read_claude_token`, `_read_codex_auth`(Task 2); `official_fetch_settings`/`credit_to_usd`(budget); `parse_claude`/`parse_codex`(official_parser); `get_fetch_state`/`insert_official_buckets`/`upsert_fetch_state`(db); `parse_ts`(aggregate).
- Produces:
  - `@dataclass FetchResult(provider: str, status: str, note: str | None = None, bucket_count: int = 0)` — `status` ∈ `{'ok','throttled','auth_error','http_error','disabled'}`.
  - `fetch_provider(provider: str, *, now_kst, config, conn, urlopen=urllib.request.urlopen) -> FetchResult` — 옵트인/throttle/성공/에러 전 경로. `urlopen`은 테스트에서 stub 주입(`urllib.request.Request`를 받아 컨텍스트매니저(`.read()` 보유)를 반환하는 호출가능).

**테스트용 stub 규약(테스트에서 정의):** `urlopen(req, timeout=...)`는 `with` 가능 객체를 반환해야 하고 그 객체는 `.read() -> bytes`를 제공한다. 에러 경로는 stub이 `urllib.error.HTTPError`/`urllib.error.URLError`/`TimeoutError`를 raise한다.

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_official_fetch.py`에 추가(상단 import도 보강):

```python
import urllib.error
from datetime import datetime, timedelta

from tokenomy.aggregate import KST
from tokenomy.db import connect, get_fetch_state, latest_official_snapshot
from tokenomy.official_fetch import FetchResult, fetch_provider


class _FakeResp:
    def __init__(self, payload: dict):
        self._b = json.dumps(payload).encode("utf-8")
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._b


def _opener(payload=None, exc=None):
    """urlopen stub. exc가 있으면 raise, 없으면 payload를 담은 _FakeResp 반환.
    호출 여부 기록을 위해 .calls 리스트를 단다."""
    calls = []
    def _open(req, timeout=None):
        calls.append((req.full_url, timeout))
        if exc is not None:
            raise exc
        return _FakeResp(payload or {})
    _open.calls = calls
    return _open


def _never(req, timeout=None):
    raise AssertionError("network must not be called")


_CLAUDE_RAW = {
    "spend": {"used": {"amount_minor": 3000, "exponent": 2},
              "limit": {"amount_minor": 10000, "exponent": 2}},
    "extra_usage": {"monthly_limit": 10000},
}
_CODEX_RAW = {"spend_control": {"individual_limit": {
    "limit": "2000", "used": "500.0", "remaining": "1500.0",
    "used_percent": 25, "reset_at": 1782864001}}}

_NOW = datetime(2026, 6, 10, 9, tzinfo=KST)
_CFG_ON = {"official_fetch": {"enabled": True, "claude": True, "codex": True,
                             "min_interval_minutes": 5}, "credit_to_usd": 0.04}


def _patch_creds(monkeypatch, tmp_path):
    """fetch_provider가 읽을 크레덴셜 파일 경로를 tmp로 바꾼다."""
    import tokenomy.official_fetch as of
    cp = tmp_path / "claude.json"
    cp.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-x"}}), encoding="utf-8")
    xp = tmp_path / "codex.json"
    xp.write_text(json.dumps({"tokens": {"access_token": "jwt", "account_id": "acc"}}),
                  encoding="utf-8")
    monkeypatch.setattr(of, "CLAUDE_CREDS", cp)
    monkeypatch.setattr(of, "CODEX_AUTH", xp)


def test_fetch_disabled_when_optin_off(monkeypatch):
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    conn = connect(":memory:")
    r = fetch_provider("claude", now_kst=_NOW, config={}, conn=conn, urlopen=_never)
    assert r.status == "disabled"
    # state 미기록(옵트인 off는 시도 아님)
    assert get_fetch_state(conn, "claude") is None


def test_fetch_disabled_when_provider_toggle_off(monkeypatch):
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    conn = connect(":memory:")
    cfg = {"official_fetch": {"enabled": True, "claude": True, "codex": False}}
    r = fetch_provider("codex", now_kst=_NOW, config=cfg, conn=conn, urlopen=_never)
    assert r.status == "disabled"


def test_fetch_env_skip(monkeypatch):
    monkeypatch.setenv("TOKENOMY_SKIP_OFFICIAL_FETCH", "1")
    conn = connect(":memory:")
    r = fetch_provider("claude", now_kst=_NOW, config=_CFG_ON, conn=conn, urlopen=_never)
    assert r.status == "disabled"


def test_fetch_claude_success_stores_buckets(monkeypatch, tmp_path):
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    _patch_creds(monkeypatch, tmp_path)
    conn = connect(":memory:")
    op = _opener(payload=_CLAUDE_RAW)
    r = fetch_provider("claude", now_kst=_NOW, config=_CFG_ON, conn=conn, urlopen=op)
    assert r.status == "ok" and r.bucket_count >= 1
    rows = latest_official_snapshot(conn, "claude")
    assert any(row["used_usd"] == 30.0 for row in rows)
    st = get_fetch_state(conn, "claude")
    assert st["last_status"] == "ok" and st["last_success_at"] is not None
    # 올바른 엔드포인트 호출
    assert op.calls and op.calls[0][0] == "https://api.anthropic.com/api/oauth/usage"


def test_fetch_codex_success_no_pii_stored(monkeypatch, tmp_path):
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    _patch_creds(monkeypatch, tmp_path)
    conn = connect(":memory:")
    op = _opener(payload=_CODEX_RAW)
    r = fetch_provider("codex", now_kst=_NOW, config=_CFG_ON, conn=conn, urlopen=op)
    assert r.status == "ok"
    # PII(account_id="acc", access_token="jwt")가 DB 어디에도 없어야 한다
    dump = json.dumps([dict(row) for row in latest_official_snapshot(conn, "codex")])
    assert "acc" not in dump and "jwt" not in dump
    state_dump = json.dumps(dict(get_fetch_state(conn, "codex")))
    assert "acc" not in state_dump and "jwt" not in state_dump


def test_fetch_throttled_keeps_window(monkeypatch, tmp_path):
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    _patch_creds(monkeypatch, tmp_path)
    conn = connect(":memory:")
    # 직전 시도 2분 전 기록 → 5분 미달이라 throttled
    from tokenomy.db import upsert_fetch_state
    prev = (_NOW - timedelta(minutes=2)).isoformat()
    upsert_fetch_state(conn, "claude", last_attempt_at=prev, last_success_at=prev,
                       last_status="ok", last_error=None)
    r = fetch_provider("claude", now_kst=_NOW, config=_CFG_ON, conn=conn, urlopen=_never)
    assert r.status == "throttled"
    # state의 last_attempt_at이 미끄러지지 않았다(윈도우 보존)
    assert get_fetch_state(conn, "claude")["last_attempt_at"] == prev


def test_fetch_401_is_auth_error_preserves_success(monkeypatch, tmp_path):
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    _patch_creds(monkeypatch, tmp_path)
    conn = connect(":memory:")
    from tokenomy.db import upsert_fetch_state
    upsert_fetch_state(conn, "codex", last_attempt_at="2026-06-01T00:00:00+09:00",
                       last_success_at="2026-06-01T00:00:00+09:00",
                       last_status="ok", last_error=None)
    err = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
    r = fetch_provider("codex", now_kst=_NOW, config=_CFG_ON, conn=conn,
                       urlopen=_opener(exc=err))
    assert r.status == "auth_error" and "Codex" in (r.note or "")
    st = get_fetch_state(conn, "codex")
    assert st["last_status"] == "auth_error"
    assert st["last_success_at"] == "2026-06-01T00:00:00+09:00"   # 보존


def test_fetch_500_is_http_error(monkeypatch, tmp_path):
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    _patch_creds(monkeypatch, tmp_path)
    conn = connect(":memory:")
    err = urllib.error.HTTPError("u", 500, "err", {}, None)
    r = fetch_provider("claude", now_kst=_NOW, config=_CFG_ON, conn=conn,
                       urlopen=_opener(exc=err))
    assert r.status == "http_error"
    assert get_fetch_state(conn, "claude")["last_status"] == "http_error"


def test_fetch_network_error_is_http_error(monkeypatch, tmp_path):
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    _patch_creds(monkeypatch, tmp_path)
    conn = connect(":memory:")
    r = fetch_provider("claude", now_kst=_NOW, config=_CFG_ON, conn=conn,
                       urlopen=_opener(exc=urllib.error.URLError("timed out")))
    assert r.status == "http_error"


def test_fetch_missing_creds_is_auth_error(monkeypatch, tmp_path):
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    import tokenomy.official_fetch as of
    monkeypatch.setattr(of, "CLAUDE_CREDS", tmp_path / "nope.json")
    conn = connect(":memory:")
    r = fetch_provider("claude", now_kst=_NOW, config=_CFG_ON, conn=conn, urlopen=_never)
    assert r.status == "auth_error"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python.exe -m pytest tests/test_official_fetch.py -q`
Expected: FAIL — `ImportError: cannot import name 'FetchResult'`(또는 `fetch_provider`)

- [ ] **Step 3: `FetchResult` + 헬퍼 + `fetch_provider` 구현** — `tokenomy/official_fetch.py`의 리더 아래에 추가:

```python
@dataclass
class FetchResult:
    """한 provider 취득 결과(표시·로깅용). DB 적재는 fetch_provider가 직접 한다."""
    provider: str
    status: str            # 'ok'|'throttled'|'auth_error'|'http_error'|'disabled'
    note: str | None = None
    bucket_count: int = 0


def _auth_note(provider: str) -> str:
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
    req = urllib.request.Request(url, headers=headers)
    with urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_provider(provider: str, *, now_kst, config, conn,
                   urlopen=urllib.request.urlopen) -> FetchResult:
    """공식 사용량 1회 취득(옵트인·비차단·단발). 결과 FetchResult.

    경로: 옵트인/env-skip → throttle → 토큰읽기 → GET(≤3s) → 파서 → 트랜잭션 적재 → state(ok).
    실패(AuthError/HTTP/네트워크/파싱)는 예외를 삼켜 state에 기록하고 마지막 스냅샷을 유지한다.
    urlopen은 테스트에서 stub 주입(기본 urllib.request.urlopen).
    """
    settings = official_fetch_settings(config)
    if os.environ.get("TOKENOMY_SKIP_OFFICIAL_FETCH"):
        return FetchResult(provider, "disabled", "skip(env)")
    if not settings["enabled"] or not settings.get(provider, True):
        return FetchResult(provider, "disabled")

    state = get_fetch_state(conn, provider)
    if _throttled(state, now_kst, settings["min_interval_minutes"]):
        return FetchResult(provider, "throttled")

    ts = now_kst.isoformat()
    try:
        if provider == "claude":
            tok = _read_claude_token()
            headers = {"Authorization": f"Bearer {tok}",
                       "anthropic-beta": _CLAUDE_BETA, "User-Agent": _CLAUDE_UA}
            raw = _http_get_json(CLAUDE_USAGE_URL, headers, urlopen)
            buckets = parse_claude(raw, credit_to_usd=credit_to_usd(config))
        else:
            tok, acct = _read_codex_auth()
            headers = {"Authorization": f"Bearer {tok}",
                       "ChatGPT-Account-Id": acct, "User-Agent": _CODEX_UA}
            raw = _http_get_json(CODEX_USAGE_URL, headers, urlopen)
            buckets = parse_codex(raw, credit_to_usd=credit_to_usd(config))
    except AuthError as e:
        upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=None,
                           last_status="auth_error", last_error=str(e)[:200])
        return FetchResult(provider, "auth_error", _auth_note(provider))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=None,
                               last_status="auth_error", last_error="HTTP 401")
            return FetchResult(provider, "auth_error", _auth_note(provider))
        upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=None,
                           last_status="http_error", last_error=f"HTTP {e.code}")
        return FetchResult(provider, "http_error")
    except Exception as e:   # URLError, TimeoutError/socket.timeout, JSON 파싱 등 — 백오프 없이 포기
        upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=None,
                           last_status="http_error", last_error=str(e)[:200])
        return FetchResult(provider, "http_error")

    n = insert_official_buckets(conn, provider=provider, fetched_at=ts,
                                buckets=buckets, created_at=ts)
    upsert_fetch_state(conn, provider, last_attempt_at=ts, last_success_at=ts,
                       last_status="ok", last_error=None)
    return FetchResult(provider, "ok", bucket_count=n)
```

> 주의: `urllib.error.HTTPError`는 `URLError`의 하위클래스다. 반드시 `HTTPError`를 먼저 잡고 그 다음 광범위 `Exception`을 잡아야 401/5xx 분기가 동작한다. `try`는 토큰읽기+네트워크+파싱만 감싼다 — 적재(`insert`/`upsert ok`)는 try 밖이라 버그가 가려지지 않는다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python.exe -m pytest tests/test_official_fetch.py -q`
Expected: PASS(신규 포함 전부)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/official_fetch.py tests/test_official_fetch.py
git commit -m "feat(official): fetch_provider — 옵트인·throttle·성공·에러분류(401/http) 전 경로"
```

---

## Task 4: `POST /official/refresh` 라우트

**Files:**
- Modify: `tokenomy/web/app.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `fetch_provider`(Task 3); `load_config`(budget); `connect`(db); `KST`(aggregate).
- Produces: `POST /official/refresh` — Form `provider`(빈값/미허용 → 둘 다). 각 provider에 `fetch_provider` 호출 후 결과 무관 `/`로 303 redirect. 백오프 없음.

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_web.py`에 추가:

```python
def test_official_refresh_calls_fetch_and_redirects(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(app_module, "fetch_provider",
                        lambda p, **k: calls.append(p))
    r = client.post("/official/refresh", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert set(calls) == {"claude", "codex"}


def test_official_refresh_scopes_single_provider(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(app_module, "fetch_provider",
                        lambda p, **k: calls.append(p))
    r = client.post("/official/refresh", data={"provider": "claude"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert calls == ["claude"]


def test_official_refresh_redirects_even_on_fetch_error(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    def boom(p, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(app_module, "fetch_provider", boom)
    r = client.post("/official/refresh", data={}, follow_redirects=False)
    assert r.status_code == 303   # 결과 무관 redirect(예외도 삼킴)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python.exe -m pytest tests/test_web.py -q -k official_refresh`
Expected: FAIL — 404(라우트 없음) 또는 `AttributeError: fetch_provider`

- [ ] **Step 3: 라우트 구현** — `tokenomy/web/app.py`:

import 보강(상단 import 블록):

```python
from tokenomy.budget import budget_from_config, credit_to_usd as _credit_to_usd, load_config, official_fetch_settings, save_config
from tokenomy.official_fetch import fetch_provider
```

(`from tokenomy.aggregate import KST, DIM_COLUMNS, PROVIDERS, parse_ts`는 이미 KST 포함 — 확인.)

`do_ingest` 라우트 아래에 추가:

```python
@app.post("/official/refresh")
def official_refresh(provider: str = Form("")):
    conn = connect()
    config = load_config()
    now = datetime.now(KST)
    targets = [provider] if provider in PROVIDERS else list(PROVIDERS)
    for p in targets:
        try:
            fetch_provider(p, now_kst=now, config=config, conn=conn)
        except Exception:
            pass   # 결과 무관 — 상태는 fetch_state에 기록됨, 페이지에서 표시
    return RedirectResponse("/", status_code=303)
```

> `PROVIDERS`는 `aggregate`에서 이미 import됨(app.py:12). `("claude","codex")` 튜플이므로 `list(PROVIDERS)`로 순서 보존.

- [ ] **Step 4: 테스트 통과 확인**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python.exe -m pytest tests/test_web.py -q -k official_refresh`
Expected: PASS(3건)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/app.py tests/test_web.py
git commit -m "feat(official): POST /official/refresh 라우트(결과 무관 redirect, 백오프 없음)"
```

---

## Task 5: 설정 UI — 공식 자동 취득 토글 지속

**Files:**
- Modify: `tokenomy/web/app.py`(`settings_get`/`settings_post`)
- Modify: `tokenomy/web/templates/settings.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `official_fetch_settings`(budget); `get_fetch_state`(db).
- Produces: `settings_get`가 `official_fetch`(정규화 설정) + `official_states`(provider별 fetch_state dict 또는 None)를 컨텍스트에 추가. `settings_post`가 `official_enabled`/`official_claude`/`official_codex`/`min_interval` Form을 `config["official_fetch"]`로 저장.

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_web.py`에 추가:

```python
def test_settings_shows_official_fetch_section(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "공식 사용량 자동 취득" in r.text
    assert 'name="official_enabled"' in r.text


def test_settings_post_saves_official_fetch(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    cfg = tmp_path / "cfg.json"
    r = client.post("/settings", data={
        "claude": "100", "codex": "50", "budget_start": "", "credit_to_usd": "0.04",
        "official_enabled": "on", "official_claude": "on", "min_interval": "10",
        # official_codex 미체크(체크박스 미전송) → False로 저장
    }, follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    of = saved["official_fetch"]
    assert of["enabled"] is True
    assert of["claude"] is True
    assert of["codex"] is False
    assert of["min_interval_minutes"] == 10
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python.exe -m pytest tests/test_web.py -q -k "settings_shows_official or settings_post_saves_official"`
Expected: FAIL — 본문에 "공식 사용량 자동 취득" 없음 / `official_fetch` 키 없음

- [ ] **Step 3: `settings_get` 확장** — `tokenomy/web/app.py`의 `settings_get`(현재 app.py:141-156) 반환 dict에 추가. 함수 본문에서 `from tokenomy.db import connect, get_fetch_state`가 필요하므로 상단 import를 `from tokenomy.db import connect`에서 `from tokenomy.db import connect, get_fetch_state`로 보강. 그리고 반환 컨텍스트에 추가:

```python
@app.get("/settings")
def settings_get(request: Request):
    config = load_config()
    budget = budget_from_config(config)
    conn = connect()
    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    pricing = apply_pricing_overrides(load_pricing(), config.get("pricing_overrides"))
    ofs = official_fetch_settings(config)
    official_states = {p: (dict(st) if (st := get_fetch_state(conn, p)) else None)
                       for p in PROVIDERS}
    return templates.TemplateResponse(
        request, "settings.html",
        {"claude": budget.claude, "codex": budget.codex,
         "budget_start": config.get("budget_start") or "",
         "credit_to_usd": _credit_to_usd(config),
         "official_fetch": ofs, "official_states": official_states,
         "active_nav": "settings", "update_tag": check_update(conn),
         "last_ts": last["t"] if last and last["t"] else None,
         **coverage_card_context(conn, pricing)},
    )
```

- [ ] **Step 4: `settings_post` 확장** — `tokenomy/web/app.py`의 `settings_post`(현재 app.py:177-187):

```python
@app.post("/settings")
def settings_post(claude: str = Form(""), codex: str = Form(""),
                  budget_start: str = Form(""), credit_to_usd: str = Form(""),
                  official_enabled: str = Form(""), official_claude: str = Form(""),
                  official_codex: str = Form(""), min_interval: str = Form("")):
    config = load_config()
    config["budget"]["claude"] = _to_float(claude)
    config["budget"]["codex"] = _to_float(codex)
    config["budget_start"] = _valid_date_or_none(budget_start)
    ctu = _to_float(credit_to_usd)
    config["credit_to_usd"] = ctu if ctu > 0 else 0.04
    mi = int(_to_float(min_interval))
    config["official_fetch"] = {
        "enabled": bool(official_enabled),
        "claude": bool(official_claude),
        "codex": bool(official_codex),
        "min_interval_minutes": mi if mi > 0 else 5,
    }
    save_config(config)
    return RedirectResponse("/", status_code=303)
```

> 체크박스는 체크 시에만 값(`"on"`)이 전송되고 미체크면 미전송(Form 기본 `""`) → `bool("on")=True`, `bool("")=False`. `_to_float`는 비숫자/빈값을 0.0으로 떨군다.

- [ ] **Step 5: 설정 템플릿 카드 추가** — `tokenomy/web/templates/settings.html`의 "월 예산" `</section>`(line 18) 바로 뒤에 카드 삽입:

```html
<section class="card">
  <h2>공식 사용량 자동 취득</h2>
  <p class="muted">각 CLI의 로컬 OAuth 토큰으로 공식 사용량 API를 <strong>읽기 전용 단발 호출</strong>합니다.
    옵트인(기본 꺼짐) — 켜야만 네트워크를 사용합니다. 토큰은 읽기만 하고 refresh하지 않으며,
    사용량 수치만 저장합니다(토큰·계정 식별자 미저장).</p>
  <form method="post" action="/settings" class="settings">
    <label><input type="checkbox" name="official_enabled" {% if official_fetch.enabled %}checked{% endif %}> 자동 취득 사용</label>
    <label><input type="checkbox" name="official_claude" {% if official_fetch.claude %}checked{% endif %}> Claude</label>
    <label><input type="checkbox" name="official_codex" {% if official_fetch.codex %}checked{% endif %}> Codex</label>
    <label>최소 취득 간격(분) <input type="number" step="1" min="1" name="min_interval" value="{{ official_fetch.min_interval_minutes }}"></label>
    <p class="muted">엔드포인트 quota는 CLI와 공유됩니다 — 간격을 너무 짧게 두면 한도(429)에 걸릴 수 있습니다(기본 5분).</p>
    <button class="btn" type="submit">저장</button>
  </form>
  <div class="muted">
    마지막 취득 상태:
    {% for p in ["claude", "codex"] %}
      {% set st = official_states[p] %}
      <span>· {{ p }}: {% if st %}{{ st.last_status }}{% if st.last_attempt_at %} ({{ st.last_attempt_at[:16] }}){% endif %}{% else %}미시도{% endif %}</span>
    {% endfor %}
  </div>
  <p class="disclaimer">ⓘ 토큰 만료(401) 시 마지막 값을 유지합니다. Codex는 CLI를 1회 실행하면 토큰이 갱신됩니다.</p>
</section>
```

> 이 form은 예산 form과 별개지만 같은 `POST /settings`로 보낸다. `settings_post`의 예산/credit_to_usd Form 인자는 기본값(`""`)이 있어, 이 form만 제출돼도 예산이 0으로 덮이지 않게 하려면 **두 form을 하나로 합치거나** hidden으로 현재 예산을 실어야 한다. → 다음 Step에서 한 form으로 통합한다.

- [ ] **Step 6: 예산 + 공식 취득을 한 form으로 통합(예산 덮어쓰기 방지)** — "월 예산" 카드의 `<form>`과 위에서 추가한 공식 취득 `<form>`을 **하나의 form**으로 합친다. settings.html을 다음 구조로 정리(두 카드 유지, 단일 form이 두 카드를 감쌈):

```html
{% extends "base.html" %}
{% block body %}
<h1 class="page-title">설정</h1>

<form method="post" action="/settings" class="settings">
<section class="card">
  <h2>월 예산</h2>
  <p class="muted">종량제(API 달러 과금) 기준 월 예산. 0이면 한도 없이 사용량만 추적합니다.</p>
  <label>Claude (USD) <input type="number" step="0.01" min="0" name="claude" value="{{ '%.2f'|format(claude) }}"></label>
  <label>Codex (USD) <input type="number" step="0.01" min="0" name="codex" value="{{ '%.2f'|format(codex) }}"></label>
  <label>예산 도입일 <input type="date" name="budget_start" value="{{ budget_start }}"></label>
  <p class="muted">도입일을 지정하면 그 날짜부터 예산을 계산합니다(이전 지출 제외). 비우면 매월 1일 기준.</p>
  <label>credit_to_usd <span class="muted">(크레딧→USD 환산, 기본 0.04)</span>
    <input type="number" step="0.001" min="0" name="credit_to_usd" value="{{ credit_to_usd }}"></label>
  <p class="disclaimer">ⓘ 값은 로컬 config(tokenomy.config.json)에만 저장됩니다.</p>
</section>

<section class="card">
  <h2>공식 사용량 자동 취득</h2>
  <p class="muted">각 CLI의 로컬 OAuth 토큰으로 공식 사용량 API를 <strong>읽기 전용 단발 호출</strong>합니다.
    옵트인(기본 꺼짐) — 켜야만 네트워크를 사용합니다. 토큰은 읽기만 하고 refresh하지 않으며,
    사용량 수치만 저장합니다(토큰·계정 식별자 미저장).</p>
  <label><input type="checkbox" name="official_enabled" {% if official_fetch.enabled %}checked{% endif %}> 자동 취득 사용</label>
  <label><input type="checkbox" name="official_claude" {% if official_fetch.claude %}checked{% endif %}> Claude</label>
  <label><input type="checkbox" name="official_codex" {% if official_fetch.codex %}checked{% endif %}> Codex</label>
  <label>최소 취득 간격(분) <input type="number" step="1" min="1" name="min_interval" value="{{ official_fetch.min_interval_minutes }}"></label>
  <p class="muted">엔드포인트 quota는 CLI와 공유됩니다 — 간격을 너무 짧게 두면 한도(429)에 걸릴 수 있습니다(기본 5분).</p>
  <div class="muted">
    마지막 취득 상태:
    {% for p in ["claude", "codex"] %}
      {% set st = official_states[p] %}
      <span>· {{ p }}: {% if st %}{{ st.last_status }}{% if st.last_attempt_at %} ({{ st.last_attempt_at[:16] }}){% endif %}{% else %}미시도{% endif %}</span>
    {% endfor %}
  </div>
  <p class="disclaimer">ⓘ 토큰 만료(401) 시 마지막 값을 유지합니다. Codex는 CLI를 1회 실행하면 토큰이 갱신됩니다.</p>
</section>

<button class="btn" type="submit">저장</button>
</form>

<section class="card">
  <h2>단가 커버리지</h2>
  <p class="muted">pricing.json이 사용 모델을 정확히 매칭하는지 진단합니다.
    상태: <strong class="status-{{ coverage_status[0] }}">{{ coverage_status[1] }}</strong></p>
  <table class="cov-table">
    <thead><tr><th>단가 항목</th><th>매칭된 모델</th><th class="num">토큰</th><th class="num">비중</th></tr></thead>
    <tbody>
      {% for g in coverage_groups %}
        {% for row in g.rows %}
        <tr>
          <td>{% if loop.first %}<code>{{ g.contains }}</code> <span class="muted">{{ g.rate }}</span>{% endif %}</td>
          <td>{{ row.model }}{% if row.status == 'suspect' %} <span title="버전경계 의심">⚠</span>{% endif %}</td>
          <td class="num">{{ row.tokens_h }}</td>
          <td class="num">{{ row.share }}</td>
        </tr>
        {% endfor %}
      {% endfor %}
      {% for row in coverage_unpriced %}
        <tr class="unpriced">
          <td><span class="status-warn">(미식별)</span></td>
          <td>{{ row.model }}</td>
          <td class="num">{{ row.tokens_h }}</td>
          <td class="num">{{ row.share }}</td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
  {% for s in coverage_suspects %}
  <p class="muted">⚠ <code>{{ s }}</code> 가 거친 항목에 매칭됨 — 다른 모델일 수 있으니 단가를 확인하세요.</p>
  {% endfor %}
  <p class="disclaimer">ⓘ 단가 추가·조정: <code>tokenomy.config.json</code> &gt; <code>pricing_overrides</code>에
    <code>{"모델키": {"provider": "...", "input": n, "output": n, "cache_read": n}}</code> 추가(새 모델도 가능). 재ingest로 반영됩니다.</p>
  <p class="disclaimer">ⓘ 단가는 시점 무관·현재 단일 단가로 계산됩니다.</p>
</section>

<section class="card">
  <h2>데이터 · 프라이버시</h2>
  <p class="muted">모든 처리는 로컬에서 이뤄지며 토큰 사용 메타와 <strong>세션 식별용 첫 프롬프트 발췌</strong>만 저장합니다 — <strong>전체 대화 기록은 저장하지 않습니다</strong>.
    데이터는 소스 실행 시 repo의 <code>data/</code>, exe 실행 시 <code>~/.tokenomy/</code>에 쌓입니다.
    공식 사용량 취득은 옵트인 시에만 외부 API를 호출합니다(토큰·계정 식별자 미저장).</p>
</section>
{% endblock %}
```

> Step 5에서 추가한 별도 form 카드는 이 통합으로 대체된다(Step 5는 의도 설명용, 최종 형태는 Step 6). 구현자는 Step 6의 전체 파일을 최종본으로 쓴다.

- [ ] **Step 7: 테스트 통과 확인**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python.exe -m pytest tests/test_web.py -q -k "settings"`
Expected: PASS(신규 2건 + 기존 settings 테스트 회귀 없음)

- [ ] **Step 8: 커밋**

```bash
git add tokenomy/web/app.py tokenomy/web/templates/settings.html tests/test_web.py
git commit -m "feat(official): 설정 UI — 자동 취득 토글/간격 지속 + 마지막 취득 상태"
```

---

## Task 6: 대시보드 새로고침 버튼 + 취득 상태 표면

**Files:**
- Modify: `tokenomy/web/views.py`(`official_fetch_status` 신규 + `overview_context` 배선)
- Modify: `tokenomy/web/templates/overview.html`(공식 패널 헤더 버튼 + 상태/안내)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `official_fetch_settings`(budget); `get_fetch_state`(db).
- Produces:
  - `official_fetch_status(conn, config) -> dict` — `{"enabled": bool, "claude": {...}, "codex": {...}}`. provider별 dict: `{"last_status": str|None, "last_attempt_at": str|None, "last_error": str|None, "note": str|None}`. `note`는 last_status가 `auth_error`면 provider별 안내, `http_error`면 "취득 실패 — 잠시 후 다시", 그 외 None.
  - `overview_context` 반환에 `"official_fetch": official_fetch_status(conn, config)` 추가.

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_web.py`에 추가:

```python
def test_overview_has_refresh_button(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/")
    assert r.status_code == 200
    assert 'action="/official/refresh"' in r.text


def test_overview_shows_auth_error_note(tmp_path, monkeypatch):
    client, fake_connect = _client(tmp_path, monkeypatch)
    # codex 토큰 만료 상태를 심는다
    from tokenomy.db import upsert_fetch_state
    conn = fake_connect()
    upsert_fetch_state(conn, "codex", last_attempt_at="2026-06-10T09:00:00+09:00",
                       last_success_at=None, last_status="auth_error", last_error="HTTP 401")
    r = client.get("/")
    assert "Codex CLI를 1회 실행" in r.text
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python.exe -m pytest tests/test_web.py -q -k "refresh_button or auth_error_note"`
Expected: FAIL — 버튼/안내 문자열 없음

- [ ] **Step 3: `official_fetch_status` 구현** — `tokenomy/web/views.py`. import 보강(budget import 줄에 `official_fetch_settings` 추가, db import 추가):

```python
from tokenomy.budget import (
    budget_from_config, budget_start_kst, credit_to_usd, load_config,
    official_fetch_settings, user_label,
)
from tokenomy.db import get_fetch_state
```

`overview_context` 위(또는 `_provider_has_data` 아래)에 추가:

```python
def _remediation(provider: str, status: str | None) -> str | None:
    if status == "auth_error":
        return ("Codex CLI를 1회 실행해 토큰을 갱신하세요"
                if provider == "codex" else "재로그인이 필요합니다")
    if status == "http_error":
        return "취득 실패 — 잠시 후 다시 시도하세요"
    return None


def official_fetch_status(conn, config: dict) -> dict:
    """공식 취득 옵트인 여부 + provider별 마지막 fetch 상태/안내(표시용)."""
    enabled = official_fetch_settings(config)["enabled"]
    out = {"enabled": enabled}
    for p in ("claude", "codex"):
        st = get_fetch_state(conn, p)
        status = st["last_status"] if st else None
        out[p] = {
            "last_status": status,
            "last_attempt_at": st["last_attempt_at"] if st else None,
            "last_error": st["last_error"] if st else None,
            "note": _remediation(p, status),
        }
    return out
```

- [ ] **Step 4: `overview_context` 배선** — `tokenomy/web/views.py`의 `overview_context` 반환 dict(현재 views.py:95-118)에 한 줄 추가. `claude_official`/`codex_official` 줄 근처에:

```python
        "claude_official": claude_official, "codex_official": codex_official,
        "official_fetch": official_fetch_status(conn, config),
```

- [ ] **Step 5: 템플릿 — 새로고침 버튼 + 상태/안내** — `tokenomy/web/templates/overview.html`의 공식 패널 `<h2>`(현재 line 29)를 헤더+버튼 줄로 교체:

```html
  <div class="card-head">
    <h2>공식 사용량 <span class="muted">· 공식 앱 미러 + 예측</span></h2>
    <form method="post" action="/official/refresh" class="inline-form">
      <button class="btn-sm" type="submit">↻ 공식 새로고침</button>
    </form>
  </div>
  {% if not official_fetch.enabled %}
  <p class="muted">자동 취득이 꺼져 있습니다 — <a href="/settings">설정</a>에서 켜면 공식 사용량을 자동으로 가져옵니다.</p>
  {% endif %}
  {% for p in ["claude", "codex"] %}
    {% if official_fetch[p].note %}
    <p class="muted">⚠ {{ p }}: {{ official_fetch[p].note }}{% if official_fetch[p].last_attempt_at %} <span class="muted">({{ official_fetch[p].last_attempt_at[:16] }})</span>{% endif %}</p>
    {% endif %}
  {% endfor %}
```

> `.card-head`/`.inline-form`/`.btn-sm`는 새 클래스다. 스타일은 기존 토큰을 재사용하되 없으면 `static/src/input.css`의 `@layer components`에 최소 규칙을 추가하고 `.\build_css.ps1`로 `static/app.css`를 재빌드한다(런타임 무빌드 유지 — 산출 app.css는 커밋). 버튼이 동작하고 가독성만 확보되면 충분(기능 우선). CSS 빌드가 어려우면 인라인 없이 기본 `btn` 클래스로 대체 가능.

- [ ] **Step 6: 테스트 통과 확인**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python.exe -m pytest tests/test_web.py -q`
Expected: PASS(신규 2건 + 기존 회귀 없음)

- [ ] **Step 7: 커밋**

```bash
git add tokenomy/web/views.py tokenomy/web/templates/overview.html tokenomy/web/static/app.css tokenomy/web/static/src/input.css tests/test_web.py
git commit -m "feat(official): 대시보드 새로고침 버튼 + 취득 상태/만료 안내 표면"
```

> CSS를 건드리지 않았다면 `git add`에서 css 파일은 빼고 커밋한다.

---

## Task 7: ingest 비차단 자동 취득 훅

**Files:**
- Modify: `tokenomy/cli.py`(`_official_fetch_worker` 신규 + `cmd_ingest` 훅)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `fetch_provider`(Task 3); `official_fetch_settings`(budget); `connect`(db); `KST`(aggregate).
- Produces:
  - `_official_fetch_worker(config: dict, now_kst, *, connect_fn=connect) -> None` — 옵트인 시 자기 conn으로 enabled provider를 순회 fetch. 모든 예외 삼킴. 비옵트인이면 즉시 반환(네트워크 없음).
  - `cmd_ingest`가 옵트인 시 `_official_fetch_worker`를 **데몬 스레드**로 기동(起動·ingest 비차단). 옵트인 off면 스레드 미생성.

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_cli.py`에 추가:

```python
import tokenomy.cli as cli_module
from tokenomy.budget import KST as _BKST  # noqa: F401  (KST는 aggregate에서 import)


def test_official_worker_skips_when_disabled(monkeypatch):
    # 옵트인 off → fetch_provider 미호출(네트워크 0)
    called = []
    monkeypatch.setattr(cli_module, "fetch_provider",
                        lambda p, **k: called.append(p))
    cli_module._official_fetch_worker({}, datetime(2026, 6, 10, 9, tzinfo=KST))
    assert called == []


def test_official_worker_fetches_enabled_providers(monkeypatch):
    called = []
    monkeypatch.setattr(cli_module, "fetch_provider",
                        lambda p, **k: called.append(p))
    cfg = {"official_fetch": {"enabled": True, "claude": True, "codex": False}}
    cli_module._official_fetch_worker(
        cfg, datetime(2026, 6, 10, 9, tzinfo=KST),
        connect_fn=lambda: connect(":memory:"))
    assert called == ["claude"]   # codex 토글 off


def test_official_worker_swallows_exceptions(monkeypatch):
    def boom(p, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(cli_module, "fetch_provider", boom)
    cfg = {"official_fetch": {"enabled": True}}
    # 예외를 삼켜 worker가 깨지지 않는다
    cli_module._official_fetch_worker(
        cfg, datetime(2026, 6, 10, 9, tzinfo=KST),
        connect_fn=lambda: connect(":memory:"))
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python.exe -m pytest tests/test_cli.py -q -k official_worker`
Expected: FAIL — `AttributeError: module 'tokenomy.cli' has no attribute '_official_fetch_worker'`

- [ ] **Step 3: worker + 훅 구현** — `tokenomy/cli.py`.

import 보강(상단):

```python
import threading
from tokenomy.budget import budget_from_config, load_config, user_label, credit_to_usd, official_fetch_settings
from tokenomy.official_fetch import fetch_provider
```

`cmd_official_import` 아래(또는 `cmd_ingest` 위)에 worker 추가:

```python
def _official_fetch_worker(config: dict, now_kst, *, connect_fn=connect) -> None:
    """옵트인 시 공식 사용량을 취득(자기 conn). 모든 예외 삼킴 — 起動/ingest 비차단용.

    cmd_ingest에서 데몬 스레드로 호출된다. 스레드는 자기 sqlite conn을 연다
    (sqlite conn은 스레드 간 공유 금지). 비옵트인이면 즉시 반환(네트워크 없음).
    """
    settings = official_fetch_settings(config)
    if not settings["enabled"]:
        return
    try:
        conn = connect_fn()
        for p in ("claude", "codex"):
            if settings.get(p, True):
                fetch_provider(p, now_kst=now_kst, config=config, conn=conn)
    except Exception as e:   # 자동 취득 실패는 치명적이지 않음
        print(f"[official] 자동 취득 건너뜀: {e}")
```

`cmd_ingest`에 훅 추가 — 현재 `cmd_ingest`(cli.py:27-45)는 `load_config()`를 인라인 호출한다. 한 번 바인딩하고 끝에서 스레드 기동:

```python
def cmd_ingest(conn) -> None:
    config = load_config()
    pricing = apply_pricing_overrides(load_pricing(), config.get("pricing_overrides"))
    n_claude = ingest_root(conn, CLAUDE_ROOT, pricing, provider="claude")
    n_arch = archive_tree(CLAUDE_ROOT, conn, provider="claude")
    n_codex = ingest_codex(conn, CODEX_ROOT, pricing)
    archive_tree(CODEX_ROOT, conn, provider="codex")
    n_titles = ingest_titles(conn, CLAUDE_ROOT)
    n_turns = ingest_user_turns(conn, CLAUDE_ROOT)
    repriced = maybe_reprice(conn, pricing)
    record_ingest(conn, datetime.now(KST))
    msg = (
        f"[ingest] claude={n_claude}  codex={n_codex}  "
        f"archived_files={n_arch}  titles={n_titles}  turns={n_turns}  new records"
    )
    if repriced:
        msg += f"\n[reprice] 단가 변경 감지 — 기존 {repriced}행 비용 재계산"
    print(msg)
    # 공식 사용량 자동 취득(옵트인) — 데몬 스레드로 분리해 起動/ingest를 블록하지 않는다.
    if official_fetch_settings(config)["enabled"]:
        threading.Thread(
            target=_official_fetch_worker, args=(config, datetime.now(KST)),
            daemon=True,
        ).start()
```

> 데몬 스레드는 자기 conn(`connect()` 기본 경로)을 연다 — cmd_ingest의 `conn`을 스레드와 공유하지 않는다(sqlite 스레드 안전성). 옵트인 off(기본)면 스레드를 만들지 않으므로 기존 테스트(POST /ingest 등)에 영향 0.

- [ ] **Step 4: 테스트 통과 확인**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python.exe -m pytest tests/test_cli.py -q`
Expected: PASS(신규 3건 + 기존 2건)

- [ ] **Step 5: cmd_ingest 비차단 회귀 확인(기존 web 테스트)**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python.exe -m pytest tests/test_web.py -q -k ingest`
Expected: PASS — 기본 config(옵트인 off)라 스레드 미생성, 회귀 없음

- [ ] **Step 6: 커밋**

```bash
git add tokenomy/cli.py tests/test_cli.py
git commit -m "feat(official): ingest 비차단 자동 취득 훅(옵트인·데몬스레드·자기conn)"
```

---

## Task 8: 문서 갱신 + 전체 스위트 green

**Files:**
- Modify: `CLAUDE.md`(아키텍처 다이어그램·핵심 게시·환경변수)
- Modify: `README.md`(공식 사용량 자동 취득 섹션 — 옵트인·프라이버시)
- Test: 전체 스위트

**Interfaces:** 없음(문서/검증).

- [ ] **Step 1: CLAUDE.md 아키텍처 다이어그램에 official_fetch 추가** — `## 아키텍처`의 데이터 파이프라인 블록에 아웃바운드 1줄 추가. 기존:

```
~/.claude/projects/**/*.jsonl      ─ parser.py ───────┐
~/.codex/sessions/**/rollout-*     ─ codex_parser.py ─┤→ UsageRecord → db.py(SQLite) → aggregate.py ─┬→ cli.py (report)
                                                      │                                              └→ web/ (FastAPI+Jinja2)
                                                 archive.py (raw 30일 휘발 전 원문 보존)
```

아래에 추가:

```
공식 사용량 API(옵트인) ── official_fetch.py(유일한 아웃바운드, ≤3s, 백오프 없음) ─ raw JSON ─ official_parser.py ─→ db.py(official_buckets)
```

- [ ] **Step 2: CLAUDE.md 모듈 설명 추가** — `- **parser.py / codex_parser.py**` 항목 근처에 추가:

```markdown
- **official_fetch.py** — 공식 사용량 라이브 취득(유일한 아웃바운드). 각 CLI의 로컬 OAuth 토큰을
  **읽기 전용**으로 사용해 공식 API를 단발 GET(≤3s/provider, 백오프 없음). 옵트인(`official_fetch.enabled`
  기본 false) + provider 토글 + throttle(`min_interval_minutes` 기본 5). 401→auth_error, 그 외 실패→http_error,
  **마지막 스냅샷·last_success_at 보존**. PII(토큰/account_id) 미저장 — 헤더에 쓰고 버린다.
  트리거: 웹 `POST /official/refresh`(1차) + `cmd_ingest` 데몬스레드 훅(보조, 비차단). 표준 라이브러리만.
```

- [ ] **Step 3: CLAUDE.md 핵심 게시 + 환경변수 추가** — `## 핵심 게시(gotchas)` 끝에:

```markdown
- **공식 사용량 취득은 옵트인·비차단.** `official_fetch.enabled` 기본 false면 네트워크 0. on이어도
  `cmd_ingest`는 fetch를 **데몬 스레드**(자기 sqlite conn)로 분리해 起動(`launcher._safe_ingest`)을 막지 않는다.
  타임아웃 ≤3s, **백오프 없음**(단발 시도, 실패 즉시 포기). throttle은 우리 호출 빈도만 제어(엔드포인트 quota는 CLI와 공유 — 충돌 못 막음).
- **토큰은 읽기 전용, refresh 금지.** Claude `~/.claude/.credentials.json`, Codex `~/.codex/auth.json`을 읽기만.
  Codex 401(토큰 만료)은 마지막 값 유지 + "Codex CLI 1회 실행" 안내(직접 refresh 안 함).
```

`## 환경변수`에 추가:

```markdown
- `TOKENOMY_SKIP_OFFICIAL_FETCH` — 설정 시 공식 사용량 라이브 취득을 항상 skip(오프라인/CI/테스트).
```

- [ ] **Step 4: README.md 섹션 추가** — README.md에 "공식 사용량" 관련 섹션이 있으면 자동 취득 옵트인을 명시하고, 없으면 기능 목록에 한 줄 추가:

```markdown
### 공식 사용량 자동 취득(옵트인)

설정에서 켜면(기본 꺼짐) 각 CLI의 로컬 OAuth 토큰으로 공식 사용량 API를 읽기 전용 단발 호출해
공식 앱과 같은 버킷(Claude 월 한도·이벤트 크레딧 / Codex 월간 크레딧)을 미러링한다.
토큰은 읽기만 하고 refresh하지 않으며, **사용량 수치만 저장**한다(토큰·계정 식별자 미저장).
네트워크는 옵트인 시에만 사용(`official_fetch.enabled`). 환경변수 `TOKENOMY_SKIP_OFFICIAL_FETCH`로 강제 차단 가능.
```

> README에 기존 "공식 사용량 수동 입력" 서술이 남아 있으면 삭제/대체한다(Phase 1에서 수동 입력은 제거됨).

- [ ] **Step 5: 전체 스위트 실행**

Run: `cd "C:/projects/samsung/tokenomy/.claude/worktrees/feat+official-usage-auto-fetch"; TOKENOMY_SKIP_UPDATE_CHECK=1 "C:/projects/samsung/tokenomy/.venv/Scripts/python.exe" -m pytest -q -p no:cacheprovider`
Expected: 신규 포함 전부 PASS. `test_launcher.py` 포트 8765 2건만 known-env 실패(회귀 아님). 그 외 0 fail.

- [ ] **Step 6: 커밋**

```bash
git add CLAUDE.md README.md
git commit -m "docs(official): 라이브 취득(Phase 2) 아키텍처·게시·환경변수 갱신"
```

---

## 라이브 스모크(수동 — 구현 후 경식님 실행)

자동 테스트는 stub transport라 네트워크를 타지 않는다. 실제 검증은 경식님이 수동으로:

1. **개인계정(집/원격)** — `tokenomy.config.json`에 `official_fetch.enabled=true` 설정 → 대시보드 "↻ 공식 새로고침" → fetch→인증→파싱→적재→표시 전 경로 라이브. 개인 구독은 `five_hour`/`seven_day` % 창만 나옴(enterprise 버킷/USD 환산은 fixture로만 검증). 401 나오면 Codex CLI 1회 실행 후 재시도.
2. **enterprise(사내망)** — 같은 토글로 1회 새로고침 → Claude 버킷(월 한도·이벤트 크레딧 USD)·Codex 월간 크레딧 실값 적재/표시 확인. 사내 TLS 인터셉트 프록시 환경에서 호출 성공 여부도 함께 확인(실패 시 http_error로 떨어지고 마지막 값 유지되는지).

스모크 결과는 별도 보고(코드 변경 아님). 사내망 보안 이슈 없는 범위에서만.

---

## Self-Review (작성자 점검)

**1. 스펙 커버리지(§7·§9·§11 Phase 2):**
- 옵트인 + provider 토글 + `TOKENOMY_SKIP_OFFICIAL_FETCH` → Task 1·3 ✅
- throttle(min_interval, state 미끄러짐 방지) → Task 3 ✅
- HTTP 타임아웃 ≤3s, 백오프 없음 → Task 3 ✅
- 起動 비차단(데몬스레드·자기 conn) → Task 7 ✅
- 토큰 소스(읽기전용)·스키마 드리프트→auth_error → Task 2·3 ✅
- 에러 분류(401→auth_error, 그 외→http_error, last_success 보존) → Task 3 ✅
- `POST /official/refresh`(결과무관 redirect, 백오프 없음) → Task 4 ✅
- settings 토글/간격/상태 → Task 5 ✅
- 새로고침 버튼 + "마지막 업데이트"·만료 안내 → Task 6 ✅
- `credit_to_usd` 주입(fetch_provider가 config에서) → Task 3 ✅
- PII 미저장 → Task 3 테스트 ✅
- 문서/환경변수 → Task 8 ✅
- **의도적 제외(YAGNI, 스펙 §9 "reset_cycle 편집"):** Claude=월·Codex=주는 provider 청구 주기로 **고정**이라 사용자 편집 필드는 오설정만 부른다(현재 aggregate가 config의 reset_cycle을 읽지도 않음). 노출하지 않는다. — *실행 시 리뷰어가 스펙 위반으로 볼 수 있으니 컨트롤러가 판단(전역 원칙: 근거 있는 제외).*
- **stale 데이터 경고**: rows가 있으면 official_view.status="ok"라, 마지막 fetch가 실패해도 패널 본체는 "ok"로 보인다. Task 6의 `official_fetch_status`가 **별도 상태줄**로 fetch 실패를 표면화해 보완(데이터는 보존하면서 실패를 알림). 스펙 §9 "마지막 업데이트 N분 전 + 안내" 의도 충족.

**2. 플레이스홀더 스캔:** 모든 코드 스텝에 실제 코드 포함. TBD/TODO 없음.

**3. 타입 정합:** `fetch_provider(provider, *, now_kst, config, conn, urlopen)` 시그니처가 Task 3 정의 = Task 4 라우트 호출 = Task 7 worker 호출에서 일치. `official_fetch_settings` 반환 키(`enabled/claude/codex/min_interval_minutes`)가 Task 1 정의 = Task 3·5·6·7 사용에서 일치. `FetchResult(provider,status,note,bucket_count)` 일관. `upsert_fetch_state`/`get_fetch_state`/`insert_official_buckets` 시그니처는 Phase 1 db.py 그대로 사용(변경 없음).

**4. 모호성:** Task 5의 두-form 문제(예산 덮어쓰기)는 Step 6의 단일-form 통합으로 명시 해소. Task 6 CSS는 기능 우선(빌드 어려우면 기본 btn 대체) 명시.
