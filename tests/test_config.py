import json

from tokenomy.config import (
    debug_mode,
    load_config,
    save_config,
    user_label,
)


def test_debug_mode_default_false():
    assert debug_mode({}) is False


def test_debug_mode_default_in_loaded_config(tmp_path):
    cfg = load_config(tmp_path / "nope.json")
    assert cfg["debug_mode"] is False


def test_debug_mode_reads_true():
    assert debug_mode({"debug_mode": True}) is True


def test_debug_mode_coerces_truthy():
    assert debug_mode({"debug_mode": 1}) is True
    assert debug_mode({"debug_mode": 0}) is False
    assert debug_mode({"debug_mode": None}) is False


def test_debug_mode_roundtrips(tmp_path):
    p = tmp_path / "c.json"
    cfg = load_config(p)
    cfg["debug_mode"] = True
    save_config(cfg, p)
    assert debug_mode(load_config(p)) is True


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


from tokenomy.config import onboarding_pending


def test_onboarding_pending_when_unconfigured_and_no_creds(monkeypatch):
    # 미설정(None) + 크레덴셜 없음 = 완전 신규 진입자 → 온보딩.
    import tokenomy.config as b
    monkeypatch.setattr(b, "creds_present", lambda p: False)
    assert onboarding_pending({}) is True
    assert onboarding_pending({"tracked_providers": None}) is True


def test_onboarding_pending_false_when_creds_present(monkeypatch):
    # 미설정이라도 크레덴셜이 있으면 시드가 비지 않음 → 온보딩 아님(정상 화면).
    import tokenomy.config as b
    monkeypatch.setattr(b, "creds_present", lambda p: p == "claude")
    assert onboarding_pending({}) is False


def test_onboarding_pending_false_when_explicitly_empty(monkeypatch):
    # 명시적 빈 리스트는 '사용자가 전부 끔' — 온보딩이 아니다(기존 '설정에서 켜세요' 유지).
    import tokenomy.config as b
    monkeypatch.setattr(b, "creds_present", lambda p: False)
    assert onboarding_pending({"tracked_providers": []}) is False


def test_onboarding_pending_false_when_explicit_list(monkeypatch):
    # 명시적으로 provider를 고른 사용자는 온보딩 대상이 아니다.
    import tokenomy.config as b
    monkeypatch.setattr(b, "creds_present", lambda p: False)
    assert onboarding_pending({"tracked_providers": ["claude"]}) is False


from tokenomy.config import mini_view_settings


def test_mini_view_settings_default_main_no_position():
    # 미설정 → 배타 전환 기본은 일반뷰("main"), 위치는 미정(None) → 런처가 기본 코너에 둔다.
    assert mini_view_settings({}) == {"last_view": "main", "x": None, "y": None}


def test_mini_view_settings_reads_last_view_and_position():
    s = mini_view_settings({"mini_view": {"last_view": "mini", "x": 1500, "y": 880}})
    assert s == {"last_view": "mini", "x": 1500, "y": 880}


def test_mini_view_settings_invalid_last_view_falls_back_to_main():
    # 알 수 없는 값/누락은 "main"으로 — 재시작 시 일반뷰로 안전 복원.
    assert mini_view_settings({"mini_view": {"last_view": "widget"}})["last_view"] == "main"
    assert mini_view_settings({"mini_view": {}})["last_view"] == "main"


def test_mini_view_settings_bad_coords_fall_back_to_none():
    # 비숫자·누락 좌표는 None으로 — 오설정으로 창 배치가 깨지지 않게(런처가 기본 코너).
    assert mini_view_settings({"mini_view": {"last_view": "mini", "x": "nope"}})["x"] is None
    assert mini_view_settings({"mini_view": {"last_view": "mini", "y": None}})["y"] is None
    assert mini_view_settings({"mini_view": {"last_view": "mini"}}) == {"last_view": "mini", "x": None, "y": None}


from tokenomy.config import account_mode


def test_account_mode_unset_is_none():
    # 미설정(키 없음/None)은 None — 첫 공식 취득 때 데이터로 자동 시드될 상태(ADR 0015).
    assert account_mode({}) is None
    assert account_mode({"account_mode": None}) is None


def test_account_mode_reads_explicit_values():
    assert account_mode({"account_mode": "enterprise"}) == "enterprise"
    assert account_mode({"account_mode": "subscription"}) == "subscription"


def test_account_mode_unknown_value_is_none():
    # 오타·알 수 없는 값은 미설정 취급(None) — 오설정으로 모드 게이트가 깨지지 않게.
    assert account_mode({"account_mode": "enterpryse"}) is None
    assert account_mode({"account_mode": ""}) is None
    assert account_mode({"account_mode": 1}) is None


def test_load_config_default_account_mode_is_none(tmp_path):
    # 파일 없을 때 기본 account_mode 키는 존재하되 미설정(None) — tracked_providers None 동형.
    cfg = load_config(tmp_path / "nope.json")
    assert "account_mode" in cfg
    assert cfg["account_mode"] is None


from tokenomy.config import seed_account_mode


def test_seed_account_mode_seeds_enterprise_when_usd_budget(tmp_path):
    # 미설정 + USD 예산 버킷 존재 → enterprise로 시드·영속(save_config), load로 왕복 확인.
    p = tmp_path / "c.json"
    cfg = load_config(p)
    assert seed_account_mode(cfg, has_usd_budget=True, path=p) == "enterprise"
    assert cfg["account_mode"] == "enterprise"          # in-memory dict도 갱신
    assert load_config(p)["account_mode"] == "enterprise"  # 파일에 영속(sticky)


def test_seed_account_mode_seeds_subscription_when_no_usd_budget(tmp_path):
    # 미설정 + USD 예산 없음(rate-window만) → subscription으로 시드·영속.
    p = tmp_path / "c.json"
    cfg = load_config(p)
    assert seed_account_mode(cfg, has_usd_budget=False, path=p) == "subscription"
    assert load_config(p)["account_mode"] == "subscription"


def test_seed_account_mode_respects_explicit_value(tmp_path):
    # 이미 명시 설정이면 데이터와 무관하게 존중·반환하고 덮어쓰지 않는다(sticky·사용자 토글 우선).
    p = tmp_path / "c.json"
    cfg = {"account_mode": "subscription"}
    assert seed_account_mode(cfg, has_usd_budget=True, path=p) == "subscription"
    assert cfg["account_mode"] == "subscription"
    assert not p.exists()                               # 존중 경로는 영속하지 않는다(쓰기 없음)
