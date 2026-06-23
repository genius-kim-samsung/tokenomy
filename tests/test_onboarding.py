"""완전 신규(크레덴셜·config 없음) 진입자 온보딩 — views/라우트 (A 모집단).

대시보드는 빈 껍데기 + 틀린 '설정에서 켜세요' 대신 '시작하기' 안내 카드를 띄운다.
판정은 config.onboarding_pending(미설정 None + 빈 시드)이며, 명시적 끔([])과 구분한다.
"""
from fastapi.testclient import TestClient

import tokenomy.config as cfgmod
from tokenomy.db import connect
from tokenomy.web import app as app_module
from tokenomy.web.views import overview_context


def _setup(tmp_path, monkeypatch, *, config_json=None, creds=lambda p: False):
    """온보딩 시나리오 env/patch — config_json 없으면 config 파일 부재(미설정).

    creds로 creds_present를 주입(기본: 둘 다 없음 = 빈 시드 = 크레덴셜 없음).
    """
    cfg = tmp_path / "cfg.json"
    if config_json is not None:
        cfg.write_text(config_json, encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    monkeypatch.setenv("TOKENOMY_SKIP_UPDATE_CHECK", "1")
    monkeypatch.setattr(cfgmod, "creds_present", creds)


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


def test_dashboard_explicit_empty_keeps_settings_hint(tmp_path, monkeypatch):
    # 사용자가 설정에서 전부 끔(명시적 []) → 온보딩 아님(기존 '설정에서 켜세요' 유지).
    client = _client(tmp_path, monkeypatch, config_json='{"tracked_providers": []}')
    r = client.get("/")
    assert r.status_code == 200
    assert "Tokenomy 시작하기" not in r.text
    assert "표시할 AI가 없습니다" in r.text
