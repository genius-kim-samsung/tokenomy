# 토큰 구성비 + 캐시 재구축 신호 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 토큰 4종 구성비(전역 오버뷰 미니바 + 차원별 analysis 칸)와 캐시 재구축 신호(기존 `cache_miss` 재사용 insight)를 노출한다. 스키마 변경 없음.

**Architecture:** `aggregate.py`에 순수 집계 함수 `token_composition` 신설(자체 SELECT — `_range_rows`는 `output_tokens` 미포함). `insights`에 `by_day_session.cache_miss` 기반 재구축 카드(고유 `session_id` 집계, 달력 월). 표현은 `views.py`(overview만 추가, dimension은 무변경) + 템플릿 2개.

**Tech Stack:** Python(stdlib sqlite3/dataclass), FastAPI+Jinja2, pytest. 단가/스키마 무변경 → CSS 무빌드, 마이그레이션 없음.

설계 출처: `docs/superpowers/specs/2026-06-16-token-composition-cache-rebuild-design.md` (codex 리뷰 반영본 `d1990d9`).

---

### Task 1: `token_composition` 집계 함수

**Files:**
- Modify: `tokenomy/aggregate.py` (dataclass + 함수 추가, `parse_ts`·`month_bounds` 뒤 적당한 위치)
- Test: `tests/test_aggregate.py`

**주의:** `_range_rows`(134)는 `output_tokens`를 SELECT하지 않으므로 재사용 금지 → 자체 SELECT. 비중은 0~100 **퍼센트값**(94.2), `cache_ratio`(0~1)와 단위 다름. `cost_usd`는 담지 않는다.

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_aggregate.py` import 줄에 `token_composition`을 추가하고(기존 `from tokenomy.aggregate import (...)` 목록), 아래 테스트를 파일 끝에 추가:

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py::test_token_composition_shares_are_percent -v`
Expected: FAIL — `ImportError: cannot import name 'token_composition'`

- [ ] **Step 3: 구현**

`tokenomy/aggregate.py`에 추가(예: `by_dimension` 정의 부근, `parse_ts` 사용 가능 위치):

```python
@dataclass
class TokenComposition:
    """기간 내 토큰 4종 합계 + 비중(토큰량 기준, 0~100 퍼센트값). 비용은 담지 않는다(바에 비용 오해 방지)."""
    input_tokens: int
    output_tokens: int
    cache_creation: int
    cache_read: int
    total: int
    input_pct: float
    output_pct: float
    cache_creation_pct: float
    cache_read_pct: float


def token_composition(conn, provider: str | None, start, nxt) -> TokenComposition:
    """기간 [start, nxt) 내 input/output/cache_creation/cache_read 합계와 비중(%)을 반환.

    _range_rows는 output_tokens를 select하지 않아 재사용하지 않고 자체 SELECT한다.
    비중은 0~100 퍼센트값(round(x/total*100,1)) — cache_ratio(0~1)와 단위가 다르다.
    """
    sql = "SELECT ts, input_tokens, output_tokens, cache_creation, cache_read FROM messages"
    if provider is None:
        rows = conn.execute(sql).fetchall()
    else:
        rows = conn.execute(sql + " WHERE provider=?", (provider,)).fetchall()
    it = ot = cc = cr = 0
    for r in rows:
        dt = parse_ts(r["ts"])
        if not (dt and start <= dt < nxt):
            continue
        it += r["input_tokens"] or 0
        ot += r["output_tokens"] or 0
        cc += r["cache_creation"] or 0
        cr += r["cache_read"] or 0
    total = it + ot + cc + cr

    def pct(x: int) -> float:
        return round(x / total * 100, 1) if total else 0.0

    return TokenComposition(
        input_tokens=it, output_tokens=ot, cache_creation=cc, cache_read=cr,
        total=total, input_pct=pct(it), output_pct=pct(ot),
        cache_creation_pct=pct(cc), cache_read_pct=pct(cr),
    )
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k token_composition -v`
Expected: PASS (2 tests)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): token_composition — 토큰 4종 구성비(토큰량 기준)"
```

---

### Task 2: `insights`에 캐시 재구축 카드

**Files:**
- Modify: `tokenomy/aggregate.py:636` (`insights` 함수 — `by_day_session` 호출 추가)
- Test: `tests/test_aggregate.py`

**주의:** `cache_miss` 행은 (날짜×세션)이므로 **고유 `session_id`로 집계**(같은 세션 N일 miss → 1). 달력 월(`month_bounds(now_kst)`) 기준 — 기존 insights의 `_month_rows`와 동일. 시그니처 무변경.

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_aggregate.py`에 추가:

```python
def test_insights_cache_rebuild_unique_sessions():
    conn = connect(":memory:")
    # 세션 s: 6/4 첫 등장(캐시 충분), 6/6·6/7 이어짐(캐시 빈약 → 재구축, 2일)
    _insert(conn, "2026-06-04T00:00:00Z", 1.0, session="s", cache_read=1000, input_t=10)
    _insert(conn, "2026-06-06T00:00:00Z", 1.0, session="s", cache_read=0, input_t=1000)
    _insert(conn, "2026-06-07T00:00:00Z", 1.0, session="s", cache_read=0, input_t=1000)
    bd = burndown(conn, Budget(claude=0, codex=0), NOW, "claude")
    cards = insights(conn, bd, NOW, None)
    rebuild = [c for c in cards if "재구축" in c.text]
    assert len(rebuild) == 1
    assert "1개 세션" in rebuild[0].text   # 2일 miss여도 고유 세션 1


def test_insights_no_rebuild_for_first_day_only():
    conn = connect(":memory:")
    # 첫 등장일만 — 캐시 빈약해도 is_continued=False라 제외
    _insert(conn, "2026-06-06T00:00:00Z", 1.0, session="s", cache_read=0, input_t=1000)
    bd = burndown(conn, Budget(claude=0, codex=0), NOW, "claude")
    cards = insights(conn, bd, NOW, None)
    assert not any("재구축" in c.text for c in cards)
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py::test_insights_cache_rebuild_unique_sessions -v`
Expected: FAIL — `assert 0 == 1` (재구축 카드 없음)

- [ ] **Step 3: 구현**

`tokenomy/aggregate.py`의 `insights`(636) 안, `web_search` 카드 블록 다음에 추가(`if bd.unpriced_count:` 앞):

```python
    # 캐시 재구축: 이어지는 세션인데 캐시를 못 읽은(cache_miss) 고유 세션 수.
    # by_day_session이 첫 등장일을 제외(is_continued)하므로 오해 없음. 달력 월 기준.
    month_start, month_nxt = month_bounds(now_kst)
    rebuild_sessions = {
        r.session_id
        for r in by_day_session(conn, provider, start=month_start, nxt=month_nxt)
        if r.cache_miss
    }
    if rebuild_sessions:
        cards.append(Insight(
            "info",
            f"캐시 재구축 {len(rebuild_sessions)}개 세션 — 이어지는 작업에서 컨텍스트 재빌드(세션 유지로 개선 여지)",
        ))
```

(`by_day_session`·`month_bounds`는 같은 모듈에 이미 정의돼 있어 import 불필요.)

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k "insights" -v`
Expected: PASS (재구축 2건 포함, 기존 insights 테스트 회귀 없음)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): insights에 캐시 재구축 카드(고유 세션, 달력 월)"
```

---

### Task 3: analysis.html `cache_wr` 칸

**Files:**
- Modify: `tokenomy/web/templates/analysis.html` (테이블 헤더/행/빈 상태 colspan)
- Test: `tests/test_web.py`

**주의:** `views.dimension_context`의 table dict는 이미 `cache_creation`을 담고 있다(`views.py:148`) → 템플릿만. 헤더·행·colspan 3곳 모두 8→9.

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_web.py`에 추가:

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py::test_analysis_cache_wr_column -v`
Expected: FAIL — `assert 'cache_wr' in r.text` (없음)

- [ ] **Step 3: 구현**

`tokenomy/web/templates/analysis.html`의 테이블에서:

헤더 — `<th>output</th>` 다음에 `<th>cache_wr</th>` 추가:
```html
    <thead><tr><th>비용</th><th>비중</th><th>세션</th><th>캐시%</th><th>input</th><th>output</th><th>cache_wr</th><th>cache_rd</th><th>{{ dim_label }}</th></tr></thead>
```

행 — output `<td>` 다음에 cache_creation `<td>` 추가:
```html
      <tr><td>${{ '%.2f'|format(m.cost) }}</td><td>{{ '%.1f'|format(m.share) }}%</td>
          <td>{{ m.sessions }}</td><td>{{ '%.0f'|format(m.cache_ratio * 100) }}%</td>
          <td>{{ '{:,}'.format(m.input_tokens) }}</td><td>{{ '{:,}'.format(m.output_tokens) }}</td>
          <td>{{ '{:,}'.format(m.cache_creation) }}</td><td>{{ '{:,}'.format(m.cache_read) }}</td><td>{{ m.key }}</td></tr>
```

빈 상태 — `colspan="8"` → `colspan="9"`:
```html
      {% else %}<tr><td colspan="9" class="muted">이 기간 데이터 없음</td></tr>{% endfor %}
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py::test_analysis_cache_wr_column -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/templates/analysis.html tests/test_web.py
git commit -m "feat(web): analysis 차원 테이블에 cache_wr 칸(토큰 4분할)"
```

---

### Task 4: 오버뷰 전역 토큰 구성 미니바

**Files:**
- Modify: `tokenomy/web/views.py` (import + `overview_context`에 `token_comp` 추가)
- Modify: `tokenomy/web/templates/overview.html` (효율 코치 섹션 앞에 토큰 구성 카드)
- Test: `tests/test_web.py`

**주의:** 바에는 토큰량 비중만. `cost_usd`를 바에 붙이지 않는다(비용% 오해 방지). 비중은 퍼센트값(템플릿서 ×100 금지).

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_web.py`에 추가:

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py::test_dashboard_token_composition -v`
Expected: FAIL — `assert '토큰 구성' in r.text` (없음)

- [ ] **Step 3: 구현 (views.py)**

`tokenomy/web/views.py` 상단 import에서 `token_composition`과 `month_bounds`를 추가:
```python
from tokenomy.aggregate import (
    KST, DIM_COLUMNS, DateGroup, DaySessionRow, FolderGroup, burndown,
    by_day_session, by_dimension, by_project, by_session, codex_burndown,
    daily_series, insights, month_bounds, period_bounds, session_detail,
    sidechain_split, stacked_trend, token_composition,
)
```

`overview_context`(35) 안, `return {` 직전에 추가:
```python
    token_comp = token_composition(conn, None, *month_bounds(now))
```

그리고 반환 dict에 한 줄 추가(예: `"has_data": has_data,` 앞):
```python
        "token_comp": token_comp,
```

- [ ] **Step 4: 구현 (overview.html)**

`tokenomy/web/templates/overview.html`의 `<section class="card">` "통합 효율 코치"(`<h2>통합 효율 코치</h2>`) **앞에** 추가:
```html
{% if has_data %}
<section class="card">
  <h2>토큰 구성 <span class="muted">(이번 달 · 토큰량 기준)</span></h2>
  <span class="bar" style="display:flex">
    <span class="fill" style="width: {{ token_comp.cache_read_pct }}%; background: var(--color-accent)"></span>
    <span class="fill" style="width: {{ token_comp.cache_creation_pct }}%; background: var(--color-warn)"></span>
    <span class="fill" style="width: {{ token_comp.output_pct }}%; background: var(--color-primary)"></span>
    <span class="fill" style="width: {{ token_comp.input_pct }}%; background: var(--color-muted)"></span>
  </span>
  <p class="muted">캐시읽기 {{ token_comp.cache_read_pct }}% · 캐시생성 {{ token_comp.cache_creation_pct }}% · 출력 {{ token_comp.output_pct }}% · 입력 {{ token_comp.input_pct }}%</p>
  <p class="disclaimer">ⓘ 토큰 수 기준 비중 — 비용 비중과 다름(캐시 읽기 단가 0.1×, 출력 단가 높음)</p>
</section>
{% endif %}
```

- [ ] **Step 5: 통과 확인 + 전체 회귀**

Run: `.venv\Scripts\python -m pytest tests/test_web.py::test_dashboard_token_composition -v`
Expected: PASS
Run: `.venv\Scripts\python -m pytest`
Expected: 전체 PASS(기존 테스트 회귀 없음)

- [ ] **Step 6: 커밋**

```bash
git add tokenomy/web/views.py tokenomy/web/templates/overview.html tests/test_web.py
git commit -m "feat(web): 오버뷰 토큰 구성 미니바(토큰량 기준, 비용≠토큰 주석)"
```

---

## Self-Review (작성자 점검)

**Spec 커버리지:**
- analysis cache_wr 칸 → Task 3 ✅
- 오버뷰 전역 토큰 구성 미니바(토큰량 기준 + 디스클레이머) → Task 4 ✅
- 캐시 재구축 insight(고유 세션, 달력 월, cache_miss 재사용) → Task 2 ✅
- token_composition 자체 SELECT, 비중 퍼센트값, cost_usd 미포함 → Task 1 ✅
- 테스트(첫등장 제외·멀티데이 1세션·퍼센트값·렌더) → Task 1·2·3·4 ✅

**Placeholder 스캔:** 없음(모든 step에 실제 코드/명령).

**타입 일관성:** `TokenComposition` 필드(`input_pct` 등)와 템플릿 참조(`token_comp.input_pct`) 일치. `token_composition(conn, provider, start, nxt)` 시그니처가 Task 1 정의와 Task 4 호출(`*month_bounds(now)`) 일치. `insights` 시그니처 무변경(Task 2는 내부에서 `month_bounds(now_kst)` 생성).
