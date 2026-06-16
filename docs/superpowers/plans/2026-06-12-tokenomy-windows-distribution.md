# Tokenomy Windows 배포 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 비개발자가 터미널·Python·git 없이 더블클릭으로 Tokenomy를 실행하고, 새 버전을 원클릭으로 받게 한다(Windows 단일 exe).

**Architecture:** 기존 FastAPI 웹 대시보드를 그대로 재사용한다. ① 데이터 경로를 `paths.py`로 중앙화(exe면 `~/.tokenomy/`, 소스면 repo 루트), ② `launcher.py`가 ingest→포트탐색→브라우저→uvicorn을 수행하는 exe 진입점, ③ `update.py`가 GitHub Releases 최신 태그를 1일 1회 확인해 대시보드 배너로 노출, ④ PyInstaller onefile + GitHub Actions로 빌드·배포.

**Tech Stack:** Python 3, FastAPI/uvicorn/Jinja2(기존), PyInstaller(신규, 빌드 전용), urllib(stdlib, 업데이트 확인), pytest. 런타임 신규 의존성 없음.

**설계 출처:** `docs/superpowers/specs/2026-06-12-tokenomy-windows-distribution-design.md`

**Base commit:** `610f7d3`. worktree에서 작업 권장(`superpowers:using-git-worktrees`). 모든 경로는 repo 루트 상대.

**공통 테스트 명령:** repo 루트에서 `python -m pytest -q`. 현재 기준선 = **112 passed**.

---

## 파일 구조

| 파일 | 책임 | 작업 |
|---|---|---|
| `tokenomy/paths.py` | 데이터 루트(쓰기) + 번들 리소스(읽기) 경로 중앙 해석 | Create |
| `tokenomy/db.py` | `connect()` 기본 경로를 `paths.db_path()`로 | Modify |
| `tokenomy/archive.py` | `archive_tree()` 기본 경로를 `paths.archive_root()`로 | Modify |
| `tokenomy/budget.py` | `_config_path()` 기본을 `paths.config_path()`로 | Modify |
| `tokenomy/pricing.py` | `load_pricing()` 기본을 `paths.resource_path()`로 | Modify |
| `tokenomy/web/app.py` | `_BASE`를 `paths.resource_path()`로 + 업데이트 배너 컨텍스트 | Modify |
| `tokenomy/update.py` | GitHub Releases 최신 태그 확인 + semver 비교 + 1일 캐시 | Create |
| `tokenomy/launcher.py` | exe 진입점: ingest→포트탐색→브라우저→serve | Create |
| `tokenomy/web/templates/dashboard.html` | 업데이트 배너 | Modify |
| `tokenomy/web/static/style.css` | `.banner.update` 스타일 | Modify |
| `tokenomy.spec` | PyInstaller onefile 정의 | Create |
| `.github/workflows/release.yml` | 태그 push→빌드→Release 업로드 | Create |
| `.gitignore` | 빌드 산출물(`build/`,`dist/`) 무시 | Modify |
| `README.md` / `README.ko.md` | exe 설치/첫 실행/SmartScreen 안내 | Modify |
| `tests/test_paths.py` | 경로 해석 단위 테스트 | Create |
| `tests/test_update.py` | 버전 비교 + 네트워크 모킹 | Create |
| `tests/test_launcher.py` | `--version`/포트 탐색 | Create |
| `tests/test_db.py` / `test_budget.py` / `test_web.py` | paths 경유·배너 테스트 추가 | Modify |

**구현 순서(의존성):** paths(T1) → 경로 적용(T2) → update(T3) → 웹 배너(T4) → launcher(T5) → spec/빌드(T6) → CI(T7) → 문서(T8).

---

## Task 1: `paths.py` — 경로 중앙 해석

데이터(쓰기: config/DB/archive)는 `data_dir()` 아래, 번들 리소스(읽기: pricing.json/템플릿)는 `resource_path()`로 해석한다. 소스 실행은 기존 repo 레이아웃과 100% 동일하고, exe(frozen)는 `~/.tokenomy/`로 분리된다.

**Files:**
- Create: `tokenomy/paths.py`
- Test: `tests/test_paths.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_paths.py` 생성:

```python
from tokenomy import paths


def test_data_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENOMY_DATA", str(tmp_path / "custom"))
    d = paths.data_dir()
    assert d == tmp_path / "custom"
    assert d.exists()


def test_data_dir_frozen_uses_home_dot_tokenomy(tmp_path, monkeypatch):
    monkeypatch.delenv("TOKENOMY_DATA", raising=False)
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
    d = paths.data_dir()
    assert d == tmp_path / ".tokenomy"
    assert d.exists()


def test_data_dir_source_is_repo_root(monkeypatch):
    monkeypatch.delenv("TOKENOMY_DATA", raising=False)
    monkeypatch.setattr(paths.sys, "frozen", False, raising=False)
    d = paths.data_dir()
    assert (d / "tokenomy" / "__init__.py").exists()  # repo 루트 표지


def test_path_helpers_under_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENOMY_DATA", str(tmp_path))
    assert paths.db_path() == tmp_path / "data" / "tokenomy.db"
    assert paths.archive_root() == tmp_path / "data" / "archive"
    assert paths.config_path() == tmp_path / "config" / "tokenomy.config.json"


def test_resource_path_source_finds_real_file(monkeypatch):
    monkeypatch.delattr(paths.sys, "_MEIPASS", raising=False)
    p = paths.resource_path("config/pricing.json")
    assert p.name == "pricing.json"
    assert p.exists()  # 소스 실행: repo의 실제 파일
```

- [ ] **Step 2: 실행 → 실패 확인**

Run: `python -m pytest tests/test_paths.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tokenomy.paths'`

- [ ] **Step 3: `paths.py` 구현**

`tokenomy/paths.py` 생성:

```python
"""경로 중앙 해석.

두 부류를 구분한다:
- 데이터(쓰기): config/DB/archive — `data_dir()` 아래. exe면 ~/.tokenomy/,
  소스 실행이면 repo 루트(기존 호환), env TOKENOMY_DATA로 전체 오버라이드.
- 리소스(읽기): pricing.json·웹 템플릿/static — `resource_path()`. PyInstaller
  onefile이면 _MEIPASS, 소스면 repo 루트.

입력 로그(~/.claude, ~/.codex)는 대상이 아니다 — 각 parser가 홈에서 직접 읽는다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent  # tokenomy/의 부모 = repo 루트


def data_dir() -> Path:
    env = os.environ.get("TOKENOMY_DATA")
    if env:
        base = Path(env).expanduser()
    elif getattr(sys, "frozen", False):       # PyInstaller exe
        base = Path.home() / ".tokenomy"
    else:                                      # 소스/개발 실행 → repo 루트(기존 호환)
        base = _REPO_ROOT
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    return data_dir() / "data" / "tokenomy.db"


def archive_root() -> Path:
    return data_dir() / "data" / "archive"


def config_path() -> Path:
    return data_dir() / "config" / "tokenomy.config.json"


def resource_path(rel: str) -> Path:
    """번들된 읽기전용 리소스. frozen이면 _MEIPASS, 소스면 repo 루트 기준."""
    base = Path(getattr(sys, "_MEIPASS", _REPO_ROOT))
    return base / rel
```

- [ ] **Step 4: 실행 → 통과 확인**

Run: `python -m pytest tests/test_paths.py -v`
Expected: PASS (5개)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/paths.py tests/test_paths.py
git commit -m "feat(paths): 데이터/리소스 경로 중앙 해석(frozen 분기)"
```

---

## Task 2: 경로 중앙화 적용 (db/archive/budget/pricing/app)

기존 상대경로 기본값을 `paths` 경유로 바꾼다. **명시 인자를 주는 기존 테스트는 안 깨진다**(기본값만 변경). 소스 실행 시 해석 결과가 기존과 동일하므로 회귀 없음.

**Files:**
- Modify: `tokenomy/db.py:116-122`
- Modify: `tokenomy/archive.py:14,41-47`
- Modify: `tokenomy/budget.py:16,36-40`
- Modify: `tokenomy/pricing.py:25-26`
- Modify: `tokenomy/web/app.py:17`
- Test: `tests/test_db.py`, `tests/test_budget.py` (통합 테스트 추가)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_db.py` 끝에 추가:

```python
def test_connect_default_uses_paths_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENOMY_DATA", str(tmp_path))
    conn = connect()  # 인자 없음 → paths.db_path()
    conn.execute("INSERT INTO meta (key, value) VALUES ('x', '1')")
    conn.commit()
    assert (tmp_path / "data" / "tokenomy.db").exists()
```

`tests/test_budget.py` 끝에 추가:

```python
def test_config_path_default_uses_paths(tmp_path, monkeypatch):
    from tokenomy.budget import _config_path
    monkeypatch.delenv("TOKENOMY_CONFIG", raising=False)
    monkeypatch.setenv("TOKENOMY_DATA", str(tmp_path))
    assert _config_path() == tmp_path / "config" / "tokenomy.config.json"
```

- [ ] **Step 2: 실행 → 실패 확인**

Run: `python -m pytest tests/test_db.py::test_connect_default_uses_paths_db tests/test_budget.py::test_config_path_default_uses_paths -v`
Expected: FAIL — `connect()`는 `data/tokenomy.db`(repo 상대)에 만들고, `_config_path()`는 `config/tokenomy.config.json`(repo 상대)을 반환

- [ ] **Step 3: `db.connect` 수정**

`tokenomy/db.py`의 `connect`(116–122줄)를 교체:

```python
def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    from tokenomy.paths import db_path as _default_db_path
    p = Path(db_path) if db_path is not None else _default_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    conn.executescript(SCHEMA)
    return conn
```

(`":memory:"`를 주는 기존 테스트는 `db_path is not None`이라 그대로 동작. `Path(":memory:")`는 sqlite가 파일로 안 만들지만 기존 테스트는 `connect(":memory:")`로 문자열을 직접 넘기므로 영향 없음 — 단, `Path(":memory:")`로 감싸면 `str()` 결과가 `:memory:`로 보존되는지 주의. 아래 Step 3-fix 참조.)

- [ ] **Step 3-fix: `:memory:` 보존 처리**

`Path(":memory:")`를 `str()`하면 OS에 따라 `:memory:`가 유지되지만, 명시적으로 분기해 안전하게 한다. `connect`를 최종본으로 교체:

```python
def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    if db_path is None:
        from tokenomy.paths import db_path as _default_db_path
        p = _default_db_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        target = str(p)
    else:
        target = str(db_path)
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    conn.executescript(SCHEMA)
    return conn
```

- [ ] **Step 4: `archive.archive_tree` 수정**

`tokenomy/archive.py` 14줄의 `ARCHIVE_ROOT = Path("data/archive")`를 삭제하고, `archive_tree` 시그니처/본문(41–47줄)을 교체:

```python
def archive_tree(
    root, conn: sqlite3.Connection, provider: str = "claude",
    archive_root=None,
) -> int:
    """root 아래 모든 *.jsonl을 증분 아카이브. 새 바이트가 복사된 파일 수 반환."""
    if archive_root is None:
        from tokenomy.paths import archive_root as _default_archive_root
        archive_root = _default_archive_root()
    root = Path(root).expanduser()
    archive_root = Path(archive_root)
    copied = 0
```

(이하 `for src in discover_session_files(root):` 본문은 그대로 둔다.)

- [ ] **Step 5: `budget._config_path` 수정**

`tokenomy/budget.py` 16줄의 `_DEFAULT_CONFIG = Path("config/tokenomy.config.json")`를 삭제하고, `_config_path`(36–40줄)를 교체:

```python
def _config_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get("TOKENOMY_CONFIG")
    if env:
        return Path(env)
    from tokenomy.paths import config_path
    return config_path()
```

- [ ] **Step 6: `pricing.load_pricing` 수정**

`tokenomy/pricing.py`의 `load_pricing`(25–26줄)을 교체:

```python
def load_pricing(path: str | Path | None = None) -> dict:
    if path is None:
        from tokenomy.paths import resource_path
        path = resource_path("config/pricing.json")
    return json.loads(Path(path).read_text(encoding="utf-8"))
```

- [ ] **Step 7: `app.py` 리소스 경로 수정**

`tokenomy/web/app.py` 17줄을 교체:

```python
from tokenomy.paths import resource_path

_BASE = resource_path("tokenomy/web")
```

(소스 실행 시 `repo/tokenomy/web` = 기존 `Path(__file__).resolve().parent`와 동일.)

- [ ] **Step 8: 신규 + 전체 회귀 통과 확인**

Run: `python -m pytest -q`
Expected: PASS — 전체 통과(Task 1의 `test_paths` 포함 + 신규 db/budget 2개). 명시 인자를 주는 기존 테스트는 영향 없음

- [ ] **Step 9: 커밋**

```bash
git add tokenomy/db.py tokenomy/archive.py tokenomy/budget.py tokenomy/pricing.py tokenomy/web/app.py tests/test_db.py tests/test_budget.py
git commit -m "refactor: 데이터/리소스 경로를 paths 경유로 중앙화"
```

---

## Task 3: `update.py` — 업데이트 확인

GitHub Releases 최신 태그를 가져와 `__version__`과 semver 비교한다. 1일 1회만 네트워크 조회(`meta.last_update_check`), 실패/오프라인은 조용히 `None`. 신규 의존성 없음(urllib + 튜플 비교).

**Files:**
- Create: `tokenomy/update.py`
- Test: `tests/test_update.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_update.py` 생성:

```python
from datetime import date

from tokenomy import update
from tokenomy.db import connect
from tokenomy.update import _parse_version, is_newer


def test_parse_version():
    assert _parse_version("v0.2.0") == (0, 2, 0)
    assert _parse_version("0.1.0") == (0, 1, 0)
    assert _parse_version("v1.2") == (1, 2)


def test_is_newer():
    assert is_newer("v0.2.0", "0.1.0") is True
    assert is_newer("v0.1.0", "0.1.0") is False
    assert is_newer("v0.0.9", "0.1.0") is False


def test_is_newer_bad_tag_is_false():
    assert is_newer("garbage", "0.1.0") is False


def test_check_update_skip_env(monkeypatch):
    monkeypatch.setenv("TOKENOMY_SKIP_UPDATE_CHECK", "1")
    assert update.check_update() is None


def test_check_update_returns_newer(monkeypatch):
    monkeypatch.delenv("TOKENOMY_SKIP_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(update, "_fetch_latest_tag", lambda timeout=3.0: "v99.0.0")
    assert update.check_update() == "v99.0.0"


def test_check_update_none_when_current(monkeypatch):
    monkeypatch.delenv("TOKENOMY_SKIP_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(update, "_fetch_latest_tag", lambda timeout=3.0: "v0.0.1")
    assert update.check_update() is None


def test_check_update_network_fail_is_none(monkeypatch):
    monkeypatch.delenv("TOKENOMY_SKIP_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(update, "_fetch_latest_tag", lambda timeout=3.0: None)
    assert update.check_update() is None


def test_check_update_daily_cache(monkeypatch):
    monkeypatch.delenv("TOKENOMY_SKIP_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(update, "_fetch_latest_tag", lambda timeout=3.0: "v99.0.0")
    conn = connect(":memory:")
    d = date(2026, 6, 12)
    assert update.check_update(conn, today=d) == "v99.0.0"  # 첫 호출
    assert update.check_update(conn, today=d) is None        # 같은 날 → 캐시
```

- [ ] **Step 2: 실행 → 실패 확인**

Run: `python -m pytest tests/test_update.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tokenomy.update'`

- [ ] **Step 3: `update.py` 구현**

`tokenomy/update.py` 생성:

```python
"""인앱 업데이트 확인 — GitHub Releases 최신 태그 vs 현재 버전.

- 1일 1회만 네트워크 조회(meta.last_update_check)
- 실패/오프라인/타임아웃은 조용히 None(앱 동작 무영향)
- env TOKENOMY_SKIP_UPDATE_CHECK가 설정되면 항상 None(테스트/CI/오프라인)
- 의존성 추가 없음: urllib(stdlib), semver는 튜플 비교
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import date

from tokenomy import __version__

_REPO = "genius-kim-samsung/tokenomy"
RELEASES_API = f"https://api.github.com/repos/{_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{_REPO}/releases/latest"
_CHECK_KEY = "last_update_check"


def _parse_version(v: str) -> tuple[int, ...]:
    v = v.lstrip("vV").split("-")[0].split("+")[0]
    parts: list[int] = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


def is_newer(remote: str, current: str) -> bool:
    return _parse_version(remote) > _parse_version(current)


def _fetch_latest_tag(timeout: float = 3.0) -> str | None:
    try:
        req = urllib.request.Request(
            RELEASES_API,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "tokenomy"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("tag_name")
    except Exception:
        return None


def check_update(conn=None, today: date | None = None) -> str | None:
    """새 버전 태그(예 'v0.2.0')를 반환. 없거나 확인 불가/캐시면 None."""
    if os.environ.get("TOKENOMY_SKIP_UPDATE_CHECK"):
        return None
    today = today or date.today()
    if conn is not None:
        from tokenomy.db import get_meta, set_meta
        if get_meta(conn, _CHECK_KEY) == today.isoformat():
            return None  # 오늘 이미 확인함
        set_meta(conn, _CHECK_KEY, today.isoformat())
    tag = _fetch_latest_tag()
    if tag and is_newer(tag, __version__):
        return tag
    return None
```

- [ ] **Step 4: 실행 → 통과 확인**

Run: `python -m pytest tests/test_update.py -v`
Expected: PASS (8개)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/update.py tests/test_update.py
git commit -m "feat(update): GitHub Releases 최신버전 확인 + semver 비교 + 1일 캐시"
```

---

## Task 4: 웹 업데이트 배너

대시보드 라우트에서 `check_update`를 호출해 컨텍스트에 넣고, 템플릿 상단에 배너를 띄운다. 네트워크 I/O는 순수 집계(`views.py`)에 넣지 않고 라우트에서만 호출한다.

**Files:**
- Modify: `tokenomy/web/app.py` (import + `dashboard` 라우트)
- Modify: `tokenomy/web/templates/dashboard.html` (배너)
- Modify: `tokenomy/web/static/style.css` (`.banner.update`)
- Test: `tests/test_web.py` (`_client`에 SKIP env + 배너 테스트 2개)

- [ ] **Step 1: 실패 테스트 작성**

먼저 `tests/test_web.py`의 `_client` 헬퍼(9–18줄)에 SKIP env를 추가해 모든 웹 테스트가 네트워크를 안 타게 한다. `monkeypatch.setenv("TOKENOMY_CONFIG", ...)` 줄 **아래**에 추가:

```python
    monkeypatch.setenv("TOKENOMY_SKIP_UPDATE_CHECK", "1")  # 웹 테스트는 업데이트 네트워크 미사용
```

`tests/test_web.py` 끝에 배너 테스트 추가:

```python
def test_dashboard_shows_update_banner(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app_module, "check_update", lambda conn: "v9.9.9")
    r = client.get("/")
    assert r.status_code == 200
    assert "새 버전 v9.9.9" in r.text
    assert "releases/latest" in r.text


def test_dashboard_no_update_banner_when_current(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app_module, "check_update", lambda conn: None)
    r = client.get("/")
    assert "새 버전" not in r.text
```

- [ ] **Step 2: 실행 → 실패 확인**

Run: `python -m pytest tests/test_web.py -k update -v`
Expected: FAIL — `AttributeError: module 'tokenomy.web.app' has no attribute 'check_update'`

- [ ] **Step 3: `app.py`에 업데이트 컨텍스트 주입**

`tokenomy/web/app.py` import 블록(15줄 인근)에 추가:

```python
from tokenomy.update import check_update
```

`dashboard` 라우트(35–42줄)를 교체:

```python
@app.get("/")
def dashboard(request: Request, provider: str = "claude", sort: str = "cost",
              notice: str | None = None):
    provider = provider if provider in _PROVIDERS else "claude"
    sort = sort if sort in _SORTS else "cost"
    conn = connect()
    ctx = dashboard_context(conn, provider, sort)
    update_tag = check_update(conn)
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"notice": notice, "update_tag": update_tag, **ctx},
    )
```

- [ ] **Step 4: 템플릿 배너 추가**

`tokenomy/web/templates/dashboard.html`에서 `notice` 배너 블록(12–14줄) **뒤**에 추가:

```html
{% if update_tag %}
<div class="banner update">새 버전 {{ update_tag }} 사용 가능 —
  <a href="https://github.com/genius-kim-samsung/tokenomy/releases/latest" target="_blank" rel="noopener">다운로드</a>
</div>
{% endif %}
```

- [ ] **Step 5: 배너 스타일 추가**

`tokenomy/web/static/style.css`의 `.banner.error` 줄(25줄) 아래에 추가:

```css
.banner.update { background:#0f2e1a; color:var(--ok); }
```

- [ ] **Step 6: 통과 확인**

Run: `python -m pytest tests/test_web.py -v`
Expected: PASS (기존 web 테스트 + 신규 2개)

- [ ] **Step 7: 커밋**

```bash
git add tokenomy/web/app.py tokenomy/web/templates/dashboard.html tokenomy/web/static/style.css tests/test_web.py
git commit -m "feat(web): 새 버전 업데이트 배너 + 다운로드 링크"
```

---

## Task 5: `launcher.py` — exe 진입점

더블클릭 시 ingest → 빈 포트 탐색 → 브라우저 자동 오픈 → uvicorn 기동. `--version`은 서버 없이 버전만 출력(CI 스모크용).

**Files:**
- Create: `tokenomy/launcher.py`
- Test: `tests/test_launcher.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_launcher.py` 생성:

```python
import socket

from tokenomy import launcher


def test_version_flag(capsys):
    launcher.main(["--version"])
    out = capsys.readouterr().out.strip()
    assert out == launcher.__version__


def test_find_free_port_returns_bindable():
    port = launcher.find_free_port(8765)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))  # 반환된 포트는 실제로 bind 가능


def test_find_free_port_skips_occupied():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occ:
        occ.bind(("127.0.0.1", 8765))
        port = launcher.find_free_port(8765)
        assert port != 8765
        assert 8765 < port < 8785
```

- [ ] **Step 2: 실행 → 실패 확인**

Run: `python -m pytest tests/test_launcher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tokenomy.launcher'`

- [ ] **Step 3: `launcher.py` 구현**

`tokenomy/launcher.py` 생성:

```python
"""exe 진입점 — 더블클릭 실행.

데이터 디렉토리 보장 → ingest 1회 → 빈 포트 탐색 → 브라우저 자동 오픈 →
uvicorn 기동(127.0.0.1, 로컬 전용). PyInstaller 엔트리 스크립트.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser

from tokenomy import __version__


def find_free_port(start: int = 8765, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"빈 포트를 찾지 못함 ({start}~{start + tries})")


def _safe_ingest() -> None:
    try:
        from tokenomy.cli import cmd_ingest
        from tokenomy.db import connect
        conn = connect()
        cmd_ingest(conn)
    except Exception as e:  # ingest 실패는 치명적이지 않음 — 기존 데이터로 표시
        print(f"[launcher] ingest 건너뜀: {e}")


def _open_browser_when_ready(port: int) -> None:
    for _ in range(40):  # 최대 ~10초 대기
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.25)
    webbrowser.open(f"http://127.0.0.1:{port}/")


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] in ("--version", "-V"):
        print(__version__)
        return

    _safe_ingest()
    port = find_free_port()
    threading.Thread(target=_open_browser_when_ready, args=(port,), daemon=True).start()

    import uvicorn
    from tokenomy.web.app import app
    print(f"[Tokenomy] http://127.0.0.1:{port}/  (이 창을 닫으면 종료됩니다)")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 실행 → 통과 확인**

Run: `python -m pytest tests/test_launcher.py -v`
Expected: PASS (3개). 전체: `python -m pytest -q` → 모두 PASS

(주의: `test_find_free_port_*`는 OS 포트 8765 가용성에 의존. 로컬에서 8765를 점유한 dev 서버가 떠 있으면 `test_find_free_port_returns_bindable`이 실패할 수 있다 — 그 경우 dev 서버를 끄고 재실행.)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/launcher.py tests/test_launcher.py
git commit -m "feat(launcher): exe 진입점(ingest→포트탐색→브라우저→serve)"
```

---

## Task 6: PyInstaller spec + 로컬 빌드 검증

`tokenomy.spec`로 단일 exe를 빌드한다. console=True로 둬서 (a) 종료법 안내가 보이고 (b) `--version` 스모크가 stdout으로 검증된다. 빌드 산출물은 gitignore.

**Files:**
- Create: `tokenomy.spec`
- Modify: `.gitignore`

- [ ] **Step 1: `.gitignore`에 빌드 산출물 추가**

`.gitignore`의 `# Python` 블록 아래에 추가:

```
# PyInstaller 빌드 산출물
build/
dist/
```

- [ ] **Step 2: `tokenomy.spec` 작성**

`tokenomy.spec` 생성(repo 루트):

```python
# PyInstaller onefile spec — Tokenomy.exe
# 빌드: pyinstaller tokenomy.spec   →   dist/Tokenomy.exe
from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ['tokenomy/launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('config/pricing.json', 'config'),
        ('tokenomy/web/templates', 'tokenomy/web/templates'),
        ('tokenomy/web/static', 'tokenomy/web/static'),
    ],
    hiddenimports=collect_submodules('uvicorn'),
    hookspath=[],
    runtime_hooks=[],
    excludes=['pytest', 'httpx'],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Tokenomy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,           # 콘솔: 종료법 안내 + --version 스모크
    disable_windowed_traceback=False,
)
```

- [ ] **Step 3: 로컬 빌드 (수동 검증 — 인터넷 가능 환경)**

```bash
pip install pyinstaller
pyinstaller tokenomy.spec
```

Expected: `dist/Tokenomy.exe` 생성

- [ ] **Step 4: exe 스모크 검증 (수동)**

```bash
dist/Tokenomy.exe --version
```

Expected: `0.1.0` 출력

이어서 더블클릭(또는 인자 없이 실행) → 콘솔에 `[Tokenomy] http://127.0.0.1:8765/` → 브라우저 자동 오픈 → 대시보드 표시 → 데이터가 `~/.tokenomy/`에 생성됐는지 확인:

```bash
python -c "from pathlib import Path; p=Path.home()/'.tokenomy'; print(p, p.exists()); print(list(p.rglob('*'))[:10])"
```

Expected: `~/.tokenomy/data/tokenomy.db` 및 `~/.tokenomy/config/` 존재(첫 실행 후)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy.spec .gitignore
git commit -m "build: PyInstaller onefile spec(Tokenomy.exe) + 빌드 산출물 gitignore"
```

---

## Task 7: 릴리스 CI (`.github/workflows/release.yml`)

`v*` 태그 push 시 windows runner에서 exe를 빌드해 GitHub Release에 업로드한다. 태그와 `__version__` 불일치 시 빌드 실패.

**Files:**
- Create: `.github/workflows/release.yml`

- [ ] **Step 1: 워크플로 작성**

`.github/workflows/release.yml` 생성:

```yaml
name: release
on:
  push:
    tags: ['v*']

permissions:
  contents: write

jobs:
  build-windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install -r requirements.txt pyinstaller

      - name: Verify tag matches __version__
        shell: bash
        run: |
          TAG=${GITHUB_REF_NAME#v}
          VER=$(python -c "import tokenomy; print(tokenomy.__version__)")
          echo "tag=$TAG  __version__=$VER"
          if [ "$TAG" != "$VER" ]; then
            echo "::error::git tag ($TAG) != tokenomy.__version__ ($VER)"
            exit 1
          fi

      - name: Build exe
        run: pyinstaller tokenomy.spec

      - name: Smoke test exe
        shell: bash
        run: |
          OUT=$(./dist/Tokenomy.exe --version)
          echo "exe --version: $OUT"
          test "$OUT" = "$(python -c "import tokenomy; print(tokenomy.__version__)")"

      - name: Upload exe to release
        uses: softprops/action-gh-release@v2
        with:
          files: dist/Tokenomy.exe
```

- [ ] **Step 2: 검증 (수동 — 첫 릴리스 시)**

릴리스는 태그 push로 트리거된다. 검증 절차(문서화만, 실제 태깅은 배포 시점에):

```bash
# 1) __version__ 과 태그를 맞춘다 (예: 0.1.0)
# 2) git tag v0.1.0 && git push origin v0.1.0
# 3) GitHub Actions에서 build-windows 통과 + Release에 Tokenomy.exe 첨부 확인
```

YAML 문법 점검(로컬, 선택):

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/release.yml')); print('YAML OK')"
```

Expected: `YAML OK`

- [ ] **Step 3: 커밋**

```bash
git add .github/workflows/release.yml
git commit -m "ci: 태그 push 시 windows exe 빌드 → GitHub Release 업로드"
```

---

## Task 8: 문서 — README 설치/업데이트/SmartScreen

비개발자용 exe 설치 경로를 README 최상단 빠른 시작에 추가하고, 기존 소스 설치는 "개발자용"으로 보존한다. SmartScreen 통과법을 명시한다.

**Files:**
- Modify: `README.md`
- Modify: `README.ko.md`

- [ ] **Step 1: `README.ko.md` 빠른 시작 교체**

`README.ko.md`의 `## 빠른 시작` 섹션 전체(22–30줄 인근)를 교체:

```markdown
## 빠른 시작 (비개발자 — Windows)

1. [Releases](https://github.com/genius-kim-samsung/tokenomy/releases/latest)에서
   `Tokenomy.exe`를 내려받는다.
2. 더블클릭한다. (Windows SmartScreen 경고가 뜨면 **추가 정보 → 실행**을 누른다 —
   서명되지 않은 개인 도구라 뜨는 정상 경고다.)
3. 콘솔 창이 열리고 브라우저에 대시보드가 자동으로 뜬다. 데이터는
   `C:\Users\<이름>\.tokenomy\` 에 저장된다. **창을 닫으면 종료**된다.
4. 새 버전이 나오면 대시보드 상단에 알림 배너가 뜬다 — 눌러서 새 `Tokenomy.exe`를
   받아 기존 파일을 덮어쓰면 된다.

## 빠른 시작 (개발자 — 소스 실행)

\```bash
pip install -r requirements.txt
cp config/tokenomy.config.example.json config/tokenomy.config.json   # 예산 편집
python -m tokenomy.cli ingest
python -m tokenomy.cli report
python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765
\```

Windows는 `start_tokenomy.bat` 더블클릭(ingest → 대시보드 → 브라우저 자동 오픈).
```

(위 블록의 `\``` 는 실제 파일에선 백틱 3개로 적는다 — 코드펜스 중첩 표기일 뿐.)

- [ ] **Step 2: `README.md` 빠른 시작 교체 (영문)**

`README.md`의 `## Quick start` 섹션을 교체:

```markdown
## Quick start (non-developer — Windows)

1. Download `Tokenomy.exe` from
   [Releases](https://github.com/genius-kim-samsung/tokenomy/releases/latest).
2. Double-click it. (If Windows SmartScreen warns, click **More info → Run
   anyway** — it's the normal warning for an unsigned personal tool.)
3. A console window opens and the dashboard opens in your browser. Data is
   stored under `C:\Users\<you>\.tokenomy\`. **Close the window to quit.**
4. When a new version ships, the dashboard shows an update banner — click it,
   download the new `Tokenomy.exe`, and overwrite the old one.

## Quick start (developer — from source)

\```bash
pip install -r requirements.txt
cp config/tokenomy.config.example.json config/tokenomy.config.json   # then edit your budget
python -m tokenomy.cli ingest
python -m tokenomy.cli report
python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765
\```
```

- [ ] **Step 3: 문서 정합성 점검**

Run: `python -m pytest -q`
Expected: PASS (문서 변경이 테스트를 깨지 않음 — 최종 기준선 유지)

- [ ] **Step 4: 커밋**

```bash
git add README.md README.ko.md
git commit -m "docs: exe 설치/업데이트/SmartScreen 안내(비개발자) + 소스 경로 보존"
```

---

## 완료 기준

- [ ] `python -m pytest -q` 전체 통과 (기준선 112 + 신규 약 20 = ~132: test_paths 5 / test_update 8 / test_launcher 3 / db·budget·web 4)
- [ ] `pyinstaller tokenomy.spec` → `dist/Tokenomy.exe` 생성, `Tokenomy.exe --version` = `0.1.0`
- [ ] exe 실행 → 브라우저 대시보드 + 데이터가 `~/.tokenomy/`에 생성
- [ ] 대시보드에서 `check_update`가 새 태그를 줄 때 배너 노출(테스트로 검증)
- [ ] `v0.1.0` 태그 push 시 CI가 exe를 Release에 업로드(첫 배포 시 확인)
- [ ] 소스 실행(`python -m tokenomy.cli`, `start_tokenomy.bat`)이 기존대로 동작(repo 루트 데이터)

## 범위 밖 (설계 §10 — 후속)

- 코드사이닝 인증서, 완전 자동 업데이트(자기교체), 트레이 앱(pywebview)
- 백그라운드 상시 수집, Mac/Linux exe, 인스톨러(Inno Setup)
- 깨진 SessionEnd hook 경로 수정은 본 계획과 무관한 즉시 항목(별도 처리)
