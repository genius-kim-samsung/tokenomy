# Tokenomy 범용 public 전환 — 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 조직 예산 등급 테이블을 제거하고 예산을 사용자 config로 받아, 종량제 사용자가 쓸 수 있는 범용 도구로 코드 구조를 전환한다.

**Architecture:** `tiers.json`/`tiers.py`(조직 예산 등급 매핑)를 삭제하고 `tokenomy.config.json`(gitignore) + `budget.py`로 대체한다. provider 내부 키 `chatgpt`를 `codex`로 통일한다. 웹에 예산 설정 화면을 추가하고, pricing override를 지원한다. UI 영문화·git 히스토리 정리는 **이 계획의 범위 밖**(후속).

**Tech Stack:** Python 3, SQLite, FastAPI + Jinja2, pytest. Windows(PowerShell) 로컬 실행.

**Scope 경계 (이 계획에서 하지 않음):**
- UI(대시보드/CLI) 문자열 영문화 — 별도 후속 plan
- git 히스토리 정리 / public 원격 push — 별도 세션(되돌리기 어려움)
- `docs/ccusage-분석-보고서.md` 조직 정보 점검 — 배포 세션
- 신규 도구(Gemini CLI 등) 파서 — 후속

**참조 spec:** `docs/superpowers/specs/2026-06-12-tokenomy-public-generalization-design.md`

**공통 테스트 명령:** 저장소 루트에서 `python -m pytest -q` (개별: `python -m pytest tests/test_x.py -v`). 가상환경이 있으면 `.venv\Scripts\python -m pytest ...`.

---

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `config/tokenomy.config.json` | 사용자 개인 예산/설정 | 신규(gitignore) |
| `config/tokenomy.config.example.json` | 설정 템플릿 | 신규(커밋) |
| `tokenomy/budget.py` | config 로드/저장 + `Budget` 모델 | 신규(`tiers.py` 대체) |
| `config/tiers.json` | 조직 예산 등급 정책 | **삭제** |
| `tokenomy/tiers.py` | 티어→예산 매핑 | **삭제** |
| `tests/test_budget.py` | budget 단위 테스트 | 신규(`test_tiers.py` 대체) |
| `tokenomy/codex_parser.py` | provider 키 | `chatgpt`→`codex` |
| `tokenomy/cli.py` | provider 루프 + 로더 교체 | 수정 |
| `tokenomy/aggregate.py` | provider 루프 | `chatgpt`→`codex` |
| `tokenomy/web/app.py` | provider 화이트리스트 + `/settings` 라우트 | 수정 |
| `tokenomy/web/views.py` | config 기반 컨텍스트 + 온보딩 플래그 | 수정 |
| `tokenomy/web/templates/dashboard.html` | provider 토글/예산미설정/온보딩 | 수정 |
| `tokenomy/web/templates/settings.html` | 예산 설정 폼 | 신규 |
| `tokenomy/pricing.py` | override 병합 함수 | 함수 추가 |
| `config/pricing.json` | provider 키 + 조직 전용 주석 제거 | 수정 |
| `README.md` / `README.ko.md` / `LICENSE` | 범용 문서 | 신규/재작성 |
| `.gitignore` | 개인 config 무시 | 1줄 추가 |

---

## Task 1: provider 내부 키 `chatgpt` → `codex` 통일

**Files:**
- Modify: `tokenomy/codex_parser.py:72`
- Modify: `config/pricing.json:16-18`
- Modify: `tokenomy/cli.py:29,61`
- Modify: `tokenomy/aggregate.py:99`
- Modify: `tokenomy/web/app.py:30`
- Modify: `tokenomy/web/templates/dashboard.html:20,24`
- Test: `tests/test_codex_parser.py:24,37,41`

- [ ] **Step 1: 테스트 기대값을 `codex`로 수정 (failing)**

`tests/test_codex_parser.py`에서 3곳을 수정한다.

24번째 줄:
```python
    assert rec.provider == "codex"
```

37–38번째 줄(인라인 pricing fixture의 provider):
```python
    pricing = {"match": [{"contains": "gpt-5", "provider": "codex",
                          "input": 1.25, "output": 10.0, "cache_write": 0.0, "cache_read": 0.125}]}
```

41번째 줄:
```python
    assert cost.provider == "codex"
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_codex_parser.py -v`
Expected: FAIL — `test_parse_rollout_uses_last_cumulative`에서 `assert 'chatgpt' == 'codex'`.

- [ ] **Step 3: codex_parser 구현 변경**

`tokenomy/codex_parser.py` 72번째 줄:
```python
        provider="codex",
```

- [ ] **Step 4: 단가/루프/화이트리스트/템플릿의 `chatgpt` 일괄 교체**

`config/pricing.json` — `gpt-5`, `o4`, `gpt-4` 세 항목의 `"provider": "chatgpt"`를 모두 `"provider": "codex"`로 변경.

`tokenomy/cli.py` 29번째 줄:
```python
    archive_tree(CODEX_ROOT, conn, provider="codex")
```

`tokenomy/cli.py` 61번째 줄:
```python
    for prov in ("claude", "codex"):  # codex = Codex CLI
```

`tokenomy/aggregate.py` 99번째 줄:
```python
    for prov in ("claude", "codex"):
```

`tokenomy/web/app.py` 30번째 줄:
```python
_PROVIDERS = ("claude", "codex")
```

`tokenomy/web/templates/dashboard.html` 20번째 줄(토글 링크와 라벨):
```html
      <a href="/?provider=codex&sort={{ sort }}" class="{{ 'on' if provider == 'codex' }}">Codex</a>
```

`tokenomy/web/templates/dashboard.html` 24번째 줄:
```html
  {% if not has_data and provider == "codex" %}
```

- [ ] **Step 5: 전체 테스트 통과 확인**

Run: `python -m pytest -q`
Expected: PASS (전부 통과). 잔존 `chatgpt` 확인: `python -m pytest -q` 통과 후, 코드에 남은 키가 없는지 점검.

Run: `git grep -n "chatgpt" -- "tokenomy" "config" "tests"`
Expected: 출력 없음(0 매치). 남으면 해당 위치를 `codex`로 마저 고친다.

- [ ] **Step 6: 커밋**

```bash
git add tokenomy/codex_parser.py config/pricing.json tokenomy/cli.py tokenomy/aggregate.py tokenomy/web/app.py tokenomy/web/templates/dashboard.html tests/test_codex_parser.py
git commit -m "refactor: provider 내부 키 chatgpt → codex 통일"
```

---

## Task 2: `budget.py` + config 스키마 (tiers.py와 병존 추가)

이 태스크는 `tiers.py`를 **아직 지우지 않는다**(호출부가 살아 있으므로). 새 모듈과 테스트만 추가해 전체 테스트가 통과하는 상태를 유지한다. 삭제는 Task 3.

**Files:**
- Create: `tokenomy/budget.py`
- Create: `config/tokenomy.config.example.json`
- Test: `tests/test_budget.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_budget.py` 생성:
```python
import json

from tokenomy.budget import (
    Budget,
    budget_from_config,
    load_config,
    save_config,
    user_label,
)


def test_budget_splits_providers():
    b = Budget(claude=223, codex=50)
    assert b.claude == 223
    assert b.codex == 50
    assert b.total == 273


def test_limit_for():
    b = Budget(claude=223, codex=50)
    assert b.limit_for("claude") == 223
    assert b.limit_for("codex") == 50


def test_budget_from_config_reads_budget_block():
    cfg = {"budget": {"claude": 100, "codex": 30}}
    b = budget_from_config(cfg)
    assert b.claude == 100 and b.codex == 30


def test_budget_from_config_missing_block_is_zero():
    b = budget_from_config({})
    assert b.total == 0


def test_load_config_missing_file_returns_zero_tracking(tmp_path):
    cfg = load_config(tmp_path / "nope.json")
    assert cfg["budget"]["claude"] == 0
    assert cfg["budget"]["codex"] == 0


def test_save_then_load_roundtrip(tmp_path):
    p = tmp_path / "c.json"
    save_config({"user_label": "alice", "budget": {"claude": 7, "codex": 8}}, p)
    cfg = load_config(p)
    assert cfg["user_label"] == "alice"
    assert budget_from_config(cfg).codex == 8


def test_user_label_falls_back(monkeypatch):
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    assert user_label({}) == "me"


def test_example_config_is_valid():
    cfg = json.loads(open("config/tokenomy.config.example.json", encoding="utf-8").read())
    assert "budget" in cfg
    assert "claude" in cfg["budget"] and "codex" in cfg["budget"]
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_budget.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tokenomy.budget'`.

- [ ] **Step 3: `budget.py` 구현**

`tokenomy/budget.py` 생성:
```python
"""예산/설정 모델.

사용자 config(tokenomy.config.json)에서 provider별 월 예산을 읽는다.
- claude: Claude Code 월 예산 USD
- codex:  Codex CLI 월 예산 USD
config가 없으면 예산 0(추적 전용 모드)으로 동작한다. example 파일은 템플릿일 뿐
자동 로드하지 않는다(사용자가 복사해서 tokenomy.config.json을 만든다).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_CONFIG = Path("config/tokenomy.config.json")


@dataclass
class Budget:
    claude: float
    codex: float

    @property
    def total(self) -> float:
        return self.claude + self.codex

    def limit_for(self, provider: str) -> float:
        return self.claude if provider == "claude" else self.codex


def _default_label() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or "me"


def _config_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get("TOKENOMY_CONFIG")
    return Path(env) if env else _DEFAULT_CONFIG


def load_config(path: str | Path | None = None) -> dict:
    p = _config_path(path)
    if not p.exists():
        return {"user_label": _default_label(),
                "budget": {"claude": 0.0, "codex": 0.0},
                "pricing_overrides": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def save_config(config: dict, path: str | Path | None = None) -> None:
    p = _config_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def budget_from_config(config: dict) -> Budget:
    b = config.get("budget") or {}
    return Budget(claude=float(b.get("claude") or 0), codex=float(b.get("codex") or 0))


def user_label(config: dict) -> str:
    return config.get("user_label") or _default_label()
```

- [ ] **Step 4: example config 생성**

`config/tokenomy.config.example.json` 생성:
```json
{
  "user_label": "me",
  "budget": {
    "claude": 100,
    "codex": 50
  },
  "pricing_overrides": {}
}
```

- [ ] **Step 5: 통과 확인**

Run: `python -m pytest tests/test_budget.py -v`
Expected: PASS (7개 통과).

- [ ] **Step 6: 커밋**

```bash
git add tokenomy/budget.py config/tokenomy.config.example.json tests/test_budget.py
git commit -m "feat: budget.py + config 스키마 (tiers.py 대체 모듈)"
```

---

## Task 3: 호출부를 budget.py로 교체하고 tiers.* 삭제

**Files:**
- Modify: `tokenomy/cli.py:19,45-52`
- Modify: `tokenomy/web/views.py:9,18-50`
- Modify: `tokenomy/web/templates/dashboard.html:4`
- Delete: `tokenomy/tiers.py`, `config/tiers.json`, `tests/test_tiers.py`

- [ ] **Step 1: cli.py 로더 교체**

`tokenomy/cli.py` 19번째 줄(import) 교체:
```python
from tokenomy.budget import budget_from_config, load_config, user_label
```

`tokenomy/cli.py` `cmd_report` 함수의 45–52번째 줄을 교체. 기존:
```python
    tiers = load_tiers()
    du = tiers["default_user"]
    budget = budget_for(du["tier"], tiers, du.get("provider_choice"))
    now = datetime.now(KST)

    print(f"=== Tokenomy — {now:%Y-%m} (KST, 이 머신 데이터만) ===")
    print(f"User: {du['user_id']}   Tier: {du['tier']}")
```
교체 후:
```python
    config = load_config()
    budget = budget_from_config(config)
    now = datetime.now(KST)

    print(f"=== Tokenomy — {now:%Y-%m} (KST, 이 머신 데이터만) ===")
    print(f"User: {user_label(config)}")
```

- [ ] **Step 2: web/views.py 로더 교체**

`tokenomy/web/views.py` 9번째 줄(import) 교체:
```python
from tokenomy.budget import budget_from_config, load_config, user_label
```

`tokenomy/web/views.py` `dashboard_context` 19–22번째 줄 교체. 기존:
```python
    now = now_kst or datetime.now(KST)
    tiers = load_tiers()
    du = tiers["default_user"]
    budget = budget_for(du["tier"], tiers, du.get("provider_choice"))
```
교체 후:
```python
    now = now_kst or datetime.now(KST)
    config = load_config()
    budget = budget_from_config(config)
```

`tokenomy/web/views.py` return dict의 42번째 줄 교체. 기존:
```python
        "user_id": du["user_id"], "tier": du["tier"],
```
교체 후:
```python
        "user_label": user_label(config),
        "budget_configured": budget.total > 0,
```

- [ ] **Step 3: dashboard.html 헤더의 user_id/tier 교체**

`tokenomy/web/templates/dashboard.html` 4번째 줄 교체:
```html
  <div>🪙 Tokenomy · {{ month }} (KST) · {{ user_label }}</div>
```

- [ ] **Step 4: tiers 자산 삭제**

```bash
git rm tokenomy/tiers.py config/tiers.json tests/test_tiers.py
```

`aggregate.py`는 `from tokenomy.tiers import Budget`를 import한다(11번째 줄). `budget.py`로 바꾼다.

`tokenomy/aggregate.py` 11번째 줄 교체:
```python
from tokenomy.budget import Budget
```

- [ ] **Step 5: web 테스트의 config 격리**

`tokenomy/web/views.py`가 이제 `load_config()`를 호출하므로(기본 경로 `config/tokenomy.config.json`), 기존 web 테스트가 로컬 개인 config에 의존하지 않도록 `tests/test_web.py`의 `_client` 헬퍼를 임시 config 경로로 격리한다. 기존 `_client` 함수의 `db = tmp_path / "t.db"` 바로 다음 줄에 추가:
```python
    monkeypatch.setenv("TOKENOMY_CONFIG", str(tmp_path / "cfg.json"))  # 개인 config 격리(미존재 → 예산 0)
```

- [ ] **Step 6: 전체 테스트 + 잔존 import 점검**

Run: `git grep -n "tokenomy.tiers\|load_tiers\|budget_for\|tiers.json" -- tokenomy tests`
Expected: 출력 없음. 남으면 해당 파일을 마저 고친다.

Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 7: 커밋**

```bash
git add -A
git commit -m "refactor: 호출부를 budget.py로 교체하고 조직 tiers.* 삭제"
```

---

## Task 4: 웹 예산 설정 화면 (`/settings`)

**Files:**
- Modify: `tokenomy/web/app.py` (라우트 2개 추가)
- Create: `tokenomy/web/templates/settings.html`
- Test: `tests/test_web.py` (테스트 3개 추가)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_web.py` 끝에 추가. Task 3에서 `_client`가 이미 `TOKENOMY_CONFIG`를 `tmp_path/cfg.json`으로 격리하므로, 그 config 경로를 함께 돌려주는 얇은 헬퍼만 더한다.
```python
def _client_with_config(tmp_path, monkeypatch):
    """_client(=config 격리됨) + 그 config 파일 경로를 함께 돌려준다."""
    client, _ = _client(tmp_path, monkeypatch)   # TOKENOMY_CONFIG → tmp_path/cfg.json
    return client, tmp_path / "cfg.json"


def test_settings_get_renders_form(tmp_path, monkeypatch):
    client, _ = _client_with_config(tmp_path, monkeypatch)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "예산" in r.text
    assert 'name="claude"' in r.text
    assert 'name="codex"' in r.text


def test_settings_post_writes_config(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings", data={"claude": "150", "codex": "40"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    import json
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["budget"]["claude"] == 150.0
    assert saved["budget"]["codex"] == 40.0


def test_settings_post_invalid_number_falls_back_zero(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    r = client.post("/settings", data={"claude": "abc", "codex": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    import json
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["budget"]["claude"] == 0.0
    assert saved["budget"]["codex"] == 0.0
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_web.py -k settings -v`
Expected: FAIL — `/settings` 404 (라우트 없음).

- [ ] **Step 3: settings 라우트 구현**

`tokenomy/web/app.py` import에 추가(13번째 줄 인근):
```python
from tokenomy.budget import budget_from_config, load_config, save_config
```

`tokenomy/web/app.py` 끝에 라우트 추가:
```python
@app.get("/settings")
def settings_get(request: Request):
    config = load_config()
    budget = budget_from_config(config)
    return templates.TemplateResponse(
        request, "settings.html",
        {"claude": budget.claude, "codex": budget.codex},
    )


def _to_float(value: str | None) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0
    except ValueError:
        return 0.0


@app.post("/settings")
def settings_post(claude: str = Form(""), codex: str = Form("")):
    config = load_config()
    config.setdefault("budget", {})
    config["budget"]["claude"] = _to_float(claude)
    config["budget"]["codex"] = _to_float(codex)
    save_config(config)
    return RedirectResponse("/", status_code=303)
```

`tokenomy/web/app.py` 상단 fastapi import에 `Form`을 추가(6번째 줄):
```python
from fastapi import FastAPI, Form, Request
```

- [ ] **Step 4: settings.html 작성**

`tokenomy/web/templates/settings.html` 생성:
```html
{% extends "base.html" %}
{% block body %}
<header class="topbar">
  <div>🪙 Tokenomy · 설정</div>
  <div class="topbar-right"><a class="btn" href="/">← 대시보드</a></div>
</header>

<section class="card">
  <h2>월 예산</h2>
  <p class="muted">종량제(API 달러 과금) 기준 월 예산. 0이면 한도 없이 사용량만 추적합니다.</p>
  <form method="post" action="/settings" class="settings">
    <label>Claude (USD) <input type="number" step="0.01" min="0" name="claude" value="{{ '%.2f'|format(claude) }}"></label>
    <label>Codex (USD) <input type="number" step="0.01" min="0" name="codex" value="{{ '%.2f'|format(codex) }}"></label>
    <button class="btn" type="submit">저장</button>
  </form>
  <p class="disclaimer">ⓘ 값은 로컬 config(tokenomy.config.json)에만 저장됩니다.</p>
</section>
{% endblock %}
```

- [ ] **Step 5: 통과 확인**

Run: `python -m pytest tests/test_web.py -v`
Expected: PASS (기존 + 신규 settings 테스트 전부).

- [ ] **Step 6: 커밋**

```bash
git add tokenomy/web/app.py tokenomy/web/templates/settings.html tests/test_web.py
git commit -m "feat(web): 예산 설정 화면(/settings) + config 저장"
```

---

## Task 5: 온보딩 배너 + 예산 미설정 안내

**Files:**
- Modify: `tokenomy/web/templates/dashboard.html:11-13,28-30`
- Test: `tests/test_web.py` (테스트 2개 추가)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_web.py` 끝에 추가:
```python
def test_dashboard_shows_onboarding_when_no_budget(tmp_path, monkeypatch):
    client, _ = _client_with_config(tmp_path, monkeypatch)  # config 없음 → 예산 0
    r = client.get("/")
    assert r.status_code == 200
    assert "예산을 설정하세요" in r.text
    assert "/settings" in r.text


def test_dashboard_hides_onboarding_when_budget_set(tmp_path, monkeypatch):
    client, cfg = _client_with_config(tmp_path, monkeypatch)
    cfg.write_text('{"budget": {"claude": 100, "codex": 0}}', encoding="utf-8")
    r = client.get("/")
    assert "예산을 설정하세요" not in r.text
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_web.py -k onboarding -v`
Expected: FAIL — "예산을 설정하세요" 문구 없음.

- [ ] **Step 3: dashboard 온보딩 배너 + 예산 미설정 문구 교체**

`tokenomy/web/templates/dashboard.html`에서 `notice` 배너 블록(11–13번째 줄) **뒤에** 온보딩 배너를 추가:
```html
{% if not budget_configured %}
<div class="banner">예산을 설정하세요 → <a href="/settings">설정</a> (지금은 사용량 추적만)</div>
{% endif %}
```

같은 파일에서 예산 미설정 안내(28–30번째 줄)의 조직 전용 문구를 교체. 기존:
```html
  {% elif bd.limit == 0 %}
    <p class="muted">한도 미설정 (티어 base 또는 provider 미선택) · 지출 ${{ '%.2f'|format(bd.spent) }}</p>
    <p class="disclaimer">ⓘ 공개 API 단가 기준 추정 · 이 머신 데이터만</p>
```
교체 후:
```html
  {% elif bd.limit == 0 %}
    <p class="muted">예산 미설정 · 지출 ${{ '%.2f'|format(bd.spent) }} · <a href="/settings">예산 설정</a></p>
    <p class="disclaimer">ⓘ 공개 API 단가 기준 추정 · 이 머신 데이터만</p>
```

- [ ] **Step 4: 통과 확인**

Run: `python -m pytest tests/test_web.py -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/web/templates/dashboard.html tests/test_web.py
git commit -m "feat(web): 예산 미설정 시 온보딩 배너 + 설정 링크"
```

---

## Task 6: pricing override 지원 + 조직 전용 주석 정리

**Files:**
- Modify: `tokenomy/pricing.py` (함수 추가)
- Modify: `tokenomy/cli.py` (`cmd_ingest`에서 override 병합)
- Modify: `config/pricing.json` (`_meta` 조직 전용 문구 제거)
- Test: `tests/test_pricing.py` (테스트 2개 추가)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_pricing.py` 끝에 추가:
```python
from tokenomy.pricing import apply_pricing_overrides


def test_apply_overrides_replaces_rate_fields():
    pricing = {"match": [
        {"contains": "opus", "provider": "claude", "input": 15.0, "output": 75.0,
         "cache_write": 18.75, "cache_read": 1.50},
    ]}
    out = apply_pricing_overrides(pricing, {"opus": {"input": 9.0, "output": 36.0}})
    rate = out["match"][0]
    assert rate["input"] == 9.0
    assert rate["output"] == 36.0
    assert rate["cache_read"] == 1.50   # 미지정 필드는 보존


def test_apply_overrides_empty_is_noop():
    pricing = {"match": [{"contains": "opus", "input": 15.0}]}
    out = apply_pricing_overrides(pricing, {})
    assert out["match"][0]["input"] == 15.0
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_pricing.py -k override -v`
Expected: FAIL — `ImportError: cannot import name 'apply_pricing_overrides'`.

- [ ] **Step 3: pricing.py에 override 병합 함수 추가**

`tokenomy/pricing.py` 끝에 추가:
```python
_OVERRIDABLE = ("input", "output", "cache_write", "cache_read")


def apply_pricing_overrides(pricing: dict, overrides: dict | None) -> dict:
    """pricing_overrides({contains: {input/output/...}})로 match[] 단가를 덮어쓴다.

    contains 키가 일치하는 항목의 지정된 단가 필드만 교체한다(미지정 필드 보존).
    """
    if not overrides:
        return pricing
    for entry in pricing.get("match", []):
        ov = overrides.get(entry.get("contains"))
        if ov:
            for k in _OVERRIDABLE:
                if k in ov:
                    entry[k] = ov[k]
    return pricing
```

- [ ] **Step 4: cmd_ingest에서 override 병합**

`tokenomy/cli.py` import에 추가(18번째 줄 인근):
```python
from tokenomy.pricing import apply_pricing_overrides, load_pricing
from tokenomy.budget import load_config
```
(이미 `load_config`가 import돼 있으면 중복 추가하지 않는다.)

`tokenomy/cli.py` `cmd_ingest` 함수 첫 줄 교체. 기존:
```python
def cmd_ingest(conn) -> None:
    pricing = load_pricing()
```
교체 후:
```python
def cmd_ingest(conn) -> None:
    pricing = apply_pricing_overrides(load_pricing(), load_config().get("pricing_overrides"))
```

- [ ] **Step 5: pricing.json `_meta` 조직 전용 문구 정리**

`config/pricing.json`의 `_meta` 블록을 범용 문구로 교체. 기존 `basis`/`verify_pricing_with`의 "요금 환산율", "내부 참조" 등을 제거하고 다음으로 대체:
```json
  "_meta": {
    "unit": "USD per 1,000,000 tokens",
    "basis": "Public API list prices (defaults). Override per-model via pricing_overrides in tokenomy.config.json if your billing differs.",
    "fields": "input=input_tokens, output=output_tokens, cache_write=cache_creation_input_tokens, cache_read=cache_read_input_tokens",
    "matching": "First match[] entry whose 'contains' is a substring of the model id. No match → cost not computed + warning.",
    "note": "Update list prices from official Anthropic/OpenAI pricing pages as they change."
  },
```
또한 `match[]` 각 항목의 `_note`(조직 placeholder/내부 검증 항목 언급)를 제거한다. `synthetic` 항목은 유지(로컬 합성 메시지 무과금).

- [ ] **Step 6: 통과 확인**

Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 7: 커밋**

```bash
git add tokenomy/pricing.py tokenomy/cli.py config/pricing.json tests/test_pricing.py
git commit -m "feat: pricing_overrides 지원 + 단가 메타 범용 문구화"
```

---

## Task 7: 범용 문서 (README 영문/한글 + LICENSE) 및 gitignore

코드 변경이 없는 문서 태스크라 TDD가 아니다. 조직 맥락(플랜 등급, 내부 기획 경로, 내부 검증 항목)을 제거하고 범용 사용자가 설치·설정·실행할 수 있게 한다.

**Files:**
- Modify: `.gitignore`
- Create: `LICENSE`
- Modify: `README.md` (영문 재작성)
- Create: `README.ko.md`

- [ ] **Step 1: gitignore에 개인 config 추가**

`.gitignore`의 `# env / secrets` 블록에 한 줄 추가:
```
# personal config (budget) — never commit
config/tokenomy.config.json
```
(주의: `*.local.json` 패턴은 유지. `tokenomy.config.example.json`은 커밋 대상이므로 무시되지 않는지 확인 — `config/tokenomy.config.json`만 정확히 무시한다.)

- [ ] **Step 2: LICENSE 추가 (MIT)**

`LICENSE` 생성(연도/저작자는 공개 배포 시점에 맞춰 채운다 — 현재는 2026 / 저장소 소유자):
```
MIT License

Copyright (c) 2026 Tokenomy contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 3: README.md 영문 재작성**

`README.md` 전체를 다음으로 교체:
```markdown
# Tokenomy

A local "budget book" for your AI coding token spend. Tokenomy parses your
**local** Claude Code / Codex CLI session logs, then shows monthly burndown
against a budget you set, cost per project/session, and cache-efficiency
signals — so pay-as-you-go users don't blow past their budget mid-month.

> Korean README: [README.ko.md](README.ko.md)

## Who it's for

Pay-as-you-go (API-metered) users of Claude Code and/or Codex CLI who want to
track and cap their own monthly spend. Subscription (Pro/Max/Plus) users can
still track usage — costs show as *public-list-price estimates*.

## Privacy

- Parses only token **metadata** (tokens, time, project, model). **No prompt
  or conversation content is stored.**
- Runs fully locally. The web dashboard binds to `127.0.0.1` only — do not
  expose it to a network.

## Quick start

```bash
pip install -r requirements.txt
cp config/tokenomy.config.example.json config/tokenomy.config.json   # then edit your budget
python -m tokenomy.cli ingest    # parse local session logs into the DB
python -m tokenomy.cli report    # terminal summary
python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765   # web dashboard
```

On Windows you can double-click `start_tokenomy.bat` (ingest → dashboard → opens browser).

## Configure your budget

Edit `config/tokenomy.config.json`, or use the **Settings** page in the
dashboard (`/settings`):

```json
{
  "user_label": "me",
  "budget": { "claude": 100, "codex": 50 },
  "pricing_overrides": {}
}
```

- `budget.claude` / `budget.codex`: your monthly cap in USD. `0` = no cap
  (usage-only tracking).
- `pricing_overrides`: override per-model rates if your billing differs from
  public list prices, e.g. `{"opus": {"input": 9.0, "output": 36.0}}`.

## Data sources

- Claude Code: `~/.claude/projects/**/*.jsonl` (per-message usage + cache).
- Codex CLI: `~/.codex/sessions/**/rollout-*.jsonl` (per-session cumulative).

## Pricing

`config/pricing.json` ships with public API list prices. Update them as
providers change prices, or override per-user via `pricing_overrides`.

## Adding a parser for another tool

Tokenomy normalizes each tool's logs into `UsageRecord` (see
`tokenomy/parser.py`). To support another CLI, write a module that discovers
its log files and yields `UsageRecord`s, then ingest them via
`tokenomy.db.ingest_records(conn, records, pricing)` — see
`tokenomy/codex_parser.py` as a reference implementation.

## License

MIT — see [LICENSE](LICENSE).
```

- [ ] **Step 4: README.ko.md 작성 (한글 병기)**

`README.ko.md` 생성:
```markdown
# Tokenomy (토큰 가계부)

AI 코딩 토큰 지출을 가계부처럼 관리하는 **로컬** 도구. Claude Code / Codex CLI의
로컬 세션 로그를 파싱해 — 직접 설정한 예산 대비 월 번다운, 프로젝트/세션별 비용,
캐시 효율 신호를 보여준다. 종량제 사용자가 월말에 예산을 초과하지 않도록 돕는다.

> English README: [README.md](README.md)

## 누구를 위한 도구인가

Claude Code / Codex CLI를 **종량제(API 과금)** 로 쓰며 자기 월 지출을 추적·관리하려는
사용자. 구독(Pro/Max/Plus) 사용자도 사용량 추적은 가능하며, 비용은 *공개 단가 기준
추정치* 로 표시된다.

## 프라이버시

- 토큰 **메타데이터**(토큰/시간/프로젝트/모델)만 파싱한다. **대화 원문은 저장하지 않는다.**
- 완전 로컬 실행. 웹 대시보드는 `127.0.0.1` 에만 바인딩 — 외부에 노출하지 말 것.

## 빠른 시작

```bash
pip install -r requirements.txt
cp config/tokenomy.config.example.json config/tokenomy.config.json   # 예산 편집
python -m tokenomy.cli ingest
python -m tokenomy.cli report
python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765
```

Windows는 `start_tokenomy.bat` 더블클릭(ingest → 대시보드 → 브라우저 자동 오픈).

## 예산 설정

`config/tokenomy.config.json` 을 편집하거나 대시보드의 **설정**(`/settings`) 화면에서:

```json
{
  "user_label": "me",
  "budget": { "claude": 100, "codex": 50 },
  "pricing_overrides": {}
}
```

- `budget.claude` / `budget.codex`: 월 한도(USD). `0` = 한도 없음(추적 전용).
- `pricing_overrides`: 청구 단가가 공개 단가와 다르면 모델별로 덮어쓰기.

## 라이선스

MIT — [LICENSE](LICENSE) 참고.
```

- [ ] **Step 5: 문서 정합성 점검**

Run: `git grep -n -i "플랜 등급\|내부 기획\|S1\|S2\|Budget\|me" -- README.md README.ko.md`
Expected: 출력 없음(조직 맥락 미포함).

Run: `python -m pytest -q`
Expected: PASS (문서 변경이 테스트를 깨지 않음).

- [ ] **Step 6: 커밋**

```bash
git add .gitignore LICENSE README.md README.ko.md
git commit -m "docs: 범용 영문/한글 README + MIT LICENSE + 개인 config gitignore"
```

---

## 완료 후 검증

- [ ] `python -m pytest -q` 전체 통과.
- [ ] `git grep -n "chatgpt\|tokenomy.tiers\|load_tiers\|budget_for" -- tokenomy config tests` → 출력 없음.
- [ ] 대시보드 수동 확인: config 없는 상태에서 기동 → 온보딩 배너 노출 → `/settings`에서 예산 저장 → 대시보드에 번다운 표시.
- [ ] `config/tokenomy.config.json`이 `git status`에 나타나지 않음(gitignore 확인).

## 후속(별도 plan/세션 — 이 계획 밖)

1. UI(대시보드/CLI 출력) 영문화(i18n).
2. git 히스토리 정리 + public 원격 push(되돌리기 어려움 — 신중히).
3. `docs/ccusage-분석-보고서.md` 등 기존 문서의 조직 정보 점검·정리.
4. config 홈 경로(`~/.tokenomy/`) 지원, 신규 도구 파서.
