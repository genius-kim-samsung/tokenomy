# 단가 커버리지 신뢰성 진단 — 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** pricing.json이 사용 모델을 얼마나 정확히 매칭하는지 진단(미식별·오매칭 의심·거친 매칭)하고, `pricing_overrides`로 새 모델 단가까지 자가 추가 가능하게 한다.

**Architecture:** 순수 함수(`aggregate.pricing_coverage`)가 모델별 토큰 집계 + 단가 매칭 상태를 산출 → settings 진단 카드(`views.coverage_card_context` + `settings.html`)·overview 경고(`insights`)·CLI `report`로 노출. 별도로 `pricing.apply_pricing_overrides`를 확장해 새 `contains` 항목 추가를 지원한다. 모두 읽기 전용 집계 + 메모리상 pricing dict 보정이라 DB 스키마·적재 파이프라인은 불변.

**Tech Stack:** Python(stdlib sqlite3/dataclasses), FastAPI + Jinja2, pytest.

**Spec:** `docs/superpowers/specs/2026-06-17-pricing-coverage-diagnostics-design.md`

**테스트 실행 주의:** 이 워크트리엔 `.venv`가 없다. 메인 repo의 인터프리터를 쓴다 —
모든 `pytest` 명령은 `C:/projects/samsung/tokenomy/.venv/Scripts/python -m pytest ...` 형태로 워크트리 cwd에서 실행한다.

---

## File Structure

- `tokenomy/pricing.py` — (수정) `_is_version_boundary` 휴리스틱 추가, `apply_pricing_overrides` 확장(새 항목 prepend).
- `tokenomy/aggregate.py` — (수정) `CoverageModel`·`CoverageReport` dataclass + `pricing_coverage` 함수 추가, `insights`에 `cov` 인자 연동.
- `tokenomy/web/views.py` — (수정) `_human_tokens`·`_share_pct` 포맷 헬퍼, `coverage_card_context` 추가, `overview_context`에서 `cov` 계산·전달.
- `tokenomy/web/app.py` — (수정) `settings_get`에서 `coverage_card_context` 병합.
- `tokenomy/web/templates/settings.html` — (수정) 단가 커버리지 카드 섹션 추가.
- `tokenomy/cli.py` — (수정) `cmd_report` 끝에 커버리지 요약 한 줄.
- `tests/test_pricing.py` — (수정) 휴리스틱 + overrides 확장 테스트.
- `tests/test_aggregate.py` — (수정) `_insert`에 `model` 인자, `pricing_coverage` 테스트.
- `tests/test_web.py` — (수정) settings 카드 렌더 테스트.
- `README.md` · `docs/ROADMAP.md` — (수정) overrides 새 모델 예시, #1 진행 표시.

---

## Task 1: 버전경계 의심 휴리스틱 (`_is_version_boundary`)

**Files:**
- Modify: `tokenomy/pricing.py` (find_rate 아래)
- Test: `tests/test_pricing.py`

- [ ] **Step 1: Write the failing test**

`tests/test_pricing.py` 상단 import에 `_is_version_boundary` 추가:

```python
from tokenomy.pricing import (
    CostResult,
    _is_version_boundary,
    apply_pricing_overrides,
    compute_cost,
    find_rate,
    load_pricing,
)
```

파일 끝에 추가:

```python
def test_version_boundary_suspect_when_digit_or_dot_follows():
    # contains 토큰 직후가 숫자/'.'이면 다음 버전 의심
    assert _is_version_boundary("gpt-5.5", "gpt-5") is True
    assert _is_version_boundary("gpt-4o", "gpt-4") is False   # 'o'는 숫자/'.' 아님
    assert _is_version_boundary("gpt-4.1", "gpt-4") is True


def test_version_boundary_safe_when_separator_or_end_follows():
    assert _is_version_boundary("claude-opus-4-8", "opus") is False  # 직후 '-'
    assert _is_version_boundary("gpt-5", "gpt-5") is False           # 직후 없음(끝)
    assert _is_version_boundary("anything", "missing") is False      # 토큰 부재
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python -m pytest tests/test_pricing.py::test_version_boundary_suspect_when_digit_or_dot_follows -v`
Expected: FAIL — `ImportError: cannot import name '_is_version_boundary'`

- [ ] **Step 3: Write minimal implementation**

`tokenomy/pricing.py`의 `find_rate` 함수 바로 아래에 추가:

```python
def _is_version_boundary(model: str, contains: str) -> bool:
    """매칭된 contains 토큰 직후 문자가 숫자나 '.'이면 버전 경계 의심.

    부분일치가 새 버전을 그럴듯하게 틀리게 잡는 경우를 추정한다
    (예: 'gpt-5' 항목이 'gpt-5.5' 모델을 가로챔). 토큰 직후가 구분자('-')거나
    문자열 끝이면 안전으로 본다(예: 'opus' → 'claude-opus-4-8').
    """
    idx = model.find(contains)
    if idx < 0:
        return False
    nxt = model[idx + len(contains): idx + len(contains) + 1]
    return nxt.isdigit() or nxt == "."
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python -m pytest tests/test_pricing.py -k version_boundary -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add tokenomy/pricing.py tests/test_pricing.py
git commit -m "feat(pricing): 버전경계 의심 휴리스틱 _is_version_boundary"
```

---

## Task 2: overrides 확장 — 새 모델 항목 추가 (`apply_pricing_overrides`)

**Files:**
- Modify: `tokenomy/pricing.py:75-88` (`apply_pricing_overrides`)
- Test: `tests/test_pricing.py`

- [ ] **Step 1: Write the failing test**

`tests/test_pricing.py` 끝에 추가:

```python
def test_apply_overrides_adds_new_model_prepended():
    # 기존에 없는 contains 키 → 새 항목으로 추가, 기존보다 앞(prepend)
    pricing = {"match": [
        {"contains": "gpt-5", "provider": "codex", "input": 1.25, "output": 10.0,
         "cache_write": 0.0, "cache_read": 0.125},
    ]}
    out = apply_pricing_overrides(pricing, {
        "gpt-5.5": {"provider": "codex", "input": 2.0, "output": 12.0, "cache_read": 0.2},
    })
    assert out["match"][0]["contains"] == "gpt-5.5"   # prepend
    # 새 항목이 먼저 매칭되어 gpt-5.5가 정확 단가로 잡힌다
    assert find_rate("gpt-5.5", out)["input"] == 2.0
    # 누락 필드는 0.0
    assert out["match"][0]["cache_write"] == 0.0


def test_apply_overrides_new_model_priced_via_compute_cost():
    pricing = {"match": [
        {"contains": "opus", "provider": "claude", "input": 15.0, "output": 75.0,
         "cache_write": 18.75, "cache_read": 1.50},
    ]}
    out = apply_pricing_overrides(pricing, {
        "gpt-5.5": {"provider": "codex", "input": 2.0, "output": 12.0},
    })
    r = compute_cost(_rec("gpt-5.5", output_tokens=1_000_000), out)
    assert r.priced is True
    assert r.cost_usd == 12.0
    assert r.provider == "codex"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python -m pytest tests/test_pricing.py::test_apply_overrides_adds_new_model_prepended -v`
Expected: FAIL — `IndexError`/`KeyError` 또는 `out["match"][0]["contains"] == "gpt-5"` (prepend 미구현)

- [ ] **Step 3: Write minimal implementation**

`tokenomy/pricing.py`의 `apply_pricing_overrides`를 교체:

```python
def apply_pricing_overrides(pricing: dict, overrides: dict | None) -> dict:
    """pricing_overrides({contains: {input/output/...[/provider]}})로 match[]를 보정한다.

    기존 contains 항목은 지정 단가 필드만 교체(미지정 필드 보존). 기존에 없는
    contains 키는 새 항목으로 만들어 match[] 앞에 prepend한다 — find_rate가
    위에서부터 첫 부분일치를 쓰므로, 더 구체적인 사용자 항목이 기존 거친 항목보다
    먼저 매칭된다(예: 'gpt-5.5'가 'gpt-5'를 앞선다). 누락 단가 필드는 0.0.
    """
    if not overrides:
        return pricing
    existing = {e.get("contains") for e in pricing.get("match", [])}
    for entry in pricing.get("match", []):
        ov = overrides.get(entry.get("contains"))
        if ov:
            for k in _OVERRIDABLE:
                if k in ov:
                    entry[k] = ov[k]
    new = [
        {"contains": contains, "provider": ov.get("provider"),
         "input": ov.get("input", 0.0), "output": ov.get("output", 0.0),
         "cache_write": ov.get("cache_write", 0.0), "cache_read": ov.get("cache_read", 0.0)}
        for contains, ov in overrides.items() if contains not in existing
    ]
    pricing["match"] = new + pricing.get("match", [])
    return pricing
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python -m pytest tests/test_pricing.py -v`
Expected: PASS (기존 overrides 테스트 + 신규 2건 모두 통과 — 회귀 없음)

- [ ] **Step 5: Commit**

```bash
git add tokenomy/pricing.py tests/test_pricing.py
git commit -m "feat(pricing): overrides가 새 모델 항목 추가(prepend) 지원"
```

---

## Task 3: 진단 집계 (`pricing_coverage`)

**Files:**
- Modify: `tokenomy/aggregate.py` (Insight dataclass 근처에 추가)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: `_insert` 헬퍼에 model 인자 추가 (테스트 준비)**

`tests/test_aggregate.py:19-27`의 `_insert`를 교체(기존 호출은 default로 동일 동작 — 회귀 없음):

```python
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
```

- [ ] **Step 2: Write the failing test**

`tests/test_aggregate.py` import에 추가(기존 import 줄에 `pricing_coverage`, `CoverageReport` 합류):

```python
from tokenomy.aggregate import (
    DayPoint, KST, burndown, by_day_session, by_dimension, by_model, by_project, by_session,
    combined_burndown, daily_series, insights, month_bounds, normalize_project,
    parse_ts, period_bounds, session_detail, sidechain_split, SidechainSplit, stacked_trend,
    token_composition, pricing_coverage, CoverageReport,
)
```

파일 끝에 추가:

```python
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python -m pytest tests/test_aggregate.py::test_pricing_coverage_empty_db_safe -v`
Expected: FAIL — `ImportError: cannot import name 'pricing_coverage'`

- [ ] **Step 4: Write minimal implementation**

`tokenomy/aggregate.py` 상단 import에 `find_rate`, `_is_version_boundary` 추가:

```python
from tokenomy.pricing import find_rate, _is_version_boundary
```

`Insight` dataclass(`aggregate.py:676-679` 근처) 위 또는 아래에 추가:

```python
@dataclass
class CoverageModel:
    provider: str
    model: str | None
    matched_contains: str | None   # 매칭된 pricing 항목의 contains. None이면 미식별
    status: str                    # "ok" | "suspect" | "unpriced"
    tokens: int
    token_share: float


@dataclass
class CoverageReport:
    models: list[CoverageModel]    # 토큰 내림차순
    total_tokens: int
    unpriced_count: int            # status=="unpriced" 모델 종 수(메시지 건수 아님)
    unpriced_token_share: float
    suspect_count: int
    coarse_contains: list[str]     # 2개 이상 distinct 모델이 매칭된 contains


def pricing_coverage(conn, pricing: dict) -> CoverageReport:
    """distinct (provider, model)별 토큰 집계 + 단가 매칭 진단(읽기 전용).

    - find_rate로 매칭, 매칭 항목의 contains 보존. rate None → unpriced.
    - 버전경계 의심(_is_version_boundary) → suspect, 그 외 ok.
    - coarse_contains: 같은 contains에 매칭된 distinct 모델이 2개 이상인 항목.
    """
    rows = conn.execute(
        "SELECT provider, model, "
        "SUM(input_tokens+output_tokens+cache_creation+cache_read) AS toks "
        "FROM messages GROUP BY provider, model"
    ).fetchall()
    total = sum((r["toks"] or 0) for r in rows)
    models: list[CoverageModel] = []
    contains_models: dict[str, set] = {}
    for r in rows:
        model = r["model"]
        toks = r["toks"] or 0
        rate = find_rate(model, pricing)
        if rate is None:
            matched, status = None, "unpriced"
        else:
            matched = rate.get("contains")
            status = "suspect" if _is_version_boundary(model or "", matched or "") else "ok"
            contains_models.setdefault(matched, set()).add(model)
        models.append(CoverageModel(
            provider=r["provider"], model=model, matched_contains=matched,
            status=status, tokens=toks,
            token_share=(toks / total) if total else 0.0,
        ))
    models.sort(key=lambda m: m.tokens, reverse=True)
    unpriced = [m for m in models if m.status == "unpriced"]
    return CoverageReport(
        models=models,
        total_tokens=total,
        unpriced_count=len(unpriced),
        unpriced_token_share=(sum(m.tokens for m in unpriced) / total) if total else 0.0,
        suspect_count=sum(1 for m in models if m.status == "suspect"),
        coarse_contains=sorted(c for c, ms in contains_models.items() if len(ms) >= 2),
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python -m pytest tests/test_aggregate.py -k pricing_coverage -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Run full aggregate/pricing tests (회귀 확인)**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python -m pytest tests/test_aggregate.py tests/test_pricing.py -v`
Expected: PASS (전체 통과 — `_insert` 변경에도 기존 테스트 회귀 없음)

- [ ] **Step 7: Commit**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): pricing_coverage 진단 집계(미식별/의심/거친매칭)"
```

---

## Task 4: overview 경고 확장 (`insights` ↔ `cov`)

**Files:**
- Modify: `tokenomy/aggregate.py:682-708` (`insights`)
- Modify: `tokenomy/web/views.py:35-50` (`overview_context`)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: Write the failing test**

`tests/test_aggregate.py` 끝에 추가:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python -m pytest tests/test_aggregate.py::test_insights_unpriced_warning_uses_coverage_species_and_share -v`
Expected: FAIL — `TypeError: insights() got an unexpected keyword argument 'cov'`

- [ ] **Step 3: Write minimal implementation**

`tokenomy/aggregate.py`의 `insights` 시그니처에 `cov=None` 추가하고, 기존 미식별 경고 블록(`aggregate.py:707-708`)을 교체:

시그니처:
```python
def insights(conn, bd: "Burndown", now_kst: datetime, provider: str | None,
             cov: "CoverageReport | None" = None) -> list[Insight]:
```

`if bd.unpriced_count:` 블록(707-708)을 다음으로 교체:
```python
    if cov is not None and cov.unpriced_count:
        pct = cov.unpriced_token_share * 100
        cards.append(Insight(
            "warn",
            f"단가 미식별 {cov.unpriced_count}종(토큰 {pct:.0f}%) — 비용 누락, 설정에서 확인",
        ))
    elif cov is None and bd.unpriced_count:   # cov 미전달 시 하위호환(메시지 건수)
        cards.append(Insight("warn", f"단가 미식별 {bd.unpriced_count}건 — 비용 누락 가능"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python -m pytest tests/test_aggregate.py -k insights -v`
Expected: PASS

- [ ] **Step 5: overview_context가 cov를 계산해 insights에 전달**

`tokenomy/web/views.py` 상단 import 보강:
```python
from tokenomy.aggregate import (
    KST, DIM_COLUMNS, DateGroup, DaySessionRow, FolderGroup, burndown,
    by_day_session, by_dimension, by_project, by_session, codex_burndown,
    daily_series, insights, month_bounds, period_bounds, session_detail,
    sidechain_split, stacked_trend, token_composition, pricing_coverage,
)
from tokenomy.pricing import apply_pricing_overrides, load_pricing
```

`overview_context`의 `coach = insights(conn, claude_bd, now, None)`(views.py:50)을 교체:
```python
    pricing = apply_pricing_overrides(load_pricing(), config.get("pricing_overrides"))
    cov = pricing_coverage(conn, pricing)
    coach = insights(conn, claude_bd, now, None, cov=cov)
```

- [ ] **Step 6: Run web + aggregate tests (회귀)**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python -m pytest tests/test_aggregate.py tests/test_web.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add tokenomy/aggregate.py tokenomy/web/views.py tests/test_aggregate.py
git commit -m "feat(overview): 미식별 경고를 coverage 기반(모델 종 수+토큰 비중)으로 확장"
```

---

## Task 5: settings 진단 카드

**Files:**
- Modify: `tokenomy/web/views.py` (포맷 헬퍼 + `coverage_card_context`)
- Modify: `tokenomy/web/app.py:141-152` (`settings_get`)
- Modify: `tokenomy/web/templates/settings.html`
- Test: `tests/test_web.py`

- [ ] **Step 1: 포맷 헬퍼 테스트 (views)**

`tests/test_web.py` 끝에 추가:

```python
def test_human_tokens_and_share_pct():
    from tokenomy.web.views import _human_tokens, _share_pct
    assert _human_tokens(0) == "0"
    assert _human_tokens(950) == "950"
    assert _human_tokens(12_000) == "12.0K"
    assert _human_tokens(1_500_000) == "1.5M"
    assert _human_tokens(2_300_000_000) == "2.3B"
    assert _share_pct(0.0) == "0%"
    assert _share_pct(0.004) == "<1%"
    assert _share_pct(0.5) == "50%"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python -m pytest tests/test_web.py::test_human_tokens_and_share_pct -v`
Expected: FAIL — `ImportError: cannot import name '_human_tokens'`

- [ ] **Step 3: 포맷 헬퍼 + `coverage_card_context` 구현 (views.py)**

`tokenomy/web/views.py` 끝에 추가:

```python
def _human_tokens(n: int) -> str:
    """토큰 수를 K/M/B 단위 문자열로(예: 1_500_000 → '1.5M')."""
    for unit, div in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(n)


def _share_pct(x: float) -> str:
    """비중(0~1)을 퍼센트 문자열로. 0 초과 1% 미만은 '<1%'."""
    p = x * 100
    return "<1%" if 0 < p < 1 else f"{p:.0f}%"


def coverage_card_context(conn) -> dict:
    """settings 단가 커버리지 카드용 컨텍스트.

    pricing 항목(match[]) 기준 역방향 그룹핑(항목 → 매칭 모델들) + 미식별 별도 묶음.
    거친 매칭은 한 그룹에 모델이 여러 행으로 나타나 자연히 드러난다.
    """
    config = load_config()
    pricing = apply_pricing_overrides(load_pricing(), config.get("pricing_overrides"))
    cov = pricing_coverage(conn, pricing)

    def _row(m):
        return {"model": m.model, "status": m.status,
                "tokens_h": _human_tokens(m.tokens), "share": _share_pct(m.token_share)}

    # match[] 순서대로 그룹 생성(빈 그룹은 제외)
    order = [e.get("contains") for e in pricing.get("match", [])]
    grouped: dict[str, list] = {}
    for m in cov.models:
        if m.matched_contains is not None:
            grouped.setdefault(m.matched_contains, []).append(m)
    groups = []
    for contains in order:
        ms = grouped.get(contains)
        if not ms:
            continue
        rate = next((e for e in pricing["match"] if e.get("contains") == contains), {})
        groups.append({
            "contains": contains,
            "rate": f"${rate.get('input', 0):g}/${rate.get('output', 0):g}",
            "rows": [_row(m) for m in ms],
        })

    unpriced_rows = [_row(m) for m in cov.models if m.status == "unpriced"]
    suspects = [m.model for m in cov.models if m.status == "suspect"]

    if cov.unpriced_count:
        status = ("warn", f"미식별 {cov.unpriced_count}종")
    elif cov.suspect_count:
        status = ("info", f"확인 필요 {cov.suspect_count}종")
    else:
        status = ("ok", "모든 모델 단가 식별됨")

    return {
        "coverage_groups": groups,
        "coverage_unpriced": unpriced_rows,
        "coverage_suspects": suspects,
        "coverage_status": status,   # (level, label)
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python -m pytest tests/test_web.py::test_human_tokens_and_share_pct -v`
Expected: PASS

- [ ] **Step 5: `settings_get`에서 카드 컨텍스트 병합 (app.py)**

`tokenomy/web/app.py` 상단 import에 추가:
```python
from tokenomy.web.views import coverage_card_context
```
(이미 views에서 다른 것을 import 중이면 같은 줄에 합류)

`settings_get`(app.py:141-152)의 `return` dict에 카드 컨텍스트를 펼쳐 병합:
```python
@app.get("/settings")
def settings_get(request: Request):
    config = load_config()
    budget = budget_from_config(config)
    conn = connect()
    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    return templates.TemplateResponse(
        request, "settings.html",
        {"claude": budget.claude, "codex": budget.codex,
         "budget_start": config.get("budget_start") or "",
         "active_nav": "settings", "update_tag": check_update(conn),
         "last_ts": last["t"] if last and last["t"] else None,
         **coverage_card_context(conn)},
    )
```

- [ ] **Step 6: settings.html 카드 추가**

`tokenomy/web/templates/settings.html`의 "데이터 · 프라이버시" `<section>` **앞**에 삽입:

```html
<section class="card">
  <h2>단가 커버리지</h2>
  <p class="muted">pricing.json이 사용 모델을 정확히 매칭하는지 진단합니다.
    상태: <strong class="status-{{ coverage_status[0] }}">{{ coverage_status[1] }}</strong></p>
  <table class="cov-table">
    <thead><tr><th>단가 항목</th><th>매칭된 모델</th><th class="num">토큰</th><th class="num">비중</th></tr></thead>
    <tbody>
      {% for g in coverage_groups %}
        {% for row in g.rows %}
        <tr>
          <td>{% if loop.first %}<code>{{ g.contains }}</code> <span class="muted">{{ g.rate }}</span>{% endif %}</td>
          <td>{{ row.model }}{% if row.status == 'suspect' %} <span title="버전경계 의심">⚠</span>{% endif %}</td>
          <td class="num">{{ row.tokens_h }}</td>
          <td class="num">{{ row.share }}</td>
        </tr>
        {% endfor %}
      {% endfor %}
      {% for row in coverage_unpriced %}
        <tr class="unpriced">
          <td><span class="status-warn">(미식별)</span></td>
          <td>{{ row.model }}</td>
          <td class="num">{{ row.tokens_h }}</td>
          <td class="num">{{ row.share }}</td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
  {% for s in coverage_suspects %}
  <p class="muted">⚠ <code>{{ s }}</code> 가 거친 항목에 매칭됨 — 다른 모델일 수 있으니 단가를 확인하세요.</p>
  {% endfor %}
  <p class="disclaimer">ⓘ 단가 추가·조정: <code>tokenomy.config.json</code> &gt; <code>pricing_overrides</code>에
    <code>{"모델키": {"provider": "...", "input": n, "output": n, "cache_read": n}}</code> 추가(새 모델도 가능). 재ingest로 반영됩니다.</p>
  <p class="disclaimer">ⓘ 단가는 시점 무관·현재 단일 단가로 계산됩니다.</p>
</section>
```

> 주의: 위 블록에 의도적 오타가 없도록 — `{% for g ... %}` 다음 줄은 정확히 `{% for row in g.rows %}`여야 한다(아래 Step 7에서 렌더로 검증).

- [ ] **Step 7: 카드 렌더 테스트**

`tests/test_web.py` 끝에 추가:

```python
def test_settings_coverage_card_renders(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    # priced 모델 + 미식별 모델
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,input_tokens,cost_usd,priced) "
                 "VALUES ('a','claude','s1','p','2026-06-10T10:00:00Z','claude-opus-4-8',100,5.0,1)")
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,input_tokens,cost_usd,priced) "
                 "VALUES ('b','codex','s2','p','2026-06-10T10:00:00Z','gpt-unknown',100,0.0,0)")
    conn.commit()
    r = client.get("/settings")
    assert r.status_code == 200
    assert "단가 커버리지" in r.text
    assert "(미식별)" in r.text
    assert "gpt-unknown" in r.text


def test_settings_coverage_card_empty_db(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "단가 커버리지" in r.text   # 빈 DB에서도 카드 표시(상태: 식별됨)
```

- [ ] **Step 8: Run test to verify it passes**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python -m pytest tests/test_web.py -v`
Expected: PASS. 실패 시 Step 6의 Jinja 블록 오타(특히 `{% for row in g.rows %}` 줄)를 점검.

- [ ] **Step 9: 카드 스타일 (CSS)**

`tokenomy/web/static/src/input.css`의 `@layer components`에 추가(클래스가 이미 있으면 생략):

```css
.cov-table { width: 100%; border-collapse: collapse; margin-top: .5rem; font-size: .875rem; }
.cov-table th, .cov-table td { text-align: left; padding: .25rem .5rem; border-bottom: 1px solid var(--border, #eee); }
.cov-table .num { text-align: right; font-variant-numeric: tabular-nums; }
.cov-table tr.unpriced { color: #b45309; }
.status-warn { color: #b45309; } .status-info { color: #2563eb; } .status-ok { color: #16a34a; }
```

그 다음 CSS 빌드(Tailwind standalone):

Run: `.\build_css.ps1`
Expected: `static/app.css` 재생성. 이 파일을 커밋한다(런타임 무빌드 유지).

> CSS 빌드가 환경상 불가하면(standalone CLI 부재) 이 Step은 스킵하고 PR 설명에 "app.css 재빌드 필요"를 명시한다. 기능 테스트는 Step 8에서 이미 통과한다(스타일 누락은 표시상 문제일 뿐 기능 무관).

- [ ] **Step 10: Commit**

```bash
git add tokenomy/web/views.py tokenomy/web/app.py tokenomy/web/templates/settings.html tokenomy/web/static/ tests/test_web.py
git commit -m "feat(settings): 단가 커버리지 진단 카드"
```

---

## Task 6: CLI report 커버리지 요약

**Files:**
- Modify: `tokenomy/cli.py` (`cmd_report` 끝, import)
- Test: 수동 확인(report는 print 기반 — 통합 스모크)

- [ ] **Step 1: import + 요약 출력 구현**

`tokenomy/cli.py` 상단 import에 추가:
```python
from tokenomy.aggregate import pricing_coverage
from tokenomy.pricing import apply_pricing_overrides, load_pricing
```
(이미 `load_pricing`/`apply_pricing_overrides`를 import 중이면 중복 제거)

`cmd_report`의 마지막 `_print_recent_sessions(conn, now)` **뒤**에 추가:
```python
    pricing = apply_pricing_overrides(load_pricing(), config.get("pricing_overrides"))
    cov = pricing_coverage(conn, pricing)
    if cov.unpriced_count or cov.suspect_count:
        print(f"\n단가 커버리지: 미식별 {cov.unpriced_count}종 · 확인 필요 {cov.suspect_count}종 "
              f"(설정/pricing.json 확인)")
    else:
        print("\n단가 커버리지: 정상")
```

> `config`는 `cmd_report` 첫 줄(`config = load_config()`)에서 이미 바인딩되어 있다.

- [ ] **Step 2: 스모크 — report 실행 확인**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python -m tokenomy.cli report`
Expected: 출력 끝에 `단가 커버리지: ...` 한 줄. 크래시 없음.
(데이터가 없으면 `단가 커버리지: 정상`)

- [ ] **Step 3: Commit**

```bash
git add tokenomy/cli.py
git commit -m "feat(cli): report 끝에 단가 커버리지 요약"
```

---

## Task 7: 문서 갱신 (README · ROADMAP)

**Files:**
- Modify: `README.md` (pricing_overrides 안내)
- Modify: `docs/ROADMAP.md` (#1 항목)

- [ ] **Step 1: README의 pricing_overrides 안내에 "새 모델 추가" 예시 보강**

`README.md`에서 `pricing_overrides`를 설명하는 위치를 찾아(없으면 단가/pricing 섹션에 신설) 다음을 추가:

```markdown
`tokenomy.config.json`의 `pricing_overrides`로 단가를 보정하거나 **새 모델을 추가**할 수 있다(앱 업데이트 불필요, 재ingest로 반영):

```json
"pricing_overrides": {
  "opus":    { "input": 9.0, "output": 36.0 },
  "gpt-5.5": { "provider": "codex", "input": 1.25, "output": 10.0, "cache_read": 0.125 }
}
```

키는 모델 id에 부분일치하는 토큰이다. 기존에 없는 키는 새 단가 항목으로 추가되며,
더 구체적인 키가 기존 항목보다 먼저 매칭된다(예: `gpt-5.5`가 `gpt-5`를 앞선다).
미식별·확인 필요 모델은 설정 화면의 "단가 커버리지" 카드에서 확인한다.
```

> README에 이미 단가 섹션이 있으면 그 톤에 맞춰 위 내용을 녹인다. 없으면 "## 단가" 소제목으로 신설.

- [ ] **Step 2: ROADMAP #1 항목 갱신**

`docs/ROADMAP.md`의 채택 세트 #1 줄(약 53행)을 교체:

```markdown
- [x] **1. 단가 커버리지 신뢰성 진단** (`priced=0`+오매칭+거친매칭) — 모델별 매칭 상태를 settings 카드·
  overview 경고·CLI report로 노출. `pricing_overrides` 확장으로 사용자가 새 모델 단가까지 자가 추가.
  *(단가 편집 GUI는 후속 sub-project로 분해 — spec/plan: `2026-06-17-pricing-coverage-diagnostics*`.)*
```

- [ ] **Step 3: Commit**

```bash
git add README.md docs/ROADMAP.md
git commit -m "docs: 단가 커버리지 진단 — README overrides 예시 + ROADMAP #1 반영"
```

---

## Task 8: 전체 테스트 + 마무리

- [ ] **Step 1: 전체 테스트**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python -m pytest`
Expected: 전체 PASS (test_launcher 포트 충돌만 환경 이슈로 허용 — 메모리 참조).

- [ ] **Step 2: 앱 수동 확인 (선택)**

Run: `C:/projects/samsung/tokenomy/.venv/Scripts/python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765`
브라우저로 `/settings` 진입 → "단가 커버리지" 카드 표시 확인, `/` overview에서 미식별 시 경고 확인.

- [ ] **Step 3: 마무리**

`superpowers:finishing-a-development-branch` 스킬로 병합/PR 옵션을 진행한다.

---

## Self-Review (작성자 점검 완료)

- **Spec 커버리지**: §4 진단(Task 3)·§5 settings 카드(Task 5)·§6 overrides 확장(Task 2)·§7 overview 경고(Task 4)·§8 CLI(Task 6)·§9 테스트(각 Task)·§10 영향범위(전 Task)·휴리스틱(Task 1)·문서(Task 7) — 전부 매핑됨.
- **타입 일관성**: `CoverageReport`(models/total_tokens/unpriced_count/unpriced_token_share/suspect_count/coarse_contains)·`CoverageModel`(provider/model/matched_contains/status/tokens/token_share)·`insights(..., cov=None)`·`coverage_card_context(conn)`·`_human_tokens`/`_share_pct` 시그니처가 정의·사용처에서 일치.
- **플레이스홀더**: 없음(모든 코드 블록 실체 포함). CSS 빌드 불가 시 대체 경로 명시.
