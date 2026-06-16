# 내역(History) 화면 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 가계부식 "내역" 화면(`/history`)을 추가해 토큰 지출을 (날짜 × 세션) 단위로 일별 흐름으로 보여준다.

**Architecture:** 적재(parser/db)는 손대지 않고 읽기 경로만 추가한다. `aggregate.by_day_session`이 메시지를 (KST날짜, session_id)로 버킷팅하고 이어짐(↩)·캐시미스(⚠)를 판정 → `views.history_context`가 정렬에 따라 그룹/평면으로 조립 → `/history` 라우트가 전체 페이지 또는 fragment를 렌더 → 드롭다운/정렬은 vanilla JS fetch로 표만 부분 갱신.

**Tech Stack:** Python 3.14 / FastAPI / Jinja2 / SQLite(stdlib) / pytest. 신규 런타임 의존성 없음(부분 갱신은 vanilla JS).

설계 스펙: `docs/superpowers/specs/2026-06-13-history-view-design.md`

---

## 파일 구조

**수정:**
- `tokenomy/aggregate.py` — `DaySessionRow`/`DayGroup` 데이터클래스 + `by_day_session()` 추가
- `tokenomy/web/views.py` — `history_context()` + `_group_by_date()` 추가
- `tokenomy/web/app.py` — `GET /history` 라우트 추가, `history_context` import
- `tokenomy/web/templates/_tabs.html` — 상단바를 `_topbar.html`로 분리해 include(기존 동작 불변)
- `tokenomy/web/templates/overview.html` / `dashboard.html` — 세션 미리보기에 "전체 내역 보기 →" 링크
- `tokenomy/web/static/style.css` — 날짜 그룹 헤더·필터 바·↩/⚠ 신호 스타일
- `tests/test_aggregate.py` — `by_day_session` + `history_context` 테스트
- `tests/test_web.py` — `/history` 라우트 테스트

**생성:**
- `tokenomy/web/templates/_topbar.html` — 상단바(월·데이터최신·설정·새로고침·배너)만. `_tabs.html`에서 분리
- `tokenomy/web/templates/history.html` — `/history` 전체 페이지
- `tokenomy/web/templates/_history_rows.html` — 표 영역 fragment(부분 갱신 응답 + 전체 페이지 공용)

**책임 경계:** 집계(aggregate)는 행 생성·신호 판정만, 화면 조립(views)은 정렬·그룹/평면 결정만, 라우트(app)는 검증·템플릿 선택만. 스펙의 계층 분리를 그대로 따른다.

---

## Task 1: `by_day_session` 집계 — (날짜 × 세션) 행 + 이어짐/캐시미스 판정

**Files:**
- Modify: `tokenomy/aggregate.py` (데이터클래스는 `SessionRow` 정의부 근처, 함수는 `by_session` 뒤)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_aggregate.py` 상단 import에 `by_day_session`, `month_bounds`를 추가한다(기존 import 줄에 이어붙임):

```python
from tokenomy.aggregate import (
    KST, burndown, by_day_session, by_project, by_session, combined_burndown,
    daily_series, insights, month_bounds, parse_ts, period_bounds, session_detail,
)
```

파일 맨 아래에 테스트를 추가한다. `_msg`(파일에 이미 있는 fixture)와 `month_bounds`로 6월 범위를 만든다:

```python
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k by_day_session -x`
Expected: FAIL — `ImportError: cannot import name 'by_day_session'`

- [ ] **Step 3: 데이터클래스 + 함수 구현**

`tokenomy/aggregate.py`의 `SessionRow` 데이터클래스 정의(`@dataclass class SessionRow:` 블록) **바로 위**에 두 데이터클래스를 추가한다:

```python
@dataclass
class DaySessionRow:
    """한 행 = (KST 날짜 × 세션). 같은 세션이 N일 걸치면 N행."""
    date: str               # "2026-06-13" (KST)
    session_id: str
    provider: str | None
    summary: str | None     # 작업요약(aiTitle 캐시)
    project: str | None
    label: str | None       # 수동 귀속 라벨
    cost: float
    msgs: int
    cache_ratio: float
    is_continued: bool      # 세션 최초등장일보다 이후 날짜인가 → ↩
    cache_miss: bool        # is_continued AND cache_ratio < 임계 → ⚠


@dataclass
class DayGroup:
    """날짜별 묶음(그룹 모드). views._group_by_date가 생성."""
    date: str
    weekday: str            # '금'
    subtotal: float
    rows: list  # list[DaySessionRow]
```

`by_session` 함수 정의가 끝나는 곳(다음 `@dataclass class ModelRow:` 직전) 사이에 함수를 추가한다:

```python
def by_day_session(conn, provider: str | None, *, start: datetime, nxt: datetime) -> list[DaySessionRow]:
    """(KST날짜 × 세션) 단위 행. 기간 [start, nxt) 내 메시지를 날짜+세션으로 버킷팅한다.

    is_continued: 세션 최초 등장일(전체 messages의 MIN(ts))보다 이 행 날짜가 이후인가.
                  조회 범위가 아닌 전체에서 구해야 지난달 시작→이번달 이어짐을 오판하지 않는다.
    cache_miss:   is_continued AND cache_ratio < INSIGHT_CACHE_READ_MIN(첫 등장일은 절대 제외).
    """
    rows = _range_rows(conn, provider, start, nxt)

    # 세션별 최초 등장일(전체 기준, KST 날짜 문자열)
    first_day: dict[str, str] = {}
    for r in conn.execute("SELECT session_id, MIN(ts) m FROM messages GROUP BY session_id").fetchall():
        dt = parse_ts(r["m"])
        if dt:
            first_day[r["session_id"]] = dt.date().isoformat()

    meta = {
        r["session_id"]: (r["label"], r["summary"], r["provider"])
        for r in conn.execute("SELECT session_id, label, summary, provider FROM sessions").fetchall()
    }

    agg: dict = {}
    for r in rows:
        dt = parse_ts(r["ts"])
        if not dt:
            continue
        date = dt.date().isoformat()
        key = (date, r["session_id"])
        a = agg.setdefault(
            key,
            {"project": r["project"], "cost": 0.0, "msgs": 0, "cr": 0, "den": 0},
        )
        a["cost"] += r["cost_usd"] or 0
        a["msgs"] += 1
        a["cr"] += r["cache_read"] or 0
        a["den"] += (r["input_tokens"] or 0) + (r["cache_creation"] or 0) + (r["cache_read"] or 0)

    out: list[DaySessionRow] = []
    for (date, sid), a in agg.items():
        cache_ratio = (a["cr"] / a["den"]) if a["den"] else 0.0
        is_continued = first_day.get(sid, date) < date
        cache_miss = is_continued and cache_ratio < INSIGHT_CACHE_READ_MIN
        label, summary, sprov = meta.get(sid, (None, None, None))
        out.append(DaySessionRow(
            date=date, session_id=sid, provider=sprov,
            summary=summary, project=a["project"], label=label,
            cost=round(a["cost"], 4), msgs=a["msgs"],
            cache_ratio=round(cache_ratio, 4),
            is_continued=is_continued, cache_miss=cache_miss,
        ))
    return out
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k by_day_session -v`
Expected: PASS (9 passed)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): by_day_session — 날짜x세션 행 + 이어짐/캐시미스 판정"
```

---

## Task 2: `history_context` — 정렬에 따른 그룹/평면 조립

**Files:**
- Modify: `tokenomy/web/views.py` (import에 `by_day_session`, `month_bounds`, `DayGroup` 추가; `sessions_context` 뒤에 함수 추가)
- Test: `tests/test_aggregate.py` (views 테스트가 이 파일에 함께 있음)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_aggregate.py`의 views import 줄에 `history_context`를 추가한다:

```python
from tokenomy.web.views import (
    dashboard_context, history_context, overview_context, projects_context,
    sessions_context, session_context,
)
```

파일 맨 아래에 추가한다:

```python
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
    ctx = history_context(conn, _ANCHOR_613, "", "date_desc", now_kst=_NOW_613)
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
    ctx = history_context(conn, _ANCHOR_613, "", "date_asc", now_kst=_NOW_613)
    assert [g.date for g in ctx["groups"]] == ["2026-06-12", "2026-06-13"]


def test_history_context_day_cost_sorts_groups_by_subtotal(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _seed_history(conn)
    ctx = history_context(conn, _ANCHOR_613, "", "day_cost", now_kst=_NOW_613)
    # 6/13 소계 11 > 6/12 소계 1
    assert [g.date for g in ctx["groups"]] == ["2026-06-13", "2026-06-12"]


def test_history_context_flat_cost(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _seed_history(conn)
    ctx = history_context(conn, _ANCHOR_613, "", "cost", now_kst=_NOW_613)
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
    ctx = history_context(conn, _ANCHOR_613, "", "cache", now_kst=_NOW_613)
    assert ctx["is_grouped"] is False
    # 캐시율 오름차순(낮은 것 먼저): 6/12 s1(0.1)이 맨 위
    assert ctx["flat_rows"][0].cache_ratio == 0.1


def test_history_context_nav_and_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _seed_history(conn)
    ctx = history_context(conn, _ANCHOR_613, "claude", "date_desc", now_kst=_NOW_613)
    assert ctx["provider"] == "claude"
    assert ctx["active_tab"] == "claude"
    assert ctx["period_label"] == "2026-06"
    assert ctx["anchor"] == "2026-06-13"
    assert ctx["prev_anchor"] == "2026-05-31"     # 6/1 - 1일
    assert ctx["next_anchor"] == "2026-07-01"
    assert ctx["has_next"] is False               # _NOW_613이 6월 → 다음 없음
```

(`_NOW_613`, `_ANCHOR_613`은 파일에 이미 정의되어 있다: `datetime(2026, 6, 13, 12, 0, tzinfo=KST)`, `datetime(2026, 6, 13, tzinfo=KST)`.)

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k history_context -x`
Expected: FAIL — `ImportError: cannot import name 'history_context'`

- [ ] **Step 3: 구현**

`tokenomy/web/views.py` 맨 위 import를 수정한다. `from datetime import datetime, timedelta`를 다음으로 바꾼다:

```python
from datetime import date, datetime, timedelta
```

aggregate import 블록에 `DayGroup`, `by_day_session`, `month_bounds`를 추가한다:

```python
from tokenomy.aggregate import (
    KST, PROVIDERS, DayGroup, burndown, by_day_session, by_project, by_session,
    combined_burndown, daily_series, insights, month_bounds, period_bounds, session_detail,
)
```

파일 맨 아래(`sessions_context` 뒤)에 추가한다:

```python
_GROUPED_SORTS = ("date_desc", "date_asc", "day_cost")
_WEEKDAY = "월화수목금토일"


def _group_by_date(rows: list) -> list:
    """DaySessionRow 리스트 → 날짜별 DayGroup. 그룹 내부 행은 비용 내림차순."""
    by: dict = {}
    for r in rows:
        by.setdefault(r.date, []).append(r)
    out = []
    for d, rs in by.items():
        rs.sort(key=lambda x: x.cost, reverse=True)
        wd = _WEEKDAY[date.fromisoformat(d).weekday()]
        out.append(DayGroup(date=d, weekday=wd,
                            subtotal=round(sum(x.cost for x in rs), 4), rows=rs))
    return out


def history_context(conn, anchor_kst: datetime, provider: str,
                    sort: str, now_kst: datetime | None = None) -> dict:
    """내역(/history). 월 고정. sort에 따라 그룹(date_desc/date_asc/day_cost) 또는
    평면(cost/cache)으로 조립한다. 평면은 날짜 그룹을 깨고 단일 정렬 리스트."""
    now = now_kst or datetime.now(KST)
    config = load_config()
    start, nxt = month_bounds(anchor_kst)
    rows = by_day_session(conn, provider or None, start=start, nxt=nxt)
    total = round(sum(r.cost for r in rows), 4)
    count = len(rows)

    is_grouped = sort in _GROUPED_SORTS
    groups: list = []
    flat_rows: list = []
    if is_grouped:
        groups = _group_by_date(rows)
        if sort == "date_asc":
            groups.sort(key=lambda g: g.date)
        elif sort == "day_cost":
            groups.sort(key=lambda g: g.subtotal, reverse=True)
        else:  # date_desc (기본)
            groups.sort(key=lambda g: g.date, reverse=True)
    elif sort == "cache":
        flat_rows = sorted(rows, key=lambda r: r.cache_ratio)            # 낮은 순
    else:  # cost
        flat_rows = sorted(rows, key=lambda r: r.cost, reverse=True)

    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    return {
        "active_tab": provider or "overview",
        "user_label": user_label(config),
        "provider": provider, "sort": sort,
        "is_grouped": is_grouped, "groups": groups, "flat_rows": flat_rows,
        "count": count, "total": total,
        "period_label": start.strftime("%Y-%m"),
        "anchor": anchor_kst.strftime("%Y-%m-%d"),
        "prev_anchor": (start - timedelta(days=1)).strftime("%Y-%m-%d"),
        "next_anchor": nxt.strftime("%Y-%m-%d"),
        "has_next": nxt <= now,
        "month": now.strftime("%Y-%m"),
        "last_ts": last["t"] if last and last["t"] else None,
    }
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k history_context -v`
Expected: PASS (6 passed)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/views.py tests/test_aggregate.py
git commit -m "feat(views): history_context — 정렬별 그룹/평면 조립"
```

---

## Task 3: `GET /history` 라우트 + 입력 검증

**Files:**
- Modify: `tokenomy/web/app.py`
- Test: `tests/test_web.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_web.py` 맨 아래에 추가한다:

```python
def test_history_page_ok(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/history")
    assert r.status_code == 200
    assert "내역" in r.text
    assert 'id="provider-filter"' in r.text       # AI 드롭다운
    assert 'id="sort-filter"' in r.text           # 정렬 드롭다운


def test_history_bad_params_fall_back(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/history?provider=evil&sort=drop")
    assert r.status_code == 200                    # 화이트리스트 폴백, 크래시 없음


def test_history_renders_grouped_rows(tmp_path, monkeypatch):
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
    assert "합계 $3.00" in r.text                   # 일별 소계 또는 기간 합계


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
    assert "<!doctype html>" not in r.text.lower()  # 전체 페이지 chrome 없음
    assert 'id="provider-filter"' not in r.text     # 드롭다운(페이지 셸)도 없음


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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -k history -x`
Expected: FAIL — 404 (`/history` 라우트 없음) → assert status_code 실패

- [ ] **Step 3: 라우트 구현**

`tokenomy/web/app.py`의 views import에 `history_context`를 추가한다:

```python
from tokenomy.web.views import (
    dashboard_context, history_context, overview_context,
    projects_context, sessions_context, session_context,
)
```

`_ORDERS = ("cost", "recent")` 아래에 정렬 화이트리스트를 추가한다:

```python
_HISTORY_SORTS = ("date_desc", "date_asc", "day_cost", "cost", "cache")
```

`sessions_view` 함수 정의 뒤에 라우트를 추가한다:

```python
@app.get("/history")
def history_view(request: Request, anchor: str | None = None, provider: str = "",
                 sort: str = "date_desc", partial: str | None = None,
                 notice: str | None = None):
    provider = provider if provider in PROVIDERS else ""
    sort = sort if sort in _HISTORY_SORTS else "date_desc"
    conn = connect()
    update_tag = check_update(conn)
    ctx = history_context(conn, _parse_anchor(anchor), provider, sort)
    template = "_history_rows.html" if partial == "1" else "history.html"
    return templates.TemplateResponse(
        request, template,
        {**ctx, "notice": notice, "update_tag": update_tag},
    )
```

(템플릿 `history.html` / `_history_rows.html`은 Task 4에서 만든다. 이 단계에서는 라우트만으로 `TemplateNotFound`가 날 수 있으므로 Task 4와 묶어 검증한다. 먼저 Step 4의 라우트 등록만 확인하려면 `test_history_bad_params_fall_back`은 템플릿이 있어야 통과한다 — Task 4 완료 후 함께 PASS.)

- [ ] **Step 4: 임시 확인(라우트 등록)**

Run: `.venv\Scripts\python -c "from tokenomy.web.app import app; print([r.path for r in app.routes if getattr(r,'path','')=='/history'])"`
Expected: `['/history']` 출력(라우트 등록됨). 전체 테스트는 Task 4 후 통과.

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/app.py tests/test_web.py
git commit -m "feat(web): /history 라우트 + 입력 검증(provider/sort/partial)"
```

---

## Task 4: 템플릿 — 상단바 분리 + history.html + fragment

**Files:**
- Create: `tokenomy/web/templates/_topbar.html`
- Modify: `tokenomy/web/templates/_tabs.html`
- Create: `tokenomy/web/templates/history.html`
- Create: `tokenomy/web/templates/_history_rows.html`
- Test: `tests/test_web.py` (Task 3의 테스트가 여기서 통과)

- [ ] **Step 1: 상단바를 `_topbar.html`로 분리**

`tokenomy/web/templates/_topbar.html`을 새로 만든다. 내용은 현재 `_tabs.html`의 상단바+배너 부분을 그대로 옮긴 것이다:

```html
<header class="topbar">
  <div>🪙 Tokenomy · {{ month }} (KST){% if user_label %} · {{ user_label }}{% endif %}</div>
  <div class="topbar-right">
    {% if last_ts %}<span class="muted">데이터 최신: {{ last_ts|kstfmt }}</span>{% endif %}
    <a class="btn" href="/settings">⚙ 설정</a>
    <form method="post" action="/ingest" class="inline"><button class="btn">↻ 새로고침</button></form>
  </div>
</header>

{% if notice == "ingest-failed" %}
<div class="banner error">새로고침(ingest) 중 오류 — 기존 데이터를 표시합니다.</div>
{% endif %}

{% if update_tag %}
<div class="banner update">새 버전 {{ update_tag }} 사용 가능 —
  <a href="https://github.com/genius-kim-samsung/tokenomy/releases/latest" target="_blank" rel="noopener">다운로드</a>
</div>
{% endif %}
```

- [ ] **Step 2: `_tabs.html`이 `_topbar.html`을 include하도록 변경**

`tokenomy/web/templates/_tabs.html` 전체를 다음으로 교체한다(기존 화면 동작은 불변 — 상단바를 partial로 뺐을 뿐):

```html
{% include "_topbar.html" %}

<nav class="tabs">
  <a href="/" class="{{ 'on' if active_tab == 'overview' }}">전체</a>
  <a href="/?provider=claude" class="{{ 'on' if active_tab == 'claude' }}">Claude</a>
  <a href="/?provider=codex" class="{{ 'on' if active_tab == 'codex' }}">Codex</a>
</nav>
```

- [ ] **Step 3: fragment `_history_rows.html` 작성**

`tokenomy/web/templates/_history_rows.html`을 만든다. 그룹/평면 분기와 요약줄·신호를 담는다:

```html
<p class="muted">{{ period_label }} · {{ count }}건 · 합계 ${{ '%.2f'|format(total) }}</p>

{% macro row_cells(r, show_date) %}
  {% if show_date %}<td class="col-time">{{ r.date[5:] }}</td>{% endif %}
  <td>{{ r.provider or '—' }}</td>
  <td class="col-sum" title="{{ r.summary or '' }}">{{ r.summary or '—' }}{% if r.is_continued %} <span class="cont" title="이전 날짜에서 이어진 세션">↩</span>{% endif %}</td>
  <td class="col-proj" title="{{ r.project or '' }}">{{ (r.project or '(unknown)').replace('\\', '/').split('/')[-1] or '(unknown)' }}</td>
  <td>{{ r.label or '—' }}</td>
  <td>${{ '%.2f'|format(r.cost) }}</td>
  <td>{{ r.msgs }}</td>
  <td class="{{ 'cache-miss' if r.cache_miss }}">{{ '%.0f'|format(r.cache_ratio * 100) }}%{% if r.cache_miss %} <span title="이어진 세션인데 캐시 재사용률 낮음 — 재개로 컨텍스트 재구축 가능성">⚠</span>{% endif %}</td>
  <td><a href="/session/{{ r.session_id }}">▸</a></td>
{% endmacro %}

{% if count == 0 %}
  <p class="muted">이 기간 데이터 없음</p>
{% elif is_grouped %}
  {% for g in groups %}
  <div class="day-group">
    <div class="day-head"><span class="day-label">━ {{ g.date[5:] }} ({{ g.weekday }})</span>
      <span class="day-sub">합계 ${{ '%.2f'|format(g.subtotal) }}</span></div>
    <table class="grid sess">
      <thead><tr><th>AI</th><th>작업요약</th><th>프로젝트</th><th>라벨</th><th>비용</th><th>메시지</th><th>캐시%</th><th></th></tr></thead>
      <tbody>
        {% for r in g.rows %}<tr>{{ row_cells(r, false) }}</tr>{% endfor %}
      </tbody>
    </table>
  </div>
  {% endfor %}
{% else %}
  <table class="grid sess">
    <thead><tr><th>날짜</th><th>AI</th><th>작업요약</th><th>프로젝트</th><th>라벨</th><th>비용</th><th>메시지</th><th>캐시%</th><th></th></tr></thead>
    <tbody>
      {% for r in flat_rows %}<tr>{{ row_cells(r, true) }}</tr>{% endfor %}
    </tbody>
  </table>
{% endif %}
```

- [ ] **Step 4: 전체 페이지 `history.html` 작성**

`tokenomy/web/templates/history.html`을 만든다. 상단바 + 필터 바 + 월 네비 + fragment 컨테이너 + 부분갱신 스크립트:

```html
{% extends "base.html" %}
{% block body %}
{% include "_topbar.html" %}

<section class="card">
  <div class="card-head">
    <h2>내역 — 일별 토큰 지출</h2>
    <div class="filters">
      <select id="provider-filter" aria-label="AI 필터">
        <option value="" {{ 'selected' if provider == '' }}>전체</option>
        <option value="claude" {{ 'selected' if provider == 'claude' }}>Claude</option>
        <option value="codex" {{ 'selected' if provider == 'codex' }}>Codex</option>
      </select>
      <select id="sort-filter" aria-label="정렬">
        <option value="date_desc" {{ 'selected' if sort == 'date_desc' }}>날짜 최신순</option>
        <option value="date_asc" {{ 'selected' if sort == 'date_asc' }}>날짜 오래된순</option>
        <option value="day_cost" {{ 'selected' if sort == 'day_cost' }}>일별 지출 많은순</option>
        <option value="cost" {{ 'selected' if sort == 'cost' }}>세션 비용순</option>
        <option value="cache" {{ 'selected' if sort == 'cache' }}>캐시 낮은순</option>
      </select>
    </div>
  </div>

  <div class="period-nav">
    <a class="btn" href="/history?anchor={{ prev_anchor }}&provider={{ provider }}&sort={{ sort }}">‹ 이전</a>
    <span class="label">{{ period_label }}</span>
    {% if has_next %}<a class="btn" href="/history?anchor={{ next_anchor }}&provider={{ provider }}&sort={{ sort }}">다음 ›</a>{% endif %}
  </div>

  <div id="history-table">{% include "_history_rows.html" %}</div>
</section>
{% endblock %}

{% block scripts %}
<script>
  (function () {
    var ANCHOR = "{{ anchor }}";
    var tbl = document.getElementById('history-table');
    var pf = document.getElementById('provider-filter');
    var sf = document.getElementById('sort-filter');
    function refresh() {
      var qs = 'provider=' + encodeURIComponent(pf.value) +
               '&sort=' + encodeURIComponent(sf.value) +
               '&anchor=' + encodeURIComponent(ANCHOR);
      fetch('/history?' + qs + '&partial=1')
        .then(function (r) { return r.text(); })
        .then(function (html) {
          tbl.innerHTML = html;
          history.pushState(null, '', '/history?' + qs);
        });
    }
    pf.addEventListener('change', refresh);
    sf.addEventListener('change', refresh);
  })();
</script>
{% endblock %}
```

- [ ] **Step 5: Task 3 + Task 4 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -k history -v`
Expected: PASS (5 passed)

기존 화면이 깨지지 않았는지도 확인:

Run: `.venv\Scripts\python -m pytest tests/test_web.py -v`
Expected: PASS (전부)

- [ ] **Step 6: 커밋**

```bash
git add tokenomy/web/templates/_topbar.html tokenomy/web/templates/_tabs.html tokenomy/web/templates/history.html tokenomy/web/templates/_history_rows.html
git commit -m "feat(web): history.html + fragment + 상단바 partial 분리"
```

---

## Task 5: 진입 링크 — 대시보드/overview 세션 미리보기에 "전체 내역 보기 →"

**Files:**
- Modify: `tokenomy/web/templates/overview.html` (line 111 부근)
- Modify: `tokenomy/web/templates/dashboard.html` (line 89 부근)
- Test: `tests/test_web.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_web.py` 맨 아래에 추가한다:

```python
def test_overview_has_history_link(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/")
    assert 'href="/history' in r.text
    assert "내역 보기" in r.text


def test_dashboard_has_history_link(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/?provider=claude")
    assert "/history?provider=claude" in r.text
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -k history_link -x`
Expected: FAIL — `assert 'href="/history' in r.text`

- [ ] **Step 3: 링크 추가**

`tokenomy/web/templates/overview.html`에서 복기 섹션의 전체 보기 줄을 찾아 내역 링크를 덧붙인다. 현재:

```html
  <p class="muted"><a href="/sessions">전체 보기 →</a></p>
```

다음으로 교체:

```html
  <p class="muted"><a href="/sessions">전체 보기 →</a> · <a href="/history">전체 내역 보기 →</a></p>
```

`tokenomy/web/templates/dashboard.html`에서 복기 섹션의 전체 보기 줄을 찾아 바꾼다. 현재:

```html
  <p class="muted"><a href="/sessions?provider={{ provider }}">전체 보기 →</a></p>
```

다음으로 교체:

```html
  <p class="muted"><a href="/sessions?provider={{ provider }}">전체 보기 →</a> · <a href="/history?provider={{ provider }}">전체 내역 보기 →</a></p>
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -k history_link -v`
Expected: PASS (2 passed)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/templates/overview.html tokenomy/web/templates/dashboard.html tests/test_web.py
git commit -m "feat(web): 대시보드/overview에 전체 내역 보기 링크"
```

---

## Task 6: 스타일 — 날짜 그룹 헤더 · 필터 바 · ↩/⚠ 신호

**Files:**
- Modify: `tokenomy/web/static/style.css` (파일 끝에 추가)
- Test: `tests/test_web.py` (클래스 존재 확인)

CSS는 시각 요소라 단위 테스트가 약하다. "클래스가 렌더된다" 수준만 검증하고, 실제 모양은 Task 7 스모크에서 눈으로 확인한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_web.py` 맨 아래에 추가한다:

```python
def test_history_renders_signal_classes(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    # s1: 6/9 첫 등장(캐시율 높음), 6/10 이어짐(캐시율 0.1 → cache_miss)
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,"
                 "input_tokens,cache_read,cost_usd,priced) VALUES "
                 "('a','claude','s1','myproj','2026-06-09T01:00:00Z',10,90,1.0,1)")
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,"
                 "input_tokens,cache_read,cost_usd,priced) VALUES "
                 "('b','claude','s1','myproj','2026-06-10T01:00:00Z',90,10,1.0,1)")
    conn.commit()
    r = client.get("/history?anchor=2026-06-10&sort=date_desc")
    assert "day-head" in r.text          # 날짜 그룹 헤더
    assert "cache-miss" in r.text        # 캐시미스 셀 클래스
    assert "↩" in r.text                 # 이어짐 표시
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -k signal_classes -x`
Expected: 그룹 헤더/↩는 이미 Task 4에서 렌더되므로 통과할 수 있으나, `cache-miss`도 Task 4 fragment에 이미 포함되어 있다 → 이 테스트는 Task 4 직후 PASS일 수 있다. CSS가 없어도 클래스는 렌더되므로, 이 단계의 목적은 **클래스 계약 고정**이다. PASS면 그대로 Step 3로(스타일 추가).

- [ ] **Step 3: CSS 추가**

`tokenomy/web/static/style.css` 맨 아래에 추가한다:

```css
/* ── 내역(/history) ── */
.filters { display: flex; gap: 8px; }
.filters select {
  padding: 4px 8px; border: 1px solid var(--line, #ddd);
  border-radius: 6px; background: #fff; font-size: 13px;
}
.day-group { margin-bottom: 14px; }
.day-head {
  display: flex; justify-content: space-between; align-items: baseline;
  padding: 6px 2px; border-bottom: 2px solid var(--line, #e5e5e5); margin-bottom: 2px;
}
.day-label { font-weight: 600; }
.day-sub { color: var(--muted, #888); font-variant-numeric: tabular-nums; }
.cont { color: var(--muted, #999); cursor: help; }       /* ↩ 이어진 세션 */
td.cache-miss { color: #c0392b; font-weight: 600; }       /* ⚠ 재개 캐시미스 의심 */
td.cache-miss span { cursor: help; }
```

(변수 `--line`/`--muted`/`--accent`가 이미 정의돼 있으면 그 값을, 없으면 fallback을 쓴다. 기존 `style.css` 상단의 `:root` 변수명을 확인하고 일치시킬 것.)

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -k signal_classes -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/static/style.css tests/test_web.py
git commit -m "style(web): 내역 화면 — 날짜 그룹/필터/캐시미스 신호 스타일"
```

---

## Task 7: 통합 검증 — 전체 테스트 + 수동 스모크

**Files:** (없음 — 검증만)

- [ ] **Step 1: 전체 테스트**

Run: `.venv\Scripts\python -m pytest`
Expected: PASS (신규 포함 전부. 기존 `test_launcher` 포트 충돌 2건은 환경 이슈로 알려져 있음 — 무관하면 무시)

- [ ] **Step 2: 수동 스모크(부분 갱신 동작 확인)**

서버를 띄운다:

Run: `.venv\Scripts\python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765`

브라우저에서 `http://127.0.0.1:8765/history` 확인:
- 날짜 그룹 헤더 + 일별 소계가 보인다
- AI 드롭다운을 바꾸면 **페이지 새로고침 없이** 표만 갱신된다(Network 탭에 `partial=1` 요청)
- 정렬을 "세션 비용순"으로 바꾸면 그룹이 풀리고 날짜 칸이 있는 평면 표로 바뀐다
- 이어진 세션에 `↩`, 캐시 낮은 이어짐 행에 `⚠`(빨간 캐시%)가 보인다
- 월 `‹ 이전` 링크는 전체 페이지로 이동한다
- 대시보드(`/`)의 복기 섹션에 "전체 내역 보기 →" 링크가 있다

확인 후 `Ctrl+C`로 종료.

- [ ] **Step 3: 최종 커밋(있으면)**

스모크 중 수정이 있었다면 커밋한다. 없으면 생략.

```bash
git add -A && git commit -m "test: 내역 화면 통합 검증" || echo "변경 없음"
```

---

## Self-Review 결과(작성자 점검)

- **스펙 §1(아키텍처):** Task 1~4가 aggregate→views→app→templates 흐름을 그대로 구현. ✓
- **스펙 §2(화면):** 드롭다운 필터(Task 4), 부분 갱신 JS(Task 4 Step 4), 정렬 5종(Task 2), 칸 매핑(Task 4 fragment), 진입 링크(Task 5), 월 네비 전체 페이지 링크(Task 4). ✓
- **스펙 §3(판정):** 이어짐=전체 MIN(ts) 기준(Task 1, `test_..._across_month_boundary`), 캐시미스=이어짐+임계(Task 1), 첫날 제외(Task 1), KST 버킷팅(Task 1), 결측 `—`/`(unknown)`(Task 4), sort 폴백(Task 3), 빈 데이터(Task 4). ✓
- **스펙 §4(테스트):** aggregate 7+시나리오, views 그룹/평면/정렬, app sort폴백·partial·provider. ✓
- **타입 일관성:** `DaySessionRow`/`DayGroup` 필드명이 Task 1 정의와 Task 2/4 사용처에서 일치(`is_continued`,`cache_miss`,`subtotal`,`weekday`). `history_context` 반환 키가 Task 4 템플릿 변수와 일치(`is_grouped`,`groups`,`flat_rows`,`period_label`,`anchor`,`prev_anchor`,`next_anchor`,`has_next`). ✓
- **플레이스홀더:** 없음(모든 step에 실제 코드/명령/기대출력 포함). ✓
- **주의:** Task 3의 라우트는 Task 4 템플릿 없이는 `TemplateNotFound`가 나므로 Task 3 테스트는 Task 4 완료 후 함께 통과한다(Task 3 Step 4에서 라우트 등록만 선확인).
```
