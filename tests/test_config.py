import json

from tokenomy.config import (
    load_config,
    save_config,
    user_label,
)


def test_load_config_missing_file_returns_zero_tracking(tmp_path):
    """파일 없을 때 기본 설정 형태 확인 — budget 키 없음, tracked_providers/official_fetch 있음."""
    cfg = load_config(tmp_path / "nope.json")
    assert "tracked_providers" in cfg
    assert "official_fetch" in cfg
    assert "budget" not in cfg


def test_save_then_load_roundtrip(tmp_path):
    p = tmp_path / "c.json"
    save_config({"user_label": "alice"}, p)
    cfg = load_config(p)
    assert cfg["user_label"] == "alice"


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
    assert "budget" not in cfg          # 새 기본 형태 — budget 키 없음


def test_config_path_default_uses_paths(tmp_path, monkeypatch):
    from tokenomy.config import _config_path
    monkeypatch.delenv("TOKENOMY_CONFIG", raising=False)
    monkeypatch.setenv("TOKENOMY_DATA", str(tmp_path))
    assert _config_path() == tmp_path / "config" / "tokenomy.config.json"


from tokenomy.config import credit_to_usd


def test_credit_to_usd_default_when_missing():
    assert credit_to_usd({}) == 0.04


def test_credit_to_usd_reads_config():
    assert credit_to_usd({"credit_to_usd": 0.05}) == 0.05


def test_credit_to_usd_rejects_bad_values():
    assert credit_to_usd({"credit_to_usd": -1}) == 0.04
    assert credit_to_usd({"credit_to_usd": "x"}) == 0.04
    assert credit_to_usd({"credit_to_usd": None}) == 0.04


from tokenomy.config import official_fetch_settings


def test_official_fetch_settings_defaults():
    # 자동 갱신 간격 기본 10분(quota를 CLI와 공유 → 보수적)
    s = official_fetch_settings({})
    assert s == {"min_interval_minutes": 10, "background_poll": True}


def test_official_fetch_settings_bad_interval_falls_back():
    assert official_fetch_settings({"official_fetch": {"min_interval_minutes": "x"}})["min_interval_minutes"] == 10
    assert official_fetch_settings({"official_fetch": {"min_interval_minutes": -3}})["min_interval_minutes"] == 10
    assert official_fetch_settings({"official_fetch": {"min_interval_minutes": 9}})["min_interval_minutes"] == 9


def test_official_fetch_settings_background_poll_default_on():
    # 상주 중 백그라운드 공식 갱신 폴은 기본 ON(콜드스타트 방지, ADR 0007)
    assert official_fetch_settings({})["background_poll"] is True


def test_official_fetch_settings_background_poll_explicit_off():
    assert official_fetch_settings(
        {"official_fetch": {"background_poll": False}})["background_poll"] is False


def test_load_config_default_auto_interval_is_10(tmp_path):
    # 파일 없을 때 기본 자동 갱신 간격 10분
    cfg = load_config(tmp_path / "nope.json")
    assert cfg["official_fetch"]["min_interval_minutes"] == 10


from tokenomy.config import forecast_settings


def test_forecast_settings_default():
    # 소비속도 트레일링 창 기본 2주
    assert forecast_settings({}) == {"rate_window_weeks": 2}


def test_forecast_settings_reads_config():
    assert forecast_settings({"forecast_settings": {"rate_window_weeks": 4}})["rate_window_weeks"] == 4


def test_forecast_settings_clamps_and_falls_back():
    assert forecast_settings({"forecast_settings": {"rate_window_weeks": 0}})["rate_window_weeks"] == 1    # 하한
    assert forecast_settings({"forecast_settings": {"rate_window_weeks": 99}})["rate_window_weeks"] == 8   # 상한
    assert forecast_settings({"forecast_settings": {"rate_window_weeks": "x"}})["rate_window_weeks"] == 2  # 이상치→기본
    assert forecast_settings({"forecast_settings": {"rate_window_weeks": None}})["rate_window_weeks"] == 2


def test_load_config_default_forecast_window_is_2(tmp_path):
    cfg = load_config(tmp_path / "nope.json")
    assert cfg["forecast_settings"]["rate_window_weeks"] == 2


from tokenomy.config import tracked_providers
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
    # 키 자체가 없거나(None) 미설정이면 크레덴셜로 시드(무설정 첫 실행이 대개 정답).
    import tokenomy.config as b
    monkeypatch.setattr(b, "creds_present", lambda p: p == "claude")
    assert tracked_providers({}) == ["claude"]
    assert tracked_providers({"tracked_providers": None}) == ["claude"]


def test_tracked_providers_empty_list_persists(monkeypatch):
    # 명시적 빈 리스트는 "전부 끄기"의 영속 상태 — 크레덴셜이 있어도 재시드하지 않는다.
    import tokenomy.config as b
    monkeypatch.setattr(b, "creds_present", lambda p: True)
    assert tracked_providers({"tracked_providers": []}) == []
