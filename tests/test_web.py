import json

from fastapi.testclient import TestClient

from tokenomy.db import connect
from tokenomy.web import app as app_module


def _client(tmp_path, monkeypatch):
    """app.connect를 임시 DB로 교체한 TestClient."""
    db = tmp_path / "t.db"
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "cfg.json"))  # 개인 config 격리(미존재 → 예산 0)
    monkeypatch.setenv("TOKENOMY_SKIP_UPDATE_CHECK", "1")  # 웹 테스트는 업데이트 네트워크 미사용

    def fake_connect(*a, **k):
        return connect(str(db))

    monkeypatch.setattr(app_module, "connect", fake_connect)
    return TestClient(app_module.app), fake_connect


def test_dashboard_empty_db_ok(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/")
    assert r.status_code == 200
    assert "번다운" in r.text


def test_dashboard_bad_query_falls_back(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/?provider=evil&sort=drop")
    assert r.status_code == 200          # 화이트리스트 fallback, 크래시 없음


def test_session_404(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/session/none")
    assert r.status_code == 404


def test_ingest_redirects(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    # cmd_ingest가 실제 홈 디렉터리를 안 긁도록 no-op로 교체
    monkeypatch.setattr(app_module, "cmd_ingest", lambda conn: None)
    r = client.post("/ingest", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_dashboard_renders_sections_with_data(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute(
        "INSERT INTO messages (dedup_key, provider, session_id, project, ts, model, "
        "input_tokens, cache_read, cost_usd, priced) "
        "VALUES ('a','claude','s1','proj','2026-06-10T10:00:00Z','claude-opus-4-8',"
        "100, 10, 12.5, 1)"
    )
    conn.commit()
    r = client.get("/?provider=claude")
    assert r.status_code == 200
    for section in ("번다운", "일별 추세", "효율 코치", "프로젝트별", "복기"):
        assert section in r.text
    assert "공개 API 단가 기준 추정" in r.text   # §5.2 비용 신뢰도 표기
    assert "proj" in r.text                       # 프로젝트별 행


def test_ingest_failure_shows_banner(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    def boom(conn):
        raise RuntimeError("fail")

    monkeypatch.setattr(app_module, "cmd_ingest", boom)
    r = client.post("/ingest", follow_redirects=True)
    assert r.status_code == 200
    assert "오류" in r.text          # /?notice=ingest-failed 배너


def test_trend_data_embedded(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute(
        "INSERT INTO messages (dedup_key, provider, session_id, ts, cost_usd, priced) "
        "VALUES ('a','claude','s1','2026-06-10T10:00:00Z', 7.0, 1)"
    )
    conn.commit()
    r = client.get("/?provider=claude")
    assert "/static/vendor/chart.min.js" in r.text
    assert "trendActual" in r.text          # 임베드된 데이터 변수


def _client_with_config(tmp_path, monkeypatch):
    """_client(=config 격리됨) + 그 config 파일 경로를 함께 돌려준다."""
    client, _ = _client(tmp_path, monkeypatch)   # TOKENOMY_CONFIG → tmp_path/cfg.json
    return client, tmp_path / "cfg.json"


def test_settings_get_renders_form(tmp_path, monkeypatch):
    client, _ = _client_with_config(tmp_path, monkeypatch)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "예산" in r.text
    assert 'name="claude"' in r.text
    assert 'name="codex"' in r.text


def test_settings_post_writes_config(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings", data={"claude": "150", "codex": "40"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["budget"]["claude"] == 150.0
    assert saved["budget"]["codex"] == 40.0


def test_settings_post_invalid_number_falls_back_zero(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings", data={"claude": "abc", "codex": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["budget"]["claude"] == 0.0
    assert saved["budget"]["codex"] == 0.0


def test_dashboard_shows_onboarding_when_no_budget(tmp_path, monkeypatch):
    client, _ = _client_with_config(tmp_path, monkeypatch)  # config 없음 → 예산 0
    r = client.get("/")
    assert r.status_code == 200
    assert "예산을 설정하세요" in r.text
    assert "/settings" in r.text
    assert 'href="/settings">설정</a>' in r.text   # 온보딩 배너 내 설정 링크 직접 검증


def test_dashboard_hides_onboarding_when_budget_set(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"budget": {"claude": 100, "codex": 0}}', encoding="utf-8")
    r = client.get("/")
    assert "예산을 설정하세요" not in r.text


def test_dashboard_has_settings_link_even_with_budget(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"budget": {"claude": 100, "codex": 0}}', encoding="utf-8")
    r = client.get("/")
    assert "/settings" in r.text   # 예산 설정 후에도 설정 페이지 접근 링크 존재


def test_dashboard_shows_update_banner(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    # setattr으로 check_update 자체를 교체하므로 _client의 SKIP env는 우회됨
    monkeypatch.setattr(app_module, "check_update", lambda conn: "v9.9.9")
    r = client.get("/")
    assert r.status_code == 200
    assert "새 버전 v9.9.9" in r.text
    assert "releases/latest" in r.text


def test_dashboard_no_update_banner_when_current(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app_module, "check_update", lambda conn: None)
    r = client.get("/")
    assert "새 버전" not in r.text


def test_root_renders_overview(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/")
    assert r.status_code == 200
    assert "통합 번다운" in r.text
    assert "AI별 현황" in r.text


def test_provider_query_renders_detail(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/?provider=claude")
    assert r.status_code == 200
    assert "번다운" in r.text
    assert "AI별 현황" not in r.text          # detail은 통합 화면 아님


def test_bad_provider_falls_back_to_overview(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/?provider=evil")
    assert r.status_code == 200
    assert "통합 번다운" in r.text             # 화이트리스트 밖 → overview


def test_overview_aggregates_providers(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute(
        "INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,cost_usd,priced) "
        "VALUES ('a','claude','s1','proj','2026-06-10T10:00:00Z','claude-opus-4-8',12.5,1)"
    )
    conn.execute(
        "INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,cost_usd,priced) "
        "VALUES ('b','codex','s2','proj','2026-06-10T11:00:00Z','gpt-5',7.5,1)"
    )
    conn.commit()
    r = client.get("/")
    assert r.status_code == 200
    for section in ("통합 번다운", "AI별 현황", "통합 추세", "통합 효율 코치",
                    "통합 프로젝트별", "복기"):
        assert section in r.text
    assert "proj" in r.text
    assert 'class="tabs"' in r.text
    assert 'class="ai-cards"' in r.text


def test_overview_tabs_active_state(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/")
    assert 'href="/"' in r.text
    assert 'href="/?provider=claude"' in r.text
    assert 'href="/?provider=codex"' in r.text


def test_projects_page_ok(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/projects")
    assert r.status_code == 200
    assert "전체 프로젝트별 비용" in r.text


def test_projects_bad_period_falls_back(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/projects?period=evil&provider=evil&sort=drop")
    assert r.status_code == 200          # 화이트리스트 폴백, 크래시 없음


def test_projects_page_renders_rows_and_drilldown(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute(
        "INSERT INTO messages (dedup_key,provider,session_id,project,ts,cost_usd,priced) "
        "VALUES ('a','claude','s1','myproj','2026-06-10T10:00:00Z',12.5,1)"
    )
    conn.commit()
    r = client.get("/projects?anchor=2026-06-10&period=month")
    assert r.status_code == 200
    assert "myproj" in r.text
    assert "/sessions?project=" in r.text      # 드릴다운 링크
    assert "합계 $12.50" in r.text             # 요약 헤더


def test_sessions_page_ok(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/sessions")
    assert r.status_code == 200
    assert "복기" in r.text
    assert "최신순" in r.text                 # 정렬 토글


def test_sessions_bad_params_fall_back(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/sessions?period=evil&provider=evil&order=drop")
    assert r.status_code == 200


def test_sessions_page_renders_rows_and_filter(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute(
        "INSERT INTO messages (dedup_key,provider,session_id,project,ts,cost_usd,priced) "
        "VALUES ('a','claude','s1','myproj','2026-06-10T10:00:00Z',8.0,1)"
    )
    conn.commit()
    r = client.get("/sessions?anchor=2026-06-10&period=month&project=myproj")
    assert r.status_code == 200
    assert "myproj" in r.text
    assert "프로젝트 필터: myproj" in r.text   # 필터 표시 + 해제 링크
    assert "합계 $8.00" in r.text


def test_sessions_drilldown_slash_project_roundtrips(tmp_path, monkeypatch):
    # 슬래시를 포함하는 프로젝트 경로가 쿼리로 왕복되어 그 프로젝트만 필터되는지 검증
    # (드릴다운 링크는 /sessions?project=/proj/sub 형태 — 슬래시는 query에서 합법)
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute(
        "INSERT INTO messages (dedup_key,provider,session_id,project,ts,cost_usd,priced) "
        "VALUES ('a','claude','s1','/proj/sub','2026-06-10T10:00:00Z',3.0,1)"
    )
    conn.execute(
        "INSERT INTO messages (dedup_key,provider,session_id,project,ts,cost_usd,priced) "
        "VALUES ('b','claude','s2','/other','2026-06-10T11:00:00Z',5.0,1)"
    )
    conn.commit()
    r = client.get("/sessions?anchor=2026-06-10&period=month&project=/proj/sub")
    assert r.status_code == 200
    assert "합계 $3.00" in r.text          # /proj/sub 의 s1만 (5.0짜리 /other 제외)
    assert "프로젝트 필터: /proj/sub" in r.text


def test_overview_has_full_view_links(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/")
    assert 'href="/projects' in r.text
    assert 'href="/sessions' in r.text
    assert "전체 보기" in r.text


def test_dashboard_has_full_view_links(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/?provider=claude")
    assert "/projects?provider=claude" in r.text
    assert "/sessions?provider=claude" in r.text
