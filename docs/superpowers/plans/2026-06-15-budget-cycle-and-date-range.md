# 예산 주기 차별화 · 도입일 · 임의 구간 조회 — 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claude는 월간·Codex는 주간 누적(carryover) 번다운으로 분리하고, 예산 도입일(`budget_start`)을 기간 시작으로 반영하며, 내역/모델별에 주·월 토글과 사용자 지정 날짜 구간 조회를 추가한다.

**Architecture:** 설정에는 한도 + 도입일만 두고(접근 A), "Codex 주간 = 월÷4, 월 내 누적, 월요일 경계" 규칙은 코드에 담는다. `_compute_burndown`을 "기간 `[start, end)`를 받는 순수 함수"로 일반화해 Claude(월간 clamp)와 신규 `codex_burndown`(주간 누적)이 공유한다. 집계 계층은 이미 임의 `[start, nxt)`를 지원하므로 신규 집계는 최소.

**Tech Stack:** Python 3 stdlib(sqlite3/datetime), FastAPI + Jinja2, htmx, pytest. 시간은 KST(UTC+9), 테스트는 `now_kst`/`budget_start` 주입으로 결정적.

**설계 문서:** `docs/superpowers/specs/2026-06-15-budget-cycle-and-date-range-design.md`

---

## 파일 구조

| 파일 | 책임 | 변경 |
|------|------|------|
| `tokenomy/budget.py` | 설정·예산 모델 | `budget_start_kst()`, `Budget.weekly_codex_limit()`, config 키 |
| `tokenomy/aggregate.py` | 집계·번다운 | `effective_month_start()`, `week_count()`, `_compute_burndown` 일반화, `burndown` clamp, `codex_burndown()` + `CodexBurndown` |
| `tokenomy/web/views.py` | 화면 조립 | `overview_context`(카드 2개+총지출), `history_context`/`models_context`(period/custom) |
| `tokenomy/web/app.py` | 라우트(얇게) | `/history`·`/models`에 `period`/`start`/`end` 검증, `/settings` POST에 `budget_start` |
| `tokenomy/web/templates/overview.html` | 대시보드 | 통합 바 → 총지출 요약, AI별 번다운 카드 |
| `tokenomy/web/templates/settings.html` | 설정 폼 | 도입일 입력 |
| `tokenomy/web/templates/_history_body.html` | 내역 컨트롤 | 주/월 토글 + 날짜 범위 |
| `tokenomy/web/templates/models.html` | 모델별 컨트롤 | 주/월 토글 + 날짜 범위 |
| `config/tokenomy.config.example.json` | 설정 예시 | `budget_start` 예시 |

진행 순서: **Phase 1 설정 → Phase 2 번다운 모델 → Phase 3 대시보드 → Phase 4 구간 조회.** 각 Phase는 독립적으로 테스트 통과 상태를 유지한다.

---

## Phase 1 — 설정 (budget_start)

### Task 1: `budget.py` — 도입일 파싱 + 주간 한도 헬퍼

**Files:**
- Modify: `tokenomy/budget.py`
- Test: `tests/test_budget.py`

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_budget.py` 끝에 추가

```python
from datetime import datetime, timezone, timedelta

from tokenomy.budget import budget_start_kst

_KST = timezone(timedelta(hours=9))


def test_weekly_codex_limit_is_quarter():
    b = Budget(claude=200, codex=40)
    assert b.weekly_codex_limit() == 10.0   # 40 / 4


def test_budget_start_kst_parses_iso_date():
    dt = budget_start_kst({"budget_start": "2026-06-12"})
    assert dt == datetime(2026, 6, 12, 0, 0, tzinfo=_KST)


def test_budget_start_kst_none_when_absent_or_blank():
    assert budget_start_kst({}) is None
    assert budget_start_kst({"budget_start": ""}) is None
    assert budget_start_kst({"budget_start": "garbage"}) is None


def test_load_config_keeps_budget_start(tmp_path):
    p = tmp_path / "c.json"
    save_config({"budget": {"claude": 1, "codex": 2}, "budget_start": "2026-06-12"}, p)
    cfg = load_config(p)
    assert cfg["budget_start"] == "2026-06-12"


def test_load_config_missing_budget_start_is_none(tmp_path):
    cfg = load_config(tmp_path / "nope.json")
    assert cfg.get("budget_start") is None
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_budget.py -k "weekly_codex or budget_start" -v`
Expected: FAIL — `ImportError: cannot import name 'budget_start_kst'` / `AttributeError: weekly_codex_limit`

- [ ] **Step 3: 구현** — `tokenomy/budget.py`

상단 import에 datetime 추가:

```python
from datetime import datetime, timedelta, timezone
```

`KST` 상수를 파일 상단(`@dataclass` 위)에 추가(aggregate를 import하면 순환되므로 자체 정의):

```python
KST = timezone(timedelta(hours=9))
```

`Budget` 데이터클래스에 메서드 추가(`limit_for` 아래):

```python
    def weekly_codex_limit(self) -> float:
        """Codex 주간 한도 = 월 한도 ÷ 4 (예산 정책)."""
        return self.codex / 4
```

`load_config`의 `base` 딕셔너리에 키 추가:

```python
    base = {"user_label": _default_label(),
            "budget": {"claude": 0.0, "codex": 0.0},
            "budget_start": None,
            "pricing_overrides": {}}
```

파일 끝에 함수 추가:

```python
def budget_start_kst(config: dict) -> datetime | None:
    """config['budget_start']('YYYY-MM-DD')를 KST 자정 datetime으로 파싱.

    빈 문자열·None·형식 오류는 모두 None(미설정)으로 취급한다(하위호환).
    """
    raw = config.get("budget_start")
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=KST)
    except (ValueError, TypeError):
        return None
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_budget.py -v`
Expected: PASS (기존 + 신규 전부)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/budget.py tests/test_budget.py
git commit -m "feat(budget): budget_start 도입일 파싱 + Codex 주간 한도 헬퍼"
```

---

### Task 2: 설정 화면 — 도입일 입력·저장

**Files:**
- Modify: `tokenomy/web/app.py:125-152` (`settings_get`, `settings_post`)
- Modify: `tokenomy/web/templates/settings.html:8-12`
- Modify: `config/tokenomy.config.example.json`
- Test: `tests/test_web.py`

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_web.py` 끝에 추가

```python
def test_settings_get_shows_budget_start_field(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"budget": {"claude": 100, "codex": 40}, "budget_start": "2026-06-12"}',
                   encoding="utf-8")
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'name="budget_start"' in r.text
    assert "2026-06-12" in r.text          # 기존 값 표시


def test_settings_post_writes_budget_start(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings",
                    data={"claude": "200", "codex": "40", "budget_start": "2026-06-12"},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["budget_start"] == "2026-06-12"


def test_settings_post_blank_budget_start_is_null(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings", data={"claude": "200", "codex": "40", "budget_start": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["budget_start"] is None
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -k "budget_start" -v`
Expected: FAIL — `name="budget_start"` 미존재 / 저장 안 됨

- [ ] **Step 3: 구현**

`tokenomy/web/app.py` — `settings_get`의 TemplateResponse에 `budget_start` 추가:

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
         "last_ts": last["t"] if last and last["t"] else None},
    )
```

`settings_post` — `budget_start` 폼 필드 처리(빈 문자열·형식 오류 → None):

```python
@app.post("/settings")
def settings_post(claude: str = Form(""), codex: str = Form(""),
                  budget_start: str = Form("")):
    config = load_config()
    config["budget"]["claude"] = _to_float(claude)
    config["budget"]["codex"] = _to_float(codex)
    config["budget_start"] = _valid_date_or_none(budget_start)
    save_config(config)
    return RedirectResponse("/", status_code=303)
```

`_to_float` 아래에 헬퍼 추가:

```python
def _valid_date_or_none(value: str | None) -> str | None:
    """'YYYY-MM-DD'면 그대로, 아니면 None. 잘못된 입력으로 config가 깨지지 않게 한다."""
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return None
```

`tokenomy/web/templates/settings.html` — 예산 폼에 입력 추가(`codex` label 아래, `button` 위):

```html
    <label>Codex (USD) <input type="number" step="0.01" min="0" name="codex" value="{{ '%.2f'|format(codex) }}"></label>
    <label>예산 도입일 <input type="date" name="budget_start" value="{{ budget_start }}"></label>
    <p class="muted">도입일을 지정하면 그 날짜부터 예산을 계산합니다(이전 지출 제외). 비우면 매월 1일 기준.</p>
    <button class="btn" type="submit">저장</button>
```

`config/tokenomy.config.example.json` — `budget_start` 예시 추가:

```json
{
  "user_label": "me",
  "budget": {
    "claude": 100,
    "codex": 50
  },
  "budget_start": null,
  "pricing_overrides": {}
}
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -k "settings" tests/test_budget.py -k "example_config" -v`
Expected: PASS (도입일 신규 + 기존 settings 테스트 + example config 유효성)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/app.py tokenomy/web/templates/settings.html config/tokenomy.config.example.json tests/test_web.py
git commit -m "feat(settings): 예산 도입일(budget_start) 입력·저장"
```

---

## Phase 2 — 번다운 모델

### Task 3: `aggregate.py` — effective_start + 주차 카운트 헬퍼

**Files:**
- Modify: `tokenomy/aggregate.py` (import + 신규 함수)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_aggregate.py` 끝에 추가

```python
from tokenomy.aggregate import effective_month_start, week_count


def test_effective_month_start_clamps_to_budget_start():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=KST)
    bs = datetime(2026, 6, 12, 0, 0, tzinfo=KST)
    assert effective_month_start(now, bs) == datetime(2026, 6, 12, 0, 0, tzinfo=KST)


def test_effective_month_start_none_returns_month_first():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=KST)
    assert effective_month_start(now, None) == datetime(2026, 6, 1, 0, 0, tzinfo=KST)


def test_effective_month_start_ignores_other_month_budget_start():
    # 도입일이 이번 달(6월)이 아니면(과거/미래) 달력 월 1일 사용
    now = datetime(2026, 6, 15, 12, 0, tzinfo=KST)
    assert effective_month_start(now, datetime(2026, 5, 3, tzinfo=KST)) == datetime(2026, 6, 1, 0, 0, tzinfo=KST)
    assert effective_month_start(now, datetime(2026, 7, 9, tzinfo=KST)) == datetime(2026, 6, 1, 0, 0, tzinfo=KST)


def test_week_count_same_week_is_one():
    eff = datetime(2026, 6, 12, 0, 0, tzinfo=KST)   # 금
    now = datetime(2026, 6, 12, 18, 0, tzinfo=KST)  # 같은 주
    assert week_count(eff, now) == 1


def test_week_count_counts_monday_resets():
    # 도입 6/12(금, 2주차 6/8~14) → 오늘 6/15(월, 3주차) = 2회 충전
    eff = datetime(2026, 6, 12, 0, 0, tzinfo=KST)
    now = datetime(2026, 6, 15, 9, 0, tzinfo=KST)
    assert week_count(eff, now) == 2


def test_week_count_partial_first_week_of_month():
    # 7/1(수) effective → 1주차. 7/6(월) → 2주차
    assert week_count(datetime(2026, 7, 1, tzinfo=KST), datetime(2026, 7, 1, 12, tzinfo=KST)) == 1
    assert week_count(datetime(2026, 7, 1, tzinfo=KST), datetime(2026, 7, 6, 9, tzinfo=KST)) == 2
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k "effective_month_start or week_count" -v`
Expected: FAIL — `ImportError: cannot import name 'effective_month_start'`

- [ ] **Step 3: 구현** — `tokenomy/aggregate.py`의 `month_bounds` 아래에 추가

```python
def _midnight(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def effective_month_start(now_kst: datetime, budget_start: datetime | None) -> datetime:
    """이번 달 기간 시작 — budget_start가 이번 달 안이면 그 날짜로 clamp, 아니면 1일.

    budget_start가 과거·미래 달이면 무시(달력 월 1일). 일회성 도입일이 첫 달만
    영향을 주도록 한다.
    """
    start, nxt = month_bounds(now_kst)
    if budget_start and start <= budget_start < nxt:
        return _midnight(budget_start)
    return start


def week_count(effective_start: datetime, now_kst: datetime) -> int:
    """effective_start가 속한 주(1주차)부터 now가 속한 주까지의 주 수(월요일 경계).

    Codex 주간 한도 충전 횟수 N. 각 주 시작(월요일)마다 +1, effective_start의 주를 1로 센다.
    """
    eff_mon = _midnight(effective_start) - timedelta(days=effective_start.weekday())
    now_mon = _midnight(now_kst) - timedelta(days=now_kst.weekday())
    return (now_mon - eff_mon).days // 7 + 1
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k "effective_month_start or week_count" -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): effective_month_start + week_count(주차) 헬퍼"
```

---

### Task 4: `_compute_burndown` 일반화 + Claude 도입일 clamp

**Files:**
- Modify: `tokenomy/aggregate.py` (`_compute_burndown`, `burndown`)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_aggregate.py` 끝에 추가

```python
def test_burndown_clamps_to_budget_start():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 50.0, session="pre")    # 도입 전(제외)
    _insert(conn, "2026-06-13T00:00:00Z", 10.0, session="post")   # 도입 후
    now = datetime(2026, 6, 15, 12, 0, tzinfo=KST)
    bs = datetime(2026, 6, 12, 0, 0, tzinfo=KST)
    bd = burndown(conn, Budget(claude=100, codex=0), now, "claude", budget_start=bs)
    assert bd.spent == 10.0                       # 6/5 제외, 6/13만
    # 기간 6/12~6/30(19일), 경과 6/12~6/15 = 4일
    assert bd.days_in_month == 19
    assert bd.day_of_month == 4
    assert bd.daily_avg == 2.5                    # 10 / 4
    assert bd.projected_month == round(2.5 * 19, 4)


def test_burndown_no_budget_start_is_unchanged():
    # budget_start 미지정이면 기존(달력 월) 동작 그대로
    conn = connect(":memory:")
    for _ in range(3):
        _insert(conn, "2026-06-05T00:00:00Z", 10.0, session=str(_))
    bd = burndown(conn, Budget(claude=100, codex=0), NOW, "claude")
    assert bd.spent == 30.0
    assert bd.days_in_month == 30
    assert bd.day_of_month == 10                  # NOW = 6/10
    assert bd.daily_avg == 3.0
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k "clamps_to_budget_start or no_budget_start_is_unchanged" -v`
Expected: FAIL — `burndown() got an unexpected keyword argument 'budget_start'`

- [ ] **Step 3: 구현** — `tokenomy/aggregate.py`

`_compute_burndown` 시그니처와 기간 계산을 일반화(기존 로직 유지, 기간만 주입 가능하게):

```python
def _compute_burndown(provider: str, spent: float, limit: float,
                      unpriced: int, now_kst: datetime, *,
                      period_start: datetime | None = None,
                      period_end: datetime | None = None) -> Burndown:
    """집계된 (spent, limit, unpriced)로 Burndown을 산출하는 순수 함수.

    period_start/end 미지정 시 now_kst의 달력 월을 기간으로 쓴다(하위호환). 지정 시
    그 기간 [start, end)를 기준으로 경과일·예상치를 계산한다(예: 도입일 clamp).
    provider별 burndown과 통합 combined_burndown이 공유한다.
    """
    if period_start is None or period_end is None:
        period_start, period_end = month_bounds(now_kst)
    days_in_month = (period_end - period_start).days
    day_of_month = (_midnight(now_kst) - period_start).days + 1
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
```

`burndown`을 도입일 clamp 적용으로 교체:

```python
def burndown(conn, budget: Budget, now_kst: datetime, provider: str = "claude",
             *, budget_start: datetime | None = None) -> Burndown:
    period_start = effective_month_start(now_kst, budget_start)
    _, period_end = month_bounds(now_kst)
    rows = _range_rows(conn, provider, period_start, period_end)
    spent = sum((r["cost_usd"] or 0) for r in rows)
    unpriced = sum(1 for r in rows if not r["priced"])
    limit = budget.limit_for(provider)
    return _compute_burndown(provider, spent, limit, unpriced, now_kst,
                             period_start=period_start, period_end=period_end)
```

> 참고: `combined_burndown`은 `_compute_burndown`을 period 인자 없이 호출하므로 변경 불필요(달력 월 유지). 기존 테스트 그대로 통과.

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k "burndown" -v`
Expected: PASS (신규 clamp 2건 + 기존 burndown/combined/status 전부)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): _compute_burndown 기간 일반화 + Claude 도입일 clamp"
```

---

### Task 5: `codex_burndown` — 주간 누적(carryover) 모델

**Files:**
- Modify: `tokenomy/aggregate.py` (`CodexBurndown` dataclass + `codex_burndown`)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_aggregate.py` 끝에 추가

```python
from tokenomy.aggregate import CodexBurndown, codex_burndown


def test_codex_burndown_carryover_denominator_and_remaining():
    conn = connect(":memory:")
    # 월한도 40 → W=10. 도입 6/12(2주차), 오늘 6/15(3주차) → N=2 → 분모 20
    _insert(conn, "2026-06-13T00:00:00Z", 6.0, session="x", provider="codex")  # KST 6/13 09:00
    now = datetime(2026, 6, 15, 12, 0, tzinfo=KST)
    bs = datetime(2026, 6, 12, 0, 0, tzinfo=KST)
    cb = codex_burndown(conn, Budget(claude=0, codex=40), now, budget_start=bs)
    assert cb.weekly_limit == 10.0
    assert cb.weeks_elapsed == 2
    assert cb.limit_to_date == 20.0
    assert cb.spent == 6.0
    assert cb.remaining == 14.0          # 20 - 6 (3주차 새 10 + 2주차 미사용 4 이월)
    assert cb.pct == 0.3
    assert cb.status == "ok"


def test_codex_burndown_excludes_pre_budget_start():
    conn = connect(":memory:")
    _insert(conn, "2026-06-05T00:00:00Z", 99.0, session="pre", provider="codex")  # 도입 전
    _insert(conn, "2026-06-13T00:00:00Z", 3.0, session="post", provider="codex")
    now = datetime(2026, 6, 15, 12, 0, tzinfo=KST)
    bs = datetime(2026, 6, 12, 0, 0, tzinfo=KST)
    cb = codex_burndown(conn, Budget(claude=0, codex=40), now, budget_start=bs)
    assert cb.spent == 3.0               # 6/5 제외


def test_codex_burndown_exceeds_when_over_accumulated():
    conn = connect(":memory:")
    _insert(conn, "2026-06-13T00:00:00Z", 25.0, session="x", provider="codex")
    now = datetime(2026, 6, 15, 12, 0, tzinfo=KST)
    bs = datetime(2026, 6, 12, 0, 0, tzinfo=KST)
    cb = codex_burndown(conn, Budget(claude=0, codex=40), now, budget_start=bs)
    assert cb.limit_to_date == 20.0
    assert cb.spent == 25.0
    assert cb.remaining == -5.0
    assert cb.status == "exceeds"


def test_codex_burndown_no_budget_start_uses_month_first():
    conn = connect(":memory:")
    # 도입일 없음 → 6/1부터. 6/1=월이라 6/15(월)=3주차 → N=3 → 분모 30
    _insert(conn, "2026-06-02T00:00:00Z", 5.0, session="x", provider="codex")
    now = datetime(2026, 6, 15, 12, 0, tzinfo=KST)
    cb = codex_burndown(conn, Budget(claude=0, codex=40), now)
    assert cb.weeks_elapsed == 3
    assert cb.limit_to_date == 30.0
    assert cb.spent == 5.0


def test_codex_burndown_week_spent_only_current_week():
    conn = connect(":memory:")
    _insert(conn, "2026-06-13T00:00:00Z", 4.0, session="prev", provider="codex")  # 2주차
    _insert(conn, "2026-06-15T03:00:00Z", 2.0, session="cur", provider="codex")   # KST 6/15 12:00 (3주차)
    now = datetime(2026, 6, 15, 18, 0, tzinfo=KST)
    bs = datetime(2026, 6, 12, 0, 0, tzinfo=KST)
    cb = codex_burndown(conn, Budget(claude=0, codex=40), now, budget_start=bs)
    assert cb.spent == 6.0               # 전체 누적
    assert cb.week_spent == 2.0          # 이번 주(6/15~)만
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k "codex_burndown" -v`
Expected: FAIL — `ImportError: cannot import name 'codex_burndown'`

- [ ] **Step 3: 구현** — `tokenomy/aggregate.py`의 `combined_burndown` 아래에 추가

```python
@dataclass
class CodexBurndown:
    """Codex 주간 누적(carryover) 번다운.

    분모 limit_to_date = weekly_limit(W) × weeks_elapsed(N).
    분자 spent = effective_start ~ 이번 달 누적 지출. remaining = 이번 주 가용(이월 포함).
    월이 바뀌면 분자·분모 모두 리셋(이월 소멸). 주간 모델이라 예상 월말은 내지 않는다.
    """
    provider: str           # "codex"
    weekly_limit: float     # W = 월한도 ÷ 4
    weeks_elapsed: int      # N (이번 달 충전 횟수)
    limit_to_date: float    # W × N
    spent: float            # 이번 달 누적 지출(effective_start~)
    remaining: float        # 이번 주 가용 = limit_to_date − spent
    pct: float
    status: str             # "ok" | "exceeds"
    unpriced_count: int
    week_spent: float       # 이번 주(월요일~)만의 지출(표시용)


def codex_burndown(conn, budget: Budget, now_kst: datetime,
                   *, budget_start: datetime | None = None) -> CodexBurndown:
    month_start, month_end = month_bounds(now_kst)
    eff = effective_month_start(now_kst, budget_start)
    weekly = budget.weekly_codex_limit()
    weeks = week_count(eff, now_kst)
    limit_to_date = round(weekly * weeks, 4)

    rows = _range_rows(conn, "codex", eff, month_end)
    spent = round(sum((r["cost_usd"] or 0) for r in rows), 4)
    unpriced = sum(1 for r in rows if not r["priced"])
    remaining = round(limit_to_date - spent, 4)
    pct = round(spent / limit_to_date, 4) if limit_to_date > 0 else 0.0

    week_start = max(_midnight(now_kst) - timedelta(days=now_kst.weekday()), eff)
    week_rows = _range_rows(conn, "codex", week_start, month_end)
    week_spent = round(sum((r["cost_usd"] or 0) for r in week_rows), 4)

    status = "exceeds" if (limit_to_date > 0 and spent >= limit_to_date) else "ok"

    return CodexBurndown(
        provider="codex", weekly_limit=round(weekly, 4), weeks_elapsed=weeks,
        limit_to_date=limit_to_date, spent=spent, remaining=remaining, pct=pct,
        status=status, unpriced_count=unpriced, week_spent=week_spent,
    )
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k "codex_burndown" -v`
Expected: PASS (5건)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): codex_burndown 주간 누적(carryover) 모델"
```

---

## Phase 3 — 대시보드

### Task 6: `overview_context` — provider별 카드 + 총지출 요약

**Files:**
- Modify: `tokenomy/web/views.py:27-69` (`overview_context`)
- Modify: `tests/test_aggregate.py` (기존 `test_overview_context_shape` 갱신)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: 기존 테스트 갱신 + 신규 테스트** — `tests/test_aggregate.py`의 `test_overview_context_shape`를 아래로 교체하고, 그 아래에 신규 테스트 추가

기존 `test_overview_context_shape`(381~400행 부근) 본문을 교체:

```python
def test_overview_context_shape(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"budget": {"claude": 100, "codex": 40}}', encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=10.0, project="/p")
    _msg(conn, dedup_key="b", provider="codex", ts="2026-06-11T10:00:00Z", cost_usd=4.0, project="/p")
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)
    assert ctx["active_nav"] == "dashboard"
    # provider별 분리 카드
    assert ctx["claude_bd"].spent == 10.0
    assert ctx["codex_bd"].spent == 4.0
    assert ctx["codex_bd"].weekly_limit == 10.0          # 40 / 4
    # 총지출 요약 = 두 카드 spent 합
    assert ctx["month_total"] == 14.0
    assert ctx["budget_configured"] is True
    assert ctx["projects"][0].project == "/p"
    assert ctx["projects"][0].cost == 14.0
    assert ctx["has_data"] is True
    assert "daily_labels" in ctx and "insights" in ctx and "sessions" in ctx
```

신규 테스트 추가:

```python
def test_overview_context_applies_budget_start(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"budget": {"claude": 100, "codex": 40}, "budget_start": "2026-06-12"}',
                   encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    conn = connect(":memory:")
    _msg(conn, dedup_key="pre", provider="claude", ts="2026-06-05T10:00:00Z", cost_usd=99.0)
    _msg(conn, dedup_key="post", provider="claude", ts="2026-06-13T10:00:00Z", cost_usd=10.0)
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)   # _NOW_STATUS = 6/15
    assert ctx["claude_bd"].spent == 10.0                # 6/5(도입 전) 제외
    assert ctx["claude_bd"].days_in_month == 19          # 6/12~6/30


def test_overview_context_no_budget_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=10.0)
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)
    assert ctx["budget_configured"] is False
    assert ctx["claude_bd"].limit == 0
```

> `test_overview_context_provider_without_data`(403~411행)는 `ctx["cards"]`를 참조하므로 함께 갱신한다. 본문을 아래로 교체:

```python
def test_overview_context_provider_without_data(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", provider="claude", ts="2026-06-10T10:00:00Z", cost_usd=10.0)
    ctx = overview_context(conn, sort="cost", now_kst=_NOW_STATUS)
    assert ctx["claude_has_data"] is True
    assert ctx["codex_has_data"] is False                # codex 로그 없음
    assert ctx["budget_configured"] is False
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k "overview_context" -v`
Expected: FAIL — `KeyError: 'claude_bd'` 등

- [ ] **Step 3: 구현** — `tokenomy/web/views.py`

import에 신규 심볼 추가(`from tokenomy.aggregate import (...)` 블록):

```python
from tokenomy.aggregate import (
    KST, PROVIDERS, DateGroup, DaySessionRow, FolderGroup, burndown, by_day_session,
    by_model, by_project, by_session, codex_burndown, daily_series,
    insights, month_bounds, period_bounds, session_detail,
)
from tokenomy.budget import budget_from_config, budget_start_kst, load_config, user_label
```

`overview_context`를 교체:

```python
def overview_context(conn, sort: str, now_kst: datetime | None = None) -> dict:
    now = now_kst or datetime.now(KST)
    config = load_config()
    budget = budget_from_config(config)
    bs = budget_start_kst(config)

    claude_bd = burndown(conn, budget, now, "claude", budget_start=bs)
    codex_bd = codex_burndown(conn, budget, now, budget_start=bs)
    month_total = round(claude_bd.spent + codex_bd.spent, 4)

    projects = by_project(conn, None, now)
    projects.sort(key=_SORT_KEYS.get(sort, _SORT_KEYS["cost"]), reverse=True)
    projects = projects[:10]
    sessions = by_session(conn, None, now, limit_n=10)
    # 효율 코치/추세는 전 AI 합산·달력 월 기준 유지(설계). Burndown 인자는 claude 카드 재사용.
    coach = insights(conn, claude_bd, now, None)
    daily = daily_series(conn, None, now)

    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    has_data = last is not None and last["t"] is not None

    return {
        "active_nav": "dashboard", "sort": sort,
        "user_label": user_label(config),
        "budget_configured": budget.total > 0,
        "budget_start": config.get("budget_start"),
        "month": now.strftime("%Y-%m"),
        "claude_bd": claude_bd, "codex_bd": codex_bd, "month_total": month_total,
        "claude_has_data": _provider_has_data(conn, "claude"),
        "codex_has_data": _provider_has_data(conn, "codex"),
        "projects": projects, "sessions": sessions, "insights": coach,
        "daily_labels": [p.day for p in daily],
        "daily_actual": [p.cumulative_cost for p in daily],
        "daily_pace": [round(claude_bd.limit / claude_bd.days_in_month * p.day, 4)
                       if claude_bd.limit else 0.0 for p in daily],
        "last_ts": last["t"] if has_data else None,
        "has_data": has_data,
    }
```

> `combined_burndown` import는 더 이상 overview에서 안 쓰지만, 기존 다른 테스트/호환을 위해 `aggregate.py`·`views.py` import 목록에서 제거하지 않는다(여전히 export). views import에서 `combined_burndown`만 사용처가 없으면 제거 가능 — 제거 시 `test_aggregate.py`가 `from tokenomy.aggregate import combined_burndown`로 직접 import하므로 영향 없음.

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -v`
Expected: PASS (overview 신규/갱신 포함 전체)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/views.py tests/test_aggregate.py
git commit -m "feat(views): 대시보드 provider별 카드 + 이번 달 총지출 요약"
```

---

### Task 7: `overview.html` — 통합 바 격하 + AI별 번다운 카드

**Files:**
- Modify: `tokenomy/web/templates/overview.html:9-55`
- Modify: `tests/test_web.py` (통합 바 텍스트를 참조하는 기존 테스트)
- Test: `tests/test_web.py`

- [ ] **Step 1: 기존 테스트 갱신 + 신규** — `tests/test_web.py`

아래 기존 assertion들을 새 섹션명으로 바꾼다(통합 바 → 총지출/번다운):

`test_dashboard_renders_sections_with_data`의 루프를 교체:
```python
    for section in ("이번 달 총지출", "AI별 번다운", "통합 추세", "통합 효율 코치", "통합 프로젝트별", "복기"):
        assert section in r.text
```

`test_overview_aggregates_providers`의 루프를 교체:
```python
    for section in ("이번 달 총지출", "AI별 번다운", "통합 추세", "통합 효율 코치",
                    "통합 프로젝트별", "복기"):
        assert section in r.text
```

`test_dashboard_empty_db_ok`의 마지막 줄을 교체:
```python
    assert "총지출" in r.text
```

`test_root_renders_overview`의 두 줄을 교체:
```python
    assert "이번 달 총지출" in r.text
    assert "AI별 번다운" in r.text
```

신규 테스트 추가(끝에):
```python
def test_dashboard_shows_codex_weekly_card(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"budget": {"claude": 100, "codex": 40}, "budget_start": "2026-06-12"}',
                   encoding="utf-8")
    conn_factory = lambda: connect(str(tmp_path / "t.db"))
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,cost_usd,priced) "
                 "VALUES ('a','codex','s1','2026-06-13T01:00:00Z',6.0,1)")
    conn.commit()
    # connect를 t.db로 고정
    monkeypatch.setattr(app_module, "connect", conn_factory)
    r = client.get("/")
    assert r.status_code == 200
    assert "주간" in r.text          # Codex 카드 주간 한도 표기
    assert "Codex" in r.text
```

> 참고: `_client`는 이미 `app_module.connect`를 `tmp_path/t.db`로 교체한다. 위 테스트는 같은 DB 경로를 쓰면 되므로, 실제 작성 시 `_client`가 돌려준 `conn_factory`를 사용해도 된다(다른 web 테스트와 동일 패턴).

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -k "dashboard or overview or root" -v`
Expected: FAIL — 새 섹션명 미존재

- [ ] **Step 3: 구현** — `tokenomy/web/templates/overview.html`

9~55행(통합 번다운 `section` + `AI별 현황` `section`)을 아래로 교체:

```html
<section class="card">
  <h2>이번 달 총지출 <span class="muted">(전 AI 합산{% if budget_start %} · 도입 {{ budget_start }}{% endif %})</span></h2>
  {% if not has_data %}
    <p class="muted">데이터 없음 · 사이드바 [↻ 새로고침]을 누르세요</p>
  {% else %}
    <p class="bd-num serif">${{ '%.2f'|format(month_total) }}
      <span class="muted">= Claude ${{ '%.2f'|format(claude_bd.spent) }} + Codex ${{ '%.2f'|format(codex_bd.spent) }}</span></p>
    <p class="disclaimer">ⓘ 공개 API 단가 기준 추정 · 이 머신 데이터만</p>
  {% endif %}
</section>

<section class="card">
  <h2>AI별 번다운</h2>
  <div class="ai-cards">
    {# Claude — 월간 한도 #}
    <a class="ai-card" href="/history?provider=claude">
      <div class="ai-name">Claude <span class="muted">· 월간</span></div>
      {% if not claude_has_data %}
        <div class="muted">(이 머신에 Claude 로그 없음)</div>
      {% elif claude_bd.limit == 0 %}
        <div class="ai-num">${{ '%.2f'|format(claude_bd.spent) }} <span class="muted">사용량만</span></div>
      {% else %}
        <div class="ai-num">${{ '%.2f'|format(claude_bd.spent) }} / ${{ '%.0f'|format(claude_bd.limit) }}</div>
        <span class="bar"><span class="fill s-{{ claude_bd.status }}" style="width: {{ [claude_bd.pct * 100, 100]|min }}%"></span></span>
        <div class="muted">{{ '%.1f'|format(claude_bd.pct * 100) }}% · 예상월말 ${{ '%.0f'|format(claude_bd.projected_month) }}
          <span class="status s-{{ claude_bd.status }}">{{ {'ok':'✅','warn':'⚠','exceeds':'⛔'}[claude_bd.status] }}</span>
        </div>
      {% endif %}
    </a>
    {# Codex — 주간 누적(carryover) #}
    <a class="ai-card" href="/history?provider=codex">
      <div class="ai-name">Codex <span class="muted">· 주간({{ codex_bd.weeks_elapsed }}주차)</span></div>
      {% if not codex_has_data %}
        <div class="muted">(이 머신에 Codex 로그 없음)</div>
      {% elif codex_bd.weekly_limit == 0 %}
        <div class="ai-num">${{ '%.2f'|format(codex_bd.spent) }} <span class="muted">사용량만</span></div>
      {% else %}
        <div class="ai-num">${{ '%.2f'|format(codex_bd.spent) }} / ${{ '%.2f'|format(codex_bd.limit_to_date) }}</div>
        <span class="bar"><span class="fill s-{{ codex_bd.status }}" style="width: {{ [codex_bd.pct * 100, 100]|min }}%"></span></span>
        <div class="muted">이번 주 ${{ '%.2f'|format(codex_bd.remaining) }} 남음 <span class="muted">(주간 한도 ${{ '%.2f'|format(codex_bd.weekly_limit) }})</span>
          <span class="status s-{{ codex_bd.status }}">{{ {'ok':'✅','exceeds':'⛔'}[codex_bd.status] }}</span>
        </div>
      {% endif %}
    </a>
  </div>
</section>
```

> `{% set bd = combined %}`(9행) 제거됨에 유의. 차트(`trend`)·코치·프로젝트·세션 섹션(57행 이하)은 변경 없음.

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -v`
Expected: PASS (대시보드 전체)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/templates/overview.html tests/test_web.py
git commit -m "feat(web): 대시보드 통합 바 → 총지출 요약 + Claude/Codex 번다운 카드"
```

---

## Phase 4 — 구간 조회 (내역 / 모델별)

### Task 8: `views.py` — period(week/month) + 사용자 지정 범위

**Files:**
- Modify: `tokenomy/web/views.py` (`history_context`, `models_context`)
- Test: `tests/test_aggregate.py`

기간 해석 규칙(두 컨텍스트 공유):
- `start`/`end`(YYYY-MM-DD)가 **둘 다** 유효하고 `start ≤ end`면 사용자 지정: `[start_KST, (end+1일)_KST)`.
- 아니면 `period`(`week`|`month`) + `anchor`로 `period_bounds(period, anchor)`.

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_aggregate.py` 끝에 추가

```python
def test_history_context_week_period(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="in", session_id="s1", ts="2026-06-09T01:00:00Z", cost_usd=2.0)  # 6/9 주 안
    _msg(conn, dedup_key="out", session_id="s2", ts="2026-06-20T01:00:00Z", cost_usd=9.0)  # 주 밖
    ctx = history_context(conn, _ANCHOR_613, "", "date_desc", now_kst=_NOW_613, period="week")
    assert ctx["period"] == "week"
    assert ctx["total"] == 2.0                       # 6/8~6/14 주만
    assert ctx["period_label"] == "2026-06-08 ~ 06-14"


def test_history_context_custom_range(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-12T01:00:00Z", cost_usd=3.0)
    _msg(conn, dedup_key="b", session_id="s2", ts="2026-06-30T01:00:00Z", cost_usd=7.0)
    _msg(conn, dedup_key="c", session_id="s3", ts="2026-06-05T01:00:00Z", cost_usd=5.0)  # 범위 밖
    ctx = history_context(conn, _ANCHOR_613, "", "date_desc", now_kst=_NOW_613,
                          start="2026-06-12", end="2026-06-30")
    assert ctx["total"] == 10.0                      # 6/12~6/30 (6/5 제외)
    assert ctx["period_label"] == "2026-06-12 ~ 2026-06-30"
    assert ctx["custom"] is True


def test_history_context_invalid_range_falls_back_to_month(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", ts="2026-06-12T01:00:00Z", cost_usd=3.0)
    # start>end → 폴백(월간)
    ctx = history_context(conn, _ANCHOR_613, "", "date_desc", now_kst=_NOW_613,
                          start="2026-06-30", end="2026-06-01")
    assert ctx["custom"] is False
    assert ctx["period_label"] == "2026-06"


def test_models_context_week_period(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "none.json"))
    conn = connect(":memory:")
    _msg(conn, dedup_key="a", session_id="s1", model="claude-opus-4-8",
         ts="2026-06-09T10:00:00Z", cost_usd=8.0)                       # 주 안
    _msg(conn, dedup_key="b", session_id="s2", model="claude-haiku-4-5",
         ts="2026-06-20T10:00:00Z", cost_usd=2.0)                       # 주 밖
    ctx = models_context(conn, _ANCHOR_613, "", now_kst=_NOW_613, period="week")
    assert ctx["total"] == 8.0
    assert ctx["period_label"] == "2026-06-08 ~ 06-14"
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k "week_period or custom_range or invalid_range" -v`
Expected: FAIL — `history_context() got an unexpected keyword argument 'period'`

- [ ] **Step 3: 구현** — `tokenomy/web/views.py`

`models_context`/`history_context` 위에 공유 헬퍼 추가:

```python
def _resolve_range(anchor_kst: datetime, period: str, start: str | None, end: str | None):
    """조회 기간 [start, nxt)와 표시 메타를 해석한다.

    우선순위: 유효한 사용자 지정(start≤end) > period(week/month) + anchor.
    반환: (start_dt, nxt_dt, label, period, custom)
    """
    s = _parse_date(start)
    e = _parse_date(end)
    if s and e and s <= e:
        nxt = e + timedelta(days=1)
        label = f"{s.strftime('%Y-%m-%d')} ~ {e.strftime('%Y-%m-%d')}"
        return s, nxt, label, period, True
    period = period if period in ("week", "month") else "month"
    start_dt, nxt_dt, label = period_bounds(period, anchor_kst)
    return start_dt, nxt_dt, label, period, False


def _parse_date(value: str | None) -> datetime | None:
    """YYYY-MM-DD → KST 자정. 빈/오류 → None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=KST)
    except ValueError:
        return None
```

`history_context`를 교체:

```python
def history_context(conn, anchor_kst: datetime, provider: str, sort: str,
                    now_kst: datetime | None = None, *,
                    period: str = "month", start: str | None = None,
                    end: str | None = None) -> dict:
    """내역 — 날짜→폴더→세션 트리. 주/월 기간 또는 사용자 지정 [start, end]."""
    now = now_kst or datetime.now(KST)
    config = load_config()
    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    s, nxt, label, period, custom = _resolve_range(anchor_kst, period, start, end)
    rows = by_day_session(conn, provider or None, start=s, nxt=nxt)
    tree = build_date_tree(rows, sort)
    return {
        "active_nav": "history",
        "user_label": user_label(config),
        "provider": provider, "sort": sort,
        "period": period, "custom": custom,
        "anchor": anchor_kst.strftime("%Y-%m-%d"),
        "start": start or "", "end": end or "",
        "month": now.strftime("%Y-%m"),
        "last_ts": last["t"] if last and last["t"] else None,
        "period_label": label,
        "prev_anchor": (s - timedelta(days=1)).strftime("%Y-%m-%d"),
        "next_anchor": nxt.strftime("%Y-%m-%d"),
        "has_next": nxt <= now,
        "tree": tree,
        "count": len(rows),
        "total": round(sum(r.cost for r in rows), 4),
    }
```

`models_context`를 교체:

```python
def models_context(conn, anchor_kst: datetime, provider: str,
                   now_kst: datetime | None = None, *,
                   period: str = "month", start: str | None = None,
                   end: str | None = None) -> dict:
    """모델별 사용/비용. 주/월 기간 또는 사용자 지정 [start, end]. 행에 비중%(share)."""
    now = now_kst or datetime.now(KST)
    config = load_config()
    s, nxt, label, period, custom = _resolve_range(anchor_kst, period, start, end)
    rows = by_model(conn, provider or None, s, nxt)
    total = round(sum(m.cost for m in rows), 4)
    table = [
        {"model": m.model or "(unknown)", "cost": m.cost,
         "share": round(m.cost / total * 100, 1) if total else 0.0,
         "sessions": m.sessions, "cache_ratio": m.cache_ratio,
         "input_tokens": m.input_tokens, "output_tokens": m.output_tokens,
         "cache_creation": m.cache_creation, "cache_read": m.cache_read}
        for m in rows
    ]
    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    return {
        "active_nav": "models", "user_label": user_label(config),
        "provider": provider, "rows": table, "count": len(table), "total": total,
        "period": period, "custom": custom,
        "period_label": label,
        "anchor": anchor_kst.strftime("%Y-%m-%d"),
        "start": start or "", "end": end or "",
        "prev_anchor": (s - timedelta(days=1)).strftime("%Y-%m-%d"),
        "next_anchor": nxt.strftime("%Y-%m-%d"),
        "has_next": nxt <= now,
        "month": now.strftime("%Y-%m"),
        "last_ts": last["t"] if last and last["t"] else None,
    }
```

> `month_bounds`는 이 두 함수에서 더 이상 직접 쓰지 않지만 import는 유지(다른 곳 사용). `period_bounds`를 import에 추가(Task 6에서 이미 추가함).

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k "history_context or models_context" -v`
Expected: PASS (신규 + 기존 nav/shape — 기존은 `period`/`start` 미지정이라 월간 폴백으로 동일 동작)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/views.py tests/test_aggregate.py
git commit -m "feat(views): 내역·모델별 주/월 기간 + 사용자 지정 구간 조회"
```

---

### Task 9: `app.py` — 라우트 파라미터 검증

**Files:**
- Modify: `tokenomy/web/app.py:84-112` (`history_view`, `models_view`)
- Test: `tests/test_web.py`

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_web.py` 끝에 추가

```python
def test_history_week_period_param(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,cost_usd,priced) "
                 "VALUES ('a','claude','s1','myproj','2026-06-09T01:00:00Z',2.0,1)")
    conn.commit()
    r = client.get("/history?anchor=2026-06-13&period=week")
    assert r.status_code == 200
    assert "2026-06-08 ~ 06-14" in r.text


def test_history_custom_range_param(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,cost_usd,priced) "
                 "VALUES ('a','claude','s1','myproj','2026-06-12T01:00:00Z',3.0,1)")
    conn.commit()
    r = client.get("/history?start=2026-06-12&end=2026-06-30")
    assert r.status_code == 200
    assert "2026-06-12 ~ 2026-06-30" in r.text


def test_history_bad_period_falls_back(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/history?period=decade&start=nonsense")
    assert r.status_code == 200                      # 크래시 없이 월간 폴백


def test_models_week_period_param(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,ts,model,cost_usd,priced) "
                 "VALUES ('a','claude','s1','2026-06-09T10:00:00Z','claude-opus-4-8',8.0,1)")
    conn.commit()
    r = client.get("/models?anchor=2026-06-13&period=week")
    assert r.status_code == 200
    assert "2026-06-08 ~ 06-14" in r.text
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -k "week_period or custom_range or bad_period" -v`
Expected: FAIL — 라우트가 `period`/`start`/`end`를 무시(월간 그대로)

- [ ] **Step 3: 구현** — `tokenomy/web/app.py`

`_HISTORY_SORTS` 아래에 상수 추가:

```python
_PERIODS = ("week", "month")
```

`history_view` 시그니처·호출 교체:

```python
@app.get("/history")
def history_view(request: Request, anchor: str | None = None, provider: str = "",
                 sort: str | None = None, period: str | None = None,
                 start: str | None = None, end: str | None = None,
                 partial: str | None = None, notice: str | None = None):
    provider = provider if provider in PROVIDERS else ""
    sort = sort if sort in _HISTORY_SORTS else "date_desc"
    period = period if period in _PERIODS else "month"
    conn = connect()
    hx_partial = (request.headers.get("HX-Request") == "true"
                  and request.headers.get("HX-History-Restore-Request") != "true")
    is_partial = partial == "1" or hx_partial
    update_tag = None if is_partial else check_update(conn)
    ctx = history_context(conn, _parse_anchor(anchor), provider, sort,
                          period=period, start=start, end=end)
    template = "_history_body.html" if is_partial else "history.html"
    return templates.TemplateResponse(
        request, template, {**ctx, "notice": notice, "update_tag": update_tag},
    )
```

`models_view` 시그니처·호출 교체:

```python
@app.get("/models")
def models_view(request: Request, anchor: str | None = None, provider: str = "",
                period: str | None = None, start: str | None = None,
                end: str | None = None, notice: str | None = None):
    provider = provider if provider in PROVIDERS else ""
    period = period if period in _PERIODS else "month"
    conn = connect()
    update_tag = check_update(conn)
    ctx = models_context(conn, _parse_anchor(anchor), provider,
                         period=period, start=start, end=end)
    return templates.TemplateResponse(
        request, "models.html",
        {**ctx, "notice": notice, "update_tag": update_tag},
    )
```

> `start`/`end`는 views의 `_resolve_range`/`_parse_date`가 검증하므로 라우트는 화이트리스트(`period`)만 처리하고 날짜는 그대로 넘긴다(얇은 라우트 유지).

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/app.py tests/test_web.py
git commit -m "feat(web): /history·/models에 period·start·end 파라미터 검증"
```

---

### Task 10: 템플릿 — 주/월 토글 + 날짜 범위 컨트롤

**Files:**
- Modify: `tokenomy/web/templates/_history_body.html:1-27`
- Modify: `tokenomy/web/templates/models.html:6-17`
- Test: `tests/test_web.py`

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_web.py` 끝에 추가

```python
def test_history_has_period_toggle_and_range(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/history")
    assert r.status_code == 200
    assert 'name="period"' in r.text                 # 주/월 토글
    assert 'name="start"' in r.text and 'name="end"' in r.text   # 날짜 범위 입력


def test_models_has_period_toggle_and_range(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/models")
    assert r.status_code == 200
    assert 'name="period"' in r.text
    assert 'name="start"' in r.text and 'name="end"' in r.text
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -k "period_toggle_and_range" -v`
Expected: FAIL — 입력 미존재

- [ ] **Step 3: 구현**

`tokenomy/web/templates/_history_body.html` — `period-nav` div(2~6행)와 filters form을 아래로 교체(전체 1~27행 영역):

```html
<div class="card-head">
  <div class="period-nav">
    <a class="btn" href="/history?anchor={{ prev_anchor }}&provider={{ provider }}&sort={{ sort }}&period={{ period }}">‹ 이전</a>
    <span class="label">{{ period_label }}</span>
    {% if has_next %}<a class="btn" href="/history?anchor={{ next_anchor }}&provider={{ provider }}&sort={{ sort }}&period={{ period }}">다음 ›</a>{% endif %}
  </div>

  {# 컨트롤+표 전체를 #history-body 한 조각으로 swap → 필터 변경 후에도 기간 네비가 현재 상태 반영 #}
  <form class="filters" hx-get="/history" hx-target="#history-body"
        hx-swap="innerHTML" hx-trigger="change" hx-push-url="true"
        hx-indicator="#history-loading">
    <input type="hidden" name="anchor" value="{{ anchor }}">
    <select id="period-filter" name="period" aria-label="기간 단위">
      <option value="month" {{ 'selected' if period == 'month' }}>월간</option>
      <option value="week" {{ 'selected' if period == 'week' }}>주간</option>
    </select>
    <select id="provider-filter" name="provider" aria-label="AI 필터">
      <option value="" {{ 'selected' if provider == '' }}>전체</option>
      <option value="claude" {{ 'selected' if provider == 'claude' }}>Claude</option>
      <option value="codex" {{ 'selected' if provider == 'codex' }}>Codex</option>
    </select>
    <select id="sort-filter" name="sort" aria-label="정렬">
      {% set sort_opts = [('date_desc','날짜 최신순'),('date_asc','날짜 오래된순'),('day_cost','일별 지출많은순')] %}
      {% for val, lbl in sort_opts %}
      <option value="{{ val }}" {{ 'selected' if sort == val }}>{{ lbl }}</option>
      {% endfor %}
    </select>
    <span class="range">
      <input type="date" name="start" value="{{ start }}" aria-label="시작일">
      ~
      <input type="date" name="end" value="{{ end }}" aria-label="종료일">
    </span>
    <button type="button" class="btn" id="toggle-all" data-collapsed="false">모두 접기</button>
    <span id="history-loading" class="htmx-indicator muted">갱신 중…</span>
  </form>
</div>

{% include "_history_rows.html" %}
```

`tokenomy/web/templates/models.html` — `card-head`(6~17행)를 아래로 교체:

```html
  <div class="card-head">
    <div class="period-nav">
      <a class="btn" href="/models?anchor={{ prev_anchor }}&provider={{ provider }}&period={{ period }}">‹ 이전</a>
      <span class="label">{{ period_label }}</span>
      {% if has_next %}<a class="btn" href="/models?anchor={{ next_anchor }}&provider={{ provider }}&period={{ period }}">다음 ›</a>{% endif %}
    </div>
    <form class="filters" method="get" action="/models">
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
      <a href="/models?anchor={{ anchor }}&period={{ period }}" class="{{ 'on' if provider == '' }}">전체</a>
      <a href="/models?anchor={{ anchor }}&period={{ period }}&provider=claude" class="{{ 'on' if provider == 'claude' }}">Claude</a>
      <a href="/models?anchor={{ anchor }}&period={{ period }}&provider=codex" class="{{ 'on' if provider == 'codex' }}">Codex</a>
    </nav>
  </div>
```

> CSS: `.range` 정렬용 스타일이 없어도 입력은 동작한다. 시각 정렬이 필요하면 `static/src/input.css`의 `@layer components`에 `.range { display:inline-flex; align-items:center; gap:.25rem; }`를 추가하고 `.\build_css.ps1` 후 `static/app.css`를 커밋한다(선택).

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/templates/_history_body.html tokenomy/web/templates/models.html tests/test_web.py
git commit -m "feat(web): 내역·모델별 주/월 토글 + 날짜 범위 컨트롤"
```

---

## 마무리 검증

- [ ] **전체 테스트**

Run: `.venv\Scripts\python -m pytest`
Expected: PASS (포트 점유로 인한 `test_launcher` 2건 실패는 알려진 환경 이슈 — 회귀 아님)

- [ ] **수동 스모크(선택)**

```powershell
.venv\Scripts\python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765
```
설정에서 도입일 `2026-06-12` 저장 → 대시보드에서 Claude 카드 기간(6/12~)과 Codex 주차(3주차) 확인 → 내역에서 주/월 토글·날짜 범위 확인.

- [ ] **문서 동기화(선택)** — 동작 확정 후 `CLAUDE.md` 핵심 게시에 "Codex는 주간 누적, Claude는 월간; `budget_start`로 도입일 clamp" 한 줄 추가 검토.

---

## 자기검토 메모 (작성자 확인 완료)

- **스펙 커버리지:** §1 설정→Task 1·2 / §2 번다운(Claude clamp·Codex carryover)→Task 3·4·5 / §3 대시보드→Task 6·7 / §4 구간 조회→Task 8·9·10 / §5 edge(월 첫 부분주·월 경계)→Task 3·5 테스트로 고정 / §7 테스트→각 Task TDD. 누락 없음.
- **타입 일관성:** `budget_start_kst`/`effective_month_start`/`week_count`/`codex_burndown`/`CodexBurndown`/`_resolve_range` 시그니처가 호출부와 일치. `Burndown` 필드명은 기존 그대로 재사용(`days_in_month` 등 — 기간 의미만 일반화).
- **하위호환:** `budget_start` 미설정·`period`/`start` 미지정 시 모든 기존 동작 보존(Task 4·8 테스트로 고정). `combined_burndown`은 미변경.
- **회귀 처리:** 통합 바 텍스트·`ctx["cards"]` 참조 기존 테스트를 Task 6·7에서 명시적으로 갱신.
