"""CLI official import-fixture 명령 테스트."""
from __future__ import annotations

import json
from datetime import datetime

from tokenomy.aggregate import KST
from tokenomy.cli import cmd_official_import
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
