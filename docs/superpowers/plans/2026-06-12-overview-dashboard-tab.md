# 전체 현황 대시보드 탭 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `전체 · Claude · Codex` 상단 탭바를 신설하고, 전 AI를 합산한 "전체(overview)" 화면을 메인(`/`)으로 제공한다.

**Architecture:** 기존 집계 함수가 모두 거치는 `_month_rows()`에 `provider=None`(=전 AI) 분기를 추가해 합산 집계를 공짜로 얻는다(A안). AI별 카드는 provider별 `burndown()`을 루프로 돌려 얻고, 통합 번다운은 한도 있는 provider만 합산한다. 라우트 `/`는 `provider` 파라미터 유무로 overview/detail을 분기한다.

**Tech Stack:** Python 3.14, FastAPI, Jinja2, SQLite, Chart.js, pytest + fastapi TestClient.

---

## 사전 확인 (구현 전 읽을 것)

- 집계 진입점: `tokenomy/aggregate.py` — `_month_rows`, `burndown`, `by_project`, `by_session`, `daily_series`, `insights`.
- 화면 조립: `tokenomy/web/views.py` — `dashboard_context`.
- 라우팅: `tokenomy/web/app.py` — `GET /`.
- 템플릿: `tokenomy/web/templates/{base,dashboard,settings}.html`, 정적: `static/style.css`.
- 테스트 관례: `tests/test_aggregate.py`(`_insert`, `_msg`, `NOW`, `_NOW_STATUS`, `_B`), `tests/test_web.py`(`_client`).
- 예산은 `Budget(claude, codex)` 고정 — **이번 작업에서 변경하지 않는다**(N-provider 구조만 준비).

## 파일 구조 (생성/수정)

| 파일 | 책임 | 작업 |
| --- | --- | --- |
| `tokenomy/aggregate.py` | `PROVIDERS` 상수, `_month_rows(provider=None)`, `_compute_burndown` 추출, `combined_burndown` | 수정 |
| `tokenomy/web/views.py` | `overview_context` 신설, `dashboard_context`에 `active_tab` | 수정 |
| `tokenomy/web/app.py` | `/` overview/detail 분기, `PROVIDERS` import | 수정 |
| `tokenomy/web/templates/_tabs.html` | 공유 헤더 + 탭바 + 공통 배너 | 생성 |
| `tokenomy/web/templates/overview.html` | 전체 탭 화면 | 생성 |
| `tokenomy/web/templates/dashboard.html` | `_tabs` include + 카드 내 토글 제거 | 수정 |
| `tokenomy/web/static/style.css` | `.tabs`, `.ai-cards`, `.ai-card` 스타일 | 수정 |
| `tests/test_aggregate.py` | combined/None 집계 + overview_context 테스트 | 수정 |
| `tests/test_web.py` | 라우팅 + overview 통합 테스트 | 수정 |

---

### Task 1: `PROVIDERS` 상수 + `_month_rows(provider=None)` 전 AI 합산

**Files:**
- Modify: `tokenomy/aggregate.py`
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: 실패하는 테스트 추가**

`tests/test_aggregate.py` 상단 import에 `by_project`는 이미 있음. 파일 끝에 추가:

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_aggregate.py::test_providers_constant tests/test_aggregate.py::test_by_project_combines_providers_when_none -v`
Expected: FAIL (`PROVIDERS` ImportError / provider=None이 `WHERE provider=?`에 None 바인딩되어 0행).

- [ ] **Step 3: 구현**

`tokenomy/aggregate.py`에서 `KST = ...` 아래에 상수 추가:

```python
KST = timezone(timedelta(hours=9))

# 합산/탭바가 도는 provider 목록. 3번째 AI 추가 시 여기 + Budget 필드 + 파서 + 단가만 보강.
PROVIDERS = ("claude", "codex")
```

그리고 `_month_rows`를 `provider=None`(전 AI) 지원으로 교체:

```python
def _month_rows(conn, provider: str | None, now_kst: datetime) -> list:
    start, nxt = month_bounds(now_kst)
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
```

- [ ] **Step 4: 통과 확인**

Run: `python -m pytest tests/test_aggregate.py -v`
Expected: PASS (신규 2건 + 기존 전부).

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): PROVIDERS 상수 + _month_rows 전 AI 합산(provider=None)"
```

---

### Task 2: burndown 산술을 순수 헬퍼 `_compute_burndown`로 추출 (리팩터)

**Files:**
- Modify: `tokenomy/aggregate.py:82-117` (`burndown`)
- Test: 기존 `tests/test_aggregate.py`의 burndown 테스트가 안전망

- [ ] **Step 1: 기존 테스트로 green 베이스라인 확인**

Run: `python -m pytest tests/test_aggregate.py -k burndown -v`
Expected: PASS (리팩터 전 현재 동작 기준선).

- [ ] **Step 2: 산술 추출 + `burndown` 재작성**

`tokenomy/aggregate.py`의 `burndown` 함수(82~117행)를 아래로 교체. 산술은 `_compute_burndown`로 빼고 `burndown`은 행 조회만 한다(동작 동일):

```python
def _compute_burndown(provider: str, spent: float, limit: float,
                      unpriced: int, now_kst: datetime) -> Burndown:
    """집계된 (spent, limit, unpriced)로 Burndown을 산출하는 순수 함수.
    provider별 burndown과 통합 combined_burndown이 공유한다."""
    start, nxt = month_bounds(now_kst)
    days_in_month = (nxt - start).days
    day_of_month = now_kst.day
    days_left = days_in_month - day_of_month
    daily_avg = spent / day_of_month if day_of_month else 0.0
    projected = daily_avg * days_in_month
    pct = (spent / limit) if limit > 0 else 0.0

    exhaust_day: int | None = None
    if daily_avg > 0 and limit > 0:
        d = limit / daily_avg
        if d <= days_in_month:
            exhaust_day = int(d) if d == int(d) else int(d) + 1  # ceil

    on_track = (projected <= limit) if limit > 0 else True

    if limit > 0 and spent >= limit:
        status = "exceeds"
    elif limit > 0 and projected > limit:
        status = "warn"
    else:
        status = "ok"

    return Burndown(
        provider=provider, limit=limit, spent=round(spent, 4), pct=round(pct, 4),
        days_in_month=days_in_month, day_of_month=day_of_month, days_left=days_left,
        daily_avg=round(daily_avg, 4), projected_month=round(projected, 4),
        exhaust_day=exhaust_day, on_track=on_track, unpriced_count=unpriced,
        status=status,
    )


def burndown(conn, budget: Budget, now_kst: datetime, provider: str = "claude") -> Burndown:
    rows = _month_rows(conn, provider, now_kst)
    spent = sum((r["cost_usd"] or 0) for r in rows)
    unpriced = sum(1 for r in rows if not r["priced"])
    limit = budget.limit_for(provider)
    return _compute_burndown(provider, spent, limit, unpriced, now_kst)
```

- [ ] **Step 3: 무회귀 확인**

Run: `python -m pytest tests/test_aggregate.py -v`
Expected: PASS (모든 기존 burndown/insights 테스트 그대로).

- [ ] **Step 4: 커밋**

```bash
git add tokenomy/aggregate.py
git commit -m "refactor(aggregate): burndown 산술을 _compute_burndown 순수 함수로 추출"
```

---

### Task 3: `combined_burndown` — 한도 있는 provider만 합산

**Files:**
- Modify: `tokenomy/aggregate.py`
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: 실패하는 테스트 추가**

`tests/test_aggregate.py` import 줄에 `combined_burndown` 추가하고(`from tokenomy.aggregate import (... combined_burndown ...)`), 파일 끝에 추가:

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_aggregate.py -k combined_burndown -v`
Expected: FAIL (`combined_burndown` ImportError).

- [ ] **Step 3: 구현**

`tokenomy/aggregate.py`의 `burndown` 함수 바로 아래에 추가:

```python
def combined_burndown(cards: list[Burndown], now_kst: datetime) -> Burndown:
    """provider별 Burndown 리스트 → 통합 Burndown.

    한도(limit>0)가 있는 provider만 spent·limit·unpriced를 합산해 분자/분모 범위를
    일치시킨다(예: claude 한도만 있으면 codex 지출은 통합 바에서 제외). 한도 있는
    provider가 하나도 없으면 limit=0(사용량만, spent=전체 합산)으로 둔다.
    """
    capped = [c for c in cards if c.limit > 0]
    if capped:
        spent = sum(c.spent for c in capped)
        limit = sum(c.limit for c in capped)
        unpriced = sum(c.unpriced_count for c in capped)
    else:
        spent = sum(c.spent for c in cards)
        limit = 0.0
        unpriced = sum(c.unpriced_count for c in cards)
    return _compute_burndown("전체", spent, limit, unpriced, now_kst)
```

- [ ] **Step 4: 통과 확인**

Run: `python -m pytest tests/test_aggregate.py -v`
Expected: PASS (combined 3건 + 기존 전부).

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): combined_burndown(한도 있는 provider만 합산)"
```

---

### Task 4: `overview_context` + `dashboard_context` active_tab

**Files:**
- Modify: `tokenomy/web/views.py`
- Test: `tests/test_aggregate.py` (views 테스트가 이 파일에 있음)

- [ ] **Step 1: 실패하는 테스트 추가**

`tests/test_aggregate.py` 상단 import를 다음으로 교체:

```python
from tokenomy.web.views import dashboard_context, overview_context, session_context
```

파일 끝에 추가:

```python
def test_overview_context_shape(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"budget": {"claude": 100, "codex": 50}}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=10.0, project="/p")
    _msg(conn, dedup_key="b", provider="codex", ts="2026-06-11T10:00:00Z", cost_usd=4.0, project="/p")
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)
    assert ctx["active_tab"] == "overview"
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


def test_dashboard_context_has_active_tab(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    ctx = dashboard_context(conn, provider="codex", sort="cost", now_kst=_NOW_STATUS)
    assert ctx["active_tab"] == "codex"
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_aggregate.py -k "overview_context or active_tab" -v`
Expected: FAIL (`overview_context` ImportError, `active_tab` KeyError).

- [ ] **Step 3: 구현**

`tokenomy/web/views.py`의 import를 교체·확장:

```python
from tokenomy.aggregate import (
    KST, PROVIDERS, burndown, by_project, by_session, combined_burndown,
    daily_series, insights, session_detail,
)
```

`dashboard_context`의 return dict에 `"active_tab": provider,` 한 줄 추가(예: `"provider": provider, "sort": sort,` 바로 아래).

파일 끝(또는 `session_context` 위)에 추가:

```python
def _provider_has_data(conn, provider: str) -> bool:
    row = conn.execute(
        "SELECT MAX(ts) t FROM messages WHERE provider=?", (provider,)
    ).fetchone()
    return row is not None and row["t"] is not None


def overview_context(conn, sort: str, now_kst: datetime | None = None) -> dict:
    now = now_kst or datetime.now(KST)
    config = load_config()
    budget = budget_from_config(config)

    cards = [
        {"provider": p, "name": p.capitalize(),
         "bd": burndown(conn, budget, now, p),
         "has_data": _provider_has_data(conn, p)}
        for p in PROVIDERS
    ]
    combined = combined_burndown([c["bd"] for c in cards], now)

    projects = by_project(conn, None, now)
    projects.sort(key=_SORT_KEYS.get(sort, _SORT_KEYS["cost"]), reverse=True)
    projects = projects[:10]
    sessions = by_session(conn, None, now, limit_n=10)
    coach = insights(conn, combined, now, None)
    daily = daily_series(conn, None, now)
    pace = [round(combined.limit / combined.days_in_month * p.day, 4)
            if combined.limit else 0.0 for p in daily]

    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    has_data = last is not None and last["t"] is not None

    return {
        "active_tab": "overview", "sort": sort,
        "user_label": user_label(config),
        "budget_configured": combined.limit > 0,
        "month": now.strftime("%Y-%m"),
        "combined": combined, "cards": cards,
        "projects": projects, "sessions": sessions, "insights": coach,
        "daily_labels": [p.day for p in daily],
        "daily_actual": [p.cumulative_cost for p in daily],
        "daily_pace": pace,
        "last_ts": last["t"] if has_data else None,
        "has_data": has_data,
    }
```

- [ ] **Step 4: 통과 확인**

Run: `python -m pytest tests/test_aggregate.py -v`
Expected: PASS (신규 3건 + 기존 전부).

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/views.py tests/test_aggregate.py
git commit -m "feat(web): overview_context + dashboard_context active_tab"
```

---

### Task 5: `/` 라우트 overview/detail 분기

**Files:**
- Modify: `tokenomy/web/app.py:31,35-46`
- Test: `tests/test_web.py`

- [ ] **Step 1: 실패하는 테스트 추가**

`tests/test_web.py` 끝에 추가:

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_web.py -k "overview or detail or fall_back" -v`
Expected: FAIL (현재 `/`는 dashboard.html을 렌더 → "통합 번다운"/"AI별 현황" 없음; overview.html 미존재).

> 주의: 이 단계의 FAIL은 라우트 미분기 + 템플릿 미존재 양쪽이 원인. 라우트는 Step 3에서, 템플릿은 Task 6에서 만든다. Task 6 완료 후 본 테스트가 최종 통과한다.

- [ ] **Step 3: 라우트 분기 구현**

`tokenomy/web/app.py` 상단 import 교체:

```python
from tokenomy.aggregate import PROVIDERS, parse_ts
```

`from tokenomy.web.views import ...` 줄을 교체:

```python
from tokenomy.web.views import dashboard_context, overview_context, session_context
```

`_PROVIDERS = ("claude", "codex")` 줄을 삭제(이제 `aggregate.PROVIDERS` 사용). `_SORTS`는 유지.

`dashboard` 라우트(35~46행)를 교체:

```python
@app.get("/")
def dashboard(request: Request, provider: str | None = None, sort: str = "cost",
              notice: str | None = None):
    sort = sort if sort in _SORTS else "cost"
    conn = connect()
    update_tag = check_update(conn)
    if provider in PROVIDERS:
        ctx = dashboard_context(conn, provider, sort)
        template = "dashboard.html"
    else:
        ctx = overview_context(conn, sort)
        template = "overview.html"
    return templates.TemplateResponse(
        request, template,
        {**ctx, "notice": notice, "update_tag": update_tag},
    )
```

- [ ] **Step 4: 부분 확인 (라우트 단독)**

Run: `python -m pytest tests/test_web.py -k "detail" -v`
Expected: `test_provider_query_renders_detail` PASS (detail 경로는 기존 dashboard.html 사용). overview 테스트 2건은 템플릿 미존재로 여전히 FAIL → Task 6에서 해결.

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/app.py tests/test_web.py
git commit -m "feat(web): / 라우트 provider 유무로 overview/detail 분기"
```

---

### Task 6: 템플릿 — `_tabs.html` 신설, `overview.html` 신설, `dashboard.html` 리팩터

**Files:**
- Create: `tokenomy/web/templates/_tabs.html`
- Create: `tokenomy/web/templates/overview.html`
- Modify: `tokenomy/web/templates/dashboard.html`
- Test: `tests/test_web.py`

- [ ] **Step 1: 실패하는 통합 테스트 추가 + 기존 테스트 수정**

`tests/test_web.py`에서 기존 `test_dashboard_renders_sections_with_data`의 `r = client.get("/")`를 `r = client.get("/?provider=claude")`로 바꾼다(이 테스트는 이제 detail 페이지 섹션을 검증). 나머지는 그대로 둔다.

`tests/test_web.py` 끝에 추가:

```python
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
    assert "proj" in r.text                    # provider 무관 프로젝트 합산
    assert 'class="tabs"' in r.text
    assert 'class="ai-cards"' in r.text


def test_overview_tabs_active_state(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/")
    assert 'href="/"' in r.text
    assert 'href="/?provider=claude"' in r.text
    assert 'href="/?provider=codex"' in r.text
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_web.py -k "overview" -v`
Expected: FAIL (overview.html 미존재 → TemplateNotFound / 500).

- [ ] **Step 3: `_tabs.html` 생성**

`tokenomy/web/templates/_tabs.html`:

```html
<header class="topbar">
  <div>🪙 Tokenomy · {{ month }} (KST){% if user_label %} · {{ user_label }}{% endif %}</div>
  <div class="topbar-right">
    {% if last_ts %}<span class="muted">데이터 최신: {{ last_ts|kstfmt }}</span>{% endif %}
    <a class="btn" href="/settings">⚙ 설정</a>
    <form method="post" action="/ingest" class="inline"><button class="btn">↻ 새로고침</button></form>
  </div>
</header>

<nav class="tabs">
  <a href="/" class="{{ 'on' if active_tab == 'overview' }}">전체</a>
  <a href="/?provider=claude" class="{{ 'on' if active_tab == 'claude' }}">Claude</a>
  <a href="/?provider=codex" class="{{ 'on' if active_tab == 'codex' }}">Codex</a>
</nav>

{% if notice == "ingest-failed" %}
<div class="banner error">새로고침(ingest) 중 오류 — 기존 데이터를 표시합니다.</div>
{% endif %}

{% if update_tag %}
<div class="banner update">새 버전 {{ update_tag }} 사용 가능 —
  <a href="https://github.com/genius-kim-samsung/tokenomy/releases/latest" target="_blank" rel="noopener">다운로드</a>
</div>
{% endif %}
```

- [ ] **Step 4: `overview.html` 생성**

`tokenomy/web/templates/overview.html`:

```html
{% extends "base.html" %}
{% block body %}
{% include "_tabs.html" %}

{% if not budget_configured %}
<div class="banner">예산을 설정하세요 → <a href="/settings">설정</a> (지금은 사용량 추적만)</div>
{% endif %}

{% set bd = combined %}
<section class="card">
  <h2>통합 번다운 <span class="muted">(한도 설정한 AI 합산)</span></h2>
  {% if not has_data %}
    <p class="muted">데이터 없음 · [↻ 새로고침]을 누르세요</p>
  {% elif bd.limit == 0 %}
    <p class="muted">예산 미설정 · 지출 ${{ '%.2f'|format(bd.spent) }} · <a href="/settings">예산 설정</a></p>
    <p class="disclaimer">ⓘ 공개 API 단가 기준 추정 · 이 머신 데이터만</p>
  {% else %}
    <div class="bd-row">
      <span class="bd-label">전체</span>
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

<section class="card">
  <h2>AI별 현황</h2>
  <div class="ai-cards">
    {% for c in cards %}
    {% set b = c.bd %}
    <a class="ai-card" href="/?provider={{ c.provider }}">
      <div class="ai-name">{{ c.name }}</div>
      {% if not c.has_data %}
        <div class="muted">(이 머신에 {{ c.name }} 로그 없음)</div>
      {% elif b.limit == 0 %}
        <div class="ai-num">${{ '%.2f'|format(b.spent) }} <span class="muted">사용량만</span></div>
      {% else %}
        <div class="ai-num">${{ '%.2f'|format(b.spent) }} / ${{ '%.0f'|format(b.limit) }}</div>
        <span class="bar"><span class="fill s-{{ b.status }}" style="width: {{ [b.pct * 100, 100]|min }}%"></span></span>
        <div class="muted">{{ '%.1f'|format(b.pct * 100) }}% · 예상월말 ${{ '%.0f'|format(b.projected_month) }}
          <span class="status s-{{ b.status }}">{{ {'ok':'✅','warn':'⚠','exceeds':'⛔'}[b.status] }}</span>
        </div>
      {% endif %}
    </a>
    {% endfor %}
  </div>
</section>

{% if has_data %}
<section class="card">
  <h2>통합 추세 <span class="muted">(전 AI 합산)</span></h2>
  <canvas id="trend" height="120"></canvas>
</section>
{% endif %}

<section class="card">
  <h2>통합 효율 코치</h2>
  <ul class="coach">
    {% for c in insights %}<li class="lvl-{{ c.level }}">{{ c.text }}</li>{% endfor %}
  </ul>
</section>

<section class="card">
  <div class="card-head">
    <h2>통합 프로젝트별 비용 <span class="muted">(Top 10)</span></h2>
    <nav class="toggle small">
      <a href="/?sort=cost" class="{{ 'on' if sort == 'cost' }}">비용</a>
      <a href="/?sort=sessions" class="{{ 'on' if sort == 'sessions' }}">세션</a>
      <a href="/?sort=cache" class="{{ 'on' if sort == 'cache' }}">캐시</a>
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
  <h2>복기 — 최근 비싼 세션 <span class="muted">(Top 10)</span></h2>
  <table class="grid">
    <thead><tr><th>기간</th><th>비용</th><th>프로젝트</th><th>라벨</th><th></th></tr></thead>
    <tbody>
      {% for s in sessions %}
      <tr><td>{{ s.first_ts|kstfmt }}</td><td>${{ '%.2f'|format(s.cost) }}</td>
          <td>{{ s.project or '(unknown)' }}</td><td>{{ s.label or '[라벨 없음]' }}</td>
          <td><a href="/session/{{ s.session_id }}">▸</a></td></tr>
      {% else %}<tr><td colspan="5" class="muted">데이터 없음</td></tr>{% endfor %}
    </tbody>
  </table>
</section>
{% endblock %}
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

- [ ] **Step 5: `dashboard.html` 리팩터 (전체 교체)**

`tokenomy/web/templates/dashboard.html`을 아래로 전체 교체. 변경점: 상단 `<header>`+배너를 `{% include "_tabs.html" %}`로 대체, 번다운 카드 내 `[Claude][Codex]` 토글 제거(프로젝트 정렬 토글은 유지):

```html
{% extends "base.html" %}
{% block body %}
{% include "_tabs.html" %}

{% if not budget_configured %}
<div class="banner">예산을 설정하세요 → <a href="/settings">설정</a> (지금은 사용량 추적만)</div>
{% endif %}

<section class="card">
  <h2>번다운</h2>
  {% set bd = burndown %}
  {% if not has_data and provider == "codex" %}
    <p class="muted">(이 머신에 Codex 로그 없음)</p>
  {% elif not has_data %}
    <p class="muted">데이터 없음 · [↻ 새로고침]을 누르세요</p>
  {% elif bd.limit == 0 %}
    <p class="muted">예산 미설정 · 지출 ${{ '%.2f'|format(bd.spent) }} · <a href="/settings">예산 설정</a></p>
    <p class="disclaimer">ⓘ 공개 API 단가 기준 추정 · 이 머신 데이터만</p>
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
    <h2>프로젝트별 비용</h2>
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
      <tr><td>{{ s.first_ts|kstfmt }}</td><td>${{ '%.2f'|format(s.cost) }}</td>
          <td>{{ s.project or '(unknown)' }}</td><td>{{ s.label or '[라벨 없음]' }}</td>
          <td><a href="/session/{{ s.session_id }}">▸</a></td></tr>
      {% else %}<tr><td colspan="5" class="muted">데이터 없음</td></tr>{% endfor %}
    </tbody>
  </table>
</section>
{% endblock %}
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

- [ ] **Step 6: CSS 추가**

`tokenomy/web/static/style.css` 끝(마지막 `a { ... }` 줄 아래)에 추가:

```css
.tabs { display:flex; gap:4px; border-bottom:1px solid var(--line); margin:4px 0 14px; }
.tabs a { color:var(--muted); text-decoration:none; padding:8px 16px; border-radius:8px 8px 0 0; }
.tabs a.on { color:#fff; background:var(--card); border:1px solid var(--line); border-bottom-color:var(--card); margin-bottom:-1px; }
.ai-cards { display:flex; gap:12px; flex-wrap:wrap; }
.ai-card { flex:1; min-width:200px; display:block; background:#0d1117; border:1px solid var(--line);
           border-radius:8px; padding:12px; text-decoration:none; color:var(--fg); }
.ai-card:hover { border-color:#3d444d; }
.ai-name { font-weight:600; margin-bottom:6px; }
.ai-num { font-size:16px; margin:4px 0; }
.ai-card .bar { margin:6px 0; }
```

- [ ] **Step 7: 전체 통과 확인**

Run: `python -m pytest tests/test_web.py -v`
Expected: PASS (overview/detail/탭/통합 + 기존 전부). Task 5에서 FAIL로 남았던 overview 테스트들도 통과.

- [ ] **Step 8: 커밋**

```bash
git add tokenomy/web/templates/_tabs.html tokenomy/web/templates/overview.html tokenomy/web/templates/dashboard.html tokenomy/web/static/style.css tests/test_web.py
git commit -m "feat(web): 전체 현황 overview 탭 + 상단 탭바(전체·Claude·Codex)"
```

---

### Task 7: 전체 검증 + 수동 스모크

**Files:** 없음(검증만)

- [ ] **Step 1: 전체 테스트**

Run: `python -m pytest -q`
Expected: 전체 PASS(기존 + 신규).

- [ ] **Step 2: 수동 스모크 (선택, 환경 가능 시)**

Run: `python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765`
브라우저 `http://127.0.0.1:8765/` 접속 후 확인:
- 상단 탭바 `전체 · Claude · Codex`, `전체` 활성.
- 통합 번다운(데이터/예산 있으면 바), AI별 카드 2개(클릭 시 해당 detail 이동), 통합 추세 그래프, 효율 코치, 프로젝트 Top 10, 세션 Top 10.
- `Claude`/`Codex` 탭 클릭 → 기존 상세 화면, 카드 내 provider 토글 없음, 정렬 토글 동작.
- `⚙ 설정` → 예산 저장 후 `/`(전체)로 복귀.

- [ ] **Step 3: 마무리 커밋(필요 시)**

스모크 중 발견한 사소한 수정만 별도 커밋.

---

## Self-Review (작성자 점검 결과)

**1. Spec coverage**
- 라우팅/탭바(spec §1) → Task 5(분기) + Task 6(`_tabs.html`, 활성표시). ✓
- 통합 번다운 = 한도 있는 provider만 합산(spec §2-1) → Task 3 `combined_burndown`. ✓
- AI별 카드(spec §2-2, 데이터/한도/무데이터 분기) → Task 4 `overview_context` cards + Task 6 overview.html. ✓
- 통합 추세(spec §2-3, pace는 combined.limit, 한도0 시 숨김) → Task 4 pace + Task 6 캔버스/스크립트. ✓
- 통합 효율 코치(spec §2-4) → Task 4 `insights(..., None)`. ✓
- 통합 프로젝트 Top 10 + 정렬(spec §2-5) → Task 4 정렬 후 `[:10]` + Task 6 토글. ✓
- 최근 비싼 세션 Top 10(spec §2-6) → Task 4 `by_session(..., limit_n=10)`. ✓
- `PROVIDERS` 중앙 상수(spec §3) → Task 1. ✓
- 카드 내 provider 토글 제거(spec §1) → Task 6 Step 5. ✓
- 엣지(데이터/예산 전무, 혼합, 무데이터 provider)(spec §5) → Task 3 + Task 4 + overview.html 분기 + Task 4 두 번째 테스트. ✓
- 테스트 전략(spec §6) → Task 1·3·4(aggregate/views), Task 5·6(web). ✓
- Budget 미변경(spec §3) → 어떤 Task도 budget.py 수정 안 함. ✓

**2. Placeholder scan:** "TBD"/"적절히"/추상 단계 없음. 모든 코드 단계에 완전한 코드 포함. ✓

**3. Type consistency:**
- `combined_burndown(cards, now)`는 Task 3에서 `list[Burndown]`을 받음. Task 4는 `[c["bd"] for c in cards]`(Burndown 리스트)로 호출 — 일치. ✓
- `overview_context`의 cards는 dict(`provider`/`name`/`bd`/`has_data`); overview.html은 `c.provider`/`c.name`/`c.bd`/`c.has_data`로 접근 — 일치(Jinja2는 dict 키를 속성 접근 허용). ✓
- `_compute_burndown(provider, spent, limit, unpriced, now_kst)` 시그니처를 `burndown`·`combined_burndown` 양쪽이 동일 인자 순서로 호출 — 일치. ✓
- `active_tab` 값: overview="overview", detail=provider("claude"/"codex"); `_tabs.html` 비교문과 일치. ✓
