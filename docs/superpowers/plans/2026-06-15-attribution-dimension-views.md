# 차원별 분석 뷰 (스킬·브랜치 귀속 + 서브에이전트 비중) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 이미 적재되지만 미노출인 `attribution_skill`·`git_branch`·`is_sidechain`을, 기존 모델별 뷰를 일반화한 `/analysis` "차원별" 화면(차원 선택기 + 서브에이전트 비중 카드)으로 노출한다.

**Architecture:** `aggregate.by_model`을 `by_dimension(dim)`으로 일반화(차원→컬럼은 고정 화이트리스트 dict, SQL 인젝션 차단)하고 `by_model`은 얇은 래퍼로 유지(회귀 가드). `views.models_context`를 `dimension_context`로 일반화하고, `app.py`에 `/analysis` 라우트를 신설·`/models`는 301 리다이렉트로 전환. 템플릿 `models.html`→`analysis.html`. 계층 분리(라우트/뷰/집계/적재) 유지, 스키마·파서 무변경.

**Tech Stack:** Python 3 / sqlite3, FastAPI + Jinja2, pytest. 프론트는 기존 Tailwind 산출 CSS(`static/app.css`)의 기존 클래스 재사용(무빌드).

**테스트 실행(중요):** 이 워크트리에는 `.venv`가 없다(gitignore). 메인 repo의 venv로, **워크트리 디렉토리에서** 실행한다(`python -m pytest`가 cwd의 `tokenomy/`를 우선 import):

```bash
cd C:/projects/tokenomy/.claude/worktrees/attr-dim-views
C:/projects/tokenomy/.venv/Scripts/python -m pytest <args>
```

아래 모든 `pytest` 명령은 이 프리픽스(`C:/projects/tokenomy/.venv/Scripts/python -m`)를 붙여 워크트리에서 실행한다.

**커밋:** 메시지 끝에 다음 트레일러를 붙인다(`-m` 두 번):
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `tokenomy/aggregate.py` | 순수 집계 | `DIM_COLUMNS`·`DimensionRow`·`by_dimension`·`SidechainSplit`·`sidechain_split` 추가, `by_model` 래퍼화 |
| `tokenomy/web/views.py` | 화면 dict 조립 | `models_context`→`dimension_context` 일반화, import 정리 |
| `tokenomy/web/app.py` | 라우팅·입력검증 | `/analysis` 신설, `/models` 301 리다이렉트, import 갱신 |
| `tokenomy/web/templates/analysis.html` | 차원별 화면 | `models.html` 개편(차원 선택기 + 서브에이전트 카드 + 동적 헤더) |
| `tokenomy/web/templates/models.html` | (제거) | `analysis.html`로 대체 |
| `tokenomy/web/templates/_sidebar.html` | 내비 | `모델별`(/models)→`차원별`(/analysis) |
| `tests/test_aggregate.py` | 집계/뷰 테스트 | `_msg` 확장, `by_dimension`·`sidechain_split`·`dimension_context` 테스트, `test_models_context_shape` 갱신 |
| `tests/test_web.py` | 라우트 테스트 | `/analysis` 테스트 추가, `/models`·나브 의존 테스트 갱신 |

---

## Task 1: `aggregate.by_dimension` — 차원 파라미터화 롤업

**Files:**
- Modify: `tokenomy/aggregate.py` (near `by_model` 496-527, `ModelUsageRow` 485-493)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: `_msg` 헬퍼에 attribution_skill·git_branch 추가**

`tests/test_aggregate.py`의 `_msg`(140-161)는 두 컬럼을 INSERT하지 않아 차원 테스트가 불가하다. INSERT 컬럼·VALUES·딕셔너리에 두 필드를 추가한다(기존 호출부는 기본 None이라 무영향).

```python
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
```

- [ ] **Step 2: 실패 테스트 작성 — `by_dimension`**

`tests/test_aggregate.py`의 `by_model` 테스트 블록(789 근처) 아래에 추가. import에 `by_dimension`을 추가한다(상단 import 블록 5-9에 `by_dimension` 추가).

```python
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
```

- [ ] **Step 3: 테스트 실패 확인**

Run: `pytest tests/test_aggregate.py -k by_dimension -v`
Expected: FAIL — `ImportError: cannot import name 'by_dimension'`.

- [ ] **Step 4: `by_dimension` + `DimensionRow` + `DIM_COLUMNS` 구현, `by_model` 래퍼화**

`tokenomy/aggregate.py`에서 기존 `ModelUsageRow`/`by_model`(485-527)을 아래로 교체한다.

```python
# 차원 키 → messages 컬럼. 사용자 입력은 이 dict의 '키'로만 받고, SQL엔 '값'(컬럼명)만 넣는다.
DIM_COLUMNS = {"model": "model", "skill": "attribution_skill", "branch": "git_branch"}


@dataclass
class DimensionRow:
    key: str | None
    cost: float
    sessions: int
    input_tokens: int
    output_tokens: int
    cache_creation: int
    cache_read: int
    cache_ratio: float


def by_dimension(conn, provider: str | None, start: datetime, nxt: datetime,
                 dim: str = "model") -> list[DimensionRow]:
    """기간 [start, nxt) 내 차원(dim) 단위 합계. 비용 내림차순.

    dim은 DIM_COLUMNS 화이트리스트 키. 빈 문자열/NULL 키는 None 버킷(미귀속)으로 접는다.
    """
    col = DIM_COLUMNS.get(dim, "model")
    sql = (f"SELECT ts, {col} AS key, cost_usd, session_id, input_tokens, output_tokens, "
           "cache_creation, cache_read FROM messages")
    if provider is None:
        rows = conn.execute(sql).fetchall()
    else:
        rows = conn.execute(sql + " WHERE provider=?", (provider,)).fetchall()
    agg: dict = {}
    for r in rows:
        dt = parse_ts(r["ts"])
        if not (dt and start <= dt < nxt):
            continue
        key = r["key"]
        if key == "":
            key = None
        a = agg.setdefault(key, {"cost": 0.0, "sessions": set(), "it": 0, "ot": 0, "cc": 0, "cr": 0})
        a["cost"] += r["cost_usd"] or 0
        a["sessions"].add(r["session_id"])
        a["it"] += r["input_tokens"] or 0
        a["ot"] += r["output_tokens"] or 0
        a["cc"] += r["cache_creation"] or 0
        a["cr"] += r["cache_read"] or 0
    out = [
        DimensionRow(
            key=k, cost=round(a["cost"], 4), sessions=len(a["sessions"]),
            input_tokens=a["it"], output_tokens=a["ot"],
            cache_creation=a["cc"], cache_read=a["cr"],
            cache_ratio=round(a["cr"] / (a["it"] + a["cc"] + a["cr"]), 4) if (a["it"] + a["cc"] + a["cr"]) else 0.0,
        )
        for k, a in agg.items()
    ]
    out.sort(key=lambda x: x.cost, reverse=True)
    return out


@dataclass
class ModelUsageRow:
    model: str | None
    cost: float
    sessions: int
    input_tokens: int
    output_tokens: int
    cache_creation: int
    cache_read: int
    cache_ratio: float


def by_model(conn, provider: str | None, start: datetime, nxt: datetime) -> list[ModelUsageRow]:
    """기간 [start, nxt) 내 모델 단위 합계(=by_dimension(dim='model')). 비용 내림차순."""
    return [
        ModelUsageRow(
            model=r.key, cost=r.cost, sessions=r.sessions,
            input_tokens=r.input_tokens, output_tokens=r.output_tokens,
            cache_creation=r.cache_creation, cache_read=r.cache_read, cache_ratio=r.cache_ratio,
        )
        for r in by_dimension(conn, provider, start, nxt, "model")
    ]
```

- [ ] **Step 5: 테스트 통과 확인 (회귀 포함)**

Run: `pytest tests/test_aggregate.py -k "by_dimension or by_model" -v`
Expected: PASS (신규 4건 + 기존 `test_by_model_*` 2건 모두).

- [ ] **Step 6: 커밋**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): by_model을 by_dimension(스킬/브랜치/모델)으로 일반화" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `aggregate.sidechain_split` — 부모 vs 서브에이전트 비중

**Files:**
- Modify: `tokenomy/aggregate.py` (Task 1 블록 바로 아래)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: 실패 테스트 작성**

상단 import에 `sidechain_split`, `SidechainSplit` 추가 후:

```python
from tokenomy.aggregate import SidechainSplit, sidechain_split  # (상단 import 블록에 통합)


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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_aggregate.py -k sidechain_split -v`
Expected: FAIL — `cannot import name 'sidechain_split'`.

- [ ] **Step 3: 구현**

`tokenomy/aggregate.py`의 Task 1 블록 아래에 추가:

```python
@dataclass
class SidechainSplit:
    parent_cost: float
    sub_cost: float
    total_cost: float
    sub_share: float        # 서브에이전트 비중 % (= sub/total*100)
    parent_tokens: int
    sub_tokens: int


def sidechain_split(conn, provider: str | None, start: datetime, nxt: datetime) -> SidechainSplit:
    """기간 [start, nxt) 내 is_sidechain 기준 부모 vs 서브에이전트 비용·토큰 분리."""
    sql = ("SELECT ts, is_sidechain, cost_usd, input_tokens, output_tokens, "
           "cache_creation, cache_read FROM messages")
    if provider is None:
        rows = conn.execute(sql).fetchall()
    else:
        rows = conn.execute(sql + " WHERE provider=?", (provider,)).fetchall()
    pc = sc = 0.0
    pt = st = 0
    for r in rows:
        dt = parse_ts(r["ts"])
        if not (dt and start <= dt < nxt):
            continue
        tok = (r["input_tokens"] or 0) + (r["output_tokens"] or 0) \
            + (r["cache_creation"] or 0) + (r["cache_read"] or 0)
        if r["is_sidechain"]:
            sc += r["cost_usd"] or 0
            st += tok
        else:
            pc += r["cost_usd"] or 0
            pt += tok
    total = pc + sc
    return SidechainSplit(
        parent_cost=round(pc, 4), sub_cost=round(sc, 4), total_cost=round(total, 4),
        sub_share=round(sc / total * 100, 1) if total else 0.0,
        parent_tokens=pt, sub_tokens=st,
    )
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_aggregate.py -k sidechain_split -v`
Expected: PASS (2건).

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): sidechain_split — 부모 vs 서브에이전트 비중" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `views.dimension_context` — 모델별 뷰 일반화

`models_context`를 `dimension_context`로 일반화한다. 이 태스크 종료 시 `/models`는 여전히 200으로 동작한다(라우트는 `dimension_context(dim="model")` 호출 + 기존 `models.html` 렌더, 행 키는 `m.key`).

**Files:**
- Modify: `tokenomy/web/views.py` (import 6-10, `models_context` 126-157)
- Modify: `tokenomy/web/app.py` (import 18-20, `/models` 라우트 107-120)
- Modify: `tokenomy/web/templates/models.html` (행 셀 `m.model`→`m.key`)
- Test: `tests/test_aggregate.py` (`test_models_context_shape` 825 갱신)

- [ ] **Step 1: 실패 테스트로 갱신 — `dimension_context`**

`tests/test_aggregate.py`의 `test_models_context_shape`(825)를 아래로 교체하고, import(12)의 `models_context`를 `dimension_context`로 바꾼다.

```python
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_aggregate.py -k dimension_context -v`
Expected: FAIL — `cannot import name 'dimension_context'`.

- [ ] **Step 3: views.py import 정리 + `dimension_context` 구현**

import 블록(6-11)을 갱신 — `by_model` 제거, `by_dimension, sidechain_split, DIM_COLUMNS` 추가:

```python
from tokenomy.aggregate import (
    KST, DIM_COLUMNS, DateGroup, DaySessionRow, FolderGroup, burndown,
    by_day_session, by_dimension, by_project, by_session, codex_burndown,
    daily_series, insights, period_bounds, session_detail, sidechain_split,
    stacked_trend,
)
```

`models_context`(126-157) 전체를 아래로 교체:

```python
DIM_LABELS = {"model": "모델", "skill": "스킬", "branch": "브랜치"}
_NULL_BUCKET = {"model": "(unknown)", "skill": "(미귀속)", "branch": "(브랜치 없음)"}


def dimension_context(conn, anchor_kst: datetime, provider: str, *,
                      dim: str = "model", now_kst: datetime | None = None,
                      period: str = "month", start: str | None = None,
                      end: str | None = None) -> dict:
    """차원별(모델/스킬/브랜치) 사용/비용 + 서브에이전트 비중. 주/월 또는 사용자 지정 구간."""
    dim = dim if dim in DIM_COLUMNS else "model"
    now = now_kst or datetime.now(KST)
    config = load_config()
    s, nxt, label, period, custom = _resolve_range(anchor_kst, period, start, end)
    rows = by_dimension(conn, provider or None, s, nxt, dim)
    total = round(sum(r.cost for r in rows), 4)
    null_label = _NULL_BUCKET[dim]
    table = [
        {"key": (r.key if r.key not in (None, "") else null_label), "cost": r.cost,
         "share": round(r.cost / total * 100, 1) if total else 0.0,
         "sessions": r.sessions, "cache_ratio": r.cache_ratio,
         "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
         "cache_creation": r.cache_creation, "cache_read": r.cache_read}
        for r in rows
    ]
    split = sidechain_split(conn, provider or None, s, nxt)
    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    return {
        "active_nav": "analysis", "user_label": user_label(config),
        "provider": provider, "dim": dim, "dim_label": DIM_LABELS[dim],
        "claude_only": dim in ("skill", "branch"), "split": split,
        "rows": table, "count": len(table), "total": total,
        "period": period, "custom": custom, "period_label": label,
        "anchor": anchor_kst.strftime("%Y-%m-%d"),
        "start": start or "", "end": end or "",
        "prev_anchor": (s - timedelta(days=1)).strftime("%Y-%m-%d"),
        "next_anchor": nxt.strftime("%Y-%m-%d"),
        "has_next": nxt <= now,
        "month": now.strftime("%Y-%m"),
        "last_ts": last["t"] if last and last["t"] else None,
    }
```

주의: `models_context`의 호출 시그니처는 위치 인자 `(conn, anchor, provider)` 뒤 키워드였다. `dimension_context`는 `dim` 등을 키워드 전용(`*`)으로 둔다 — app.py 호출도 키워드로 맞춘다(Step 4).

- [ ] **Step 4: app.py — import·`/models` 라우트를 dimension_context로 (임시: 여전히 models.html 렌더)**

import(18-20) 교체:

```python
from tokenomy.web.views import (
    dimension_context, history_context, overview_context, session_context,
)
```

`/models` 라우트(107-120)의 본문에서 `models_context(...)` 호출을 교체. 시그니처·검증은 유지하되 `dim="model"` 고정:

```python
    ctx = dimension_context(conn, _parse_anchor(anchor), provider, dim="model",
                            period=period, start=start, end=end)
    return templates.TemplateResponse(
        request, "models.html",
        {**ctx, "notice": notice, "update_tag": update_tag},
    )
```

- [ ] **Step 5: models.html — 행 키 셀을 `m.key`로**

`tokenomy/web/templates/models.html`의 마지막 셀(42행) `{{ m.model }}` → `{{ m.key }}`. (헤더 "모델"은 이 태스크에선 유지 — Task 4에서 동적화.)

```html
          <td>{{ '{:,}'.format(m.cache_read) }}</td><td>{{ m.key }}</td></tr>
```

- [ ] **Step 6: 테스트 통과 확인 (회귀 포함)**

Run: `pytest tests/test_aggregate.py -k "dimension_context" -v`
Then: `pytest tests/test_web.py -k models -v`
Expected: PASS — 신규 dimension_context 3건 + 기존 `/models` 웹 테스트(`test_models_page_ok`, `test_models_page_renders_rows`, `test_models_week_period_param`, `test_models_has_period_toggle_and_range`)가 여전히 통과(라우트·템플릿 아직 /models).

- [ ] **Step 7: 커밋**

```bash
git add tokenomy/web/views.py tokenomy/web/app.py tokenomy/web/templates/models.html tests/test_aggregate.py
git commit -m "refactor(views): models_context를 dimension_context로 일반화" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `/analysis` 화면 — 라우트·템플릿·내비 + 기존 테스트 갱신

차원 선택기·서브에이전트 카드를 노출하고 `/models`를 리다이렉트로 전환한다. 이 태스크 종료 시 기능 완성.

**Files:**
- Create: `tokenomy/web/templates/analysis.html`
- Modify: `tokenomy/web/app.py` (`/models` 라우트 → 301, `/analysis` 신설)
- Modify: `tokenomy/web/templates/_sidebar.html` (나브)
- Delete: `tokenomy/web/templates/models.html`
- Test: `tests/test_web.py`

- [ ] **Step 1: `analysis.html` 생성**

`tokenomy/web/templates/models.html`을 토대로 신규 작성. 차원 선택기(기존 `.toggle.small` 클래스 재사용)·서브에이전트 카드·동적 헤더·`claude_only` 안내 추가. 모든 링크는 현재 `dim`/`provider`/`period`를 보존한다.

```html
{% extends "base.html" %}
{% block body %}
<h1 class="page-title">차원별</h1>

<section class="card">
  <div class="card-head">
    <div class="period-nav">
      <a class="btn" href="/analysis?dim={{ dim }}&anchor={{ prev_anchor }}&provider={{ provider }}&period={{ period }}">‹ 이전</a>
      <span class="label">{{ period_label }}</span>
      {% if has_next %}<a class="btn" href="/analysis?dim={{ dim }}&anchor={{ next_anchor }}&provider={{ provider }}&period={{ period }}">다음 ›</a>{% endif %}
    </div>
    <form class="filters" method="get" action="/analysis">
      <input type="hidden" name="dim" value="{{ dim }}">
      <input type="hidden" name="anchor" value="{{ anchor }}">
      <input type="hidden" name="provider" value="{{ provider }}">
      <select name="period" aria-label="기간 단위" onchange="this.form.submit()">
        <option value="month" {{ 'selected' if period == 'month' }}>월간</option>
        <option value="week" {{ 'selected' if period == 'week' }}>주간</option>
      </select>
      <span class="range">
        <input type="date" name="start" value="{{ start }}" aria-label="시작일">
        ~
        <input type="date" name="end" value="{{ end }}" aria-label="종료일">
        <button class="btn" type="submit">조회</button>
      </span>
    </form>
    <nav class="toggle small">
      <a href="/analysis?anchor={{ anchor }}&period={{ period }}&provider={{ provider }}" class="{{ 'on' if provider == '' }}">전체</a>
      <a href="/analysis?dim={{ dim }}&anchor={{ anchor }}&period={{ period }}&provider=claude" class="{{ 'on' if provider == 'claude' }}">Claude</a>
      <a href="/analysis?dim={{ dim }}&anchor={{ anchor }}&period={{ period }}&provider=codex" class="{{ 'on' if provider == 'codex' }}">Codex</a>
    </nav>
  </div>

  <nav class="toggle small" aria-label="차원 선택">
    <a href="/analysis?dim=model&anchor={{ anchor }}&period={{ period }}&provider={{ provider }}" class="{{ 'on' if dim == 'model' }}">모델</a>
    <a href="/analysis?dim=skill&anchor={{ anchor }}&period={{ period }}&provider={{ provider }}" class="{{ 'on' if dim == 'skill' }}">스킬</a>
    <a href="/analysis?dim=branch&anchor={{ anchor }}&period={{ period }}&provider={{ provider }}" class="{{ 'on' if dim == 'branch' }}">브랜치</a>
  </nav>

  {% if split.total_cost > 0 %}
  <p class="muted">서브에이전트 비중: ${{ '%.2f'|format(split.sub_cost) }} / ${{ '%.2f'|format(split.total_cost) }} ({{ '%.1f'|format(split.sub_share) }}%) · 부모 ${{ '%.2f'|format(split.parent_cost) }}</p>
  {% endif %}

  {% if claude_only %}
  <p class="muted">ⓘ 스킬·브랜치 귀속은 Claude 로그 기준(Codex는 귀속 메타 없음 → 미귀속).</p>
  {% endif %}

  <p class="muted">{{ period_label }} · {{ dim_label }} {{ count }}개 · 합계 ${{ '%.2f'|format(total) }}</p>

  <table class="gtable">
    <thead><tr><th>비용</th><th>비중</th><th>세션</th><th>캐시%</th><th>input</th><th>output</th><th>cache_rd</th><th>{{ dim_label }}</th></tr></thead>
    <tbody>
      {% for m in rows %}
      <tr><td>${{ '%.2f'|format(m.cost) }}</td><td>{{ '%.1f'|format(m.share) }}%</td>
          <td>{{ m.sessions }}</td><td>{{ '%.0f'|format(m.cache_ratio * 100) }}%</td>
          <td>{{ '{:,}'.format(m.input_tokens) }}</td><td>{{ '{:,}'.format(m.output_tokens) }}</td>
          <td>{{ '{:,}'.format(m.cache_read) }}</td><td>{{ m.key }}</td></tr>
      {% else %}<tr><td colspan="8" class="muted">이 기간 데이터 없음</td></tr>{% endfor %}
    </tbody>
  </table>
  <p class="disclaimer">ⓘ 공개 API 단가 기준 추정 · 이 머신 데이터만</p>
</section>
{% endblock %}
```

- [ ] **Step 2: `_sidebar.html` 나브 교체**

`tokenomy/web/templates/_sidebar.html`의 모델별 줄(9)을 교체:

```html
    <a href="/analysis" class="nav-item {{ 'on' if active_nav == 'analysis' }}">차원별</a>
```

- [ ] **Step 3: app.py — `/models` 301 리다이렉트 + `/analysis` 신설**

`/models` 라우트(Task 3에서 dimension_context 호출하던 블록)를 통째로 아래로 교체. import에 `DIM_COLUMNS` 추가(`from tokenomy.aggregate import KST, DIM_COLUMNS, PROVIDERS, parse_ts`).

```python
@app.get("/models")
def models_redirect():
    return RedirectResponse("/analysis?dim=model", status_code=301)


@app.get("/analysis")
def analysis_view(request: Request, anchor: str | None = None, provider: str = "",
                  dim: str = "model", period: str | None = None,
                  start: str | None = None, end: str | None = None,
                  notice: str | None = None):
    dim = dim if dim in DIM_COLUMNS else "model"
    provider = provider if provider in PROVIDERS else ""
    period = period if period in _PERIODS else "month"
    conn = connect()
    update_tag = check_update(conn)
    ctx = dimension_context(conn, _parse_anchor(anchor), provider, dim=dim,
                            period=period, start=start, end=end)
    return templates.TemplateResponse(
        request, "analysis.html",
        {**ctx, "notice": notice, "update_tag": update_tag},
    )
```

- [ ] **Step 4: `models.html` 삭제**

```bash
git rm tokenomy/web/templates/models.html
```

- [ ] **Step 5: 기존 web 테스트 갱신 (깨지는 5건)**

`tests/test_web.py`에서 아래를 교체한다.

`test_root_renders_overview`(187-195)의 마지막 줄:

```python
    assert 'href="/analysis"' in r.text   # 나브: 모델별→차원별
```

`test_models_page_ok`(367-372) → 리다이렉트 검증으로 교체:

```python
def test_models_redirects_to_analysis(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/models", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/analysis?dim=model"
```

`test_models_page_renders_rows`(395-404) → `/analysis`로:

```python
def test_analysis_renders_rows(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,model,cost_usd,priced) "
                 "VALUES ('a','claude','s1','2026-06-10T10:00:00Z','claude-opus-4-8',12.5,1)")
    conn.commit()
    r = client.get("/analysis?anchor=2026-06-10&dim=model")
    assert r.status_code == 200
    assert "claude-opus-4-8" in r.text
    assert "합계 $12.50" in r.text
```

`test_models_week_period_param`(479-487) → `/analysis`:

```python
def test_analysis_week_period_param(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,model,cost_usd,priced) "
                 "VALUES ('a','claude','s1','2026-06-09T10:00:00Z','claude-opus-4-8',8.0,1)")
    conn.commit()
    r = client.get("/analysis?anchor=2026-06-13&period=week&dim=model")
    assert r.status_code == 200
    assert "2026-06-08 ~ 06-14" in r.text
```

`test_models_has_period_toggle_and_range`(498-503) → `/analysis`:

```python
def test_analysis_has_period_toggle_and_range(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/analysis")
    assert r.status_code == 200
    assert 'name="period"' in r.text
    assert 'name="start"' in r.text and 'name="end"' in r.text
```

- [ ] **Step 6: `/analysis` 신규 동작 테스트 추가**

`tests/test_web.py` 끝에 추가:

```python
def test_analysis_dim_selector_and_skill(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,model,cost_usd,priced,attribution_skill) "
                 "VALUES ('a','claude','s1','2026-06-10T10:00:00Z','claude-opus-4-8',5.0,1,'brainstorming')")
    conn.commit()
    r = client.get("/analysis?anchor=2026-06-10&dim=skill")
    assert r.status_code == 200
    assert "brainstorming" in r.text
    assert ">스킬</a>" in r.text                       # 차원 선택기 항목
    assert "Claude 로그 기준" in r.text                # claude_only 안내


def test_analysis_bad_dim_falls_back(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/analysis?dim=evil")
    assert r.status_code == 200                        # 화이트리스트 폴백


def test_analysis_shows_sidechain_card(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,model,cost_usd,priced,is_sidechain) "
                 "VALUES ('a','claude','s1','2026-06-10T10:00:00Z','claude-opus-4-8',8.0,1,0)")
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,model,cost_usd,priced,is_sidechain) "
                 "VALUES ('b','claude','s1','2026-06-10T11:00:00Z','claude-opus-4-8',2.0,1,1)")
    conn.commit()
    r = client.get("/analysis?anchor=2026-06-10")
    assert "서브에이전트 비중" in r.text
```

- [ ] **Step 7: 전체 web·집계 테스트 통과 확인**

Run: `pytest tests/test_web.py tests/test_aggregate.py -v`
Expected: PASS (전부). `/models`는 301, `/analysis`는 차원 선택기·서브에이전트 카드·스킬 안내 렌더.

- [ ] **Step 8: 커밋**

```bash
git add tokenomy/web/app.py tokenomy/web/templates/_sidebar.html tokenomy/web/templates/analysis.html tests/test_web.py
git commit -m "feat(web): /analysis 차원별 뷰(선택기+서브에이전트 카드), /models 리다이렉트" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: 전체 회귀 검증

**Files:** (없음 — 검증만)

- [ ] **Step 1: 전체 테스트 실행**

Run: `pytest`
Expected: 전부 PASS. (단, 메모리상 `test_launcher`의 포트 8765 점유 케이스 2건은 앱 실행 중이면 환경 문제로 실패할 수 있음 — 회귀 아님.)

- [ ] **Step 2: 라우트 스모크(선택)**

Run: `C:/projects/tokenomy/.venv/Scripts/python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8799` 후 `/analysis?dim=skill`·`/analysis?dim=branch`·`/models`(→301) 육안 확인. 확인 후 종료.

- [ ] **Step 3: 잔여 정리 확인**

`grep -rn "models_context\|by_model(" tokenomy/` 로 죽은 참조가 없는지 확인(`by_model`은 aggregate 정의 + 테스트만, `models.html`/`models_context` 참조 0).

---

## Self-Review (작성자 점검 결과)

**1. Spec coverage:**
- §3.1 by_dimension/DIM_COLUMNS → Task 1 ✓ / §3.2 sidechain_split → Task 2 ✓
- §3.3 dimension_context(fallback·split·claude_only·미귀속) → Task 3 + Task 4 Step6 ✓
- §3.4 /analysis 라우트 + /models 301 → Task 4 ✓ / §3.5 나브·템플릿·동적 헤더 → Task 4 ✓
- §3.6 미귀속 버킷 라벨·provider 가용성 안내 → Task 3(_NULL_BUCKET) + Task 4(claude_only 문구) ✓
- §6 테스트(aggregate·web) → 각 태스크 + Task 5 ✓
- 비범위(server tool/추세/라벨) → 계획에 미포함 ✓

**2. Placeholder scan:** TBD/TODO 없음. 모든 코드 단계에 실제 코드 포함.

**3. Type consistency:** `DimensionRow.key`·`SidechainSplit.sub_share`·`dimension_context`의 `dim/dim_label/claude_only/split` 키가 Task 3 정의와 Task 4 템플릿/테스트에서 일치. `by_model` 래퍼는 `ModelUsageRow`(model 필드)를 유지해 기존 `test_by_model_*`·`session_detail` 등 무영향. app.py 호출이 `dimension_context`의 키워드 전용 시그니처(`*`)와 일치(`dim=`, `period=` 등 키워드).
