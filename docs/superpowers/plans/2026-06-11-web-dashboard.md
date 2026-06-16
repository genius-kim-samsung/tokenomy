# 웹 대시보드 (Task 8) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 완성된 백엔드(parser/pricing/db/tiers/aggregate) 위에 FastAPI+Jinja2 로컬 웹 대시보드를 얹어, 번다운·업무별 비용·효율 코치·복기를 브라우저에서 보고 더블클릭 런처로 실행한다.

**Architecture:** 싱글 페이지 서버사이드 렌더(SSR). `aggregate.py`에 신규 집계 함수(by_session/session_detail/insights/daily_series + burndown status)를 추가하고, `web/views.py`가 이를 화면용 dict로 조립, `web/app.py`가 라우팅만 담당. 추세선 1곳만 vendored Chart.js, 나머지는 CSS.

**Tech Stack:** FastAPI, Jinja2, uvicorn, SQLite(stdlib), pytest+httpx(TestClient), Chart.js(로컬 vendored).

---

## 파일 구조

| 파일 | 책임 | 생성/수정 |
|---|---|---|
| `tokenomy/aggregate.py` | 신규 집계 함수 + burndown status | 수정 |
| `tokenomy/web/__init__.py` | 패키지 마커 | 생성 |
| `tokenomy/web/views.py` | DB→화면 dict 조립 (라우트/집계 분리) | 생성 |
| `tokenomy/web/app.py` | FastAPI 앱 + 라우트 (얇게) | 생성 |
| `tokenomy/web/templates/base.html` | 공통 셸 + CSS/JS 링크 | 생성 |
| `tokenomy/web/templates/dashboard.html` | 메인 대시보드 | 생성 |
| `tokenomy/web/templates/session.html` | drill-down 상세 + 404 | 생성 |
| `tokenomy/web/static/style.css` | CSS 막대바·게이지·테이블 | 생성 |
| `tokenomy/web/static/vendor/chart.min.js` | Chart.js 로컬 번들 (추세선 전용) | 생성(다운로드) |
| `tests/test_aggregate.py` | 신규 집계 단위 테스트 | 생성 |
| `tests/test_web.py` | 라우트 스모크 (TestClient) | 생성 |
| `start_tokenomy.bat` | 더블클릭 런처 | 생성 |

집계 로직은 전부 `aggregate.py`에 모으고(기존 `burndown`/`by_project`와 같은 위치), 웹은 표현만 담당한다. `views.py`는 집계 호출+정렬+dict 조립, `app.py`는 라우팅+입력 검증만.

---

## Task 1: burndown 에 status 필드 추가

**Files:**
- Modify: `tokenomy/aggregate.py` (`Burndown` dataclass + `burndown()` 함수)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: Write the failing test**

`tests/test_aggregate.py` 신규 생성:

```python
from datetime import datetime

from tokenomy.aggregate import KST, burndown
from tokenomy.db import connect
from tokenomy.tiers import Budget


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


NOW = datetime(2026, 6, 15, tzinfo=KST)  # 6월 15일 = 30일 중 15일 경과
B = Budget(claude=223.0, chatgpt=223.0)


def test_burndown_status_ok():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=10.0)
    bd = burndown(conn, B, NOW, "claude")
    # spent 10, daily_avg 0.67, projected ~20 << 223 → ok
    assert bd.status == "ok"


def test_burndown_status_warn():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=120.0)
    bd = burndown(conn, B, NOW, "claude")
    # spent 120 < 223 이지만 projected 120/15*30 = 240 > 223 → warn
    assert bd.status == "warn"


def test_burndown_status_exceeds():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=250.0)
    bd = burndown(conn, B, NOW, "claude")
    # spent 250 >= 223 → exceeds
    assert bd.status == "exceeds"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_aggregate.py -k status -v`
Expected: FAIL with `AttributeError: 'Burndown' object has no attribute 'status'`

- [ ] **Step 3: Write minimal implementation**

`tokenomy/aggregate.py`의 `Burndown` dataclass에 필드 추가 (`unpriced_count` 다음 줄):

```python
    unpriced_count: int
    status: str  # "ok" | "warn" | "exceeds"
```

`burndown()` 함수에서 `on_track` 계산 직후, `return Burndown(...)` 직전에 status 계산을 추가:

```python
    on_track = (projected <= limit) if limit > 0 else True

    if limit > 0 and spent >= limit:
        status = "exceeds"
    elif limit > 0 and projected > limit:
        status = "warn"
    else:
        status = "ok"
```

그리고 `return Burndown(...)` 호출의 마지막 인자에 `status=status,` 추가:

```python
        exhaust_day=exhaust_day, on_track=on_track, unpriced_count=unpriced,
        status=status,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_aggregate.py -k status -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): burndown에 status(ok/warn/exceeds) 추가"
```

---

## Task 2: by_session (복기 뷰용 세션별 집계)

**Files:**
- Modify: `tokenomy/aggregate.py` (`_month_rows` SELECT에 web_search 추가, `SessionRow` + `by_session` 신규)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: Write the failing test**

`tests/test_aggregate.py`에 추가:

```python
from tokenomy.aggregate import by_session


def test_by_session_aggregates_and_sorts():
    conn = connect(":memory:")
    # s1: 두 메시지 합 $30, s2: 한 메시지 $50 → 비용순 s2, s1
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-10T10:00:00Z",
         cost_usd=10.0, cache_read=70, input_tokens=30)
    _msg(conn, dedup_key="b", session_id="s1", ts="2026-06-11T10:00:00Z",
         cost_usd=20.0, cache_read=0, input_tokens=0)
    _msg(conn, dedup_key="c", session_id="s2", ts="2026-06-12T10:00:00Z",
         cost_usd=50.0, cache_read=0, input_tokens=100)
    # s1 에 라벨 부여
    conn.execute("INSERT INTO sessions (session_id, label) VALUES ('s1', '대시보드 작업')")
    conn.commit()

    rows = by_session(conn, "claude", NOW)
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
    rows = by_session(conn, "claude", NOW)
    assert len(rows) == 1
    assert rows[0].cost == 5.0           # 5월 메시지 제외
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_aggregate.py -k by_session -v`
Expected: FAIL with `ImportError: cannot import name 'by_session'`

- [ ] **Step 3: Write minimal implementation**

먼저 `_month_rows`의 SELECT에 `web_search`를 추가 (Task 4에서도 쓰임). 기존:

```python
    rows = conn.execute(
        "SELECT ts, cost_usd, priced, session_id, project, "
        "input_tokens, cache_creation, cache_read "
        "FROM messages WHERE provider=?",
        (provider,),
    ).fetchall()
```

다음으로 교체:

```python
    rows = conn.execute(
        "SELECT ts, cost_usd, priced, session_id, project, "
        "input_tokens, cache_creation, cache_read, web_search "
        "FROM messages WHERE provider=?",
        (provider,),
    ).fetchall()
```

그리고 `by_project` 함수 아래에 `SessionRow`/`by_session` 추가:

```python
@dataclass
class SessionRow:
    session_id: str
    project: str | None
    label: str | None
    cost: float
    first_ts: str | None
    last_ts: str | None
    msgs: int
    cache_ratio: float


def by_session(conn, provider: str, now_kst: datetime, limit_n: int | None = None) -> list[SessionRow]:
    rows = _month_rows(conn, provider, now_kst)
    labels = {
        r["session_id"]: r["label"]
        for r in conn.execute("SELECT session_id, label FROM sessions").fetchall()
    }
    agg: dict = {}
    for r in rows:
        sid = r["session_id"]
        a = agg.setdefault(
            sid,
            {"project": r["project"], "cost": 0.0, "msgs": 0,
             "first": r["ts"], "last": r["ts"], "cr": 0, "den": 0},
        )
        a["cost"] += r["cost_usd"] or 0
        a["msgs"] += 1
        if r["ts"] and (a["first"] is None or r["ts"] < a["first"]):
            a["first"] = r["ts"]
        if r["ts"] and (a["last"] is None or r["ts"] > a["last"]):
            a["last"] = r["ts"]
        a["cr"] += r["cache_read"] or 0
        a["den"] += (r["input_tokens"] or 0) + (r["cache_creation"] or 0) + (r["cache_read"] or 0)

    out = [
        SessionRow(
            session_id=sid, project=a["project"], label=labels.get(sid),
            cost=round(a["cost"], 4), first_ts=a["first"], last_ts=a["last"],
            msgs=a["msgs"], cache_ratio=round(a["cr"] / a["den"], 4) if a["den"] else 0.0,
        )
        for sid, a in agg.items()
    ]
    out.sort(key=lambda x: x.cost, reverse=True)
    return out[:limit_n] if limit_n else out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_aggregate.py -k by_session -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): by_session 복기 집계 + _month_rows에 web_search"
```

---

## Task 3: session_detail (drill-down 상세)

**Files:**
- Modify: `tokenomy/aggregate.py` (`ModelRow`/`SessionDetail` + `session_detail`)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: Write the failing test**

`tests/test_aggregate.py`에 추가:

```python
from tokenomy.aggregate import session_detail


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_aggregate.py -k session_detail -v`
Expected: FAIL with `ImportError: cannot import name 'session_detail'`

- [ ] **Step 3: Write minimal implementation**

`tokenomy/aggregate.py`의 `by_session` 아래에 추가:

```python
@dataclass
class ModelRow:
    model: str | None
    cost: float
    input_tokens: int
    output_tokens: int
    cache_creation: int
    cache_read: int


@dataclass
class SessionDetail:
    session_id: str
    project: str | None
    provider: str | None
    label: str | None
    first_ts: str | None
    last_ts: str | None
    cost: float
    msgs: int
    web_search: int
    web_fetch: int
    models: list[ModelRow]


def session_detail(conn, session_id: str) -> SessionDetail | None:
    totals = conn.execute(
        "SELECT COUNT(*) msgs, SUM(cost_usd) cost, SUM(web_search) ws, "
        "SUM(web_fetch) wf, MIN(ts) first_ts, MAX(ts) last_ts, MAX(provider) provider "
        "FROM messages WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if not totals or not totals["msgs"]:
        return None

    meta = conn.execute(
        "SELECT project, provider, label FROM sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()

    model_rows = conn.execute(
        "SELECT model, SUM(cost_usd) cost, SUM(input_tokens) it, SUM(output_tokens) ot, "
        "SUM(cache_creation) cc, SUM(cache_read) cr "
        "FROM messages WHERE session_id=? GROUP BY model ORDER BY cost DESC",
        (session_id,),
    ).fetchall()

    return SessionDetail(
        session_id=session_id,
        project=meta["project"] if meta else None,
        provider=(meta["provider"] if meta else None) or totals["provider"],
        label=meta["label"] if meta else None,
        first_ts=totals["first_ts"], last_ts=totals["last_ts"],
        cost=round(totals["cost"] or 0, 4), msgs=totals["msgs"],
        web_search=totals["ws"] or 0, web_fetch=totals["wf"] or 0,
        models=[
            ModelRow(
                model=m["model"], cost=round(m["cost"] or 0, 4),
                input_tokens=m["it"] or 0, output_tokens=m["ot"] or 0,
                cache_creation=m["cc"] or 0, cache_read=m["cr"] or 0,
            )
            for m in model_rows
        ],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_aggregate.py -k session_detail -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): session_detail drill-down 집계"
```

---

## Task 4: insights (효율 코치 휴리스틱)

**Files:**
- Modify: `tokenomy/aggregate.py` (임계 상수 + `Insight`/`insights`)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: Write the failing test**

`tests/test_aggregate.py`에 추가:

```python
from tokenomy.aggregate import insights


def test_insights_low_cache_and_websearch():
    conn = connect(":memory:")
    # cache_read 비율 = 10/(90+0+10)=0.1 < 0.30 → warn 카드
    # web_search 합 60 > 50 → info 카드
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=5.0,
         input_tokens=90, cache_read=10, web_search=60)
    bd = burndown(conn, B, NOW, "claude")
    cards = insights(conn, bd, NOW, "claude")
    levels = {c.level for c in cards}
    texts = " ".join(c.text for c in cards)
    assert "warn" in levels and "info" in levels
    assert "캐시" in texts
    assert "web_search" in texts


def test_insights_unpriced_card():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=0.0,
         input_tokens=100, cache_read=100, priced=0)
    bd = burndown(conn, B, NOW, "claude")
    cards = insights(conn, bd, NOW, "claude")
    assert any("미식별" in c.text for c in cards)


def test_insights_clean_returns_placeholder():
    conn = connect(":memory:")
    # 캐시 충분(0.9), web_search 적음, priced, projected 낮음 → 특이신호 없음
    _msg(conn, dedup_key="a", ts="2026-06-10T10:00:00Z", cost_usd=1.0,
         input_tokens=10, cache_read=90, web_search=0, priced=1)
    bd = burndown(conn, B, NOW, "claude")
    cards = insights(conn, bd, NOW, "claude")
    assert len(cards) == 1
    assert "특이 신호 없음" in cards[0].text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_aggregate.py -k insights -v`
Expected: FAIL with `ImportError: cannot import name 'insights'`

- [ ] **Step 3: Write minimal implementation**

`tokenomy/aggregate.py` 상단(`KST = ...` 아래)에 임계 상수 추가:

```python
# 효율 코치 휴리스틱 임계값 — 실데이터 캘리브레이션 전 튜닝값(단정 금지, 신호로만 사용)
INSIGHT_CACHE_READ_MIN = 0.30   # 월 cache_read 비율이 이 미만이면 경고
INSIGHT_WEB_SEARCH_MAX = 50     # 월 web_search 합이 이 초과면 정보 카드
```

`session_detail` 아래에 `Insight`/`insights` 추가 (`Burndown`을 인자로 받아 projected/unpriced 재사용):

```python
@dataclass
class Insight:
    level: str  # "info" | "warn"
    text: str


def insights(conn, bd: "Burndown", now_kst: datetime, provider: str) -> list[Insight]:
    rows = _month_rows(conn, provider, now_kst)
    cr = sum(r["cache_read"] or 0 for r in rows)
    den = sum((r["input_tokens"] or 0) + (r["cache_creation"] or 0) + (r["cache_read"] or 0) for r in rows)
    cache_ratio = (cr / den) if den else 1.0
    web_search = sum(r["web_search"] or 0 for r in rows)

    cards: list[Insight] = []
    if den and cache_ratio < INSIGHT_CACHE_READ_MIN:
        cards.append(Insight("warn", f"캐시 활용 {cache_ratio * 100:.0f}% — 컨텍스트 재구축 낭비 가능성"))
    if web_search > INSIGHT_WEB_SEARCH_MAX:
        cards.append(Insight("info", f"web_search {web_search}회 — 비용 영향 점검 권장"))
    if bd.unpriced_count:
        cards.append(Insight("warn", f"단가 미식별 {bd.unpriced_count}건 — 비용 누락 가능"))
    if bd.limit > 0 and bd.projected_month > bd.limit:
        cards.append(Insight("warn", f"현 추세 월말 ${bd.projected_month:.0f} 예상 — 한도 초과 가능"))

    if not cards:
        cards.append(Insight("info", "특이 신호 없음"))
    return cards
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_aggregate.py -k insights -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): insights 효율 코치 휴리스틱(튜닝 임계)"
```

---

## Task 5: daily_series (일별 누적 추세선 데이터)

**Files:**
- Modify: `tokenomy/aggregate.py` (`DayPoint` + `daily_series`)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: Write the failing test**

`tests/test_aggregate.py`에 추가:

```python
from tokenomy.aggregate import daily_series


def test_daily_series_cumulative():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-01T10:00:00Z", cost_usd=5.0)
    _msg(conn, dedup_key="b", ts="2026-06-02T10:00:00Z", cost_usd=3.0)
    _msg(conn, dedup_key="c", ts="2026-06-02T12:00:00Z", cost_usd=2.0)
    pts = daily_series(conn, "claude", NOW)   # NOW = 6/15
    assert len(pts) == 15                      # 1일~15일
    assert pts[0].day == 1 and pts[0].cumulative_cost == 5.0
    assert pts[1].cumulative_cost == 10.0      # 5 + (3+2) 누적
    assert pts[14].cumulative_cost == 10.0     # 이후 변동 없음, 누적 유지
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_aggregate.py -k daily_series -v`
Expected: FAIL with `ImportError: cannot import name 'daily_series'`

- [ ] **Step 3: Write minimal implementation**

`tokenomy/aggregate.py`의 `insights` 아래에 추가:

```python
@dataclass
class DayPoint:
    day: int
    cumulative_cost: float


def daily_series(conn, provider: str, now_kst: datetime) -> list[DayPoint]:
    rows = _month_rows(conn, provider, now_kst)
    per_day: dict = {}
    for r in rows:
        dt = parse_ts(r["ts"])
        if dt:
            per_day[dt.day] = per_day.get(dt.day, 0.0) + (r["cost_usd"] or 0)
    out: list[DayPoint] = []
    cumulative = 0.0
    for d in range(1, now_kst.day + 1):
        cumulative += per_day.get(d, 0.0)
        out.append(DayPoint(day=d, cumulative_cost=round(cumulative, 4)))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_aggregate.py -k daily_series -v`
Expected: PASS (1 passed). 전체 확인: `pytest tests/test_aggregate.py -v` → 모두 PASS.

- [ ] **Step 5: Commit**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): daily_series 일별 누적 추세 데이터"
```

---

## Task 6: views.py — DB→화면 dict 조립

**Files:**
- Create: `tokenomy/web/__init__.py`
- Create: `tokenomy/web/views.py`
- Test: `tests/test_aggregate.py` (views는 순수 함수라 단위 테스트 가능)

- [ ] **Step 1: Write the failing test**

`tests/test_aggregate.py`에 추가:

```python
from tokenomy.web.views import dashboard_context, session_context


def test_dashboard_context_shape():
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-10T10:00:00Z", cost_usd=10.0)
    ctx = dashboard_context(conn, provider="claude", sort="cost", now_kst=NOW)
    assert ctx["provider"] == "claude"
    assert ctx["user_id"] == "me"
    assert ctx["tier"] == "Budget"
    assert ctx["burndown"].limit == 223.0
    assert ctx["projects"]                      # 업무별 리스트
    assert "sessions" in ctx and "insights" in ctx and "daily" in ctx
    assert ctx["has_data"] is True


def test_dashboard_context_empty_db():
    conn = connect(":memory:")
    ctx = dashboard_context(conn, provider="claude", sort="cost", now_kst=NOW)
    assert ctx["has_data"] is False             # 빈 DB → 빈 상태 플래그
    assert ctx["projects"] == []


def test_session_context_missing():
    conn = connect(":memory:")
    assert session_context(conn, "nope") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_aggregate.py -k context -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tokenomy.web'`

- [ ] **Step 3: Write minimal implementation**

`tokenomy/web/__init__.py` 생성 (빈 파일):

```python
```

`tokenomy/web/views.py` 생성:

```python
"""DB → 화면용 dict 조립. 라우트(app.py)와 집계(aggregate.py)를 분리한다."""
from __future__ import annotations

from datetime import datetime

from tokenomy.aggregate import (
    KST, burndown, by_project, by_session, daily_series, insights, session_detail,
)
from tokenomy.tiers import budget_for, load_tiers

_SORT_KEYS = {
    "cost": lambda x: x.cost,
    "sessions": lambda x: x.sessions,
    "cache": lambda x: x.cache_ratio,
}


def dashboard_context(conn, provider: str, sort: str, now_kst: datetime | None = None) -> dict:
    now = now_kst or datetime.now(KST)
    tiers = load_tiers()
    du = tiers["default_user"]
    budget = budget_for(du["tier"], tiers, du.get("provider_choice"))

    bd = burndown(conn, budget, now, provider)
    projects = by_project(conn, provider, now)
    projects.sort(key=_SORT_KEYS.get(sort, _SORT_KEYS["cost"]), reverse=True)
    sessions = by_session(conn, provider, now, limit_n=10)
    cards = insights(conn, bd, now, provider)
    daily = daily_series(conn, provider, now)

    # 예산 페이스 라인(한도 ÷ 월일수 × day) — Chart.js 비교선
    pace = [round(bd.limit / bd.days_in_month * p.day, 4) if bd.limit else 0.0 for p in daily]

    last = conn.execute(
        "SELECT MAX(ts) t FROM messages WHERE provider=?", (provider,)
    ).fetchone()
    has_data = last is not None and last["t"] is not None

    return {
        "provider": provider, "sort": sort,
        "user_id": du["user_id"], "tier": du["tier"],
        "month": now.strftime("%Y-%m"),
        "burndown": bd, "projects": projects, "sessions": sessions,
        "insights": cards,
        "daily_labels": [p.day for p in daily],
        "daily_actual": [p.cumulative_cost for p in daily],
        "daily_pace": pace,
        "daily": daily,
        "last_ts": last["t"] if has_data else None,
        "has_data": has_data,
    }


def session_context(conn, session_id: str) -> dict | None:
    detail = session_detail(conn, session_id)
    if detail is None:
        return None
    return {"detail": detail}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_aggregate.py -k context -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add tokenomy/web/__init__.py tokenomy/web/views.py tests/test_aggregate.py
git commit -m "feat(web): views.py 화면 컨텍스트 조립"
```

---

## Task 7: app.py — FastAPI 라우트 + 스모크 테스트

**Files:**
- Create: `tokenomy/web/app.py`
- Test: `tests/test_web.py`

> 템플릿(Task 8)이 아직 없으므로, 이 태스크에서 **최소 플레이스홀더 템플릿**을 먼저 만들어 라우트가 200을 반환하게 한다. Task 8에서 본 템플릿으로 교체한다.

- [ ] **Step 1: Write the failing test**

`tests/test_web.py` 신규 생성:

```python
from fastapi.testclient import TestClient

from tokenomy.db import connect
from tokenomy.web import app as app_module


def _client(tmp_path, monkeypatch):
    """app.connect를 임시 DB로 교체한 TestClient."""
    db = tmp_path / "t.db"

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web.py -v`
Expected: FAIL with `ModuleNotFoundError` / `AttributeError: module ... has no attribute 'app'`

- [ ] **Step 3: Write minimal implementation**

먼저 플레이스홀더 템플릿 3개를 만든다 (Task 8에서 교체):

`tokenomy/web/templates/base.html`:

```html
<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>Tokenomy</title></head>
<body>{% block body %}{% endblock %}</body></html>
```

`tokenomy/web/templates/dashboard.html`:

```html
{% extends "base.html" %}{% block body %}
<h1>Tokenomy — {{ month }} · {{ user_id }} · {{ tier }}</h1>
<form method="post" action="/ingest"><button>↻ 새로고침</button></form>
<h2>번다운</h2>
{% if not has_data %}<p>데이터 없음 · [↻ 새로고침]을 누르세요</p>{% endif %}
{% endblock %}
```

`tokenomy/web/templates/session.html`:

```html
{% extends "base.html" %}{% block body %}
{% if not detail %}<p>세션을 찾을 수 없음 · <a href="/">← 대시보드</a></p>
{% else %}<h1>세션 상세 — {{ detail.session_id }}</h1>{% endif %}
{% endblock %}
```

`tokenomy/web/app.py` 생성:

```python
"""FastAPI 라우트 (얇게 — 라우팅+입력검증만). 데이터 조립은 views.py."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tokenomy.cli import cmd_ingest
from tokenomy.db import connect
from tokenomy.web.views import dashboard_context, session_context

_BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_BASE / "templates"))

app = FastAPI(title="Tokenomy")
app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")

_PROVIDERS = ("claude", "chatgpt")
_SORTS = ("cost", "sessions", "cache")


@app.get("/")
def dashboard(request: Request, provider: str = "claude", sort: str = "cost",
              notice: str | None = None):
    provider = provider if provider in _PROVIDERS else "claude"
    sort = sort if sort in _SORTS else "cost"
    conn = connect()
    ctx = dashboard_context(conn, provider, sort)
    return templates.TemplateResponse("dashboard.html", {"request": request, "notice": notice, **ctx})


@app.get("/session/{session_id}")
def session_view(request: Request, session_id: str):
    conn = connect()
    ctx = session_context(conn, session_id)
    if ctx is None:
        return templates.TemplateResponse(
            "session.html", {"request": request, "detail": None}, status_code=404
        )
    return templates.TemplateResponse("session.html", {"request": request, **ctx})


@app.post("/ingest")
def do_ingest():
    conn = connect()
    try:
        cmd_ingest(conn)
    except Exception:
        return RedirectResponse("/?notice=ingest-failed", status_code=303)
    return RedirectResponse("/", status_code=303)
```

`static/` 디렉터리가 없으면 `StaticFiles`가 에러를 내므로 빈 디렉터리를 보장한다 (Task 8/9에서 파일 채움):

```bash
mkdir -p tokenomy/web/static/vendor
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_web.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add tokenomy/web/app.py tokenomy/web/templates tests/test_web.py
git commit -m "feat(web): app.py 라우트(/, /session, /ingest) + 스모크 테스트"
```

---

## Task 8: 본 템플릿 + CSS

**Files:**
- Modify: `tokenomy/web/templates/base.html`, `dashboard.html`, `session.html`
- Create: `tokenomy/web/static/style.css`
- Test: `tests/test_web.py` (렌더 내용 검증 추가)

- [ ] **Step 1: Write the failing test**

`tests/test_web.py`에 추가:

```python
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
    for section in ("번다운", "효율 코치", "업무별", "복기"):
        assert section in r.text
    assert "공개 API 단가 기준 추정" in r.text   # §5.2 비용 신뢰도 표기
    assert "proj" in r.text                       # 업무별 행
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web.py -k renders_sections -v`
Expected: FAIL (플레이스홀더 dashboard.html에 "효율 코치"/"업무별"/"복기" 없음)

- [ ] **Step 3: Write minimal implementation**

`tokenomy/web/templates/base.html` 교체:

```html
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tokenomy</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <main class="wrap">{% block body %}{% endblock %}</main>
  {% block scripts %}{% endblock %}
</body>
</html>
```

`tokenomy/web/templates/dashboard.html` 교체:

```html
{% extends "base.html" %}
{% block body %}
<header class="topbar">
  <div>🪙 Tokenomy · {{ month }} (KST) · {{ user_id }} · {{ tier }}</div>
  <div class="topbar-right">
    {% if last_ts %}<span class="muted">데이터 최신: {{ last_ts }}</span>{% endif %}
    <form method="post" action="/ingest" class="inline"><button class="btn">↻ 새로고침</button></form>
  </div>
</header>

{% if notice == "ingest-failed" %}
<div class="banner error">새로고침(ingest) 중 오류 — 기존 데이터를 표시합니다.</div>
{% endif %}

<section class="card">
  <div class="card-head">
    <h2>번다운</h2>
    <nav class="toggle">
      <a href="/?provider=claude" class="{{ 'on' if provider == 'claude' }}">Claude</a>
      <a href="/?provider=chatgpt" class="{{ 'on' if provider == 'chatgpt' }}">ChatGPT</a>
    </nav>
  </div>
  {% set bd = burndown %}
  {% if not has_data and provider == "chatgpt" %}
    <p class="muted">(이 머신에 Codex 로그 없음)</p>
  {% elif not has_data %}
    <p class="muted">데이터 없음 · [↻ 새로고침]을 누르세요</p>
  {% elif bd.limit == 0 %}
    <p class="muted">한도 미설정 (티어 base 또는 provider 미선택) · 지출 ${{ '%.2f'|format(bd.spent) }}</p>
  {% else %}
    <div class="bd-row">
      <span class="bd-label">{{ provider|capitalize }}</span>
      <span class="bd-num">${{ '%.2f'|format(bd.spent) }} / ${{ '%.0f'|format(bd.limit) }}</span>
      <span class="bar"><span class="fill s-{{ bd.status }}" style="width: {{ [bd.pct * 100, 100]|min }}%"></span></span>
      <span class="bd-pct">{{ '%.1f'|format(bd.pct * 100) }}%</span>
      <span class="status s-{{ bd.status }}">{{ {'ok':'✅ OK','warn':'⚠ 초과예상','exceeds':'⛔ 초과'}[bd.status] }}</span>
    </div>
    <p class="muted">
      {{ bd.day_of_month }}/{{ bd.days_in_month }}일 경과 · 일평균 ${{ '%.2f'|format(bd.daily_avg) }} ·
      예상 월말 ${{ '%.0f'|format(bd.projected_month) }}
      {% if bd.exhaust_day %}· → 이대로면 {{ bd.exhaust_day }}일에 소진 (남은 {{ bd.days_left }}일){% endif %}
    </p>
    <p class="disclaimer">ⓘ 공개 API 단가 기준 추정 · 이 머신 데이터만{% if bd.unpriced_count %} · <span class="badge">단가 미식별 {{ bd.unpriced_count }}건</span>{% endif %}</p>
  {% endif %}
</section>

{% if has_data %}
<section class="card">
  <h2>일별 추세</h2>
  <canvas id="trend" height="120"></canvas>
</section>
{% endif %}

<section class="card">
  <h2>⚠ 효율 코치</h2>
  <ul class="coach">
    {% for c in insights %}<li class="lvl-{{ c.level }}">{{ c.text }}</li>{% endfor %}
  </ul>
</section>

<section class="card">
  <div class="card-head">
    <h2>업무별 비용</h2>
    <nav class="toggle small">
      <a href="/?provider={{ provider }}&sort=cost" class="{{ 'on' if sort == 'cost' }}">비용</a>
      <a href="/?provider={{ provider }}&sort=sessions" class="{{ 'on' if sort == 'sessions' }}">세션</a>
      <a href="/?provider={{ provider }}&sort=cache" class="{{ 'on' if sort == 'cache' }}">캐시</a>
    </nav>
  </div>
  <table class="grid">
    <thead><tr><th>비용</th><th>캐시</th><th>세션</th><th>프로젝트</th></tr></thead>
    <tbody>
      {% for p in projects %}
      <tr><td>${{ '%.2f'|format(p.cost) }}</td><td>{{ '%.0f'|format(p.cache_ratio * 100) }}%</td>
          <td>{{ p.sessions }}</td><td>{{ p.project or '(unknown)' }}</td></tr>
      {% else %}<tr><td colspan="4" class="muted">데이터 없음</td></tr>{% endfor %}
    </tbody>
  </table>
</section>

<section class="card">
  <h2>복기 — 최근 비싼 세션</h2>
  <table class="grid">
    <thead><tr><th>기간</th><th>비용</th><th>프로젝트</th><th>라벨</th><th></th></tr></thead>
    <tbody>
      {% for s in sessions %}
      <tr><td>{{ s.first_ts }}</td><td>${{ '%.2f'|format(s.cost) }}</td>
          <td>{{ s.project or '(unknown)' }}</td><td>{{ s.label or '[라벨 없음]' }}</td>
          <td><a href="/session/{{ s.session_id }}">▸</a></td></tr>
      {% else %}<tr><td colspan="5" class="muted">데이터 없음</td></tr>{% endfor %}
    </tbody>
  </table>
</section>
{% endblock %}
```

`tokenomy/web/templates/session.html` 교체:

```html
{% extends "base.html" %}
{% block body %}
<p><a href="/">← 대시보드</a></p>
{% if not detail %}
<section class="card"><h1>세션을 찾을 수 없음</h1></section>
{% else %}
{% set d = detail %}
<header class="topbar"><div>세션 상세 · {{ d.project or '(unknown)' }} · {{ d.provider }}</div></header>
<section class="card">
  <p>{{ d.first_ts }} ~ {{ d.last_ts }} · ${{ '%.2f'|format(d.cost) }} · {{ d.msgs }} msg
     {% if d.label %}· {{ d.label }}{% endif %}</p>
  <h2>모델별</h2>
  <table class="grid">
    <thead><tr><th>모델</th><th>비용</th><th>input</th><th>output</th><th>cache_cr</th><th>cache_rd</th></tr></thead>
    <tbody>
      {% for m in d.models %}
      <tr><td>{{ m.model or '(unknown)' }}</td><td>${{ '%.2f'|format(m.cost) }}</td>
          <td>{{ '{:,}'.format(m.input_tokens) }}</td><td>{{ '{:,}'.format(m.output_tokens) }}</td>
          <td>{{ '{:,}'.format(m.cache_creation) }}</td><td>{{ '{:,}'.format(m.cache_read) }}</td></tr>
      {% endfor %}
    </tbody>
  </table>
  <p class="muted">web_search {{ d.web_search }} · web_fetch {{ d.web_fetch }}</p>
</section>
{% endif %}
{% endblock %}
```

`tokenomy/web/static/style.css` 생성:

```css
:root { --bg:#0f1115; --card:#1a1d24; --fg:#e6e8eb; --muted:#9aa0a6;
        --ok:#3fb950; --warn:#d29922; --exceeds:#f85149; --line:#2d333b; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:14px/1.5 -apple-system,"Segoe UI",Roboto,"Malgun Gothic",sans-serif; }
.wrap { max-width:880px; margin:0 auto; padding:16px; }
.topbar { display:flex; justify-content:space-between; align-items:center; gap:12px; padding:8px 0; }
.topbar-right { display:flex; gap:12px; align-items:center; }
.inline { display:inline; } .muted { color:var(--muted); font-size:13px; }
.btn { background:#238636; color:#fff; border:0; border-radius:6px; padding:6px 12px; cursor:pointer; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin:12px 0; }
.card-head { display:flex; justify-content:space-between; align-items:center; }
h1 { font-size:18px; } h2 { font-size:15px; margin:0 0 8px; }
.toggle a { color:var(--muted); text-decoration:none; padding:2px 8px; border-radius:6px; }
.toggle a.on { color:#fff; background:#30363d; } .toggle.small a { font-size:13px; }
.bd-row { display:flex; align-items:center; gap:10px; margin:6px 0; }
.bd-label { width:64px; } .bd-num { width:150px; } .bd-pct { width:56px; text-align:right; }
.bar { flex:1; height:12px; background:#0d1117; border-radius:6px; overflow:hidden; }
.fill { display:block; height:100%; }
.fill.s-ok { background:var(--ok); } .fill.s-warn { background:var(--warn); } .fill.s-exceeds { background:var(--exceeds); }
.status.s-ok { color:var(--ok); } .status.s-warn { color:var(--warn); } .status.s-exceeds { color:var(--exceeds); }
.disclaimer { font-size:12px; color:var(--muted); margin-top:8px; }
.badge { background:#3d2c00; color:var(--warn); padding:1px 6px; border-radius:4px; }
.banner.error { background:#3d1418; color:var(--exceeds); padding:8px 12px; border-radius:8px; }
.coach { list-style:none; padding:0; margin:0; }
.coach li { padding:4px 0; } .coach .lvl-warn::before { content:"⚠ "; color:var(--warn); }
.coach .lvl-info::before { content:"• "; color:var(--muted); }
table.grid { width:100%; border-collapse:collapse; }
.grid th, .grid td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }
.grid th { color:var(--muted); font-weight:600; font-size:12px; }
a { color:#58a6ff; }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_web.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add tokenomy/web/templates tokenomy/web/static/style.css tests/test_web.py
git commit -m "feat(web): 대시보드/세션 템플릿 + CSS"
```

---

## Task 9: Chart.js vendoring + 추세선

**Files:**
- Create: `tokenomy/web/static/vendor/chart.min.js` (다운로드)
- Modify: `tokenomy/web/templates/dashboard.html` (scripts 블록)

> 추세선은 JS라 자동 테스트가 어렵다. 데이터 임베드(JSON)가 템플릿에 들어갔는지 텍스트로 검증하고, 실제 그래프는 Task 11 수동 검증에서 확인한다.

- [ ] **Step 1: Write the failing test**

`tests/test_web.py`에 추가:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web.py -k trend -v`
Expected: FAIL ("trendActual" 없음)

- [ ] **Step 3: Write minimal implementation**

Chart.js UMD 번들을 vendored로 다운로드 (집 데스크탑은 인터넷 가능):

```bash
curl -L https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js -o tokenomy/web/static/vendor/chart.min.js
```

다운로드 검증 (파일이 비어있지 않고 Chart 정의 포함):

```bash
grep -q "Chart" tokenomy/web/static/vendor/chart.min.js && echo OK
```

`dashboard.html` 맨 아래(`{% endblock %}` 직전이 아니라 별도 scripts 블록)에 추가. `body` 블록의 마지막 `{% endblock %}` 뒤에:

```html
{% block scripts %}
{% if has_data %}
<script src="/static/vendor/chart.min.js"></script>
<script>
  const trendLabels = {{ daily_labels|tojson }};
  const trendActual = {{ daily_actual|tojson }};
  const trendPace   = {{ daily_pace|tojson }};
  new Chart(document.getElementById('trend'), {
    type: 'line',
    data: { labels: trendLabels, datasets: [
      { label: '누적 실제', data: trendActual, borderColor: '#58a6ff', tension: .2 },
      { label: '예산 페이스', data: trendPace, borderColor: '#9aa0a6', borderDash: [5,4], pointRadius: 0 },
    ]},
    options: { plugins:{ legend:{ labels:{ color:'#e6e8eb' } } },
      scales:{ x:{ ticks:{ color:'#9aa0a6' } }, y:{ ticks:{ color:'#9aa0a6' } } } }
  });
</script>
{% endif %}
{% endblock %}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_web.py -k trend -v`
Expected: PASS. 전체: `pytest -v` → 모두 PASS.

- [ ] **Step 5: Commit**

```bash
git add tokenomy/web/static/vendor/chart.min.js tokenomy/web/templates/dashboard.html tests/test_web.py
git commit -m "feat(web): vendored Chart.js 일별 추세선(누적 실제 vs 예산 페이스)"
```

---

## Task 10: 더블클릭 런처 (start_tokenomy.bat)

**Files:**
- Create: `start_tokenomy.bat`
- Modify: `README.md` (실행 섹션 갱신)

> .bat은 자동 테스트 대상이 아니다. Task 11 수동 검증에서 더블클릭 동작을 확인한다.

- [ ] **Step 1: 런처 작성**

`start_tokenomy.bat` 생성 (저장소 루트):

```bat
@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

echo [tokenomy] 세션 로그 수집 중...
"%PY%" -m tokenomy.cli ingest

echo [tokenomy] 대시보드 기동: http://127.0.0.1:8765
start "" http://127.0.0.1:8765
"%PY%" -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765
```

> 설계 §8의 "포트 점유 시 다음 포트 탐색(8765→8766…)"은 PoC 단순화를 위해 고정 포트로 시작한다. 점유 시 uvicorn이 에러를 출력하므로 사용자가 인지할 수 있고, 자동 포트 탐색은 후속(필요 시). 바인딩은 `127.0.0.1`(로컬 전용)로 외부 노출을 막는다.

- [ ] **Step 2: README 실행 섹션 갱신**

`README.md`의 "## 실행 (예정)" 섹션을 찾아 다음으로 교체:

```markdown
## 실행

더블클릭: `start_tokenomy.bat` (ingest → 대시보드 기동 → 브라우저 자동 오픈)

수동:
```bash
pip install -r requirements.txt
python -m tokenomy.cli ingest       # 세션 로그 파싱 → DB
python -m tokenomy.cli report       # 터미널 요약
python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765  # 웹 대시보드
```
```

- [ ] **Step 3: Commit**

```bash
git add start_tokenomy.bat README.md
git commit -m "feat: 더블클릭 런처 start_tokenomy.bat + README 실행 섹션"
```

---

## Task 11: 수동 검증 (실제 데이터로 동작 확인)

**Files:** 없음 (실행 검증만)

- [ ] **Step 1: 전체 테스트 통과 확인**

Run: `pytest -v`
Expected: 모든 테스트 PASS (test_aggregate, test_web, test_db, test_parser, test_pricing).

- [ ] **Step 2: 실제 ingest + 대시보드 기동**

```bash
python -m tokenomy.cli ingest
python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765
```

브라우저로 `http://127.0.0.1:8765` 열어 확인:
- 번다운 막대/색상(status), 일별 추세선(2개 라인), 효율 코치 카드, 업무별 테이블(정렬 링크 동작), 복기 테이블
- 업무 행의 `▸` 클릭 → `/session/{id}` 상세(모델별 표) 렌더, "← 대시보드" 복귀
- `?provider=chatgpt` → "(이 머신에 Codex 로그 없음)" 또는 ChatGPT 데이터
- [↻ 새로고침] 클릭 → ingest 후 `/`로 복귀

- [ ] **Step 3: 더블클릭 런처 검증**

탐색기에서 `start_tokenomy.bat` 더블클릭 → ingest 로그 → 브라우저 자동 오픈 확인.

- [ ] **Step 4: 최종 커밋 (필요 시)**

검증 중 수정이 있었다면 커밋. 없으면 skip.

---

## 완료 기준

- [ ] `pytest -v` 전부 통과 (신규 test_aggregate 12+개, test_web 6개 포함)
- [ ] 설계 §4 레이아웃의 4개 섹션(번다운/효율코치/업무별/복기) + 추세선 렌더
- [ ] drill-down(`/session/{id}`) 동작 + 404 처리
- [ ] §5.2 비용 신뢰도 표기, §6 엣지(빈 DB/limit=0/없는 세션/ingest 실패/잘못된 쿼리) 처리
- [ ] 더블클릭 런처 동작
