"""공식 사용량 라이브 취득 — 토큰 리더 + fetch_provider 전 경로(stub transport)."""
from __future__ import annotations

import json
import os
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tokenomy.aggregate import KST
from tokenomy.db import (
    connect, get_fetch_state, latest_official_snapshot,
    get_official_raw, list_official_raw,
)
from tokenomy.official_fetch import (
    AuthError, FetchResult, _auto_refresh_allowed, _read_claude_token, _read_codex_auth,
    ensure_fresh_claude_token, fetch_provider, refresh_claude_token,
)


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path_factory, monkeypatch):
    """기본 config 경로 격리 — refresh_tracked가 성공 취득 후 account_mode를 자동 시드·영속하므로
    (ADR 0015) 이 모듈의 어떤 테스트도 개인/레포 config를 건드리지 않게 한다. 명시적으로
    TOKENOMY_CONFIG를 다시 setenv하는 테스트는 그 값이 우선한다(나중 호출이 이김)."""
    monkeypatch.setenv("TOKENOMY_CONFIG",
                       str(tmp_path_factory.mktemp("of_cfg") / "tokenomy.config.json"))


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


# ---------------------------------------------------------------------------
# ProviderSpec 레지스트리 — 완전성 + 미등록 provider fail-loud
# ---------------------------------------------------------------------------

def test_provider_specs_cover_all_providers():
    """레지스트리가 domain.PROVIDERS와 정확히 일치 — 새 provider 누락/유령 spec 방지."""
    from tokenomy.official_fetch import PROVIDER_SPECS
    from tokenomy import domain
    assert set(PROVIDER_SPECS) == set(domain.PROVIDERS)


def test_fetch_unknown_provider_is_disabled_fail_loud(monkeypatch):
    """미등록 provider는 tracked에 넣어도 레지스트리 게이트에서 'unknown provider'로 거부.

    tracked/creds 게이트보다 레지스트리 체크가 먼저임을 검증(gemini를 tracked에 넣어도
    creds_absent가 아니라 unknown provider가 나와야 함). state도 기록하지 않는다."""
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    conn = _memory_conn()
    cfg = {"tracked_providers": ["gemini"], "credit_to_usd": 0.04}
    res = fetch_provider("gemini", now_kst=_NOW, config=cfg, conn=conn, urlopen=_never)
    assert res.status == "disabled"
    assert res.note == "unknown provider"
    assert get_fetch_state(conn, "gemini") is None


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
# 공식 raw 포착(official_raw) — ADR 0014. 성공/PII스크럽/parse_error/http_error 바디.
# ---------------------------------------------------------------------------

class _TextResp:
    """비-JSON 응답 바디 stub(파싱 실패 경로용)."""
    def __init__(self, text): self._b = text.encode("utf-8")
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._b


def _text_opener(text):
    def _open(req, timeout=None): return _TextResp(text)
    return _open


def test_fetch_success_captures_scrubbed_raw(monkeypatch, tmp_path):
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    _patch_creds(monkeypatch, tmp_path)
    conn = connect(":memory:")
    fetch_provider("claude", now_kst=_NOW, config=_CFG_ON, conn=conn,
                   urlopen=_opener(payload=_CLAUDE_RAW))
    row = get_official_raw(conn, "claude", _NOW.isoformat())
    assert row is not None
    assert row["status"] == "ok" and row["http_code"] == 200
    assert json.loads(row["raw_text"])["spend"]["used"]["amount_minor"] == 3000


def test_fetch_codex_raw_redacts_pii(monkeypatch, tmp_path):
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    _patch_creds(monkeypatch, tmp_path)
    conn = connect(":memory:")
    # PII 값은 키 이름과 겹치지 않게(값만 가려지는지 정확히 보려고).
    payload = {"user_id": "USERZZZ", "account_id": "ACCTZZZ",
               "email": "secret@x.z", **_CODEX_RAW}
    fetch_provider("codex", now_kst=_NOW, config=_CFG_ON, conn=conn,
                   urlopen=_opener(payload=payload))
    row = get_official_raw(conn, "codex", _NOW.isoformat())
    assert row is not None
    # PII 값이 사라지고 [redacted]로 대체
    assert "USERZZZ" not in row["raw_text"]
    assert "ACCTZZZ" not in row["raw_text"]
    assert "secret@x.z" not in row["raw_text"]
    assert json.loads(row["raw_text"])["user_id"] == "[redacted]"
    assert json.loads(row["raw_text"])["account_id"] == "[redacted]"
    # 사용량 수치는 보존
    assert json.loads(row["raw_text"])["spend_control"]["individual_limit"]["used"] == "500.0"


def test_fetch_non_json_200_is_parse_error_and_captures_body(monkeypatch, tmp_path):
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    _patch_creds(monkeypatch, tmp_path)
    conn = connect(":memory:")
    r = fetch_provider("claude", now_kst=_NOW, config=_CFG_ON, conn=conn,
                       urlopen=_text_opener("<html>oops</html>"))
    assert r.status == "http_error"   # 외부 동작은 그대로(파싱 실패=http_error)
    row = get_official_raw(conn, "claude", _NOW.isoformat())
    assert row is not None and row["status"] == "parse_error"
    assert "<html>oops</html>" in row["raw_text"]


def test_fetch_http_error_captures_error_body(monkeypatch, tmp_path):
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    _patch_creds(monkeypatch, tmp_path)
    conn = connect(":memory:")
    import io
    err = urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO(b'{"error":"boom"}'))
    r = fetch_provider("claude", now_kst=_NOW, config=_CFG_ON, conn=conn,
                       urlopen=_opener(exc=err))
    assert r.status == "http_error"
    row = get_official_raw(conn, "claude", _NOW.isoformat())
    assert row is not None and row["status"] == "http_error" and row["http_code"] == 500
    assert "boom" in row["raw_text"]


def test_fetch_bodiless_401_skips_raw_capture(monkeypatch, tmp_path):
    """빈 바디(fp=None) 401은 official_raw에 빈 행을 남기지 않는다(상태는 fetch_state에)."""
    monkeypatch.delenv("TOKENOMY_SKIP_OFFICIAL_FETCH", raising=False)
    _patch_creds(monkeypatch, tmp_path)
    conn = connect(":memory:")
    err = urllib.error.HTTPError("u", 401, "unauth", {}, None)
    fetch_provider("codex", now_kst=_NOW, config=_CFG_ON, conn=conn,
                   urlopen=_opener(exc=err))
    assert get_official_raw(conn, "codex", _NOW.isoformat()) is None


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


# --- 백그라운드 공식 갱신 폴 루프(ADR 0007) ---

import threading
from tokenomy.official_fetch import background_poll_loop


class _FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_background_poll_loop_disabled_does_not_poll():
    calls = []
    background_poll_loop(
        {"official_fetch": {"background_poll": False}},
        conn_factory=lambda: _FakeConn(),
        now_fn=lambda: "T",
        stop_event=threading.Event(),
        sleep_fn=lambda s: None,
        refresh_fn=lambda *a, **k: calls.append(1),
    )
    assert calls == []


def test_background_poll_loop_polls_until_stop():
    stop = threading.Event()
    calls = []
    intervals = []

    def fake_sleep(sec):
        intervals.append(sec)
        if len(intervals) >= 2:
            stop.set()

    def fake_refresh(config, *, now_kst, conn, manual):
        calls.append((now_kst, manual))

    background_poll_loop(
        {"official_fetch": {"min_interval_minutes": 10, "background_poll": True}},
        conn_factory=lambda: _FakeConn(),
        now_fn=lambda: "T",
        stop_event=stop,
        sleep_fn=fake_sleep,
        refresh_fn=fake_refresh,
    )
    assert calls == [("T", False), ("T", False)]   # manual=False(자동, throttle 적용)
    assert intervals == [600, 600]                  # min_interval_minutes × 60


def test_background_poll_loop_swallows_refresh_errors():
    stop = threading.Event()
    n = []

    def fake_sleep(sec):
        n.append(1)
        if len(n) >= 2:
            stop.set()

    def boom(*a, **k):
        raise RuntimeError("network down")

    background_poll_loop(
        {"official_fetch": {"background_poll": True}},
        conn_factory=lambda: _FakeConn(),
        now_fn=lambda: "T",
        stop_event=stop,
        sleep_fn=fake_sleep,
        refresh_fn=boom,
    )
    assert len(n) == 2   # 예외에도 루프가 계속 돌고 정상 종료


def test_background_poll_loop_closes_conn():
    fc = _FakeConn()
    stop = threading.Event()
    stop.set()           # 즉시 멈춤(루프 0회) — 그래도 conn 생성 후 종료
    background_poll_loop(
        {"official_fetch": {"background_poll": True}},
        conn_factory=lambda: fc,
        now_fn=lambda: "T",
        stop_event=stop,
        sleep_fn=lambda s: None,
        refresh_fn=lambda *a, **k: None,
    )
    assert fc.closed is True


# ---------------------------------------------------------------------------
# refresh_tracked — account_mode 자동 시드(첫 공식 취득 성공 때, ADR 0015 1단계)
# ---------------------------------------------------------------------------

from tokenomy.config import account_mode, load_config
from tokenomy.db import insert_official_buckets
from tokenomy.official_parser import OfficialBucket


def _ob(key, kind, used_usd, limit_usd, raw="r", resets=None):
    """테스트용 official 버킷 — USD 단위. limit_usd None이면 한도 없는 버킷(rate_window 등)."""
    return OfficialBucket(
        bucket_key=key, raw_key=raw, bucket_kind=kind, label=key, native_unit="usd",
        used_native=used_usd, limit_native=limit_usd,
        remaining_native=(limit_usd - used_usd) if limit_usd else None,
        used_usd=used_usd, limit_usd=limit_usd,
        remaining_usd=(limit_usd - used_usd) if limit_usd else None,
        utilization=0.0, resets_at=resets,
    )


def _seed_buckets(conn, provider, buckets):
    insert_official_buckets(conn, provider=provider, fetched_at=_NOW.isoformat(),
                            buckets=buckets, created_at=_NOW.isoformat())


def _stub_fetch(monkeypatch, status="ok"):
    """fetch_provider를 네트워크 없이 status 결과로 스텁(버킷은 테스트가 미리 적재)."""
    monkeypatch.setattr("tokenomy.official_fetch.fetch_provider",
                        lambda p, **k: FetchResult(p, status))


def test_refresh_tracked_seeds_enterprise_on_usd_budget(tmp_path, monkeypatch):
    # 첫 공식 취득 성공 + USD 예산 버킷 존재 → account_mode 자동 시드 enterprise·영속(sticky).
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "c.json"))
    _stub_fetch(monkeypatch, "ok")
    conn = _memory_conn()
    _seed_buckets(conn, "claude", [_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend")])
    from tokenomy.official_fetch import refresh_tracked
    cfg = {"tracked_providers": ["claude"], "credit_to_usd": 0.04}
    refresh_tracked(cfg, now_kst=_NOW, conn=conn)
    assert account_mode(cfg) == "enterprise"
    assert load_config(tmp_path / "c.json")["account_mode"] == "enterprise"   # 파일에 영속


def test_refresh_tracked_seeds_subscription_when_no_usd_budget(tmp_path, monkeypatch):
    # 성공했으나 USD 한도 버킷 없음(rate_window만) → subscription으로 시드.
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "c.json"))
    _stub_fetch(monkeypatch, "ok")
    conn = _memory_conn()
    _seed_buckets(conn, "codex", [_ob("rate_window", "rate_window", None, None, raw="five_hour")])
    from tokenomy.official_fetch import refresh_tracked
    cfg = {"tracked_providers": ["codex"], "credit_to_usd": 0.04}
    refresh_tracked(cfg, now_kst=_NOW, conn=conn)
    assert account_mode(cfg) == "subscription"


def test_refresh_tracked_respects_explicit_account_mode(tmp_path, monkeypatch):
    # 명시 설정이면 USD 예산이 와도 덮어쓰지 않는다(사용자 토글 우선·sticky).
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "c.json"))
    _stub_fetch(monkeypatch, "ok")
    conn = _memory_conn()
    _seed_buckets(conn, "claude", [_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend")])
    from tokenomy.official_fetch import refresh_tracked
    cfg = {"tracked_providers": ["claude"], "credit_to_usd": 0.04,
           "account_mode": "subscription"}
    refresh_tracked(cfg, now_kst=_NOW, conn=conn)
    assert account_mode(cfg) == "subscription"


def test_refresh_tracked_no_seed_without_successful_fetch(tmp_path, monkeypatch):
    # 이번 사이클에 성공(ok)이 없으면(전부 실패) 미확정 유지 — 콜드 실패에 subscription 오시드 방지.
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "c.json"))
    _stub_fetch(monkeypatch, "http_error")
    conn = _memory_conn()
    from tokenomy.official_fetch import refresh_tracked
    cfg = {"tracked_providers": ["claude"], "credit_to_usd": 0.04}
    refresh_tracked(cfg, now_kst=_NOW, conn=conn)
    assert account_mode(cfg) is None


# ---------------------------------------------------------------------------
# refresh_claude_token — OAuth refresh + atomic write-back (ADR 0021)
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, text): self._t = text
    def read(self): return self._t.encode("utf-8")
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _creds(tmp_path, exp_ms=1000):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "old-acc", "refreshToken": "old-ref",
        "expiresAt": exp_ms, "scopes": ["x"], "subscriptionType": "max"}}),
        encoding="utf-8")
    return p


def test_refresh_success_writes_back_and_preserves_keys(tmp_path):
    p = _creds(tmp_path)
    body = json.dumps({"access_token": "new-acc", "refresh_token": "new-ref",
                       "expires_in": 28800})
    new = refresh_claude_token(p, now_ms=1_000_000, urlopen=lambda req, timeout: _Resp(body))
    assert new == "new-acc"
    o = json.loads(p.read_text(encoding="utf-8"))["claudeAiOauth"]
    assert o["accessToken"] == "new-acc"
    assert o["refreshToken"] == "new-ref"                 # rotation 기록
    assert o["expiresAt"] == 1_000_000 + 28800 * 1000     # now_ms + expires_in*1000
    assert o["subscriptionType"] == "max"                 # 기존 키 보존


def test_refresh_http_error_leaves_file_untouched(tmp_path):
    p = _creds(tmp_path)
    before = p.read_text(encoding="utf-8")
    def _boom(req, timeout):
        raise urllib.error.HTTPError("u", 403, "forbidden", {}, None)
    assert refresh_claude_token(p, now_ms=1_000_000, urlopen=_boom) is None
    assert p.read_text(encoding="utf-8") == before         # 무손상


def test_refresh_bad_schema_leaves_file_untouched(tmp_path):
    p = _creds(tmp_path)
    before = p.read_text(encoding="utf-8")
    body = json.dumps({"unexpected": "shape"})             # access_token 없음
    assert refresh_claude_token(p, now_ms=1, urlopen=lambda req, timeout: _Resp(body)) is None
    assert p.read_text(encoding="utf-8") == before


def test_refresh_write_failure_returns_none_and_cleans_tmp(tmp_path, monkeypatch):
    import tokenomy.official_fetch as of
    p = _creds(tmp_path)                       # Task 3에서 정의한 헬퍼 재사용
    before = p.read_text(encoding="utf-8")
    body = json.dumps({"access_token": "new-acc", "refresh_token": "new-ref", "expires_in": 28800})
    def _boom_replace(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(of.os, "replace", _boom_replace)
    assert of.refresh_claude_token(p, now_ms=1, urlopen=lambda req, timeout: _Resp(body)) is None
    assert p.read_text(encoding="utf-8") == before          # 원본 무손상
    assert not p.with_name(p.name + ".tmp").exists()        # 임시파일 정리됨


def test_refresh_creates_tmp_with_0600_mode(tmp_path, monkeypatch):
    import tokenomy.official_fetch as of
    p = _creds(tmp_path)                              # 기존 헬퍼 재사용
    body = json.dumps({"access_token": "a2", "refresh_token": "r2", "expires_in": 28800})
    seen = {}
    real_open = os.open
    def _spy(path_arg, flags, mode=0o777):
        seen["mode"] = mode
        return real_open(path_arg, flags, mode)
    monkeypatch.setattr(of.os, "open", _spy)
    assert of.refresh_claude_token(p, now_ms=1, urlopen=lambda req, timeout: _Resp(body)) == "a2"
    assert seen["mode"] == 0o600


# ---------------------------------------------------------------------------
# ensure_fresh_claude_token + _auto_refresh_allowed (ADR 0021, Task 4)
# ---------------------------------------------------------------------------

_NOW_T4 = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)


def _creds_exp(tmp_path, exp_ms):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "cur-acc", "refreshToken": "cur-ref", "expiresAt": exp_ms}}),
        encoding="utf-8")
    return p


def test_ensure_refreshes_when_expiring(tmp_path):
    near = int(_NOW_T4.timestamp() * 1000) + 60_000          # 1분 뒤 만료(임박)
    p = _creds_exp(tmp_path, near)
    body = json.dumps({"access_token": "fresh", "refresh_token": "r2", "expires_in": 28800})
    cfg = {"official_fetch": {"auto_refresh_token": "always"}}
    tok = ensure_fresh_claude_token(cfg, connect(":memory:"), now=_NOW_T4, path=p,
                                    urlopen=lambda req, timeout: _Resp(body))
    assert tok == "fresh"


def test_ensure_skips_when_not_expiring(tmp_path):
    far = int(_NOW_T4.timestamp() * 1000) + 3_600_000        # 1시간 뒤(여유)
    p = _creds_exp(tmp_path, far)
    cfg = {"official_fetch": {"auto_refresh_token": "always"}}
    def _never(req, timeout): raise AssertionError("refresh 호출되면 안 됨")
    tok = ensure_fresh_claude_token(cfg, connect(":memory:"), now=_NOW_T4, path=p, urlopen=_never)
    assert tok == "cur-acc"


def test_auto_refresh_allowed_old_activity_returns_true():
    """auto 모드 + 마지막 claude 활동이 safety_hours보다 오래됨 → True(refresh 허용, ADR 0021 핵심 유스케이스).

    CLI를 안 쓰는 기기에서 토큰이 만료됐어도 갱신이 허용되어야 한다.
    _NOW_T4 = 2026-06-26T12:00:00Z 기준 ~25h 전 활동 → 24h 임계 초과 → True.
    """
    conn = connect(":memory:")
    conn.execute("INSERT INTO messages (dedup_key, provider, ts) VALUES (?,?,?)",
                 ("k_old", "claude", "2026-06-25T11:00:00+00:00"))   # _NOW_T4(2026-06-26 12:00Z) 기준 25h 전
    conn.commit()
    assert _auto_refresh_allowed({"official_fetch": {"auto_refresh_token": "auto",
                                  "auto_refresh_safety_hours": 24}}, conn, _NOW_T4, "auto") is True


def test_auto_refresh_allowed_modes():
    conn = connect(":memory:")
    assert _auto_refresh_allowed({"official_fetch": {"auto_refresh_token": "off"}}, conn, _NOW_T4, "off") is False
    assert _auto_refresh_allowed({"official_fetch": {"auto_refresh_token": "always"}}, conn, _NOW_T4, "always") is True
    # auto + 최근 활동 없음 → 허용
    assert _auto_refresh_allowed({"official_fetch": {"auto_refresh_token": "auto"}}, conn, _NOW_T4, "auto") is True
    # auto + 최근(1시간 전) claude 활동 → skip(False)
    conn.execute("INSERT INTO messages (dedup_key, provider, ts) VALUES (?,?,?)",
                 ("k", "claude", "2026-06-26T11:00:00+00:00"))   # _NOW_T4(12:00)보다 1h 전
    conn.commit()
    assert _auto_refresh_allowed({"official_fetch": {"auto_refresh_token": "auto",
                                  "auto_refresh_safety_hours": 24}}, conn, _NOW_T4, "auto") is False


# ---------------------------------------------------------------------------
# fetch_provider 통합 — 선제 갱신 + 401 반응형 재시도 (ADR 0021, Task 5)
# ---------------------------------------------------------------------------

_NOW2 = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)


def test_reactive_refresh_on_401_then_retry(tmp_path, monkeypatch):
    import tokenomy.official_fetch as of
    p = tmp_path / ".credentials.json"
    far = int(_NOW2.timestamp() * 1000) + 3_600_000       # 선제는 건너뛰게(여유)
    p.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "a", "refreshToken": "r", "expiresAt": far}}), encoding="utf-8")
    monkeypatch.setattr(of, "CLAUDE_CREDS", p)
    # refresh는 성공으로 스텁(파일 토큰 교체 흉내)
    monkeypatch.setattr(of, "refresh_claude_token", lambda path, *, now_ms, urlopen: "a2")
    calls = {"n": 0}
    def _op(req, timeout):
        # usage GET만 여기 온다(refresh는 위에서 스텁). 첫 호출 401, 둘째 200.
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError("u", 401, "unauth", {}, None)
        return _Resp(json.dumps({}))                       # 빈 버킷이라도 200이면 ok 경로
    cfg = {"tracked_providers": ["claude"],
           "official_fetch": {"auto_refresh_token": "always"}}
    r = fetch_provider("claude", now_kst=_NOW2, config=cfg, conn=connect(":memory:"), urlopen=_op)
    assert calls["n"] == 2                                 # 401 후 정확히 1회 재시도
    assert r.status in ("ok", "http_error")               # 재시도가 일어났음을 호출 횟수로 확인


# ---------------------------------------------------------------------------
# Codex 토큰 능동 갱신 (ADR 0022) — _jwt_exp_ms / refresh_codex_token /
# ensure_fresh_codex_token / fetch_provider codex 401 반응형
# ---------------------------------------------------------------------------

import base64


def _b64url(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).rstrip(b"=").decode("ascii")


def _codex_jwt(exp_sec: int) -> str:
    """exp 클레임만 든 가짜 access_token JWT(서명 검증 안 하므로 sig는 더미)."""
    return f"{_b64url({'alg': 'RS256'})}.{_b64url({'exp': exp_sec})}.sig"


def test_jwt_exp_ms_decodes_exp_to_ms():
    from tokenomy.official_fetch import _jwt_exp_ms
    assert _jwt_exp_ms(_codex_jwt(1782920685)) == 1782920685 * 1000


def test_jwt_exp_ms_bad_token_returns_none():
    from tokenomy.official_fetch import _jwt_exp_ms
    assert _jwt_exp_ms("not-a-jwt") is None
    assert _jwt_exp_ms("") is None
    assert _jwt_exp_ms("a.b.c") is None                     # payload가 base64 JSON 아님


def _codex_auth(tmp_path, *, refresh="old-ref", auth_mode="chatgpt", access="old-acc"):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({
        "auth_mode": auth_mode, "OPENAI_API_KEY": None,
        "tokens": {"id_token": "old-id", "access_token": access,
                   "refresh_token": refresh, "account_id": "acc-keep"},
        "last_refresh": "2026-06-21T00:00:00.000000000Z"}), encoding="utf-8")
    return p


def test_refresh_codex_success_writes_back_rotation_preserves_account(tmp_path):
    from tokenomy.official_fetch import refresh_codex_token
    p = _codex_auth(tmp_path)
    body = json.dumps({"access_token": "new-acc", "refresh_token": "new-ref",
                       "id_token": "new-id", "expires_in": 864000})
    new = refresh_codex_token(p, now_ms=1_000_000_000, urlopen=lambda req, timeout: _Resp(body))
    assert new == "new-acc"
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["tokens"]["access_token"] == "new-acc"
    assert d["tokens"]["refresh_token"] == "new-ref"        # rotation 기록(필수)
    assert d["tokens"]["id_token"] == "new-id"
    assert d["tokens"]["account_id"] == "acc-keep"          # 보존(재유도 안 함)
    assert d["last_refresh"] != "2026-06-21T00:00:00.000000000Z"   # 갱신됨
    assert d["auth_mode"] == "chatgpt"                      # 기존 키 보존


def test_refresh_codex_http_error_leaves_file_untouched(tmp_path):
    from tokenomy.official_fetch import refresh_codex_token
    p = _codex_auth(tmp_path)
    before = p.read_text(encoding="utf-8")
    def _boom(req, timeout):
        raise urllib.error.HTTPError("u", 400, "bad", {}, None)
    assert refresh_codex_token(p, now_ms=1, urlopen=_boom) is None
    assert p.read_text(encoding="utf-8") == before          # 무손상


def test_refresh_codex_bad_schema_leaves_file_untouched(tmp_path):
    from tokenomy.official_fetch import refresh_codex_token
    p = _codex_auth(tmp_path)
    before = p.read_text(encoding="utf-8")
    body = json.dumps({"no": "access_token"})               # access_token 없음
    assert refresh_codex_token(p, now_ms=1, urlopen=lambda req, timeout: _Resp(body)) is None
    assert p.read_text(encoding="utf-8") == before


def test_refresh_codex_apikey_mode_is_noop(tmp_path):
    """auth_mode가 chatgpt가 아니면(API-key 로그인) 갱신할 OAuth 토큰이 없어 네트워크도 안 친다."""
    from tokenomy.official_fetch import refresh_codex_token
    p = _codex_auth(tmp_path, auth_mode="apikey")
    before = p.read_text(encoding="utf-8")
    assert refresh_codex_token(p, now_ms=1, urlopen=_never) is None
    assert p.read_text(encoding="utf-8") == before


def test_refresh_codex_missing_refresh_token_is_noop(tmp_path):
    from tokenomy.official_fetch import refresh_codex_token
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"auth_mode": "chatgpt",
                             "tokens": {"access_token": "a", "account_id": "x"}}),
                 encoding="utf-8")
    assert refresh_codex_token(p, now_ms=1, urlopen=_never) is None


def test_auto_refresh_allowed_codex_gates_on_codex_activity():
    """안전망은 provider별 활동으로 판정 — codex 갱신은 *codex* 활동에만 묶인다(ADR 0022 공유설정).

    최근 claude 활동이 있어도 codex 활동이 없으면 codex 갱신은 허용되어야 한다(혼합 기기).
    """
    cfg = {"official_fetch": {"auto_refresh_token": "auto", "auto_refresh_safety_hours": 24}}
    conn = connect(":memory:")
    conn.execute("INSERT INTO messages (dedup_key, provider, ts) VALUES (?,?,?)",
                 ("kc", "claude", "2026-06-26T11:00:00+00:00"))   # _NOW_T4 1h 전(최근)
    conn.commit()
    assert _auto_refresh_allowed(cfg, conn, _NOW_T4, "auto", "codex") is True   # codex 활동 없음 → 허용
    conn.execute("INSERT INTO messages (dedup_key, provider, ts) VALUES (?,?,?)",
                 ("kx", "codex", "2026-06-26T11:00:00+00:00"))
    conn.commit()
    assert _auto_refresh_allowed(cfg, conn, _NOW_T4, "auto", "codex") is False  # codex 최근 활동 → skip


def test_ensure_codex_refreshes_when_jwt_expiring(tmp_path):
    from tokenomy.official_fetch import ensure_fresh_codex_token
    near = int(_NOW_T4.timestamp()) + 60                    # 1분 뒤 만료(초) → JWT exp
    p = _codex_auth(tmp_path, access=_codex_jwt(near))
    fresh = _codex_jwt(near + 864000)
    body = json.dumps({"access_token": fresh, "refresh_token": "r2",
                       "id_token": "id2", "expires_in": 864000})
    cfg = {"official_fetch": {"auto_refresh_token": "always"}}
    tok, acct = ensure_fresh_codex_token(cfg, connect(":memory:"), now=_NOW_T4, path=p,
                                         urlopen=lambda req, timeout: _Resp(body))
    assert tok == fresh                                     # 갱신된 access_token 반환
    assert acct == "acc-keep"                              # account_id 동반 반환·보존


def test_ensure_codex_skips_when_not_expiring(tmp_path):
    from tokenomy.official_fetch import ensure_fresh_codex_token
    far = int(_NOW_T4.timestamp()) + 864000                 # 10일 뒤(여유)
    p = _codex_auth(tmp_path, access=_codex_jwt(far))
    cfg = {"official_fetch": {"auto_refresh_token": "always"}}
    tok, acct = ensure_fresh_codex_token(cfg, connect(":memory:"), now=_NOW_T4, path=p, urlopen=_never)
    assert tok == _codex_jwt(far)                           # 기존 토큰 그대로(네트워크 미호출)
    assert acct == "acc-keep"


def test_reactive_refresh_codex_on_401_then_retry(tmp_path, monkeypatch):
    """fetch_provider("codex")가 401을 만나면 refresh_codex_token으로 갱신 후 1회 재시도(ADR 0022)."""
    import tokenomy.official_fetch as of
    from tokenomy import paths
    far = int(_NOW2.timestamp()) + 864000                   # 선제는 건너뛰게(JWT exp 여유)
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
        "access_token": _codex_jwt(far), "refresh_token": "r",
        "id_token": "i", "account_id": "acc"}}), encoding="utf-8")
    monkeypatch.setattr(of, "CODEX_AUTH", p)
    monkeypatch.setattr(paths, "CODEX_AUTH", p)             # creds_present 게이트용
    monkeypatch.setattr(of, "refresh_codex_token", lambda path, *, now_ms, urlopen: "a2")
    calls = {"n": 0}
    def _op(req, timeout):
        calls["n"] += 1                                     # usage GET만 여기 온다(refresh는 스텁)
        if calls["n"] == 1:
            raise urllib.error.HTTPError("u", 401, "unauth", {}, None)
        return _Resp(json.dumps({}))                        # 200(빈 버킷이라도 ok 경로)
    cfg = {"tracked_providers": ["codex"], "credit_to_usd": 0.04,
           "official_fetch": {"auto_refresh_token": "always"}}
    r = fetch_provider("codex", now_kst=_NOW2, config=cfg, conn=connect(":memory:"), urlopen=_op)
    assert calls["n"] == 2                                  # 401 후 정확히 1회 재시도
    assert r.status in ("ok", "http_error")
