import json
from pathlib import Path

from fastapi.testclient import TestClient

from tokenomy.db import connect, insert_official_buckets
from tokenomy.web import app as app_module

_FIX = Path(__file__).parent / "fixtures" / "official"


def _seed_official(conn, provider, fixture, parse):
    """엔터프라이즈 실측 fixture를 parse→insert로 시드(골든 테스트 공용)."""
    raw = json.loads((_FIX / fixture).read_text(encoding="utf-8"))
    buckets = parse(raw, credit_to_usd=0.04)
    insert_official_buckets(conn, provider=provider,
                            fetched_at="2026-06-20T09:00:00+09:00",
                            buckets=buckets, created_at="2026-06-20T09:00:00+09:00")


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
    assert "총지출" in r.text


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
    for section in ("이번 달 총지출", "통합 추세", "통합 효율 코치", "통합 프로젝트별", "복기"):
        assert section in r.text
    assert "AI별 사용 현황" not in r.text          # 번다운 카드 섹션 제거
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
    assert "trendSeries" in r.text          # AI별 스택 시리즈 데이터
    assert "trendBudget" not in r.text      # 예산 가로선 제거
    assert "월 예산" not in r.text           # 가로선 레이블 제거
    assert "endLabels" in r.text            # 끝점 라벨 플러그인(상시 구성 표시)


def _client_with_config(tmp_path, monkeypatch):
    """_client(=config 격리됨) + 그 config 파일 경로를 함께 돌려준다."""
    client, _ = _client(tmp_path, monkeypatch)   # TOKENOMY_CONFIG → tmp_path/cfg.json
    return client, tmp_path / "cfg.json"


def test_settings_get_renders_form(tmp_path, monkeypatch):
    client, _ = _client_with_config(tmp_path, monkeypatch)
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'name="track_claude"' in r.text
    assert 'name="track_codex"' in r.text
    assert 'class="sidebar"' in r.text
    assert "전체 대화 기록은 저장하지 않습니다" in r.text



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
    assert "이번 달 총지출" in r.text
    assert "AI별 사용 현황" not in r.text   # 번다운 카드 섹션 제거
    assert 'class="sidebar"' in r.text
    assert 'href="/history"' in r.text
    assert 'href="/analysis"' in r.text   # 나브: 모델별→차원별


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
    for section in ("이번 달 총지출", "통합 추세", "통합 효율 코치",
                    "통합 프로젝트별", "복기"):
        assert section in r.text
    assert "AI별 사용 현황" not in r.text   # 번다운 카드 섹션 제거
    assert "proj" in r.text



def test_dashboard_no_budget_banner(tmp_path, monkeypatch):
    """예산 온보딩 배너가 더 이상 없어야 함."""
    client, _ = _client(tmp_path, monkeypatch)
    html = client.get("/").text
    assert "예산을 설정하세요" not in html


def test_dashboard_shows_month_total(tmp_path, monkeypatch):
    """이번 달 총지출 카드가 렌더링되어야 함."""
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute(
        "INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,cost_usd,priced) "
        "VALUES ('a','claude','s1','proj','2026-06-10T10:00:00Z','claude-opus-4-8',5.0,1)"
    )
    conn.commit()
    html = client.get("/").text
    assert "이번 달 총지출" in html


def test_dashboard_no_burndown_cards(tmp_path, monkeypatch):
    """번다운 카드 섹션이 제거되어야 함."""
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute(
        "INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,cost_usd,priced) "
        "VALUES ('a','claude','s1','proj','2026-06-10T10:00:00Z','claude-opus-4-8',5.0,1)"
    )
    conn.commit()
    html = client.get("/").text
    assert "AI별 사용 현황" not in html


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


def test_models_redirects_to_analysis(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/models", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/analysis?dim=model"


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


def test_analysis_renders_rows(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,model,cost_usd,priced) "
                 "VALUES ('a','claude','s1','2026-06-10T10:00:00Z','claude-opus-4-8',12.5,1)")
    conn.commit()
    r = client.get("/analysis?anchor=2026-06-10&dim=model")
    assert r.status_code == 200
    assert "claude-opus-4-8" in r.text
    assert "합계 $12.50" in r.text


def test_dashboard_shows_codex_section(tmp_path, monkeypatch):
    """Codex 공식 패널이 대시보드에 렌더링된다(번다운 카드 제거 후)."""
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,cost_usd,priced) "
                 "VALUES ('a','codex','s1','2026-06-13T01:00:00Z',6.0,1)")
    conn.commit()
    r = client.get("/")
    assert r.status_code == 200
    assert "Codex" in r.text


def test_history_week_period_param(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,cost_usd,priced) "
                 "VALUES ('a','claude','s1','myproj','2026-06-09T01:00:00Z',2.0,1)")
    conn.commit()
    r = client.get("/history?anchor=2026-06-13&period=week")
    assert r.status_code == 200
    assert "2026-06-08 ~ 06-14" in r.text


def test_history_custom_range_param(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,cost_usd,priced) "
                 "VALUES ('a','claude','s1','myproj','2026-06-12T01:00:00Z',3.0,1)")
    conn.commit()
    r = client.get("/history?start=2026-06-12&end=2026-06-30")
    assert r.status_code == 200
    assert "2026-06-12 ~ 2026-06-30" in r.text


def test_history_bad_period_falls_back(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/history?period=decade&start=nonsense")
    assert r.status_code == 200                      # 크래시 없이 월간 폴백


def test_analysis_week_period_param(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,model,cost_usd,priced) "
                 "VALUES ('a','claude','s1','2026-06-09T10:00:00Z','claude-opus-4-8',8.0,1)")
    conn.commit()
    r = client.get("/analysis?anchor=2026-06-13&period=week&dim=model")
    assert r.status_code == 200
    assert "2026-06-08 ~ 06-14" in r.text


def test_history_has_period_toggle_and_range(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/history")
    assert r.status_code == 200
    assert 'name="period"' in r.text                 # 주/월 토글
    assert 'name="start"' in r.text and 'name="end"' in r.text   # 날짜 범위 입력


def test_analysis_has_period_toggle_and_range(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/analysis")
    assert r.status_code == 200
    assert 'name="period"' in r.text
    assert 'name="start"' in r.text and 'name="end"' in r.text


def test_analysis_dim_selector_and_skill(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,model,cost_usd,priced,attribution_skill) "
                 "VALUES ('a','claude','s1','2026-06-10T10:00:00Z','claude-opus-4-8',5.0,1,'brainstorming')")
    conn.commit()
    r = client.get("/analysis?anchor=2026-06-10&dim=skill")
    assert r.status_code == 200
    assert "brainstorming" in r.text
    assert ">스킬</a>" in r.text                       # 차원 선택기 항목
    assert "Claude 로그 기준" in r.text                # claude_only 안내


def test_analysis_bad_dim_falls_back(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/analysis?dim=evil")
    assert r.status_code == 200                        # 화이트리스트 폴백


def test_analysis_shows_sidechain_card(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,model,cost_usd,priced,is_sidechain) "
                 "VALUES ('a','claude','s1','2026-06-10T10:00:00Z','claude-opus-4-8',8.0,1,0)")
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,model,cost_usd,priced,is_sidechain) "
                 "VALUES ('b','claude','s1','2026-06-10T11:00:00Z','claude-opus-4-8',2.0,1,1)")
    conn.commit()
    r = client.get("/analysis?anchor=2026-06-10")
    assert "서브에이전트 비중" in r.text


def test_analysis_all_provider_link_preserves_dim(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/analysis?dim=branch")
    assert r.status_code == 200
    # 모든 네비 링크가 dim을 보존해야 함 — dim 없는 /analysis?anchor= 링크가 존재하면 안 됨
    assert "/analysis?anchor=" not in r.text


def test_analysis_cache_wr_column(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute(
        "INSERT INTO messages(dedup_key,provider,session_id,project,ts,model,"
        "input_tokens,output_tokens,cache_creation,cache_read,cost_usd,priced) "
        "VALUES('a','claude','s1','p','2026-06-10T10:00:00Z','claude-opus-4-8',10,20,30,40,1.0,1)"
    )
    conn.commit()
    r = client.get("/analysis?dim=model")
    assert r.status_code == 200
    assert "cache_wr" in r.text


def test_dashboard_token_composition(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute(
        "INSERT INTO messages(dedup_key,provider,session_id,project,ts,model,"
        "input_tokens,output_tokens,cache_creation,cache_read,cost_usd,priced) "
        "VALUES('a','claude','s1','p','2026-06-10T10:00:00Z','claude-opus-4-8',10,20,30,40,1.0,1)"
    )
    conn.commit()
    r = client.get("/")
    assert r.status_code == 200
    assert "토큰 구성" in r.text


def test_human_tokens_and_share_pct():
    from tokenomy.web.views import _human_tokens, _share_pct
    assert _human_tokens(0) == "0"
    assert _human_tokens(950) == "950"
    assert _human_tokens(12_000) == "12.0K"
    assert _human_tokens(1_500_000) == "1.5M"
    assert _human_tokens(2_300_000_000) == "2.3B"
    assert _share_pct(0.0) == "0%"
    assert _share_pct(0.004) == "<1%"
    assert _share_pct(0.5) == "50%"


def test_settings_coverage_card_renders(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,input_tokens,cost_usd,priced) "
                 "VALUES ('a','claude','s1','p','2026-06-10T10:00:00Z','claude-opus-4-8',100,5.0,1)")
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,input_tokens,cost_usd,priced) "
                 "VALUES ('b','codex','s2','p','2026-06-10T10:00:00Z','gpt-unknown',100,0.0,0)")
    conn.commit()
    r = client.get("/settings")
    assert r.status_code == 200
    assert "단가 커버리지" in r.text
    assert "(미식별)" in r.text
    assert "gpt-unknown" in r.text


def test_settings_coverage_card_empty_db(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "단가 커버리지" in r.text


def test_settings_coverage_card_shows_suspect(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    # 미배포 모델 gpt-5.9는 전용 항목이 없어 실제 pricing.json의 'gpt-5'에 부분일치
    # → 직후가 '.'이라 버전경계 의심(suspect). (gpt-5.5는 이제 전용 항목으로 정상 매칭)
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,input_tokens,cost_usd,priced) "
                 "VALUES ('a','codex','s1','p','2026-06-10T10:00:00Z','gpt-5.9',100,1.0,1)")
    conn.commit()
    r = client.get("/settings")
    assert r.status_code == 200
    assert "확인 필요" in r.text   # suspect 상태 라벨
    assert "gpt-5.9" in r.text     # 의심 안내에 모델명


def test_coverage_card_context_injected_pricing():
    # 주입형 시그니처 — 실제 pricing.json이 아닌 테스트 dict를 주입해 격리 검증
    from tokenomy.web.views import coverage_card_context
    conn = connect(":memory:")
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,input_tokens,cost_usd,priced) "
                 "VALUES ('a','codex','s1','p','2026-06-10T10:00:00Z','gpt-5.5',100,1.0,1)")
    conn.commit()
    pricing = {"match": [
        {"contains": "gpt-5", "provider": "codex", "input": 1.25, "output": 10.0,
         "cache_write": 0.0, "cache_read": 0.125},
    ]}
    ctx = coverage_card_context(conn, pricing)
    # gpt-5.5가 주입 pricing의 'gpt-5'에 부분일치 → 버전경계 의심
    assert ctx["coverage_status"][0] == "info"
    assert "gpt-5.5" in ctx["coverage_suspects"]


# ── Task 6+7 TDD 신규 테스트 ──────────────────────────────────────────────────

def test_overview_context_keys(tmp_path, monkeypatch):
    """overview_context가 claude_official/codex_official을 반환하고 gauge/official_notes는 없어야 한다."""
    from tokenomy.web.views import overview_context
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    ctx = overview_context(conn, "cost")
    assert "claude_official" in ctx and "codex_official" in ctx
    assert "gauge" not in ctx and "official_notes" not in ctx


def test_overview_has_official_panels(tmp_path, monkeypatch):
    """대시보드에 수동 입력 폼(/official)이 없어야 한다."""
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,cost_usd,priced) "
                 "VALUES ('a','codex','s1','p','2026-06-10T10:00:00Z','gpt-5.5',5.0,1)")
    conn.commit()
    r = client.get("/")
    assert r.status_code == 200
    assert 'action="/official"' not in r.text
    assert "공식 사용량 입력" not in r.text


def test_overview_official_panel_renders(tmp_path, monkeypatch):
    """공식 버킷을 삽입하면 공식 미러 패널이 렌더링되어야 한다."""
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    from tokenomy.db import insert_official_buckets
    from tokenomy.official_parser import OfficialBucket
    conn_b = OfficialBucket(
        bucket_key="monthly", raw_key="spend", bucket_kind="monthly_limit", label="월 사용 한도",
        native_unit="usd", used_native=30.0, limit_native=100.0, remaining_native=70.0,
        used_usd=30.0, limit_usd=100.0, remaining_usd=70.0, utilization=30.0, resets_at=None,
    )
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[conn_b], created_at="2026-06-10T09:00:00+09:00")
    r = client.get("/")
    assert r.status_code == 200
    assert 'action="/official"' not in r.text     # 수동 입력 폼 제거
    assert "공식" in r.text                         # 공식 미러 패널 노출
    assert "월 사용 한도" in r.text                 # 버킷 라벨 렌더 확인
    assert "30" in r.text and "100" in r.text      # used/limit USD 렌더 확인


# ── 엔터프라이즈 view 골든 테스트(실측 응답 고정) ────────────────────────────────
# 개인 구독(% 창)이 아니라 enterprise 계정의 달러/크레딧 게이지 분기를 고정한다.
# 실측 raw(docs/enterprise-usage-api-response.md)를 fixture로 시드 → 렌더 → 출력 단언.

def test_overview_enterprise_claude_dollar_buckets_render(tmp_path, monkeypatch):
    """엔터프라이즈 Claude(실측): 달러 버킷이 USD 게이지로 렌더되어야 한다.

    spend(월 사용 한도 $0/$243) + cinder_cove(포함된 크레딧 $393.10/$1,000) 두 버킷이
    used/limit USD와 소진율로 나온다. 개인 구독 % 창이 아니라 달러 게이지 분기 검증.
    """
    from tokenomy.official_parser import parse_claude
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    _seed_official(conn, "claude", "claude_enterprise_real.json", parse_claude)
    r = client.get("/")
    assert r.status_code == 200
    # cinder_cove 이벤트 크레딧(달러 본체) — 코드네임 비의존 라벨 + USD 게이지
    assert "포함된 크레딧" in r.text
    assert "$393.10" in r.text and "1,000" in r.text
    assert "39% 사용됨" in r.text
    # spend 월 사용 한도($0/$243) — used 0도 게이지로 렌더
    assert "월 사용 한도" in r.text and "243" in r.text


def test_overview_enterprise_codex_credit_gauge_renders(tmp_path, monkeypatch):
    """엔터프라이즈 Codex(실측): 크레딧 한도가 credit_to_usd 환산 USD 게이지로 렌더되어야 한다.

    individual_limit used 1073.94 / limit 5875 credits → ×0.04 = $42.96 / $235 월간 게이지.
    주간(월÷4) 추정 게이지도 함께. 개인 구독 % 창이 아니라 USD 월간 분기 검증.
    """
    from tokenomy.official_parser import parse_codex
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    _seed_official(conn, "codex", "codex_enterprise_real.json", parse_codex)
    r = client.get("/")
    assert r.status_code == 200
    assert "월간 한도" in r.text
    assert "$42.96" in r.text and "235" in r.text
    assert "이번 주" in r.text       # 주간(월÷4) 추정 게이지도 렌더


def test_settings_shows_credit_to_usd(tmp_path, monkeypatch):
    """설정 페이지에 credit_to_usd 입력 필드가 있어야 한다."""
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "credit_to_usd" in r.text or "크레딧" in r.text


def test_settings_post_persists_credit_to_usd(tmp_path, monkeypatch):
    """POST /settings에서 credit_to_usd를 저장하고 GET /settings에서 조회할 수 있어야 한다."""
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings", data={"credit_to_usd": "0.05"},
                    follow_redirects=False)
    assert r.status_code == 303
    g = client.get("/settings")
    assert "0.05" in g.text


# ── Task 4 TDD: official_refresh ──────────────────────────────────────────────────

def test_official_refresh_calls_fetch_and_redirects(tmp_path, monkeypatch):
    # tracked_providers를 명시 설정해 크레덴셜 파일 유무와 무관하게 결정론적으로 동작
    client, cfg_path = _client_with_config(tmp_path, monkeypatch)
    cfg_path.write_text('{"tracked_providers": ["claude", "codex"]}', encoding="utf-8")
    calls = []
    monkeypatch.setattr(app_module, "fetch_provider",
                        lambda p, **k: calls.append(p))
    r = client.post("/official/refresh", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert set(calls) == {"claude", "codex"}


def test_official_refresh_scopes_single_provider(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(app_module, "fetch_provider",
                        lambda p, **k: calls.append(p))
    r = client.post("/official/refresh", data={"provider": "claude"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert calls == ["claude"]


def test_official_refresh_redirects_even_on_fetch_error(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    def boom(p, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(app_module, "fetch_provider", boom)
    r = client.post("/official/refresh", data={}, follow_redirects=False)
    assert r.status_code == 303   # 결과 무관 redirect(예외도 삼킴)


# ── Task 5 TDD: 설정 UI — 공식 자동 취득 토글 지속 ──────────────────────────────

def test_settings_shows_official_fetch_section(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "공식 사용량 자동 취득" in r.text
    assert 'name="min_interval"' in r.text


def test_settings_post_saves_official_fetch(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    cfg = tmp_path / "cfg.json"
    r = client.post("/settings", data={
        "claude": "100", "codex": "50", "budget_start": "", "credit_to_usd": "0.04",
        "min_interval": "10",
    }, follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    of = saved["official_fetch"]
    assert of["min_interval_minutes"] == 10
    assert "enabled" not in of


# ── Task 6 TDD: 대시보드 새로고침 버튼 + 취득 상태 표면 ──────────────────────────────

def test_overview_has_refresh_button(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/")
    assert r.status_code == 200
    assert 'action="/official/refresh"' in r.text


def test_overview_shows_auth_error_note(tmp_path, monkeypatch):
    client, fake_connect = _client(tmp_path, monkeypatch)
    # codex 토큰 만료 상태를 심는다
    from tokenomy.db import upsert_fetch_state
    conn = fake_connect()
    upsert_fetch_state(conn, "codex", last_attempt_at="2026-06-10T09:00:00+09:00",
                       last_success_at=None, last_status="auth_error", last_error="HTTP 401")
    r = client.get("/")
    assert "Codex CLI를 1회 실행" in r.text


# ── Task 6 설정 UI: 예산 입력 제거, tracked_providers 선택 ─────────────────────────

def test_settings_post_writes_tracked_providers(tmp_path, monkeypatch):
    client, cfg_path = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings", data={"track_claude": "on", "min_interval": "7",
                                       "credit_to_usd": "0.05"},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["tracked_providers"] == ["claude"]
    assert saved["official_fetch"]["min_interval_minutes"] == 7
    assert "budget" not in saved


def test_settings_get_has_provider_checkboxes(tmp_path, monkeypatch):
    client, _ = _client_with_config(tmp_path, monkeypatch)
    html = client.get("/settings").text
    assert 'name="track_claude"' in html
    assert 'name="track_codex"' in html
    assert "월 예산" not in html
