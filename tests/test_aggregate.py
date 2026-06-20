from datetime import date, datetime, timedelta

import pytest

from tokenomy.aggregate import (
    CodexBurndown, codex_burndown, codex_weekly_window,
    DayPoint, DaySessionRow, KST,
    add_business_days, burndown, business_days_between, by_day_session,
    by_dimension, by_model, by_project, by_session, combined_burndown, daily_series,
    effective_month_start, insights,
    month_bounds, normalize_project, official_merged_burndown, official_view,
    OfficialMergedBurndown, OfficialView,
    parse_ts, period_bounds, session_detail, sidechain_split,
    SidechainSplit, stacked_trend, token_composition, pricing_coverage, CoverageReport,
    week_count,
)
from tokenomy.db import connect, insert_official_buckets, insert_official_snapshot, ingest_records
from tokenomy.budget import Budget
from tokenomy.official_parser import OfficialBucket
from tokenomy.parser import UsageRecord
from tokenomy.web.views import build_date_tree, history_context, dimension_context, overview_context, session_context

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


def test_burndown_on_track():
    conn = connect(":memory:")
    for _ in range(3):
        _insert(conn, "2026-06-05T00:00:00Z", 10.0, session=str(_))
    bd = burndown(conn, Budget(claude=100, codex=0), NOW, "claude")
    assert bd.spent == 30.0
    assert bd.pct == 0.3
    assert bd.daily_avg == 3.75         # 30 / 8 영업일(6/1~6/10, 주말 2일 제외)
    assert bd.projected_month == 82.5   # 30 + 3.75 * 14 영업일
    assert bd.on_track is True
    assert bd.exhaust_day is None       # 잔여 70/3.75≈19영업일 > 남은 14영업일


def test_burndown_over_budget_predicts_exhaust():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 50.0)
    bd = burndown(conn, Budget(claude=100, codex=0), NOW, "claude")
    assert bd.daily_avg == 6.25         # 50 / 8 영업일
    assert bd.projected_month == 137.5  # 50 + 6.25 * 14 영업일
    assert bd.on_track is False
    assert bd.exhaust_day == 22         # 6/10 + 8영업일 = 6/22


def test_burndown_excludes_other_months():
    conn = connect(":memory:")
    _insert(conn, "2026-05-30T00:00:00Z", 99.0)   # May (KST still May 30 09:00)
    _insert(conn, "2026-06-05T00:00:00Z", 10.0)
    bd = burndown(conn, Budget(claude=100, codex=0), NOW, "claude")
    assert bd.spent == 10.0


def test_unpriced_counted():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 0.0, priced=0)
    bd = burndown(conn, Budget(claude=100, codex=0), NOW, "claude")
    assert bd.unpriced_count == 1


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
_B = Budget(claude=223.0, codex=223.0)


def test_burndown_status_ok():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=10.0)
    bd = burndown(conn, _B, _NOW_STATUS, "claude")
    # spent 10, daily_avg 0.67, projected ~20 << 223 → ok
    assert bd.status == "ok"


def test_burndown_status_warn():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=120.0)
    bd = burndown(conn, _B, _NOW_STATUS, "claude")
    # spent 120 < 223 이지만 projected 120/15*30 = 240 > 223 → warn
    assert bd.status == "warn"


def test_burndown_status_exceeds():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=250.0)
    bd = burndown(conn, _B, _NOW_STATUS, "claude")
    # spent 250 >= 223 → exceeds
    assert bd.status == "exceeds"


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
    bd = burndown(conn, _B, _NOW_STATUS, "claude")
    cards = insights(conn, bd, _NOW_STATUS, "claude")
    levels = {c.level for c in cards}
    texts = " ".join(c.text for c in cards)
    assert "warn" in levels and "info" in levels
    assert "캐시" in texts
    assert "web_search" in texts


def test_insights_unpriced_card():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=0.0,
         input_tokens=100, cache_read=100, priced=0)
    bd = burndown(conn, _B, _NOW_STATUS, "claude")
    cards = insights(conn, bd, _NOW_STATUS, "claude")
    assert any("미식별" in c.text for c in cards)


def test_insights_clean_returns_placeholder():
    conn = connect(":memory:")
    # 캐시 충분(0.9), web_search 적음, priced, projected 낮음 → 특이신호 없음
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=1.0,
         input_tokens=10, cache_read=90, web_search=0, priced=1)
    bd = burndown(conn, _B, _NOW_STATUS, "claude")
    cards = insights(conn, bd, _NOW_STATUS, "claude")
    assert len(cards) == 1
    assert "특이 신호 없음" in cards[0].text


def test_daily_series_clamps_to_budget_start():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 50.0, session="pre")    # 도입 전(제외)
    _insert(conn, "2026-06-13T00:00:00Z", 10.0, session="post")   # 도입 후(KST 6/13)
    now = datetime(2026, 6, 15, 12, 0, tzinfo=KST)
    bs = datetime(2026, 6, 12, 0, 0, tzinfo=KST)
    pts = daily_series(conn, "claude", now, budget_start=bs)
    assert len(pts) == 19                       # 6/12~6/30
    assert pts[0].day == 12 and pts[0].cumulative_cost == 0.0    # 12일 지출 없음
    assert pts[1].day == 13 and pts[1].cumulative_cost == 10.0   # 6/5 $50 제외
    assert pts[3].day == 15 and pts[3].cumulative_cost == 10.0   # 오늘까지 누적 유지
    assert pts[4].cumulative_cost is None       # 16일(미래) → None
    assert pts[-1].day == 30 and pts[-1].cumulative_cost is None


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


def test_combined_burndown_sums_capped():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 30.0, provider="claude", session="c")
    _insert(conn, "2026-06-05T00:00:00Z", 10.0, provider="codex", session="x")
    cards = [burndown(conn, Budget(claude=100, codex=50), NOW, p) for p in ("claude", "codex")]
    cb = combined_burndown(cards, NOW)
    assert cb.spent == 40.0          # 30 + 10
    assert cb.limit == 150.0         # 100 + 50
    assert cb.pct == round(40 / 150, 4)
    assert cb.status == "ok"         # projected 120 < 150


def test_combined_burndown_usage_only_when_no_caps():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 30.0, provider="claude", session="c")
    _insert(conn, "2026-06-05T00:00:00Z", 10.0, provider="codex", session="x")
    cards = [burndown(conn, Budget(claude=0, codex=0), NOW, p) for p in ("claude", "codex")]
    cb = combined_burndown(cards, NOW)
    assert cb.limit == 0.0
    assert cb.spent == 40.0          # 사용량만: 전체 합산
    assert cb.pct == 0.0
    assert cb.status == "ok"


def test_combined_burndown_mixed_caps_only_capped():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 30.0, provider="claude", session="c")
    _insert(conn, "2026-06-05T00:00:00Z", 10.0, provider="codex", session="x")
    cards = [burndown(conn, Budget(claude=100, codex=0), NOW, p) for p in ("claude", "codex")]
    cb = combined_burndown(cards, NOW)
    assert cb.limit == 100.0         # claude만
    assert cb.spent == 30.0          # codex(미설정) 지출 제외 → 분자/분모 범위 일치
    assert cb.status == "ok"     # spent 30, limit 100, projected 90 < 100 → ok


def test_combined_burndown_empty_cards():
    # 실제론 PROVIDERS가 비어있지 않아 발생하지 않지만, 빈 입력의 안전 동작을 고정한다.
    cb = combined_burndown([], NOW)
    assert cb.limit == 0.0
    assert cb.spent == 0.0
    assert cb.status == "ok"


def test_overview_context_shape(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"budget": {"claude": 100, "codex": 40}}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=10.0, project="/p")
    _msg(conn, dedup_key="b", provider="codex", ts="2026-06-11T10:00:00Z", cost_usd=4.0, project="/p")
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)
    assert ctx["active_nav"] == "dashboard"
    # provider별 분리 카드
    assert ctx["claude_bd"].spent == 10.0
    assert ctx["codex_bd"].spent == 4.0
    assert ctx["codex_bd"].weekly_limit == 10.0          # 40 / 4
    # 총지출 요약 = 두 카드 spent 합
    assert ctx["month_total"] == 14.0
    assert ctx["budget_configured"] is True
    assert ctx["projects"][0].project == "/p"
    assert ctx["projects"][0].cost == 14.0
    assert ctx["has_data"] is True
    assert "daily_labels" in ctx and "insights" in ctx and "sessions" in ctx


def test_overview_context_provider_without_data(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=10.0)
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)
    assert ctx["claude_has_data"] is True
    assert ctx["codex_has_data"] is False                # codex 로그 없음
    assert ctx["budget_configured"] is False


def test_overview_context_applies_budget_start(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"budget": {"claude": 100, "codex": 40}, "budget_start": "2026-06-12"}',
                   encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="pre", provider="claude", ts="2026-06-05T10:00:00Z", cost_usd=99.0)
    _msg(conn, dedup_key="post", provider="claude", ts="2026-06-13T10:00:00Z", cost_usd=10.0)
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)   # _NOW_STATUS = 6/15
    assert ctx["claude_bd"].spent == 10.0                # 6/5(도입 전) 제외
    assert ctx["claude_bd"].days_in_month == 19          # 6/12~6/30


def test_overview_context_trend_uses_combined_budget(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"budget": {"claude": 100, "codex": 40}, "budget_start": "2026-06-12"}',
                   encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="pre", provider="claude", ts="2026-06-05T10:00:00Z", cost_usd=99.0)
    _msg(conn, dedup_key="post", provider="claude", ts="2026-06-13T10:00:00Z", cost_usd=10.0)
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)   # 6/15
    # x축: 6/12~6/30 (19일)
    assert ctx["daily_labels"][0] == 12
    assert ctx["daily_labels"][-1] == 30
    assert len(ctx["daily_labels"]) == 19
    # 추세 스택: codex 데이터 없음 → Claude 밴드 1개
    assert "daily_actual" not in ctx
    series = ctx["trend_series"]
    assert [s["label"] for s in series] == ["Claude"]
    assert series[0]["cum"][0] == 0.0           # 6/12 지출 없음
    assert series[0]["cum"][1] == 10.0          # 6/13
    assert series[0]["cum"][-1] is None         # 6/30(미래)
    assert series[0]["top"] == series[0]["cum"] # 단일 밴드: top == cum
    assert ctx["trend_totals"][1] == 10.0
    assert ctx["trend_totals"][-1] is None
    # 페이스선·가로선: 통합 예산(100+40=140) 기준, 말일에 수렴
    assert ctx["daily_pace"][-1] == 140.0
    assert ctx["daily_budget"] == [140.0] * 19


def test_overview_context_no_budget_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=10.0)
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)
    assert ctx["budget_configured"] is False
    assert ctx["claude_bd"].limit == 0


def test_overview_context_gauge_merges_official(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"budget": {"claude": 100, "codex": 0}}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=30.0)
    insert_official_snapshot(
        conn, provider="claude", target_month="2026-06", cumulative_usd=45.0,
        snapshot_ts="2026-06-14T09:00:00+09:00", created_at="2026-06-14T09:00:00+09:00",
    )
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)   # 6/15
    g = ctx["gauge"]
    assert g["official_spent"] == 45.0
    assert g["cli_spent"] == 30.0
    assert g["spent"] == 45.0              # max(45, 30) 병합
    assert g["missing_delta"] == 15.0
    assert g["stale_days"] == 1            # 6/14 입력 → 6/15
    assert ctx["claude_bd"].spent == 45.0  # 병합값이 번다운에도 반영
    # staleness 경고 노트(웹/앱 미반영) 노출
    assert any("미반영" in n["text"] for n in ctx["official_notes"])


def test_overview_context_no_official_shows_input_prompt(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"budget": {"claude": 100, "codex": 0}}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=30.0)
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)
    assert ctx["gauge"]["official_spent"] is None
    assert any("미입력" in n["text"] for n in ctx["official_notes"])


def test_overview_context_official_lower_than_cli_warns(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"budget": {"claude": 100, "codex": 0}}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=60.0)
    insert_official_snapshot(
        conn, provider="claude", target_month="2026-06", cumulative_usd=40.0,
        snapshot_ts="2026-06-15T09:00:00+09:00", created_at="2026-06-15T09:00:00+09:00",
    )
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)
    assert ctx["gauge"]["spent"] == 60.0           # max → CLI 유지
    assert ctx["gauge"]["official_lt_cli"] is True
    assert any("확인" in n["text"] for n in ctx["official_notes"])


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


# ─── effective_month_start + week_count ────────────────────────────────────────


def test_effective_month_start_clamps_to_budget_start():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=KST)
    bs = datetime(2026, 6, 12, 0, 0, tzinfo=KST)
    assert effective_month_start(now, bs) == datetime(2026, 6, 12, 0, 0, tzinfo=KST)


def test_effective_month_start_none_returns_month_first():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=KST)
    assert effective_month_start(now, None) == datetime(2026, 6, 1, 0, 0, tzinfo=KST)


def test_effective_month_start_ignores_other_month_budget_start():
    # 도입일이 이번 달(6월)이 아니면(과거/미래) 달력 월 1일 사용
    now = datetime(2026, 6, 15, 12, 0, tzinfo=KST)
    assert effective_month_start(now, datetime(2026, 5, 3, tzinfo=KST)) == datetime(2026, 6, 1, 0, 0, tzinfo=KST)
    assert effective_month_start(now, datetime(2026, 7, 9, tzinfo=KST)) == datetime(2026, 6, 1, 0, 0, tzinfo=KST)


def test_week_count_same_week_is_one():
    eff = datetime(2026, 6, 12, 0, 0, tzinfo=KST)   # 금
    now = datetime(2026, 6, 12, 18, 0, tzinfo=KST)  # 같은 주
    assert week_count(eff, now) == 1


def test_week_count_counts_monday_resets():
    # 도입 6/12(금, 2주차 6/8~14) → 오늘 6/15(월, 3주차) = 2회 충전
    eff = datetime(2026, 6, 12, 0, 0, tzinfo=KST)
    now = datetime(2026, 6, 15, 9, 0, tzinfo=KST)
    assert week_count(eff, now) == 2


def test_week_count_partial_first_week_of_month():
    # 7/1(수) effective → 1주차. 7/6(월) → 2주차
    assert week_count(datetime(2026, 7, 1, tzinfo=KST), datetime(2026, 7, 1, 12, tzinfo=KST)) == 1
    assert week_count(datetime(2026, 7, 1, tzinfo=KST), datetime(2026, 7, 6, 9, tzinfo=KST)) == 2


def test_burndown_clamps_to_budget_start():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 50.0, session="pre")    # 도입 전(제외)
    _insert(conn, "2026-06-13T00:00:00Z", 10.0, session="post")   # 도입 후
    now = datetime(2026, 6, 15, 12, 0, tzinfo=KST)
    bs = datetime(2026, 6, 12, 0, 0, tzinfo=KST)
    bd = burndown(conn, Budget(claude=100, codex=0), now, "claude", budget_start=bs)
    assert bd.spent == 10.0                       # 6/5 제외, 6/13만
    # 기간 6/12~6/30(19일), 경과 6/12~6/15 = 달력 4일
    assert bd.days_in_month == 19
    assert bd.day_of_month == 4
    # 경과 영업일 2(6/12 금·6/15 월, 주말 6/13·6/14 제외), 남은 영업일 11
    assert bd.business_days_elapsed == 2
    assert bd.daily_avg == 5.0                     # 10 / 2 영업일
    assert bd.projected_month == round(10 + 5.0 * 11, 4)


def test_burndown_no_budget_start_is_unchanged():
    # budget_start 미지정이면 기존(달력 월) 동작 그대로
    conn = connect(":memory:")
    for _ in range(3):
        _insert(conn, "2026-06-05T00:00:00Z", 10.0, session=str(_))
    bd = burndown(conn, Budget(claude=100, codex=0), NOW, "claude")
    assert bd.spent == 30.0
    assert bd.days_in_month == 30
    assert bd.day_of_month == 10                  # NOW = 6/10 (달력 경계는 그대로)
    assert bd.daily_avg == 3.75                   # 30 / 8 영업일


# ─── codex_burndown: 주간 누적(carryover) 모델 ────────────────────────────────


def test_codex_burndown_carryover_denominator_and_remaining():
    conn = connect(":memory:")
    # 월한도 40 → W=10. 도입 6/12(2주차), 오늘 6/15(3주차) → N=2 → 분모 20
    _insert(conn, "2026-06-13T00:00:00Z", 6.0, session="x", provider="codex")  # KST 6/13 09:00
    now = datetime(2026, 6, 15, 12, 0, tzinfo=KST)
    bs = datetime(2026, 6, 12, 0, 0, tzinfo=KST)
    cb = codex_burndown(conn, Budget(claude=0, codex=40), now, budget_start=bs)
    assert cb.weekly_limit == 10.0
    assert cb.weeks_elapsed == 2
    assert cb.limit_to_date == 20.0
    assert cb.spent == 6.0
    assert cb.remaining == 14.0          # 20 - 6 (3주차 새 10 + 2주차 미사용 4 이월)
    assert cb.pct == 0.3
    assert cb.status == "ok"


def test_codex_burndown_excludes_pre_budget_start():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 99.0, session="pre", provider="codex")  # 도입 전
    _insert(conn, "2026-06-13T00:00:00Z", 3.0, session="post", provider="codex")
    now = datetime(2026, 6, 15, 12, 0, tzinfo=KST)
    bs = datetime(2026, 6, 12, 0, 0, tzinfo=KST)
    cb = codex_burndown(conn, Budget(claude=0, codex=40), now, budget_start=bs)
    assert cb.spent == 3.0               # 6/5 제외


def test_codex_burndown_exceeds_when_over_accumulated():
    conn = connect(":memory:")
    _insert(conn, "2026-06-13T00:00:00Z", 25.0, session="x", provider="codex")
    now = datetime(2026, 6, 15, 12, 0, tzinfo=KST)
    bs = datetime(2026, 6, 12, 0, 0, tzinfo=KST)
    cb = codex_burndown(conn, Budget(claude=0, codex=40), now, budget_start=bs)
    assert cb.limit_to_date == 20.0
    assert cb.spent == 25.0
    assert cb.remaining == -5.0
    assert cb.status == "exceeds"


def test_codex_burndown_no_budget_start_uses_month_first():
    conn = connect(":memory:")
    # 도입일 없음 → 6/1부터. 6/1=월이라 6/15(월)=3주차 → N=3 → 분모 30
    _insert(conn, "2026-06-02T00:00:00Z", 5.0, session="x", provider="codex")
    now = datetime(2026, 6, 15, 12, 0, tzinfo=KST)
    cb = codex_burndown(conn, Budget(claude=0, codex=40), now)
    assert cb.weeks_elapsed == 3
    assert cb.limit_to_date == 30.0
    assert cb.spent == 5.0


def test_codex_burndown_week_spent_only_current_week():
    conn = connect(":memory:")
    _insert(conn, "2026-06-13T00:00:00Z", 4.0, session="prev", provider="codex")  # 2주차
    _insert(conn, "2026-06-15T03:00:00Z", 2.0, session="cur", provider="codex")   # KST 6/15 12:00 (3주차)
    now = datetime(2026, 6, 15, 18, 0, tzinfo=KST)
    bs = datetime(2026, 6, 12, 0, 0, tzinfo=KST)
    cb = codex_burndown(conn, Budget(claude=0, codex=40), now, budget_start=bs)
    assert cb.spent == 6.0               # 전체 누적
    assert cb.week_spent == 2.0          # 이번 주(6/15~)만


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
    bd = burndown(conn, Budget(claude=0, codex=0), NOW, "claude")
    cards = insights(conn, bd, NOW, None)
    # "캐시 재구축"으로 매칭(기존 캐시활용 경고는 "컨텍스트 재구축"이라 미충돌)
    rebuild = [c for c in cards if "캐시 재구축" in c.text]
    assert len(rebuild) == 1
    assert "1개 세션" in rebuild[0].text   # 2일 miss여도 고유 세션 1


def test_insights_no_rebuild_for_first_day_only():
    conn = connect(":memory:")
    # 첫 등장일만 — 캐시 빈약해도 is_continued=False라 제외
    _insert(conn, "2026-06-06T00:00:00Z", 1.0, session="s", cache_read=0, input_t=1000)
    bd = burndown(conn, Budget(claude=0, codex=0), NOW, "claude")
    cards = insights(conn, bd, NOW, None)
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
    bd = burndown(conn, Budget(claude=100, codex=0), NOW, "claude")
    cards = insights(conn, bd, NOW, None, cov=cov)
    texts = [c.text for c in cards]
    # 모델 "종" 수(1) + 토큰 비중(50%) 형태, "설정에서 확인" 포함
    assert any("미식별 1종" in t and "50%" in t and "설정" in t for t in texts)


def test_insights_no_unpriced_warning_when_coverage_clean():
    conn = connect(":memory:")
    _insert(conn, TS, 1.0, model="claude-opus-4-8", input_t=100)
    cov = pricing_coverage(conn, COV_PRICING)
    bd = burndown(conn, Budget(claude=100, codex=0), NOW, "claude")
    cards = insights(conn, bd, NOW, None, cov=cov)
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


# --- _compute_burndown 영업일 전환 + D-day 신규 필드 -----------------------


def test_burndown_business_day_fields_on_track():
    conn = connect(":memory:")
    for _ in range(3):
        _insert(conn, "2026-06-05T00:00:00Z", 10.0, session=str(_))
    bd = burndown(conn, Budget(claude=100, codex=0), NOW, "claude")   # NOW=6/10
    assert bd.business_days_elapsed == 8       # 6/1~6/10, 주말 6/6·6/7 제외
    assert bd.business_days_left == 14         # 6/11~6/30 영업일
    assert bd.daily_avg == 3.75                # 30 / 8 영업일
    assert bd.exhaust_date is None             # remaining 70 / 3.75 ≈ 19영업일 > 14 남음
    assert bd.dday_warning is False


def test_burndown_predicts_exhaust_date_in_business_days():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 50.0)
    bd = burndown(conn, Budget(claude=100, codex=0), NOW, "claude")
    assert bd.daily_avg == 6.25                # 50 / 8
    assert bd.exhaust_date == date(2026, 6, 22)  # 6/10 + 8영업일
    assert bd.exhaust_day == 22                # exhaust_date.day(cli 호환)
    assert bd.idle_business_days == 7          # 6/22~6/30 영업일(월말 공백)
    assert bd.dday_warning is True             # 공백 7영업일 ≥ 3


def test_burndown_dday_warning_on_low_remaining():
    # 추세는 느려 이번 달 소진은 안 하지만 잔량 ≤20% → 경고
    conn = connect(":memory:")
    _insert(conn, "2026-06-02T00:00:00Z", 85.0)
    now = datetime(2026, 6, 29, 12, 0, tzinfo=KST)
    bd = burndown(conn, Budget(claude=100, codex=0), now, "claude")
    assert bd.exhaust_date is None
    assert bd.idle_business_days == 0
    assert bd.dday_warning is True             # 잔량 15% ≤ 20%


def test_burndown_calendar_fallback_when_no_business_days_elapsed():
    # 월초 주말(8/1 토·8/2 일)에만 사용 → 경과 영업일 0 → 달력 fallback(거짓 안전 방지)
    conn = connect(":memory:")
    _insert(conn, "2026-08-01T03:00:00Z", 10.0)   # KST 8/1 12:00
    now = datetime(2026, 8, 2, 12, 0, tzinfo=KST)
    bd = burndown(conn, Budget(claude=100, codex=0), now, "claude")
    assert bd.business_days_elapsed == 0
    assert bd.daily_avg == 5.0                 # fallback: 10 / 2 달력일
    assert bd.projected_month == 155.0         # 5 × 31


# --- 공식(회사) 사용량 병합 official_merged_burndown -------------------------


def test_official_merge_no_snapshot_uses_cli():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 30.0)
    m = official_merged_burndown(conn, Budget(claude=100, codex=0), NOW, "claude")
    assert isinstance(m, OfficialMergedBurndown)
    assert m.official_spent is None
    assert m.cli_spent == 30.0
    assert m.burndown.spent == 30.0            # 공식 없음 → CLI 그대로
    assert m.missing_delta == 0.0
    assert m.stale_days is None


def test_official_merge_official_higher_shows_missing_delta():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 30.0)
    insert_official_snapshot(
        conn, provider="claude", target_month="2026-06", cumulative_usd=45.0,
        snapshot_ts="2026-06-09T09:00:00+09:00", created_at="2026-06-09T09:00:00+09:00",
    )
    m = official_merged_burndown(conn, Budget(claude=100, codex=0), NOW, "claude")
    assert m.official_spent == 45.0
    assert m.cli_spent == 30.0
    assert m.burndown.spent == 45.0            # max(45, 30) = 공식
    assert m.missing_delta == 15.0             # 45 - 30 (웹/앱 등 CLI 미포함분)
    assert m.official_lt_cli is False
    assert m.burndown.pct == 0.45              # 병합 spent로 재계산


def test_official_merge_official_lower_keeps_cli():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 60.0)
    insert_official_snapshot(
        conn, provider="claude", target_month="2026-06", cumulative_usd=40.0,
        snapshot_ts="2026-06-09T09:00:00+09:00", created_at="2026-06-09T09:00:00+09:00",
    )
    m = official_merged_burndown(conn, Budget(claude=100, codex=0), NOW, "claude")
    assert m.burndown.spent == 60.0            # max(40, 60) = CLI(공식 과소 → 차단)
    assert m.missing_delta == 0.0
    assert m.official_lt_cli is True           # 공식 < CLI → 주의 신호


def test_official_merge_stale_days():
    conn = connect(":memory:")
    insert_official_snapshot(
        conn, provider="claude", target_month="2026-06", cumulative_usd=20.0,
        snapshot_ts="2026-06-07T09:00:00+09:00", created_at="2026-06-07T09:00:00+09:00",
    )
    m = official_merged_burndown(conn, Budget(claude=100, codex=0), NOW, "claude")  # NOW 6/10
    assert m.official_spent == 20.0
    assert m.stale_days == 3                    # 6/7 입력 → 6/10 현재, 3일치 미반영


# --- Task 4: Codex 주간 윈도우 헬퍼 ---


def test_codex_weekly_window_anchors_on_first_use():
    conn = connect(":memory:")
    _insert(conn, "2026-06-08T01:00:00Z", 5.0, provider="codex", session="a")  # 첫 사용
    _insert(conn, "2026-06-10T01:00:00Z", 5.0, provider="codex", session="b")
    start, end = codex_weekly_window(conn)
    assert start.date().isoformat() == "2026-06-08"   # 첫 사용 KST 날짜(+9 → 10:00)
    assert (end - start) == timedelta(days=7)


def test_codex_weekly_window_reanchors_after_idle():
    conn = connect(":memory:")
    _insert(conn, "2026-06-01T01:00:00Z", 5.0, provider="codex", session="a")
    _insert(conn, "2026-06-12T01:00:00Z", 5.0, provider="codex", session="b")  # 11일 뒤(>7) → 재앵커
    start, _ = codex_weekly_window(conn)
    assert start.date().isoformat() == "2026-06-12"   # 마지막 사용으로 재앵커


def test_codex_weekly_window_none_without_usage():
    conn = connect(":memory:")
    _insert(conn, "2026-06-08T01:00:00Z", 5.0, provider="claude", session="a")  # claude만
    assert codex_weekly_window(conn) is None


# --- Task 5: official_view (Claude 버킷 + Codex 2게이지 + 예측 렌즈) ---


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
    v = official_view(conn, "claude", NOW, Budget(claude=100, codex=50), 0.04)
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
    v = official_view(conn, "claude", NOW, Budget(claude=100, codex=50), 0.04)
    assert v.status == "ok"
    assert v.period_used_usd == 30.0 and v.period_limit_usd == 100.0
    assert {b["bucket_key"] for b in v.buckets} == {"monthly", "event"}
    # 월 버킷 resets_at은 다음 달 경계로 채워짐
    monthly = next(b for b in v.buckets if b["bucket_key"] == "monthly")
    assert monthly["resets_at"].startswith("2026-07-01")


def test_official_view_codex_weekly_from_local():
    conn = connect(":memory:")
    # 로컬 Codex 사용(주간 used 근거)
    _insert(conn, "2026-06-09T01:00:00Z", 12.0, provider="codex", session="a")
    # 공식 월간 한도(주간 한도 = 80/4 = 20)
    insert_official_buckets(
        conn, provider="codex", fetched_at="2026-06-10T09:00:00+09:00",
        buckets=[_ob("monthly", "codex_monthly", 20.0, 80.0, raw="individual_limit",
                     unit="credit", util=25.0)],
        created_at="2026-06-10T09:00:00+09:00",
    )
    now = datetime(2026, 6, 11, 12, 0, tzinfo=KST)
    v = official_view(conn, "codex", now, Budget(claude=100, codex=50), 0.04)
    assert v.period_used_usd == 20.0 and v.period_limit_usd == 80.0  # 월간(공식)
    assert v.weekly_limit_usd == 20.0      # 공식 월 한도 80 ÷ 4
    assert v.weekly_used_usd == 12.0       # 로컬 윈도우 합(첫 사용 6/9~)
    assert v.weekly_estimated is True


def test_official_view_codex_weekly_fallback_budget():
    conn = connect(":memory:")
    _insert(conn, "2026-06-09T01:00:00Z", 5.0, provider="codex", session="a")
    now = datetime(2026, 6, 11, 12, 0, tzinfo=KST)
    # 공식 없음 → 주간 한도 = budget.codex(50) ÷ 4 = 12.5, 월간은 no_data
    v = official_view(conn, "codex", now, Budget(claude=100, codex=50), 0.04)
    assert v.weekly_limit_usd == 12.5
    assert v.weekly_used_usd == 5.0
    assert v.period_used_usd is None       # 공식 월간 없음


def test_official_view_lens_from_series():
    conn = connect(":memory:")
    # 두 스냅샷(차분 → 일일 소비속도). 6/8 used 10 → 6/10 used 30, 2영업일 차분 20 → 10/영업일
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-08T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 10.0, 100.0, raw="spend")],
                            created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend")],
                            created_at="x")
    v = official_view(conn, "claude", NOW, Budget(claude=100, codex=50), 0.04)
    assert v.lens is not None
    assert v.lens.daily_rate_usd == 10.0   # (30-10) / 2 영업일
    assert v.active_key == "monthly"


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
    v = official_view(conn, "claude", NOW, Budget(claude=100, codex=50), 0.04)
    assert v.active_key == "monthly"   # 최근 차분 30 > 2 → tie-break(event 우선) 무시하고 monthly
