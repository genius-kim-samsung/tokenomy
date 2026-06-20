import json

from tokenomy.budget import (
    Budget,
    budget_from_config,
    load_config,
    save_config,
    user_label,
)


def test_budget_splits_providers():
    b = Budget(claude=223, codex=50)
    assert b.claude == 223
    assert b.codex == 50
    assert b.total == 273


def test_limit_for():
    b = Budget(claude=223, codex=50)
    assert b.limit_for("claude") == 223
    assert b.limit_for("codex") == 50


def test_budget_from_config_reads_budget_block():
    cfg = {"budget": {"claude": 100, "codex": 30}}
    b = budget_from_config(cfg)
    assert b.claude == 100 and b.codex == 30


def test_budget_from_config_missing_block_is_zero():
    b = budget_from_config({})
    assert b.total == 0


def test_load_config_missing_file_returns_zero_tracking(tmp_path):
    cfg = load_config(tmp_path / "nope.json")
    assert cfg["budget"]["claude"] == 0
    assert cfg["budget"]["codex"] == 0


def test_save_then_load_roundtrip(tmp_path):
    p = tmp_path / "c.json"
    save_config({"user_label": "alice", "budget": {"claude": 7, "codex": 8}}, p)
    cfg = load_config(p)
    assert cfg["user_label"] == "alice"
    assert budget_from_config(cfg).codex == 8


def test_user_label_falls_back(monkeypatch):
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    assert user_label({}) == "me"


def test_user_label_uses_config_value():
    assert user_label({"user_label": "alice"}) == "alice"


def test_example_config_is_valid():
    cfg = json.loads(open("config/tokenomy.config.example.json", encoding="utf-8").read())
    assert "tracked_providers" in cfg
    assert "official_fetch" in cfg


def test_config_path_default_uses_paths(tmp_path, monkeypatch):
    from tokenomy.budget import _config_path
    monkeypatch.delenv("TOKENOMY_CONFIG", raising=False)
    monkeypatch.setenv("TOKENOMY_DATA", str(tmp_path))
    assert _config_path() == tmp_path / "config" / "tokenomy.config.json"


from datetime import datetime, timezone, timedelta

from tokenomy.budget import budget_start_kst

_KST = timezone(timedelta(hours=9))


def test_weekly_codex_limit_is_quarter():
    b = Budget(claude=200, codex=40)
    assert b.weekly_codex_limit() == 10.0   # 40 / 4


def test_budget_start_kst_parses_iso_date():
    dt = budget_start_kst({"budget_start": "2026-06-12"})
    assert dt == datetime(2026, 6, 12, 0, 0, tzinfo=_KST)


def test_budget_start_kst_none_when_absent_or_blank():
    assert budget_start_kst({}) is None
    assert budget_start_kst({"budget_start": ""}) is None
    assert budget_start_kst({"budget_start": "garbage"}) is None


def test_load_config_keeps_budget_start(tmp_path):
    p = tmp_path / "c.json"
    save_config({"budget": {"claude": 1, "codex": 2}, "budget_start": "2026-06-12"}, p)
    cfg = load_config(p)
    assert cfg["budget_start"] == "2026-06-12"


def test_load_config_missing_budget_start_is_none(tmp_path):
    cfg = load_config(tmp_path / "nope.json")
    assert cfg.get("budget_start") is None


from tokenomy.budget import credit_to_usd


def test_credit_to_usd_default_when_missing():
    assert credit_to_usd({}) == 0.04


def test_credit_to_usd_reads_config():
    assert credit_to_usd({"credit_to_usd": 0.05}) == 0.05


def test_credit_to_usd_rejects_bad_values():
    assert credit_to_usd({"credit_to_usd": -1}) == 0.04
    assert credit_to_usd({"credit_to_usd": "x"}) == 0.04
    assert credit_to_usd({"credit_to_usd": None}) == 0.04


from tokenomy.budget import official_fetch_settings


def test_official_fetch_settings_defaults():
    s = official_fetch_settings({})
    assert s == {"min_interval_minutes": 5}


def test_official_fetch_settings_bad_interval_falls_back():
    assert official_fetch_settings({"official_fetch": {"min_interval_minutes": "x"}})["min_interval_minutes"] == 5
    assert official_fetch_settings({"official_fetch": {"min_interval_minutes": -3}})["min_interval_minutes"] == 5
    assert official_fetch_settings({"official_fetch": {"min_interval_minutes": 9}})["min_interval_minutes"] == 9


from tokenomy.budget import tracked_providers
from tokenomy import paths


def test_creds_present_detects_files(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CLAUDE_CREDS", tmp_path / ".claude" / ".credentials.json")
    monkeypatch.setattr(paths, "CODEX_AUTH", tmp_path / ".codex" / "auth.json")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    assert paths.creds_present("claude") is True
    assert paths.creds_present("codex") is False


def test_tracked_providers_explicit_list_wins():
    assert tracked_providers({"tracked_providers": ["codex"]}) == ["codex"]
    # 알 수 없는 값 제거 + PROVIDERS 순서 정규화
    assert tracked_providers({"tracked_providers": ["codex", "x", "claude"]}) == ["claude", "codex"]


def test_tracked_providers_seeds_from_creds_when_absent(monkeypatch):
    import tokenomy.budget as b
    monkeypatch.setattr(b, "creds_present", lambda p: p == "claude")
    assert tracked_providers({}) == ["claude"]
    assert tracked_providers({"tracked_providers": []}) == ["claude"]   # 빈 리스트도 시드
