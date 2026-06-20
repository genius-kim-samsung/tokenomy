# 수동 예산 제거 · 공식 사용량 우선 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 수동 월 예산 입력과 그에 딸린 번다운 엔진을 제거하고, 공식 사용량 API 취득을 default-on으로 만들어 한도/잔여의 정본으로 삼는다.

**Architecture:** 공식 사용량(`official_view`+`lens`)을 유일한 예측 엔진으로 통일한다. 공식 데이터가 없으면(취득 off·미성공·한도 미제공 계정) "사용량 전용" 화면으로 폴백한다. 어떤 provider를 호출/표시할지는 새 `tracked_providers` 설정이 게이트하며, 첫 실행 시 크레덴셜 파일 존재로 시드한다.

**Tech Stack:** Python 3(stdlib: sqlite3/json/pathlib/datetime/urllib), FastAPI + Jinja2, pytest. 런타임 의존성 추가 없음.

## Global Constraints

- 모든 모듈 상단에 `from __future__ import annotations`. docstring·주석은 한국어.
- 계층 분리 유지: 라우트(app.py, 얇게) ↔ 화면 조립(views.py) ↔ 집계(aggregate.py) ↔ 적재/모델(db.py, budget.py).
- stdlib 우선. 아웃바운드 호출은 `official_fetch.py` 한 곳만(타임아웃 ≤3s, 백오프 없음).
- `TOKENOMY_SKIP_OFFICIAL_FETCH` env 비상구와 `min_interval_minutes` throttle은 **유지**한다.
- PII(토큰/account_id/email) 미저장. 프라이버시 경계(토큰 메타 + Codex 첫 프롬프트 120자 발췌만) 유지.
- 테스트 실행: `.venv\Scripts\python -m pytest`. 워크트리엔 .venv가 없으므로 **메인 repo의 `.venv\Scripts\python`** 을 워크트리 cwd에서 실행한다.
- 단일 사용자 로컬 앱 — config 마이그레이션은 "옛 키 무시"로 충분(이관 없음).

## 용어(CONTEXT.md 참조)

- **엔터프라이즈/종량제** — 공식 API가 USD 한도를 주는 형태. **개인 구독제** — rate-window(%)만 주는 정액제.
- **공식 사용량** — 한도/잔여의 정본. **사용량 전용 view** — 공식 데이터 없을 때 폴백.
- **tracked providers** — 사용자가 쓴다고 선언한 provider 집합. 호출/표시를 게이트.

## 파일 구조(생성/수정 맵)

| 파일 | 책임 | 변경 |
|---|---|---|
| `tokenomy/paths.py` | 경로/크레덴셜 위치 | **수정** — 크레덴셜 경로 상수 + `creds_present()` 추가 |
| `tokenomy/budget.py` | 설정 모델 | **수정** — Budget/burndown 모델 제거, `tracked_providers()` 추가, `official_fetch_settings` 축소 |
| `tokenomy/official_fetch.py` | 공식 취득 | **수정** — tracked-provider 게이트, 크레덴셜 부재 시 silent skip |
| `tokenomy/aggregate.py` | 집계/예측 | **수정** — 번다운 엔진 제거, `official_view`/`daily_series`/`insights` 예산 분리, `month_spend()` 추가 |
| `tokenomy/web/views.py` | 화면 조립 | **수정** — 번다운 조립 제거, 사용량+공식만 |
| `tokenomy/web/app.py` | 라우트 | **수정** — settings GET/POST, official_refresh 게이트 |
| `tokenomy/cli.py` | 터미널 report | **수정** — 번다운 의존 제거 |
| `tokenomy/web/templates/overview.html` | 대시보드 | **수정** — 배너/번다운 카드/통합바 제거 |
| `tokenomy/web/templates/_trend_chart.html` | 추세 차트 | **수정** — pace/budget 선 제거 |
| `tokenomy/web/templates/settings.html` | 설정 화면 | **수정** — 예산 입력 제거, tracked_providers 선택 |
| `config/tokenomy.config.example.json` | 예제 설정 | **수정** |
| `tests/test_budget.py` · `test_aggregate.py` · `test_web.py` · `test_official_fetch.py` | 테스트 | **수정** |
| `CLAUDE.md` · `README.md` · `README.en.md` · `AGENTS.md` | 문서 | **수정** |

---

## Task 1: 크레덴셜 감지 + `tracked_providers` 설정

**Files:**
- Modify: `tokenomy/paths.py` (크레덴셜 경로 상수 + `creds_present`)
- Modify: `tokenomy/budget.py` (`tracked_providers`, `load_config` 기본값)
- Test: `tests/test_budget.py`

**Interfaces:**
- Produces:
  - `tokenomy.paths.CLAUDE_CREDS: Path`, `tokenomy.paths.CODEX_AUTH: Path`
  - `tokenomy.paths.creds_present(provider: str) -> bool` — `provider`의 크레덴셜 파일이 존재하면 True
  - `tokenomy.budget.tracked_providers(config: dict) -> list[str]` — config의 유효 리스트, 없으면 크레덴셜 존재로 시드. 항상 `PROVIDERS` 순서 유지, 알 수 없는 값 제거

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_budget.py` 끝에 추가

```python
from tokenomy.budget import tracked_providers
from tokenomy import paths


def test_creds_present_detects_files(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CLAUDE_CREDS", tmp_path / ".claude" / ".credentials.json")
    monkeypatch.setattr(paths, "CODEX_AUTH", tmp_path / ".codex" / "auth.json")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    assert paths.creds_present("claude") is True
    assert paths.creds_present("codex") is False


def test_tracked_providers_explicit_list_wins():
    assert tracked_providers({"tracked_providers": ["codex"]}) == ["codex"]
    # 알 수 없는 값 제거 + PROVIDERS 순서 정규화
    assert tracked_providers({"tracked_providers": ["codex", "x", "claude"]}) == ["claude", "codex"]


def test_tracked_providers_seeds_from_creds_when_absent(monkeypatch):
    import tokenomy.budget as b
    monkeypatch.setattr(b, "creds_present", lambda p: p == "claude")
    assert tracked_providers({}) == ["claude"]
    assert tracked_providers({"tracked_providers": []}) == ["claude"]   # 빈 리스트도 시드
```

- [ ] **Step 2: 실패 확인**

Run: `..\..\..\.venv\Scripts\python -m pytest tests/test_budget.py -k "tracked or creds" -v`
Expected: FAIL (`ImportError: cannot import name 'tracked_providers'`)

- [ ] **Step 3: `paths.py`에 크레덴셜 감지 추가** — `tokenomy/paths.py` 끝에

```python
# 공식 사용량 취득용 로컬 OAuth 크레덴셜 위치(읽기 전용). 존재 여부 감지에만 쓴다.
CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"
CODEX_AUTH = Path.home() / ".codex" / "auth.json"

_CREDS = {"claude": CLAUDE_CREDS, "codex": CODEX_AUTH}


def creds_present(provider: str) -> bool:
    """provider의 로컬 크레덴셜 파일이 존재하면 True(내용 검증은 안 함)."""
    p = _CREDS.get(provider)
    return bool(p and p.exists())
```

(`paths.py` 상단에 `from pathlib import Path`가 이미 있는지 확인하고 없으면 추가.)

- [ ] **Step 4: `budget.py`에 `tracked_providers` 추가 + 기본값 갱신**

`tokenomy/budget.py` 상단 import에 추가:

```python
from tokenomy.paths import creds_present
```

`load_config`의 `base` 기본 dict를 교체(예산 키 제거, tracked_providers 추가, official_fetch에서 enabled/provider 토글 제거):

```python
    base = {"user_label": _default_label(),
            "tracked_providers": None,           # None → 첫 호출 시 크레덴셜로 시드
            "credit_to_usd": 0.04,
            "official_fetch": {"min_interval_minutes": 5},
            "pricing_overrides": {}}
    p = _config_path(path)
    if not p.exists():
        return base
    loaded = json.loads(p.read_text(encoding="utf-8"))
    base.update(loaded)                       # 레거시 키(budget/budget_start/official_fetch.enabled)는 들어와도 무시됨
    return base
```

함수 추가(파일 끝):

```python
def tracked_providers(config: dict) -> list[str]:
    """사용자가 쓴다고 선언한 provider 목록. 없거나 비면 크레덴셜 존재로 시드한다.

    config['tracked_providers']가 유효한 리스트면 PROVIDERS 순서로 정규화(알 수 없는 값 제거).
    비었거나 None이면 크레덴셜 파일이 있는 provider로 시드(무설정 첫 실행이 대개 정답).
    """
    from tokenomy.aggregate import PROVIDERS
    raw = config.get("tracked_providers")
    if isinstance(raw, list):
        sel = [p for p in PROVIDERS if p in raw]
        if sel:
            return sel
    return [p for p in PROVIDERS if creds_present(p)]
```

> 참고: `Budget`/`budget_from_config`/`budget_start_kst`/`weekly_codex_limit`은 **Task 7에서** 제거한다(아직 소비처가 있어 지금 지우면 빨개짐).

- [ ] **Step 5: 통과 확인**

Run: `..\..\..\.venv\Scripts\python -m pytest tests/test_budget.py -k "tracked or creds" -v`
Expected: PASS (3 passed)

- [ ] **Step 6: 커밋**

```bash
git add tokenomy/paths.py tokenomy/budget.py tests/test_budget.py
git commit -m "feat(config): tracked_providers + 크레덴셜 감지(첫 실행 시드)"
```

---

## Task 2: 공식 취득 default-on + tracked-provider 게이트

**Files:**
- Modify: `tokenomy/budget.py` (`official_fetch_settings`)
- Modify: `tokenomy/official_fetch.py` (게이트 + 크레덴셜 부재 silent skip)
- Modify: `config/tokenomy.config.example.json`
- Test: `tests/test_budget.py`, `tests/test_official_fetch.py`

**Interfaces:**
- Consumes: `tracked_providers()`, `paths.creds_present()` (Task 1)
- Produces:
  - `official_fetch_settings(config) -> {"min_interval_minutes": int}` (enabled/claude/codex 키 제거)
  - `fetch_provider(...)` 동작: provider가 tracked가 아니면 `disabled`; tracked인데 크레덴셜 파일 부재면 `disabled`(note="creds_absent", state 미기록); 파일 있는데 토큰 무효면 기존대로 `auth_error`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_budget.py`의 `official_fetch_settings` 테스트를 새 시그니처로 교체:

```python
def test_official_fetch_settings_defaults():
    s = official_fetch_settings({})
    assert s == {"min_interval_minutes": 5}


def test_official_fetch_settings_bad_interval_falls_back():
    assert official_fetch_settings({"official_fetch": {"min_interval_minutes": "x"}})["min_interval_minutes"] == 5
    assert official_fetch_settings({"official_fetch": {"min_interval_minutes": -3}})["min_interval_minutes"] == 5
    assert official_fetch_settings({"official_fetch": {"min_interval_minutes": 9}})["min_interval_minutes"] == 9
```

(`test_official_fetch_settings_reads_user_values`, `_partial_keeps_defaults`, `_defaults_optin_off` 삭제.)

`tests/test_official_fetch.py`에 추가:

```python
def test_fetch_skips_untracked_provider(tmp_path, monkeypatch):
    import tokenomy.official_fetch as of
    conn = _memory_conn()   # 기존 헬퍼 사용; 없으면 db.connect(tmp_path)
    cfg = {"tracked_providers": ["claude"]}
    res = of.fetch_provider("codex", now_kst=_NOW, config=cfg, conn=conn,
                            urlopen=_boom)   # urlopen 호출되면 실패해야 함
    assert res.status == "disabled"


def test_fetch_silent_skip_when_creds_absent(tmp_path, monkeypatch):
    import tokenomy.official_fetch as of
    from tokenomy import paths
    monkeypatch.setattr(paths, "creds_present", lambda p: False)
    conn = _memory_conn()
    res = of.fetch_provider("claude", now_kst=_NOW, config={"tracked_providers": ["claude"]},
                            conn=conn, urlopen=_boom)
    assert res.status == "disabled"
    assert res.note == "creds_absent"
    # state는 기록되지 않아야 함(거짓 auth_error 방지)
    from tokenomy.db import get_fetch_state
    assert get_fetch_state(conn, "claude") is None
```

(`_boom`은 호출 시 예외를 던지는 가짜 urlopen; `_NOW`/`_memory_conn`은 기존 테스트 헬퍼 컨벤션을 따른다. 파일 상단 헬퍼가 없으면 `test_official_fetch.py`의 기존 픽스처를 재사용하도록 맞춘다.)

- [ ] **Step 2: 실패 확인**

Run: `..\..\..\.venv\Scripts\python -m pytest tests/test_official_fetch.py tests/test_budget.py -k "official or fetch or tracked" -v`
Expected: FAIL

- [ ] **Step 3: `official_fetch_settings` 축소** — `tokenomy/budget.py`

```python
def official_fetch_settings(config: dict) -> dict:
    """공식 사용량 자동 취득 설정. 현재는 throttle 간격만 — on/off·provider 게이트는
    tracked_providers가 담당한다. 누락·오설정은 기본 5분으로 폴백한다."""
    raw = config.get("official_fetch") or {}
    try:
        mi = int(raw.get("min_interval_minutes", 5))
    except (TypeError, ValueError):
        mi = 5
    return {"min_interval_minutes": mi if mi > 0 else 5}
```

- [ ] **Step 4: `fetch_provider` 게이트 교체** — `tokenomy/official_fetch.py`

상단 import 정리(크레덴셜 경로는 paths에서):

```python
from tokenomy.paths import CLAUDE_CREDS, CODEX_AUTH, creds_present
from tokenomy.budget import credit_to_usd, official_fetch_settings, tracked_providers
```

(기존 `CLAUDE_CREDS = Path.home()...`/`CODEX_AUTH = ...` 로컬 정의 2줄 삭제.)

`fetch_provider`의 1) 옵트인 게이트 블록 교체:

```python
    # 1) 게이트 — env-skip / 미선택 provider / 크레덴셜 부재는 시도 없이 반환
    settings = official_fetch_settings(config)
    if os.environ.get("TOKENOMY_SKIP_OFFICIAL_FETCH"):
        return FetchResult(provider, "disabled", "skip(env)")
    if provider not in tracked_providers(config):
        return FetchResult(provider, "disabled")
    if not creds_present(provider):
        # 선언했지만 로그인 안 된 상태 — 거짓 auth_error를 남기지 않고 조용히 skip
        return FetchResult(provider, "disabled", "creds_absent")
```

(이후 throttle·토큰읽기·GET·파싱·적재 블록은 그대로. 토큰 파일이 존재하지만 스키마가 깨졌거나 토큰이 빈 경우는 기존 `_read_*`의 `AuthError` 경로가 그대로 `auth_error`를 기록한다.)

- [ ] **Step 5: 예제 config 갱신** — `config/tokenomy.config.example.json`

```json
{
  "user_label": "me",
  "tracked_providers": ["claude", "codex"],
  "credit_to_usd": 0.04,
  "official_fetch": {
    "min_interval_minutes": 5
  },
  "pricing_overrides": {}
}
```

- [ ] **Step 6: 통과 확인**

Run: `..\..\..\.venv\Scripts\python -m pytest tests/test_official_fetch.py tests/test_budget.py -v`
Expected: PASS (새/수정 테스트 통과, 삭제 테스트 부재)

- [ ] **Step 7: 커밋**

```bash
git add tokenomy/budget.py tokenomy/official_fetch.py config/tokenomy.config.example.json tests/test_budget.py tests/test_official_fetch.py
git commit -m "feat(official): default-on + tracked-provider 게이트, 크레덴셜 부재 silent skip"
```

---

## Task 3: `official_view`·`daily_series`·`insights`를 예산에서 분리

**Files:**
- Modify: `tokenomy/aggregate.py`
- Test: `tests/test_aggregate.py`

**Interfaces:**
- Produces (새 시그니처):
  - `official_view(conn, provider, now_kst, credit_to_usd, *) -> OfficialView` (budget·budget_start 인자 제거). Codex `weekly_limit = period_limit/4 if period_limit else None`
  - `daily_series(conn, provider, now_kst) -> list[DayPoint]` (budget_start 인자 제거 — 달력 월 기준)
  - `insights(conn, now_kst, provider, cov) -> list[Insight]` (bd 인자 제거; 예산 초과 카드 제거)
  - `month_spend(conn, provider, now_kst) -> float` (신규 — provider(또는 None=전체) 이번 달 cost_usd 합)

- [ ] **Step 1: 실패 테스트 작성/수정** — `tests/test_aggregate.py`

```python
def test_official_view_no_budget_arg(monkeypatch):
    conn = _conn_with_official_codex_monthly(limit_usd=80)  # 기존 official 픽스처 활용
    ov = official_view(conn, "codex", NOW, 0.04)
    assert ov.weekly_limit_usd == 20.0   # period_limit 80 ÷ 4


def test_daily_series_calendar_month():
    conn = _conn()
    _add(conn, "claude", "2026-06-01", 1.0)
    _add(conn, "claude", "2026-06-10", 2.0)
    pts = daily_series(conn, "claude", _NOW_STATUS)   # 6/1부터 누적
    assert pts[-1].cum == 3.0


def test_month_spend_sums_current_month():
    conn = _conn()
    _add(conn, "claude", "2026-06-05", 4.0)
    _add(conn, "claude", "2026-05-30", 9.0)   # 다른 달 제외
    assert month_spend(conn, "claude", NOW) == 4.0


def test_insights_no_budget_overrun_card(monkeypatch):
    conn = _conn()
    cards = insights(conn, _NOW_STATUS, None, cov=None)
    assert any(c.level == "info" for c in cards)   # 빈 신호 placeholder
    # 더 이상 'bd' 인자 없음 — TypeError 안 나야 함
```

기존 `test_daily_series_clamps_to_budget_start`는 삭제(budget_start 개념 제거). `official_view` 호출하는 기존 테스트의 인자에서 `budget`/`budget_start` 제거.

- [ ] **Step 2: 실패 확인**

Run: `..\..\..\.venv\Scripts\python -m pytest tests/test_aggregate.py -k "official_view or daily_series or month_spend or insights" -v`
Expected: FAIL

- [ ] **Step 3: `official_view` 시그니처/로직 수정** — `tokenomy/aggregate.py`

함수 시그니처에서 `budget: Budget` 인자 제거:

```python
def official_view(conn, provider: str, now_kst: datetime, credit_to_usd: float) -> OfficialView:
```

Codex 주간 한도 폴백(`elif budget.codex:` 분기) 제거 — 공식 period_limit만 사용:

```python
        # 주간 한도 = 공식 월 한도 ÷ 4(있을 때만). 예산 폴백 없음.
        if period_limit:
            weekly_limit = round(period_limit / 4, 4)
```

(`budget_start` 인자/`effective_month_start` 사용처가 official_view엔 없음 — 시그니처에서만 제거.)

- [ ] **Step 4: `daily_series`에서 budget_start 제거** — `tokenomy/aggregate.py`

```python
def daily_series(conn, provider: str | None, now_kst: datetime) -> list[DayPoint]:
    period_start, _ = month_bounds(now_kst)
    ...
```

(본문에서 `effective_month_start(now_kst, budget_start)` → `month_bounds(now_kst)[0]`로 교체. `period_end`는 기존대로.)

- [ ] **Step 5: `insights` 시그니처/로직 수정** — `tokenomy/aggregate.py`

```python
def insights(conn, now_kst: datetime, provider: str | None,
             cov: "CoverageReport | None" = None) -> list[Insight]:
```

본문에서 `bd` 사용 두 곳 교체:
- `elif cov is None and bd.unpriced_count:` 블록 → cov 없을 때는 rows에서 직접 미식별 건수 계산:

```python
    elif cov is None:
        unpriced = sum(1 for r in rows if not r["priced"])
        if unpriced:
            cards.append(Insight("warn", f"단가 미식별 {unpriced}건 — 비용 누락 가능"))
```

- `if bd.limit > 0 and bd.projected_month > bd.limit:` 예산 초과 카드 블록 **삭제**.

- [ ] **Step 6: `month_spend` 추가** — `tokenomy/aggregate.py` (`_month_rows` 근처)

```python
def month_spend(conn, provider: str | None, now_kst: datetime) -> float:
    """provider(또는 None=전체)의 이번 달(KST) cost_usd 합. 번다운 없이 총지출만."""
    return round(sum((r["cost_usd"] or 0) for r in _month_rows(conn, provider, now_kst)), 4)
```

- [ ] **Step 7: 통과 확인**

Run: `..\..\..\.venv\Scripts\python -m pytest tests/test_aggregate.py -k "official_view or daily_series or month_spend or insights" -v`
Expected: PASS

(이 시점에 `burndown`/`codex_burndown`은 아직 존재 — views.py가 다음 Task에서 끊는다. 전체 스위트는 Task 4 이후 green.)

- [ ] **Step 8: 커밋**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "refactor(aggregate): official_view/daily_series/insights 예산 분리 + month_spend"
```

---

## Task 4: `overview_context` + 대시보드 템플릿을 사용량+공식으로 재배선

**Files:**
- Modify: `tokenomy/web/views.py` (`overview_context`, `official_fetch_status`)
- Modify: `tokenomy/web/templates/overview.html`
- Modify: `tokenomy/web/templates/_trend_chart.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `month_spend`, `daily_series(no budget_start)`, `official_view(no budget)`, `insights(no bd)`, `tracked_providers`
- Produces: `overview_context` 반환 dict에서 제거 — `budget_configured`, `budget_start`, `claude_bd`, `codex_bd`, `combined_bd`, `both_budgeted`, `daily_pace`, `daily_budget`. 유지/추가 — `month_total`, `claude_official`, `codex_official`, `official_fetch`(enabled 키 없음), `trend_series`, `trend_totals`, `projects`, `sessions`, `insights`, `token_comp`, `has_data`, `tracked`(=tracked_providers 결과)

- [ ] **Step 1: 실패 테스트 작성/수정** — `tests/test_web.py`

```python
def test_dashboard_no_budget_banner(tmp_path, monkeypatch):
    # 예산 온보딩 배너가 더 이상 없어야 함
    client = _client(tmp_path, monkeypatch)
    html = client.get("/").text
    assert "예산을 설정하세요" not in html


def test_dashboard_shows_month_total(tmp_path, monkeypatch):
    client = _client_with_data(tmp_path, monkeypatch)   # 기존 데이터 픽스처
    html = client.get("/").text
    assert "이번 달 총지출" in html


def test_dashboard_no_burndown_cards(tmp_path, monkeypatch):
    client = _client_with_data(tmp_path, monkeypatch)
    html = client.get("/").text
    assert "AI별 사용 현황" not in html   # 번다운 카드 섹션 제거
```

삭제할 기존 테스트: `test_dashboard_shows_onboarding_when_no_budget`, `test_dashboard_hides_onboarding_when_budget_set`, `test_overview_shows_usage_ratio_when_both_budgeted`, `test_overview_hides_ratio_when_only_one_budgeted`, `test_ai_cards_use_text_status_and_labels`(번다운 카드 검증). `test_dashboard_has_settings_link_even_with_budget`는 budget 의존 제거 후 유지.

- [ ] **Step 2: 실패 확인**

Run: `..\..\..\.venv\Scripts\python -m pytest tests/test_web.py -k "dashboard or overview" -v`
Expected: FAIL

- [ ] **Step 3: `overview_context` 재작성** — `tokenomy/web/views.py`

import에서 번다운 심볼 제거, `month_spend`·`tracked_providers` 추가:

```python
from tokenomy.aggregate import (
    KST, DIM_COLUMNS, DateGroup, DaySessionRow, FolderGroup,
    by_day_session, by_dimension, by_project, by_session, daily_series,
    insights, month_bounds, month_spend, official_view, period_bounds,
    pricing_coverage, session_detail, sidechain_split, stacked_trend,
    token_composition,
)
from tokenomy.budget import (
    credit_to_usd, load_config, official_fetch_settings, tracked_providers, user_label,
)
```

`overview_context` 본문(번다운/예산 조립 제거):

```python
def overview_context(conn, sort: str, now_kst: datetime | None = None) -> dict:
    now = now_kst or datetime.now(KST)
    config = load_config()
    tracked = tracked_providers(config)

    # 공식 미러 패널(provider별) — USD 1차. 한도/잔여의 정본.
    ctu = credit_to_usd(config)
    claude_official = official_view(conn, "claude", now, ctu)
    codex_official = official_view(conn, "codex", now, ctu)

    month_total = month_spend(conn, None, now)

    projects = by_project(conn, None, now)
    projects.sort(key=_SORT_KEYS.get(sort, _SORT_KEYS["cost"]), reverse=True)
    projects = projects[:10]
    sessions = by_session(conn, None, now, limit_n=10)

    pricing = apply_pricing_overrides(load_pricing(), config.get("pricing_overrides"))
    cov = pricing_coverage(conn, pricing)
    coach = insights(conn, now, None, cov=cov)
    daily = daily_series(conn, None, now)

    trend_providers = [p for p in _TREND_STYLE if _provider_has_data(conn, p)]
    bands = stacked_trend(
        [(p, daily_series(conn, p, now)) for p in trend_providers]
    )
    trend_series = [
        {"label": _TREND_STYLE[b["provider"]][0],
         "color": _TREND_STYLE[b["provider"]][1],
         "fill": _TREND_STYLE[b["provider"]][2],
         "top": b["top"], "cum": b["cum"]}
        for b in bands
    ]
    trend_totals = bands[-1]["top"] if bands else [None for _ in daily]

    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    has_data = last is not None and last["t"] is not None
    token_comp = token_composition(conn, None, *month_bounds(now))

    return {
        "active_nav": "dashboard", "sort": sort,
        "user_label": user_label(config),
        "tracked": tracked,
        "month": now.strftime("%Y-%m"),
        "month_total": month_total,
        "claude_official": claude_official, "codex_official": codex_official,
        "official_fetch": official_fetch_status(conn, config),
        "claude_has_data": _provider_has_data(conn, "claude"),
        "codex_has_data": _provider_has_data(conn, "codex"),
        "projects": projects, "sessions": sessions, "insights": coach,
        "daily_labels": [p.day for p in daily],
        "trend_series": trend_series,
        "trend_totals": trend_totals,
        "last_ts": last["t"] if has_data else None,
        "token_comp": token_comp,
        "has_data": has_data,
    }
```

`official_fetch_status`에서 `enabled` 키 제거:

```python
def official_fetch_status(conn, config: dict) -> dict:
    """provider별 마지막 fetch 상태/안내(표시용). tracked provider만 순회."""
    out: dict = {}
    for p in tracked_providers(config):
        st = get_fetch_state(conn, p)
        status = st["last_status"] if st else None
        out[p] = {
            "last_status": status,
            "last_attempt_at": st["last_attempt_at"] if st else None,
            "last_error": st["last_error"] if st else None,
            "note": _remediation(p, status),
        }
    return out
```

(상단 import에 `from tokenomy.budget import tracked_providers` 추가 필요 — official_fetch_status도 사용.)

- [ ] **Step 4: `overview.html` 수정** — `tokenomy/web/templates/overview.html`

- 5~7행 예산 배너 **삭제**.
- 9~25행 "이번 달 총지출" 카드: `both_budgeted`/`combined_bd`/`budget_start` 분기 제거, 금액만:

```html
<section class="card">
  <h2>이번 달 총지출 <span class="muted">(전 AI 합산)</span></h2>
  {% if not has_data %}
    <p class="muted">데이터 없음 · 사이드바 [↻ 새로고침]을 누르세요</p>
  {% else %}
    <p class="total-num serif">${{ '{:,.2f}'.format(month_total) }}</p>
    <p class="disclaimer">ⓘ 공개 API 단가 기준 추정 · 이 머신 데이터만</p>
  {% endif %}
</section>
```

- 35~37행 공식 패널의 `{% if not official_fetch.enabled %}…{% endif %}` 안내 **삭제**(이제 항상 시도).
- 38행 `{% for p in ["claude", "codex"] %}` → `{% for p in tracked %}`로 변경(note 표시 루프). 마찬가지로 94~135행 "AI별 사용 현황" 번다운 카드 섹션 **전체 삭제**(`claude_bd`/`codex_bd` 참조 제거).

- [ ] **Step 5: `_trend_chart.html` 수정** — pace/budget 제거

7~8행 `trendPace`/`trendBudget` 선언과, 차트에서 두 선을 그리는 코드(`trendPace`/`trendBudget` 참조부)를 모두 삭제. 누적 밴드(`trend_series`)·총계(`trend_totals`)만 남긴다.

- [ ] **Step 6: 통과 확인**

Run: `..\..\..\.venv\Scripts\python -m pytest tests/test_web.py -v`
Expected: PASS (전체 web 스위트 green)

- [ ] **Step 7: 전체 스위트 확인(번다운 소비처 정리 완료)**

Run: `..\..\..\.venv\Scripts\python -m pytest`
Expected: 실패는 cli.py(`cmd_report`)의 번다운 사용만 — Task 5에서 해결. web/aggregate/budget green.

- [ ] **Step 8: 커밋**

```bash
git add tokenomy/web/views.py tokenomy/web/templates/overview.html tokenomy/web/templates/_trend_chart.html tests/test_web.py
git commit -m "refactor(web): 대시보드를 사용량+공식으로 재배선(번다운 카드/배너/pace선 제거)"
```

---

## Task 5: CLI report에서 번다운 제거

**Files:**
- Modify: `tokenomy/cli.py` (`cmd_report`, `_official_fetch_worker` 게이트)
- Test: `tests/test_cli.py`(있으면) 또는 `tests/test_web.py`의 cli 스모크

**Interfaces:**
- Consumes: `month_spend`, `official_view`, `tracked_providers`
- Produces: `cmd_report`가 번다운 없이 provider별 이번 달 총지출 + 공식 사용량 요약을 출력

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_cli.py`(없으면 생성)

```python
def test_report_runs_without_budget(capsys, tmp_path, monkeypatch):
    # cmd_report가 budget/burndown 없이 동작하고 provider별 총지출을 출력
    _seed_messages(tmp_path, monkeypatch)   # 기존 픽스처 컨벤션
    from tokenomy.cli import cmd_report
    cmd_report()
    out = capsys.readouterr().out
    assert "claude" in out.lower()
    assert "이번 달" in out or "총지출" in out
```

- [ ] **Step 2: 실패 확인**

Run: `..\..\..\.venv\Scripts\python -m pytest tests/test_cli.py -v`
Expected: FAIL

- [ ] **Step 3: `cmd_report` 재작성** — `tokenomy/cli.py`

import 교체:

```python
from tokenomy.aggregate import KST, by_project, by_session, month_spend, official_view, parse_ts, pricing_coverage
from tokenomy.budget import credit_to_usd, load_config, official_fetch_settings, tracked_providers, user_label
```

번다운 루프(98~113행 영역)를 provider별 총지출 + 공식 요약으로 교체:

```python
    config = load_config()
    ctu = credit_to_usd(config)
    for prov in tracked_providers(config):
        spent = month_spend(conn, prov, now)
        ov = official_view(conn, prov, now, ctu)
        line = f"{prov}: 이번 달 총지출 ${spent:,.2f}"
        if ov.period_limit_usd:
            line += f" · 공식 ${ov.period_used_usd:,.2f}/${ov.period_limit_usd:,.0f}"
        print(line)
```

`_official_fetch_worker`의 `official_fetch_settings(config)["enabled"]` 게이트 제거 — 항상 tracked provider에 대해 시도(단발·비차단):

```python
def _official_fetch_worker(config: dict, now_kst, *, connect_fn=connect) -> None:
    from tokenomy.official_fetch import fetch_provider
    conn = connect_fn()
    for p in tracked_providers(config):
        try:
            fetch_provider(p, now_kst=now_kst, config=config, conn=conn)
        except Exception:
            pass
```

`cmd_ingest`의 `if official_fetch_settings(config)["enabled"]:` 조건(68행 영역) **삭제** — 항상 데몬 스레드로 워커 기동(`TOKENOMY_SKIP_OFFICIAL_FETCH`/크레덴셜 부재는 워커 내부에서 걸러짐).

- [ ] **Step 4: 통과 확인**

Run: `..\..\..\.venv\Scripts\python -m pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/cli.py tests/test_cli.py
git commit -m "refactor(cli): report 번다운 제거 + 공식 취득 always-on 워커"
```

---

## Task 6: 설정 화면 — 예산 입력 제거, tracked_providers 선택

**Files:**
- Modify: `tokenomy/web/app.py` (`settings_get`, `settings_post`, `official_refresh`)
- Modify: `tokenomy/web/templates/settings.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `tracked_providers`, `official_fetch_settings`(min_interval만)
- Produces: `POST /settings`가 `tracked_providers`(체크박스) + `credit_to_usd` + `min_interval`만 저장. budget/budget_start/official_enabled 파라미터 제거. `official_refresh`는 tracked provider만 대상

- [ ] **Step 1: 실패 테스트 작성/수정** — `tests/test_web.py`

```python
def test_settings_post_writes_tracked_providers(tmp_path, monkeypatch):
    client, cfg_path = _settings_client(tmp_path, monkeypatch)
    client.post("/settings", data={"track_claude": "on", "min_interval": "7",
                                    "credit_to_usd": "0.05"})
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["tracked_providers"] == ["claude"]
    assert saved["official_fetch"]["min_interval_minutes"] == 7
    assert "budget" not in saved


def test_settings_get_has_provider_checkboxes(tmp_path, monkeypatch):
    client, _ = _settings_client(tmp_path, monkeypatch)
    html = client.get("/settings").text
    assert 'name="track_claude"' in html
    assert 'name="track_codex"' in html
    assert "월 예산" not in html
```

삭제할 기존 테스트: `test_settings_post_writes_config`(budget), `test_settings_post_invalid_number_falls_back_zero`, `test_settings_get_shows_budget_start_field`.

- [ ] **Step 2: 실패 확인**

Run: `..\..\..\.venv\Scripts\python -m pytest tests/test_web.py -k settings -v`
Expected: FAIL

- [ ] **Step 3: `settings_get` 수정** — `tokenomy/web/app.py`

import에서 `budget_from_config` 제거, `tracked_providers` 추가. 컨텍스트 교체:

```python
@app.get("/settings")
def settings_get(request: Request):
    config = load_config()
    conn = connect()
    last = conn.execute("SELECT MAX(ts) t FROM messages").fetchone()
    pricing = apply_pricing_overrides(load_pricing(), config.get("pricing_overrides"))
    ofs = official_fetch_settings(config)
    tracked = tracked_providers(config)
    official_states = {p: (dict(st) if (st := get_fetch_state(conn, p)) else None)
                       for p in PROVIDERS}
    return templates.TemplateResponse(
        request, "settings.html",
        {"tracked": tracked, "providers": list(PROVIDERS),
         "credit_to_usd": _credit_to_usd(config),
         "official_fetch": ofs, "official_states": official_states,
         "active_nav": "settings", "update_tag": check_update(conn),
         "last_ts": last["t"] if last and last["t"] else None,
         **coverage_card_context(conn, pricing)},
    )
```

- [ ] **Step 4: `settings_post` 수정** — `tokenomy/web/app.py`

```python
@app.post("/settings")
def settings_post(track_claude: str = Form(""), track_codex: str = Form(""),
                  credit_to_usd: str = Form(""), min_interval: str = Form("")):
    config = load_config()
    sel = [p for p, v in (("claude", track_claude), ("codex", track_codex)) if v]
    config["tracked_providers"] = sel
    ctu = _to_float(credit_to_usd)
    config["credit_to_usd"] = ctu if ctu > 0 else 0.04
    mi = int(_to_float(min_interval))
    config["official_fetch"] = {"min_interval_minutes": mi if mi > 0 else 5}
    # 레거시 키 정리(있으면 제거 — config를 깔끔하게 다시 쓴다)
    for k in ("budget", "budget_start"):
        config.pop(k, None)
    save_config(config)
    return RedirectResponse("/", status_code=303)
```

`_valid_date_or_none` 헬퍼가 다른 곳에서 안 쓰이면 삭제.

`official_refresh`(143행)의 `targets`를 tracked로 제한:

```python
    targets = ([provider] if provider in PROVIDERS
               else list(tracked_providers(config)))
```

- [ ] **Step 5: `settings.html` 수정** — 예산 섹션 제거, provider 선택 추가

`<h2>월 예산</h2>` 카드(6~16행) 전체를 아래로 교체:

```html
<section class="card">
  <h2>사용하는 AI</h2>
  <p class="muted">체크한 AI만 공식 사용량 API를 호출하고 대시보드에 표시합니다.
    토큰은 읽기 전용 단발 호출이며, 사용량 수치만 저장합니다(토큰·계정 식별자 미저장).</p>
  <label><input type="checkbox" name="track_claude" {% if "claude" in tracked %}checked{% endif %}> Claude</label>
  <label><input type="checkbox" name="track_codex" {% if "codex" in tracked %}checked{% endif %}> Codex (ChatGPT)</label>
  <label>credit_to_usd <span class="muted">(크레딧→USD 환산, 기본 0.04)</span>
    <input type="number" step="0.001" min="0" name="credit_to_usd" value="{{ credit_to_usd }}"></label>
  <p class="disclaimer">ⓘ 값은 로컬 config(tokenomy.config.json)에만 저장됩니다.</p>
</section>
```

"공식 사용량 자동 취득" 카드(18~36행)에서: `official_enabled`/`official_claude`/`official_codex` 체크박스(23~25행) **삭제**, `min_interval` 입력과 상태 표시·disclaimer는 유지. 첫 문단 "옵트인(기본 꺼짐)…" 문구를 "체크한 AI에 대해 자동 취득합니다"로 갱신.

- [ ] **Step 6: 통과 확인**

Run: `..\..\..\.venv\Scripts\python -m pytest tests/test_web.py -v`
Expected: PASS

- [ ] **Step 7: 커밋**

```bash
git add tokenomy/web/app.py tokenomy/web/templates/settings.html tests/test_web.py
git commit -m "feat(settings): 예산 입력 제거, 사용 AI(tracked_providers) 선택 UI"
```

---

## Task 7: 죽은 예산·번다운 코드 삭제 + 마이그레이션 내성

**Files:**
- Modify: `tokenomy/budget.py` (Budget/budget_from_config/budget_start_kst/KST? 제거)
- Modify: `tokenomy/aggregate.py` (burndown 4함수 + Burndown/CodexBurndown + effective_month_start 제거)
- Test: `tests/test_budget.py`, `tests/test_aggregate.py`

**Interfaces:**
- Removes: `Budget`, `budget_from_config`, `budget_start_kst`, `Budget.weekly_codex_limit/limit_for/total`; `burndown`, `codex_burndown`, `combined_burndown`, `_compute_burndown`, `effective_month_start`, `Burndown`, `CodexBurndown`
- 남는 공개 심볼: `business_days_between`/`add_business_days`(lens가 사용), `month_bounds`, `month_spend`, `official_view`, `daily_series`, `insights`, `PROVIDERS`

- [ ] **Step 1: 죽은 테스트 삭제** — `tests/`

`tests/test_aggregate.py`에서 삭제: `test_burndown_on_track`, `test_burndown_over_budget_predicts_exhaust`, `test_burndown_excludes_other_months`, `test_unpriced_counted`, `test_burndown_status_ok/warn/exceeds`, `test_combined_burndown_sums_capped`, `test_weekly_codex_limit_is_quarter`(test_budget.py), 그리고 `from tokenomy.budget import Budget` / `CodexBurndown, codex_burndown, combined_burndown, burndown` import 제거.

`tests/test_budget.py`에서 삭제: `test_budget_splits_providers`, `test_limit_for`, `test_budget_from_config_*`, `test_budget_start_kst_*`, `test_load_config_keeps_budget_start`, `test_load_config_missing_budget_start_is_none`. `test_load_config_missing_file_returns_zero_tracking`은 새 기본값(tracked_providers/official_fetch)으로 재작성. `test_example_config_is_valid`는 새 예제(budget 키 없음, tracked_providers 있음) 검증으로 갱신.

- [ ] **Step 2: 삭제 후 import 깨짐 확인(red)**

Run: `..\..\..\.venv\Scripts\python -m pytest -q`
Expected: 아직 green(코드 심볼은 남아있음) — 이 단계는 테스트만 정리.

- [ ] **Step 3: `aggregate.py` 번다운 코드 삭제**

삭제: `_compute_burndown`(195~270), `burndown`(273~282), `combined_burndown`(285~302), `CodexBurndown` dataclass(305~322), `codex_burndown`(554~), `effective_month_start`(100~108), `Burndown` dataclass(정의부). `business_days_between`/`add_business_days`는 **유지**(lens 사용). `Budget` import(`from tokenomy.budget import Budget`) 제거.

> 삭제 전 확인: `grep -rn "burndown\|effective_month_start\|Burndown\|\bBudget\b" tokenomy/` 로 남은 참조가 없어야 한다(views/cli/aggregate 내부). 있으면 그 참조부터 정리.

- [ ] **Step 4: `budget.py` 예산 모델 삭제**

삭제: `Budget` dataclass, `budget_from_config`, `budget_start_kst`. `KST` 상수는 다른 사용처 없으면 제거. `load_config`/`save_config`/`credit_to_usd`/`official_fetch_settings`/`user_label`/`tracked_providers`/`_default_label`/`_config_path`는 유지.

- [ ] **Step 5: 전체 스위트 통과 확인**

Run: `..\..\..\.venv\Scripts\python -m pytest -q`
Expected: PASS (전부 green, 죽은 import 없음)

- [ ] **Step 6: 잔존 참조 정적 점검**

Run: `..\..\..\.venv\Scripts\python -c "import tokenomy.cli, tokenomy.web.app, tokenomy.web.views, tokenomy.aggregate, tokenomy.budget, tokenomy.official_fetch; print('imports ok')"`
Expected: `imports ok`

- [ ] **Step 7: 커밋**

```bash
git add tokenomy/budget.py tokenomy/aggregate.py tests/test_budget.py tests/test_aggregate.py
git commit -m "refactor: 죽은 예산·번다운 코드 삭제(Budget/burndown 엔진)"
```

---

## Task 8: 문서 갱신

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `README.en.md`, `AGENTS.md`

**Interfaces:** 없음(문서)

- [ ] **Step 1: `CLAUDE.md` 갱신**

- 아키텍처 다이어그램/설명에서 "수동 예산", `budget_start` clamp, "예산 대비 번다운" 표현 제거. aggregate.py 설명을 "공식 사용량 기반 예측(official_view+lens) · 사용량 전용 폴백"으로 갱신.
- "예산 주기는 provider별로 다르다" 게시 → 공식 주기는 API `resets_at` 기준으로 재서술(수동 예산·`budget_start` 언급 삭제).
- "공식 사용량 취득은 옵트인·비차단" 게시 → "default-on(tracked provider만)·비차단"으로. `official_fetch.enabled` 언급 삭제, `tracked_providers` 설명 추가.
- 환경변수 섹션: `TOKENOMY_SKIP_OFFICIAL_FETCH` 유지(설명 그대로).

- [ ] **Step 2: `README.md` / `README.en.md` 갱신**

- "예산 설정" 사용법/스크린샷 설명 제거 또는 "사용 AI 선택 + 공식 사용량 자동 표시"로 교체.
- 공식 사용량이 한도/잔여의 정본임을 명시. 개인 구독제는 rate-window(%) 표시, 종량제/엔터프라이즈는 공식 USD 한도 표시로 서술.

- [ ] **Step 3: `AGENTS.md` 갱신** — 예산/번다운 언급이 있으면 위와 동일 방향으로 정정.

- [ ] **Step 4: 문서 정합성 점검**

Run: `grep -rn "예산\|budget\|budget_start\|번다운" CLAUDE.md README.md README.en.md AGENTS.md`
Expected: 남은 매치는 "옛 예산 기능 제거" 같은 의도된 서술뿐(잔존 기능 설명 없음).

- [ ] **Step 5: 커밋**

```bash
git add CLAUDE.md README.md README.en.md AGENTS.md
git commit -m "docs: 예산 제거·공식 우선·tracked_providers 반영"
```

---

## 최종 검증

- [ ] 전체 테스트: `..\..\..\.venv\Scripts\python -m pytest` → all green
- [ ] 정적 import: 모든 모듈 import OK(Task 7 Step 6)
- [ ] 수동 스모크(선택): `..\..\..\.venv\Scripts\python -m uvicorn tokenomy.web.app:app --port 8765` 후 `/`·`/settings` 렌더 확인 — 배너/번다운 카드 없음, "사용하는 AI" 체크박스 동작, 공식 패널 표시
- [ ] 레거시 config 내성: `budget`/`budget_start`/`official_fetch.enabled`가 든 옛 `tokenomy.config.json`으로 앱이 에러 없이 뜨는지 확인

## Self-Review 메모(작성자 점검 완료)

- **Spec 커버리지**: 수동 예산 제거(Task 7) · 공식 default-on(Task 2,5) · 토글 제거+env 유지(Task 2,6) · tracked_providers(Task 1,6) · 크레덴셜 부재 silent skip(Task 2) · 개인 구독제 rate-window 유지(Task 3,4에서 official_view 버킷 그대로 렌더) · 사용량 전용 폴백(Task 4) · 레거시 키 무시(Task 1,6) — 모두 태스크 매핑됨.
- **타입 정합성**: `official_view(conn, provider, now, credit_to_usd)` · `daily_series(conn, provider, now)` · `insights(conn, now, provider, cov)` · `month_spend(conn, provider, now)` · `tracked_providers(config)->list[str]` · `creds_present(provider)->bool` — Task 간 시그니처 일치 확인.
- **플레이스홀더**: 없음(테스트 헬퍼는 기존 파일 컨벤션 재사용 명시). 실제 테스트 헬퍼명(`_client`/`_conn`/`_NOW` 등)은 해당 테스트 파일의 기존 픽스처에 맞춰 구현자가 정렬할 것.
