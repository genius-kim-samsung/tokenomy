# Tokenomy Windows 배포(설치/업데이트 사용성) — 설계

**작성일:** 2026-06-12

## 1. 목표

비개발자가 **터미널·Python·git 없이** Tokenomy를 더블클릭으로 실행하고, 새 버전을
원클릭으로 받게 한다. Windows 단일 타겟, 기존 FastAPI 웹 대시보드를 그대로 재사용한다.

### 배경 / 문제

현재 설치는 `git clone` → `pip install -r requirements.txt` → config 복사 →
`start_tokenomy.bat` 더블클릭. 업데이트는 `git pull` 수동. 데이터(`config/`, `data/`)가
repo 내부에 있다. Git·Python 환경 구성이 비개발자에게 진입장벽이다.

### 타겟 사용자 (확정)

"완전 GUI 지향" — 터미널을 거의 열지 않는 사용자. Claude Code를 **VS Code/JetBrains
확장 또는 데스크탑 앱**으로 쓰는 경우 `~/.claude/projects`에 로그가 쌓이므로 데이터
소스는 존재한다(타겟 모순 없음).

### 제약

- Windows 단일 (Mac/Linux는 기존 수동 방식 유지)
- 업데이트는 **반자동**: 기동 시 알림 + 다운로드 링크(받기·교체는 사용자)
- 1인 프로젝트 — 구현 무게를 최소화(YAGNI)

## 2. 접근 (확정: A)

PyInstaller `--onefile`로 tokenomy + uvicorn + 의존성 + 템플릿/static을 **단일 `.exe`**
로 묶는다. 더블클릭 → 내장 런타임으로 ingest → 로컬 서버 → 브라우저 자동 오픈.
GitHub Releases에서 자동 빌드·배포, 기동 시 인앱 업데이트 체크.

(대안 B 인스톨러(Inno Setup), C 트레이 앱(pywebview)은 후속/범위 밖.)

## 3. 컴포넌트

| 파일 | 역할 | 작업 |
|---|---|---|
| `tokenomy/paths.py` | 데이터 루트 중앙 해석(frozen 분기) | 신규 |
| `tokenomy/db.py` / `archive.py` / `budget.py` | 경로를 `paths` 경유로 | 수정 |
| `tokenomy/launcher.py` | exe 진입점: ingest→포트탐색→브라우저→serve | 신규 |
| `tokenomy/update.py` | GitHub Releases 최신버전 확인 + semver 비교 | 신규 |
| `tokenomy/web/app.py` / `views.py` / `templates/dashboard.html` | 업데이트 배너 | 수정 |
| `tokenomy.spec` | PyInstaller onefile 정의(데이터 파일 포함) | 신규 |
| `.github/workflows/release.yml` | `v*` 태그 push → 빌드 → Release 업로드 | 신규 |
| `tests/test_paths.py` / `tests/test_update.py` | 단위 테스트 | 신규 |
| `README.md` / `README.ko.md` | exe 설치·첫 실행·SmartScreen 안내 | 수정 |

## 4. 데이터 경로 분리 (선결과제)

exe 배포 시 실행 위치(CWD)가 들쭉날쭉해 repo 내부 상대경로(`archive.ARCHIVE_ROOT =
Path("data/archive")`, DB 경로 등)가 깨진다. 데이터 루트를 한 곳에서 해석한다.

`tokenomy/paths.py`:

```
def data_dir() -> Path:
    # 우선순위: env override > exe(frozen) > 소스 실행
    env = os.environ.get("TOKENOMY_DATA")
    if env:
        base = Path(env)
    elif getattr(sys, "frozen", False):   # PyInstaller exe
        base = Path.home() / ".tokenomy"
    else:                                  # 소스/개발 실행: 기존 repo 루트 호환
        base = Path(__file__).resolve().parent.parent   # tokenomy/ 의 부모 = repo 루트
    base.mkdir(parents=True, exist_ok=True)
    return base
```

- **데이터 위치:** `~/.tokenomy/` (Windows: `C:\Users\<이름>\.tokenomy\`).
  Claude(`~/.claude`)·Codex(`~/.codex`) CLI 생태계 관례와 일관.
- config = `data_dir()/tokenomy.config.json`, DB = `data_dir()/tokenomy.db`,
  archive = `data_dir()/archive/`
- 소스 실행(비frozen)은 기존 repo 루트 유지 → 개발 편의 + 기존 사용자 호환
- 기존 `TOKENOMY_CONFIG` env(테스트 격리용)는 그대로 둔다. 신규 `TOKENOMY_DATA`는
  데이터 루트 전체 오버라이드
- 첫 실행 시 디렉토리 자동 생성. config 미존재 → 기존 온보딩 배너 흐름으로 자연 연결

**입력 데이터 소스**(`~/.claude/projects`, `~/.codex/sessions`)는 이미 홈 기반이라
변경 없음. 분리 대상은 Tokenomy가 **쓰는** 파생 데이터(config/DB/archive)뿐이다.

## 5. exe 진입점 (`launcher.py`)

PyInstaller 엔트리 = `launcher:main`. 동작:

1. `paths.data_dir()` 보장
2. **ingest 1회** — data-ingestion 계획이 후속으로 남긴 "기동 시 자동 수집(트리거 ②)"을
   여기서 해결. ingest 실패는 치명적이지 않게 처리(기존 데이터로 대시보드 표시)
3. 빈 포트 탐색(8765 점유 시 8766… 순차)
4. 브라우저 자동 오픈 후 uvicorn 기동(`127.0.0.1`, 로컬 전용)
5. 콘솔 창은 숨김/최소화(PyInstaller `--noconsole` 또는 런처에서 처리)

기존 `start_tokenomy.bat`은 소스 사용자용으로 유지(exe는 비개발자용). 두 경로를
README에 병기.

## 6. 인앱 업데이트 (`update.py` + 배너)

- `api.github.com/repos/genius-kim-samsung/tokenomy/releases/latest` 조회 →
  `tag_name`(예: `v0.2.0`) vs `__version__`("0.1.0") semver 비교
- 새 버전이면 대시보드 상단 배너: **"새 버전 vX.Y.Z 사용 가능 — 다운로드"** →
  클릭 시 **Releases `/latest` 페이지**를 브라우저로 오픈(asset 직링크가 아니라 페이지 —
  버전마다 URL이 안 바뀌어 안정적, 사용자가 변경점도 함께 봄)
- 받기·교체는 사용자가 새 exe로 교체(반자동). 완전 자동 자기교체는 무서명 exe라
  까다로워 범위 밖
- **1일 1회만 체크**: `meta` 테이블에 `last_update_check` 기록, 24h 미만이면 skip
- 네트워크 실패·타임아웃·오프라인은 **조용히 무시**(배너 없음, 앱 정상 동작)
- semver 비교는 stdlib만으로 구현(`packaging` 의존성 추가하지 않음 — 단순 튜플 비교)

## 7. 빌드/릴리스 CI (`.github/workflows/release.yml`)

- 트리거: `v*` 태그 push
- 러너: `windows-latest`
- 단계: checkout → Python setup → `pip install -r requirements.txt pyinstaller` →
  `pyinstaller tokenomy.spec` → `Tokenomy.exe`를 해당 태그 GitHub Release에 업로드
- 버전 단일출처: git 태그 ↔ `tokenomy/__init__.py:__version__` 일치 검증(불일치 시 빌드 실패)
- PR/push(비태그)에는 빌드 성공 스모크만(아티팩트 업로드 없음)

## 8. 배포 UX / SmartScreen

무서명 exe는 첫 실행 시 Windows SmartScreen이 "알 수 없는 게시자" 경고를 띄운다.

- README + Release 노트에 "추가 정보 → 실행" 안내(스크린샷 포함)
- 코드사이닝 인증서(연 비용)는 **범위 밖** — 추후 옵션
- Release 노트 템플릿: 변경점 + 설치 3줄(다운로드 → 더블클릭 → SmartScreen 통과)

## 9. 테스트 전략

- `test_paths`: frozen/비frozen/`TOKENOMY_DATA` env 분기, 디렉토리 자동 생성
- `test_update`: semver 비교 4케이스(같음/원격높음/원격낮음/잘못된 태그) +
  네트워크 모킹(정상 응답/타임아웃/HTTP 에러 → 조용히 무시), 1일 1회 캐시
- `test_web`: 업데이트 배너 렌더(있음/없음)
- CI: PyInstaller 빌드 성공 스모크(exe 산출물 존재 확인)

## 10. 범위 밖 (YAGNI / 후속)

- 코드사이닝 인증서
- 완전 자동 업데이트(exe 자기교체 + 재시작)
- 트레이 앱(pywebview/Tauri) — 접근 C
- 백그라운드 상시 수집(앱 미실행 시 수집) — 현재는 기동 시 ingest로 충분
- Mac/Linux exe
- 인스톨러(Inno Setup `setup.exe`, 시작메뉴 바로가기/언인스톨) — 접근 B, 후속

## 11. 다른 계획과의 관계

- **data-ingestion-reliability** 후속의 "웹/CLI 기동 시 자동 ingest(트리거 ②)"를
  §5(launcher) ingest 단계가 해결한다.
- **public-generalization** 후속의 "config 홈 경로(`~/.tokenomy/`) 지원"을
  §4(데이터 경로 분리)가 해결한다.
- 별개 사안: 깨진 SessionEnd hook 경로(`tokenomy` → 존재하지 않는 디렉토리)는
  본 계획과 무관한 즉시 수정 항목. exe 배포 후에도 소스/개발 환경의 hook은 유효.
