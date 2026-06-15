# Tokenomy

AI 코딩 토큰 지출용 로컬 "가계부". Claude Code / Codex CLI의 **로컬** 세션 로그(JSONL)를
파싱해 예산 대비 번다운(Claude 월간·Codex 주간 누적)·프로젝트/세션별 비용·캐시 효율을
보여준다. 단발(single-shot) 데스크톱 앱(exe) 또는 소스로 실행. 전 과정 로컬,
**DB에는 토큰 메타 + 세션 식별용 첫 프롬프트 발췌만 적재**한다(대화 본문 전체는 미저장 — raw JSONL 원문은 archive.py가 약 30일 임시 보존).

## 명령어

```powershell
# 개발 실행 (소스) — 데이터는 repo 루트의 data\, config\ 아래에 쌓인다
.venv\Scripts\python -m tokenomy.cli ingest    # 세션 로그 → SQLite (증분)
.venv\Scripts\python -m tokenomy.cli report    # 터미널 요약(번다운 + Top 프로젝트)
.venv\Scripts\python -m tokenomy.cli all       # ingest 후 report
.venv\Scripts\python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765

# 배치 (Windows 더블클릭)
start_tokenomy_dev.bat     # 개발용
start_tokenomy.bat         # ingest → 대시보드 → 브라우저 자동 열기

# 테스트
.venv\Scripts\python -m pytest
# 프론트엔드 스타일 빌드 — CSS/템플릿 클래스 변경 시에만. 산출 app.css는 커밋(런타임 무빌드).
.\build_css.ps1

# exe 빌드 — 반드시 .venv로 (아래 게시 참고)
.venv\Scripts\python -m PyInstaller tokenomy.spec   # → dist\Tokenomy.exe
```

## 아키텍처

단방향 데이터 파이프라인:

```
~/.claude/projects/**/*.jsonl      ─ parser.py ───────┐
~/.codex/sessions/**/rollout-*     ─ codex_parser.py ─┤→ UsageRecord → db.py(SQLite) → aggregate.py ─┬→ cli.py (report)
                                                      │                                              └→ web/ (FastAPI+Jinja2)
                                                 archive.py (raw 30일 휘발 전 원문 보존)
```

- **parser.py / codex_parser.py** — 각 도구 로그를 공통 `UsageRecord`로 정규화. 새 도구 추가는
  여기에 모듈 하나 더(README의 "Adding a parser" 참고).
- **db.py** — SQLite 적재. `messages`(메시지별 토큰/비용, `dedup_key` UNIQUE로 중복 제거),
  `sessions`(메타 + 요약), `scan_offsets`(증분 스캔용 byte-offset). 스키마 변경은 `_MIGRATE_COLS`에 ALTER 추가.
- **aggregate.py** — 번다운/프로젝트별/세션별 집계. 월 경계는 **KST** 기준(ts는 UTC라 변환).
  Claude는 월간, Codex는 **주간 누적(carryover)** 번다운(아래 게시). 예산 도입일(`budget_start`)로
  기간 시작을 clamp. `_compute_burndown`은 기간 `[start,end)`를 받는 순수 함수, `codex_burndown`은 별도.
- **pricing.py + config/pricing.json** — 모델명 매칭으로 토큰→USD. `pricing_overrides`로 사용자 단가 override.
- **web/app.py** — FastAPI 라우트(얇게: 라우팅 + 입력검증만). 데이터 조립은 **web/views.py**.
- **launcher.py** — exe 진입점. ingest 1회 → 빈 포트 탐색 → uvicorn(127.0.0.1) → pywebview 창(없으면 브라우저 fallback).
- **paths.py** — 경로 중앙 해석. 데이터 위치가 실행 형태로 갈린다(아래 게시).

## 핵심 게시(gotchas)

- **exe는 반드시 `.venv`로 빌드.** 시스템 Python으로 빌드하면 pywebview가 번들에서 빠져
  네이티브 창 대신 브라우저로 fallback한다. (PyInstaller는 런타임 의존성이 아니라 CI는 별도 설치.)
- **프라이버시 경계 — 발췌선을 지킬 것.** 파서는 토큰 usage 메타를 추출하고, Codex는
  세션 식별용으로 **첫 사용자 프롬프트만 120자 발췌**해 `sessions.summary`에 저장한다.
  그 외 content/프롬프트/대화 본문 전체는 DB에 적재하지 않는다(raw JSONL 원문은 archive.py가
  약 30일 임시 보존 — 아래 "raw 로그는 약 30일 후 휘발" 게시 참고).
- **데이터 위치가 실행 형태로 갈림**(`paths.data_dir()`): 소스 실행 → **repo 루트**(`data/`, `config/`),
  exe → `~/.tokenomy/`. `TOKENOMY_DATA`로 전체 override.
- **증분 파싱은 byte-offset 기반**(`scan_offsets`). 파일은 append되므로 mtime이 아닌 offset으로
  신규 라인만 읽는다. 단, ai-title은 세션 종료 시 갱신되어 `parse_titles`가 매번 전체 스캔한다.
- **raw 로그는 약 30일 후 휘발.** archive.py가 원문을 `data/archive/`에 보존하고,
  세션 요약(aiTitle)은 휘발 전 `sessions.summary`에 영구 캐시한다.
- **dedup은 ccusage와 동형.** `(provider, message_id, request_id)` 키 — 리트라이는 별개 과금으로 보존,
  비sidechain(부모)이 sidechain replay를 이긴다.
- **예산 주기는 provider별로 다르다.** Claude=월간 한도(월말까지). Codex=**주간 누적**: 주간 한도
  (월÷4)를 월요일마다 충전, 미사용분은 월 내 이월·월 바뀌면 소멸. `codex_burndown`의 분모=W×N
  (N=경과 주차), 분자=월 누적 지출, 이번 주 가용=차액. `budget_start`(설정, 조직 공통 단일 날짜)는
  도입 첫 달의 기간 시작을 그 날짜로 clamp — **미설정 시 달력 월 1일(완전 하위호환)**.
- **웹은 `127.0.0.1`만 바인딩** — 네트워크 노출 금지. 쿼리 파라미터는 화이트리스트 fallback(`provider`/`sort`/`period` 등).
  내역·모델별은 주/월 토글 + 사용자 지정 날짜 구간(`start`/`end`) 조회(`views._resolve_range`).
- **CSS는 Tailwind(standalone CLI)로 빌드.** `static/src/input.css`(토큰+`@layer components`) → `static/app.css`(커밋). 런타임/exe는 무빌드 유지. htmx는 `static/vendor/`에 vendored(오프라인). Alpine은 실수요 시 추가(현재 미사용).

## 환경변수

- `TOKENOMY_DATA` — 데이터 디렉토리 전체 override.
- `TOKENOMY_CONFIG` — config 파일 경로 override(테스트 격리용).
- `TOKENOMY_SKIP_UPDATE_CHECK` — 설정 시 업데이트 네트워크 조회 끔(테스트/CI/오프라인).

## 릴리스

- `tokenomy/__init__.py`의 `__version__`과 git 태그(`v<버전>`)가 **일치해야** CI 통과(release.yml에서 검증).
- 태그 push(`v*`) → GitHub Actions(windows-latest)가 exe 빌드 → smoke test → Releases 업로드.

## 코드 스타일

- docstring·주석은 한국어. 모든 모듈 상단에 `from __future__ import annotations`.
- 계층 분리 유지: 라우트(app.py, 얇게) ↔ 화면 조립(views.py) ↔ 집계(aggregate.py) ↔ 적재(db.py).
- stdlib 우선(sqlite3/json/pathlib/datetime). 런타임 의존성은 requirements.txt에 최소로.
