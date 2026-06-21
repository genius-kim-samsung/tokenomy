# Tokenomy

AI 코딩 토큰 지출용 로컬 "가계부". Claude Code / Codex CLI의 **로컬** 세션 로그(JSONL)를
파싱해 공식 사용량 기반 한도·잔여·예측·프로젝트/세션별 비용·캐시 효율을 보여준다.
단발(single-shot) 데스크톱 앱(exe) 또는 소스로 실행. 전 과정 로컬,
**DB에는 토큰 메타 + 세션 식별용 첫 프롬프트 발췌만 적재**한다(대화 본문 전체는 미저장 — raw JSONL 원문은 archive.py가 약 30일 임시 보존).

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

- **parser.py / codex_parser.py** — 각 도구 로그를 공통 `UsageRecord`로 정규화. 새 도구 추가는
  여기에 모듈 하나 더(README의 "Adding a parser" 참고).
- **official_fetch.py** — 공식 사용량 라이브 취득(유일한 아웃바운드). **tracked_providers**(=활성 AI; 앱 전반 표시 게이트, ADR 0005) 목록에 포함된 provider만 호출한다. 각 CLI의 로컬 OAuth 토큰을
  **읽기 전용**으로 사용해 공식 API를 단발 GET(≤3s/provider, 백오프 없음). 엔드포인트: Claude `https://api.anthropic.com/api/oauth/usage`, Codex `https://chatgpt.com/backend-api/wham/usage`. default-on + throttle(`min_interval_minutes`="자동 갱신 간격", 기본 10). 401→auth_error, 그 외 실패→http_error,
  **마지막 스냅샷·last_success_at 보존**. PII(토큰/account_id) 미저장 — 헤더에 쓰고 버린다.
  `refresh_tracked(config, providers=None, manual=False)`가 tracked(또는 지정 providers) 전체를 1회 갱신(예외 삼킴)한다.
  트리거(ADR 0003): **수동** 갱신 버튼 `POST /official/refresh`(manual=True, throttle bypass) + **자동** 폴링 `GET /official/section`(manual=False, throttle 적용 — 대시보드 `hx-trigger="load, every Nm"`, load가 起動 갱신 겸용). `cmd_ingest`(수집)는 갱신을 트리거하지 않는다. 표준 라이브러리만.
- **db.py** — SQLite 적재. `messages`(메시지별 토큰/비용, `dedup_key` UNIQUE로 중복 제거),
  `sessions`(메타 + 요약 + 턴수), `session_day_turns`(세션×날짜 턴 수), `scan_offsets`(증분 스캔용 byte-offset),
  `official_buckets`(공식 사용량 멀티버킷 USD 스냅샷), `official_fetch_state`(자동 취득 상태). 스키마 변경은 `_MIGRATE_COLS`에 ALTER 추가.
- **aggregate.py** — 공식 사용량 기반 예측(`official_view`+`lens`) · 프로젝트별/세션별 집계 · 사용량 전용 폴백. 월 경계는 **KST** 기준(ts는 UTC라 변환). 공식 데이터가 없으면 로컬 JSONL 기반 사용량 전용 view로 자동 폴백. 집계 함수는 `provider:str|None`(None=DB전체) 외에 키워드 `providers:list[str]|None`(=**활성 AI** 집합)을 받는다 — `provider=None and providers!=None`이면 `WHERE provider IN (...)`로 합산, 빈 집합 `[]`은 `WHERE 0`(빈 결과). 뷰 경계가 활성 집합을 주입하므로 화면의 "전체"는 **DB 전체가 아니라 활성 AI 합산**이다(ADR 0005).
- **pricing.py + config/pricing.json** — 모델명 매칭으로 토큰→USD. `pricing_overrides`로 사용자 단가 override.
  `cost_usd`는 (토큰×단가)의 **캐시값** — 단가(pricing.json/overrides)가 바뀌면 `ingest`가 단가
  핑거프린트로 감지해 기존 행을 **자동 재계산**(`db.maybe_reprice`). 1h 캐시 분리는 `cache_creation_1h`
  컬럼에 저장해 재계산도 정확. 증분 적재 + dedup 가드는 옛 행을 다시 안 건드리므로 이 경로가 필수.
- **web/app.py** — FastAPI 라우트(얇게: 라우팅 + 입력검증만). 데이터 조립은 **web/views.py**.
- **launcher.py** — exe 진입점. ingest 1회 → 빈 포트 탐색 → uvicorn(127.0.0.1) → pywebview 창(없으면 브라우저 fallback).
- **paths.py** — 경로 중앙 해석. 데이터 위치가 실행 형태로 갈린다(아래 게시).
- **config.py** — 설정 모델(`config/tokenomy.config.json` 로더). `load_config`/`save_config` ·
  `tracked_providers`(=**활성 AI**; 미설정/None이면 크레덴셜 존재로 시드, 명시적 `[]`는 빈 집합 영속 — 재시드 안 함) · `credit_to_usd`(기본 0.04) ·
  `official_fetch_settings`(min_interval_minutes) · `pricing_overrides`. **config 키를 찾으면 여기다**
  (`TOKENOMY_CONFIG`로 경로 override). (구 `budget.py`에서 리네임 — 예산 로직은 제거됨.)
- **freshness.py** — 수집 신선도. 마지막 ingest 경과 + 디스크상 최고령 raw 파일 나이(vs 30일 cleanup) →
  ≥25일이면 `warn`. 트리거가 다 실패해도 데이터 유실 위험을 사람에게 노출.
- **update.py** — 인앱 업데이트 확인(GitHub Releases 최신 태그 vs `__version__`, 1일 1회).
  실패/오프라인은 조용히 skip(`TOKENOMY_SKIP_UPDATE_CHECK`로 끔). stdlib(urllib)만.

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
- **리셋 주기는 공식 API `resets_at` 기준.** Claude=월간 리셋(공식 API가 `resets_at` 타임스탬프 제공). Codex=주간 리셋(매주 월요일, 공식 API가 남은 크레딧·리셋 시각 제공). 한도·리셋 정보는 모두 공식 취득 스냅샷에서 읽는다 — 수동 예산 입력이나 `budget_start` clamp는 더 이상 없다.
- **웹은 `127.0.0.1`만 바인딩** — 네트워크 노출 금지. 쿼리 파라미터는 화이트리스트 fallback(`provider`/`sort`/`period` 등).
  내역·차원별은 주/월 토글 + 사용자 지정 날짜 구간(`start`/`end`) 조회(`views._resolve_range`).
- **CSS는 Tailwind(standalone CLI)로 빌드.** `static/src/input.css`(토큰+`@layer components`) → `static/app.css`(커밋). 런타임/exe는 무빌드 유지. htmx는 `static/vendor/`에 vendored(오프라인). Alpine은 실수요 시 추가(현재 미사용).
- **공식 사용량은 멀티버킷(USD 통일) — Claude 버킷/Codex 월간+주간(월÷4는 주간 한도 *추정치* 도출 방식 — 공식 리셋 주기와 다름; 실제 리셋은 공식 API `resets_at`; 로컬 첫-사용 앵커는 `codex_weekly_window`의 주간 used 추정 기준).** `credit_to_usd`(config, 기본 0.04)로 크레딧 환산, 토큰 cost 경로와 분리. 구 단일값 `official_usage` 테이블은 `_migrate`가 DROP(로컬 단일 사용자라 이관 없음).
- **공식 사용량 취득(=갱신)은 default-on(tracked providers만)·비차단.** `tracked_providers` 목록에 있는 provider만 공식 API를 호출하고, 첫 실행 시 크레덴셜 파일 존재로 시드한다. 갱신은 **수집(`cmd_ingest`)과 분리** — 수집은 로컬 JSONL 재스캔만, 갱신은 웹 라우트(수동 버튼/자동 폴링)가 담당한다(ADR 0003). 起動 갱신은 대시보드 로드 시 `hx-trigger="load"`가 겸한다(launcher는 수집만 동기 실행).
  타임아웃 ≤3s, **백오프 없음**(단발 시도, 실패 즉시 포기). `min_interval_minutes`="자동 갱신 간격"(기본 10) — 자동 폴링 주기이자 자동 호출 최소 간격. **수동 갱신은 이 간격을 무시(throttle bypass)**한다. 엔드포인트 quota는 CLI와 공유 — 충돌 못 막음. `TOKENOMY_SKIP_OFFICIAL_FETCH`로 전체 강제 차단 가능.
- **토큰은 읽기 전용, refresh 금지.** Claude `~/.claude/.credentials.json`, Codex `~/.codex/auth.json`을 읽기만.
  Codex 401(토큰 만료)은 마지막 값 유지 + "Codex CLI 1회 실행" 안내(직접 refresh 안 함).

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
