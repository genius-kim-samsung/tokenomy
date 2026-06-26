from datetime import date, datetime, timedelta

import pytest

from tokenomy.aggregate import (
    DayPoint, DaySessionRow, KST,
    add_business_days, business_days_between, by_day_session,
    by_dimension, by_model, by_project, by_session, daily_series,
    insights,
    month_bounds, month_spend, normalize_project, official_view,
    OfficialView,
    parse_ts, period_bounds, session_detail, sidechain_split,
    SidechainSplit, stacked_trend, token_composition, pricing_coverage, CoverageReport,
    combined_forecast, CombinedForecast,
    _trailing_business_days, trailing_window_spend,
    pool_used_history, _segment_points, pool_history, pool_daily_history,
    pool_hourly_history,
    pool_snapshots_by_day,
    official_period_glance, ProviderGlance, PeriodSpend,
)
from tokenomy.db import connect, insert_official_buckets, ingest_records
from tokenomy.official_parser import OfficialBucket
from tokenomy.parser import UsageRecord
from tokenomy.web.views import build_date_tree, history_context, dimension_context, overview_context, session_context, pool_history_to_daily

# June 2026 has 30 days
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=KST)  # day 10 of 30


def _insert(conn, ts, cost, project="/p", session="s", cache_read=0, input_t=0,
            priced=1, provider="claude", model="claude-opus-4-8"):
    conn.execute(
        "INSERT INTO messages(dedup_key,provider,session_id,project,ts,model,"
        "input_tokens,cache_creation,cache_read,cost_usd,priced) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (f"{ts}-{cost}-{session}-{project}-{model}", provider, session, project, ts,
         model, input_t, 0, cache_read, cost, priced),
    )
    conn.commit()


def test_month_bounds_june():
    start, nxt = month_bounds(NOW)
    assert start.month == 6 and start.day == 1
    assert nxt.month == 7
    assert (nxt - start).days == 30


def test_parse_ts_utc_to_kst():
    dt = parse_ts("2026-06-05T00:00:00Z")
    assert dt.tzinfo == KST
    assert dt.hour == 9  # +9



@pytest.mark.parametrize("cwd,expected", [
    # Windows 워크트리(Claude) → 부모 repo 경로로 접힘
    (r"C:\projects\tokenomy\.claude\worktrees\history-view-spec",
     r"C:\projects\tokenomy"),
    # unix 슬래시(Codex가 unix 경로를 기록하는 경우) → 부모
    ("/home/u/proj/.claude/worktrees/feat-x", "/home/u/proj"),
    # 워크트리 하위 디렉토리에서 실행해도 부모 repo로 접힘(마커 이후 전부 제거)
    (r"C:\projects\tokenomy\.claude\worktrees\history-view-spec\sub\dir",
     r"C:\projects\tokenomy"),
    # 대소문자 무관(.Claude/Worktrees)
    (r"C:\proj\.Claude\Worktrees\b", r"C:\proj"),
    # 비워크트리 경로 → 원본 그대로
    (r"C:\projects\tokenomy", r"C:\projects\tokenomy"),
    # .claude 있지만 worktrees가 아님 → 원본 그대로
    (r"C:\projects\tokenomy\.claude\agents",
     r"C:\projects\tokenomy\.claude\agents"),
    # None/비경로 → 그대로
    (None, None),
    ("(unknown)", "(unknown)"),
])
def test_normalize_project_folds_worktree_to_parent(cwd, expected):
    assert normalize_project(cwd) == expected


def test_by_project_folds_worktree_into_parent():
    # 워크트리 세션 비용이 부모 프로젝트에 합산되어 한 행으로 묶인다(provider 무관 경로 정규화).
    conn = connect(":memory:")
    parent = r"C:\projects\tokenomy"
    wt = r"C:\projects\tokenomy\.claude\worktrees\history-view-spec"
    _insert(conn, "2026-06-05T00:00:00Z", 10.0, project=parent, session="a")
    _insert(conn, "2026-06-06T00:00:00Z", 5.0, project=wt, session="b")
    rows = by_project(conn, "claude", NOW)
    assert len(rows) == 1
    assert rows[0].project == parent
    assert rows[0].cost == 15.0
    assert rows[0].sessions == 2


def test_by_project_sorted_with_cache_ratio():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 5.0, project="/cheap", session="a", input_t=100, cache_read=0)
    _insert(conn, "2026-06-06T00:00:00Z", 20.0, project="/expensive", session="b", input_t=50, cache_read=50)
    rows = by_project(conn, "claude", NOW)
    assert rows[0].project == "/expensive"
    assert rows[0].cost == 20.0
    assert rows[0].cache_ratio == 0.5   # 50 / (50+0+50)
    assert rows[1].project == "/cheap"


def test_by_project_counts_messages():
    # 폴더별 메시지 수 = 그 폴더의 messages 행 수(세션 수와 별개).
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 5.0, project="/p", session="s1")
    _insert(conn, "2026-06-06T00:00:00Z", 3.0, project="/p", session="s1")
    _insert(conn, "2026-06-07T00:00:00Z", 1.0, project="/p", session="s2")
    rows = by_project(conn, "claude", NOW)
    assert rows[0].project == "/p"
    assert rows[0].msgs == 3       # 메시지 3건
    assert rows[0].sessions == 2   # 세션 2개


def test_by_project_sums_all_token_kinds():
    # 총 토큰 수 = input + output + cache_creation + cache_read (4종 전부).
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", project="/p", ts="2026-06-10T10:00:00Z",
         input_tokens=100, output_tokens=20, cache_creation=5, cache_read=40)
    _msg(conn, dedup_key="b", project="/p", ts="2026-06-11T10:00:00Z",
         input_tokens=1, output_tokens=2, cache_creation=3, cache_read=4)
    rows = by_project(conn, "claude", NOW)
    assert rows[0].project == "/p"
    assert rows[0].tokens == 175   # (100+20+5+40) + (1+2+3+4)


def _set_summary(conn, session, summary):
    conn.execute(
        "INSERT INTO sessions(session_id, summary) VALUES(?,?) "
        "ON CONFLICT(session_id) DO UPDATE SET summary=excluded.summary",
        (session, summary),
    )
    conn.commit()


# ─── status 필드 + 집계 fixture ───────────────────────────────────────────────

def _msg(conn, **kw):
    """messages 테이블에 직접 INSERT (집계 함수 테스트용 fixture)."""
    conn.execute(
        """INSERT INTO messages
           (dedup_key, provider, session_id, project, ts, model,
            input_tokens, output_tokens, cache_creation, cache_read,
            web_search, web_fetch, cost_usd, priced, request_id, is_sidechain,
            attribution_skill, git_branch)
           VALUES (:dedup_key,:provider,:session_id,:project,:ts,:model,
            :input_tokens,:output_tokens,:cache_creation,:cache_read,
            :web_search,:web_fetch,:cost_usd,:priced,:request_id,:is_sidechain,
            :attribution_skill,:git_branch)""",
        {
            "dedup_key": kw["dedup_key"], "provider": kw.get("provider", "claude"),
            "session_id": kw.get("session_id", "s1"), "project": kw.get("project", "proj"),
            "ts": kw["ts"], "model": kw.get("model", "claude-opus-4-8"),
            "input_tokens": kw.get("input_tokens", 0), "output_tokens": kw.get("output_tokens", 0),
            "cache_creation": kw.get("cache_creation", 0), "cache_read": kw.get("cache_read", 0),
            "web_search": kw.get("web_search", 0), "web_fetch": kw.get("web_fetch", 0),
            "cost_usd": kw.get("cost_usd", 0.0), "priced": kw.get("priced", 1),
            "request_id": kw.get("request_id"), "is_sidechain": kw.get("is_sidechain", 0),
            "attribution_skill": kw.get("attribution_skill"), "git_branch": kw.get("git_branch"),
        },
    )
    conn.commit()


# ─── by_session: 작업 요약(summary) + project/order ───────────────────────────

def test_by_session_aggregates_cost_with_summary():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 5.0, project="/p", session="s1")
    _insert(conn, "2026-06-06T00:00:00Z", 3.0, project="/p", session="s1")
    _set_summary(conn, "s1", "토큰 집계 구현")
    rows = by_session(conn, "claude", NOW)
    assert len(rows) == 1
    assert rows[0].session_id == "s1"
    assert rows[0].cost == 8.0          # 5 + 3 합산
    assert rows[0].summary == "토큰 집계 구현"
    assert rows[0].project == "/p"


def test_by_session_summary_none_when_absent():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 1.0, session="s1")
    rows = by_session(conn, "claude", NOW)
    assert rows[0].summary is None


def test_by_session_recent_order():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 1.0, session="old")
    _insert(conn, "2026-06-09T00:00:00Z", 1.0, session="new")
    rows = by_session(conn, "claude", NOW, order="recent")
    assert [r.session_id for r in rows] == ["new", "old"]


def test_by_session_cost_order_and_limit():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 2.0, session="a")
    _insert(conn, "2026-06-06T00:00:00Z", 9.0, session="b")
    _insert(conn, "2026-06-07T00:00:00Z", 5.0, session="c")
    rows = by_session(conn, "claude", NOW, limit_n=2, order="cost")
    assert [r.session_id for r in rows] == ["b", "c"]


def test_by_session_project_filter():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 1.0, project="/a", session="sa")
    _insert(conn, "2026-06-06T00:00:00Z", 1.0, project="/b", session="sb")
    rows = by_session(conn, "claude", NOW, project="/a")
    assert [r.session_id for r in rows] == ["sa"]


def test_by_session_excludes_other_months():
    conn = connect(":memory:")
    _insert(conn, "2026-05-30T00:00:00Z", 1.0, session="may")  # KST 5/30 09:00 → 5월
    _insert(conn, "2026-06-05T00:00:00Z", 1.0, session="jun")
    rows = by_session(conn, "claude", NOW)
    assert [r.session_id for r in rows] == ["jun"]


# ─── status 필드 테스트 ───────────────────────────────────────────────────────

_NOW_STATUS = datetime(2026, 6, 15, tzinfo=KST)  # 6월 15일 = 30일 중 15일 경과


def test_by_session_aggregates_and_sorts():
    conn = connect(":memory:")
    # s1: 두 메시지 합 $30, s2: 한 메시지 $50 → 비용순 s2, s1
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-10T10:00:00Z",
         cost_usd=10.0, cache_read=70, input_tokens=30)
    _msg(conn, dedup_key="b", session_id="s1", ts="2026-06-11T10:00:00Z",
         cost_usd=20.0, cache_read=0, input_tokens=0)
    _msg(conn, dedup_key="c", session_id="s2", ts="2026-06-12T10:00:00Z",
         cost_usd=50.0, cache_read=0, input_tokens=100)
    conn.execute("INSERT INTO sessions (session_id, label) VALUES ('s1', '대시보드 작업')")
    conn.execute("UPDATE sessions SET user_turns=2 WHERE session_id='s1'")
    conn.commit()

    rows = by_session(conn, "claude", _NOW_STATUS)
    assert [r.session_id for r in rows] == ["s2", "s1"]
    assert rows[1].cost == 30.0          # s1 합산
    assert rows[1].msgs == 2
    assert rows[1].label == "대시보드 작업"
    # s1 cache_ratio = 70 / (30+0+70) = 0.7
    assert rows[1].cache_ratio == 0.7


def test_by_session_only_current_month():
    conn = connect(":memory:")
    _msg(conn, dedup_key="old", session_id="s1", ts="2026-05-30T10:00:00Z", cost_usd=99.0)
    _msg(conn, dedup_key="new", session_id="s1", ts="2026-06-10T10:00:00Z", cost_usd=5.0)
    rows = by_session(conn, "claude", _NOW_STATUS)
    assert len(rows) == 1
    assert rows[0].cost == 5.0           # 5월 메시지 제외


def test_session_detail_groups_by_model():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-10T10:00:00Z",
         model="claude-opus-4-8", cost_usd=11.0, input_tokens=100, output_tokens=20,
         cache_creation=5, cache_read=40, web_search=2, web_fetch=1)
    _msg(conn, dedup_key="b", session_id="s1", ts="2026-06-10T11:00:00Z",
         model="claude-haiku-4-5", cost_usd=1.0, input_tokens=10, web_search=0)
    conn.execute("INSERT INTO sessions (session_id, project, provider, label) "
                 "VALUES ('s1', 'proj', 'claude', '라벨')")
    conn.execute("UPDATE sessions SET user_turns=2 WHERE session_id='s1'")
    conn.commit()

    d = session_detail(conn, "s1")
    assert d is not None
    assert d.cost == 12.0
    assert d.msgs == 2
    assert d.web_search == 2
    assert d.web_fetch == 1
    assert d.label == "라벨"
    # 모델별 비용순 정렬: opus(11) 먼저
    assert d.models[0].model == "claude-opus-4-8"
    assert d.models[0].cost == 11.0
    assert d.models[0].cache_read == 40


def test_session_detail_missing_returns_none():
    conn = connect(":memory:")
    assert session_detail(conn, "does-not-exist") is None


def test_session_detail_folds_worktree_project():
    # 세션 상세도 내역 목록과 일관되게 워크트리 cwd를 부모 repo로 표시한다.
    conn = connect(":memory:")
    wt = r"C:\projects\tokenomy\.claude\worktrees\history-view-spec"
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-10T10:00:00Z",
         model="claude-opus-4-8", cost_usd=3.0)
    conn.execute("INSERT INTO sessions (session_id, project, provider) "
                 "VALUES ('s1', ?, 'claude')", (wt,))
    conn.commit()
    d = session_detail(conn, "s1")
    assert d.project == r"C:\projects\tokenomy"


def test_insights_low_cache_and_websearch():
    conn = connect(":memory:")
    # cache_read 비율 = 10/(90+0+10)=0.1 < 0.30 → warn 카드
    # web_search 합 60 > 50 → info 카드
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=5.0,
         input_tokens=90, cache_read=10, web_search=60)
    cards = insights(conn, _NOW_STATUS, "claude")
    levels = {c.level for c in cards}
    texts = " ".join(c.text for c in cards)
    assert "warn" in levels and "info" in levels
    assert "캐시" in texts
    assert "web_search" in texts


def test_insights_unpriced_card():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=0.0,
         input_tokens=100, cache_read=100, priced=0)
    cards = insights(conn, _NOW_STATUS, "claude")
    assert any("미식별" in c.text for c in cards)


def test_insights_clean_returns_placeholder():
    conn = connect(":memory:")
    # 캐시 충분(0.9), web_search 적음, priced, projected 낮음 → 특이신호 없음
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=1.0,
         input_tokens=10, cache_read=90, web_search=0, priced=1)
    cards = insights(conn, _NOW_STATUS, "claude")
    assert len(cards) == 1
    assert "특이 신호 없음" in cards[0].text



def test_daily_series_cumulative():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-01T10:00:00Z", cost_usd=5.0)
    _msg(conn, dedup_key="b", ts="2026-06-02T10:00:00Z", cost_usd=3.0)
    _msg(conn, dedup_key="c", ts="2026-06-02T12:00:00Z", cost_usd=2.0)
    pts = daily_series(conn, "claude", _NOW_STATUS)   # budget_start 미지정 → 6/1부터
    assert len(pts) == 30                       # 1일~30일(말일까지 확장)
    assert pts[0].day == 1 and pts[0].cumulative_cost == 5.0
    assert pts[1].cumulative_cost == 10.0       # 5 + (3+2) 누적
    assert pts[14].day == 15 and pts[14].cumulative_cost == 10.0   # 오늘(마지막 실제값)
    assert pts[15].cumulative_cost is None      # 16일(미래) → None
    assert pts[-1].day == 30 and pts[-1].cumulative_cost is None


def test_session_context_missing():
    conn = connect(":memory:")
    assert session_context(conn, "nope") is None


def test_providers_constant():
    from tokenomy.aggregate import PROVIDERS
    assert PROVIDERS == ("claude", "codex")


def test_by_project_combines_providers_when_none():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 5.0, project="/p", session="a", provider="claude")
    _insert(conn, "2026-06-06T00:00:00Z", 7.0, project="/p", session="b", provider="codex")
    rows = by_project(conn, None, NOW)
    assert len(rows) == 1
    assert rows[0].project == "/p"
    assert rows[0].cost == 12.0      # claude 5 + codex 7 합산
    assert rows[0].sessions == 2


def test_overview_context_shape(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    # 활성=둘 고정 — "전체=활성 합산"(ADR 0005)이라 month_total 결정성을 위해 명시.
    cfg.write_text('{"tracked_providers": ["claude", "codex"], "budget": {"claude": 100, "codex": 40}}',
                   encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=10.0, project="/p")
    _msg(conn, dedup_key="b", provider="codex", ts="2026-06-11T10:00:00Z", cost_usd=4.0, project="/p")
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)
    assert ctx["active_nav"] == "dashboard"
    # 번다운 카드 제거됨 — month_total로 총지출 확인
    assert ctx["month_total"] == 14.0
    assert "budget_configured" not in ctx
    assert "claude_bd" not in ctx and "codex_bd" not in ctx
    assert ctx["projects"][0].project == "/p"
    assert ctx["projects"][0].cost == 14.0
    assert ctx["has_data"] is True
    assert "daily_labels" in ctx and "insights" in ctx and "sessions" in ctx


def test_overview_context_provider_without_data(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"tracked_providers": ["claude", "codex"]}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=10.0)
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)
    assert ctx["has_data"] is True
    assert "budget_configured" not in ctx                # 번다운 카드 제거됨


def test_overview_context_ignores_legacy_budget_start(monkeypatch, tmp_path):
    """legacy budget_start JSON 키는 무시됨. overview_context는 항상 달력 월(month_spend) 기준."""
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"tracked_providers": ["claude", "codex"], '
                   '"budget": {"claude": 100, "codex": 40}, "budget_start": "2026-06-12"}',
                   encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="pre", provider="claude", ts="2026-06-05T10:00:00Z", cost_usd=99.0)
    _msg(conn, dedup_key="post", provider="claude", ts="2026-06-13T10:00:00Z", cost_usd=10.0)
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)   # _NOW_STATUS = 6/15
    # month_spend는 달력 월 전체(6/1~) 포함
    assert ctx["month_total"] == 109.0                   # 99 + 10 모두 포함
    assert "claude_bd" not in ctx


def test_overview_context_trend_calendar_month_ignores_legacy_budget(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"tracked_providers": ["claude", "codex"], '
                   '"budget": {"claude": 100, "codex": 40}, "budget_start": "2026-06-12"}',
                   encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="pre", provider="claude", ts="2026-06-05T10:00:00Z", cost_usd=99.0)
    _msg(conn, dedup_key="post", provider="claude", ts="2026-06-13T10:00:00Z", cost_usd=10.0)
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)   # 6/15
    # x축: daily_series는 달력 월 기준(budget_start clamp 없음) → 6/1~6/30 (30일)
    assert ctx["daily_labels"][0] == 1
    assert ctx["daily_labels"][-1] == 30
    assert len(ctx["daily_labels"]) == 30
    # 추세 스택: codex 데이터 없음 → Claude 밴드 1개
    assert "daily_actual" not in ctx
    series = ctx["trend_series"]
    assert [s["label"] for s in series] == ["Claude"]
    assert series[0]["cum"][4] == 99.0          # 6/5(idx4) 지출 포함(calendar month)
    assert series[0]["cum"][12] == 109.0        # 6/13(idx12) 누적 99+10
    assert series[0]["cum"][-1] is None         # 6/30(미래) → None
    assert series[0]["top"] == series[0]["cum"] # 단일 밴드: top == cum
    assert ctx["trend_totals"][12] == 109.0
    assert ctx["trend_totals"][-1] is None
    # 페이스선·가로선 제거됨
    assert "daily_pace" not in ctx
    assert "daily_budget" not in ctx


def test_overview_context_no_budget_unconfigured(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"tracked_providers": ["claude", "codex"]}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=10.0)
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)
    assert "budget_configured" not in ctx       # 번다운 카드 제거됨
    assert "claude_bd" not in ctx



# ─── period_bounds: 일/주/월 경계 + 라벨 ──────────────────────────────────────

_ANCHOR_SAT = datetime(2026, 6, 13, 15, 0, tzinfo=KST)  # 토요일 15:00 KST


def test_period_bounds_day():
    start, nxt, label = period_bounds("day", _ANCHOR_SAT)
    assert start == datetime(2026, 6, 13, 0, 0, tzinfo=KST)
    assert nxt == datetime(2026, 6, 14, 0, 0, tzinfo=KST)
    assert label == "2026-06-13 (토)"


def test_period_bounds_week_starts_monday():
    start, nxt, label = period_bounds("week", _ANCHOR_SAT)
    assert start == datetime(2026, 6, 8, 0, 0, tzinfo=KST)   # 월요일
    assert nxt == datetime(2026, 6, 15, 0, 0, tzinfo=KST)
    assert label == "2026-06-08 ~ 06-14"


def test_period_bounds_month():
    start, nxt, label = period_bounds("month", _ANCHOR_SAT)
    assert start == datetime(2026, 6, 1, 0, 0, tzinfo=KST)
    assert nxt == datetime(2026, 7, 1, 0, 0, tzinfo=KST)
    assert label == "2026-06"


def test_period_bounds_month_year_rollover():
    start, nxt, label = period_bounds("month", datetime(2026, 12, 20, tzinfo=KST))
    assert start == datetime(2026, 12, 1, 0, 0, tzinfo=KST)
    assert nxt == datetime(2027, 1, 1, 0, 0, tzinfo=KST)
    assert label == "2026-12"


def test_period_bounds_week_crosses_month():
    # 2026-07-01(수)가 속한 주 → 월요일 2026-06-29 시작
    start, nxt, label = period_bounds("week", datetime(2026, 7, 1, tzinfo=KST))
    assert start == datetime(2026, 6, 29, 0, 0, tzinfo=KST)
    assert nxt == datetime(2026, 7, 6, 0, 0, tzinfo=KST)
    assert label == "2026-06-29 ~ 07-05"


def test_period_bounds_week_year_rollover():
    # 2026-12-31(목)이 속한 주 → 월요일 2026-12-28, end 2027-01-03(다른 연도)
    start, nxt, label = period_bounds("week", datetime(2026, 12, 31, tzinfo=KST))
    assert start == datetime(2026, 12, 28, 0, 0, tzinfo=KST)
    assert nxt == datetime(2027, 1, 4, 0, 0, tzinfo=KST)
    assert label == "2026-12-28 ~ 2027-01-03"


# ─── _range_rows: 임의 기간 집계 ──────────────────────────────────────────────

def test_by_project_range_restricts_to_week():
    conn = connect(":memory:")
    _insert(conn, "2026-06-08T00:00:00Z", 5.0, project="/p", session="a")   # KST 6/8 09:00 (주 안)
    _insert(conn, "2026-06-20T00:00:00Z", 9.0, project="/p", session="b")   # KST 6/20 (주 밖)
    start, nxt, _ = period_bounds("week", datetime(2026, 6, 13, tzinfo=KST))
    rows = by_project(conn, "claude", NOW, start=start, nxt=nxt)
    assert len(rows) == 1
    assert rows[0].cost == 5.0


def test_by_session_range_restricts_to_day():
    conn = connect(":memory:")
    _insert(conn, "2026-06-13T01:00:00Z", 3.0, session="d13")   # KST 6/13 10:00
    _insert(conn, "2026-06-14T01:00:00Z", 7.0, session="d14")   # KST 6/14 10:00
    start, nxt, _ = period_bounds("day", datetime(2026, 6, 13, tzinfo=KST))
    rows = by_session(conn, "claude", NOW, start=start, nxt=nxt)
    assert [r.session_id for r in rows] == ["d13"]


def test_by_project_partial_range_args_raise():
    conn = connect(":memory:")
    start, nxt, _ = period_bounds("day", datetime(2026, 6, 13, tzinfo=KST))
    with pytest.raises(AssertionError):
        by_project(conn, "claude", NOW, start=start)   # nxt 누락 → 가드 발동


_NOW_613 = datetime(2026, 6, 13, 12, 0, tzinfo=KST)
_ANCHOR_613 = datetime(2026, 6, 13, tzinfo=KST)


# ─── by_day_session: (날짜 × 세션) 행 + 이어짐/캐시미스 ────────────────────────

_JUN = month_bounds(datetime(2026, 6, 15, tzinfo=KST))   # (6/1, 7/1) KST


def test_by_day_session_splits_session_across_days():
    conn = connect(":memory:")
    # 한 세션 s1이 6/13, 6/14 이틀에 걸침 → 2행
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-13T01:00:00Z", cost_usd=2.0)  # KST 6/13
    _msg(conn, dedup_key="b", session_id="s1", ts="2026-06-14T01:00:00Z", cost_usd=1.0)  # KST 6/14
    rows = by_day_session(conn, "claude", start=_JUN[0], nxt=_JUN[1])
    by_date = {r.date: r for r in rows}
    assert set(by_date) == {"2026-06-13", "2026-06-14"}
    assert by_date["2026-06-13"].cost == 2.0
    assert by_date["2026-06-14"].cost == 1.0
    # 첫날은 이어짐 아님, 둘째날은 이어짐
    assert by_date["2026-06-13"].is_continued is False
    assert by_date["2026-06-14"].is_continued is True


def test_by_day_session_first_day_never_cache_miss():
    conn = connect(":memory:")
    # 첫 등장일은 캐시율이 낮아도(cache_read 0) cache_miss=False (첫 캐시 쓰기는 정상)
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-13T01:00:00Z",
         cost_usd=2.0, input_tokens=100, cache_read=0)
    rows = by_day_session(conn, "claude", start=_JUN[0], nxt=_JUN[1])
    assert rows[0].is_continued is False
    assert rows[0].cache_miss is False


def test_by_day_session_continued_low_cache_is_miss():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-13T01:00:00Z",
         cost_usd=2.0, input_tokens=10, cache_read=90)   # 첫날 캐시율 0.9
    _msg(conn, dedup_key="b", session_id="s1", ts="2026-06-14T01:00:00Z",
         cost_usd=2.0, input_tokens=90, cache_read=10)   # 둘째날 캐시율 0.1 < 0.30
    rows = by_day_session(conn, "claude", start=_JUN[0], nxt=_JUN[1])
    by_date = {r.date: r for r in rows}
    assert by_date["2026-06-14"].cache_miss is True      # 이어짐 + 캐시율 낮음
    assert by_date["2026-06-13"].cache_miss is False


def test_by_day_session_continued_high_cache_not_miss():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-13T01:00:00Z",
         cost_usd=2.0, input_tokens=10, cache_read=90)
    _msg(conn, dedup_key="b", session_id="s1", ts="2026-06-14T01:00:00Z",
         cost_usd=2.0, input_tokens=10, cache_read=90)   # 둘째날도 캐시율 0.9
    rows = by_day_session(conn, "claude", start=_JUN[0], nxt=_JUN[1])
    by_date = {r.date: r for r in rows}
    assert by_date["2026-06-14"].is_continued is True
    assert by_date["2026-06-14"].cache_miss is False     # 이어졌지만 캐시율 높음 → 정상


def test_by_day_session_continued_across_month_boundary():
    conn = connect(":memory:")
    # 세션이 5월에 시작 → 6월 행은 is_continued=True (전체 MIN(ts) 기준)
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-05-20T01:00:00Z", cost_usd=5.0)
    _msg(conn, dedup_key="b", session_id="s1", ts="2026-06-02T01:00:00Z",
         cost_usd=2.0, input_tokens=90, cache_read=10)
    rows = by_day_session(conn, "claude", start=_JUN[0], nxt=_JUN[1])
    assert len(rows) == 1                                # 6월 행만(5월은 범위 밖)
    assert rows[0].date == "2026-06-02"
    assert rows[0].is_continued is True                 # 5월 시작 → 이어짐
    assert rows[0].cache_miss is True                   # 이어짐 + 캐시율 0.1


def test_by_day_session_kst_bucketing_crosses_utc_midnight():
    conn = connect(":memory:")
    # UTC 6/13 16:00 = KST 6/14 01:00 → 6/14로 귀속
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-13T16:00:00Z", cost_usd=1.0)
    rows = by_day_session(conn, "claude", start=_JUN[0], nxt=_JUN[1])
    assert rows[0].date == "2026-06-14"


def test_by_day_session_provider_filter():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", provider="claude", session_id="s1",
         ts="2026-06-13T01:00:00Z", cost_usd=1.0)
    _msg(conn, dedup_key="b", provider="codex", session_id="s2",
         ts="2026-06-13T01:00:00Z", cost_usd=9.0)
    rows = by_day_session(conn, "claude", start=_JUN[0], nxt=_JUN[1])
    assert [r.session_id for r in rows] == ["s1"]


def test_by_day_session_empty():
    conn = connect(":memory:")
    rows = by_day_session(conn, "claude", start=_JUN[0], nxt=_JUN[1])
    assert rows == []


def test_by_day_session_carries_summary_and_label():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-13T01:00:00Z", cost_usd=1.0, project="/p")
    conn.execute("INSERT INTO sessions (session_id, summary, label, provider) "
                 "VALUES ('s1', '내역 화면 작업', '업무', 'claude')")
    conn.commit()
    rows = by_day_session(conn, "claude", start=_JUN[0], nxt=_JUN[1])
    assert rows[0].summary == "내역 화면 작업"
    assert rows[0].label == "업무"
    assert rows[0].project == "/p"


def test_by_day_session_carries_raw_cache_for_weighting():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-13T01:00:00Z",
         cost_usd=1.0, input_tokens=30, cache_creation=10, cache_read=60)
    rows = by_day_session(conn, "claude", start=_JUN[0], nxt=_JUN[1])
    assert rows[0].cache_read == 60
    assert rows[0].cache_den == 100        # input 30 + cache_creation 10 + cache_read 60
    assert rows[0].cache_ratio == 0.6


# ─── history_context: 그룹/평면 + 정렬 ────────────────────────────────────────

def _seed_history(conn):
    # 6/13: s1($2), s2($9)  /  6/12: s1 이어짐($1, 캐시율 낮음)
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-13T01:00:00Z",
         cost_usd=2.0, input_tokens=10, cache_read=90)
    _msg(conn, dedup_key="b", session_id="s2", ts="2026-06-13T02:00:00Z",
         cost_usd=9.0, input_tokens=10, cache_read=90)
    _msg(conn, dedup_key="c", session_id="s1", ts="2026-06-12T01:00:00Z",
         cost_usd=1.0, input_tokens=90, cache_read=10)   # s1 첫 등장은 6/12


def test_history_context_tree_default(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _seed_history(conn)   # 6/13: s1($2),s2($9) 같은 폴더 'proj' / 6/12: s1($1)
    ctx = history_context(conn, _ANCHOR_613, "", "date_desc", now_kst=_NOW_613)
    assert ctx["active_nav"] == "history"
    assert ctx["count"] == 3 and ctx["total"] == 12.0
    assert [d.date for d in ctx["tree"]] == ["2026-06-13", "2026-06-12"]
    d13 = ctx["tree"][0]
    assert d13.cost == 11.0
    assert d13.folders[0].project == "proj"
    assert [s.session_id for s in d13.folders[0].rows] == ["s2", "s1"]   # 비용 내림차순


def test_history_context_sort_date_asc(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _seed_history(conn)
    ctx = history_context(conn, _ANCHOR_613, "", "date_asc", now_kst=_NOW_613)
    assert [d.date for d in ctx["tree"]] == ["2026-06-12", "2026-06-13"]


def test_history_context_sort_day_cost(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _seed_history(conn)
    ctx = history_context(conn, _ANCHOR_613, "", "day_cost", now_kst=_NOW_613)
    assert [d.date for d in ctx["tree"]] == ["2026-06-13", "2026-06-12"]   # 소계 11 > 1


def test_history_context_nav_and_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _seed_history(conn)
    ctx = history_context(conn, _ANCHOR_613, "claude", "date_desc", now_kst=_NOW_613)
    assert ctx["provider"] == "claude"
    assert ctx["period_label"] == "2026-06"
    assert ctx["anchor"] == "2026-06-13"
    assert ctx["prev_anchor"] == "2026-05-31"
    assert ctx["next_anchor"] == "2026-07-01"
    assert ctx["has_next"] is False


def test_history_context_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    ctx = history_context(connect(":memory:"), _ANCHOR_613, "", "date_desc", now_kst=_NOW_613)
    assert ctx["count"] == 0 and ctx["total"] == 0.0 and ctx["tree"] == []
    assert ctx["last_ts"] is None


# ─── by_model ────────────────────────────────────────────────────────────────


def test_by_model_aggregates_and_sorts():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", model="claude-opus-4-8",
         ts="2026-06-10T10:00:00Z", cost_usd=20.0, input_tokens=100, output_tokens=30,
         cache_creation=5, cache_read=40)
    _msg(conn, dedup_key="b", session_id="s2", model="claude-haiku-4-5",
         ts="2026-06-11T10:00:00Z", cost_usd=4.0, input_tokens=10, output_tokens=2)
    _msg(conn, dedup_key="c", session_id="s1", model="claude-opus-4-8",
         ts="2026-06-12T10:00:00Z", cost_usd=10.0, input_tokens=50, cache_read=10)
    start, nxt = month_bounds(datetime(2026, 6, 15, tzinfo=KST))
    rows = by_model(conn, "claude", start, nxt)
    # 비용순: opus(30) 먼저, haiku(4)
    assert rows[0].model == "claude-opus-4-8"
    assert rows[0].cost == 30.0
    assert rows[0].sessions == 1          # s1 한 세션
    assert rows[0].cache_read == 50       # 40 + 10
    assert rows[1].model == "claude-haiku-4-5"


def test_by_model_excludes_other_months_and_providers():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", model="m1", provider="claude", ts="2026-05-30T10:00:00Z", cost_usd=9.0)
    _msg(conn, dedup_key="b", model="m1", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=3.0)
    _msg(conn, dedup_key="c", model="g", provider="codex", ts="2026-06-10T10:00:00Z", cost_usd=5.0)
    start, nxt = month_bounds(datetime(2026, 6, 15, tzinfo=KST))
    rows = by_model(conn, "claude", start, nxt)
    assert len(rows) == 1
    assert rows[0].cost == 3.0            # 5월·codex 제외


# ─── by_dimension ────────────────────────────────────────────────────────────


def test_by_dimension_skill_groups_with_null_bucket():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=10.0, attribution_skill="brainstorming")
    _msg(conn, dedup_key="b", ts="2026-06-11T10:00:00Z", cost_usd=4.0, attribution_skill="brainstorming")
    _msg(conn, dedup_key="c", ts="2026-06-12T10:00:00Z", cost_usd=2.0, attribution_skill=None)
    start, nxt = month_bounds(datetime(2026, 6, 15, tzinfo=KST))
    rows = by_dimension(conn, "claude", start, nxt, "skill")
    assert [r.key for r in rows] == ["brainstorming", None]   # 비용 내림차순, NULL 버킷 포함
    assert rows[0].cost == 14.0


def test_by_dimension_branch_and_range_filter():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-08T00:00:00Z", cost_usd=5.0, git_branch="main")     # KST 6/8 (주 안)
    _msg(conn, dedup_key="b", ts="2026-06-20T00:00:00Z", cost_usd=9.0, git_branch="main")     # 주 밖
    start, nxt, _ = period_bounds("week", datetime(2026, 6, 13, tzinfo=KST))
    rows = by_dimension(conn, "claude", start, nxt, "branch")
    assert len(rows) == 1
    assert rows[0].key == "main" and rows[0].cost == 5.0


def test_by_dimension_empty_string_folds_into_null_bucket():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=3.0, git_branch="")
    start, nxt = month_bounds(datetime(2026, 6, 15, tzinfo=KST))
    rows = by_dimension(conn, "claude", start, nxt, "branch")
    assert rows[0].key is None and rows[0].cost == 3.0    # "" → None 버킷


def test_by_dimension_model_matches_by_model_wrapper():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", model="claude-opus-4-8", ts="2026-06-10T10:00:00Z",
         cost_usd=20.0, input_tokens=100, cache_read=40)
    _msg(conn, dedup_key="b", model="claude-haiku-4-5", ts="2026-06-11T10:00:00Z", cost_usd=4.0)
    start, nxt = month_bounds(datetime(2026, 6, 15, tzinfo=KST))
    dim_rows = by_dimension(conn, "claude", start, nxt, "model")
    model_rows = by_model(conn, "claude", start, nxt)
    assert [r.key for r in dim_rows] == [m.model for m in model_rows]
    assert [r.cost for r in dim_rows] == [m.cost for m in model_rows]


# ─── sidechain_split ─────────────────────────────────────────────────────────


def test_sidechain_split_parent_and_sub():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=8.0, is_sidechain=0)
    _msg(conn, dedup_key="b", ts="2026-06-11T10:00:00Z", cost_usd=2.0, is_sidechain=1)
    start, nxt = month_bounds(datetime(2026, 6, 15, tzinfo=KST))
    sp = sidechain_split(conn, "claude", start, nxt)
    assert sp.parent_cost == 8.0
    assert sp.sub_cost == 2.0
    assert sp.total_cost == 10.0
    assert sp.sub_share == 20.0       # 2 / 10 * 100


def test_sidechain_split_empty_is_zero():
    conn = connect(":memory:")
    start, nxt = month_bounds(datetime(2026, 6, 15, tzinfo=KST))
    sp = sidechain_split(conn, "claude", start, nxt)
    assert sp.total_cost == 0.0 and sp.sub_share == 0.0


# ─── dimension_context ───────────────────────────────────────────────────────


def test_dimension_context_model_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", model="claude-opus-4-8",
         ts="2026-06-10T10:00:00Z", cost_usd=8.0)
    _msg(conn, dedup_key="b", session_id="s2", model="claude-haiku-4-5",
         ts="2026-06-10T10:00:00Z", cost_usd=2.0)
    ctx = dimension_context(conn, _ANCHOR_613, "", dim="model", now_kst=_NOW_613)
    assert ctx["active_nav"] == "analysis"
    assert ctx["dim"] == "model" and ctx["dim_label"] == "모델"
    assert ctx["total"] == 10.0
    top = ctx["rows"][0]
    assert top["key"] == "claude-opus-4-8"
    assert top["share"] == 80.0
    assert ctx["claude_only"] is False
    assert ctx["split"].total_cost == 10.0


def test_dimension_context_skill_null_bucket_and_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=5.0, attribution_skill=None)
    ctx = dimension_context(conn, _ANCHOR_613, "", dim="skill", now_kst=_NOW_613)
    assert ctx["dim_label"] == "스킬" and ctx["claude_only"] is True
    assert ctx["rows"][0]["key"] == "(미귀속)"      # NULL → 미귀속 라벨


def test_dimension_context_bad_dim_falls_back_to_model(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    ctx = dimension_context(conn, _ANCHOR_613, "", dim="evil", now_kst=_NOW_613)
    assert ctx["dim"] == "model"


# ─── build_date_tree: 날짜→폴더→세션 2단 그룹핑 ───────────────────────────────


def _dsr(date, sid, project, cost, msgs=1, cache_read=0, cache_den=0,
         summary=None, provider="claude", is_continued=False, cache_miss=False, label=None):
    return DaySessionRow(
        date=date, session_id=sid, provider=provider, summary=summary,
        project=project, label=label, cost=cost, msgs=msgs,
        cache_ratio=round(cache_read / cache_den, 4) if cache_den else 0.0,
        cache_read=cache_read, cache_den=cache_den,
        is_continued=is_continued, cache_miss=cache_miss)


def test_build_date_tree_groups_date_folder_session():
    rows = [
        _dsr("2026-06-13", "s1", "/a/tokenomy", 2.0, msgs=48, cache_read=78, cache_den=100, summary="세션표 칸 추가"),
        _dsr("2026-06-13", "s2", "/a/tokenomy", 0.2, msgs=7, cache_read=65, cache_den=100, summary="캐시 분석"),
        _dsr("2026-06-13", "s3", "/b/project-b", 0.2, msgs=5, cache_read=40, cache_den=100, summary="리팩터 검토"),
        _dsr("2026-06-12", "s1", "/a/tokenomy", 1.3, msgs=9, cache_read=40, cache_den=100, summary="세션표 칸 추가"),
    ]
    tree = build_date_tree(rows, "date_desc")
    assert [d.date for d in tree] == ["2026-06-13", "2026-06-12"]
    d13 = tree[0]
    assert d13.weekday == "토"                         # 2026-06-13 = 토
    assert d13.cost == 2.4 and d13.msgs == 60
    assert [f.project for f in d13.folders] == ["/a/tokenomy", "/b/project-b"]   # 폴더 비용 내림차순
    tok = d13.folders[0]
    assert tok.cost == 2.2 and tok.msgs == 55
    assert [s.session_id for s in tok.rows] == ["s1", "s2"]                 # 세션 비용 내림차순


def test_build_date_tree_weighted_not_simple_average():
    rows = [
        _dsr("2026-06-13", "s1", "/p", 1.0, cache_read=90, cache_den=100),    # 0.9
        _dsr("2026-06-13", "s2", "/p", 1.0, cache_read=10, cache_den=1000),   # 0.01
    ]
    f = build_date_tree(rows, "date_desc")[0].folders[0]
    assert f.cache_ratio == round(100 / 1100, 4)       # 가중 0.0909 (단순평균 0.455 아님)


def test_build_date_tree_preview_top_summaries_by_cost():
    rows = [
        _dsr("2026-06-13", "s1", "/p", 3.0, summary="비싼 작업"),
        _dsr("2026-06-13", "s2", "/p", 1.0, summary="싼 작업"),
    ]
    assert build_date_tree(rows, "date_desc")[0].preview.startswith("비싼 작업")


def test_build_date_tree_preview_fallback_when_no_summary():
    rows = [_dsr("2026-06-13", "s1", "/p", 1.0, summary=None)]
    assert build_date_tree(rows, "date_desc")[0].preview == "(요약 없음)"


def test_build_date_tree_sorts():
    rows = [
        _dsr("2026-06-11", "s1", "/p", 1.0),
        _dsr("2026-06-13", "s2", "/p", 5.0),
        _dsr("2026-06-12", "s3", "/p", 9.0),
    ]
    assert [d.date for d in build_date_tree(rows, "date_desc")] == ["2026-06-13", "2026-06-12", "2026-06-11"]
    assert [d.date for d in build_date_tree(rows, "date_asc")] == ["2026-06-11", "2026-06-12", "2026-06-13"]
    assert [d.date for d in build_date_tree(rows, "day_cost")] == ["2026-06-12", "2026-06-13", "2026-06-11"]  # 소계 9>5>1


def test_build_date_tree_zero_den_cache_zero():
    f = build_date_tree([_dsr("2026-06-13", "s1", "/p", 1.0, cache_read=0, cache_den=0)], "date_desc")[0].folders[0]
    assert f.cache_ratio == 0.0


def test_build_date_tree_unknown_project():
    f = build_date_tree([_dsr("2026-06-13", "s1", None, 1.0)], "date_desc")[0].folders[0]
    assert f.project == "(unknown)"


# ─── Task 8: history_context/dimension_context 주/월 기간 + 사용자 지정 구간 ──────

def test_history_context_week_period(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="in", session_id="s1", ts="2026-06-09T01:00:00Z", cost_usd=2.0)  # 6/9 주 안
    _msg(conn, dedup_key="out", session_id="s2", ts="2026-06-20T01:00:00Z", cost_usd=9.0)  # 주 밖
    ctx = history_context(conn, _ANCHOR_613, "", "date_desc", now_kst=_NOW_613, period="week")
    assert ctx["period"] == "week"
    assert ctx["total"] == 2.0                       # 6/8~6/14 주만
    assert ctx["period_label"] == "2026-06-08 ~ 06-14"


def test_history_context_custom_range(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-12T01:00:00Z", cost_usd=3.0)
    _msg(conn, dedup_key="b", session_id="s2", ts="2026-06-30T01:00:00Z", cost_usd=7.0)
    _msg(conn, dedup_key="c", session_id="s3", ts="2026-06-05T01:00:00Z", cost_usd=5.0)  # 범위 밖
    ctx = history_context(conn, _ANCHOR_613, "", "date_desc", now_kst=_NOW_613,
                          start="2026-06-12", end="2026-06-30")
    assert ctx["total"] == 10.0                      # 6/12~6/30 (6/5 제외)
    assert ctx["period_label"] == "2026-06-12 ~ 2026-06-30"
    assert ctx["custom"] is True


def test_history_context_invalid_range_falls_back_to_month(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-12T01:00:00Z", cost_usd=3.0)
    # start>end → 폴백(월간)
    ctx = history_context(conn, _ANCHOR_613, "", "date_desc", now_kst=_NOW_613,
                          start="2026-06-30", end="2026-06-01")
    assert ctx["custom"] is False
    assert ctx["period_label"] == "2026-06"


def test_dimension_context_week_period(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", model="claude-opus-4-8",
         ts="2026-06-09T10:00:00Z", cost_usd=8.0)                       # 주 안
    _msg(conn, dedup_key="b", session_id="s2", model="claude-haiku-4-5",
         ts="2026-06-20T10:00:00Z", cost_usd=2.0)                       # 주 밖
    ctx = dimension_context(conn, _ANCHOR_613, "", dim="model", now_kst=_NOW_613, period="week")
    assert ctx["total"] == 8.0
    assert ctx["period_label"] == "2026-06-08 ~ 06-14"


# ─── Task 5: 집계가 user_turns 사용 ──────────────────────────────────────────


def _claude_rec(msg_id, session_id="s1", **kw):
    return UsageRecord(
        provider="claude", session_id=session_id, cwd="/p",
        ts="2026-06-11T10:00:00Z", model="claude-opus-4-8",
        input_tokens=1000, output_tokens=0, cache_creation=0, cache_read=0,
        message_id=msg_id,
    )


_PRICING = {"match": [{"contains": "opus", "provider": "claude",
                       "input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50}]}


def test_by_session_msgs_uses_user_turns():
    conn = connect(":memory:")
    # 한 세션에 메시지 행 3개(어시스턴트 응답), 사용자 턴은 2
    ingest_records(conn, [_claude_rec("a"), _claude_rec("b"), _claude_rec("c")], _PRICING)
    conn.execute("UPDATE sessions SET user_turns=2 WHERE session_id='s1'")
    rows = by_session(conn, None, datetime(2026, 6, 11, tzinfo=KST))
    assert len(rows) == 1
    assert rows[0].msgs == 2   # 행 수(3)가 아니라 사용자 턴(2)


def test_session_detail_msgs_uses_user_turns():
    conn = connect(":memory:")
    ingest_records(conn, [_claude_rec("a"), _claude_rec("b")], _PRICING)
    conn.execute("UPDATE sessions SET user_turns=1 WHERE session_id='s1'")
    d = session_detail(conn, "s1")
    assert d is not None
    assert d.msgs == 1


def test_session_detail_null_user_turns_falls_back_to_zero():
    conn = connect(":memory:")
    ingest_records(conn, [_claude_rec("a")], _PRICING)  # user_turns 미설정(NULL)
    d = session_detail(conn, "s1")
    assert d is not None
    assert d.msgs == 0   # 세션은 노출되되 카운트는 0


def test_by_day_session_msgs_uses_user_turns():
    conn = connect(":memory:")
    # 한 세션에 메시지 행 3개, 사용자 턴은 2 → by_day_session이 행 수(3)가 아닌 session_day_turns(2)를 반환해야 함
    ingest_records(
        conn,
        [_claude_rec("a"), _claude_rec("b"), _claude_rec("c")],
        _PRICING,
    )
    # _claude_rec ts는 "2026-06-11T10:00:00Z" (KST 6/11 19:00) → KST 날짜 2026-06-11
    conn.execute("INSERT INTO session_day_turns (session_id, day, turns) VALUES ('s1','2026-06-11',2)")
    conn.commit()
    # _JUN(6/1~7/1) 범위 안
    rows = by_day_session(conn, None, start=_JUN[0], nxt=_JUN[1])
    assert len(rows) == 1
    assert rows[0].msgs == 2   # 행 수(3)가 아니라 session_day_turns(2)


def test_by_day_session_per_day_counts():
    from datetime import datetime
    conn = connect(":memory:")
    # 같은 세션이 두 KST 날짜에 걸침: 01:00Z→06-11, 16:00Z→06-12
    r1 = UsageRecord(provider="claude", session_id="s1", cwd="/p", ts="2026-06-11T01:00:00Z",
                     model="claude-opus-4-8", input_tokens=1000, output_tokens=0,
                     cache_creation=0, cache_read=0, message_id="a")
    r2 = UsageRecord(provider="claude", session_id="s1", cwd="/p", ts="2026-06-11T16:00:00Z",
                     model="claude-opus-4-8", input_tokens=1000, output_tokens=0,
                     cache_creation=0, cache_read=0, message_id="b")
    ingest_records(conn, [r1, r2], _PRICING)
    conn.execute("INSERT INTO session_day_turns (session_id, day, turns) VALUES ('s1','2026-06-11',2),('s1','2026-06-12',1)")
    rows = by_day_session(conn, None, start=datetime(2026, 6, 1, tzinfo=KST), nxt=datetime(2026, 7, 1, tzinfo=KST))
    by_date = {r.date: r.msgs for r in rows}
    assert by_date == {"2026-06-11": 2, "2026-06-12": 1}   # 날짜별 정확 카운트(행 수 아님)


# ─── stacked_trend: provider별 누적 → 스택 밴드 경계 ───────────────────────────

def test_stacked_trend_two_providers():
    claude = [DayPoint(1, 5.0), DayPoint(2, 8.0), DayPoint(3, None)]
    codex = [DayPoint(1, 2.0), DayPoint(2, 3.0), DayPoint(3, None)]
    bands = stacked_trend([("claude", claude), ("codex", codex)])
    assert [b["provider"] for b in bands] == ["claude", "codex"]
    assert bands[0]["cum"] == [5.0, 8.0, None]
    assert bands[0]["top"] == [5.0, 8.0, None]          # 첫 밴드 top = cum
    assert bands[1]["cum"] == [2.0, 3.0, None]
    assert bands[1]["top"] == [7.0, 11.0, None]         # running sum(아래 밴드까지)
    # 불변식: 마지막 밴드 top == provider별 cum 합
    assert bands[-1]["top"][0] == 5.0 + 2.0
    assert bands[-1]["top"][1] == 8.0 + 3.0


def test_stacked_trend_single_provider_passthrough():
    claude = [DayPoint(1, 5.0), DayPoint(2, None)]
    bands = stacked_trend([("claude", claude)])
    assert bands[0]["cum"] == [5.0, None]
    assert bands[0]["top"] == [5.0, None]               # 단일 밴드: top == cum


def test_stacked_trend_future_none_propagates():
    # 어떤 날 한 provider가 None이면 그 위 밴드 top도 None
    a = [DayPoint(1, 1.0), DayPoint(2, None)]
    b = [DayPoint(1, 2.0), DayPoint(2, None)]
    bands = stacked_trend([("a", a), ("b", b)])
    assert bands[1]["top"] == [3.0, None]


def test_stacked_trend_empty():
    assert stacked_trend([]) == []


def test_stacked_trend_asymmetric_none():
    # 한 provider만 None이어도(아래 밴드 None) 위 밴드 top은 None 전파
    a = [DayPoint(1, 5.0), DayPoint(2, None)]   # A는 2일에 None
    b = [DayPoint(1, 2.0), DayPoint(2, 3.0)]    # B는 2일에 데이터
    bands = stacked_trend([("a", a), ("b", b)])
    assert bands[0]["cum"][1] is None
    assert bands[0]["top"][1] is None
    assert bands[1]["top"] == [7.0, None]


def test_overview_context_trend_series_stacks_providers(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"budget": {"claude": 100, "codex": 40}}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="c1", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=10.0)
    _msg(conn, dedup_key="x1", provider="codex", ts="2026-06-11T10:00:00Z", cost_usd=4.0)
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)   # 6/15, budget_start 미설정 → 6/1 시작
    assert "daily_actual" not in ctx
    series = ctx["trend_series"]
    assert [s["label"] for s in series] == ["Claude", "Codex"]
    assert series[0]["color"] == "#cc785c"      # Claude 코랄
    assert series[1]["color"] == "#5db8a6"      # Codex teal
    # x축 6/1~6/30(30일). 6/10 → idx9, 6/11 → idx10
    assert series[0]["cum"][9] == 10.0          # Claude 누적
    assert series[1]["cum"][10] == 4.0          # Codex 누적
    assert series[1]["top"][10] == 14.0         # 스택 top = claude+codex 누적
    assert ctx["trend_totals"][10] == 14.0      # 합계
    # 미래(6/16~) None
    assert series[0]["cum"][-1] is None
    assert ctx["trend_totals"][-1] is None


def test_token_composition_shares_are_percent():
    conn = connect(":memory:")
    conn.execute(
        "INSERT INTO messages(dedup_key,provider,session_id,ts,model,"
        "input_tokens,output_tokens,cache_creation,cache_read,cost_usd,priced) "
        "VALUES('k','claude','s','2026-06-05T00:00:00Z','claude-opus-4-8',10,20,30,40,1.0,1)",
    )
    conn.commit()
    start, nxt = month_bounds(NOW)
    tc = token_composition(conn, None, start, nxt)
    assert tc.input_tokens == 10
    assert tc.output_tokens == 20
    assert tc.cache_creation == 30
    assert tc.cache_read == 40
    assert tc.total == 100
    assert tc.output_pct == 20.0       # 퍼센트값(0.2 아님)
    assert tc.cache_read_pct == 40.0


def test_token_composition_empty_zero():
    conn = connect(":memory:")
    start, nxt = month_bounds(NOW)
    tc = token_composition(conn, None, start, nxt)
    assert tc.total == 0
    assert tc.input_pct == 0.0


def test_insights_cache_rebuild_unique_sessions():
    conn = connect(":memory:")
    # 세션 s: 6/4 첫 등장(캐시 충분), 6/6·6/7 이어짐(캐시 빈약 → 재구축, 2일)
    _insert(conn, "2026-06-04T00:00:00Z", 1.0, session="s", cache_read=1000, input_t=10)
    _insert(conn, "2026-06-06T00:00:00Z", 1.0, session="s", cache_read=0, input_t=1000)
    _insert(conn, "2026-06-07T00:00:00Z", 1.0, session="s", cache_read=0, input_t=1000)
    cards = insights(conn, NOW, None)
    # "캐시 재구축"으로 매칭(기존 캐시활용 경고는 "컨텍스트 재구축"이라 미충돌)
    rebuild = [c for c in cards if "캐시 재구축" in c.text]
    assert len(rebuild) == 1
    assert "1개 세션" in rebuild[0].text   # 2일 miss여도 고유 세션 1


def test_insights_no_rebuild_for_first_day_only():
    conn = connect(":memory:")
    # 첫 등장일만 — 캐시 빈약해도 is_continued=False라 제외
    _insert(conn, "2026-06-06T00:00:00Z", 1.0, session="s", cache_read=0, input_t=1000)
    cards = insights(conn, NOW, None)
    assert not any("캐시 재구축" in c.text for c in cards)


COV_PRICING = {"match": [
    {"contains": "opus", "provider": "claude", "input": 15.0, "output": 75.0,
     "cache_write": 18.75, "cache_read": 1.50},
    {"contains": "gpt-5", "provider": "codex", "input": 1.25, "output": 10.0,
     "cache_write": 0.0, "cache_read": 0.125},
]}
TS = "2026-06-05T00:00:00Z"


def test_pricing_coverage_empty_db_safe():
    conn = connect(":memory:")
    cov = pricing_coverage(conn, COV_PRICING)
    assert cov.total_tokens == 0
    assert cov.unpriced_count == 0
    assert cov.models == []


def test_pricing_coverage_ok_unpriced_suspect():
    conn = connect(":memory:")
    _insert(conn, TS, 1.0, model="claude-opus-4-8", input_t=100)          # ok
    _insert(conn, TS, 0.0, model="gpt-foo", input_t=50, priced=0)         # unpriced(매칭 없음)
    _insert(conn, TS, 0.0, model="gpt-5.5", input_t=50, provider="codex") # suspect(gpt-5 부분일치)
    cov = pricing_coverage(conn, COV_PRICING)
    by_model = {m.model: m for m in cov.models}
    assert by_model["claude-opus-4-8"].status == "ok"
    assert by_model["claude-opus-4-8"].matched_contains == "opus"
    assert by_model["gpt-foo"].status == "unpriced"
    assert by_model["gpt-foo"].matched_contains is None
    assert by_model["gpt-5.5"].status == "suspect"
    assert cov.unpriced_count == 1
    assert cov.suspect_count == 1
    assert cov.total_tokens == 200
    assert abs(cov.unpriced_token_share - 0.25) < 1e-9   # 50/200
    assert abs(sum(m.token_share for m in cov.models) - 1.0) < 1e-9


def test_pricing_coverage_coarse_match():
    conn = connect(":memory:")
    _insert(conn, TS, 1.0, model="claude-opus-4-7", input_t=10, session="a")
    _insert(conn, TS, 1.0, model="claude-opus-4-8", input_t=10, session="b")
    cov = pricing_coverage(conn, COV_PRICING)
    assert cov.coarse_contains == ["opus"]   # 한 항목에 2개 distinct 모델


def test_insights_unpriced_warning_uses_coverage_species_and_share():
    conn = connect(":memory:")
    _insert(conn, TS, 1.0, model="claude-opus-4-8", input_t=100)         # priced
    _insert(conn, TS, 0.0, model="gpt-foo", input_t=100, priced=0)       # unpriced
    cov = pricing_coverage(conn, COV_PRICING)
    cards = insights(conn, NOW, None, cov=cov)
    texts = [c.text for c in cards]
    # 모델 "종" 수(1) + 토큰 비중(50%) 형태, "설정에서 확인" 포함
    assert any("미식별 1종" in t and "50%" in t and "설정" in t for t in texts)


def test_insights_no_unpriced_warning_when_coverage_clean():
    conn = connect(":memory:")
    _insert(conn, TS, 1.0, model="claude-opus-4-8", input_t=100)
    cov = pricing_coverage(conn, COV_PRICING)
    cards = insights(conn, NOW, None, cov=cov)
    assert not any("미식별" in c.text for c in cards)


# --- 영업일 헬퍼 business_days_between (주말 제외, 반열린 구간 [start, end)) -----


def test_business_days_between_full_week():
    # 6/15(월) ~ 6/22(월) [start, end) → 월~금 5영업일(다음 월요일 제외)
    assert business_days_between(date(2026, 6, 15), date(2026, 6, 22)) == 5


def test_business_days_between_single_weekday():
    # 6/15(월) ~ 6/16(화) → 월요일 1영업일
    assert business_days_between(date(2026, 6, 15), date(2026, 6, 16)) == 1


def test_business_days_between_skips_weekend():
    # 6/19(금) ~ 6/22(월) → 금요일만 1(토·일 제외)
    assert business_days_between(date(2026, 6, 19), date(2026, 6, 22)) == 1


def test_business_days_between_same_day_zero():
    assert business_days_between(date(2026, 6, 15), date(2026, 6, 15)) == 0


def test_business_days_between_weekend_only_zero():
    # 6/20(토) ~ 6/22(월) → 토·일만, 0영업일
    assert business_days_between(date(2026, 6, 20), date(2026, 6, 22)) == 0


def test_business_days_between_negative_range_zero():
    # end < start → 음수 누적 없이 0
    assert business_days_between(date(2026, 6, 22), date(2026, 6, 15)) == 0


def test_add_business_days_within_week():
    # 6/15(월) + 3영업일 = 화·수·목 → 6/18(목)
    assert add_business_days(date(2026, 6, 15), 3) == date(2026, 6, 18)


def test_add_business_days_skips_weekend():
    # 6/19(금) + 1영업일 = 토·일 건너뛴 6/22(월)
    assert add_business_days(date(2026, 6, 19), 1) == date(2026, 6, 22)


def test_add_business_days_zero_returns_same():
    assert add_business_days(date(2026, 6, 15), 0) == date(2026, 6, 15)


# --- official_view (Claude 버킷 + Codex 월간 + 예측 렌즈) ---


def _ob(key, kind, used_usd, limit_usd, raw="r", unit="usd", util=0.0, resets=None):
    return OfficialBucket(
        bucket_key=key, raw_key=raw, bucket_kind=kind, label=key, native_unit=unit,
        used_native=used_usd, limit_native=limit_usd,
        remaining_native=(limit_usd - used_usd) if limit_usd else None,
        used_usd=used_usd, limit_usd=limit_usd,
        remaining_usd=(limit_usd - used_usd) if limit_usd else None,
        utilization=util, resets_at=resets,
    )


def test_official_view_no_data_status():
    conn = connect(":memory:")
    v = official_view(conn, "claude", NOW, 0.04)
    assert isinstance(v, OfficialView)
    assert v.status == "no_data"
    assert v.buckets == []


def test_official_view_claude_monthly_period():
    conn = connect(":memory:")
    insert_official_buckets(
        conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
        buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                 _ob("event", "event_credit", 125.0, 500.0, raw="cinder")],
        created_at="2026-06-10T09:00:00+09:00",
    )
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.status == "ok"
    assert v.period_used_usd == 30.0 and v.period_limit_usd == 100.0
    assert {b["bucket_key"] for b in v.buckets} == {"monthly", "event"}
    # 월 버킷 resets_at은 다음 달 경계로 채워짐
    monthly = next(b for b in v.buckets if b["bucket_key"] == "monthly")
    assert monthly["resets_at"].startswith("2026-07-01")


def test_official_view_codex_monthly_only_no_weekly():
    # 추정 주간 게이지 제거(ADR 0012) — 로컬 Codex 사용이 있어도 월간 버킷만 노출.
    conn = connect(":memory:")
    _insert(conn, "2026-06-09T01:00:00Z", 12.0, provider="codex", session="a")
    insert_official_buckets(
        conn, provider="codex", fetched_at="2026-06-10T09:00:00+09:00",
        buckets=[_ob("monthly", "codex_monthly", 20.0, 80.0, raw="individual_limit",
                     unit="credit", util=25.0)],
        created_at="2026-06-10T09:00:00+09:00",
    )
    now = datetime(2026, 6, 11, 12, 0, tzinfo=KST)
    v = official_view(conn, "codex", now, 0.04)
    assert v.period_used_usd == 20.0 and v.period_limit_usd == 80.0  # 월간(공식)
    assert not hasattr(v, "weekly_used_usd")    # 주간 필드 제거됨
    assert {b["bucket_key"] for b in v.buckets} == {"monthly"}


def test_official_view_lens_uses_official_rate():
    # 카드 고스트도 공식 기울기(ADR 0015 D3) — 단일 스냅샷 → 공식 월초누적, 로컬 거액은 무시.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 40.0, 100.0, raw="spend")],
                            created_at="x")
    _insert(conn, "2026-06-05T01:00:00Z", 999.0, provider="claude", session="a")  # 로컬 — 무시돼야
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.lens is not None
    assert v.lens.daily_rate_usd == 5.0   # 공식 월초누적 40/8영업일(6/1~6/10), 로컬 999 무시
    assert v.active_key == "monthly"


def test_official_view_codex_lens_uses_official_rate():
    # Codex 카드 고스트도 공식 기울기 — 로컬은 무시.
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T01:00:00Z", 999.0, provider="codex", session="a")  # 로컬 — 무시
    insert_official_buckets(conn, provider="codex", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "codex_monthly", 40.0, 80.0, raw="individual_limit",
                                         unit="credit", util=50.0)],
                            created_at="x")
    v = official_view(conn, "codex", NOW, 0.04)
    assert v.lens is not None
    assert v.lens.daily_rate_usd == 5.0   # 공식 월초누적 40/8


def test_lens_none_rate_without_official_used():
    # 공식 used=0 → (a)트레일링 불가·(b)월초누적 0 → 고스트 기울기 None(로컬 있어도 무시).
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 0.0, 100.0, raw="spend")],
                            created_at="x")
    _insert(conn, "2026-06-05T01:00:00Z", 100.0, provider="claude", session="a")  # 로컬 — 무시
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.lens is not None
    assert v.lens.daily_rate_usd is None


def test_official_view_active_bucket_largest_diff():
    conn = connect(":memory:")
    # Claude: event + monthly 둘 다 활성. monthly의 최근 차분이 더 커서 active=monthly(이벤트가 tie-break 우선임에도)
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-09T09:00:00+09:00",
                            buckets=[_ob("event", "event_credit", 100.0, 500.0, raw="cinder"),
                                     _ob("monthly", "monthly_limit", 10.0, 100.0, raw="spend")],
                            created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("event", "event_credit", 102.0, 500.0, raw="cinder"),   # +2
                                     _ob("monthly", "monthly_limit", 40.0, 100.0, raw="spend")],  # +30
                            created_at="x")
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.active_key == "monthly"   # 최근 차분 30 > 2 → tie-break(event 우선) 무시하고 monthly


# ─── Task 3: 예산 분리 신규 테스트 ─────────────────────────────────────────────


def _conn_with_official_codex_monthly(limit_usd: float):
    """공식 Codex 월간 버킷이 있는 in-memory DB. official_view 테스트용."""
    conn = connect(":memory:")
    insert_official_buckets(
        conn, provider="codex", fetched_at="2026-06-10T09:00:00+09:00",
        buckets=[_ob("monthly", "codex_monthly", 20.0, limit_usd, raw="individual_limit",
                     unit="credit", util=0.0)],
        created_at="2026-06-10T09:00:00+09:00",
    )
    return conn


def test_official_view_no_budget_arg():
    conn = _conn_with_official_codex_monthly(limit_usd=80)
    ov = official_view(conn, "codex", NOW, 0.04)
    assert ov.period_limit_usd == 80.0   # 공식 월간 한도(수동 예산 인자 없음)


def test_daily_series_calendar_month():
    conn = connect(":memory:")
    _insert(conn, "2026-06-01T00:00:00Z", 1.0, session="s1")
    _insert(conn, "2026-06-10T00:00:00Z", 2.0, session="s2")
    pts = daily_series(conn, "claude", _NOW_STATUS)   # 6/1부터 달력 월 기준
    assert pts[0].day == 1                           # 달력 월 1일 시작
    assert pts[14].day == 15                         # _NOW_STATUS.day = 15
    assert pts[14].cumulative_cost == 3.0            # 1.0 + 2.0 누적
    assert pts[-1].day == 30 and pts[-1].cumulative_cost is None  # 미래 → None


def test_month_spend_sums_current_month():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 4.0, session="jun")
    _insert(conn, "2026-05-30T00:00:00Z", 9.0, session="may")   # 다른 달 제외
    assert month_spend(conn, "claude", NOW) == 4.0


def test_insights_no_budget_overrun_card():
    conn = connect(":memory:")
    cards = insights(conn, _NOW_STATUS, None, cov=None)
    assert any(c.level == "info" for c in cards)   # 빈 신호 placeholder
    # bd 인자 없음 — TypeError 안 나야 함(시그니처 확인)
    texts = " ".join(c.text for c in cards)
    assert "한도 초과" not in texts and "월말" not in texts


# --- 통합 월말 전망(combined_forecast) ---


def _fc_views(conn, now):
    return [official_view(conn, "claude", now, 0.04),
            official_view(conn, "codex", now, 0.04)]


def test_combined_forecast_empty_pool_none():
    # 공식 한도 전무 → None(히어로 숨김)
    conn = connect(":memory:")
    assert combined_forecast(conn, _fc_views(conn, NOW), NOW) is None


def test_combined_forecast_pool_sums_used_and_limit():
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend")],
                            created_at="x")
    insert_official_buckets(conn, provider="codex", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "codex_monthly", 20.0, 80.0,
                                         raw="individual_limit", unit="credit", util=25.0)],
                            created_at="x")
    fc = combined_forecast(conn, _fc_views(conn, NOW), NOW)
    assert set(fc.providers) == {"claude", "codex"}
    assert fc.used_usd == 50.0 and fc.limit_usd == 180.0
    assert fc.remaining_usd == 130.0


def test_combined_forecast_surplus():
    # 엔터 기울기=공식(ADR 0015). 단일 스냅샷 → 월초누적 used40/8영업일=5/일 → 110<200 여유.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 40.0, 200.0, raw="spend")],
                            created_at="x")
    fc = combined_forecast(conn, _fc_views(conn, NOW), NOW)
    assert fc.providers == ["claude"]
    assert fc.daily_rate_usd == 5.0                 # 공식 월초누적: 40 / 8영업일(6/1~6/10)
    assert fc.bdays_remaining == 14
    assert fc.projected_used_usd == 110.0           # 40 + 5*14
    assert fc.projected_remaining_usd == 90.0       # 200 - 110 → 여유
    assert fc.exhaust_date is None
    assert fc.is_exhausted is False


def test_combined_forecast_shortfall_with_exhaust_date():
    # 공식 월초누적 used80/8영업일=10/일 → 80+10*14=220>100 부족, 소진 6/12.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 80.0, 100.0, raw="spend")],
                            created_at="x")
    fc = combined_forecast(conn, _fc_views(conn, NOW), NOW)
    assert fc.daily_rate_usd == 10.0
    assert fc.projected_used_usd == 220.0           # 80 + 10*14
    assert fc.projected_remaining_usd == -120.0     # 부족
    assert fc.exhaust_date == date(2026, 6, 12)     # ceil((100-80)/10)=2 영업일 후


def test_combined_forecast_insufficient_when_no_official_used():
    # 공식 used=0 → (a)트레일링 불가·(b)월초누적 0 → 기울기 None(위치만, 로컬 기울기 폐기).
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 0.0, 200.0, raw="spend")],
                            created_at="x")
    # 로컬 소비가 있어도 엔터 기울기엔 영향 없음(로컬 기울기 폐기, ADR 0015 D3).
    _insert(conn, "2026-06-05T01:00:00Z", 100.0, provider="claude", session="a")
    fc = combined_forecast(conn, _fc_views(conn, NOW), NOW)
    assert fc.daily_rate_usd is None                # 공식 소비 0 → 기울기 없음
    assert fc.projected_remaining_usd is None
    assert fc.remaining_usd == 200.0


def test_combined_forecast_official_slope_prefers_trailing_delta():
    # 윈도우 시작 전 베이스(5/20 used50) + 윈도우 내(6/10 used150) → 트레일링 델타 100/10영업일=10.
    # 월초누적이면 150/8≈18.75라 달라 — (a)트레일링이 (b)월초누적보다 우선임을 보인다.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-05-20T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 50.0, 500.0, raw="spend")],
                            created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 150.0, 500.0, raw="spend")],
                            created_at="x")
    fc = combined_forecast(conn, _fc_views(conn, NOW), NOW)
    assert fc.daily_rate_usd == 10.0                # 트레일링 100/10, 월초누적(150/8) 아님


def test_combined_forecast_already_exhausted():
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 100.0, 100.0, raw="spend")],
                            created_at="x")
    _insert(conn, "2026-06-05T01:00:00Z", 50.0, provider="claude", session="a")
    fc = combined_forecast(conn, _fc_views(conn, NOW), NOW)
    assert fc.is_exhausted is True
    assert fc.projected_used_usd is None            # 소진이면 전망 생략


# --- 풀 집계: 월간 + 포함 크레딧 합산(ADR 0004 갱신) ---


def test_official_view_pool_sums_monthly_and_credit():
    # 전망 풀 기여 = 월간 + opt-in 크레딧 합(ADR 0016: 크레딧은 큐레이션으로 풀 포함). 카드 게이지(period_*)는 월간만.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 500.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    v = official_view(conn, "claude", NOW, 0.04, is_pooled=_pool_with("cinder"))
    assert v.period_used_usd == 30.0 and v.period_limit_usd == 100.0   # 카드 게이지=월간만
    assert v.pool_used_usd == 155.0 and v.pool_limit_usd == 600.0      # 풀=월간+opt-in 크레딧


def test_official_view_pool_excludes_expired_credit():
    # 만료(resets_at 과거) 크레딧은 풀에서 제외(candidates stale 기준 공유).
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 500.0, raw="cinder",
                                         resets=datetime(2026, 5, 1, tzinfo=KST))],  # 과거 → 제외
                            created_at="x")
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.pool_used_usd == 30.0 and v.pool_limit_usd == 100.0


def test_official_view_pool_none_without_usd_limit():
    # USD 한도 버킷이 없으면(개인 구독 rate_window 등) 풀 기여 없음.
    conn = connect(":memory:")
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.pool_used_usd is None and v.pool_limit_usd is None


# ─── 버킷 큐레이션: event_credit opt-in 풀(ADR 0016) ─────────────────────────────


# 테스트용 is_pooled: 안정 키 + (opt-in한 raw_key) 풀 포함.
def _pool_with(*opted):
    return lambda p, rk, bk: bk in ("monthly_limit", "codex_monthly") or rk in opted


def test_official_view_event_credit_excluded_from_pool_by_default():
    # ADR 0016: 회전 코드네임 달러 크레딧은 기본 풀 제외(opt-in). 풀=월간만.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 25000.0, raw="amber_ladder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.pool_used_usd == 30.0 and v.pool_limit_usd == 100.0   # $25k 유령 천장 제외


def test_official_view_event_credit_pooled_when_opted_in():
    # is_pooled가 opt-in하면 진짜 크레딧을 풀에 합산.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 500.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    v = official_view(conn, "claude", NOW, 0.04, is_pooled=_pool_with("cinder"))
    assert v.pool_used_usd == 155.0 and v.pool_limit_usd == 600.0


def test_official_view_excluded_credit_still_shown_as_bucket():
    # 풀 제외돼도 게이지(buckets)에는 남는다(발견 신호). hidden은 views가 처리.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 25000.0, raw="amber_ladder")],
                            created_at="x")
    v = official_view(conn, "claude", NOW, 0.04)
    assert {b["bucket_key"] for b in v.buckets} == {"monthly", "event"}


def test_official_view_excluded_credit_not_active():
    # 풀 제외 크레딧은 active 버킷(렌즈 구동) 후보에서도 빠진다 — 차분이 더 커도 월간이 active.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-09T09:00:00+09:00",
                            buckets=[_ob("event", "event_credit", 100.0, 25000.0, raw="amber_ladder"),
                                     _ob("monthly", "monthly_limit", 10.0, 100.0, raw="spend")],
                            created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("event", "event_credit", 180.0, 25000.0, raw="amber_ladder"),  # +80
                                     _ob("monthly", "monthly_limit", 40.0, 100.0, raw="spend")],         # +30
                            created_at="x")
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.active_key == "monthly"   # event 차분(80)이 더 커도 풀 제외라 active 후보 아님


def test_pool_used_history_excludes_event_credit_by_default():
    # ADR 0016: 기본 풀 제외 → 누적 시계열은 월간만.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 25000.0, raw="amber_ladder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    assert pool_used_history(conn, "claude") == [(parse_ts("2026-06-10T09:00:00+09:00"), 30.0)]


def test_pool_used_history_includes_event_credit_when_opted_in():
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 500.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    hist = pool_used_history(conn, "claude", is_pooled=_pool_with("cinder"))
    assert hist == [(parse_ts("2026-06-10T09:00:00+09:00"), 155.0)]


def test_combined_forecast_includes_event_credit():
    # 통합 풀이 Claude opt-in 크레딧(실제 닳는 버킷)을 합산(ADR 0016). 오버리지($0/$100)만 보던 회귀 방지.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 0.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 500.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST))],
                            created_at="x")
    insert_official_buckets(conn, provider="codex", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "codex_monthly", 20.0, 80.0,
                                         raw="individual_limit", unit="credit", util=25.0)],
                            created_at="x")
    ip = _pool_with("cinder")
    views = [official_view(conn, "claude", NOW, 0.04, is_pooled=ip),
             official_view(conn, "codex", NOW, 0.04, is_pooled=ip)]
    fc = combined_forecast(conn, views, NOW, is_pooled=ip)
    assert fc.used_usd == 145.0      # 0 + 125 + 20
    assert fc.limit_usd == 680.0     # 100 + 500 + 80
    assert fc.remaining_usd == 535.0
    # per_provider도 풀(월간+크레딧) 합산을 반영
    claude = next(p for p in fc.per_provider if p["provider"] == "claude")
    assert claude["used_usd"] == 125.0 and claude["limit_usd"] == 600.0


# ─── 트레일링 소비속도(행동 속성): _trailing_business_days / trailing_window_spend ───
# 기울기 창을 월초 누적 → 오늘 포함 최근 weeks×7일로. 분모는 적응형(earliest-msg clamp).


def test_trailing_business_days_full_window_established():
    # 기성 사용자(창 시작 이전에도 데이터) → 창 전체 영업일 = 5×weeks = 10(기본 2주).
    # NOW=6/10, 2주 창 = [5/28, 6/10] 14일 → 영업일 정확히 10.
    conn = connect(":memory:")
    _insert(conn, "2026-05-01T00:00:00Z", 1.0)   # 창 이전 앵커 → earliest_msg 고정(풀 창)
    assert _trailing_business_days(conn, NOW, 2) == 10


def test_trailing_business_days_warmup_clamps_to_earliest():
    # 신규/복귀: 창 시작 이전 데이터 없음 → 분모를 최초메시지일로 clamp(존재 이전 일수 제외).
    # 6/5 첫 사용 → business_days(6/5, 6/11) = 4(금·월·화·수).
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T01:00:00Z", 10.0)
    assert _trailing_business_days(conn, NOW, 2) == 4


def test_trailing_business_days_empty_db_zero():
    # 모집단에 메시지 전무 → 0(→ rate None).
    conn = connect(":memory:")
    assert _trailing_business_days(conn, NOW, 2) == 0


def test_trailing_business_days_population_per_provider():
    # 분모 모집단 = spend와 동일 필터. codex 신규(6/8)면 codex 분모는 6/8로 clamp,
    # claude 오래됨(5/1)이면 풀 창. (분자/분모 모집단 일치)
    conn = connect(":memory:")
    _insert(conn, "2026-05-01T00:00:00Z", 1.0, provider="claude")
    _insert(conn, "2026-06-08T01:00:00Z", 5.0, provider="codex")
    assert _trailing_business_days(conn, NOW, 2, provider="codex") == 3    # 6/8,9,10
    assert _trailing_business_days(conn, NOW, 2, provider="claude") == 10  # 풀 창


def test_trailing_business_days_weeks_param_widens():
    # weeks=1 → 오늘 포함 7일 [6/4, 6/10] → 영업일 5.
    conn = connect(":memory:")
    _insert(conn, "2026-05-01T00:00:00Z", 1.0)
    assert _trailing_business_days(conn, NOW, 1) == 5


def test_trailing_window_spend_sums_in_window():
    # 창 안만 합산, 이전·미래는 제외. 2주 창 = [5/28, 6/10].
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T01:00:00Z", 30.0)   # 창 안
    _insert(conn, "2026-05-20T01:00:00Z", 99.0)   # 창 밖(이전) 제외
    _insert(conn, "2026-06-15T01:00:00Z", 99.0)   # 창 밖(미래) 제외
    assert trailing_window_spend(conn, "claude", NOW, 2) == 30.0


def test_trailing_window_spend_boundary_inclusive():
    # 창 시작일(5/28 KST) 포함, 직전(5/27 KST) 제외.
    conn = connect(":memory:")
    _insert(conn, "2026-05-28T01:00:00Z", 5.0)
    _insert(conn, "2026-05-27T01:00:00Z", 7.0)
    assert trailing_window_spend(conn, "claude", NOW, 2) == 5.0


def test_trailing_window_spend_weeks_widens():
    # 1주 창 [6/4,6/10]은 6/3 제외, 2주 창 [5/28,6/10]은 포함.
    conn = connect(":memory:")
    _insert(conn, "2026-06-03T01:00:00Z", 8.0)
    assert trailing_window_spend(conn, "claude", NOW, 1) == 0.0
    assert trailing_window_spend(conn, "claude", NOW, 2) == 8.0


def test_trailing_window_spend_providers_filter():
    # provider=None + providers로 활성 합산. 빈 집합은 0.
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T01:00:00Z", 5.0, provider="claude")
    _insert(conn, "2026-06-06T01:00:00Z", 7.0, provider="codex")
    assert trailing_window_spend(conn, None, NOW, 2, providers=["claude"]) == 5.0
    assert trailing_window_spend(conn, None, NOW, 2, providers=["claude", "codex"]) == 12.0
    assert trailing_window_spend(conn, None, NOW, 2, providers=[]) == 0.0


def test_combined_forecast_official_slope_ignores_local_spend():
    # 엔터 기울기는 로컬 소비를 보지 않는다(로컬 기울기 폐기, ADR 0015 D3). 공식 월초누적만.
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 40.0, 200.0, raw="spend")],
                            created_at="x")
    _insert(conn, "2026-06-05T01:00:00Z", 999.0, provider="claude")  # 로컬 거액 — 기울기에 영향 없어야
    fc = combined_forecast(conn, _fc_views(conn, NOW), NOW)
    # 공식 월초누적 40/8=5. 로컬 999가 섞이면 깨진다.
    assert fc.daily_rate_usd == 5.0


def test_lens_uses_official_trailing_delta():
    # 카드 고스트도 (a)공식 트레일링 우선: 윈도우 전 베이스(5/20 used50)+윈도우 내(6/10 used150)
    # → 100/10영업일=10. 로컬 거액은 무시(공식만, ADR 0015 D3).
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-05-20T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 50.0, 500.0, raw="spend")],
                            created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 150.0, 500.0, raw="spend")],
                            created_at="x")
    _insert(conn, "2026-06-05T01:00:00Z", 999.0, provider="claude")  # 로컬 — 무시
    v = official_view(conn, "claude", NOW, 0.04)
    assert v.lens.daily_rate_usd == 10.0   # 공식 트레일링 100/10, 로컬 무시


# ─── Commit 2(활성 AI): providers 필터(WHERE provider IN) ─────────────────────
# provider=None(전체)일 때 키워드 providers로 활성 집합만 합산. 빈 집합은 빈 결과.

def _seed_two_providers(conn):
    """claude $5 + codex $7(같은 달). 활성 필터 동치/빈집합/가중평균 검증용."""
    _insert(conn, "2026-06-05T00:00:00Z", 5.0, session="c", provider="claude",
            input_t=50, cache_read=50, model="claude-opus-4-8")
    _insert(conn, "2026-06-06T00:00:00Z", 7.0, session="x", provider="codex",
            input_t=70, cache_read=30, model="gpt-5-codex")


def test_month_spend_providers_single_equals_positional():
    conn = connect(":memory:")
    _seed_two_providers(conn)
    # providers=["claude"]는 단일 positional provider="claude"와 동치
    assert month_spend(conn, None, NOW, providers=["claude"]) == month_spend(conn, "claude", NOW)
    assert month_spend(conn, None, NOW, providers=["claude"]) == 5.0


def test_month_spend_providers_all_equals_none():
    conn = connect(":memory:")
    _seed_two_providers(conn)
    # DB가 두 provider뿐이므로 활성=둘 == None(전체)
    assert month_spend(conn, None, NOW, providers=["claude", "codex"]) == month_spend(conn, None, NOW)
    assert month_spend(conn, None, NOW, providers=["claude", "codex"]) == 12.0


def test_month_spend_empty_providers_is_zero():
    conn = connect(":memory:")
    _seed_two_providers(conn)
    # 활성 0개 → 빈 결과(전체로 새지 않음 — 빈 상태가 깨지지 않게)
    assert month_spend(conn, None, NOW, providers=[]) == 0.0


def test_aggregations_empty_providers_are_empty():
    conn = connect(":memory:")
    _seed_two_providers(conn)
    start, nxt = month_bounds(NOW)
    assert by_project(conn, None, NOW, providers=[]) == []
    assert by_session(conn, None, NOW, providers=[]) == []
    assert token_composition(conn, None, start, nxt, providers=[]).total == 0
    assert by_dimension(conn, None, start, nxt, "model", providers=[]) == []
    assert sidechain_split(conn, None, start, nxt, providers=[]).total_cost == 0.0


def test_by_dimension_providers_weighted_ratio_claude_only():
    conn = connect(":memory:")
    _seed_two_providers(conn)
    start, nxt = month_bounds(NOW)
    rows = by_dimension(conn, None, start, nxt, "model", providers=["claude"])
    # claude 행만 — cache_ratio = 50/(50+0+50)=0.5, codex 분모가 섞이지 않음(뷰 재합산 대비 정밀)
    assert [r.key for r in rows] == ["claude-opus-4-8"]
    assert rows[0].cache_ratio == 0.5


def test_token_composition_providers_single():
    conn = connect(":memory:")
    _seed_two_providers(conn)
    start, nxt = month_bounds(NOW)
    tc = token_composition(conn, None, start, nxt, providers=["codex"])
    assert tc.total == 100                       # codex만: input 70 + cache_read 30
    assert tc.input_tokens == 70 and tc.cache_read == 30


def test_sidechain_split_providers_single():
    conn = connect(":memory:")
    _seed_two_providers(conn)
    start, nxt = month_bounds(NOW)
    assert sidechain_split(conn, None, start, nxt, providers=["claude"]).total_cost == 5.0


def test_pricing_coverage_providers_filter():
    conn = connect(":memory:")
    _seed_two_providers(conn)
    full = pricing_coverage(conn, _PRICING)
    claude_only = pricing_coverage(conn, _PRICING, providers=["claude"])
    empty = pricing_coverage(conn, _PRICING, providers=[])
    assert {m.provider for m in full.models} == {"claude", "codex"}
    assert {m.provider for m in claude_only.models} == {"claude"}
    assert claude_only.models[0].model == "claude-opus-4-8"
    assert empty.models == [] and empty.total_tokens == 0


def test_insights_providers_passthrough():
    conn = connect(":memory:")
    # claude 캐시 충분(0.9), codex 캐시 낮음(0.1). 활성=claude면 codex의 낮은 캐시 신호가 안 떠야 함.
    _msg(conn, dedup_key="cl", provider="claude", ts="2026-06-10T10:00:00Z",
         cost_usd=1.0, input_tokens=10, cache_read=90)
    _msg(conn, dedup_key="cx", provider="codex", ts="2026-06-10T10:00:00Z",
         cost_usd=1.0, input_tokens=90, cache_read=10)
    cards = insights(conn, _NOW_STATUS, None, cov=None, providers=["claude"])
    texts = " ".join(c.text for c in cards)
    assert "캐시 활용" not in texts          # claude만 보면 캐시 0.9라 경고 없음


# ─── Commit 3(활성 AI): overview_context 활성 threading ───────────────────────

def test_overview_context_respects_active_subset(monkeypatch, tmp_path):
    # 활성=claude만 → 총지출·프로젝트·추세·official_cards 모두 codex 제외(데이터는 보존, 표시만 숨김).
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"tracked_providers": ["claude"]}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="cl", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=10.0, project="/p")
    _msg(conn, dedup_key="cx", provider="codex", ts="2026-06-11T10:00:00Z", cost_usd=4.0, project="/p")
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)
    assert ctx["month_total"] == 10.0                                  # 14가 아니라 claude만
    assert [c["provider"] for c in ctx["official_cards"]] == ["claude"]
    assert [s["label"] for s in ctx["trend_series"]] == ["Claude"]     # codex 밴드 없음
    assert ctx["projects"][0].cost == 10.0                             # 프로젝트 합도 claude만
    assert ctx["has_data"] is True


def test_overview_context_empty_active_is_empty_state(monkeypatch, tmp_path):
    # 활성 0개 → 데이터는 있어도 빈 상태(표시 대상 없음). 전체로 새지 않음.
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"tracked_providers": []}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="cl", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=10.0)
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)
    assert ctx["has_data"] is False
    assert ctx["month_total"] == 0.0
    assert ctx["official_cards"] == []
    assert ctx["trend_series"] == []


# ─── Commit 4(활성 AI): history/analysis 활성 필터 + 필터 옵션 파생 ─────────────

def test_history_context_inactive_provider_falls_back_to_active(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"tracked_providers": ["claude"]}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="cl", provider="claude", session_id="s1", ts="2026-06-13T01:00:00Z", cost_usd=2.0)
    _msg(conn, dedup_key="cx", provider="codex", session_id="s2", ts="2026-06-13T01:00:00Z", cost_usd=9.0)
    # provider=codex 요청이지만 codex가 비활성 → 전체(활성=claude)로 폴백, codex 행 안 뜸
    ctx = history_context(conn, _ANCHOR_613, "codex", "date_desc", now_kst=_NOW_613)
    assert ctx["provider"] == ""
    assert ctx["total"] == 2.0


def test_history_context_filter_single_active_hidden(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"tracked_providers": ["claude"]}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    ctx = history_context(conn, _ANCHOR_613, "", "date_desc", now_kst=_NOW_613)
    assert ctx["show_filter"] is False
    assert [p["key"] for p in ctx["filter_providers"]] == ["claude"]


def test_history_context_filter_multi_active_shown(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"tracked_providers": ["claude", "codex"]}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    ctx = history_context(conn, _ANCHOR_613, "", "date_desc", now_kst=_NOW_613)
    assert ctx["show_filter"] is True
    assert [p["key"] for p in ctx["filter_providers"]] == ["claude", "codex"]
    assert [p["label"] for p in ctx["filter_providers"]] == ["Claude", "Codex"]


def test_dimension_context_inactive_provider_falls_back(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"tracked_providers": ["claude"]}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="cl", model="claude-opus-4-8", provider="claude",
         ts="2026-06-10T10:00:00Z", cost_usd=8.0)
    _msg(conn, dedup_key="cx", model="gpt-5", provider="codex",
         ts="2026-06-10T10:00:00Z", cost_usd=2.0)
    ctx = dimension_context(conn, _ANCHOR_613, "codex", dim="model", now_kst=_NOW_613)
    assert ctx["provider"] == ""           # 비활성 codex → 전체 폴백
    assert ctx["total"] == 8.0             # claude만
    assert ctx["show_filter"] is False     # 활성 1개


# ─── Commit 6(활성 AI): 라벨 적응 + 빈 상태 플래그 ─────────────────────────────

def _ctx_with_active(monkeypatch, tmp_path, providers_json):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"tracked_providers": ' + providers_json + '}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="cl", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=10.0)
    return overview_context(conn, sort="cost", now_kst=_NOW_STATUS)


def test_overview_context_label_flags_single(monkeypatch, tmp_path):
    ctx = _ctx_with_active(monkeypatch, tmp_path, '["claude"]')
    assert ctx["combined"] is False
    assert ctx["solo_label"] == "Claude"   # 활성 1개 → provider명
    assert ctx["active_empty"] is False


def test_overview_context_label_flags_multi(monkeypatch, tmp_path):
    ctx = _ctx_with_active(monkeypatch, tmp_path, '["claude", "codex"]')
    assert ctx["combined"] is True         # 활성 ≥2 → 통합/전 AI 합산
    assert ctx["active_empty"] is False


def test_overview_context_active_empty_flag(monkeypatch, tmp_path):
    ctx = _ctx_with_active(monkeypatch, tmp_path, '[]')
    assert ctx["active_empty"] is True
    assert ctx["combined"] is False


# --- 공식 사용량 스냅샷 이력: 통합 풀 used 시계열(ADR 0007) ---


def test_pool_used_history_empty():
    conn = connect(":memory:")
    assert pool_used_history(conn, "claude") == []


def test_pool_used_history_ascending_per_snapshot():
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend")],
                            created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:10:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 42.0, 100.0, raw="spend")],
                            created_at="x")
    hist = pool_used_history(conn, "claude")
    assert [u for _, u in hist] == [30.0, 42.0]
    assert hist[0][0] == parse_ts("2026-06-10T09:00:00+09:00")
    assert hist[0][0] < hist[1][0]


def test_pool_used_history_sums_usd_buckets_excludes_rate_window():
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                                     _ob("event", "event_credit", 125.0, 500.0, raw="cinder",
                                         resets=datetime(2026, 9, 10, tzinfo=KST)),
                                     _ob("rate_window", "rate_window", None, None, raw="five_hour",
                                         unit="percent", util=40.0)],
                            created_at="x")
    # opt-in 크레딧(cinder)은 합산, rate_window는 USD 한도 없어 제외(ADR 0016/0007).
    hist = pool_used_history(conn, "claude", is_pooled=_pool_with("cinder"))
    assert hist == [(parse_ts("2026-06-10T09:00:00+09:00"), 155.0)]  # 30 + 125, rate_window 제외


def test_pool_used_history_excludes_snapshot_without_usd_limit():
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("rate_window", "rate_window", None, None, raw="five_hour",
                                         unit="percent", util=40.0)],
                            created_at="x")
    assert pool_used_history(conn, "claude") == []


# --- 세그먼트 분할(_segment_points): 리셋·갭에서 선 끊기(ADR 0007) ---


def _t(minute):
    return datetime(2026, 6, 10, 9, 0, tzinfo=KST) + timedelta(minutes=minute)


def test_segment_points_empty():
    assert _segment_points([], max_gap_minutes=30) == []


def test_segment_points_single():
    p = [(_t(0), 10.0)]
    assert _segment_points(p, max_gap_minutes=30) == [p]


def test_segment_points_monotonic_one_segment():
    p = [(_t(0), 10.0), (_t(10), 20.0), (_t(20), 25.0)]
    assert _segment_points(p, max_gap_minutes=30) == [p]


def test_segment_points_splits_on_reset_drop():
    p = [(_t(0), 80.0), (_t(10), 90.0), (_t(20), 5.0), (_t(30), 12.0)]  # 90→5 리셋
    assert _segment_points(p, max_gap_minutes=30) == [
        [(_t(0), 80.0), (_t(10), 90.0)], [(_t(20), 5.0), (_t(30), 12.0)]]


def test_segment_points_splits_on_time_gap():
    p = [(_t(0), 10.0), (_t(10), 20.0), (_t(50), 30.0)]  # 10→50 = 40분 > 30
    assert _segment_points(p, max_gap_minutes=30) == [
        [(_t(0), 10.0), (_t(10), 20.0)], [(_t(50), 30.0)]]


def test_segment_points_no_gap_break_within_threshold():
    p = [(_t(0), 10.0), (_t(25), 20.0)]  # 25 ≤ 30
    assert _segment_points(p, max_gap_minutes=30) == [p]


def test_segment_points_none_gap_only_reset_breaks():
    p = [(_t(0), 10.0), (_t(500), 20.0), (_t(510), 5.0)]  # max_gap None → 갭 무시, 리셋만
    assert _segment_points(p, max_gap_minutes=None) == [
        [(_t(0), 10.0), (_t(500), 20.0)], [(_t(510), 5.0)]]


def test_segment_points_noise_dip_stays_one_segment():
    """미세 하락(누적값 진동 노이즈)은 리셋이 아니라 한 세그먼트 유지(ADR 0007)."""
    p = [(_t(0), 44.88), (_t(10), 44.87), (_t(20), 44.88), (_t(30), 44.87)]
    assert _segment_points(p, max_gap_minutes=60) == [p]


def test_segment_points_drop_above_half_not_reset():
    """절반 이상 남은 하락은 리셋 아님 — 청구 리셋은 절반 미만 급락으로만 판정."""
    p = [(_t(0), 100.0), (_t(10), 60.0)]   # 40% 하락(60 > 50)
    assert _segment_points(p, max_gap_minutes=60) == [p]


# --- 통합 풀 과거 곡선(pool_history): forward-fill 합산 + 갭/리셋 끊기(ADR 0007) ---


def _seed_pool(conn, provider, kind, raw, samples, limit=100.0, unit="usd"):
    for minute, used in samples:
        insert_official_buckets(
            conn, provider=provider, fetched_at=_t(minute).isoformat(),
            buckets=[_ob("monthly", kind, used, limit, raw=raw, unit=unit)], created_at="x")


def test_pool_history_empty():
    conn = connect(":memory:")
    assert pool_history(conn, ["claude", "codex"], max_gap_minutes=30) == []


def test_pool_history_single_provider_segments_on_reset():
    conn = connect(":memory:")
    _seed_pool(conn, "claude", "monthly_limit", "spend", [(0, 80.0), (10, 90.0), (20, 5.0)])
    segs = pool_history(conn, ["claude"], max_gap_minutes=30)
    assert [[p["used_usd"] for p in s] for s in segs] == [[80.0, 90.0], [5.0]]
    assert segs[0][0]["ts"] == _t(0).isoformat()


def test_pool_history_two_providers_summed_forward_fill():
    conn = connect(":memory:")
    _seed_pool(conn, "claude", "monthly_limit", "spend", [(0, 30.0), (10, 40.0)])
    _seed_pool(conn, "codex", "codex_monthly", "individual_limit", [(0, 20.0), (10, 25.0)],
               limit=80.0, unit="credit")
    segs = pool_history(conn, ["claude", "codex"], max_gap_minutes=30)
    assert len(segs) == 1
    assert [p["used_usd"] for p in segs[0]] == [50.0, 65.0]  # 30+20, 40+25


def test_pool_history_breaks_on_provider_reset():
    conn = connect(":memory:")
    _seed_pool(conn, "claude", "monthly_limit", "spend", [(0, 30.0), (10, 40.0), (20, 50.0)])
    _seed_pool(conn, "codex", "codex_monthly", "individual_limit", [(0, 20.0), (10, 25.0), (20, 2.0)],
               limit=80.0, unit="credit")  # codex 리셋 t20: 25→2
    segs = pool_history(conn, ["claude", "codex"], max_gap_minutes=30)
    assert [[p["used_usd"] for p in s] for s in segs] == [[50.0, 65.0], [52.0]]


# --- pool_history_to_daily: 과거 곡선을 전망 차트 일-인덱스에 매핑(ADR 0007) ---


def test_pool_history_to_daily_maps_by_day_last_wins():
    now = datetime(2026, 6, 10, 12, 0, tzinfo=KST)
    days = list(range(1, 31))  # 1..30
    segs = [[
        {"ts": datetime(2026, 6, 3, 9, 0, tzinfo=KST).isoformat(), "used_usd": 10.0},
        {"ts": datetime(2026, 6, 3, 15, 0, tzinfo=KST).isoformat(), "used_usd": 12.0},  # 같은 날 → 나중 값
        {"ts": datetime(2026, 6, 5, 9, 0, tzinfo=KST).isoformat(), "used_usd": 20.0},
    ]]
    out = pool_history_to_daily(segs, days, now)
    assert out[2] == 12.0    # day 3 → index 2
    assert out[4] == 20.0    # day 5 → index 4
    assert out[0] is None and out[3] is None  # 데이터 없는 날 = None(끊김)


def test_pool_history_to_daily_ignores_other_months():
    now = datetime(2026, 6, 10, 12, 0, tzinfo=KST)
    days = list(range(1, 31))
    segs = [[{"ts": datetime(2026, 5, 20, 9, 0, tzinfo=KST).isoformat(), "used_usd": 99.0}]]  # 5월 → 무시
    assert all(v is None for v in pool_history_to_daily(segs, days, now))


def test_pool_history_to_daily_empty():
    now = datetime(2026, 6, 10, 12, 0, tzinfo=KST)
    days = list(range(1, 31))
    assert pool_history_to_daily([], days, now) == [None] * 30


# --- pool_daily_history: 날짜별 통합 풀 소비 델타 + 커버리지(ADR 0010) ---

_JUNE_START = datetime(2026, 6, 1, tzinfo=KST)
_JULY_START = datetime(2026, 7, 1, tzinfo=KST)


def _seed_days(conn, provider, kind, raw, day_used, limit=100.0, unit="usd"):
    """(day, used_usd) 표본을 2026-06-<day> 12:00 KST 스냅샷으로 적재."""
    for day, used in day_used:
        dt = datetime(2026, 6, day, 12, 0, tzinfo=KST)
        insert_official_buckets(
            conn, provider=provider, fetched_at=dt.isoformat(),
            buckets=[_ob("monthly", kind, used, limit, raw=raw, unit=unit)], created_at="x")


def test_pool_daily_history_basic_deltas():
    """일별 소비 = 인접 누적차. 첫 표본은 기준 0에서의 누적(=그 값)."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend", [(3, 10.0), (4, 25.0), (5, 40.0)])
    rows = pool_daily_history(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    covered = {r["date"]: r["used_usd"] for r in rows if r["covered"]}
    assert covered == {date(2026, 6, 3): 10.0, date(2026, 6, 4): 15.0, date(2026, 6, 5): 15.0}


def test_pool_daily_history_reset_counts_post_reset_only():
    """리셋(누적 하락)은 음수/거대 막대를 만들지 않고 post-reset 값만 계상한다."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend", [(3, 80.0), (4, 90.0), (5, 5.0)])
    rows = pool_daily_history(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    covered = {r["date"]: r["used_usd"] for r in rows if r["covered"]}
    assert covered[date(2026, 6, 3)] == 80.0
    assert covered[date(2026, 6, 4)] == 10.0
    assert covered[date(2026, 6, 5)] == 5.0   # 리셋: 90→5, -85이나 95가 아니라 5


def test_pool_daily_history_noise_dip_offsets_not_reset():
    """누적값 미세 진동(44.88↔44.87)은 리셋 오판 없이 상계되어 그날 소비≈0(ADR 0010)."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend",
               [(3, 23, 0, 44.88),                                  # 전날 마지막(기준)
                (4, 0, 0, 44.88), (4, 1, 0, 44.87), (4, 2, 0, 44.88)])  # 진동(노이즈)
    rows = pool_daily_history(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    covered = {r["date"]: r["used_usd"] for r in rows if r["covered"]}
    assert covered[date(2026, 6, 4)] == pytest.approx(0.0, abs=1e-9)  # 거대값(44.87) 아님


def test_pool_daily_history_gap_lumps_and_marks_uncovered():
    """갭 가로지른 소비는 첫 post-gap 날에 합산, 표본 없는 날은 covered=False·used=None(0 아님)."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend", [(3, 10.0), (6, 40.0)])  # day4,5 표본 없음
    rows = pool_daily_history(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    by_d = {r["date"]: r for r in rows}
    assert by_d[date(2026, 6, 3)]["used_usd"] == 10.0
    assert by_d[date(2026, 6, 6)]["used_usd"] == 30.0    # day4,5,6 소비가 day6에 lump
    assert by_d[date(2026, 6, 4)]["covered"] is False and by_d[date(2026, 6, 4)]["used_usd"] is None
    assert by_d[date(2026, 6, 1)]["covered"] is False    # 첫 표본 이전도 미커버
    assert len(rows) == 30                                # 구간 모든 날이 행으로(막대 x축)


def test_pool_daily_history_per_provider_breakdown_sums():
    """provider별 일별 델타가 분해로 노출되고, 통합 델타는 그 합과 정확히 일치(스택 무결성)."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend", [(3, 10.0), (4, 30.0)])
    _seed_days(conn, "codex", "codex_monthly", "individual_limit", [(3, 5.0), (4, 11.0)],
               limit=80.0, unit="credit")
    rows = pool_daily_history(conn, ["claude", "codex"], start=_JUNE_START, nxt=_JULY_START)
    by_d = {r["date"]: r for r in rows}
    assert by_d[date(2026, 6, 3)]["per_provider"] == {"claude": 10.0, "codex": 5.0}
    assert by_d[date(2026, 6, 4)]["per_provider"] == {"claude": 20.0, "codex": 6.0}  # 30-10, 11-5
    for r in rows:
        if r["covered"]:
            assert r["used_usd"] == round(sum(r["per_provider"].values()), 6)


def test_pool_daily_history_excludes_rate_window_and_empty_pool():
    """rate_window-only provider는 소진형 풀에 0 기여, 빈 풀은 전부 미커버(라우트 숨김 근거)."""
    conn = connect(":memory:")
    dt = datetime(2026, 6, 3, 12, 0, tzinfo=KST)
    insert_official_buckets(   # rate_window만(limit_usd None) — 소진형 아님
        conn, provider="claude", fetched_at=dt.isoformat(),
        buckets=[_ob("rate_window", "rate_window", None, None, raw="five_hour",
                     unit="percent", util=50.0)], created_at="x")
    rows = pool_daily_history(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    assert all(not r["covered"] for r in rows)
    empty = pool_daily_history(connect(":memory:"), ["claude", "codex"],
                               start=_JUNE_START, nxt=_JULY_START)
    assert all(not r["covered"] for r in empty)


# --- pool_hourly_history: 단일 날짜의 시간(0~23)별 통합 풀 소비 델타(ADR 0019) ---

_JUNE3 = datetime(2026, 6, 3, tzinfo=KST)


def test_pool_hourly_history_basic_deltas():
    """시간별 소비 = 인접 누적차. 첫 표본은 기준 0에서의 누적(=그 값). 24개 시각 행."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend",
               [(3, 9, 0, 10.0), (3, 10, 0, 25.0), (3, 14, 0, 40.0)])
    rows = pool_hourly_history(conn, ["claude"], day_start=_JUNE3)
    covered = {r["hour"]: r["used_usd"] for r in rows if r["covered"]}
    assert covered == {9: 10.0, 10: 15.0, 14: 15.0}
    assert len(rows) == 24


def test_pool_hourly_history_baseline_carries_from_prior_day():
    """당일 첫 시각 소비는 전날 마지막 표본 기준 델타 — 자정에 0으로 리셋되지 않는다."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend",
               [(2, 23, 0, 30.0), (3, 9, 0, 35.0), (3, 10, 0, 38.0)])
    rows = pool_hourly_history(conn, ["claude"], day_start=_JUNE3)
    covered = {r["hour"]: r["used_usd"] for r in rows if r["covered"]}
    assert covered == {9: 5.0, 10: 3.0}   # 35-30, 38-35 — 전날 $30 기준


def test_pool_hourly_history_reset_counts_post_reset_only():
    """하루 안의 리셋(누적 하락)은 음수/거대 막대 없이 post-reset 값만 계상."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend",
               [(3, 9, 0, 80.0), (3, 10, 0, 90.0), (3, 11, 0, 5.0)])
    rows = pool_hourly_history(conn, ["claude"], day_start=_JUNE3)
    covered = {r["hour"]: r["used_usd"] for r in rows if r["covered"]}
    assert covered == {9: 80.0, 10: 10.0, 11: 5.0}   # 리셋 90→5는 5(=-85/95 아님)


def test_pool_hourly_history_gap_lumps_and_marks_uncovered():
    """갭 가로지른 소비는 첫 post-gap 시각에 합산, 표본 없는 시각은 covered=False·used=None."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend",
               [(3, 9, 0, 10.0), (3, 14, 0, 40.0)])   # 10~13시 표본 없음
    rows = pool_hourly_history(conn, ["claude"], day_start=_JUNE3)
    by_h = {r["hour"]: r for r in rows}
    assert by_h[9]["used_usd"] == 10.0
    assert by_h[14]["used_usd"] == 30.0               # 10~14시 소비가 14시에 lump
    assert by_h[10]["covered"] is False and by_h[10]["used_usd"] is None
    assert by_h[0]["covered"] is False                # 첫 표본 이전도 미커버


def test_pool_hourly_history_per_provider_breakdown_sums():
    """provider별 시간 델타 분해 + 통합 델타 = 그 합(스택 무결성)."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend", [(3, 9, 0, 10.0), (3, 10, 0, 30.0)])
    _seed_snap(conn, "codex", "codex_monthly", "individual_limit",
               [(3, 9, 0, 5.0), (3, 10, 0, 11.0)], limit=80.0, unit="credit")
    rows = pool_hourly_history(conn, ["claude", "codex"], day_start=_JUNE3)
    by_h = {r["hour"]: r for r in rows}
    assert by_h[9]["per_provider"] == {"claude": 10.0, "codex": 5.0}
    assert by_h[10]["per_provider"] == {"claude": 20.0, "codex": 6.0}   # 30-10, 11-5
    for r in rows:
        if r["covered"]:
            assert r["used_usd"] == round(sum(r["per_provider"].values()), 6)


def test_pool_hourly_history_excludes_rate_window_and_empty_pool():
    """rate_window-only는 소진형 풀에 0 기여, 빈 풀은 전부 미커버(라우트 숨김 근거)."""
    conn = connect(":memory:")
    dt = datetime(2026, 6, 3, 9, 0, tzinfo=KST)
    insert_official_buckets(
        conn, provider="claude", fetched_at=dt.isoformat(),
        buckets=[_ob("rate_window", "rate_window", None, None, raw="five_hour",
                     unit="percent", util=50.0)], created_at="x")
    rows = pool_hourly_history(conn, ["claude"], day_start=_JUNE3)
    assert all(not r["covered"] for r in rows)
    empty = pool_hourly_history(connect(":memory:"), ["claude", "codex"], day_start=_JUNE3)
    assert all(not r["covered"] for r in empty) and len(empty) == 24


# --- pool_snapshots_by_day: 일 소비 재구성 드릴다운(ADR 0010) ---


def _seed_snap(conn, provider, kind, raw, samples, limit=100.0, unit="usd"):
    """(day, hour, minute, used_usd) 표본을 2026-06 KST 스냅샷으로 적재."""
    for day, hour, minute, used in samples:
        dt = datetime(2026, 6, day, hour, minute, tzinfo=KST)
        insert_official_buckets(
            conn, provider=provider, fetched_at=dt.isoformat(),
            buckets=[_ob("monthly", kind, used, limit, raw=raw, unit=unit)], created_at="x")


def test_pool_snapshots_by_day_first_ever_two_snaps():
    """추적 첫날: 직전 기준 없음(first_ever), 첫 표본 델타=누적값 전체, 합=일 소비."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend", [(3, 9, 0, 10.0), (3, 14, 0, 18.0)])
    by_day = pool_snapshots_by_day(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    detail = by_day[date(2026, 6, 3)]
    assert len(detail) == 1
    pd = detail[0]
    assert pd["provider"] == "claude"
    assert pd["first_ever"] is True and pd["baseline"] is None and pd["gap_days"] == 0
    assert [(s["delta"], s["reset"]) for s in pd["snapshots"]] == [(10.0, False), (8.0, False)]
    assert pd["total_delta"] == 18.0


def test_pool_snapshots_by_day_baseline_from_previous_day():
    """연속일: 직전 기준 = 어제 마지막 표본, 당일 첫 델타 = 당일값 - 기준, gap_days=1."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend",
               [(3, 23, 0, 10.0), (4, 9, 0, 25.0), (4, 18, 0, 30.0)])
    by_day = pool_snapshots_by_day(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    pd = by_day[date(2026, 6, 4)][0]
    assert pd["first_ever"] is False and pd["gap_days"] == 1
    assert pd["baseline"]["used_usd"] == 10.0
    assert pd["baseline"]["ts"] == datetime(2026, 6, 3, 23, 0, tzinfo=KST).isoformat()
    assert [s["delta"] for s in pd["snapshots"]] == [15.0, 5.0]   # 25-10, 30-25
    assert pd["total_delta"] == 20.0


def test_pool_snapshots_by_day_gap_lumps_into_first_post_gap_day():
    """3일 갭: 기준이 3일 전, gap_days=3 → 그 사이 소비가 이 날 델타에 합산됨을 노출."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend", [(3, 12, 0, 10.0), (6, 12, 0, 52.0)])
    by_day = pool_snapshots_by_day(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    assert date(2026, 6, 4) not in by_day and date(2026, 6, 5) not in by_day  # 표본 없는 날 키 없음
    pd = by_day[date(2026, 6, 6)][0]
    assert pd["gap_days"] == 3 and pd["baseline"]["used_usd"] == 10.0
    assert pd["snapshots"][0]["delta"] == 42.0   # 52-10, 4·5일치 흡수


def test_pool_snapshots_by_day_reset_flag():
    """리셋(누적 하락): 해당 표본 reset=True, 델타=post-reset 값(음수/거대값 아님)."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend", [(3, 9, 0, 90.0), (4, 9, 0, 5.0)])
    by_day = pool_snapshots_by_day(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    pd = by_day[date(2026, 6, 4)][0]
    assert pd["snapshots"][0]["reset"] is True and pd["snapshots"][0]["delta"] == 5.0


def test_pool_snapshots_by_day_noise_dip_not_reset():
    """미세 하락(노이즈)은 reset=False·델타는 부호 그대로 음수(post-reset 거대값 아님, ADR 0010)."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend",
               [(3, 23, 0, 44.88), (4, 9, 0, 44.87)])   # 0.01 하락 = 노이즈
    by_day = pool_snapshots_by_day(conn, ["claude"], start=_JUNE_START, nxt=_JULY_START)
    pd = by_day[date(2026, 6, 4)][0]
    assert pd["snapshots"][0]["reset"] is False
    assert pd["snapshots"][0]["delta"] == pytest.approx(-0.01, abs=1e-6)


def test_pool_snapshots_by_day_multi_provider_ordered():
    """여러 provider는 인자 순서대로 리스트에 분해돼, 합산 일 소비를 provider별로 설명한다."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend", [(3, 9, 0, 10.0), (4, 9, 0, 30.0)])
    _seed_snap(conn, "codex", "codex_monthly", "individual_limit", [(3, 10, 0, 5.0), (4, 10, 0, 11.0)],
               limit=80.0, unit="credit")
    by_day = pool_snapshots_by_day(conn, ["claude", "codex"], start=_JUNE_START, nxt=_JULY_START)
    detail = by_day[date(2026, 6, 4)]
    assert [pd["provider"] for pd in detail] == ["claude", "codex"]
    assert {pd["provider"]: pd["total_delta"] for pd in detail} == {"claude": 20.0, "codex": 6.0}


def test_pool_snapshots_by_day_reconciles_with_daily_history():
    """불변식: 각 날 detail의 per-provider total_delta 합 == pool_daily_history의 일 소비."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend",
               [(3, 9, 0, 10.0), (3, 20, 0, 14.0), (6, 9, 0, 40.0)])  # 갭(4,5) + 같은날 2표본
    _seed_snap(conn, "codex", "codex_monthly", "individual_limit",
               [(3, 9, 0, 5.0), (4, 9, 0, 9.0)], limit=80.0, unit="credit")
    by_day = pool_snapshots_by_day(conn, ["claude", "codex"], start=_JUNE_START, nxt=_JULY_START)
    daily = {r["date"]: r["used_usd"] for r in
             pool_daily_history(conn, ["claude", "codex"], start=_JUNE_START, nxt=_JULY_START)
             if r["covered"]}
    for d, detail in by_day.items():
        assert round(sum(pd["total_delta"] for pd in detail), 6) == daily[d]


def test_pool_snapshots_by_day_baseline_can_predate_range_start():
    """구간 첫날의 기준은 start 이전 표본일 수 있다(경계에서 델타 보존)."""
    conn = connect(":memory:")
    _seed_snap(conn, "claude", "monthly_limit", "spend", [(3, 9, 0, 40.0), (5, 9, 0, 55.0)])
    start = datetime(2026, 6, 4, tzinfo=KST)   # day3 표본은 구간 밖, 기준으로만 쓰임
    by_day = pool_snapshots_by_day(conn, ["claude"], start=start, nxt=_JULY_START)
    assert date(2026, 6, 3) not in by_day
    pd = by_day[date(2026, 6, 5)][0]
    assert pd["first_ever"] is False and pd["baseline"]["used_usd"] == 40.0
    assert pd["snapshots"][0]["delta"] == 15.0   # 55-40


# --- official_period_glance: 공식 오늘·이번주 소비 글랜스(ADR 0011) ---

_NOW_WED = datetime(2026, 6, 10, 15, 0, tzinfo=KST)   # 수 15:00 — 주 시작=월 06-08


def test_period_glance_today_complete():
    """어제 baseline + 오늘 표본 → today.usd=오늘 델타, state=complete."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend", [(9, 20.0), (10, 30.0)])
    g = official_period_glance(conn, "claude", _NOW_WED)
    assert g.today.usd == 10.0           # 30-20
    assert g.today.state == "complete"


def test_period_glance_week_sums_covered_days():
    """이번주 = 월~오늘 covered 합. 주말 baseline(일) 있으면 complete."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend",
               [(7, 10.0), (8, 15.0), (9, 22.0), (10, 30.0)])   # 일 baseline + 월·화·수
    g = official_period_glance(conn, "claude", _NOW_WED)
    assert g.week.usd == 20.0            # (15-10)+(22-15)+(30-22)
    assert g.week.state == "complete"
    assert g.week.covered_days == 3 and g.week.total_days == 3   # 월·화·수


def test_period_glance_today_none_when_no_sample_today():
    """오늘 표본이 없으면 today.state=none, usd=None('$0'과 구분)."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend", [(8, 10.0), (9, 18.0)])  # 오늘(10) 없음
    g = official_period_glance(conn, "claude", _NOW_WED)
    assert g.today.state == "none"
    assert g.today.usd is None


def test_period_glance_today_partial_on_gap():
    """오늘 직전 baseline이 3일 전(gap_days≥2)이면 today partial + observed_from."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend", [(7, 10.0), (10, 40.0)])   # 8,9 갭
    g = official_period_glance(conn, "claude", _NOW_WED)
    assert g.today.state == "partial"
    assert g.today.usd == 30.0       # 40-10 (8,9,10 lump)
    assert g.today.observed_from == datetime(2026, 6, 10, 12, 0, tzinfo=KST).isoformat()


def test_period_glance_week_gap_robust_sum_preserved():
    """주중 하루 갭이어도 주 합계는 정확(총량 보존), covered_days<total_days로 노출."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend",
               [(7, 10.0), (8, 15.0), (10, 30.0)])   # 화(9) 갭
    g = official_period_glance(conn, "claude", _NOW_WED)
    assert g.week.usd == 20.0        # 30-10, 합 보존
    assert g.week.state == "complete"
    assert g.week.covered_days == 2 and g.week.total_days == 3


def test_period_glance_week_partial_when_first_ever():
    """주 시작 전 baseline이 전무(첫 표본이 이번주)면 week partial(추적 시작 — 이전 미분리)."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend", [(8, 10.0), (9, 18.0), (10, 30.0)])
    g = official_period_glance(conn, "claude", _NOW_WED)
    assert g.week.state == "partial"
    assert g.week.usd == 30.0        # 추적 시작분 포함


def test_period_glance_handles_reset_within_week():
    """기간 내 리셋(누적 하락)은 post-reset만 계상 — 거대/음수 막대 없음."""
    conn = connect(":memory:")
    _seed_days(conn, "claude", "monthly_limit", "spend",
               [(7, 80.0), (8, 90.0), (9, 5.0), (10, 12.0)])   # 화(9) 리셋 90→5
    g = official_period_glance(conn, "claude", _NOW_WED)
    assert g.week.usd == 22.0        # (90-80)+5+(12-5)
    assert g.today.usd == 7.0        # 12-5


# ── 기간별 사용량 카드 기반(ADR 0017) — 로컬 임의 구간 합 + 공식 이전 동일구간 ──────────


def test_range_spend_sums_active_window():
    """range_spend = [start, nxt) 로컬 cost_usd 합(활성 합산·단일 둘 다)."""
    from tokenomy.aggregate import range_spend
    conn = connect(":memory:")
    _insert(conn, "2026-06-10T01:00:00Z", 5.0, provider="claude")   # KST 6/10 10:00
    _insert(conn, "2026-06-10T05:00:00Z", 2.0, provider="codex")    # KST 6/10 14:00
    _insert(conn, "2026-06-09T01:00:00Z", 99.0, provider="claude")  # 어제 — 제외
    start = datetime(2026, 6, 10, 0, 0, tzinfo=KST)
    nxt = datetime(2026, 6, 11, 0, 0, tzinfo=KST)
    assert range_spend(conn, None, start, nxt, providers=["claude", "codex"]) == 7.0
    assert range_spend(conn, "claude", start, nxt) == 5.0


def _seed_span_snap(conn, provider, dt, used, *, limit=200.0, kind="monthly_limit", raw="spend"):
    """단일 USD 풀 버킷 스냅샷을 임의 시각 dt(KST)로 적재(공식 이전 구간 테스트용)."""
    insert_official_buckets(conn, provider=provider, fetched_at=dt.isoformat(),
                            buckets=[_ob("monthly", kind, used, limit, raw=raw)], created_at="x")


def _Dt(day, hour):
    return datetime(2026, 6, day, hour, 0, tzinfo=KST)


def test_official_span_spend_boundary_diff():
    """경계가 max_gap 내 관측되면 [start, end] 소비 = 누적차(풀 합산)."""
    from tokenomy.aggregate import official_span_spend
    conn = connect(":memory:")
    _seed_span_snap(conn, "claude", _Dt(9, 0), 10.0)
    _seed_span_snap(conn, "claude", _Dt(9, 12), 16.0)
    _seed_span_snap(conn, "codex", _Dt(9, 0), 5.0)
    _seed_span_snap(conn, "codex", _Dt(9, 12), 8.0)
    spend = official_span_spend(conn, ["claude", "codex"], _Dt(9, 0), _Dt(9, 12),
                                max_gap_minutes=180)
    assert spend == 9.0    # (16-10)+(8-5)


def test_official_span_spend_reset_counts_post_reset():
    """구간 내 리셋(누적 하락)은 post-reset만 계상 — 월 경계 이전 구간도 성립."""
    from tokenomy.aggregate import official_span_spend
    conn = connect(":memory:")
    _seed_span_snap(conn, "claude", _Dt(9, 0), 180.0)    # 직전 주기 말(baseline)
    _seed_span_snap(conn, "claude", _Dt(9, 1), 5.0)      # 리셋 후 새 주기
    _seed_span_snap(conn, "claude", _Dt(9, 12), 20.0)
    spend = official_span_spend(conn, ["claude"], _Dt(9, 0), _Dt(9, 12), max_gap_minutes=180)
    assert spend == 20.0    # 5(post-reset) + (20-5)


def test_official_span_spend_none_when_start_gap():
    """start 직전 baseline이 max_gap보다 오래면(경계 미관측) None — leading-gap 부풀림 차단."""
    from tokenomy.aggregate import official_span_spend
    conn = connect(":memory:")
    _seed_span_snap(conn, "claude", _Dt(7, 12), 10.0)    # start 36h 전
    _seed_span_snap(conn, "claude", _Dt(9, 12), 16.0)
    assert official_span_spend(conn, ["claude"], _Dt(9, 0), _Dt(9, 12),
                               max_gap_minutes=180) is None


def test_official_span_spend_none_before_tracking():
    """start 이전 표본이 전무(추적 시작 이전)면 None."""
    from tokenomy.aggregate import official_span_spend
    conn = connect(":memory:")
    _seed_span_snap(conn, "claude", _Dt(9, 6), 12.0)     # 첫 표본이 start 이후
    assert official_span_spend(conn, ["claude"], _Dt(9, 0), _Dt(9, 12),
                               max_gap_minutes=180) is None


def test_official_span_spend_none_when_end_gap():
    """구간 마지막 표본이 end에서 max_gap보다 오래면(end 미관측) None — 일부 소비 누락."""
    from tokenomy.aggregate import official_span_spend
    conn = connect(":memory:")
    _seed_span_snap(conn, "claude", _Dt(9, 0), 10.0)
    _seed_span_snap(conn, "claude", _Dt(9, 2), 12.0)     # end 10h 전이 마지막
    assert official_span_spend(conn, ["claude"], _Dt(9, 0), _Dt(9, 12),
                               max_gap_minutes=180) is None
