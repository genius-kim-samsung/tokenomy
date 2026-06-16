# Tokenomy 네이티브 데스크톱 셸 (pywebview) — 설계

- 날짜: 2026-06-12
- 상태: 설계 승인 대기
- 작성: brainstorming 세션 결과

## 1. 배경 / 문제

현재 Tokenomy는 PyInstaller onefile(`Tokenomy.exe`, ~19MB)로 배포된다. 더블클릭하면:

1. **콘솔 창**이 백그라운드에 떠 있다 (`tokenomy.spec`의 `console=True`).
2. 대시보드가 **외부 기본 브라우저 탭**에 뜬다 (`launcher.py`의 `webbrowser.open()`).

이 두 가지 때문에 "네이티브 앱" 느낌이 전혀 없다. 목표는 콘솔을 없애고, 외부 브라우저 대신 **자체 앱 창**에 대시보드를 띄우며, 작업표시줄/창 아이콘을 갖춘 단발(single-shot) 데스크톱 앱으로 만드는 것이다.

### 목표 (Goals)

- **창만 네이티브**: 콘솔 제거 + 독립 앱 창 + 작업표시줄/창 아이콘.
- **단발 앱**: 실행 시 최신 ingest 1회, 창을 닫으면 프로세스 완전 종료.
- **Windows 우선**, 코드는 향후 macOS/Linux로 확장 가능한 구조 유지.
- **기존 자산 무변경 재활용**: 도메인 로직(1,400줄) + 테스트(1,442줄) + FastAPI/Jinja/Chart.js UI를 그대로 둔다.

### 비목표 (Non-goals)

- 백엔드 언어 재작성 (Go/Rust/TS 포팅은 이번 범위 아님).
- 트레이 상주 / 백그라운드 자동 갱신 (필요해지면 별도 프로젝트로).
- OS 네이티브 위젯/메뉴/룩앤필 네이티브화.
- 코드서명 (기존과 동일하게 미서명, SmartScreen 감수).

## 2. 결정 근거 (왜 pywebview)

| 후보 | 기존 자산 | 재작성량 | 배포 크기 | 크로스플랫폼 | 판정 |
|------|----------|---------|----------|-------------|------|
| **pywebview** | 100% 재활용 | `launcher.py` 교체 + 스니펫 1개 | ~19MB 유지 | WebView2/WKWebView/WebKitGTK 자동 | **채택** |
| Tauri (Rust) | 백엔드 재작성 | 2,800줄 재작성+재검증 + Rust 학습 | ~3–10MB | 우수 | 기각 (비용↑, 동기 약함) |
| Wails (Go) | 백엔드 재작성 | 2,800줄 재작성+재검증 + Go 학습 | ~10–20MB | 우수 | 기각 (현 시점 ROI 낮음) |
| Electron (TS) | 백엔드 재작성 | 2,800줄 재작성 | **100MB+** | 우수 | 기각 (크기 퇴행) |

핵심 통찰: pywebview는 그린필드 정답(Wails/Tauri)과 **아키텍처 철학이 동일**하다 — `[네이티브 셸] + [OS WebView] + [웹 UI]`. 차이는 셸 구현 언어(Python vs Go/Rust)뿐이고, 그 차이는 순수하게 배포 품질(크기·서명·자동업데이트)에서만 드러난다. 따라서:

- **셸 아키텍처**는 지금 확정하는 것이 옳다 → WebView 철학 채택.
- **언어**는 지금 바꿀 필요 없다 → 나중에 배포 품질이 사업적으로 중요해지면, 그때 HTML/CSS/Chart.js 프론트를 그대로 들고 백엔드만 포팅하면 된다. **pywebview는 미래 재작성 경로를 막지 않는다.**

재작성으로 얻는 이득(작은 바이너리)은 정작 호소한 통증(콘솔/브라우저)과 무관하고, 그 통증은 pywebview로 **코드 0줄 재작성**으로 해결된다.

## 3. 아키텍처

```
[더블클릭 Tokenomy.exe]
  └─ launcher.main()
       ├─ (argv 처리: --version 등은 webview import 전에 반환 — 헤드리스 CI smoke 보존)
       ├─ _safe_ingest()                  # 기존 그대로
       ├─ port = find_free_port()         # 기존 그대로
       ├─ uvicorn을 daemon thread로 기동 (127.0.0.1:port, log_level=warning)
       ├─ _wait_until_ready(port)         # 기존 대기 루프 재활용 (소켓 connect_ex)
       │     └─ 타임아웃 시 fallback: webbrowser.open + 콘솔 안내 (WebView 미가용 환경)
       └─ webview.create_window("Tokenomy", f"http://127.0.0.1:{port}/",
                                width=1200, height=800, js_api=Api())  # 크기는 추후 조정
          webview.start()                 # ← GUI 메인루프 (블로킹)
             └─ 창 닫힘 → start() 반환 → 데몬 스레드와 함께 프로세스 종료 = 단발 앱
```

- Windows는 OS 내장 **WebView2(Edge Chromium)** 렌더러를 사용 → exe 크기 거의 불변.
- FastAPI 라우트 / Jinja 템플릿 / Chart.js / SQLite / parser / aggregate 전부 **무변경**.
- in-process 운영: HTTP 서버(127.0.0.1)는 유지하되 외부 브라우저 대신 WebView 창이 그 URL을 로드. 기존 라우팅·상대경로 링크가 그대로 동작한다.

## 4. 변경 파일

| 파일 | 변경 내용 |
|------|----------|
| `tokenomy/launcher.py` | pywebview 진입점으로 교체. `webbrowser.open` → `webview.create_window`/`webview.start`. uvicorn을 데몬 스레드로 기동. `Api` 클래스(`open_external`) 추가. WebView 미가용 시 fallback. webview는 `main()` 내부 지연 import. |
| `tokenomy.spec` | `console=False` (콘솔 제거), `icon='assets/tokenomy.ico'`, pywebview 백엔드 번들 처리(hiddenimports/hook). |
| `requirements.txt` | `pywebview` 추가 (Windows 백엔드는 `pythonnet` 경유 EdgeChromium). |
| `tokenomy/web/templates/base.html` | 외부 링크 가로채기 JS 스니펫 1개 (아래 5절). |
| `assets/tokenomy.ico` | 신규 — 창/작업표시줄/exe 아이콘. |

## 5. 외부 링크 처리

WebView 안에서 외부 링크(`target="_blank"`)를 열면 작은 앱 창 안에 GitHub 페이지가 로드되어 어색하다(뒤로가기도 없음). 외부 링크는 **기본 브라우저**로 연다.

- 현재 외부 링크는 **단 하나**: `dashboard.html:18`의 업데이트 배너 "다운로드"(GitHub releases). 나머지는 전부 내부 라우트(상대경로)라 WebView 안에서 정상 동작.
- 구현:
  - `launcher`에 `Api` 객체 등록: `open_external(url)` → `webbrowser.open(url)`.
  - `base.html`에 스니펫: `document` 클릭을 위임 처리하여, `http(s)`로 시작하되 호스트가 `127.0.0.1`/`localhost`가 아닌 앵커는 기본 동작을 막고 `window.pywebview.api.open_external(href)` 호출.
- 이것이 웹 코드에 가하는 **유일한** 변경이다(스니펫 1개). `pywebview.api`가 없는 일반 브라우저(개발 모드)에서는 스니펫이 no-op이 되어 링크가 평소대로 동작하도록 가드한다.

## 6. 빌드 / 패키징

- `console=False` (windowed) → 콘솔 창 제거. 단, 이로 인해 stdout 기반 안내가 사라지므로 사용자 피드백은 WebView 창 자체가 담당.
- `icon='assets/tokenomy.ico'` → exe/창/작업표시줄 아이콘.
- pywebview Windows 백엔드(EdgeChromium)는 `pythonnet`(.NET interop)을 경유한다. PyInstaller가 관련 어셈블리/네이티브 의존성을 수집하도록 hook/hiddenimports를 보강한다(pywebview 제공 hook 활용).
- onefile 형식 유지. 태그 push 시 GitHub Release 업로드하는 기존 CI 흐름 유지.
- 빌드 후 **exe 크기 실측** → README 수치 갱신 (pythonnet으로 수 MB 증가 가능).

## 7. 개발 워크플로 (이중 진입점)

- **개발**: `python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765` → 일반 브라우저(hot-reload, DevTools). **유지** — pywebview를 거치지 않는다.
- **배포/네이티브 실행**: `launcher.main()` → pywebview 창 → exe.
- `--version`/`-V` 등 CLI 경로는 webview import **전에** 처리하여, webview가 없는 헤드리스 CI에서도 smoke 테스트가 통과하도록 한다.

## 8. 리스크 & 전제

| 리스크 | 대응 |
|--------|------|
| 구형 Win10에 WebView2 런타임 부재 | `webview.start()` 실패 시 `webbrowser.open` + 안내로 **graceful fallback**. Win11은 100% 내장. |
| pythonnet 번들로 exe 크기 증가 | 빌드 후 실측, README 갱신. onefile 의존성 그래프 점검. |
| `console=False`로 stdout 안내 소실 | 기동 단계 피드백을 WebView 창/대기 로직으로 대체. fallback 경로에서는 일시적으로 콘솔이 필요할 수 있어, 안내를 메시지박스 또는 로그파일로 보완 검토. |
| 미서명 exe / SmartScreen | 기존과 동일(변화 없음). README 안내 유지. |

## 9. 테스트 전략

- `launcher` 단위 테스트: `find_free_port`, `_safe_ingest`, readiness 대기, fallback 분기. **webview는 모킹**(헤드리스 CI에서 GUI 미기동).
- `Api.open_external`이 외부 URL에만 `webbrowser.open`을 호출하는지 검증.
- 기존 web/parser/db/aggregate 테스트는 **불변** — 회귀만 확인.
- CI smoke: `Tokenomy.exe --version`이 webview 없이 동작.

## 10. 크로스플랫폼 확장 경로 (이번엔 Windows만)

동일한 `launcher.py`가 그대로 작동 — pywebview가 macOS는 WKWebView, Linux는 WebKitGTK를 자동 선택한다. 향후 확장 시 추가 작업은 **각 OS용 패키징(.app/.AppImage)과 아이콘**뿐이고 애플리케이션 코드는 공유된다.

## 11. 작업 항목 (구현 플랜은 writing-plans 단계에서 상세화)

1. `requirements.txt`에 `pywebview` 추가, 로컬 설치.
2. `launcher.py` 재작성: 데몬 스레드 uvicorn + readiness 대기 + webview 창 + `Api` + fallback + 지연 import.
3. `base.html` 외부 링크 가로채기 스니펫 추가(브라우저 가드 포함).
4. `assets/tokenomy.ico` 추가.
5. `tokenomy.spec` 수정: `console=False`, `icon`, pywebview 번들 hook.
6. `launcher` 단위 테스트 작성/갱신(webview 모킹).
7. 빌드 → 콘솔 미표시·자체 창·아이콘·외부 링크 동작 수동 검증 → exe 크기 실측 → README 갱신.
