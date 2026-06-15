# 통합 추세 그래프 — AI별 구성 비중(스택 영역) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 대시보드 통합 추세 차트의 `누적 실제` 단일선을 AI별 누적 스택 영역으로 교체해, 합계 번다운을 유지하면서 AI별 기여분과 % 점유율을 한 그래프에서 보여준다.

**Architecture:** aggregate.py에 provider별 누적 시계열을 스택 밴드 경계값으로 바꾸는 순수 함수 `stacked_trend`를 추가하고(TDD), views.py가 provider 레지스트리로 색·라벨을 결합해 `trend_series`/`trend_totals`를 조립한다. 템플릿은 Chart.js v4의 `fill` 상대참조(축 stacking 미사용)로 영역을 쌓고 툴팁에 금액+% 점유율을 표시한다.

**Tech Stack:** Python 3(stdlib sqlite3/datetime), FastAPI+Jinja2, Chart.js v4.4.1(vendored, UMD), pytest.

---

## 배경 참고(스펙)

설계 스펙: `docs/superpowers/specs/2026-06-15-trend-ai-composition-design.md`

현재 구조(워크트리 기준 사실):
- `tokenomy/aggregate.py` — `DayPoint(day, cumulative_cost)` 데이터클래스, `daily_series(conn, provider, now_kst, *, budget_start=None)`가 provider별/합산 일별 누적 시계열을 반환(미래 날 `cumulative_cost=None`).
- `tokenomy/web/views.py:overview_context` — `daily = daily_series(conn, None, ...)`로 합산 시계열을 만들고 `daily_labels/daily_actual/daily_pace/daily_budget`를 컨텍스트에 넣음. `_provider_has_data(conn, provider)` 헬퍼 존재.
- `tokenomy/web/templates/_trend_chart.html` — Chart.js 라인 차트. 데이터셋 3개(`누적 실제` `#cc785c`, `예산 페이스` `#a09d96`, `월 예산` `#d4a017`).
- `tests/test_aggregate.py` — `connect(":memory:")` + `_msg(conn, **kw)`/`_insert(...)` fixture. `_NOW_STATUS = datetime(2026,6,15,tzinfo=KST)`(30일 중 15일). 기존 테스트 `test_overview_context_trend_uses_combined_budget`가 `ctx["daily_actual"]`을 검증함 → **본 작업에서 제거되므로 갱신 필요**.

## File Structure

- **Modify** `tokenomy/aggregate.py` — `stacked_trend` 순수 함수 추가(기존 `daily_series` 바로 뒤). provider별 누적→스택 밴드 경계값.
- **Modify** `tokenomy/web/views.py` — `_TREND_STYLE` 레지스트리 + `overview_context`에서 `trend_series`/`trend_totals` 조립, `daily_actual` 제거. `stacked_trend` import.
- **Modify** `tokenomy/web/templates/_trend_chart.html` — 단일 라인 → 스택 영역 datasets + 툴팁 콜백(금액+%, 합계 footer).
- **Modify** `tests/test_aggregate.py` — `stacked_trend` 단위테스트 추가, overview 추세 테스트 갱신/추가.

각 변경은 계층 경계(집계↔조립↔템플릿)를 그대로 따른다.

---

## Task 1: `stacked_trend` 순수 함수 (aggregate.py)

provider별 누적 시계열 리스트를 받아, 각 밴드의 원본 누적(`cum`)과 아래 밴드까지 더한 스택 경계(`top`)를 만든다. 미래 날(`None`)은 `top`으로 전파한다.

**Files:**
- Modify: `tokenomy/aggregate.py` (기존 `daily_series` 정의 직후, 파일 끝부분)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_aggregate.py` 상단 import에 `DayPoint, stacked_trend`를 추가한다. 현재 import 블록:

```python
from tokenomy.aggregate import (
    KST, burndown, by_day_session, by_model, by_project, by_session,
    combined_burndown, daily_series, insights, month_bounds, normalize_project,
    parse_ts, period_bounds, session_detail,
)
```

다음으로 교체:

```python
from tokenomy.aggregate import (
    DayPoint, KST, burndown, by_day_session, by_model, by_project, by_session,
    combined_burndown, daily_series, insights, month_bounds, normalize_project,
    parse_ts, period_bounds, session_detail, stacked_trend,
)
```

그리고 파일 끝에 테스트 추가:

```python
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k stacked_trend -v`
Expected: FAIL — `ImportError: cannot import name 'stacked_trend'`

- [ ] **Step 3: 최소 구현**

`tokenomy/aggregate.py`에서 `daily_series` 함수 정의가 끝나는 줄(현재 파일의 `return out` 다음, 공백 줄 뒤)에 추가:

```python
def stacked_trend(
    per_provider: list[tuple[str, list[DayPoint]]],
) -> list[dict]:
    """provider별 누적 시계열을 스택 밴드 경계값으로 변환.

    per_provider: [(provider, [DayPoint, …]), …] — 모든 리스트가 같은 길이·날짜 정렬
        (동일 now_kst/budget_start로 만든 daily_series라 보장됨).
    반환: [{"provider": str, "cum": [float|None], "top": [float|None]}, …]
        - cum = 그 provider의 원본 누적(툴팁 표시·% 분모용)
        - top = 아래 밴드까지 더한 running sum(차트 fill 경계용)
        - 어떤 날 cum 또는 아래 밴드 top이 None이면 그 날 top도 None(미래 끊김 전파).
    """
    out: list[dict] = []
    running: list | None = None   # 직전(아래) 밴드의 top 배열
    for provider, points in per_provider:
        cum = [p.cumulative_cost for p in points]
        if running is None:
            top = list(cum)
        else:
            top = [
                None if c is None or r is None else round(r + c, 4)
                for c, r in zip(cum, running)
            ]
        out.append({"provider": provider, "cum": cum, "top": top})
        running = top
    return out
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k stacked_trend -v`
Expected: PASS (4 passed)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): provider별 누적→스택 밴드 stacked_trend 추가"
```

---

## Task 2: overview 컨텍스트 조립 (views.py)

provider 레지스트리(색·라벨)로 데이터 있는 provider만 스택 순서대로 모아 `trend_series`/`trend_totals`를 만들고, 더는 안 쓰는 `daily_actual`을 제거한다.

**Files:**
- Modify: `tokenomy/web/views.py` (import, `_TREND_STYLE` 추가, `overview_context`)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: 실패/갱신 테스트 작성**

(1) 기존 테스트 `test_overview_context_trend_uses_combined_budget`에서 `daily_actual`을 검증하는 블록을 교체한다. 현재(워크트리 사실):

```python
    # 실제선: 6/5(도입 전) 제외, 오늘 이후 None
    assert ctx["daily_actual"][0] == 0.0        # 6/12 지출 없음
    assert ctx["daily_actual"][1] == 10.0       # 6/13
    assert ctx["daily_actual"][-1] is None      # 6/30(미래)
```

다음으로 교체:

```python
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
```

(2) 두 provider 스택을 검증하는 새 테스트를 파일 끝에 추가:

```python
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k "trend_uses_combined_budget or trend_series_stacks" -v`
Expected: FAIL — `KeyError: 'trend_series'` / `assert 'daily_actual' not in ctx`(아직 존재).

- [ ] **Step 3: 구현 — import**

`tokenomy/web/views.py` 상단 import 블록. 현재:

```python
from tokenomy.aggregate import (
    KST, DateGroup, DaySessionRow, FolderGroup, burndown, by_day_session,
    by_model, by_project, by_session, codex_burndown, daily_series,
    insights, period_bounds, session_detail,
)
```

다음으로 교체:

```python
from tokenomy.aggregate import (
    KST, DateGroup, DaySessionRow, FolderGroup, burndown, by_day_session,
    by_model, by_project, by_session, codex_burndown, daily_series,
    insights, period_bounds, session_detail, stacked_trend,
)
```

- [ ] **Step 4: 구현 — 레지스트리 추가**

`_SORT_KEYS = {...}` 정의 바로 뒤에 추가:

```python
# 통합 추세 스택 영역 — provider별 (라벨, 선 색, 채움 색[반투명]).
# 스택 순서 = 등록 순서(아래→위). 신규 provider는 여기 한 줄만 추가하면 밴드가 자동 생성된다.
_TREND_STYLE: dict[str, tuple[str, str, str]] = {
    "claude": ("Claude", "#cc785c", "rgba(204,120,92,0.5)"),   # 코랄(기존 누적선 색 유지)
    "codex": ("Codex", "#5db8a6", "rgba(93,184,166,0.5)"),     # teal(DESIGN.md accent-teal)
}
```

- [ ] **Step 5: 구현 — 조립**

`overview_context` 안에서 `daily = daily_series(conn, None, now, budget_start=bs)` 줄 바로 뒤에 추가:

```python
    # 통합 추세: provider별 누적을 스택 밴드로. 데이터 있는 provider만 등록 순서대로.
    trend_providers = [p for p in _TREND_STYLE if _provider_has_data(conn, p)]
    bands = stacked_trend(
        [(p, daily_series(conn, p, now, budget_start=bs)) for p in trend_providers]
    )
    trend_series = [
        {"label": _TREND_STYLE[b["provider"]][0],
         "color": _TREND_STYLE[b["provider"]][1],
         "fill": _TREND_STYLE[b["provider"]][2],
         "top": b["top"], "cum": b["cum"]}
        for b in bands
    ]
    trend_totals = bands[-1]["top"] if bands else [None for _ in daily]
```

이어서 반환 dict에서 다음 줄을 제거:

```python
        "daily_actual": [p.cumulative_cost for p in daily],
```

같은 자리에 추가(`"daily_labels"` 줄 뒤):

```python
        "trend_series": trend_series,
        "trend_totals": trend_totals,
```

(`daily_labels`, `daily_pace`, `daily_budget`는 그대로 유지 — `daily` 길이/라벨에 계속 의존.)

- [ ] **Step 6: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k "trend_uses_combined_budget or trend_series_stacks" -v`
Expected: PASS (2 passed)

- [ ] **Step 7: 전체 회귀 확인**

Run: `.venv\Scripts\python -m pytest -q`
Expected: 전부 PASS(포트 점유로 `test_launcher`의 포트 테스트가 실패하면 그것은 환경 문제 — 본 변경과 무관).

- [ ] **Step 8: 커밋**

```bash
git add tokenomy/web/views.py tests/test_aggregate.py
git commit -m "feat(views): 통합 추세 trend_series/trend_totals 조립, daily_actual 제거"
```

---

## Task 3: 스택 영역 차트 렌더링 (_trend_chart.html)

단일 라인을 스택 영역으로 바꾸고, 툴팁에 provider별 금액+% 점유율과 합계를 띄운다. 축 stacking은 쓰지 않고 서버 사전합산 `top` + `fill` 상대참조로 그린다(기준선 가독성 보존).

**Files:**
- Modify: `tokenomy/web/templates/_trend_chart.html` (전체 교체)

- [ ] **Step 1: 템플릿 전체 교체**

`tokenomy/web/templates/_trend_chart.html` 전체를 다음으로 교체:

```html
{% if has_data %}
<script src="/static/vendor/chart.min.js"></script>
<script>
  const trendLabels = {{ daily_labels|tojson }};
  const trendSeries = {{ trend_series|tojson }};   // [{label,color,fill,top,cum}] 아래→위
  const trendTotals = {{ trend_totals|tojson }};    // 그날 합계(=맨 위 밴드 top)
  const trendPace   = {{ daily_pace|tojson }};
  const trendBudget = {{ daily_budget|tojson }};

  // provider 밴드: top을 데이터로, fill 상대참조로 아래 밴드까지 채움
  const areaSets = trendSeries.map((s, i) => ({
    label: s.label,
    data: s.top,
    cum: s.cum,                          // 툴팁용 raw 누적(병렬 배열)
    borderColor: s.color,
    backgroundColor: s.fill,
    fill: i === 0 ? 'origin' : '-1',
    tension: .2,
    pointRadius: 0,
  }));

  new Chart(document.getElementById('trend'), {
    type: 'line',
    data: { labels: trendLabels, datasets: [
      ...areaSets,
      { label: '예산 페이스', data: trendPace, borderColor: '#a09d96', borderDash: [5,4], pointRadius: 0, fill: false },
      { label: '월 예산', data: trendBudget, borderColor: '#d4a017', borderDash: [2,2], pointRadius: 0, fill: false },
    ]},
    options: {
      plugins: {
        legend: { labels: { color: '#faf9f5' } },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const ds = ctx.dataset;
              if (ds.cum) {                              // provider 밴드 → 금액 + % 점유율
                const v = ds.cum[ctx.dataIndex];
                if (v == null) return null;
                const total = trendTotals[ctx.dataIndex] || 0;
                const pct = total ? Math.round(v / total * 100) : 0;
                return `${ds.label}  $${v.toFixed(2)} (${pct}%)`;
              }
              const y = ctx.parsed.y;                    // 기준선(페이스/예산)
              return y == null ? null : `${ds.label}  $${y.toFixed(2)}`;
            },
            footer: (items) => {
              const t = trendTotals[items[0].dataIndex];
              return t == null ? '' : `합계  $${t.toFixed(2)}`;
            },
          },
        },
      },
      scales: { x: { ticks: { color: '#a09d96' } }, y: { ticks: { color: '#a09d96' } } }
    }
  });
</script>
{% endif %}
```

- [ ] **Step 2: 템플릿 문법 점검(렌더 가능 여부)**

Run: `.venv\Scripts\python -c "from jinja2 import Environment, FileSystemLoader; Environment(loader=FileSystemLoader('tokenomy/web/templates')).get_template('_trend_chart.html'); print('OK')"`
Expected: `OK` (Jinja2 구문 오류 없음)

- [ ] **Step 3: 커밋**

```bash
git add tokenomy/web/templates/_trend_chart.html
git commit -m "feat(web): 통합 추세 차트를 AI별 스택 영역+툴팁 %로 교체"
```

---

## Task 4: 수동 시각 확인(선택) 및 마무리

JS 렌더는 단위테스트 대상이 아니므로 개발 서버로 눈으로 확인한다. 자동 게이트는 Task 2의 pytest다.

**Files:** 없음(확인만)

- [ ] **Step 1: 개발 서버 실행**

Run: `.venv\Scripts\python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765`
(포트 점유 시 `--port 8770` 등으로 변경)

- [ ] **Step 2: 대시보드 확인**

`http://127.0.0.1:8765/` 접속 → "통합 추세" 카드에서 확인:
- Claude(코랄)·Codex(teal) 밴드가 쌓이고 맨 윗선이 합계.
- 점선 `예산 페이스`·`월 예산`이 영역 위에 또렷이 보임.
- 점에 마우스 올리면 `Claude $X (NN%)` / `Codex $X (NN%)` / `합계 $X` 표시.
- 한쪽 AI만 데이터 있으면 밴드 1개만, 100%로 표시.

(브라우저 자동화가 필요하면 사용자 워크플로상 gstack `/browse` 스킬 사용.)

- [ ] **Step 3: 전체 테스트 최종 확인**

Run: `.venv\Scripts\python -m pytest -q`
Expected: PASS(환경성 `test_launcher` 포트 실패 제외).

---

## Self-Review 결과

- **스펙 커버리지:** 합계 보존 스택(Task 1·2·3) / 밴드 두께 기여분(Task 3 fill) / 툴팁 금액+%(Task 3) / N-provider 레지스트리(Task 2 `_TREND_STYLE`) / `daily_actual` 제거(Task 2) / 엣지(한쪽 AI=밴드1, 예산 미설정=기준선0, has_data False=미렌더) — 모두 태스크에 매핑됨. 비목표(토글·100% 정규화)는 미구현으로 일치.
- **플레이스홀더:** 없음(모든 step에 실제 코드/명령/기대출력).
- **타입 일관성:** `stacked_trend` 반환 dict 키 `provider/cum/top` ↔ views `_TREND_STYLE[b["provider"]]`·`b["cum"]`·`b["top"]` ↔ 템플릿 `s.label/s.color/s.fill/s.top/s.cum`·`trend_totals` 일치. `DayPoint(day, cumulative_cost)` 위치 인자 일치.
- **회귀:** `daily_actual` 제거로 깨지는 기존 테스트(`test_overview_context_trend_uses_combined_budget`)를 Task 2 Step 1에서 갱신.
