# 네이티브 데스크톱 셸 (pywebview) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tokenomy 실행 시 콘솔 창과 외부 브라우저 대신, OS 내장 WebView를 쓰는 자체 앱 창에 대시보드를 띄운다.

**Architecture:** `launcher.py`만 교체한다. WebView 가용 시 uvicorn을 데몬 스레드로 띄우고 pywebview 창(메인 스레드 블로킹)에 `127.0.0.1`을 로드한다. 창을 닫으면 프로세스가 종료된다(단발 앱). WebView 미가용 환경(구형 Win10)에서는 기존 방식(기본 브라우저 + uvicorn 메인 블로킹)으로 graceful fallback한다. FastAPI/Jinja/Chart.js/SQLite/parser 등 기존 자산은 무변경.

**Tech Stack:** Python, pywebview(Windows: pythonnet 경유 WebView2/EdgeChromium), FastAPI, uvicorn, PyInstaller(onefile), pytest.

---

## 참고 설계 문서

`docs/superpowers/specs/2026-06-12-native-desktop-shell-pywebview-design.md`

## 사전 정보 (코드베이스 컨텍스트)

- 테스트는 pytest. 설정 파일(pyproject/pytest.ini) 없이 기본 discovery로 동작. 실행: `python -m pytest tests/ -v`.
- 가상환경은 `.venv/`. 명령은 활성화된 venv 기준으로 기술한다(Windows: `.\.venv\Scripts\activate`).
- `tokenomy/launcher.py`의 기존 공개 심볼: `main()`, `find_free_port()`, `__version__`(re-export). 기존 테스트(`tests/test_launcher.py`)가 이 셋에 의존하므로 **시그니처를 유지**한다.
- `tokenomy/web/app.py`의 `app`(FastAPI), `tokenomy/cli.py`의 `cmd_ingest`, `tokenomy/db.py`의 `connect`는 그대로 사용.
- `.gitignore`가 `docs/`를 제외하므로 이 플랜·스펙 문서는 커밋되지 않는다(소스/테스트/spec/assets/README는 정상 커밋 대상).

## File Structure

| 파일 | 책임 | 변경 |
|------|------|------|
| `tokenomy/launcher.py` | exe 진입점: ingest → 포트 → 서버 → 창/브라우저 분기 | 교체 |
| `tests/test_launcher.py` | 런처 단위 테스트(webview는 모킹) | 추가 |
| `tokenomy/web/templates/base.html` | 외부 링크를 기본 브라우저로 보내는 스니펫 | 수정 |
| `assets/tokenomy.ico` | 창/작업표시줄/exe 아이콘 | 신규 |
| `scripts/make_icon.py` | 임시 아이콘 생성(일회성, 런타임 무관) | 신규 |
| `tokenomy.spec` | `console=False`, `icon`, pywebview 번들 | 수정 |
| `requirements.txt` | `pywebview` 추가 | 수정 |
| `README.md`, `README.ko.md` | "콘솔 창" 설명 → "앱 창"으로 갱신 | 수정 |

---

## Task 1: pywebview 의존성 추가

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: requirements.txt 확인**

Run: `cat requirements.txt`
Expected: 현재 의존성 목록 표시(`fastapi`, `uvicorn`, `jinja2` 등).

- [ ] **Step 2: pywebview 추가**

`requirements.txt` 끝에 한 줄 추가:

```
pywebview>=5.0
```

- [ ] **Step 3: 설치**

Run: `python -m pip install -r requirements.txt`
Expected: `pywebview` 및 Windows 의존성(`pythonnet`, `proxy_tools` 등) 설치 성공.

- [ ] **Step 4: import 가능 확인**

Run: `python -c "import webview; print(webview.__version__)"`
Expected: 버전 문자열 출력(예: `5.x`). 오류 없이 종료.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt
git commit -m "build: pywebview 의존성 추가"
```

---

## Task 2: 외부 링크 브리지 `Api` 클래스 (TDD)

WebView 안의 외부 링크 클릭을 기본 브라우저로 넘기는 JS 브리지 객체. `http`/`https` URL만 연다.

**Files:**
- Modify: `tokenomy/launcher.py`
- Test: `tests/test_launcher.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_launcher.py` 상단 import는 그대로 두고, 파일 끝에 추가:

```python
def test_api_open_external_opens_http(monkeypatch):
    opened = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda u: opened.append(u))
    launcher.Api().open_external("https://example.com/x")
    assert opened == ["https://example.com/x"]


def test_api_open_external_ignores_non_http(monkeypatch):
    opened = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda u: opened.append(u))
    launcher.Api().open_external("javascript:alert(1)")
    launcher.Api().open_external("/settings")
    launcher.Api().open_external(None)
    assert opened == []
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest tests/test_launcher.py::test_api_open_external_opens_http -v`
Expected: FAIL — `AttributeError: module 'tokenomy.launcher' has no attribute 'Api'`.

- [ ] **Step 3: `Api` 클래스 구현**

`tokenomy/launcher.py`에서 `import webbrowser`가 이미 있는지 확인(있음). `from tokenomy import __version__` 아래에 추가:

```python
WINDOW_TITLE = "Tokenomy"
WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 800


class Api:
    """pywebview JS 브리지 — 외부 링크를 기본 브라우저로 연다."""

    def open_external(self, url: str) -> None:
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            webbrowser.open(url)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m pytest tests/test_launcher.py -k api_open_external -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add tokenomy/launcher.py tests/test_launcher.py
git commit -m "feat(launcher): 외부 링크 브리지 Api 추가"
```

---

## Task 3: 서버 준비 대기 `_wait_until_ready` (TDD)

기존 `_open_browser_when_ready` 안에 섞여 있던 "포트 응답 대기" 로직을 순수 함수로 분리한다(브라우저 오픈과 분리해서 테스트 가능하게).

**Files:**
- Modify: `tokenomy/launcher.py`
- Test: `tests/test_launcher.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_launcher.py` 끝에 추가:

```python
def test_wait_until_ready_true_when_serving():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        assert launcher._wait_until_ready(port, timeout=1.0) is True


def test_wait_until_ready_false_when_closed():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # 위 with 블록 종료로 포트는 닫힘 — listen하는 곳이 없음
    assert launcher._wait_until_ready(port, timeout=0.5) is False
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest tests/test_launcher.py -k wait_until_ready -v`
Expected: FAIL — `AttributeError: ... has no attribute '_wait_until_ready'`.

- [ ] **Step 3: `_wait_until_ready` 구현**

`tokenomy/launcher.py`에 추가(`Api` 아래 권장). `import time`은 이미 있음:

```python
def _wait_until_ready(port: int, timeout: float = 10.0, interval: float = 0.25) -> bool:
    """서버가 127.0.0.1:port에서 응답할 때까지 대기. 준비되면 True, 타임아웃이면 False."""
    for _ in range(max(1, int(timeout / interval))):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(interval)
    return False
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m pytest tests/test_launcher.py -k wait_until_ready -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add tokenomy/launcher.py tests/test_launcher.py
git commit -m "refactor(launcher): 서버 준비 대기를 _wait_until_ready로 분리"
```

---

## Task 4: `main()` 재구성 — WebView 창 / 브라우저 fallback 분기 (TDD)

진입점을 새 흐름으로 교체한다. `--version`은 webview import **전에** 처리해 헤드리스 CI에서도 동작하게 한다.

**Files:**
- Modify: `tokenomy/launcher.py`
- Test: `tests/test_launcher.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_launcher.py` 끝에 추가:

```python
def test_main_uses_window_when_webview_available(monkeypatch):
    calls = {}
    monkeypatch.setattr(launcher, "_safe_ingest", lambda: None)
    monkeypatch.setattr(launcher, "find_free_port", lambda: 9999)
    monkeypatch.setattr(launcher, "_webview_available", lambda: True)
    monkeypatch.setattr(launcher, "_wait_until_ready", lambda port, **k: True)
    monkeypatch.setattr(launcher, "_serve", lambda port: None)
    monkeypatch.setattr(launcher, "_launch_window",
                        lambda port: calls.__setitem__("window", port))
    monkeypatch.setattr(launcher, "_open_browser_when_ready",
                        lambda port: calls.__setitem__("browser", port))
    launcher.main([])
    assert calls.get("window") == 9999
    assert "browser" not in calls


def test_main_falls_back_to_browser_when_no_webview(monkeypatch):
    calls = {}
    monkeypatch.setattr(launcher, "_safe_ingest", lambda: None)
    monkeypatch.setattr(launcher, "find_free_port", lambda: 9999)
    monkeypatch.setattr(launcher, "_webview_available", lambda: False)
    monkeypatch.setattr(launcher, "_serve", lambda port: calls.__setitem__("serve", port))
    monkeypatch.setattr(launcher, "_launch_window",
                        lambda port: calls.__setitem__("window", port))
    monkeypatch.setattr(launcher, "_open_browser_when_ready",
                        lambda port: calls.__setitem__("browser", port))
    launcher.main([])
    assert calls.get("serve") == 9999
    assert calls.get("browser") == 9999
    assert "window" not in calls
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m pytest tests/test_launcher.py -k main_ -v`
Expected: FAIL — `_webview_available` / `_serve` / `_launch_window` 미정의.

- [ ] **Step 3: 헬퍼와 `main()` 구현**

`tokenomy/launcher.py`에서 다음을 적용한다.

(3a) 헬퍼 추가(`_wait_until_ready` 아래):

```python
def _webview_available() -> bool:
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


def _serve(port: int) -> None:
    """uvicorn 기동(블로킹). 데몬 스레드 또는 메인 스레드에서 호출."""
    import uvicorn
    from tokenomy.web.app import app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def _launch_window(port: int) -> None:
    """pywebview 창을 띄운다(블로킹). 창을 닫으면 반환된다."""
    import webview
    webview.create_window(
        WINDOW_TITLE, f"http://127.0.0.1:{port}/",
        width=WINDOW_WIDTH, height=WINDOW_HEIGHT, js_api=Api(),
    )
    webview.start()
```

(3b) 기존 `_open_browser_when_ready`는 fallback용으로 유지하되, 대기 로직을 `_wait_until_ready` 재사용으로 정리:

```python
def _open_browser_when_ready(port: int) -> None:
    if _wait_until_ready(port):
        webbrowser.open(f"http://127.0.0.1:{port}/")
    else:
        print(f"[launcher] 서버가 {port}에서 응답하지 않아 브라우저를 열지 않습니다")
```

(3c) 기존 `main()` 본문 전체를 아래로 교체:

```python
def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] in ("--version", "-V"):
        print(__version__)
        return

    _safe_ingest()
    port = find_free_port()

    if _webview_available():
        # 서버는 데몬 스레드, 창이 메인 스레드를 점유 → 창 닫으면 프로세스 종료(단발 앱)
        threading.Thread(target=_serve, args=(port,), daemon=True).start()
        if not _wait_until_ready(port):
            print(f"[Tokenomy] 서버가 {port}에서 응답하지 않습니다")
            return
        _launch_window(port)
    else:
        # WebView 미가용(구형 환경) — 기존 방식: 브라우저 + uvicorn 메인 블로킹
        threading.Thread(
            target=_open_browser_when_ready, args=(port,), daemon=True
        ).start()
        print(f"[Tokenomy] http://127.0.0.1:{port}/  (이 창을 닫으면 종료됩니다)")
        _serve(port)
```

기존 `main()` 안에 있던 인라인 `import uvicorn` / `from tokenomy.web.app import app` 줄은 `_serve`로 옮겨졌으므로 **제거**한다.

- [ ] **Step 4: 신규 + 기존 테스트 모두 통과 확인**

Run: `python -m pytest tests/test_launcher.py -v`
Expected: 기존 `test_version_flag`, `test_find_free_port_*` 포함 전부 PASS. 신규 `test_main_*` 2개 PASS.

- [ ] **Step 5: 전체 회귀 확인**

Run: `python -m pytest tests/ -q`
Expected: 전체 통과(웹/파서/db 등 불변).

- [ ] **Step 6: Commit**

```bash
git add tokenomy/launcher.py tests/test_launcher.py
git commit -m "feat(launcher): WebView 창 진입점 + 브라우저 fallback"
```

---

## Task 5: 외부 링크 가로채기 스니펫 (`base.html`)

WebView 안에서 외부(비 localhost) 링크 클릭을 `Api.open_external`로 위임한다. 일반 브라우저(개발 모드)에서는 `window.pywebview`가 없어 자동으로 평소 동작.

**Files:**
- Modify: `tokenomy/web/templates/base.html`

- [ ] **Step 1: 스니펫 추가**

`base.html`의 `{% block scripts %}{% endblock %}` 줄과 `</body>` 사이에 삽입:

```html
  {% block scripts %}{% endblock %}
  <script>
    // 외부 링크(비 localhost http/https)는 네이티브 창 대신 기본 브라우저로.
    // pywebview 환경에서만 가로채고, 일반 브라우저에서는 가드로 no-op.
    document.addEventListener('click', function (e) {
      var a = e.target.closest && e.target.closest('a[href]');
      if (!a) return;
      var href = a.href;
      if (!/^https?:\/\//i.test(href)) return;
      var host;
      try { host = new URL(href).hostname; } catch (_) { return; }
      if (host === '127.0.0.1' || host === 'localhost') return;
      if (window.pywebview && window.pywebview.api && window.pywebview.api.open_external) {
        e.preventDefault();
        window.pywebview.api.open_external(href);
      }
    });
  </script>
```

- [ ] **Step 2: 개발 모드 회귀 확인 (스니펫이 일반 브라우저를 깨지 않는지)**

Run: `python -m pytest tests/test_web.py -q`
Expected: 통과(렌더링된 HTML에 스니펫이 포함되어도 기존 단언에 영향 없음).

- [ ] **Step 3: Commit**

```bash
git add tokenomy/web/templates/base.html
git commit -m "feat(web): 외부 링크를 기본 브라우저로 여는 스니펫"
```

---

## Task 6: 아이콘 + PyInstaller spec

**Files:**
- Create: `assets/tokenomy.ico`
- Create: `scripts/make_icon.py`
- Modify: `tokenomy.spec`

- [ ] **Step 1: 아이콘 생성 스크립트 작성**

`scripts/make_icon.py` 생성(Pillow는 개발 일회성 도구 — 런타임/빌드 의존성 아님, `tokenomy.spec`의 `excludes`에서 `PIL` 제외 유지):

```python
"""임시 앱 아이콘 생성(일회성). 사용: pip install pillow && python scripts/make_icon.py"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "assets" / "tokenomy.ico"
OUT.parent.mkdir(parents=True, exist_ok=True)

img = Image.new("RGBA", (256, 256), (37, 99, 235, 255))  # Tokenomy 블루
d = ImageDraw.Draw(img)
d.text((96, 86), "T", fill=(255, 255, 255, 255))  # 단순 마크(추후 교체)
img.save(OUT, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
print(f"wrote {OUT}")
```

- [ ] **Step 2: 아이콘 생성 실행**

Run: `python -m pip install pillow && python scripts/make_icon.py`
Expected: `wrote .../assets/tokenomy.ico` 출력, 파일 생성.

- [ ] **Step 3: spec 수정 — 콘솔 제거 + 아이콘 + pywebview 번들**

`tokenomy.spec`을 아래로 교체:

```python
# PyInstaller onefile spec — Tokenomy.exe
# 빌드: pyinstaller tokenomy.spec   →   dist/Tokenomy.exe
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

a = Analysis(
    ['tokenomy/launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('config/pricing.json', 'config'),
        ('tokenomy/web/templates', 'tokenomy/web/templates'),
        ('tokenomy/web/static', 'tokenomy/web/static'),
    ] + collect_data_files('webview'),
    hiddenimports=(
        collect_submodules('uvicorn')
        + collect_submodules('webview')
        + ['clr']  # pythonnet(.NET interop) — Windows EdgeChromium 백엔드
    ),
    hookspath=[],
    runtime_hooks=[],
    # 런타임 미사용 의존성 제외. PIL/numpy는 Tokenomy/pywebview 런타임이 import하지 않음.
    excludes=['pytest', '_pytest', 'httpx', 'numpy', 'PIL', 'setuptools'],
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
    console=False,                 # 콘솔 제거 — 네이티브 창
    icon='assets/tokenomy.ico',
    disable_windowed_traceback=False,
)
```

- [ ] **Step 4: Commit**

```bash
git add assets/tokenomy.ico scripts/make_icon.py tokenomy.spec
git commit -m "build: 아이콘 + console=False + pywebview 번들 spec"
```

---

## Task 7: 빌드 · 수동 검증 · 문서 갱신

자동 테스트로 못 잡는 GUI/패키징을 실제 빌드로 검증한다.

**Files:**
- Modify: `README.md`, `README.ko.md`

- [ ] **Step 1: exe 빌드**

Run: `python -m PyInstaller tokenomy.spec`
Expected: `dist/Tokenomy.exe` 생성, 빌드 에러 없음.

> 빌드는 성공했으나 실행 시 `ModuleNotFoundError`/`clr` 관련 오류가 나면, 누락 모듈명을 `tokenomy.spec`의 `hiddenimports`에 추가하고 재빌드한다. pywebview의 자체 PyInstaller hook이 대부분 처리하지만 환경에 따라 보강이 필요할 수 있다.

- [ ] **Step 2: 수동 검증 체크리스트**

`dist/Tokenomy.exe`를 더블클릭하고 확인:

- [ ] 콘솔 창이 **뜨지 않는다**
- [ ] "Tokenomy" 제목의 자체 앱 창이 뜨고 대시보드가 보인다
- [ ] 작업표시줄/창 아이콘이 적용돼 있다
- [ ] 내부 네비게이션(설정/세션/탭/정렬 링크)이 창 안에서 동작한다
- [ ] (업데이트 배너가 있을 때) "다운로드" 클릭 → **기본 브라우저**로 GitHub releases가 열린다
- [ ] 창을 닫으면 프로세스가 완전히 종료된다(작업관리자에 `Tokenomy.exe` 잔존 없음)

- [ ] **Step 3: `--version` 스모크(헤드리스 경로) 확인**

Run: `dist/Tokenomy.exe --version`
Expected: 버전 문자열만 출력하고 즉시 종료(창/서버 미기동).

- [ ] **Step 4: exe 크기 실측**

Run: `ls -lh dist/Tokenomy.exe`
Expected: 크기 확인(pythonnet 번들로 기존 ~19MB 대비 증가 가능). 수치 기록.

- [ ] **Step 5: README 갱신**

`README.md`의 Quick start(non-developer) 3번 항목을 콘솔→앱 창으로 수정. 현재 문구:

```
3. A console window opens and the dashboard opens in your browser. Data is
   stored under `C:\Users\<you>\.tokenomy\` (in the `data\` and `config\`
   subfolders). **Close the window to quit.**
```

다음으로 교체:

```
3. The Tokenomy app window opens with the dashboard. Data is stored under
   `C:\Users\<you>\.tokenomy\` (in the `data\` and `config\` subfolders).
   **Close the window to quit.**
```

`README.ko.md`에서 동일하게 "콘솔 창" 문구를 "Tokenomy 앱 창"으로 수정한다(해당 줄을 `cat README.ko.md`로 찾아 대응 문구 교체).

- [ ] **Step 6: 전체 테스트 최종 확인**

Run: `python -m pytest tests/ -q`
Expected: 전체 통과.

- [ ] **Step 7: Commit**

```bash
git add README.md README.ko.md
git commit -m "docs: 콘솔 창 → Tokenomy 앱 창 안내 갱신"
```

---

## Self-Review (작성자 점검 결과)

- **스펙 커버리지:** 스펙 §3 아키텍처→Task 4, §4 변경 파일→Task 1~6 전부 매핑, §5 외부 링크→Task 2+5, §6 빌드→Task 6+7, §8 fallback→Task 4, §9 테스트→Task 2~4, §10 크로스플랫폼→코드 변경 없음(설계상 자동), §11 작업항목 7개→Task 1~7. 누락 없음.
- **플레이스홀더:** 모든 코드 스텝에 실제 코드/명령/기대출력 포함. 아이콘은 생성 스크립트로 구체화.
- **타입 일관성:** `Api.open_external`, `_wait_until_ready(port, timeout, interval)`, `_webview_available()`, `_serve(port)`, `_launch_window(port)`, `_open_browser_when_ready(port)` — 정의와 호출부 시그니처 일치 확인. `WINDOW_TITLE/WIDTH/HEIGHT` 상수 Task 2에서 정의, Task 4에서 사용.
- **알려진 한계:** WebView 미가용 fallback은 `console=False` 빌드에서 종료 UX가 약하다(콘솔 닫기 불가). 구형 Win10 일부에 한정되며 Win11은 도달하지 않음. 우아한 종료는 트레이 상주(비목표)가 필요하므로 이번 범위에서 제외.
