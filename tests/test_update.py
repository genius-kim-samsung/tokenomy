from datetime import date

from tokenomy import update
from tokenomy.db import connect
from tokenomy.update import _parse_version, is_newer


def test_parse_version():
    assert _parse_version("v0.2.0") == (0, 2, 0)
    assert _parse_version("0.1.0") == (0, 1, 0)
    assert _parse_version("v1.2") == (1, 2)


def test_is_newer():
    assert is_newer("v0.2.0", "0.1.0") is True
    assert is_newer("v0.1.0", "0.1.0") is False
    assert is_newer("v0.0.9", "0.1.0") is False


def test_is_newer_bad_tag_is_false():
    assert is_newer("garbage", "0.1.0") is False


def test_check_update_skip_env(monkeypatch):
    monkeypatch.setenv("TOKENOMY_SKIP_UPDATE_CHECK", "1")
    assert update.check_update() is None


def test_check_update_returns_newer(monkeypatch):
    monkeypatch.delenv("TOKENOMY_SKIP_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(update, "_fetch_latest_tag", lambda timeout=3.0: "v99.0.0")
    assert update.check_update() == "v99.0.0"


def test_check_update_none_when_current(monkeypatch):
    monkeypatch.delenv("TOKENOMY_SKIP_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(update, "_fetch_latest_tag", lambda timeout=3.0: "v0.0.1")
    assert update.check_update() is None


def test_check_update_network_fail_is_none(monkeypatch):
    monkeypatch.delenv("TOKENOMY_SKIP_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(update, "_fetch_latest_tag", lambda timeout=3.0: None)
    assert update.check_update() is None


def test_check_update_daily_cache(monkeypatch):
    monkeypatch.delenv("TOKENOMY_SKIP_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(update, "_fetch_latest_tag", lambda timeout=3.0: "v99.0.0")
    conn = connect(":memory:")
    d = date(2026, 6, 12)
    assert update.check_update(conn, today=d) == "v99.0.0"  # 첫 호출
    assert update.check_update(conn, today=d) is None        # 같은 날 → 캐시
