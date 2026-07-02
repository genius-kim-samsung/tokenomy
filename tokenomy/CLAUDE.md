# tokenomy/ — 코어 패키지

로그 파싱 → SQLite 적재 → 집계 → 화면(웹/CLI)의 단방향 파이프라인.
전체 지도·핵심 게시의 정본은 [루트 CLAUDE.md](../CLAUDE.md) — 여기는 "어느 파일을 열지"만 안내한다.

## 소유 범위 (owns)

- 로그 파싱 — `parser.py`(Claude) · `codex_parser.py`(Codex) → 공통 `UsageRecord`
- 공식 사용량 — `official_fetch.py`(유일한 아웃바운드·토큰 능동 갱신) · `official_parser.py`
- 적재 — `db.py`(SQLite·dedup·마이그레이션) · `archive.py`(raw 30일 보존)
- 집계 — `aggregate.py`(official_view·프로젝트/세션별·KST 경계)
- 단가 — `pricing.py` + `config/pricing.json`
- 화면 — `web/app.py`(라우트, 얇게) · `web/views.py`(조립) · `web/templates/` · `web/static/`
- 진입점 — `cli.py`(단발) · `launcher.py`(exe/트레이 상주·단일 인스턴스)
- 인프라 — `paths.py`(경로·플랫폼 게이트 단일 진실원) · `config.py`(설정) · `freshness.py` · `update.py`

## 수정 패턴 (common patterns)

- 새 AI 도구 파서 추가 → 파서 모듈 신설 + `UsageRecord` 정규화(README "Adding a parser").
- 스키마 컬럼 추가 → `db.py`의 `_MIGRATE_COLS`에 ALTER 추가(신규 테이블은 `_migrate`).
- config 키 추가 → `config.py`(모델+로더). 기존 키를 찾을 때도 여기부터.
- 화면 데이터 변경 → `web/views.py`에 조립 로직, `web/app.py`는 라우팅+입력검증만.
- 집계 함수 추가 → `provider:str|None` + `providers:list[str]|None`(활성 AI 집합) 시그니처 관례를 따른다(ADR 0005).

## 비자명 게시 (gotchas)

- 주의: `cost_usd`는 캐시값 — 단가 변경은 ingest의 reprice 경로(`db.maybe_reprice`)가 처리한다. 직접 UPDATE 금지.
- 주의: 증분 파싱은 `scan_offsets`의 byte-offset 기반 — mtime 비교 로직을 넣지 말 것.
- 주의: 월/일 경계는 KST 기준(저장 ts는 UTC) — 집계에서 변환한다.
- 나머지(웹뷰 상주·플랫폼 분기·프라이버시 발췌선·토큰 갱신 등)는 [루트 CLAUDE.md](../CLAUDE.md)의 "핵심 게시(gotchas)"가 정본.

## 모듈 간 의존성 (cross-module dependencies)

- `web/views.py` → `aggregate.py` → `db.py` 단방향. 역방향 import 금지.
- `launcher.py` ↔ 라우트의 순환 import는 `web/control.py` 콜백 레지스트리로 끊는다(ADR 0006).
- `paths.py`가 데이터/설정 경로와 `mini_view_available()`의 단일 진실원 — launcher와 웹 사이드바가 공유.
- 테스트는 [tests/](../tests/CLAUDE.md)에 모듈별 1:1 대응(`tests/test_db.py` ↔ `db.py` 식).
