# 통합 추세 그래프 예산 도입일 정합 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 대시보드 통합 추세 그래프를 예산 도입일(`budget_start`)로 clamp하고 x축을 말일까지 확장하며, 페이스선·월 예산 가로선을 통합 예산(`budget.total`) 기준으로 정합시킨다.

**Architecture:** `daily_series`(집계)를 번다운 카드와 동일하게 `budget_start` 인자로 파라미터화해 내부에서 `effective_month_start`로 기간을 clamp한다. x축은 기간 시작~말일까지 모든 날을 포함하되 오늘 이후 실제 누적값은 `None`(차트에서 선이 끊김)으로 둔다. `views.overview_context`는 페이스선·가로선을 `budget.total`(Claude+Codex 월 예산) 기준으로 계산하고, 템플릿에 월 예산 가로선 데이터셋을 추가한다.

**Tech Stack:** Python(stdlib: datetime/sqlite3), FastAPI+Jinja2, Chart.js(vendored), pytest.

---

## File Structure

| 파일 | 책임 | 변경 |
|------|------|------|
| `tokenomy/aggregate.py` | 집계 — `DayPoint`, `daily_series` | `DayPoint.cumulative_cost` 타입 확장, `daily_series` clamp+말일 확장 |
| `tokenomy/web/views.py` | DB→화면 dict 조립 — `overview_context` | `daily_series`에 `budget_start` 전달, `daily_pace`/`daily_budget`를 `budget.total` 기준으로 |
| `tokenomy/web/templates/_trend_chart.html` | 추세 차트 렌더 | `월 예산` 가로선 데이터셋 추가 |
| `tests/test_aggregate.py` | 집계+views 단위 테스트 | `daily_series` 신규/갱신 테스트, `overview_context` 추세 테스트 |
| `tests/test_web.py` | 웹 통합 테스트 | `test_trend_data_embedded`에 가로선 검증 추가 |

**비고:** 템플릿 변경은 JS 데이터셋 추가뿐(새 CSS 클래스 없음) → `build_css.ps1` 재빌드 불필요.

---

## Task 1: `daily_series` — 도입일 clamp + 말일까지 확장 + 미래 None

**Files:**
- Modify: `tokenomy/aggregate.py` (`DayPoint` 약 555–558행, `daily_series` 약 561–573행)
- Test: `tests/test_aggregate.py` (신규 `test_daily_series_clamps_to_budget_start`, 기존 `test_daily_series_cumulative` 약 305–314행 갱신)

- [ ] **Step 1: 실패하는 신규 테스트 작성**

`tests/test_aggregate.py`의 기존 `test_daily_series_cumulative`(약 305행) **바로 위 또는 아래**에 추가. `_insert`(positional ts, cost; provider 기본 "claude")·`KST`는 이미 import됨.

```python
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
```

- [ ] **Step 2: 테스트 실행 → 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py::test_daily_series_clamps_to_budget_start -v`
Expected: FAIL — `TypeError: daily_series() got an unexpected keyword argument 'budget_start'`

- [ ] **Step 3: `DayPoint` 타입 확장 + `daily_series` 구현**

`tokenomy/aggregate.py`의 `DayPoint`(약 555행) `cumulative_cost` 타입을 `float | None`로:

```python
@dataclass
class DayPoint:
    day: int
    cumulative_cost: float | None   # 미래(오늘 이후) 구간은 None → 차트에서 선이 끊김
```

같은 파일 `daily_series`(약 561행)를 통째로 교체:

```python
def daily_series(conn, provider: str | None, now_kst: datetime,
                 *, budget_start: datetime | None = None) -> list[DayPoint]:
    """일별 누적 비용 시계열. 기간 [effective_month_start, 말일].

    실제 누적값은 오늘까지만 채우고 이후 날은 None(미래 구간 — 차트에서 선이 끊김).
    budget_start로 도입일을 clamp한다(번다운 카드와 동일). 미지정 시 달력 월 1일(하위호환).
    """
    period_start = effective_month_start(now_kst, budget_start)
    _, period_end = month_bounds(now_kst)
    last_day = (period_end - timedelta(days=1)).day
    rows = _range_rows(conn, provider, period_start, period_end)
    per_day: dict = {}
    for r in rows:
        dt = parse_ts(r["ts"])
        if dt:
            per_day[dt.day] = per_day.get(dt.day, 0.0) + (r["cost_usd"] or 0)
    out: list[DayPoint] = []
    cumulative = 0.0
    for d in range(period_start.day, last_day + 1):
        if d <= now_kst.day:
            cumulative += per_day.get(d, 0.0)
            out.append(DayPoint(day=d, cumulative_cost=round(cumulative, 4)))
        else:
            out.append(DayPoint(day=d, cumulative_cost=None))
    return out
```

`effective_month_start`·`month_bounds`·`timedelta`·`_range_rows`·`parse_ts`는 모두 같은 모듈에 이미 존재(추가 import 불필요).

- [ ] **Step 4: 기존 `test_daily_series_cumulative` 갱신(말일 확장 + None 꼬리)**

기존 본문(약 305–314행)을 아래로 교체. `_msg`·`_NOW_STATUS`(=6/15)는 이미 정의됨.

```python
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
```

- [ ] **Step 5: 두 테스트 실행 → 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py::test_daily_series_clamps_to_budget_start tests/test_aggregate.py::test_daily_series_cumulative -v`
Expected: PASS (2 passed)

- [ ] **Step 6: 커밋**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): daily_series 도입일 clamp + 말일까지 확장(미래 None)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `overview_context` — 통합 예산 기준 페이스 + 가로선

**Files:**
- Modify: `tokenomy/web/views.py` (`overview_context` 약 43행, 58–61행)
- Test: `tests/test_aggregate.py` (신규 `test_overview_context_trend_uses_combined_budget`)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_aggregate.py`의 `test_overview_context_applies_budget_start`(약 413행) **아래**에 추가(같은 config 주입 패턴 재사용).

```python
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
    # 실제선: 6/5(도입 전) 제외, 오늘 이후 None
    assert ctx["daily_actual"][0] == 0.0        # 6/12 지출 없음
    assert ctx["daily_actual"][1] == 10.0       # 6/13
    assert ctx["daily_actual"][-1] is None      # 6/30(미래)
    # 페이스선·가로선: 통합 예산(100+40=140) 기준, 말일에 수렴
    assert ctx["daily_pace"][-1] == 140.0
    assert ctx["daily_budget"] == [140.0] * 19
```

- [ ] **Step 2: 테스트 실행 → 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py::test_overview_context_trend_uses_combined_budget -v`
Expected: FAIL — `KeyError: 'daily_budget'` (아직 없음), 또는 `daily_pace[-1]`이 claude 한도(100) 기준이라 140 불일치.

- [ ] **Step 3: `overview_context` 구현 변경**

`tokenomy/web/views.py`에서 `daily = daily_series(conn, None, now)`(약 43행)을 교체:

```python
    daily = daily_series(conn, None, now, budget_start=bs)
```

이어서 추세 컨텍스트 4개 키(약 58–61행)를 교체:

```python
        "daily_labels": [p.day for p in daily],
        "daily_actual": [p.cumulative_cost for p in daily],
        # 추세 기준 = 통합 월 예산(Claude+Codex). 페이스선 0→limit(말일에 예산 도달),
        # 가로선 = 예산 천장. 둘이 말일에서 수렴. 분모는 clamp된 기간 일수(len(daily)).
        "daily_pace": [round(budget.total / len(daily) * (i + 1), 4) if budget.total else 0.0
                       for i, p in enumerate(daily)],
        "daily_budget": [budget.total if budget.total else 0.0 for p in daily],
```

`budget`(약 30행 `budget_from_config(config)`)·`bs`(약 31행 `budget_start_kst(config)`)는 이미 함수 내에 존재. `daily`는 항상 1개 이상 원소(`len(daily) >= 1`)라 0 나눗셈 없음.

- [ ] **Step 4: 테스트 실행 → 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py::test_overview_context_trend_uses_combined_budget tests/test_aggregate.py::test_overview_context_shape -v`
Expected: PASS (2 passed) — 기존 shape 테스트도 그대로 통과(추세 키 존재만 검사).

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/views.py tests/test_aggregate.py
git commit -m "feat(views): 추세 페이스선·월 예산 가로선을 통합 예산(budget.total) 기준으로

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: 템플릿 — 월 예산 가로선 데이터셋 추가

**Files:**
- Modify: `tokenomy/web/templates/_trend_chart.html` (전체 약 1–18행)
- Test: `tests/test_web.py` (`test_trend_data_embedded` 약 93–103행 갱신)

- [ ] **Step 1: 웹 테스트 갱신(가로선 검증)**

`tests/test_web.py`의 `test_trend_data_embedded`(약 93행) 본문 마지막 두 assert 아래에 추가:

```python
    r = client.get("/")
    assert "/static/vendor/chart.min.js" in r.text
    assert "trendActual" in r.text          # 임베드된 데이터 변수
    assert "trendBudget" in r.text          # 월 예산 가로선 데이터
    assert "월 예산" in r.text               # 가로선 레이블
```

- [ ] **Step 2: 테스트 실행 → 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py::test_trend_data_embedded -v`
Expected: FAIL — `assert "trendBudget" in r.text`(템플릿에 아직 없음)

- [ ] **Step 3: 템플릿 구현**

`tokenomy/web/templates/_trend_chart.html` 전체를 교체:

```html
{% if has_data %}
<script src="/static/vendor/chart.min.js"></script>
<script>
  const trendLabels = {{ daily_labels|tojson }};
  const trendActual = {{ daily_actual|tojson }};
  const trendPace   = {{ daily_pace|tojson }};
  const trendBudget = {{ daily_budget|tojson }};
  new Chart(document.getElementById('trend'), {
    type: 'line',
    data: { labels: trendLabels, datasets: [
      { label: '누적 실제', data: trendActual, borderColor: '#cc785c', backgroundColor: '#cc785c', tension: .2 },
      { label: '예산 페이스', data: trendPace, borderColor: '#a09d96', borderDash: [5,4], pointRadius: 0 },
      { label: '월 예산', data: trendBudget, borderColor: '#b9472e', borderDash: [2,2], pointRadius: 0 },
    ]},
    options: { plugins:{ legend:{ labels:{ color:'#faf9f5' } } },
      scales:{ x:{ ticks:{ color:'#a09d96' } }, y:{ ticks:{ color:'#a09d96' } } } }
  });
</script>
{% endif %}
```

`#b9472e` = 한도 천장용 warm 빨강(실제 `#cc785c`·페이스 `#a09d96`와 구분). 실제선의 `null` 꼬리는 Chart.js 기본(`spanGaps:false`)으로 자동 끊김.

- [ ] **Step 4: 테스트 실행 → 통과 + 전체 회귀**

Run: `.venv\Scripts\python -m pytest tests/test_web.py::test_trend_data_embedded -v`
Expected: PASS

Run(전체): `.venv\Scripts\python -m pytest`
Expected: 전체 PASS(기존 회귀 없음).

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/templates/_trend_chart.html tests/test_web.py
git commit -m "feat(web): 추세 차트에 월 예산 가로선 추가

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review 결과

**Spec coverage:** 스펙의 변경 1(daily_series clamp+말일+None)=Task 1, 변경 2(통합 예산 페이스+가로선)=Task 2, 변경 3(템플릿 데이터셋)=Task 3. 하위호환(`budget_start` 미설정)=Task 1 갱신 테스트, 엣지(예산 0)=Task 2 `if budget.total else 0.0`. 모두 커버.

**Placeholder scan:** 없음. 모든 스텝에 실제 코드·명령·기대출력 명시. 가로선 색은 구체값(`#b9472e`).

**Type consistency:** `daily_series(conn, provider, now_kst, *, budget_start=None)` 시그니처가 Task 1 정의·Task 2 호출에서 일치. `DayPoint.cumulative_cost: float | None`가 Task 1 구현·테스트(`is None`)·Task 2(`daily_actual[-1] is None`)·Task 3(`tojson`→`null`)에서 일관. `daily_budget` 키가 Task 2 생성·Task 3 소비(`trendBudget`)에서 일치.

## 검증 노트(수동, 선택)

전체 테스트 통과 후 앱 실행으로 육안 확인 권장:
`.venv\Scripts\python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765` → `/`
도입일 설정 시 추세선이 12일부터 시작하고, 페이스 대각선과 월 예산 가로선이 말일에서 만나는지 확인.
