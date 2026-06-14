from datetime import datetime

import pytest

from tokenomy.aggregate import (
    KST, burndown, by_day_session, by_month, by_project, by_session, by_week,
    combined_burndown, daily_series, insights, month_bounds, parse_ts, period_bounds,
    session_detail,
)
from tokenomy.db import connect
from tokenomy.budget import Budget
from tokenomy.web.views import history_context, overview_context, session_context

# June 2026 has 30 days
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=KST)  # day 10 of 30


def _insert(conn, ts, cost, project="/p", session="s", cache_read=0, input_t=0, priced=1, provider="claude"):
    conn.execute(
        "INSERT INTO messages(dedup_key,provider,session_id,project,ts,model,"
        "input_tokens,cache_creation,cache_read,cost_usd,priced) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (f"{ts}-{cost}-{session}-{project}", provider, session, project, ts,
         "claude-opus-4-8", input_t, 0, cache_read, cost, priced),
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
    assert bd.daily_avg == 3.0          # 30 / 10 days
    assert bd.projected_month == 90.0   # 3 * 30
    assert bd.on_track is True
    assert bd.exhaust_day is None       # 100/3 = 33.3 > 30


def test_burndown_over_budget_predicts_exhaust():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 50.0)
    bd = burndown(conn, Budget(claude=100, codex=0), NOW, "claude")
    assert bd.daily_avg == 5.0          # 50 / 10
    assert bd.projected_month == 150.0
    assert bd.on_track is False
    assert bd.exhaust_day == 20         # 100/5


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
            web_search, web_fetch, cost_usd, priced, request_id, is_sidechain)
           VALUES (:dedup_key,:provider,:session_id,:project,:ts,:model,
            :input_tokens,:output_tokens,:cache_creation,:cache_read,
            :web_search,:web_fetch,:cost_usd,:priced,:request_id,:is_sidechain)""",
        {
            "dedup_key": kw["dedup_key"], "provider": kw.get("provider", "claude"),
            "session_id": kw.get("session_id", "s1"), "project": kw.get("project", "proj"),
            "ts": kw["ts"], "model": kw.get("model", "claude-opus-4-8"),
            "input_tokens": kw.get("input_tokens", 0), "output_tokens": kw.get("output_tokens", 0),
            "cache_creation": kw.get("cache_creation", 0), "cache_read": kw.get("cache_read", 0),
            "web_search": kw.get("web_search", 0), "web_fetch": kw.get("web_fetch", 0),
            "cost_usd": kw.get("cost_usd", 0.0), "priced": kw.get("priced", 1),
            "request_id": kw.get("request_id"), "is_sidechain": kw.get("is_sidechain", 0),
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


def test_daily_series_cumulative():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-01T10:00:00Z", cost_usd=5.0)
    _msg(conn, dedup_key="b", ts="2026-06-02T10:00:00Z", cost_usd=3.0)
    _msg(conn, dedup_key="c", ts="2026-06-02T12:00:00Z", cost_usd=2.0)
    pts = daily_series(conn, "claude", _NOW_STATUS)   # _NOW_STATUS = 6/15
    assert len(pts) == 15                      # 1일~15일
    assert pts[0].day == 1 and pts[0].cumulative_cost == 5.0
    assert pts[1].cumulative_cost == 10.0      # 5 + (3+2) 누적
    assert pts[14].cumulative_cost == 10.0     # 이후 변동 없음, 누적 유지


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
    cfg.write_text('{"budget": {"claude": 100, "codex": 50}}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=10.0, project="/p")
    _msg(conn, dedup_key="b", provider="codex", ts="2026-06-11T10:00:00Z", cost_usd=4.0, project="/p")
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)
    assert ctx["active_nav"] == "dashboard"
    assert ctx["combined"].spent == 14.0           # 10 + 4
    assert ctx["combined"].limit == 150.0
    assert ctx["budget_configured"] is True
    assert len(ctx["cards"]) == 2
    assert {c["provider"] for c in ctx["cards"]} == {"claude", "codex"}
    assert all(c["has_data"] for c in ctx["cards"])
    assert ctx["projects"][0].project == "/p"
    assert ctx["projects"][0].cost == 14.0          # provider 무관 합산
    assert len(ctx["projects"]) <= 10
    assert ctx["has_data"] is True
    assert "daily_labels" in ctx and "insights" in ctx and "sessions" in ctx


def test_overview_context_provider_without_data(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=10.0)
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)
    by_p = {c["provider"]: c for c in ctx["cards"]}
    assert by_p["claude"]["has_data"] is True
    assert by_p["codex"]["has_data"] is False       # codex 로그 없음
    assert ctx["budget_configured"] is False         # 예산 미설정 → 사용량만


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


# ─── history_context: 그룹/평면 + 정렬 ────────────────────────────────────────

def _seed_history(conn):
    # 6/13: s1($2), s2($9)  /  6/12: s1 이어짐($1, 캐시율 낮음)
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-13T01:00:00Z",
         cost_usd=2.0, input_tokens=10, cache_read=90)
    _msg(conn, dedup_key="b", session_id="s2", ts="2026-06-13T02:00:00Z",
         cost_usd=9.0, input_tokens=10, cache_read=90)
    _msg(conn, dedup_key="c", session_id="s1", ts="2026-06-12T01:00:00Z",
         cost_usd=1.0, input_tokens=90, cache_read=10)   # s1 첫 등장은 6/12


def test_history_context_grouped_default(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _seed_history(conn)
    ctx = history_context(conn, "day", _ANCHOR_613, "", "date_desc", now_kst=_NOW_613)
    assert ctx["view"] == "day"
    assert ctx["active_nav"] == "history"
    assert ctx["is_grouped"] is True
    assert ctx["count"] == 3                      # (6/13,s1),(6/13,s2),(6/12,s1)
    assert ctx["total"] == 12.0
    # 날짜 최신순: 6/13 그룹이 먼저
    assert [g.date for g in ctx["groups"]] == ["2026-06-13", "2026-06-12"]
    # 6/13 소계 = 2 + 9 = 11
    assert ctx["groups"][0].subtotal == 11.0
    # 그룹 내부는 비용 내림차순: s2($9) 먼저
    assert [r.session_id for r in ctx["groups"][0].rows] == ["s2", "s1"]
    assert ctx["groups"][0].weekday == "토"        # 2026-06-13 = 토


def test_history_context_date_asc(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _seed_history(conn)
    ctx = history_context(conn, "day", _ANCHOR_613, "", "date_asc", now_kst=_NOW_613)
    assert [g.date for g in ctx["groups"]] == ["2026-06-12", "2026-06-13"]


def test_history_context_day_cost_sorts_groups_by_subtotal(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _seed_history(conn)
    ctx = history_context(conn, "day", _ANCHOR_613, "", "day_cost", now_kst=_NOW_613)
    # 6/13 소계 11 > 6/12 소계 1
    assert [g.date for g in ctx["groups"]] == ["2026-06-13", "2026-06-12"]


def test_history_context_flat_cost(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _seed_history(conn)
    ctx = history_context(conn, "day", _ANCHOR_613, "", "cost", now_kst=_NOW_613)
    assert ctx["is_grouped"] is False
    assert ctx["groups"] == []
    # 평면 비용 내림차순: s2($9, 6/13), s1($2, 6/13), s1($1, 6/12)
    assert [(r.date, r.cost) for r in ctx["flat_rows"]] == [
        ("2026-06-13", 9.0), ("2026-06-13", 2.0), ("2026-06-12", 1.0),
    ]


def test_history_context_flat_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _seed_history(conn)
    ctx = history_context(conn, "day", _ANCHOR_613, "", "cache", now_kst=_NOW_613)
    assert ctx["is_grouped"] is False
    # 캐시율 오름차순(낮은 것 먼저): 6/12 s1(0.1)이 맨 위
    assert ctx["flat_rows"][0].cache_ratio == 0.1


def test_history_context_nav_and_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _seed_history(conn)
    ctx = history_context(conn, "day", _ANCHOR_613, "claude", "date_desc", now_kst=_NOW_613)
    assert ctx["provider"] == "claude"
    assert ctx["period_label"] == "2026-06"
    assert ctx["anchor"] == "2026-06-13"
    assert ctx["prev_anchor"] == "2026-05-31"     # 6/1 - 1일
    assert ctx["next_anchor"] == "2026-07-01"
    assert ctx["has_next"] is False               # _NOW_613이 6월 → 다음 없음


def test_history_context_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    ctx = history_context(connect(":memory:"), "day", _ANCHOR_613, "", "date_desc", now_kst=_NOW_613)
    assert ctx["count"] == 0 and ctx["total"] == 0.0 and ctx["groups"] == []
    assert ctx["last_ts"] is None


def test_history_context_session_view(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-10T10:00:00Z", cost_usd=2.0)
    _msg(conn, dedup_key="b", session_id="s2", ts="2026-06-11T10:00:00Z", cost_usd=9.0)
    ctx = history_context(conn, "session", _ANCHOR_613, "", "cost", now_kst=_NOW_613)
    assert ctx["view"] == "session"
    assert [r.session_id for r in ctx["rows"]] == ["s2", "s1"]
    assert ctx["total"] == 11.0


def test_history_context_folder_view(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", project="/p", ts="2026-06-10T10:00:00Z", cost_usd=4.0)
    ctx = history_context(conn, "folder", _ANCHOR_613, "", "cost", now_kst=_NOW_613)
    assert ctx["view"] == "folder"
    assert ctx["rows"][0].project == "/p"
    assert ctx["total"] == 4.0


def test_history_context_week_view(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-09T10:00:00Z", cost_usd=3.0)
    ctx = history_context(conn, "week", _ANCHOR_613, "", "recent", now_kst=_NOW_613)
    assert ctx["view"] == "week"
    assert ctx["total"] == 3.0
    assert ctx["period_label"] == "2026-06"


def test_history_context_month_view(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-10T10:00:00Z", cost_usd=6.0)
    ctx = history_context(conn, "month", _ANCHOR_613, "", "recent", now_kst=_NOW_613)
    assert ctx["view"] == "month"
    assert ctx["period_label"] == "2026"
    assert ctx["rows"][0].label == "2026-06"
    assert ctx["has_next"] is False           # 2026이 올해 → 다음 연도 없음


# ─── by_week / by_month ───────────────────────────────────────────────────────


def test_by_week_covers_weeks_overlapping_month():
    conn = connect(":memory:")
    # 6월(2026-06) 앵커. 6/1(월)~ 주들. 6/2 KST에 $5, 6/9 KST에 $3
    _insert(conn, "2026-06-01T01:00:00Z", 5.0, session="a")  # KST 6/1 10:00 → 첫 주
    _insert(conn, "2026-06-08T01:00:00Z", 3.0, session="b")  # KST 6/8 10:00 → 둘째 주
    weeks = by_week(conn, "claude", datetime(2026, 6, 15, tzinfo=KST))
    # 6월과 겹치는 주가 5개 이상, 각 주에 week_start(월요일) 라벨
    starts = [w.week_start for w in weeks]
    assert "2026-06-01" in starts            # 6/1은 월요일
    assert "2026-06-08" in starts
    wk = {w.week_start: w for w in weeks}
    assert wk["2026-06-01"].cost == 5.0
    assert wk["2026-06-08"].cost == 3.0
    assert wk["2026-06-01"].sessions == 1


def test_by_week_full_week_even_when_partial_in_month():
    conn = connect(":memory:")
    # 5/29(금) 지출은 6/1이 포함된 주(5/25~5/31)가 아니라, 6월 첫 주는 6/1(월)~6/7.
    # 6월과 겹치는 주만 반환되는지 — 5월 단독 주는 제외
    _insert(conn, "2026-05-20T01:00:00Z", 9.0, session="m")  # KST 5/20 → 5월 셋째 주(6월과 무관)
    _insert(conn, "2026-06-03T01:00:00Z", 2.0, session="j")  # KST 6/3 → 6월 첫 주
    weeks = by_week(conn, "claude", datetime(2026, 6, 15, tzinfo=KST))
    starts = [w.week_start for w in weeks]
    assert "2026-05-18" not in starts        # 6월과 안 겹치는 5월 주 제외
    assert "2026-06-01" in starts


def test_by_week_partial_leading_week_july_anchor():
    conn = connect(":memory:")
    # 2026-07 첫 주는 6/29(월)~7/5. 6/30 KST 지출이 그 주(week_start 2026-06-29)에 집계돼야 한다.
    _insert(conn, "2026-06-30T01:00:00Z", 7.0, session="p")   # KST 6/30 10:00 → 6/29 주
    _insert(conn, "2026-07-08T01:00:00Z", 2.0, session="q")   # KST 7/8 → 7/6 주
    weeks = by_week(conn, "claude", datetime(2026, 7, 15, tzinfo=KST))
    wk = {w.week_start: w for w in weeks}
    assert "2026-06-29" in wk
    assert wk["2026-06-29"].cost == 7.0


def test_by_month_only_months_with_data():
    conn = connect(":memory:")
    _insert(conn, "2026-03-10T01:00:00Z", 4.0, session="a")  # KST 3월
    _insert(conn, "2026-06-10T01:00:00Z", 6.0, session="b")  # KST 6월
    months = by_month(conn, "claude", 2026)
    labels = [m.label for m in months]
    assert "2026-03" in labels and "2026-06" in labels
    assert "2026-01" not in labels            # 데이터 없는 달 생략
    by_l = {m.label: m for m in months}
    assert by_l["2026-06"].cost == 6.0
    assert by_l["2026-03"].month == 3
    # 기본 정렬: 최신 달 먼저
    assert labels.index("2026-06") < labels.index("2026-03")


def test_by_month_filters_year():
    conn = connect(":memory:")
    _insert(conn, "2025-06-10T01:00:00Z", 9.0, session="y25")
    _insert(conn, "2026-06-10T01:00:00Z", 6.0, session="y26")
    months = by_month(conn, "claude", 2026)
    assert [m.label for m in months] == ["2026-06"]
