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
_CFG_ON = {"tracked_providers": ["claude", "codex"], "credit_to_usd": 0.04,
           "official_fetch": {"min_interval_minutes": 5}}


def _memory_conn():
    """인메모리 SQLite 연결 — 테스트 헬퍼."""
    return connect(":memory:")


def _boom(req, timeout=None):
    """urlopen stub — 호출 시 예외(네트워크 미호출 검증용)."""
    raise AssertionError("network must not be called")


def _patch_creds(monkeypatch, tmp_path):
    """fetch_provider가 읽을 크레덴셜 파일 경로를 tmp로 바꾼다.
    paths.CLAUDE_CREDS/CODEX_AUTH → creds_present 감지용, of.CLAUDE_CREDS/CODEX_AUTH → 실제 읽기용.
    """
    import tokenomy.official_fetch as of
    from tokenomy import paths
    cp = tmp_path / "claude.json"
    cp.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-x"}}), encoding="utf-8")
    xp = tmp_path / "codex.json"
    xp.write_text(json.dumps({"tokens": {"access_token": "jwt", "account_id": "acc"}}),
                  encoding="utf-8")
    monkeypatch.setattr(of, "CLAUDE_CREDS", cp)
    monkeypatch.setattr(of, "CODEX_AUTH", xp)
    monkeypatch.setattr(paths, "CLAUDE_CREDS", cp)
    monkeypatch.setattr(paths, "CODEX_AUTH", xp)


def test_fetch_skips_untracked_provider(tmp_path, monkeypatch):
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    conn = _memory_conn()
    cfg = {"tracked_providers": ["claude"]}
    res = fetch_provider("codex", now_kst=_NOW, config=cfg, conn=conn, urlopen=_boom)
    assert res.status == "disabled"


def test_fetch_silent_skip_when_creds_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    from tokenomy import paths
    monkeypatch.setattr(paths, "creds_present", lambda p: False)
    conn = _memory_conn()
    res = fetch_provider("claude", now_kst=_NOW, config={"tracked_providers": ["claude"]},
                         conn=conn, urlopen=_boom)
    assert res.status == "disabled"
    assert res.note == "creds_absent"
    # state는 기록되지 않아야 함(거짓 auth_error 방지)
    assert get_fetch_state(conn, "claude") is None


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
    # 올바른 엔드포인트 호출
    assert op.calls and op.calls[0][0] == "https://chatgpt.com/backend-api/wham/usage"
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


# ---------------------------------------------------------------------------
# refresh_tracked — tracked 전체 자동 갱신 헬퍼(起動 hx-load·폴링·수동 전체갱신 공용)
# ---------------------------------------------------------------------------

def test_refresh_tracked_skips_when_no_tracked(monkeypatch):
    """tracked_providers 없음 → fetch 미호출, 빈 결과."""
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    calls = []
    monkeypatch.setattr("tokenomy.official_fetch.fetch_provider", lambda p, **k: calls.append(p))
    import tokenomy.config as b
    monkeypatch.setattr(b, "creds_present", lambda p: False)
    from tokenomy.official_fetch import refresh_tracked
    res = refresh_tracked({}, now_kst=_NOW, conn=connect(":memory:"))
    assert res == [] and calls == []


def test_refresh_tracked_fetches_each_tracked(monkeypatch):
    """tracked=[claude] → claude만 fetch, FetchResult 리스트 반환."""
    calls = []
    monkeypatch.setattr("tokenomy.official_fetch.fetch_provider",
                        lambda p, **k: (calls.append((p, k.get("manual"))) or FetchResult(p, "ok")))
    from tokenomy.official_fetch import refresh_tracked
    res = refresh_tracked({"tracked_providers": ["claude"]}, now_kst=_NOW,
                          conn=connect(":memory:"), manual=True)
    assert calls == [("claude", True)]                # manual 인자 전달
    assert [r.provider for r in res] == ["claude"]


def test_refresh_tracked_explicit_providers_override(monkeypatch):
    """providers를 명시하면 tracked 대신 그 집합만 갱신한다(카드별 개별 갱신용)."""
    calls = []
    monkeypatch.setattr("tokenomy.official_fetch.fetch_provider",
                        lambda p, **k: (calls.append(p) or FetchResult(p, "ok")))
    from tokenomy.official_fetch import refresh_tracked
    refresh_tracked({"tracked_providers": ["claude", "codex"]}, now_kst=_NOW,
                    conn=connect(":memory:"), providers=["claude"])
    assert calls == ["claude"]


def test_refresh_tracked_swallows_exceptions(monkeypatch):
    """한 provider fetch가 터져도 전체가 죽지 않는다(비차단)."""
    def boom(p, **k):
        raise RuntimeError("down")
    monkeypatch.setattr("tokenomy.official_fetch.fetch_provider", boom)
    from tokenomy.official_fetch import refresh_tracked
    # 예외 없이 반환되어야 한다
    refresh_tracked({"tracked_providers": ["claude"]}, now_kst=_NOW, conn=connect(":memory:"))


def test_fetch_manual_bypasses_throttle(monkeypatch, tmp_path):
    """수동 갱신(manual=True)은 throttle 윈도우 안이어도 실제 호출한다 — 사용자 명시 의사 우선."""
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    _patch_creds(monkeypatch, tmp_path)
    conn = connect(":memory:")
    from tokenomy.db import upsert_fetch_state
    prev = (_NOW - timedelta(minutes=2)).isoformat()   # 5분 미달 → 자동이면 throttled
    upsert_fetch_state(conn, "claude", last_attempt_at=prev, last_success_at=prev,
                       last_status="ok", last_error=None)
    op = _opener(payload=_CLAUDE_RAW)
    r = fetch_provider("claude", now_kst=_NOW, config=_CFG_ON, conn=conn,
                       urlopen=op, manual=True)
    assert r.status == "ok"            # throttle 무시하고 실제 취득
    assert op.calls                    # 네트워크가 실제로 호출됨
    # 수동 시도도 last_attempt_at을 갱신(이후 자동 폴링의 throttle 기준이 됨)
    assert get_fetch_state(conn, "claude")["last_attempt_at"] == _NOW.isoformat()


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


def test_fetch_bad_creds_file_is_auth_error(monkeypatch, tmp_path):
    """크레덴셜 파일은 있지만 내용이 깨진 경우 → auth_error (creds_present=True이지만 읽기 실패)."""
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    import tokenomy.official_fetch as of
    from tokenomy import paths
    # creds_present가 True를 반환하도록 paths.CLAUDE_CREDS를 실존 파일로 패치
    dummy = tmp_path / "dummy.json"
    dummy.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(paths, "CLAUDE_CREDS", dummy)
    # _read_claude_token이 읽는 of.CLAUDE_CREDS는 스키마가 깨진 파일로 패치
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(of, "CLAUDE_CREDS", bad)
    conn = connect(":memory:")
    r = fetch_provider("claude", now_kst=_NOW, config=_CFG_ON, conn=conn, urlopen=_never)
    assert r.status == "auth_error"
