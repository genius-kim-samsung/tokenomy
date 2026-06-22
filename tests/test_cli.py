"""CLI official import-fixture 명령 테스트."""
from __future__ import annotations

import json
from datetime import datetime

from tokenomy.aggregate import KST
from tokenomy.cli import cmd_official_import, cmd_report
from tokenomy.db import connect, latest_official_snapshot


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
    import tokenomy.config as b
    # tracked_providers가 ["claude"]만 반환하도록 패치(크레덴셜 파일 불필요)
    monkeypatch.setattr(b, "creds_present", lambda p: p == "claude")

    conn = connect(":memory:")
    # freshness가 db를 조회하므로 ingest 기록 없이도 동작해야 함
    cmd_report(conn)
    out = capsys.readouterr().out
    assert "claude" in out.lower()
    assert "이번 달" in out or "총지출" in out


def test_cmd_ingest_returns_total_visible_changes(monkeypatch):
    """cmd_ingest는 화면에 영향을 주는 변경 합계를 반환한다(archive 제외)."""
    import tokenomy.cli as cli
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "apply_pricing_overrides", lambda p, o: p)
    monkeypatch.setattr(cli, "load_pricing", lambda: {})
    monkeypatch.setattr(cli, "ingest_root", lambda *a, **k: 2)      # n_claude
    monkeypatch.setattr(cli, "ingest_codex", lambda *a, **k: 3)     # n_codex
    monkeypatch.setattr(cli, "archive_tree", lambda *a, **k: 9)     # 합계 제외
    monkeypatch.setattr(cli, "ingest_titles", lambda *a, **k: 1)    # n_titles
    monkeypatch.setattr(cli, "ingest_user_turns", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "maybe_reprice", lambda *a, **k: 4)    # repriced
    monkeypatch.setattr(cli, "record_ingest", lambda *a, **k: None)
    assert cli.cmd_ingest(conn=None) == 2 + 3 + 1 + 0 + 4
