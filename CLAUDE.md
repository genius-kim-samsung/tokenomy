# Tokenomy

AI 코딩 토큰 지출용 로컬 "가계부". Claude Code / Codex CLI의 **로컬** 세션 로그(JSONL)를 파싱해 공식 사용량 기반 한도·잔여·예측·프로젝트/세션별 비용·캐시 효율을 보여준다.
단발(single-shot) 데스크톱 앱(exe) 또는 소스로 실행. 전 과정 로컬, **DB에는 토큰 메타 + 세션 식별용 첫 프롬프트 발췌만 적재**한다(대화 본문 전체는 미저장 — raw JSONL 원문은 archive.py가 약 30일 임시 보존).

## 명령어

```powershell
# 개발 실행 (소스) — 데이터는 repo 루트의 data\, config\ 아래에 쌓인다
.venv\Scripts\python -m tokenomy.cli ingest    # 세션 로그 → SQLite (증분)
.venv\Scripts\python -m tokenomy.cli report    # 터미널 요약(공식 사용량 + Top 프로젝트)
.venv\Scripts\python -m tokenomy.cli all       # ingest 후 report
.venv\Scripts\python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765

# 배치 (Windows 더블클릭)
start_tokenomy_dev.bat     # 개발용
start_tokenomy.bat         # ingest → 대시보드 → 브라우저 자동 열기

# 테스트
.venv\Scripts\python -m pytest

# enterprise 공식 사용량 view 미리보기 — 개인 계정 PC에서 격리 DB로 시드 후 웹 확인(개인 DB 미오염)
.venv\Scripts\python scripts\seed_official_enterprise.py   # ~/.tokenomy-ent-preview 시드 + 실행 안내 출력

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
공식 사용량 API ── official_fetch.py(유일한 아웃바운드, ≤3s, 백오프 없음, tracked providers만) ─ raw JSON ─ official_parser.py ─→ db.py(official_buckets)
```

모듈별 소유("어느 파일이 뭘 하나")와 상세 게시의 정본은 [tokenomy/CLAUDE.md](tokenomy/CLAUDE.md). 테스트 관행은 [tests/CLAUDE.md](tests/CLAUDE.md), dev 스크립트는 [scripts/CLAUDE.md](scripts/CLAUDE.md).

## 핵심 게시(gotchas)

전 모듈 공통 불변식만 여기 둔다 — 모듈 한정 게시(플랫폼 분기·토큰 능동 갱신·증분 파싱·dedup·reprice·멀티버킷 상세)는 [tokenomy/CLAUDE.md](tokenomy/CLAUDE.md)가 정본.

- **exe는 반드시 `.venv`로 빌드.** 시스템 Python으로 빌드하면 pywebview가 번들에서 빠져 네이티브 창 대신 브라우저로 fallback한다. (PyInstaller는 런타임 의존성이 아니라 CI는 별도 설치.)
- **webview 경로는 상주 모드(ADR 0006).** exe에서 창 X = 트레이로 숨김(종료 아님), 트레이 우클릭 "종료"로만 완전 종료. 단일 인스턴스(`data/runtime.json` — 런타임 생성) — 재실행 시 기존 창 복원(`/app/ping` 정체 확인 → `/app/show`). 창 복원 시 ingest 1회 + 조건부 리로드. 트레이는 pystray+Pillow(번들). 브라우저 fallback·개발 모드는 단발 유지.
- **프라이버시 경계 — 발췌선을 지킬 것.** 파서는 토큰 usage 메타만 추출하고, Codex는 세션 식별용으로 **첫 사용자 프롬프트만 120자 발췌**해 `sessions.summary`에 저장한다. 그 외 content/프롬프트/대화 본문 전체는 DB에 적재하지 않는다.
- **데이터 위치가 실행 형태로 갈림**(`paths.data_dir()`): 소스 실행 → **repo 루트**(`data/`, `config/`), exe → `~/.tokenomy/`. `TOKENOMY_DATA`로 전체 override.
- **raw 로그는 약 30일 후 휘발.** archive.py가 원문을 `data/archive/`에 보존하고, 세션 요약(aiTitle)은 휘발 전 `sessions.summary`에 영구 캐시한다.
- **한도·리셋의 정본은 공식 API.** 공식 사용량은 멀티버킷(USD 통일) — 실제 리셋=`resets_at`, 크레딧은 `credit_to_usd`(config, 기본 0.04)로 환산. 수동 예산 입력·`budget_start` clamp는 없다. 버킷 구성·게이지 라벨 규약은 tokenomy/CLAUDE.md 참고.
- **공식 사용량 취득(=갱신)은 default-on(tracked providers만)·비차단.** 갱신은 **수집(`cmd_ingest`)과 분리** — 수집은 로컬 JSONL 재스캔만, 갱신은 웹 라우트(수동 버튼=throttle bypass, 자동 폴링=`min_interval_minutes` 기본 10 — 대시보드 load가 起動 갱신 겸용)가 담당한다(ADR 0003). `TOKENOMY_SKIP_OFFICIAL_FETCH`로 전체 강제 차단.
- **웹은 `127.0.0.1`만 바인딩** — 네트워크 노출 금지. 쿼리 파라미터는 화이트리스트 fallback(`provider`/`sort`/`period` 등).
- **CSS는 Tailwind(standalone CLI)로 빌드.** `tokenomy/web/static/src/input.css`(토큰+`@layer components`) → `tokenomy/web/static/app.css`(커밋). 런타임/exe는 무빌드 유지.
- **테스트는 실제 시계·고정 포트를 쓰지 않는다.** 시드·골든 fixture는 전부 2026-06 기준 — 시계 고정·동적 포트 관행 상세는 [tests/CLAUDE.md](tests/CLAUDE.md).

## 환경변수

- `TOKENOMY_DATA` — 데이터 디렉토리 전체 override.
- `TOKENOMY_CONFIG` — config 파일 경로 override(테스트 격리용).
- `TOKENOMY_SKIP_UPDATE_CHECK` — 설정 시 업데이트 네트워크 조회 끔(테스트/CI/오프라인).
- `TOKENOMY_SKIP_OFFICIAL_FETCH` — 설정 시 공식 사용량 라이브 취득을 항상 skip(오프라인/CI/테스트).

## 릴리스

- `tokenomy/__init__.py`의 `__version__`과 git 태그(`v<버전>`)가 **일치해야** CI 통과(release.yml에서 검증).
- 태그 push(`v*`) → GitHub Actions(windows-latest)가 exe 빌드 → smoke test → Releases 업로드.

## 코드 스타일

- docstring·주석은 한국어. 모든 모듈 상단에 `from __future__ import annotations`.
- 계층 분리 유지: 라우트(app.py, 얇게) ↔ 화면 조립(views.py) ↔ 집계(aggregate.py) ↔ 적재(db.py).
- stdlib 우선(sqlite3/json/pathlib/datetime). 런타임 의존성은 requirements.txt에 최소로.

## Agent skills

### Issue tracker

이슈는 GitHub Issues(`genius-kim-samsung/tokenomy`)에 산다 — `gh` CLI 사용. 외부 PR은 triage 표면 아님. See `docs/agents/issue-tracker.md`.

### Triage labels

canonical 5역할을 기본 문자열 그대로 사용(`needs-triage`/`needs-info`/`ready-for-agent`/`ready-for-human`/`wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

single-context — root에 `CONTEXT.md` + `docs/adr/`. See `docs/agents/domain.md`.
