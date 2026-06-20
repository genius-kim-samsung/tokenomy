"""공식 사용량 라이브 취득 — 토큰 리더 + fetch_provider 전 경로(stub transport)."""
from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tokenomy.aggregate import KST
from tokenomy.db import connect, get_fetch_state, latest_official_snapshot
from tokenomy.official_fetch import (
    AuthError, FetchResult, _read_claude_token, _read_codex_auth, fetch_provider,
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


# ---------------------------------------------------------------------------
# fetch_provider 테스트 — stub transport 사용
# ---------------------------------------------------------------------------

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
