# 전체 목록 페이지 + 기간 선택기 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 잘려 있던 Top 10 표 3곳을 별도 전용 페이지(`/projects`, `/sessions`)로 전체 노출하고, 일/주/월 기간 토글 + 과거 탐색을 붙인다.

**Architecture:** 집계 계층의 "현재 월 하드코딩"을 임의 기간 `[start, nxt)`로 일반화하되(`_range_rows` 추출, `by_project`/`by_session`에 키워드 전용 `start`/`nxt` 추가), 기존 번다운/추세/코치는 월 단위 그대로 유지(하위호환). 라우트는 얇게(입력 화이트리스트), 데이터 조립은 `views.py`의 새 컨텍스트 함수가 담당한다. 서버 렌더(Jinja2), 새 JS 없음.

**Tech Stack:** Python 3, FastAPI, Jinja2, SQLite, pytest. 모든 시각/월 경계는 KST.

**Spec:** `docs/superpowers/specs/2026-06-13-full-list-pages-design.md`

---

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `tokenomy/aggregate.py` | 기간 경계·집계 | `period_bounds` 신규, `_range_rows` 추출, `by_project`/`by_session`에 `start`/`nxt` |
| `tokenomy/web/views.py` | DB→화면 dict 조립 | `projects_context`/`sessions_context` 신규, `dashboard_context` 프로젝트 Top 10 제한 |
| `tokenomy/web/app.py` | 라우팅+입력검증 | `/projects`·`/sessions` 라우트, `_parse_anchor` |
| `tokenomy/web/templates/projects.html` | 전체 프로젝트 페이지 | 신규 |
| `tokenomy/web/templates/sessions.html` | 전체 세션 페이지 | 신규 |
| `tokenomy/web/templates/overview.html` | overview | `전체 보기 →` 링크 |
| `tokenomy/web/templates/dashboard.html` | AI별 상세 | `(Top 10)` 표기 + `전체 보기 →` 링크 |
| `tokenomy/web/static/style.css` | 스타일 | `.period-nav` 추가 |
| `tests/test_aggregate.py` | 집계/컨텍스트 테스트 | period_bounds·range·컨텍스트 |
| `tests/test_web.py` | 라우트/렌더 테스트 | 새 페이지·링크 |

테스트 실행 기본 명령: `python -m pytest <file> -v` (프로젝트 루트에서).

---

## Task 1: `period_bounds` — 기간 경계 + 라벨

**Files:**
- Modify: `tokenomy/aggregate.py` (기존 `month_bounds` 아래에 추가)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_aggregate.py` 상단 import에 `period_bounds`를 추가한다(기존 import 줄 수정):

```python
from tokenomy.aggregate import (
    KST, burndown, by_project, by_session, combined_burndown, daily_series, insights,
    month_bounds, parse_ts, period_bounds, session_detail,
)
```

파일 끝에 추가:

```python
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest tests/test_aggregate.py -k period_bounds -v`
Expected: FAIL — `ImportError: cannot import name 'period_bounds'`

- [ ] **Step 3: 최소 구현**

`tokenomy/aggregate.py`의 `month_bounds` 함수 정의 **바로 아래**에 추가:

```python
def period_bounds(period: str, anchor_kst: datetime) -> tuple[datetime, datetime, str]:
    """기간 [start, nxt) 경계와 표시 라벨. period ∈ {day, week, month}.

    anchor가 속한 일/주/월을 KST 기준으로 반환. 주는 월요일 시작.
    화이트리스트 밖 period는 월간으로 폴백(라우트에서도 검증하지만 이중 안전).
    """
    a = anchor_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        nxt = a + timedelta(days=1)
        return a, nxt, f"{a.strftime('%Y-%m-%d')} ({'월화수목금토일'[a.weekday()]})"
    if period == "week":
        start = a - timedelta(days=a.weekday())   # 월요일(weekday: 월=0)
        nxt = start + timedelta(days=7)
        end = nxt - timedelta(days=1)
        return start, nxt, f"{start.strftime('%Y-%m-%d')} ~ {end.strftime('%m-%d')}"
    start, nxt = month_bounds(a)                   # month (기본/폴백)
    return start, nxt, start.strftime("%Y-%m")
```

`timedelta`는 이미 `from datetime import datetime, timedelta, timezone`으로 import되어 있다(추가 import 불필요).

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m pytest tests/test_aggregate.py -k period_bounds -v`
Expected: 5개 PASS

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): period_bounds — 일/주/월 기간 경계+라벨(주=월요일)"
```

---

## Task 2: `_range_rows` 추출 + `by_project`/`by_session` 기간 파라미터

**Files:**
- Modify: `tokenomy/aggregate.py` (`_month_rows`, `by_project`, `by_session`)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_aggregate.py` 끝에 추가:

```python
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest tests/test_aggregate.py -k "range_restricts" -v`
Expected: FAIL — `by_project() got an unexpected keyword argument 'start'`

- [ ] **Step 3: 최소 구현**

`tokenomy/aggregate.py`에서 기존 `_month_rows`(69~82행 부근)를 아래 두 함수로 교체:

```python
def _range_rows(conn, provider: str | None, start: datetime, nxt: datetime) -> list:
    cols = ("SELECT ts, cost_usd, priced, session_id, project, "
            "input_tokens, cache_creation, cache_read, web_search FROM messages")
    if provider is None:
        rows = conn.execute(cols).fetchall()          # 전 AI 합산
    else:
        rows = conn.execute(cols + " WHERE provider=?", (provider,)).fetchall()
    out = []
    for r in rows:
        dt = parse_ts(r["ts"])
        if dt and start <= dt < nxt:
            out.append(r)
    return out


def _month_rows(conn, provider: str | None, now_kst: datetime) -> list:
    start, nxt = month_bounds(now_kst)
    return _range_rows(conn, provider, start, nxt)
```

`by_project` 시그니처와 첫 줄을 수정한다. 기존:

```python
def by_project(conn, provider: str | None, now_kst: datetime, limit_n: int | None = None) -> list[ProjectRow]:
    rows = _month_rows(conn, provider, now_kst)
```

수정 후:

```python
def by_project(conn, provider: str | None, now_kst: datetime, limit_n: int | None = None,
               *, start: datetime | None = None, nxt: datetime | None = None) -> list[ProjectRow]:
    rows = _range_rows(conn, provider, start, nxt) if (start and nxt) else _month_rows(conn, provider, now_kst)
```

`by_session` 시그니처와 첫 줄을 수정한다. 기존:

```python
def by_session(
    conn,
    provider: str | None,
    now_kst: datetime,
    limit_n: int | None = None,
    project: str | None = None,
    order: str = "cost",
) -> list[SessionRow]:
    """...docstring 유지..."""
    rows = _month_rows(conn, provider, now_kst)
```

수정 후(`*, start, nxt`를 인자 목록 끝에 추가, 첫 줄 교체):

```python
def by_session(
    conn,
    provider: str | None,
    now_kst: datetime,
    limit_n: int | None = None,
    project: str | None = None,
    order: str = "cost",
    *,
    start: datetime | None = None,
    nxt: datetime | None = None,
) -> list[SessionRow]:
    """...docstring 유지..."""
    rows = _range_rows(conn, provider, start, nxt) if (start and nxt) else _month_rows(conn, provider, now_kst)
```

(docstring 본문은 그대로 둔다.)

- [ ] **Step 4: 테스트 통과 + 회귀 확인**

Run: `python -m pytest tests/test_aggregate.py -v`
Expected: 신규 2개 포함 전부 PASS (기존 월 단위 테스트 회귀 없음 — `_month_rows`는 `_range_rows` 위임으로 동치)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "refactor(aggregate): _range_rows 추출 + by_project/by_session 기간 인자(하위호환)"
```

---

## Task 3: `projects_context` / `sessions_context` (views.py)

**Files:**
- Modify: `tokenomy/web/views.py` (import + 함수 2개 추가)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_aggregate.py` 상단 import(9행)를 수정:

```python
from tokenomy.web.views import (
    dashboard_context, overview_context, projects_context, sessions_context, session_context,
)
```

파일 끝에 추가:

```python
# ─── projects_context / sessions_context ─────────────────────────────────────

_NOW_613 = datetime(2026, 6, 13, 12, 0, tzinfo=KST)
_ANCHOR_613 = datetime(2026, 6, 13, tzinfo=KST)


def test_projects_context_current_day(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", ts="2026-06-13T01:00:00Z", cost_usd=10.0, project="/p")
    ctx = projects_context(conn, "day", _ANCHOR_613, "", "cost", now_kst=_NOW_613)
    assert ctx["period"] == "day"
    assert ctx["period_label"] == "2026-06-13 (토)"
    assert ctx["anchor"] == "2026-06-13"
    assert ctx["count"] == 1
    assert ctx["total"] == 10.0
    assert ctx["rows"][0].project == "/p"
    assert ctx["active_tab"] == "overview"
    assert ctx["has_next"] is False          # 오늘이 속한 기간 → 다음 없음


def test_projects_context_past_day_has_next(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    ctx = projects_context(conn, "day", _ANCHOR_613, "", "cost",
                           now_kst=datetime(2026, 6, 20, tzinfo=KST))
    assert ctx["has_next"] is True
    assert ctx["prev_anchor"] == "2026-06-12"
    assert ctx["next_anchor"] == "2026-06-14"


def test_sessions_context_order_and_filter(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-13T01:00:00Z", cost_usd=2.0, project="/a")
    _msg(conn, dedup_key="b", session_id="s2", ts="2026-06-13T02:00:00Z", cost_usd=9.0, project="/b")
    ctx = sessions_context(conn, "day", _ANCHOR_613, "", "cost", "", now_kst=_NOW_613)
    assert [r.session_id for r in ctx["rows"]] == ["s2", "s1"]   # 비용순
    assert ctx["total"] == 11.0
    ctx2 = sessions_context(conn, "day", _ANCHOR_613, "", "cost", "/a", now_kst=_NOW_613)
    assert [r.session_id for r in ctx2["rows"]] == ["s1"]        # 프로젝트 필터
    assert ctx2["project"] == "/a"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest tests/test_aggregate.py -k "projects_context or sessions_context" -v`
Expected: FAIL — `ImportError: cannot import name 'projects_context'`

- [ ] **Step 3: 최소 구현**

`tokenomy/web/views.py` import 2줄을 수정한다. 기존:

```python
from datetime import datetime

from tokenomy.aggregate import (
    KST, PROVIDERS, burndown, by_project, by_session, combined_burndown,
    daily_series, insights, session_detail,
)
```

수정 후(`timedelta`, `period_bounds` 추가):

```python
from datetime import datetime, timedelta

from tokenomy.aggregate import (
    KST, PROVIDERS, burndown, by_project, by_session, combined_burndown,
    daily_series, insights, period_bounds, session_detail,
)
```

`session_context` 함수 **아래**(파일 끝)에 추가:

```python
def projects_context(conn, period: str, anchor_kst: datetime, provider: str,
                     sort: str, now_kst: datetime | None = None) -> dict:
    """전체 프로젝트 목록(/projects). 기간 [start,nxt)로 집계 후 sort 키로 재정렬."""
    now = now_kst or datetime.now(KST)
    config = load_config()
    start, nxt, label = period_bounds(period, anchor_kst)
    rows = by_project(conn, provider or None, now, start=start, nxt=nxt)
    rows.sort(key=_SORT_KEYS.get(sort, _SORT_KEYS["cost"]), reverse=True)
    return {
        "active_tab": provider or "overview",
        "user_label": user_label(config),
        "period": period, "period_label": label,
        "anchor": anchor_kst.strftime("%Y-%m-%d"),
        "provider": provider, "sort": sort,
        "rows": rows, "count": len(rows),
        "total": round(sum(r.cost for r in rows), 4),
        "prev_anchor": (start - timedelta(days=1)).strftime("%Y-%m-%d"),
        "next_anchor": nxt.strftime("%Y-%m-%d"),
        "has_next": nxt <= now,                 # 현재/미래 기간이면 다음 숨김
        "month": now.strftime("%Y-%m"),         # _tabs.html 헤더용
    }


def sessions_context(conn, period: str, anchor_kst: datetime, provider: str,
                     order: str, project: str, now_kst: datetime | None = None) -> dict:
    """전체 세션 목록(/sessions). order=cost|recent, project 드릴다운 필터."""
    now = now_kst or datetime.now(KST)
    config = load_config()
    start, nxt, label = period_bounds(period, anchor_kst)
    rows = by_session(conn, provider or None, now, start=start, nxt=nxt,
                      order=order, project=project or None)
    return {
        "active_tab": provider or "overview",
        "user_label": user_label(config),
        "period": period, "period_label": label,
        "anchor": anchor_kst.strftime("%Y-%m-%d"),
        "provider": provider, "order": order, "project": project,
        "rows": rows, "count": len(rows),
        "total": round(sum(r.cost for r in rows), 4),
        "prev_anchor": (start - timedelta(days=1)).strftime("%Y-%m-%d"),
        "next_anchor": nxt.strftime("%Y-%m-%d"),
        "has_next": nxt <= now,
        "month": now.strftime("%Y-%m"),
    }
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m pytest tests/test_aggregate.py -k "projects_context or sessions_context" -v`
Expected: 3개 PASS

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/views.py tests/test_aggregate.py
git commit -m "feat(web): projects_context/sessions_context — 기간 집계+요약+네비 앵커"
```

---

## Task 4: `/projects` 라우트 + 템플릿 + CSS

**Files:**
- Modify: `tokenomy/web/app.py` (import, `_parse_anchor`, `_PERIODS`, `/projects` 라우트)
- Create: `tokenomy/web/templates/projects.html`
- Modify: `tokenomy/web/static/style.css` (`.period-nav`)
- Test: `tests/test_web.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_web.py` 끝에 추가:

```python
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest tests/test_web.py -k projects -v`
Expected: FAIL — `/projects` 404 (라우트 없음)

- [ ] **Step 3: app.py 구현**

`tokenomy/web/app.py` import 줄을 수정한다. 기존:

```python
from datetime import datetime

from tokenomy.aggregate import PROVIDERS, parse_ts
```

(현재 파일엔 `from datetime import datetime`이 없다 — 추가한다.) 수정 후 import 영역:

```python
from datetime import datetime

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tokenomy.aggregate import KST, PROVIDERS, parse_ts
from tokenomy.budget import budget_from_config, load_config, save_config
from tokenomy.cli import cmd_ingest
from tokenomy.db import connect
from tokenomy.paths import resource_path
from tokenomy.update import check_update
from tokenomy.web.views import (
    dashboard_context, overview_context, projects_context, sessions_context, session_context,
)
```

`_SORTS = ("cost", "sessions", "cache")` 정의(31행 부근) **아래**에 추가:

```python
_PERIODS = ("day", "week", "month")
_ORDERS = ("cost", "recent")


def _parse_anchor(value: str | None) -> datetime:
    """YYYY-MM-DD → KST datetime. 빈값/파싱실패 → 오늘(KST)."""
    if value:
        try:
            return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=KST)
        except ValueError:
            pass
    return datetime.now(KST)
```

`session_view` 라우트 **아래**에 `/projects` 라우트 추가:

```python
@app.get("/projects")
def projects_view(request: Request, period: str = "month", anchor: str | None = None,
                  provider: str = "", sort: str = "cost", notice: str | None = None):
    period = period if period in _PERIODS else "month"
    provider = provider if provider in PROVIDERS else ""
    sort = sort if sort in _SORTS else "cost"
    conn = connect()
    update_tag = check_update(conn)
    ctx = projects_context(conn, period, _parse_anchor(anchor), provider, sort)
    return templates.TemplateResponse(
        request, "projects.html",
        {**ctx, "notice": notice, "update_tag": update_tag},
    )
```

- [ ] **Step 4: CSS 추가**

`tokenomy/web/static/style.css` 끝에 추가:

```css
.period-nav { display:flex; align-items:center; gap:12px; margin:10px 0; }
.period-nav .label { font-weight:600; }
```

- [ ] **Step 5: projects.html 생성**

`tokenomy/web/templates/projects.html`:

```html
{% extends "base.html" %}
{% block body %}
{% include "_tabs.html" %}

<section class="card">
  <div class="card-head">
    <h2>전체 프로젝트별 비용</h2>
    <nav class="toggle small">
      <a href="/projects?period=day&anchor={{ anchor }}&provider={{ provider }}&sort={{ sort }}" class="{{ 'on' if period == 'day' }}">일간</a>
      <a href="/projects?period=week&anchor={{ anchor }}&provider={{ provider }}&sort={{ sort }}" class="{{ 'on' if period == 'week' }}">주간</a>
      <a href="/projects?period=month&anchor={{ anchor }}&provider={{ provider }}&sort={{ sort }}" class="{{ 'on' if period == 'month' }}">월간</a>
    </nav>
  </div>

  <div class="period-nav">
    <a class="btn" href="/projects?period={{ period }}&anchor={{ prev_anchor }}&provider={{ provider }}&sort={{ sort }}">‹ 이전</a>
    <span class="label">{{ period_label }}</span>
    {% if has_next %}<a class="btn" href="/projects?period={{ period }}&anchor={{ next_anchor }}&provider={{ provider }}&sort={{ sort }}">다음 ›</a>{% endif %}
  </div>

  <div class="card-head">
    <p class="muted">{{ period_label }} · 전체 {{ count }}개 · 합계 ${{ '%.2f'|format(total) }}</p>
    <nav class="toggle small">
      <a href="/projects?period={{ period }}&anchor={{ anchor }}&provider={{ provider }}&sort=cost" class="{{ 'on' if sort == 'cost' }}">비용</a>
      <a href="/projects?period={{ period }}&anchor={{ anchor }}&provider={{ provider }}&sort=sessions" class="{{ 'on' if sort == 'sessions' }}">세션</a>
      <a href="/projects?period={{ period }}&anchor={{ anchor }}&provider={{ provider }}&sort=cache" class="{{ 'on' if sort == 'cache' }}">캐시</a>
    </nav>
  </div>

  <table class="grid">
    <thead><tr><th>비용</th><th>캐시</th><th>세션</th><th>프로젝트</th></tr></thead>
    <tbody>
      {% for p in rows %}
      <tr><td>${{ '%.2f'|format(p.cost) }}</td><td>{{ '%.0f'|format(p.cache_ratio * 100) }}%</td>
          <td>{{ p.sessions }}</td>
          <td><a href="/sessions?project={{ (p.project or '(unknown)')|urlencode }}&period={{ period }}&anchor={{ anchor }}&provider={{ provider }}">{{ p.project or '(unknown)' }}</a></td></tr>
      {% else %}<tr><td colspan="4" class="muted">이 기간 데이터 없음</td></tr>{% endfor %}
    </tbody>
  </table>
</section>
{% endblock %}
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `python -m pytest tests/test_web.py -k projects -v`
Expected: 3개 PASS

- [ ] **Step 7: 커밋**

```bash
git add tokenomy/web/app.py tokenomy/web/templates/projects.html tokenomy/web/static/style.css tests/test_web.py
git commit -m "feat(web): /projects 전체 프로젝트 페이지 + 기간 토글/네비/드릴다운"
```

---

## Task 5: `/sessions` 라우트 + 템플릿

**Files:**
- Modify: `tokenomy/web/app.py` (`/sessions` 라우트)
- Create: `tokenomy/web/templates/sessions.html`
- Test: `tests/test_web.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_web.py` 끝에 추가:

```python
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest tests/test_web.py -k sessions -v`
Expected: FAIL — `/sessions` 404

- [ ] **Step 3: app.py에 `/sessions` 라우트 추가**

`tokenomy/web/app.py`의 `projects_view` 라우트 **아래**에 추가:

```python
@app.get("/sessions")
def sessions_view(request: Request, period: str = "month", anchor: str | None = None,
                  provider: str = "", order: str = "cost",
                  project: str | None = None, notice: str | None = None):
    period = period if period in _PERIODS else "month"
    provider = provider if provider in PROVIDERS else ""
    order = order if order in _ORDERS else "cost"
    conn = connect()
    update_tag = check_update(conn)
    ctx = sessions_context(conn, period, _parse_anchor(anchor), provider, order, project or "")
    return templates.TemplateResponse(
        request, "sessions.html",
        {**ctx, "notice": notice, "update_tag": update_tag},
    )
```

- [ ] **Step 4: sessions.html 생성**

`tokenomy/web/templates/sessions.html`:

```html
{% extends "base.html" %}
{% block body %}
{% include "_tabs.html" %}

<section class="card">
  <div class="card-head">
    <h2>복기 — 전체 세션</h2>
    <nav class="toggle small">
      <a href="/sessions?period=day&anchor={{ anchor }}&provider={{ provider }}&order={{ order }}{% if project %}&project={{ project|urlencode }}{% endif %}" class="{{ 'on' if period == 'day' }}">일간</a>
      <a href="/sessions?period=week&anchor={{ anchor }}&provider={{ provider }}&order={{ order }}{% if project %}&project={{ project|urlencode }}{% endif %}" class="{{ 'on' if period == 'week' }}">주간</a>
      <a href="/sessions?period=month&anchor={{ anchor }}&provider={{ provider }}&order={{ order }}{% if project %}&project={{ project|urlencode }}{% endif %}" class="{{ 'on' if period == 'month' }}">월간</a>
    </nav>
  </div>

  <div class="period-nav">
    <a class="btn" href="/sessions?period={{ period }}&anchor={{ prev_anchor }}&provider={{ provider }}&order={{ order }}{% if project %}&project={{ project|urlencode }}{% endif %}">‹ 이전</a>
    <span class="label">{{ period_label }}</span>
    {% if has_next %}<a class="btn" href="/sessions?period={{ period }}&anchor={{ next_anchor }}&provider={{ provider }}&order={{ order }}{% if project %}&project={{ project|urlencode }}{% endif %}">다음 ›</a>{% endif %}
  </div>

  {% if project %}
  <p class="muted">프로젝트 필터: {{ project }} ·
    <a href="/sessions?period={{ period }}&anchor={{ anchor }}&provider={{ provider }}&order={{ order }}">해제</a></p>
  {% endif %}

  <div class="card-head">
    <p class="muted">{{ period_label }} · 전체 {{ count }}개 · 합계 ${{ '%.2f'|format(total) }}</p>
    <nav class="toggle small">
      <a href="/sessions?period={{ period }}&anchor={{ anchor }}&provider={{ provider }}&order=cost{% if project %}&project={{ project|urlencode }}{% endif %}" class="{{ 'on' if order == 'cost' }}">비용순</a>
      <a href="/sessions?period={{ period }}&anchor={{ anchor }}&provider={{ provider }}&order=recent{% if project %}&project={{ project|urlencode }}{% endif %}" class="{{ 'on' if order == 'recent' }}">최신순</a>
    </nav>
  </div>

  <table class="grid">
    <thead><tr><th>기간</th><th>비용</th><th>프로젝트</th><th>라벨</th><th></th></tr></thead>
    <tbody>
      {% for s in rows %}
      <tr><td>{{ s.first_ts|kstfmt }}</td><td>${{ '%.2f'|format(s.cost) }}</td>
          <td>{{ s.project or '(unknown)' }}</td><td>{{ s.label or '[라벨 없음]' }}</td>
          <td><a href="/session/{{ s.session_id }}">▸</a></td></tr>
      {% else %}<tr><td colspan="5" class="muted">이 기간 데이터 없음</td></tr>{% endfor %}
    </tbody>
  </table>
</section>
{% endblock %}
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `python -m pytest tests/test_web.py -k sessions -v`
Expected: 3개 PASS

- [ ] **Step 6: 커밋**

```bash
git add tokenomy/web/app.py tokenomy/web/templates/sessions.html tests/test_web.py
git commit -m "feat(web): /sessions 전체 세션 페이지 + 정렬 토글/필터 해제"
```

---

## Task 6: 대시보드 표 → 미리보기 + `전체 보기` 링크

**Files:**
- Modify: `tokenomy/web/views.py` (`dashboard_context` 프로젝트 Top 10)
- Modify: `tokenomy/web/templates/overview.html`
- Modify: `tokenomy/web/templates/dashboard.html`
- Test: `tests/test_aggregate.py`, `tests/test_web.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_aggregate.py` 끝에 추가:

```python
def test_dashboard_context_limits_projects_to_10(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    for i in range(11):
        _msg(conn, dedup_key=f"k{i}", session_id=f"s{i}", project=f"/p{i}",
             ts="2026-06-10T10:00:00Z", cost_usd=float(i + 1))
    ctx = dashboard_context(conn, provider="claude", sort="cost", now_kst=_NOW_STATUS)
    assert len(ctx["projects"]) == 10        # AI별 프로젝트 표도 Top 10 미리보기
```

`tests/test_web.py` 끝에 추가:

```python
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest tests/test_aggregate.py -k limits_projects tests/test_web.py -k "full_view_links" -v`
Expected: FAIL (현재 dashboard 프로젝트 표 전체 노출, `전체 보기` 링크 없음)

- [ ] **Step 3: views.py — dashboard 프로젝트 Top 10**

`tokenomy/web/views.py`의 `dashboard_context` 안, 기존:

```python
    projects = by_project(conn, provider, now)
    projects.sort(key=_SORT_KEYS.get(sort, _SORT_KEYS["cost"]), reverse=True)
```

수정 후(정렬 뒤 Top 10 — overview_context와 동일 패턴):

```python
    projects = by_project(conn, provider, now)
    projects.sort(key=_SORT_KEYS.get(sort, _SORT_KEYS["cost"]), reverse=True)
    projects = projects[:10]   # AI별 상세도 Top 10 미리보기, 전체는 /projects
```

- [ ] **Step 4: overview.html — `전체 보기` 링크**

`tokenomy/web/templates/overview.html`의 통합 프로젝트 표 `</table>` **다음**, 해당 `</section>` 직전에 추가:

```html
  <p class="muted"><a href="/projects?sort={{ sort }}">전체 보기 →</a></p>
```

복기 세션 표 `</table>` **다음**, 해당 `</section>` 직전에 추가:

```html
  <p class="muted"><a href="/sessions">전체 보기 →</a></p>
```

- [ ] **Step 5: dashboard.html — `(Top 10)` 표기 + 링크**

`tokenomy/web/templates/dashboard.html`의 프로젝트 표 헤더, 기존:

```html
    <h2>프로젝트별 비용</h2>
```

수정 후:

```html
    <h2>프로젝트별 비용 <span class="muted">(Top 10)</span></h2>
```

프로젝트 표 `</table>` **다음**, 해당 `</section>` 직전에 추가:

```html
  <p class="muted"><a href="/projects?provider={{ provider }}&sort={{ sort }}">전체 보기 →</a></p>
```

복기 세션 표 `</table>` **다음**, 해당 `</section>` 직전에 추가:

```html
  <p class="muted"><a href="/sessions?provider={{ provider }}">전체 보기 →</a></p>
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `python -m pytest tests/test_aggregate.py -k limits_projects -v` 및 `python -m pytest tests/test_web.py -k full_view_links -v`
Expected: 전부 PASS

- [ ] **Step 7: 전체 스위트 회귀 확인**

Run: `python -m pytest -q`
Expected: 전부 PASS (기존 `test_launcher` 포트 충돌 2건은 환경 의존 — 앱이 8765 점유 중이면 무시, 회귀 아님)

- [ ] **Step 8: 커밋**

```bash
git add tokenomy/web/views.py tokenomy/web/templates/overview.html tokenomy/web/templates/dashboard.html tests/test_aggregate.py tests/test_web.py
git commit -m "feat(web): 대시보드 표를 미리보기(Top 10)+전체 보기 링크로 통일"
```

---

## Self-Review 메모

- **Spec 커버리지**: 라우트(T4·T5) / period_bounds(T1) / `_range_rows`+기간 인자(T2) / 컨텍스트(T3) / 템플릿·토글·네비·요약·드릴다운·정렬(T4·T5) / 대시보드 미리보기·링크·AI별 Top 10(T6) — 전부 매핑됨.
- **주 시작=월요일**: `period_bounds` week 분기 `a.weekday()` 기준, T1에서 `2026-06-08` 검증.
- **`다음 ›` 숨김**: `has_next = nxt <= now`, T3에서 현재/과거 양쪽 검증.
- **타입 일관성**: `period_bounds`는 항상 `(start, nxt, label)` 3-튜플. `by_project`/`by_session`의 키워드 전용 `start`/`nxt` 시그니처가 호출부(views)와 일치. 컨텍스트 키(`rows/count/total/period/period_label/anchor/prev_anchor/next_anchor/has_next/provider/sort|order/project`)가 템플릿 사용처와 일치.
- **시간 의존 테스트 회피**: 라우트 데이터 테스트는 `anchor`를 명시(`2026-06-10`)해 실제 현재 시각과 무관하게 동작.
- **빈 기간**: 템플릿 `{% else %}`로 "이 기간 데이터 없음", 합계 $0.00.
