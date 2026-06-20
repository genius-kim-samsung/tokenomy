"""CLI official import-fixture 명령 테스트."""
from __future__ import annotations

import json
import tokenomy.cli as cli_module
from datetime import datetime

from tokenomy.aggregate import KST
from tokenomy.cli import cmd_official_import, cmd_report
from tokenomy.db import connect, latest_official_snapshot


# ── _official_fetch_worker 테스트 ──────────────────────────────────────────


def test_official_worker_skips_when_no_tracked_providers(monkeypatch):
    """tracked_providers 없음 → fetch_provider 미호출(네트워크 0)."""
    called = []
    monkeypatch.setattr(cli_module, "fetch_provider",
                        lambda p, **k: called.append(p))
    # creds_present가 False를 반환하도록 패치 → tracked_providers가 []를 반환
    import tokenomy.budget as b
    monkeypatch.setattr(b, "creds_present", lambda p: False)
    cli_module._official_fetch_worker({}, datetime(2026, 6, 10, 9, tzinfo=KST))
    assert called == []


def test_official_worker_fetches_tracked_providers(monkeypatch):
    """tracked_providers = ["claude"] → claude만 fetch."""
    called = []
    monkeypatch.setattr(cli_module, "fetch_provider",
                        lambda p, **k: called.append(p))
    cfg = {"tracked_providers": ["claude"]}
    cli_module._official_fetch_worker(
        cfg, datetime(2026, 6, 10, 9, tzinfo=KST),
        connect_fn=lambda: connect(":memory:"))
    assert called == ["claude"]


def test_official_worker_swallows_exceptions(monkeypatch):
    """fetch_provider 예외 발생 시 worker가 삼켜 종료되지 않는다."""
    def boom(p, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(cli_module, "fetch_provider", boom)
    cfg = {"tracked_providers": ["claude"]}
    # 예외를 삼켜 worker가 깨지지 않는다
    cli_module._official_fetch_worker(
        cfg, datetime(2026, 6, 10, 9, tzinfo=KST),
        connect_fn=lambda: connect(":memory:"))


def test_official_import_claude(tmp_path):
    raw = {
        "spend": {"used": {"amount_minor": 3000, "exponent": 2},
                  "limit": {"amount_minor": 10000, "exponent": 2}},
        "extra_usage": {"monthly_limit": 10000},
    }
    p = tmp_path / "c.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    conn = connect(":memory:")
    n = cmd_official_import(conn, "claude", str(p), now_kst=datetime(2026, 6, 10, 9, tzinfo=KST),
                           credit_to_usd_value=0.04)
    assert n == 1
    rows = latest_official_snapshot(conn, "claude")
    assert rows[0]["used_usd"] == 30.0 and rows[0]["limit_usd"] == 100.0


def test_official_import_codex(tmp_path):
    raw = {"spend_control": {"individual_limit": {
        "limit": "2000", "used": "500.0", "remaining": "1500.0",
        "used_percent": 25, "reset_at": 1782864001}}}
    p = tmp_path / "x.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    conn = connect(":memory:")
    n = cmd_official_import(conn, "codex", str(p), now_kst=datetime(2026, 6, 10, 9, tzinfo=KST),
                           credit_to_usd_value=0.04)
    assert n == 1
    rows = latest_official_snapshot(conn, "codex")
    assert rows[0]["used_usd"] == 20.0   # 500 * 0.04


# ── cmd_report 번다운 제거 테스트 ─────────────────────────────────────────────


def test_report_runs_without_budget(capsys, monkeypatch):
    """cmd_report가 budget/burndown 없이 동작하고 provider별 총지출을 출력한다."""
    import tokenomy.budget as b
    # tracked_providers가 ["claude"]만 반환하도록 패치(크레덴셜 파일 불필요)
    monkeypatch.setattr(b, "creds_present", lambda p: p == "claude")

    conn = connect(":memory:")
    # freshness가 db를 조회하므로 ingest 기록 없이도 동작해야 함
    cmd_report(conn)
    out = capsys.readouterr().out
    assert "claude" in out.lower()
    assert "이번 달" in out or "총지출" in out
