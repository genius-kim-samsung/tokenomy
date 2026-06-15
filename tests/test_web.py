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
    r = client.get("/?sort=drop")
    assert r.status_code == 200          # 화이트리스트 fallback, 크래시 없음


def test_session_404(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/session/none")
    assert r.status_code == 404


def test_session_detail_renders(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,cost_usd,priced) "
                 "VALUES ('a','claude','s1','myproj','2026-06-10T10:00:00Z','claude-opus-4-8',5.0,1)")
    conn.commit()
    r = client.get("/session/s1")
    assert r.status_code == 200
    assert "세션 상세" in r.text
    assert 'class="sidebar"' in r.text
    assert 'href="/history"' in r.text


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
    r = client.get("/")
    assert r.status_code == 200
    for section in ("통합 번다운", "통합 추세", "통합 효율 코치", "통합 프로젝트별", "복기"):
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
    r = client.get("/")
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
    assert 'class="sidebar"' in r.text
    assert "대화 원문은 저장하지 않습니다" in r.text


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
    assert 'class="sidebar"' in r.text
    assert 'href="/history"' in r.text
    assert 'href="/models"' in r.text


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
    assert 'class="ai-cards"' in r.text


def test_projects_redirects_to_history(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/projects", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/history"


def test_sessions_redirects_to_history(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/sessions", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/history"


def test_overview_links_into_history(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/")
    assert 'href="/history"' in r.text
    assert "view=folder" not in r.text and "view=session" not in r.text


def test_history_page_ok(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/history")
    assert r.status_code == 200
    assert "내역" in r.text
    assert "<th>날짜</th>" in r.text and "<th>세션ID</th>" in r.text
    assert 'class="view-seg"' not in r.text          # 5탭 제거됨


def test_history_bad_params_fall_back(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/history?provider=evil&sort=drop")
    assert r.status_code == 200                    # 화이트리스트 폴백, 크래시 없음


def test_history_renders_tree(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute(
        "INSERT INTO messages (dedup_key,provider,session_id,project,ts,cost_usd,priced) "
        "VALUES ('a','claude','s1','myproj','2026-06-10T01:00:00Z',3.0,1)"
    )
    conn.commit()
    r = client.get("/history?anchor=2026-06-10&sort=date_desc")
    assert r.status_code == 200
    assert "myproj" in r.text
    assert "합계 $3.00" in r.text
    assert 'class="grp grp-date"' in r.text          # 날짜 그룹 행


def test_history_partial_returns_fragment_only(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute(
        "INSERT INTO messages (dedup_key,provider,session_id,project,ts,cost_usd,priced) "
        "VALUES ('a','claude','s1','myproj','2026-06-10T01:00:00Z',3.0,1)"
    )
    conn.commit()
    r = client.get("/history?anchor=2026-06-10&sort=date_desc&partial=1")
    assert r.status_code == 200
    assert "myproj" in r.text
    assert "<!doctype html>" not in r.text.lower()
    assert 'class="sidebar"' not in r.text
    assert 'id="provider-filter"' in r.text


def test_history_shows_data_freshness(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute(
        "INSERT INTO messages (dedup_key,provider,session_id,project,ts,cost_usd,priced) "
        "VALUES ('a','claude','s1','myproj','2026-06-10T01:00:00Z',1.0,1)"
    )
    conn.commit()
    r = client.get("/history?anchor=2026-06-10")
    assert "데이터 최신" in r.text


def test_history_renders_signal_markers(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    # s1: 6/9 첫 등장(캐시 높음), 6/10 이어짐(캐시율 0.1 → cache_miss)
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,"
                 "input_tokens,cache_read,cost_usd,priced) VALUES "
                 "('a','claude','s1','myproj','2026-06-09T01:00:00Z',10,90,1.0,1)")
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,"
                 "input_tokens,cache_read,cost_usd,priced) VALUES "
                 "('b','claude','s1','myproj','2026-06-10T01:00:00Z',90,10,1.0,1)")
    conn.commit()
    r = client.get("/history?anchor=2026-06-10&sort=date_desc")
    assert "cache-miss" in r.text        # 캐시미스 셀 클래스
    assert "↩" in r.text                 # 이어짐 표시


def test_history_has_ai_badge(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,cost_usd,priced) "
                 "VALUES ('a','claude','s1','myproj','2026-06-10T01:00:00Z',3.0,1)")
    conn.execute("INSERT INTO sessions (session_id, provider) VALUES ('s1','claude')")
    conn.commit()
    r = client.get("/history?anchor=2026-06-10")
    assert "ai-badge" in r.text


def test_history_filters_use_htmx_not_handrolled_js(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/history")
    assert r.status_code == 200
    assert 'hx-get="/history"' in r.text
    assert "fetch('/history" not in r.text
    assert "popstate" not in r.text


def test_history_hx_request_header_returns_fragment(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute(
        "INSERT INTO messages (dedup_key,provider,session_id,project,ts,cost_usd,priced) "
        "VALUES ('a','claude','s1','myproj','2026-06-10T01:00:00Z',3.0,1)"
    )
    conn.commit()
    r = client.get("/history?anchor=2026-06-10", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "myproj" in r.text
    assert "<!doctype html>" not in r.text.lower()
    assert 'class="sidebar"' not in r.text
    assert 'id="provider-filter"' in r.text


def test_history_restore_request_returns_full_page(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/history", headers={"HX-Request": "true",
                                        "HX-History-Restore-Request": "true"})
    assert r.status_code == 200
    assert 'class="sidebar"' in r.text


def test_history_partial_refreshes_nav_links_with_filter(tmp_path, monkeypatch):
    # 필터(provider/sort) 변경 시 부분 조각의 기간 네비 링크가 새 값을 반영해야 한다.
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/history?anchor=2026-06-10&provider=claude&sort=date_desc",
                   headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "anchor=2026-05-31&provider=claude&sort=date_desc" in r.text


def test_models_page_ok(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/models")
    assert r.status_code == 200
    assert "모델별" in r.text
    assert "비중" in r.text


def test_history_has_collapse_ui(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/history")
    assert r.status_code == 200
    assert 'id="toggle-all"' in r.text          # 모두 접기/펼치기 버튼
    assert "/static/tree.js" in r.text          # 접기 스크립트 로드


def test_history_folder_key_is_index_not_path(tmp_path, monkeypatch):
    # data-folder 키는 폴더 경로(역슬래시/콜론 위험)가 아니라 'YYYY-MM-DD::<정수>' 형식이어야 한다.
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,cost_usd,priced) "
                 "VALUES ('a','claude','s1','myproj','2026-06-10T01:00:00Z',3.0,1)")
    conn.commit()
    r = client.get("/history?anchor=2026-06-10")
    assert 'data-folder="2026-06-10::1"' in r.text          # 폴더 키 = 'YYYY-MM-DD::<정수>'
    assert 'data-folder="2026-06-10::myproj"' not in r.text  # 폴더명/경로가 키에 들어가지 않음


def test_models_page_renders_rows(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,model,cost_usd,priced) "
                 "VALUES ('a','claude','s1','2026-06-10T10:00:00Z','claude-opus-4-8',12.5,1)")
    conn.commit()
    r = client.get("/models?anchor=2026-06-10")
    assert r.status_code == 200
    assert "claude-opus-4-8" in r.text
    assert "합계 $12.50" in r.text


def test_settings_get_shows_budget_start_field(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"budget": {"claude": 100, "codex": 40}, "budget_start": "2026-06-12"}',
                   encoding="utf-8")
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'name="budget_start"' in r.text
    assert "2026-06-12" in r.text          # 기존 값 표시


def test_settings_post_writes_budget_start(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings",
                    data={"claude": "200", "codex": "40", "budget_start": "2026-06-12"},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["budget_start"] == "2026-06-12"


def test_settings_post_blank_budget_start_is_null(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings", data={"claude": "200", "codex": "40", "budget_start": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["budget_start"] is None
