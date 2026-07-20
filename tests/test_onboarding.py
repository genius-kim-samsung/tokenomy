"""완전 신규(크레덴셜·config 없음) 진입자 온보딩 — views/라우트 (A 모집단).

대시보드는 빈 껍데기 + 틀린 '설정에서 켜세요' 대신 '시작하기' 안내 카드를 띄운다.
판정은 config.onboarding_pending(미설정 None + 빈 시드)이며, 명시적 끔([])과 구분한다.
온보딩 상태는 모집단 둘이 겹치므로(진짜 신규 / CLI를 다른 기기에서만 쓰는 관측 전용 기기)
안내를 갈래 둘로 나눠 담는다 — CONTEXT '기기 로그인'.
"""
from fastapi.testclient import TestClient

import tokenomy.config as cfgmod
from tokenomy import paths
from tokenomy.db import connect
from tokenomy.web import app as app_module
from tokenomy.web.views import overview_context


def _setup(tmp_path, monkeypatch, *, config_json=None, creds=lambda p: False):
    """온보딩 시나리오 env/patch — config_json 없으면 config 파일 부재(미설정).

    creds로 creds_present를 주입(기본: 셋 다 없음 = 빈 시드 = 기기 로그인 없음). config는
    import 시점 바인딩, views는 paths 경유라 둘 다 갈아끼워야 실제 홈에 좌우되지 않는다.
    """
    cfg = tmp_path / "cfg.json"
    if config_json is not None:
        cfg.write_text(config_json, encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    monkeypatch.setenv("TOKENOMY_SKIP_UPDATE_CHECK", "1")
    monkeypatch.setattr(cfgmod, "creds_present", creds)
    monkeypatch.setattr(paths, "creds_present", creds)


def _client(tmp_path, monkeypatch, **kw):
    _setup(tmp_path, monkeypatch, **kw)
    db = tmp_path / "t.db"
    monkeypatch.setattr(app_module, "connect", lambda *a, **k: connect(str(db)))
    return TestClient(app_module.app)


def test_overview_context_onboarding_true_when_unconfigured_no_creds(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    ctx = overview_context(connect(":memory:"), "cost")
    assert ctx["onboarding"] is True


def test_overview_context_onboarding_false_when_creds_present(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, creds=lambda p: p == "claude")
    ctx = overview_context(connect(":memory:"), "cost")
    assert ctx["onboarding"] is False


def test_dashboard_renders_start_card_for_new_user(tmp_path, monkeypatch):
    # 완전 신규 → '시작하기' 안내 카드(평소처럼 작업 + 지금 수집). 틀린 '설정에서 켜세요' 없음.
    client = _client(tmp_path, monkeypatch)
    r = client.get("/")
    assert r.status_code == 200
    assert "Tokenomy 시작하기" in r.text
    assert "평소처럼 작업" in r.text
    assert "지금 수집" in r.text
    assert "표시할 AI가 없습니다" not in r.text


def test_start_card_guides_device_login_for_remote_user(tmp_path, monkeypatch):
    # 같은 화면의 둘째 갈래 — CLI를 다른 기기에서만 쓰는 사람에게 기기 로그인을 지시한다.
    # 실행할 명령과 '계정 전체라 바로 보인다'는 보상, 재렌더 진입점까지 한 화면에 있어야 한다.
    client = _client(tmp_path, monkeypatch)
    r = client.get("/")
    assert "다른 PC·서버에서만" in r.text
    for cmd in ("claude", "codex", "gemini"):
        assert f"<code>{cmd}</code>" in r.text
    assert "계정 전체" in r.text
    assert "로그인했어요" in r.text


def test_dashboard_explicit_empty_keeps_settings_hint(tmp_path, monkeypatch):
    # 사용자가 설정에서 전부 끔(명시적 []) + 기기 로그인 있음 → 기존 '설정에서 켜세요' 유지.
    client = _client(tmp_path, monkeypatch, config_json='{"tracked_providers": []}',
                     creds=lambda p: p == "claude")
    r = client.get("/")
    assert r.status_code == 200
    assert "Tokenomy 시작하기" not in r.text
    assert "표시할 AI가 없습니다" in r.text
    assert "자동으로 가져옵니다" in r.text


def test_login_needed_card_hides_refresh_affordances(tmp_path, monkeypatch):
    # 미로그인 provider는 ↻를 눌러도 취득 시도조차 없다 — 갱신 유도 문구와 버튼을 함께 접는다.
    client = _client(tmp_path, monkeypatch, config_json='{"tracked_providers": ["codex"]}')
    r = client.get("/")
    assert "1회 실행·로그인하면" in r.text
    assert "위 ↻로 갱신하세요" not in r.text
    assert 'name="provider" value="codex"' not in r.text     # provider별 ↻ 폼도 없음


def test_explicit_empty_without_creds_guides_device_login(tmp_path, monkeypatch):
    # 온보딩 카드를 잃은 관측 전용 기기(설정을 한 번 저장하면 명시적 [])
    # — '켜면 가져옵니다'는 거짓 약속이라 로그인 축으로 갈아끼운다.
    client = _client(tmp_path, monkeypatch, config_json='{"tracked_providers": []}')
    r = client.get("/")
    assert "표시할 AI가 없습니다" in r.text
    assert "자동으로 가져옵니다" not in r.text
    assert "로그인된" in r.text
    assert "1회 실행" in r.text
