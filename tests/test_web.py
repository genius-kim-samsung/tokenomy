import json
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from tokenomy.clock import KST
from tokenomy.db import connect, insert_official_buckets, insert_official_raw
from tokenomy.web import app as app_module
from tokenomy.web import views as views_module

_FIX = Path(__file__).parent / "fixtures" / "official"

# 라우트 경유 테스트의 고정 시계. 시드·골든 fixture가 전부 2026-06이라 실제 시계로는
# 2026-07부터 월 스코프 밖으로 밀리고 resets_at(2026-07-01) 만료 필터에 걸린다.
# view 직접 호출은 now_kst 주입으로 이미 고정 — 라우트(TestClient) 경로만 실제 시계를
# 타던 것을 같은 6월 시점으로 고정한다(공식 fixture fetched_at 이후 · 리셋 이전).
_FROZEN_NOW = datetime(2026, 6, 20, 12, 0, tzinfo=KST)


class _FrozenDatetime(datetime):
    """now만 고정 시점을 돌려주는 대역 — strptime 등 나머지 클래스 메서드는 상속."""

    @classmethod
    def now(cls, tz=None):
        return _FROZEN_NOW.astimezone(tz) if tz else _FROZEN_NOW.replace(tzinfo=None)


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
    cfg = tmp_path / "cfg.json"
    # 활성 AI를 claude·codex 둘로 고정 — "전체=활성 합산"(ADR 0005)이라 테스트 결정성을 위해
    # 명시한다(미지정 시 크레덴셜 존재로 시드돼 CI/기기별로 활성 집합이 갈린다).
    cfg.write_text('{"tracked_providers": ["claude", "codex"]}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))  # 개인 config 격리
    monkeypatch.setenv("TOKENOMY_SKIP_UPDATE_CHECK", "1")  # 웹 테스트는 업데이트 네트워크 미사용

    def fake_connect(*a, **k):
        return connect(str(db))

    monkeypatch.setattr(app_module, "connect", fake_connect)
    # 라우트는 now_kst를 안 넘기고 모듈의 datetime.now(KST)로 시계를 읽는다 → 고정 대역으로 교체.
    monkeypatch.setattr(app_module, "datetime", _FrozenDatetime)
    monkeypatch.setattr(views_module, "datetime", _FrozenDatetime)
    return TestClient(app_module.app), fake_connect


def test_humanize_count_abbreviates_k_and_m():
    h = app_module._humanize_count
    assert h(0) == "0"
    assert h(999) == "999"
    assert h(1000) == "1.0K"
    assert h(12345) == "12.3K"
    assert h(10_500_000) == "10.5M"
    assert h(None) == "0"


# ─── 표시 포맷 규칙(ADR 0020) ─────────────────────────────────────────────────

def test_usd_one_decimal_with_comma():
    u = app_module._usd
    assert u(89.1) == "$89.1"
    assert u(89.16) == "$89.2"      # 표시 직전 1자리 반올림
    assert u(1234.5) == "$1,234.5"  # 천 단위 콤마
    assert u(0) == "$0.0"
    assert u(None) == "—"           # 값 없음


def test_comma_thousands_separator():
    c = app_module._comma
    assert c(3042) == "3,042"
    assert c(999) == "999"
    assert c(1234567) == "1,234,567"
    assert c(0) == "0"
    assert c(None) == "0"


def test_fmt_datetime_kst_with_year():
    f = app_module._fmt_datetime
    assert f("2026-06-06T06:30:00Z") == "2026-06-06 15:30"  # UTC+9 → KST
    assert f(None) == ""
    assert f("") == ""


def test_modelfmt_claude_family():
    m = app_module._modelfmt
    assert m("claude-opus-4-8") == "Opus 4.8"
    assert m("claude-opus-4-1-20250805") == "Opus 4.1"
    assert m("claude-sonnet-4-6") == "Sonnet 4.6"
    assert m("claude-haiku-4-5-20251001") == "Haiku 4.5"
    assert m("claude-fable-5") == "Fable 5"
    assert m("claude-3-5-sonnet-20241022") == "Sonnet 3.5"


def test_modelfmt_codex_family():
    m = app_module._modelfmt
    assert m("gpt-5-codex") == "GPT-5 Codex"
    assert m("gpt-5.1-codex") == "GPT-5.1 Codex"
    assert m("gpt-5") == "GPT-5"
    assert m("gpt-4") == "GPT-4"
    assert m("gpt-5.4-mini") == "GPT-5.4 Mini"
    assert m("o4-mini") == "o4-mini"


def test_modelfmt_fallback_and_unknown():
    m = app_module._modelfmt
    assert m("some-weird-model") == "some-weird-model"  # 매칭 실패 → raw 폴백
    assert m(None) == "(unknown)"
    assert m("") == "(unknown)"


def test_dur_session_length():
    d = app_module._dur
    assert d("2026-06-06T00:00:00Z", "2026-06-06T10:25:00Z") == "10시간 25분"
    assert d("2026-06-06T00:00:00Z", "2026-06-06T00:45:00Z") == "45분"
    assert d("2026-06-06T00:00:00Z", "2026-06-06T00:00:30Z") == "1분 미만"
    assert d("2026-06-06T00:00:00Z", "2026-06-08T03:00:00Z") == "2일 3시간"
    assert d("2026-06-06T00:00:00Z", "2026-06-06T03:00:00Z") == "3시간"  # 0분 생략
    assert d(None, "2026-06-06T10:00:00Z") == "—"
    assert d("2026-06-06T00:00:00Z", None) == "—"


def test_dashboard_empty_db_ok(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/")
    assert r.status_code == 200
    assert "기간별 사용량" in r.text   # 총지출 카드 폐지·기간별 사용량 카드로 통합(ADR 0017)


def test_sidebar_shows_mini_switch_when_mini_view_available(tmp_path, monkeypatch):
    """미니뷰 가용 플랫폼(Windows) — 사이드바에 미니뷰 전환 버튼이 렌더된다(ADR 0008·0013)."""
    monkeypatch.setitem(app_module.templates.env.globals, "mini_view_available", True)
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/")
    assert 'id="mini-switch"' in r.text


def test_sidebar_hides_mini_switch_when_mini_view_unavailable(tmp_path, monkeypatch):
    """미니뷰 비가용 플랫폼(Linux, ADR 0013) — 버튼 자체를 렌더하지 않는다.
    Wayland에서 미니 창은 깨지므로 진입점인 버튼을 서버에서 제거한다(JS 노출 게이트만으론 부족)."""
    monkeypatch.setitem(app_module.templates.env.globals, "mini_view_available", False)
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/")
    assert 'id="mini-switch"' not in r.text


def test_dashboard_folder_usage_card(tmp_path, monkeypatch):
    # "폴더 사용량" 카드: 새 타이틀·로컬 부제·토큰 약식·basename(전체 경로 hover)·토글 제거.
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute(
        "INSERT INTO messages (dedup_key, provider, session_id, project, ts, model, "
        "input_tokens, output_tokens, cache_creation, cache_read, cost_usd, priced) "
        "VALUES ('a','claude','s1',?,'2026-06-10T10:00:00Z','claude-opus-4-8',"
        "1500000, 500000, 250000, 250000, 12.5, 1)",
        (r"C:\projects\samsung\tokenomy",),
    )
    conn.commit()
    r = client.get("/")
    assert r.status_code == 200
    assert "폴더 사용량" in r.text                      # 새 타이틀
    assert "이 기기 · 정가 환산 추정" in r.text          # 로컬·추정 부제
    assert "통합 프로젝트별" not in r.text               # 옛 타이틀 폐지
    assert "sort=cache" not in r.text                   # 비용/세션/캐시 토글 제거
    assert "2.5M" in r.text                             # 토큰 4종 합 약식(1.5M+0.5M+0.25M+0.25M)
    assert "tokenomy" in r.text                         # 폴더 basename
    assert r"C:\projects\samsung\tokenomy" in r.text    # 전체 경로 hover(title)


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
    for section in ("기간별 사용량", "통합 추세", "통합 효율 코치", "폴더 사용량", "세션별 사용량"):
        assert section in r.text
    assert "이번 달 총지출" not in r.text          # 총지출 단독 카드 폐지(ADR 0017)
    assert "AI별 사용 현황" not in r.text          # 번다운 카드 섹션 제거
    assert "공개 API 단가 기준 추정" in r.text   # 기간별 카드 로컬 디스클레이머
    assert "proj" in r.text                       # 프로젝트별 행


def test_session_usage_card_renders_tokens_model_duration(tmp_path, monkeypatch):
    """세션별 사용량 카드(ADR 0020) — 총 토큰 humanize · 주사용모델(+비중) · 세션 길이 · 메시지 콤마."""
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    # opus 900k tokens(주사용) + haiku 100k → 총 1.0M, opus 90%. 01:00→11:25 UTC = 10h25m.
    conn.execute(
        "INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,input_tokens,cost_usd,priced) "
        "VALUES ('a','claude','s1','proj','2026-06-10T01:00:00Z','claude-opus-4-8',900000,12.5,1)"
    )
    conn.execute(
        "INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,input_tokens,cost_usd,priced) "
        "VALUES ('b','claude','s1','proj','2026-06-10T11:25:00Z','claude-haiku-4-5',100000,1.0,1)"
    )
    conn.execute(
        "INSERT INTO sessions (session_id, summary, user_turns) VALUES ('s1','토큰 집계 구현',3042)"
    )
    conn.commit()
    r = client.get("/")
    assert r.status_code == 200
    assert "세션별 사용량" in r.text                   # 카드 타이틀(복기 후신)
    assert "이 기기 · 정가 환산 추정" in r.text         # 폴더 사용량과 동일 출처축 부제
    assert "1.0M" in r.text                            # 총 토큰 humanize
    assert "Opus 4.8(90%)" in r.text                   # 주사용모델 humanize + 비중
    assert "10시간 25분" in r.text                      # 세션 길이(첫~마지막 벽시계)
    assert "3,042" in r.text                           # 메시지 천 단위 콤마


def _seed_glance_history(conn, provider, day_used):
    """USD 버킷 표본을 2026-06-<day> 12:00 KST 스냅샷으로 적재(글랜스 렌더 스모크용)."""
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.official_parser import OfficialBucket
    for day, used in day_used:
        dt = datetime(2026, 6, day, 12, 0, tzinfo=KST)
        insert_official_buckets(
            conn, provider=provider, fetched_at=dt.isoformat(), created_at=dt.isoformat(),
            buckets=[OfficialBucket(
                bucket_key="monthly", raw_key="spend", bucket_kind="monthly_limit",
                label="월 사용 한도", native_unit="usd",
                used_native=used, limit_native=100.0, remaining_native=100.0 - used,
                used_usd=used, limit_usd=100.0, remaining_usd=100.0 - used,
                utilization=used, resets_at=None)])
    conn.commit()


def test_official_section_renders_period_glance(tmp_path, monkeypatch):
    """_official_section.html이 공식 기간 소비 글랜스 줄을 Jinja 오류 없이 렌더한다(ADR 0011)."""
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.official_parser import OfficialBucket
    from tokenomy.web.views import official_section_context

    _, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    _seed_glance_history(conn, "claude", [(9, 20.0), (10, 30.0)])   # 어제·오늘(NOW=6/10)
    ctx = official_section_context(conn, {"tracked_providers": ["claude"]},
                                   now_kst=datetime(2026, 6, 10, 15, 0, tzinfo=KST))
    html = app_module.templates.env.get_template("_official_section.html").render(ctx)
    assert "오늘" in html and "이번주" in html
    assert "공식 · 계정 전체" in html
    assert "$10.0" in html        # 오늘 = 30-20


def test_official_section_renders_period_card(tmp_path, monkeypatch):
    """_official_section.html이 기간별 사용량 카드(오늘/이번주/이번달 + 복사 + 숨김 문구)를 렌더한다(ADR 0017)."""
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.web.views import official_section_context

    _, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    _seed_glance_history(conn, "claude", [(9, 20.0), (10, 30.0)])   # 오늘=10, 이번달=30
    ctx = official_section_context(conn, {"tracked_providers": ["claude"],
                                          "account_mode": "enterprise"},
                                   now_kst=datetime(2026, 6, 10, 15, 0, tzinfo=KST))
    html = app_module.templates.env.get_template("_official_section.html").render(ctx)
    assert "period-card" in html and "📋 복사" in html
    assert 'class="share-src"' in html
    assert "AI 사용량 (2026-06-10, KST)" in html   # 숨김 복사 문구(공식)
    assert "기간별 사용량" in html and "이번달" in html


def test_period_card_template_shows_baseline_and_omits_when_none():
    """_period_card.html — 기준값(prev_usd) 있으면 '· 어제 $X' 병기, None이면 그 칸은 꼬리 생략(ADR 0018)."""
    ctx = {
        "mode": "local", "source_label": "이 기기 · 추정", "disclaimer": "추정",
        "has_data": True, "partial_warning": False, "share_text": None,
        "date_label": "2026-06-26",
        "periods": [
            {"key": "오늘", "usd": 8.0, "state": "complete",
             "pace": {"dir": "up", "pct": 100}, "prev_usd": 4.0, "prev_label": "어제"},
            {"key": "이번주", "usd": 24.0, "state": "complete",
             "pace": {"dir": "down", "pct": 20}, "prev_usd": 30.0, "prev_label": "지난주"},
            {"key": "이번달", "usd": 80.0, "state": "complete",
             "pace": None, "prev_usd": None, "prev_label": "지난달"},
        ],
    }
    html = app_module.templates.env.get_template("_period_card.html").render(period_card=ctx)
    assert "어제 $4.0" in html and "지난주 $30.0" in html   # 기준값 화면 병기
    assert "▲" in html and "100%" in html                    # 페이스 ▲ + %
    assert "지난달" not in html                                # prev_usd None → 꼬리·라벨 생략


def test_official_section_no_share_card_without_pool(tmp_path, monkeypatch):
    """rate-window-only(개인 구독제)면 공유 카드 없음(share None)."""
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.official_parser import OfficialBucket
    from tokenomy.web.views import official_section_context

    _, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    insert_official_buckets(
        conn, provider="claude", fetched_at="2026-06-10T12:00:00+09:00",
        created_at="2026-06-10T12:00:00+09:00",
        buckets=[OfficialBucket(
            bucket_key="rate_window", raw_key="five_hour", bucket_kind="rate_window",
            label="5시간", native_unit="percent", used_native=50.0, limit_native=100.0,
            remaining_native=50.0, used_usd=None, limit_usd=None, remaining_usd=None,
            utilization=50.0, resets_at=None)])
    ctx = official_section_context(conn, {"tracked_providers": ["claude"]},
                                   now_kst=datetime(2026, 6, 10, 15, 0, tzinfo=KST))
    html = app_module.templates.env.get_template("_official_section.html").render(ctx)
    assert "share-card" not in html


def test_mini_section_renders_share_source(tmp_path, monkeypatch):
    """_mini_section.html이 숨김 공유 소스(.share-src)를 렌더한다 — 헤더 📋가 읽는다."""
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.web.views import mini_view_context

    _, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    _seed_glance_history(conn, "claude", [(9, 20.0), (10, 30.0)])
    ctx = mini_view_context(conn, {"tracked_providers": ["claude"]},
                            now_kst=datetime(2026, 6, 10, 15, 0, tzinfo=KST))
    html = app_module.templates.env.get_template("_mini_section.html").render(ctx)
    assert 'class="share-src"' in html
    assert "AI 사용량 (2026-06-10, KST)" in html


def test_mini_section_renders_period_glance(tmp_path, monkeypatch):
    """_mini_section.html이 글랜스 강조 줄을 Jinja 오류 없이 렌더한다(ADR 0011)."""
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.official_parser import OfficialBucket
    from tokenomy.web.views import mini_view_context

    _, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    _seed_glance_history(conn, "claude", [(9, 20.0), (10, 30.0)])
    ctx = mini_view_context(conn, {"tracked_providers": ["claude"]},
                            now_kst=datetime(2026, 6, 10, 15, 0, tzinfo=KST))
    html = app_module.templates.env.get_template("_mini_section.html").render(ctx)
    assert "오늘" in html and "$10.0" in html
    assert "이번주" in html        # 미니 글랜스 라벨 "주"→"이번주"(정본 통일, ADR 0008 개정)


def test_mini_gauge_caption_moves_to_bar_tooltip(tmp_path, monkeypatch):
    """미니 게이지의 $used/$limit은 별도 캡션 줄이 아니라 bar 툴팁(title)으로(ADR 0008 개정).

    한 줄 인라인 압축의 핵심 — 캡션 줄을 빼고 hover로 절대액을 확인한다.
    """
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.web.views import mini_view_context

    _, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    _seed_glance_history(conn, "claude", [(10, 30.0)])   # 월 사용 한도 30/100 → caption "$30.0 / $100"
    ctx = mini_view_context(conn, {"tracked_providers": ["claude"]},
                            now_kst=datetime(2026, 6, 10, 15, 0, tzinfo=KST))
    html = app_module.templates.env.get_template("_mini_section.html").render(ctx)
    assert 'title="$30.0 / $100"' in html        # $used/$limit → bar 툴팁
    assert "mini-gauge-caption" not in html        # 별도 캡션 줄 제거(한 줄 압축)


def test_mini_gauge_shows_reset_countdown(tmp_path, monkeypatch):
    """미니 rate_window 게이지는 % 뒤에 거친 리셋 카운트다운(· N단위, muted)을 인라인으로 보여준다."""
    from datetime import datetime, timedelta
    from tokenomy.clock import KST
    from tokenomy.db import insert_official_buckets
    from tokenomy.official_parser import OfficialBucket
    from tokenomy.web.views import mini_view_context

    _, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    now = datetime(2026, 6, 10, 12, 0, tzinfo=KST)
    reset = now + timedelta(hours=2, minutes=35)
    insert_official_buckets(
        conn, provider="claude", fetched_at=now.isoformat(),
        buckets=[OfficialBucket(
            bucket_key="rate_window", raw_key="five_hour", bucket_kind="rate_window",
            label="5시간", native_unit="percent",
            used_native=None, limit_native=None, remaining_native=None,
            used_usd=None, limit_usd=None, remaining_usd=None,
            utilization=42.0, resets_at=reset)],
        created_at=now.isoformat())
    ctx = mini_view_context(conn, {"tracked_providers": ["claude"]}, now_kst=now)
    html = app_module.templates.env.get_template("_mini_section.html").render(ctx)
    assert "mini-reset" in html
    assert "· 2시간" in html        # 2h35m → 최대 단위 1개


def test_mini_freshness_uses_updated_variant(tmp_path, monkeypatch):
    """미니 갱신 신선도는 '갱신됨' 변형(data-rel-style=updated)으로 렌더된다.

    큰 창은 '갱신:' 접두를 붙이지만(rel-time 기본형), 미니는 접두 없이 자체 설명형
    'N분전 갱신됨'을 쓴다 — 공유 rel-time.js를 data 속성으로 분기(사이드바·큰 창 불변).
    """
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.web.views import mini_view_context

    _, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    _seed_glance_history(conn, "claude", [(10, 30.0)])
    ctx = mini_view_context(conn, {"tracked_providers": ["claude"]},
                            now_kst=datetime(2026, 6, 10, 15, 0, tzinfo=KST))
    html = app_module.templates.env.get_template("_mini_section.html").render(ctx)
    assert 'class="mini-fresh rel-time"' in html
    assert 'data-rel-style="updated"' in html


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


def test_trend_includes_gemini_when_active_with_data(tmp_path, monkeypatch):
    """gemini가 활성 AI + 로컬 데이터를 가지면 대시보드 추세 밴드에 포함된다.

    Fix 1 회귀 가드: _PROVIDER_STYLE에 gemini 엔트리가 없으면 trend_providers 파생
    (`[p for p in _PROVIDER_STYLE if p in active and _provider_has_data(...)]`)에서
    구조적으로 빠져 밴드 자체가 생기지 않는다(회색 폴백이 아니라 아예 누락).
    """
    from tokenomy.web.views import overview_context
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"tracked_providers": ["claude", "gemini"]}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))

    conn = connect(":memory:")
    conn.execute(
        "INSERT INTO messages (dedup_key, provider, session_id, ts, cost_usd, priced) "
        "VALUES ('g1','gemini','s1','2026-06-10T10:00:00Z', 3.0, 1)"
    )
    conn.commit()
    ctx = overview_context(conn, "cost", now_kst=datetime(2026, 6, 10, 12, tzinfo=KST))
    labels = {s["label"] for s in ctx["trend_series"]}
    colors = {s["color"] for s in ctx["trend_series"]}
    assert "Gemini" in labels
    assert "#4285f4" in colors


def test_official_cards_excludes_gemini(tmp_path, monkeypatch):
    """gemini는 공식 quota 미지원(OFFICIAL_PROVIDERS 밖)이라 활성이어도 공식 카드가 없다.

    Fix 2 회귀 가드: claude는 대조군으로 카드가 생성됨을 함께 확인해 게이트가
    OFFICIAL_PROVIDERS만 걸러내고 다른 활성 provider는 건드리지 않음을 검증한다.
    """
    from tokenomy.web.views import official_cards
    config = {"tracked_providers": ["claude", "gemini"]}
    conn = connect(":memory:")
    cards = official_cards(conn, config, now_kst=datetime(2026, 6, 10, 12, tzinfo=KST))
    providers = {c["provider"] for c in cards}
    assert "gemini" not in providers
    assert "claude" in providers


def test_overview_context_includes_forecast(tmp_path, monkeypatch):
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.db import connect, insert_official_buckets
    from tokenomy.official_parser import OfficialBucket
    from tokenomy.web.views import overview_context

    # 활성 AI(ADR 0005) 고정 — forecast 풀은 활성 provider 기준이므로 결정적으로 만든다.
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"tracked_providers": ["claude", "codex"]}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))

    conn = connect(":memory:")
    insert_official_buckets(
        conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
        buckets=[OfficialBucket(
            bucket_key="monthly", raw_key="spend", bucket_kind="monthly_limit",
            label="m", native_unit="usd", used_native=40.0, limit_native=200.0,
            remaining_native=160.0, used_usd=40.0, limit_usd=200.0, remaining_usd=160.0,
            utilization=20.0, resets_at=None)],
        created_at="2026-06-10T09:00:00+09:00")
    ctx = overview_context(conn, "cost", now_kst=datetime(2026, 6, 10, 12, tzinfo=KST))
    assert ctx["forecast"] is not None
    assert ctx["forecast"]["level"] in {"surplus", "shortfall", "exhausted", "insufficient"}
    assert ctx["forecast_limit"] == 200.0
    assert "forecast_line" in ctx


# ── A군 모드 게이트(ADR 0015 2단계) ────────────────────────────────────────────

def _seed_monthly_pool(conn, provider, used, limit, *, fetched="2026-06-10T09:00:00+09:00"):
    from tokenomy.official_parser import OfficialBucket
    insert_official_buckets(
        conn, provider=provider, fetched_at=fetched, created_at=fetched,
        buckets=[OfficialBucket(
            bucket_key="monthly", raw_key="spend", bucket_kind="monthly_limit",
            label="m", native_unit="usd", used_native=used, limit_native=limit,
            remaining_native=limit - used, used_usd=used, limit_usd=limit,
            remaining_usd=limit - used, utilization=used / limit * 100, resets_at=None)])


def _ovw_cfg(tmp_path, monkeypatch, mode=None):
    cfg = tmp_path / "cfg.json"
    body = {"tracked_providers": ["claude", "codex"]}
    if mode is not None:
        body["account_mode"] = mode
    cfg.write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))


def test_overview_enterprise_headline_official(tmp_path, monkeypatch):
    # 엔터프라이즈 + USD 풀 → 헤드라인=공식 pool used·히어로 표시·추세 오버레이 존재.
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.web.views import overview_context
    _ovw_cfg(tmp_path, monkeypatch, "enterprise")
    conn = connect(":memory:")
    _seed_monthly_pool(conn, "claude", 40.0, 200.0)
    ctx = overview_context(conn, "cost", now_kst=datetime(2026, 6, 10, 12, tzinfo=KST))
    assert ctx["headline_official"] is True
    assert ctx["headline_usd"] == 40.0          # 공식 pool used(로컬 추정 아님)
    assert ctx["forecast"] is not None
    assert ctx["forecast_limit"] == 200.0


def test_overview_subscription_suppresses_hero_uses_local_headline(tmp_path, monkeypatch):
    # 개인구독제: USD 버킷이 있어도(혼합 엣지) A군은 로컬 — 히어로 숨김·헤드라인 로컬·추세 오버레이 없음.
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.web.views import overview_context
    _ovw_cfg(tmp_path, monkeypatch, "subscription")
    conn = connect(":memory:")
    _seed_monthly_pool(conn, "claude", 40.0, 200.0)
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,cost_usd,priced) "
                 "VALUES ('a','claude','s','2026-06-05T10:00:00Z',7.5,1)")
    conn.commit()
    ctx = overview_context(conn, "cost", now_kst=datetime(2026, 6, 10, 12, tzinfo=KST))
    assert ctx["forecast"] is None                # 전망 히어로 숨김
    assert ctx["headline_official"] is False
    assert ctx["headline_usd"] == 7.5             # 로컬 이번 달 총지출(추정)
    assert ctx["forecast_limit"] is None
    assert ctx["forecast_line"] is None
    assert ctx["forecast_actual"] is None


def test_overview_unset_mode_with_pool_treated_official(tmp_path, monkeypatch):
    # 미설정(시드 전)이라도 USD 풀이 있으면 A군은 공식으로 — 곧 enterprise로 시드될 상태.
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.web.views import overview_context
    _ovw_cfg(tmp_path, monkeypatch, None)
    conn = connect(":memory:")
    _seed_monthly_pool(conn, "claude", 40.0, 200.0)
    ctx = overview_context(conn, "cost", now_kst=datetime(2026, 6, 10, 12, tzinfo=KST))
    assert ctx["headline_official"] is True
    assert ctx["forecast"] is not None


def test_dashboard_official_headline_renders_zone_chip(tmp_path, monkeypatch):
    # 엔터프라이즈 + USD 풀 → 총지출 카드에 "공식 · 계정 전체" 존 칩 렌더(템플릿 분기 스모크).
    client, conn_factory = _client(tmp_path, monkeypatch)
    (tmp_path / "cfg.json").write_text(json.dumps(
        {"tracked_providers": ["claude", "codex"], "account_mode": "enterprise"}),
        encoding="utf-8")
    _seed_monthly_pool(conn_factory(), "claude", 40.0, 200.0)
    r = client.get("/")
    assert r.status_code == 200
    assert "공식 · 계정 전체" in r.text


# ── 개인구독 레이아웃(ADR 0015 5단계) ──────────────────────────────────────────

def test_dashboard_local_headline_renders_zone_chip(tmp_path, monkeypatch):
    # 로컬 헤드라인(개인구독) → 총지출 "이 기기 · 추정" 칩(D7 로컬 대칭). "공식 · 계정 전체"는 official 섹션 1회만.
    client, conn_factory = _client(tmp_path, monkeypatch)
    (tmp_path / "cfg.json").write_text(json.dumps(
        {"tracked_providers": ["claude"], "account_mode": "subscription"}),
        encoding="utf-8")
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,cost_usd,priced) "
                 "VALUES ('a','claude','s','2026-06-05T10:00:00Z',7.5,1)")
    conn.commit()
    r = client.get("/")
    assert r.status_code == 200
    assert "이 기기 · 추정" in r.text                 # 총지출 로컬 존 칩(D7)
    assert r.text.count("공식 · 계정 전체") == 1      # official 섹션 존 헤더만(총지출엔 없음)


def test_official_section_subscription_throttle_framing(tmp_path, monkeypatch):
    # 개인구독제: rate-window 게이지를 '이용 한도(스로틀)'로 프레이밍(D4·유일 공식 신호).
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.official_parser import OfficialBucket
    from tokenomy.web.views import official_section_context
    _, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    insert_official_buckets(
        conn, provider="claude", fetched_at="2026-06-10T12:00:00+09:00",
        created_at="2026-06-10T12:00:00+09:00",
        buckets=[OfficialBucket(
            bucket_key="rate_window", raw_key="five_hour", bucket_kind="rate_window",
            label="5시간 한도", native_unit="percent", used_native=50.0, limit_native=100.0,
            remaining_native=50.0, used_usd=None, limit_usd=None, remaining_usd=None,
            utilization=50.0, resets_at=None)])
    now = datetime(2026, 6, 10, 15, 0, tzinfo=KST)
    sub = official_section_context(conn, {"tracked_providers": ["claude"], "account_mode": "subscription"}, now_kst=now)
    html = app_module.templates.env.get_template("_official_section.html").render(sub)
    assert "스로틀" in html                  # rate-window를 이용 한도/스로틀로 프레이밍(D4)
    ent = official_section_context(conn, {"tracked_providers": ["claude"], "account_mode": "enterprise"}, now_kst=now)
    html2 = app_module.templates.env.get_template("_official_section.html").render(ent)
    assert "스로틀" not in html2             # 엔터프라이즈엔 스로틀 프레이밍 노트 없음


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
    assert "기간별 사용량" in r.text        # 총지출 카드 폐지·통합(ADR 0017)
    assert "이번 달 총지출" not in r.text
    assert "AI별 사용 현황" not in r.text   # 번다운 카드 섹션 제거
    assert 'class="sidebar"' in r.text
    assert 'href="/history"' in r.text
    assert 'href="/analysis"' in r.text   # 나브: 모델별→기준별


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
    for section in ("기간별 사용량", "통합 추세", "통합 효율 코치",
                    "폴더 사용량", "세션별 사용량"):
        assert section in r.text
    assert "이번 달 총지출" not in r.text   # 총지출 단독 카드 폐지(ADR 0017)
    assert "AI별 사용 현황" not in r.text   # 번다운 카드 섹션 제거
    assert "proj" in r.text



def test_dashboard_no_budget_banner(tmp_path, monkeypatch):
    """예산 온보딩 배너가 더 이상 없어야 함."""
    client, _ = _client(tmp_path, monkeypatch)
    html = client.get("/").text
    assert "예산을 설정하세요" not in html


def test_dashboard_shows_month_total(tmp_path, monkeypatch):
    """이번달 소비는 기간별 사용량 카드의 한 칸으로 렌더링되어야 함(ADR 0017)."""
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute(
        "INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,cost_usd,priced) "
        "VALUES ('a','claude','s1','proj','2026-06-10T10:00:00Z','claude-opus-4-8',5.0,1)"
    )
    conn.commit()
    html = client.get("/").text
    assert "기간별 사용량" in html and "이번달" in html


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
    assert "사용 이력(로컬)" in r.text
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
    assert "합계 $3.0" in r.text
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
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.freshness import record_ingest
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    record_ingest(conn, datetime(2026, 6, 10, 12, tzinfo=KST))   # 마지막 수집 시각 기록
    conn.execute(
        "INSERT INTO messages (dedup_key,provider,session_id,project,ts,cost_usd,priced) "
        "VALUES ('a','claude','s1','myproj','2026-06-10T01:00:00Z',1.0,1)"
    )
    conn.commit()
    r = client.get("/history?anchor=2026-06-10")
    assert "수집:" in r.text          # 사이드바 신선도 = 마지막 수집 시각(데이터 최신 메시지 ts 아님)


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
    assert "합계 $12.5" in r.text


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
    assert ">스킬</a>" in r.text                       # 기준 선택기 항목
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
    """overview_context가 official_cards(활성 카드 리스트)를 반환하고, 미참조 키
    (claude_official/codex_official)·옛 gauge/official_notes는 없어야 한다(ADR 0005)."""
    from tokenomy.web.views import overview_context
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    ctx = overview_context(conn, "cost")
    assert "official_cards" in ctx
    assert "claude_official" not in ctx and "codex_official" not in ctx   # 템플릿 미참조 → 제거
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

    spend(월 사용 한도 $0/$243) + cinder_cove(포함된 크레딧 $393.1/$1,000) 두 버킷이
    used/limit USD와 소진율로 나온다. 개인 구독 % 창이 아니라 달러 게이지 분기 검증.
    """
    from tokenomy.official_parser import parse_claude
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    _seed_official(conn, "claude", "claude_enterprise_real.json", parse_claude)
    r = client.get("/")
    assert r.status_code == 200
    # cinder_cove 이벤트 크레딧(달러 본체) — 코드네임 비의존 라벨 + USD 게이지
    assert "이벤트" in r.text
    assert "$393.1" in r.text and "1,000" in r.text
    assert "39%" in r.text                       # 게이지 utilization 표시
    assert "만료 2026-09-10" in r.text            # 만료일은 라벨이 아니라 sub로 이동
    # spend 월간 ($0/$243) — used 0도 게이지로 렌더
    assert "월간" in r.text and "243" in r.text


def test_official_history_route_renders_with_depletion_pool(tmp_path, monkeypatch):
    """소진형 풀이 있으면 /official-history가 200 + 제목 렌더(ADR 0010)."""
    from tokenomy.official_parser import parse_claude
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    _seed_official(conn, "claude", "claude_enterprise_real.json", parse_claude)
    r = client.get("/official-history")
    assert r.status_code == 200
    assert "사용 이력(공식)" in r.text
    assert 'href="/official-history"' in client.get("/").text   # 소진형 풀 → 내비 노출


def test_official_history_subscription_only_empty_and_nav_hidden(tmp_path, monkeypatch):
    """구독제-only(rate-window)는 소진형 풀이 없어 빈 상태 + 내비 링크 숨김(ADR 0010)."""
    from tokenomy.official_parser import parse_claude
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    _seed_official(conn, "claude", "claude_personal.json", parse_claude)
    r = client.get("/official-history")
    assert r.status_code == 200
    assert "표시할 이력이 없습니다" in r.text                     # 페이지 빈 상태
    assert 'href="/official-history"' not in client.get("/").text  # 내비 숨김


def test_official_history_drilldown_reconstructs_daily_number(tmp_path, monkeypatch):
    """날짜 행 펼침 = 그 일 소비를 만든 스냅샷 재구성(ADR 0010 드릴다운).

    6/10 첫 표본($100, 추적 시작) + 6/11 표본($130) → 6/11 일 소비 $30이
    '직전 기준 $100 + 증가분 +$30'으로 분해돼 렌더된다.
    """
    from tokenomy.official_parser import OfficialBucket
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()

    def _snap(ts, used):
        b = OfficialBucket(
            bucket_key="monthly", raw_key="spend", bucket_kind="monthly_limit",
            label="spend", native_unit="usd",
            used_native=used, limit_native=243.0, remaining_native=243.0 - used,
            used_usd=used, limit_usd=243.0, remaining_usd=243.0 - used,
            utilization=used / 243.0, resets_at=None)
        insert_official_buckets(conn, provider="claude", fetched_at=ts,
                                buckets=[b], created_at=ts)

    _snap("2026-06-10T09:00:00+09:00", 100.0)
    _snap("2026-06-11T09:00:00+09:00", 130.0)
    r = client.get("/official-history?period=month&anchor=2026-06-15")
    assert r.status_code == 200
    assert 'data-detail="1"' in r.text       # 펼침 가능한 날 행
    assert "추적 시작" in r.text              # 첫날(6/10) = first_ever
    assert "직전 기준" in r.text              # 6/11 baseline 표시
    assert "+$30.0" in r.text               # 6/11 증가분(130-100)
    assert "위 증가분의 합" in r.text         # 재구성 합계 푸터


def test_official_history_drilldown_multi_provider_labels(tmp_path, monkeypatch):
    """활성 AI 둘(사내망 Claude+Codex)이면 드릴다운이 provider별로 분해·라벨링된다(ADR 0005/0010)."""
    from tokenomy.official_parser import OfficialBucket
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()

    def _snap(provider, kind, raw, ts, used, limit):
        b = OfficialBucket(
            bucket_key="monthly", raw_key=raw, bucket_kind=kind, label=raw, native_unit="usd",
            used_native=used, limit_native=limit, remaining_native=limit - used,
            used_usd=used, limit_usd=limit, remaining_usd=limit - used,
            utilization=used / limit, resets_at=None)
        insert_official_buckets(conn, provider=provider, fetched_at=ts, buckets=[b], created_at=ts)

    _snap("claude", "monthly_limit", "spend", "2026-06-10T09:00:00+09:00", 100.0, 243.0)
    _snap("claude", "monthly_limit", "spend", "2026-06-11T09:00:00+09:00", 130.0, 243.0)
    _snap("codex", "codex_monthly", "individual_limit", "2026-06-10T09:00:00+09:00", 20.0, 235.0)
    _snap("codex", "codex_monthly", "individual_limit", "2026-06-11T09:00:00+09:00", 35.0, 235.0)
    r = client.get("/official-history?period=month&anchor=2026-06-15")
    assert r.status_code == 200
    # 6/11 펼침 블록에 두 provider 라벨 + per-provider 합이 분해돼야 한다.
    assert ">Claude</div>" in r.text and ">Codex</div>" in r.text
    assert "+$30.0" in r.text and "+$15.0" in r.text   # claude 130-100, codex 35-20
    assert "위 증가분의 합" in r.text


def test_official_history_context_hourly_shape_and_gap(tmp_path, monkeypatch):
    """period=day → 시간대별(24행) + 갭 시각 covered=False + 누적선 리셋-세그먼트(갭=null)·한도선 제거(ADR 0019)."""
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.official_parser import OfficialBucket
    from tokenomy.web.views import official_history_context
    cfg = tmp_path / "c.json"
    cfg.write_text('{"tracked_providers": ["claude"]}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    monkeypatch.setenv("TOKENOMY_SKIP_OFFICIAL_FETCH", "1")
    monkeypatch.setenv("TOKENOMY_SKIP_UPDATE_CHECK", "1")
    conn = connect(":memory:")

    def _snap(ts, used):
        b = OfficialBucket(
            bucket_key="monthly", raw_key="spend", bucket_kind="monthly_limit", label="spend",
            native_unit="usd", used_native=used, limit_native=243.0, remaining_native=243.0 - used,
            used_usd=used, limit_usd=243.0, remaining_usd=243.0 - used,
            utilization=used / 243.0, resets_at=None)
        insert_official_buckets(conn, provider="claude", fetched_at=ts, buckets=[b], created_at=ts)

    _snap("2026-06-03T09:00:00+09:00", 100.0)   # 이 월 첫 표본(추적 시작)
    _snap("2026-06-03T10:00:00+09:00", 112.0)   # 10시 +12
    _snap("2026-06-03T14:00:00+09:00", 130.0)   # 11~13시 갭 → 14시에 lump +18
    ctx = official_history_context(conn, datetime(2026, 6, 3, tzinfo=KST), period="day",
                                   now_kst=datetime(2026, 6, 3, 23, 0, tzinfo=KST))
    assert ctx["is_hourly"] is True
    assert len(ctx["chart_labels"]) == 24 and len(ctx["table"]) == 24
    by_h = {r["hour"]: r for r in ctx["table"]}
    assert by_h[9]["used_usd"] == 100.0
    assert by_h[10]["used_usd"] == 12.0
    assert by_h[14]["used_usd"] == 18.0                 # 갭 lump(11~14시 소비가 14시에)
    assert by_h[11]["covered"] is False and by_h[11]["used_usd"] is None
    # 누적선: 한 리셋-세그먼트, 관측 시각=절대 월누적, 갭 시각=null(점선 브리지)
    assert len(ctx["cum_segments"]) == 1
    seg = ctx["cum_segments"][0]
    assert seg[9] == 100.0 and seg[10] == 112.0 and seg[14] == 130.0
    assert seg[11] is None and seg[12] is None and seg[13] is None
    assert ctx["chart_limit"] is None                   # 일 뷰 한도선 제거
    assert ctx["pool_limit"] == 243.0                   # 표 잔여용으로는 유지


def _oh_ctx_conn(tmp_path, monkeypatch):
    """context 레벨(사용 이력 공식) 테스트용 격리 conn — tracked=claude, 네트워크 skip."""
    cfg = tmp_path / "c.json"
    cfg.write_text('{"tracked_providers": ["claude"]}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    monkeypatch.setenv("TOKENOMY_SKIP_OFFICIAL_FETCH", "1")
    monkeypatch.setenv("TOKENOMY_SKIP_UPDATE_CHECK", "1")
    return connect(":memory:")


def test_official_history_month_table_hides_future_days(tmp_path, monkeypatch):
    """월 뷰 표는 오늘(KST)까지만 — 미래 날짜는 수집 공백이 아니라 미관측 대상(행 없음).

    차트 x축은 월 전체를 유지한다(축은 라벨이지 관측 주장이 아님 — CONTEXT.md).
    """
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.web.views import official_history_context
    conn = _oh_ctx_conn(tmp_path, monkeypatch)
    _seed_oh_snap(conn, "claude", "2026-06-10T09:00:00+09:00", 100.0)
    _seed_oh_snap(conn, "claude", "2026-06-11T09:00:00+09:00", 130.0)
    ctx = official_history_context(conn, datetime(2026, 6, 15, tzinfo=KST), period="month",
                                   now_kst=datetime(2026, 6, 15, 12, 0, tzinfo=KST))
    assert len(ctx["table"]) == 15                       # 6/1~6/15 — 오늘 행 포함, 내일부터 숨김
    assert ctx["table"][-1]["ymd"] == "2026-06-15"
    assert len(ctx["chart_labels"]) == 30                # 차트 축은 6월 전체 유지
    assert all(len(s["data"]) == 30 for s in ctx["bar_series"])


def test_official_history_day_table_hides_future_hours_today(tmp_path, monkeypatch):
    """오늘의 일 뷰 표는 현재 시간대까지만(부분 관측 포함) — 이후 시간대는 행 없음.

    차트는 24칸 유지. 과거 날짜의 일 뷰는 24행 전부(기존 테스트가 커버).
    """
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.web.views import official_history_context
    conn = _oh_ctx_conn(tmp_path, monkeypatch)
    _seed_oh_snap(conn, "claude", "2026-06-15T09:00:00+09:00", 100.0)
    ctx = official_history_context(conn, datetime(2026, 6, 15, tzinfo=KST), period="day",
                                   now_kst=datetime(2026, 6, 15, 14, 30, tzinfo=KST))
    assert ctx["is_hourly"] is True
    assert len(ctx["table"]) == 15                       # 0~14시 — 현재(부분) 시간대 포함
    assert ctx["table"][-1]["hour"] == 14
    assert len(ctx["chart_labels"]) == 24                # 차트 축은 24칸 유지


def test_official_history_custom_range_hides_future_days(tmp_path, monkeypatch):
    """사용자 지정 구간이 미래를 포함해도 같은 규칙 — 표는 오늘까지, 차트 축은 구간 전체."""
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.web.views import official_history_context
    conn = _oh_ctx_conn(tmp_path, monkeypatch)
    _seed_oh_snap(conn, "claude", "2026-06-10T09:00:00+09:00", 100.0)
    ctx = official_history_context(conn, datetime(2026, 6, 15, tzinfo=KST), period="month",
                                   start="2026-06-10", end="2026-06-20",
                                   now_kst=datetime(2026, 6, 15, 12, 0, tzinfo=KST))
    assert ctx["custom"] is True
    assert len(ctx["table"]) == 6                        # 6/10~6/15 — 명시 조회여도 미래는 숨김
    assert ctx["table"][-1]["ymd"] == "2026-06-15"
    assert len(ctx["chart_labels"]) == 11                # 차트 축은 6/10~6/20 구간 전체


def _seed_oh_snap(conn, provider, ts, used, limit=243.0,
                  kind="monthly_limit", raw="spend"):
    """사용 이력(공식) 라우트 테스트용 단일 소진형 스냅샷 시드."""
    from tokenomy.official_parser import OfficialBucket
    b = OfficialBucket(
        bucket_key="monthly", raw_key=raw, bucket_kind=kind, label=raw, native_unit="usd",
        used_native=used, limit_native=limit, remaining_native=limit - used,
        used_usd=used, limit_usd=limit, remaining_usd=limit - used,
        utilization=used / limit, resets_at=None)
    insert_official_buckets(conn, provider=provider, fetched_at=ts, buckets=[b], created_at=ts)


def test_official_history_table_sorted_ascending(tmp_path, monkeypatch):
    """표는 날짜 오름차순(과거 위 → 최신 아래) — 옛 |reverse 내림차순 폐지."""
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    _seed_oh_snap(conn, "claude", "2026-06-10T09:00:00+09:00", 100.0)
    _seed_oh_snap(conn, "claude", "2026-06-11T09:00:00+09:00", 130.0)
    r = client.get("/official-history?period=month&anchor=2026-06-15")
    assert r.status_code == 200
    assert r.text.index("2026-06-10") < r.text.index("2026-06-11")


def test_official_history_day_view_renders_hourly(tmp_path, monkeypatch):
    """period=day → 시간대별 표(N시 라벨) + '일' 토글 + 갭 시간 '수집 공백' + 한도선 생략(ADR 0019)."""
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    _seed_oh_snap(conn, "claude", "2026-06-03T09:00:00+09:00", 100.0)
    _seed_oh_snap(conn, "claude", "2026-06-03T14:00:00+09:00", 130.0)   # 10~13시 갭
    r = client.get("/official-history?period=day&anchor=2026-06-03")
    assert r.status_code == 200
    assert "09시" in r.text and "14시" in r.text          # 시간대 라벨
    assert "수집 공백" in r.text                            # 갭 시간대
    assert ">일</a>" in r.text                              # 일 토글 항목
    assert "const ohLimit = null" in r.text                 # 일 뷰 한도선 생략
    assert "const ohZero = false" in r.text                 # 자동 줌(beginAtZero=false)
    # 대조: 월 뷰는 한도선 유지·beginAtZero
    m = client.get("/official-history?period=month&anchor=2026-06-03")
    assert "const ohLimit = null" not in m.text and "const ohZero = true" in m.text


def test_official_history_day_view_empty_day_notice(tmp_path, monkeypatch):
    """표본 없는 날을 일 뷰로 보면 화면을 숨기지 않고 '수집 공백' 안내(ADR 0019)."""
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    _seed_oh_snap(conn, "claude", "2026-06-03T09:00:00+09:00", 100.0)   # 6/3만 표본
    r = client.get("/official-history?period=day&anchor=2026-06-10")    # 6/10 표본 없음
    assert r.status_code == 200
    assert "이 날은" in r.text and "수집 공백" in r.text   # 빈 프레임 + 안내(숨김 아님)


def test_overview_enterprise_codex_credit_gauge_renders(tmp_path, monkeypatch):
    """엔터프라이즈 Codex(실측): 크레딧 한도가 credit_to_usd 환산 USD 게이지로 렌더되어야 한다.

    individual_limit used 1073.94 / limit 5875 credits → ×0.04 = $42.9576, 표시 $43.0 / $235 월간 게이지(ADR 0020 1자리).
    추정 주간 게이지는 제거됨(ADR 0012). 개인 구독 % 창이 아니라 USD 월간 분기 검증.
    """
    from tokenomy.official_parser import parse_codex
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    _seed_official(conn, "codex", "codex_enterprise_real.json", parse_codex)
    r = client.get("/")
    assert r.status_code == 200
    assert "월간" in r.text             # codex_monthly 버킷 라벨(공식 게이지)
    assert "$43.0" in r.text and "235" in r.text
    assert "크레딧 1,074 / 5,875" in r.text   # USD는 환산값 — 원본 크레딧 병기
    assert "이번 주" not in r.text     # 추정 주간 게이지 제거(ADR 0012)


def test_dashboard_disclaimer_names_surface_axis(tmp_path, monkeypatch):
    """로컬 사용량 단서가 기기 축뿐 아니라 표면 축(Code/Codex)도 말해야 한다(ADR 0009)."""
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,cost_usd,priced) "
                 "VALUES ('a','claude','s','2026-06-10T10:00:00Z',5.0,1)")
    conn.commit()
    html = client.get("/").text
    assert "이 기기의 Claude Code와 Codex만" in html
    assert "이 기기 데이터만" not in html


def test_analysis_disclaimer_names_surface_axis(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    html = client.get("/analysis").text
    assert "이 기기의 Claude Code와 Codex만" in html
    assert "이 기기 데이터만" not in html


def test_official_section_legend_official_account_wide(tmp_path, monkeypatch):
    """공식 섹션 범례=공식 출처·계정 전체(전 기기). 로컬 추정 단서는 제거(ADR 0015 D7/D8)."""
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"tracked_providers": ["claude"]}', encoding="utf-8")
    html = client.get("/official/section").text
    assert "계정 전체" in html                              # 존 레벨 공식 출처 라벨
    assert "이 기기 Claude Code와 Codex" not in html       # 로컬 추정 단서는 로컬 존으로 이전


def test_official_section_source_chip_zone_level_once(tmp_path, monkeypatch):
    """출처 칩 '공식 · 계정 전체'는 존(섹션) 1회 — provider 카드마다 반복 안 함(ADR 0015 D7)."""
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.web.views import official_section_context
    _, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    _seed_glance_history(conn, "claude", [(9, 20.0), (10, 30.0)])
    _seed_glance_history(conn, "codex", [(9, 10.0), (10, 15.0)])
    ctx = official_section_context(conn, {"tracked_providers": ["claude", "codex"]},
                                   now_kst=datetime(2026, 6, 10, 15, 0, tzinfo=KST))
    html = app_module.templates.env.get_template("_official_section.html").render(ctx)
    # AI별 사용량 존 헤더 1회 + 기간별 사용량 카드(공식) 출처 라벨 1회 = 2(ADR 0017로 두 존 카드).
    # provider 카드마다 반복하지 않는 D7 원칙은 유지(카드당 1회).
    assert html.count("공식 · 계정 전체") == 2


def test_official_section_no_official_clean_state_no_estimate(tmp_path, monkeypatch):
    """공식 미취득 카드는 로컬 추정$·스파크 없이 '공식 미취득' 깨끗한 상태(ADR 0015 D8)."""
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.web.views import official_section_context
    _, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,cost_usd,priced) "
                 "VALUES ('a','claude','s','2026-06-10T10:00:00Z',12.0,1)")
    conn.commit()
    ctx = official_section_context(conn, {"tracked_providers": ["claude"]},
                                   now_kst=datetime(2026, 6, 10, 15, 0, tzinfo=KST))
    html = app_module.templates.env.get_template("_official_section.html").render(ctx)
    assert "공식 사용량 미취득" in html      # 깨끗한 빈 상태 안내
    assert "로컬 추정" not in html           # 폴백 문구 제거
    assert "chip-est" not in html            # 추정 칩 제거
    assert "$12.0" not in html               # 로컬 추정$ 미표시


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


# ── 계정 형태 토글(ADR 0015 6단계) ─────────────────────────────────────────────

def test_settings_shows_account_mode_toggle(tmp_path, monkeypatch):
    """설정에 계정 형태(account_mode) 토글 — 자동/엔터프라이즈/개인 구독제."""
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'name="account_mode"' in r.text
    assert "엔터프라이즈" in r.text and "개인 구독제" in r.text


def test_settings_post_persists_account_mode(tmp_path, monkeypatch):
    """POST /settings가 account_mode를 저장한다(수동 토글이 자동 시드를 덮어씀, sticky)."""
    from tokenomy.config import account_mode, load_config
    client, _ = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings", data={"account_mode": "subscription", "track_claude": "on"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert account_mode(load_config()) == "subscription"


def test_settings_post_account_mode_auto_clears_for_reseed(tmp_path, monkeypatch):
    """'자동'(빈 값)이면 account_mode=None → 다음 공식 취득 때 재시드 가능."""
    from tokenomy.config import account_mode, load_config
    client, _ = _client_with_config(tmp_path, monkeypatch)
    client.post("/settings", data={"account_mode": "enterprise", "track_claude": "on"},
                follow_redirects=False)
    assert account_mode(load_config()) == "enterprise"   # 먼저 수동값이 박힘
    client.post("/settings", data={"account_mode": "", "track_claude": "on"},
                follow_redirects=False)
    assert account_mode(load_config()) is None           # 자동으로 되돌리면 비움(재시드 가능)


# ── 전망 설정: 소비속도 추정 기간(rate_window_weeks) UI ──────────────────────────

def test_settings_get_shows_rate_window(tmp_path, monkeypatch):
    """설정에 '전망' 카드 + 소비속도 추정 기간 필드(rate_window_weeks)가 렌더돼야 한다."""
    client, _ = _client_with_config(tmp_path, monkeypatch)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "전망" in r.text
    assert 'name="rate_window_weeks"' in r.text
    assert "소비속도 추정 기간" in r.text


def test_settings_post_persists_rate_window(tmp_path, monkeypatch):
    """POST /settings에서 rate_window_weeks를 forecast_settings에 영속해야 한다."""
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings", data={"rate_window_weeks": "4"},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["forecast_settings"]["rate_window_weeks"] == 4


def test_settings_post_clamps_rate_window(tmp_path, monkeypatch):
    """범위 밖 rate_window_weeks는 getter 클램프로 정규화(99→8)되어 저장된다(POST→getter 배선)."""
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings", data={"rate_window_weeks": "99"},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["forecast_settings"]["rate_window_weeks"] == 8


# ── Task 4 TDD: official_refresh ──────────────────────────────────────────────────

def test_official_refresh_manual_fetches_and_redirects_without_hx(tmp_path, monkeypatch):
    # HX 요청이 아니면(JS 미개입) 전체 리로드 폴백(303). 수동이라 manual=True 전달.
    client, cfg_path = _client_with_config(tmp_path, monkeypatch)
    cfg_path.write_text('{"tracked_providers": ["claude", "codex"]}', encoding="utf-8")
    calls = []
    monkeypatch.setattr("tokenomy.official_fetch.fetch_provider",
                        lambda p, **k: calls.append((p, k.get("manual"))))
    r = client.post("/official/refresh", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert {p for p, _ in calls} == {"claude", "codex"}
    assert all(manual is True for _, manual in calls)   # 수동 = throttle bypass


def test_official_refresh_scopes_single_provider(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr("tokenomy.official_fetch.fetch_provider",
                        lambda p, **k: calls.append(p))
    r = client.post("/official/refresh", data={"provider": "claude"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert calls == ["claude"]


def test_official_refresh_redirects_even_on_fetch_error(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"tracked_providers": ["claude"]}', encoding="utf-8")
    def boom(p, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr("tokenomy.official_fetch.fetch_provider", boom)
    r = client.post("/official/refresh", data={}, follow_redirects=False)
    assert r.status_code == 303   # 결과 무관 redirect(예외도 삼킴)


def test_official_refresh_hx_returns_section_partial(tmp_path, monkeypatch):
    """HX 요청이면 'AI별 사용량' 섹션 조각만 반환(부분교체)."""
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"tracked_providers": ["claude"]}', encoding="utf-8")
    monkeypatch.setattr("tokenomy.official_fetch.fetch_provider", lambda p, **k: None)
    r = client.post("/official/refresh", data={"provider": "claude"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<!doctype html>" not in r.text.lower()
    assert 'class="sidebar"' not in r.text
    assert "AI별 사용량" in r.text


def test_official_section_polls_with_auto_throttle(tmp_path, monkeypatch):
    """자동 폴링 라우트 GET /official/section → 섹션 조각 + manual=False(throttle 적용)."""
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"tracked_providers": ["claude"]}', encoding="utf-8")
    calls = []
    monkeypatch.setattr("tokenomy.official_fetch.fetch_provider",
                        lambda p, **k: calls.append(k.get("manual")))
    r = client.get("/official/section")
    assert r.status_code == 200
    assert "AI별 사용량" in r.text
    assert "<!doctype html>" not in r.text.lower()
    assert calls == [False]   # 자동 = throttle 적용


def test_overview_section_has_polling_trigger(tmp_path, monkeypatch):
    """대시보드 섹션이 load(起動 갱신) + every Nm(자동 폴링) 트리거를 가진다."""
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"tracked_providers": ["claude"], '
                   '"official_fetch": {"min_interval_minutes": 10}}', encoding="utf-8")
    r = client.get("/")
    assert 'hx-get="/official/section"' in r.text
    assert "load" in r.text
    assert "every 10m" in r.text


# ── Task 5 TDD: 설정 UI — 공식 자동 취득 토글 지속 ──────────────────────────────

def test_settings_shows_official_fetch_section(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/settings")
    assert r.status_code == 200
    # '사용하는 AI'+'자동 취득'을 '공식 사용량' 카드 하나로 병합(설정 UI 재구성)
    assert "공식 사용량" in r.text
    assert "자동 갱신 간격" in r.text
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


def test_settings_shows_background_poll_toggle_checked_by_default(tmp_path, monkeypatch):
    # 상주 백그라운드 폴 토글 — 기본 ON이라 체크된 상태로 렌더(ADR 0007)
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'name="background_poll"' in r.text
    assert 'name="background_poll" checked' in r.text


def test_settings_post_background_poll_off_when_unchecked(tmp_path, monkeypatch):
    # 체크박스 미체크(폼에 키 부재) → background_poll False 영속
    client, cfg_path = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings", data={"min_interval": "10", "credit_to_usd": "0.04"},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["official_fetch"]["background_poll"] is False


def test_settings_post_background_poll_on_when_checked(tmp_path, monkeypatch):
    client, cfg_path = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings", data={"min_interval": "10", "credit_to_usd": "0.04",
                                       "background_poll": "on"}, follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["official_fetch"]["background_poll"] is True
    assert saved["official_fetch"]["min_interval_minutes"] == 10


# ── Task 6 TDD: 대시보드 새로고침 버튼 + 취득 상태 표면 ──────────────────────────────

def test_overview_has_per_provider_refresh_buttons(tmp_path, monkeypatch):
    """새로고침은 provider별 카드 안에 — Claude/Codex는 API가 달라 각자 취득한다.

    tracked면 데이터가 없어도(폴백 카드) 카드가 떠서, 각 카드가 자기 provider를
    hidden 필드로 가진 /official/refresh 폼을 렌더해야 한다(한쪽만 재시도 가능)."""
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"tracked_providers": ["claude", "codex"]}', encoding="utf-8")
    r = client.get("/")
    assert r.status_code == 200
    assert 'hx-post="/official/refresh"' in r.text          # htmx 부분교체
    assert 'name="provider" value="claude"' in r.text
    assert 'name="provider" value="codex"' in r.text


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


# ── Commit 4(활성 AI): settings POST 동적 파싱 + 필터 UI 활성 파생 ────────────────

def test_settings_post_unchecking_all_persists_empty(tmp_path, monkeypatch):
    """track_* 전부 미체크 → 빈 집합 저장(Commit 1이 영속 보장 — 재시드 안 함)."""
    client, cfg_path = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings", data={"min_interval": "10", "credit_to_usd": "0.04"},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["tracked_providers"] == []


def test_settings_post_dynamic_collects_each_provider(tmp_path, monkeypatch):
    """폼 파싱이 PROVIDERS를 순회 — 3번째 AI 추가 대비(track_<new>도 수집, 하드코딩 없음)."""
    client, cfg_path = _client_with_config(tmp_path, monkeypatch)
    monkeypatch.setattr(app_module, "PROVIDERS", ("claude", "codex", "gemini"))
    r = client.post("/settings", data={"track_claude": "on", "track_gemini": "on",
                                       "min_interval": "10", "credit_to_usd": "0.04"},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["tracked_providers"] == ["claude", "gemini"]   # track_codex 미체크 → 제외


def test_history_provider_filter_hidden_when_single_active(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"tracked_providers": ["claude"]}', encoding="utf-8")
    r = client.get("/history")
    assert r.status_code == 200
    assert 'id="provider-filter"' not in r.text     # 활성 1개 → AI 필터 숨김


def test_history_provider_filter_shows_active_only(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"tracked_providers": ["claude", "codex"]}', encoding="utf-8")
    r = client.get("/history")
    assert 'id="provider-filter"' in r.text
    assert ">Claude<" in r.text and ">Codex<" in r.text


def test_analysis_provider_toggle_hidden_when_single_active(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"tracked_providers": ["claude"]}', encoding="utf-8")
    r = client.get("/analysis")
    assert r.status_code == 200
    assert "provider=codex" not in r.text           # 활성 1개 → provider 토글 없음


# ── Commit 5(활성 AI): settings 카드 분리 + 상태칩 제거 ───────────────────────────

def test_settings_splits_active_ai_and_official_cards(tmp_path, monkeypatch):
    """설정이 '활성 AI'(토글) / '공식 사용량'(간격) 두 카드로 분리되고, fetch 상태칩은 없다.

    상태(신선도·토큰만료 등)는 대시보드 'AI별 사용량' 카드가 담당 — 설정은 구성만.
    """
    client, fake_connect = _client(tmp_path, monkeypatch)   # 활성=둘 고정
    from tokenomy.db import upsert_fetch_state
    conn = fake_connect()
    upsert_fetch_state(conn, "codex", last_attempt_at="2026-06-10T09:00:00+09:00",
                       last_success_at=None, last_status="auth_error", last_error="HTTP 401")
    html = client.get("/settings").text
    assert "활성 AI" in html and "공식 사용량" in html        # 두 카드
    assert "토큰 만료" not in html and "갱신 안 함" not in html   # 상태칩 제거
    assert "자동 갱신 간격" in html and 'name="min_interval"' in html


def test_settings_toggles_reflect_active(tmp_path, monkeypatch):
    import re
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"tracked_providers": ["claude"]}', encoding="utf-8")
    html = client.get("/settings").text
    assert re.search(r'name="track_claude"[^>]*checked', html)        # 활성 → 체크
    assert not re.search(r'name="track_codex"[^>]*checked', html)     # 비활성 → 해제


# ── Commit 6(활성 AI): 라벨 적응 + 빈 상태 ───────────────────────────────────────

def test_dashboard_heading_uses_provider_name_when_single_active(tmp_path, monkeypatch):
    client, fake_connect = _client(tmp_path, monkeypatch)
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"tracked_providers": ["claude"]}', encoding="utf-8")
    conn = fake_connect()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,cost_usd,priced) "
                 "VALUES ('a','claude','s1','p','2026-06-10T10:00:00Z','claude-opus-4-8',5.0,1)")
    conn.commit()
    html = client.get("/").text
    assert "Claude" in html                   # 활성 1개 → provider명(AI별 카드)
    assert "전 AI 합산" not in html           # "통합/전 AI" 수식어 제거(단일 활성)
    assert "통합 추세" not in html
    assert "통합 효율 코치" not in html


def test_dashboard_empty_active_shows_settings_prompt(tmp_path, monkeypatch):
    client, fake_connect = _client(tmp_path, monkeypatch)
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"tracked_providers": []}', encoding="utf-8")
    conn = fake_connect()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,cost_usd,priced) "
                 "VALUES ('a','claude','s1','p','2026-06-10T10:00:00Z','claude-opus-4-8',5.0,1)")
    conn.commit()
    html = client.get("/").text
    assert "표시할 AI가 없습니다" in html      # 데이터는 있어도 활성 0개 → 빈 상태 안내
    assert "/settings" in html


def test_history_empty_active_shows_notice(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"tracked_providers": []}', encoding="utf-8")
    html = client.get("/history").text
    assert "표시할 AI가 없습니다" in html


def test_analysis_empty_active_shows_notice(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"tracked_providers": []}', encoding="utf-8")
    html = client.get("/analysis").text
    assert "표시할 AI가 없습니다" in html


def test_settings_get_has_provider_checkboxes(tmp_path, monkeypatch):
    client, _ = _client_with_config(tmp_path, monkeypatch)
    html = client.get("/settings").text
    assert 'name="track_claude"' in html
    assert 'name="track_codex"' in html
    assert "월 예산" not in html


# ── Task 5(추세 차트): 한도 수평선 + 예상선 데이터셋 ──────────────────────────────

def test_overview_renders_forecast_chart_vars(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute(
        "INSERT INTO messages (dedup_key, provider, session_id, ts, cost_usd, priced) "
        "VALUES ('a','claude','s1','2026-06-10T10:00:00Z', 7.0, 1)"
    )
    conn.commit()
    r = client.get("/")
    assert r.status_code == 200
    assert "forecastLimit" in r.text
    assert "forecastLine" in r.text


# ── Task 3 TDD: control 레지스트리 + /app/ping · /app/show ───────────────────────

def test_control_request_show_invokes_registered_callback():
    from tokenomy.web import control
    called = []
    control.set_show_callback(lambda: called.append(True))
    control.request_show()
    assert called == [True]
    control.set_show_callback(None)  # 정리


def test_control_request_show_noop_when_unset():
    from tokenomy.web import control
    control.set_show_callback(None)
    control.request_show()  # 예외 없이 통과


def test_app_ping_returns_marker():
    from fastapi.testclient import TestClient
    from tokenomy.web.app import app
    client = TestClient(app)
    r = client.get("/app/ping")
    assert r.status_code == 200
    assert r.json() == {"app": "tokenomy"}


def test_app_show_invokes_request_show():
    from fastapi.testclient import TestClient
    from tokenomy.web import control
    from tokenomy.web.app import app
    called = []
    control.set_show_callback(lambda: called.append(True))
    client = TestClient(app)
    r = client.post("/app/show")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert called == [True]
    control.set_show_callback(None)


# ── 창 우선 기동(ADR 0023): 수집 중 배너 ──────────────────────────────────────────
def test_dashboard_shows_ingest_banner_while_ingesting(tmp_path, monkeypatch):
    """첫 수집이 백그라운드로 도는 동안 대시보드에 '수집 중' 배너가 뜨고, 끝나면 사라진다."""
    from tokenomy.web import control
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(control, "is_ingesting", lambda: True)
    assert "사용 로그를 읽는 중" in client.get("/").text
    monkeypatch.setattr(control, "is_ingesting", lambda: False)
    assert "사용 로그를 읽는 중" not in client.get("/").text


# ── 수집 가드 + 공식 갱신 지연(ADR 0023) — 모든 writer 진입점 ─────────────────────
def test_do_ingest_skips_when_already_ingesting(tmp_path, monkeypatch):
    """수동 /ingest는 진행 중인 수집이 있으면 cmd_ingest를 중복 실행하지 않는다(동시 writer 방지)."""
    from tokenomy.web import control
    client, _ = _client(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(app_module, "cmd_ingest", lambda conn: calls.append(1))
    control.begin_ingest()                       # 다른 수집이 진행 중이라고 표시
    try:
        client.post("/ingest", follow_redirects=False)
        assert calls == []                       # 중복 수집 안 함
    finally:
        control.end_ingest()


def test_do_ingest_runs_and_releases_when_free(tmp_path, monkeypatch):
    """수집 미진행이면 /ingest는 cmd_ingest를 돌리고 finally에서 상태를 해제한다."""
    from tokenomy.web import control
    control.end_ingest()                         # 정규화
    client, _ = _client(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(app_module, "cmd_ingest", lambda conn: calls.append(1))
    client.post("/ingest", follow_redirects=False)
    assert calls == [1]                          # 수집 실행
    assert control.is_ingesting() is False       # 해제됨(배너 안 굳음)


def test_official_section_skips_refresh_while_ingesting(tmp_path, monkeypatch):
    """첫 수집 중 자동 공식 갱신(/official/section)은 writer/writer라 skip — 마지막 스냅샷만 렌더."""
    from tokenomy.web import control
    client, _ = _client(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(app_module, "refresh_tracked", lambda *a, **k: calls.append(1))
    control.begin_ingest()
    try:
        client.get("/official/section")
        assert calls == []                       # 수집 중 — 자동 갱신 skip
    finally:
        control.end_ingest()
    client.get("/official/section")
    assert calls == [1]                          # 수집 끝나면 자동 갱신 재개


def test_mini_section_skips_refresh_while_ingesting(tmp_path, monkeypatch):
    """미니 자동 폴링도 같은 지연 — 첫 수집 중 공식 갱신 skip."""
    from tokenomy.web import control
    client, _ = _client(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(app_module, "refresh_tracked", lambda *a, **k: calls.append(1))
    control.begin_ingest()
    try:
        client.get("/mini/section")
        assert calls == []
    finally:
        control.end_ingest()


# ── 미니 뷰(ADR 0008): /mini 셸 + /mini/section 자동 폴링 ─────────────────────────

def _seed_monthly(conn, provider, used, limit, util):
    from tokenomy.official_parser import OfficialBucket
    insert_official_buckets(
        conn, provider=provider, fetched_at="2026-06-20T09:00:00+09:00",
        buckets=[OfficialBucket(
            bucket_key="monthly", raw_key="spend", bucket_kind="monthly_limit", label="월 사용 한도",
            native_unit="usd", used_native=used, limit_native=limit, remaining_native=limit - used,
            used_usd=used, limit_usd=limit, remaining_usd=limit - used, utilization=util, resets_at=None)],
        created_at="2026-06-20T09:00:00+09:00")


def test_mini_page_renders_standalone(tmp_path, monkeypatch):
    """GET /mini — 사이드바 없는 독립 셸. app.css 로드 + 게이지 렌더(공식 시드 시)."""
    client, conn_factory = _client(tmp_path, monkeypatch)
    _seed_monthly(conn_factory(), "claude", 80.0, 100.0, 80.0)
    r = client.get("/mini")
    assert r.status_code == 200
    assert 'class="sidebar"' not in r.text          # 독립 셸 — 큰 창 크롬 없음
    assert "/static/app.css" in r.text
    assert "월 사용 한도" in r.text and "80%" in r.text


def test_mini_page_has_polling_trigger(tmp_path, monkeypatch):
    """미니 셸이 /mini/section을 load + every Nm로 폴링한다(자체 갱신)."""
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"tracked_providers": ["claude"], '
                   '"official_fetch": {"min_interval_minutes": 10}}', encoding="utf-8")
    r = client.get("/mini")
    assert 'hx-get="/mini/section"' in r.text
    assert "load" in r.text
    assert "every 10m" in r.text


def test_mini_page_has_switch_and_hide_controls(tmp_path, monkeypatch):
    """hover 컨트롤(배타 전환) — '⊞ 일반뷰'(to_main 브리지) + '✕ 트레이 숨김'(hide_to_tray 브리지)."""
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/mini")
    assert "to_main" in r.text        # pywebview JS 브리지 — 일반뷰로 전환
    assert "hide_to_tray" in r.text   # pywebview JS 브리지 — 미니를 트레이로 숨김
    assert "open_main" not in r.text  # 구 동반-창 브리지 제거
    assert "close_mini" not in r.text


def test_dashboard_sidebar_has_mini_switch(tmp_path, monkeypatch):
    """일반뷰 사이드바에 '미니뷰로 전환' 버튼(to_mini 브리지) — webview 전용(기본 숨김)."""
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/")
    assert "to_mini" in r.text        # pywebview JS 브리지 — 미니로 전환


def test_mini_section_polls_with_auto_throttle(tmp_path, monkeypatch):
    """GET /mini/section → 압축 조각 + manual=False(자동 throttle 적용)."""
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"tracked_providers": ["claude"]}', encoding="utf-8")
    calls = []
    monkeypatch.setattr("tokenomy.official_fetch.fetch_provider",
                        lambda p, **k: calls.append(k.get("manual")))
    r = client.get("/mini/section")
    assert r.status_code == 200
    assert "<!doctype html>" not in r.text.lower()   # 조각만(셸 없음)
    assert 'class="sidebar"' not in r.text
    assert calls == [False]                          # 자동 = throttle 적용


def test_mini_section_official_only_no_local_fallback(tmp_path, monkeypatch):
    """official-only 불변식 — 공식 없고 로컬만 있으면 '공식 데이터 없음' 안내, 로컬 추정 미표시."""
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"tracked_providers": ["claude"]}', encoding="utf-8")
    conn = connect(str(tmp_path / "t.db"))
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,cost_usd,priced) "
                 "VALUES ('a','claude','s','2026-06-12T10:00:00Z',7.0,1)")
    conn.commit()
    monkeypatch.setattr("tokenomy.official_fetch.fetch_provider", lambda p, **k: None)
    r = client.get("/mini/section")
    assert r.status_code == 200
    assert "공식 데이터 없음" in r.text       # official-only 안내
    assert "로컬 추정" not in r.text          # 큰 창 폴백 문구 미사용(불변식)


def test_mini_section_empty_active_shows_settings_prompt(tmp_path, monkeypatch):
    """활성 AI 0개 → 미니에 '설정에서 켜기' 안내."""
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"tracked_providers": []}', encoding="utf-8")
    monkeypatch.setattr("tokenomy.official_fetch.fetch_provider", lambda p, **k: None)
    r = client.get("/mini/section")
    assert r.status_code == 200
    assert "/settings" in r.text


# ---------------------------------------------------------------------------
# 디버그 모드 + /official/raw (ADR 0014)
# ---------------------------------------------------------------------------

def _seed_raw_row(conn, provider="claude", ts="2026-06-24T10:00:00+09:00",
                  raw='{"hello":"world"}', status="ok", http_code=200):
    insert_official_raw(conn, provider=provider, fetched_at=ts, status=status,
                        http_code=http_code, raw_text=raw, created_at=ts)


def test_official_raw_404_when_debug_off(tmp_path, monkeypatch):
    """디버그 OFF(기본)면 라우트 자체가 404 — 완전한 숨김(ADR 0014)."""
    client, _ = _client(tmp_path, monkeypatch)
    assert client.get("/official/raw").status_code == 404


def test_debug_toggle_on_unlocks_official_raw(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    _seed_raw_row(conn_factory())
    r = client.post("/app/debug-toggle", data={"enabled": "1"}, follow_redirects=False)
    assert r.status_code == 303
    r2 = client.get("/official/raw")
    assert r2.status_code == 200
    assert "hello" in r2.text          # raw 원문 노출


def test_debug_toggle_off_relocks_official_raw(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    client.post("/app/debug-toggle", data={"enabled": "1"})
    assert client.get("/official/raw").status_code == 200   # 빈 상태도 200
    client.post("/app/debug-toggle", data={"enabled": "0"})
    assert client.get("/official/raw").status_code == 404


def test_debug_toggle_flip_without_param(tmp_path, monkeypatch):
    """enabled 파라미터 없으면 현재 상태를 뒤집는다(7탭 fallback)."""
    client, _ = _client(tmp_path, monkeypatch)
    client.post("/app/debug-toggle")                        # off→on
    assert client.get("/official/raw").status_code == 200
    client.post("/app/debug-toggle")                        # on→off
    assert client.get("/official/raw").status_code == 404


def test_sidebar_shows_debug_badge_when_on(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    assert "DEBUG" not in client.get("/").text              # off면 배지 없음
    client.post("/app/debug-toggle", data={"enabled": "1"})
    assert "DEBUG" in client.get("/").text                  # on이면 사이드바 배지


def test_official_history_shows_raw_button_when_debug_on(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    assert "raw 보기" not in client.get("/official-history").text
    client.post("/app/debug-toggle", data={"enabled": "1"})
    assert "raw 보기" in client.get("/official-history").text


def test_settings_shows_debug_off_when_on(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    assert "디버그 모드 끄기" not in client.get("/settings").text   # off면 노출 안 함
    client.post("/app/debug-toggle", data={"enabled": "1"})
    html = client.get("/settings").text
    assert "디버그 모드 끄기" in html
    assert 'value="0"' in html                              # OFF 버튼이 enabled=0 전송


# ── Task 6 TDD: 설정 UI — 자동 갱신 토글 ─────────────────────────────────────────

def test_settings_saves_auto_refresh_token(tmp_path, monkeypatch):
    """POST /settings가 auto_refresh_token(auto/always/off)을 official_fetch에 저장한다."""
    client, cfg_path = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings", data={"min_interval": "10",
                                       "auto_refresh_token": "always"},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["official_fetch"]["auto_refresh_token"] == "always"


def test_settings_page_shows_auto_refresh_control(tmp_path, monkeypatch):
    """GET /settings 렌더에 auto_refresh_token select 컨트롤이 있어야 한다."""
    client, _ = _client_with_config(tmp_path, monkeypatch)
    assert 'name="auto_refresh_token"' in client.get("/settings").text


def test_settings_preserves_auto_refresh_safety_hours(tmp_path, monkeypatch):
    """POST /settings가 UI 미노출 auto_refresh_safety_hours를 덮어쓰지 않고 보존한다(Fix 1)."""
    client, cfg_path = _client_with_config(tmp_path, monkeypatch)
    # 비기본(48h) safety_hours를 미리 파일에 심어 두고 저장한다
    cfg_path.write_text(json.dumps({"tracked_providers": ["claude", "codex"],
                                    "official_fetch": {"auto_refresh_safety_hours": 48}}),
                        encoding="utf-8")
    r = client.post("/settings", data={"min_interval": "10",
                                       "auto_refresh_token": "auto"},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["official_fetch"]["auto_refresh_safety_hours"] == 48


def test_settings_invalid_auto_refresh_token_falls_back_to_auto(tmp_path, monkeypatch):
    """POST /settings에 유효하지 않은 auto_refresh_token("garbage")을 보내면 "auto"로 폴백 저장(Fix 4)."""
    client, cfg_path = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings", data={"min_interval": "10",
                                       "auto_refresh_token": "garbage"},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["official_fetch"]["auto_refresh_token"] == "auto"


def test_web_layer_has_no_raw_max_ts_sql():
    """계층 가드: 원시 ts 조회는 aggregate.last_message_ts로 봉합 — web엔 raw SQL 금지."""
    for mod in (views_module, app_module):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "SELECT MAX(ts)" not in src, f"{mod.__name__}에 raw MAX(ts) SQL 잔존"


def test_views_does_not_import_private_provider_where():
    """계층 가드: views는 aggregate private 심볼(_provider_where)을 끌어쓰지 않는다."""
    src = Path(views_module.__file__).read_text(encoding="utf-8")
    assert "_provider_where" not in src


# ── outlook 팬아웃 공유(후보 4) — 렌더 1회당 official_view는 provider당 1회 ──────

def _count_official_view(monkeypatch):
    """official_view 호출 provider를 계수하는 대역 — outlook 경유·views 직접 팬아웃 모두 계수."""
    import tokenomy.forecast as forecast_module
    calls: list[str] = []
    real = forecast_module.official_view

    def counting(conn, provider, *a, **kw):
        calls.append(provider)
        return real(conn, provider, *a, **kw)

    monkeypatch.setattr(forecast_module, "official_view", counting)
    if hasattr(views_module, "official_view"):      # 리팩터 전 직접 팬아웃 경로도 계수
        monkeypatch.setattr(views_module, "official_view", counting)
    return calls


def test_official_section_fans_out_official_view_once_per_provider(monkeypatch):
    """섹션 조립(카드+기간 카드·공유문구)은 outlook 팬아웃 1회를 공유한다 — 재계산 금지."""
    calls = _count_official_view(monkeypatch)
    conn = connect(":memory:")
    config = {"tracked_providers": ["claude", "codex"]}
    views_module.official_section_context(conn, config, _FROZEN_NOW)
    assert sorted(calls) == ["claude", "codex"]


def test_overview_fans_out_official_view_once_per_provider(tmp_path, monkeypatch):
    """대시보드 조립(전망 히어로+카드+기간 카드)도 outlook 팬아웃 1회 공유."""
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"tracked_providers": ["claude", "codex"]}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))    # overview_context는 내부 load_config
    calls = _count_official_view(monkeypatch)
    conn = connect(":memory:")
    views_module.overview_context(conn, "cost", _FROZEN_NOW)
    assert sorted(calls) == ["claude", "codex"]


# ─── 토큰 절약 화면(ADR 0026) ─────────────────────────────────────────────────

def test_savers_page_renders(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/savers")
    assert r.status_code == 200
    assert "토큰 절약" in r.text


def test_sidebar_has_savers_nav(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/")
    assert 'href="/savers"' in r.text
