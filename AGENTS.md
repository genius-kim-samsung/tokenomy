# Tokenomy Agent Guide

## 프로젝트 개요

Tokenomy는 AI 코딩 토큰 지출용 로컬 가계부다. Claude Code / Codex CLI의 로컬 세션 로그(JSONL)를 파싱하고 공식 사용량 API를 자동 취득해 공식 한도·잔여·예측, 프로젝트/세션별 비용, 캐시 효율을 보여준다.

프라이버시 경계가 핵심이다. DB에는 토큰 메타데이터와 세션 식별용 첫 프롬프트 발췌만 저장한다. 전체 대화 본문은 저장하지 않는다.

## 기본 명령

```powershell
# 개발 실행
.venv\Scripts\python -m tokenomy.cli ingest
.venv\Scripts\python -m tokenomy.cli report
.venv\Scripts\python -m tokenomy.cli all
.venv\Scripts\python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765

# 테스트
.venv\Scripts\python -m pytest

# CSS 빌드: tokenomy/web/static/src/input.css 또는 템플릿 클래스 변경 시 실행하고 app.css 커밋
.\build_css.ps1

# exe 빌드: 반드시 .venv Python 사용
.venv\Scripts\python -m PyInstaller tokenomy.spec
```

Windows 배치 파일:

- `start_tokenomy_dev.bat`: 개발용 실행
- `start_tokenomy.bat`: ingest 후 대시보드 실행 및 브라우저 열기

## 아키텍처

단방향 데이터 파이프라인을 유지한다.

```text
~/.claude/projects/**/*.jsonl      -> parser.py       -> UsageRecord
~/.codex/sessions/**/rollout-*     -> codex_parser.py -> UsageRecord
UsageRecord -> db.py(SQLite) -> aggregate.py -> cli.py / web/
공식 사용량 API -> official_fetch.py -> official_parser.py -> db.py(official_buckets) -> official_aggregate.py -> cli.py / web/
```

주요 모듈:

- `tokenomy/parser.py`, `tokenomy/codex_parser.py`: 도구별 로그를 공통 `UsageRecord`로 정규화한다.
- `tokenomy/db.py`: SQLite 적재, 중복 제거, 스키마 마이그레이션을 담당한다. 스키마 변경은 `_MIGRATE_COLS`에 추가한다.
- `tokenomy/aggregate.py`: 로컬 롤업(messages/sessions) — 프로젝트/세션/차원(모델·스킬·브랜치)별 집계, 토큰 구성비를 담당한다. 월 경계는 KST 기준이다.
- `tokenomy/official_aggregate.py`: 공식 사용량 집계 — 공식 스냅샷(official_buckets) 기반 예측(`official_view`+`lens`), 풀 이력, 통합 전망(`combined_forecast`). aggregate.py와 상호 호출 없음.
- `tokenomy/forecast.py`: 전망 조립층 — `outlook(conn, config, now)`이 활성 AI 공식 뷰 팬아웃+통합 전망을 `Outlook(params·views·combined)` 하나로 돌려준다. 조립부(대시보드·섹션·미니)가 중간 산물(views)을 공유해 렌더당 팬아웃 1회를 지킨다.
- `tokenomy/clock.py`: 시간·달력 어휘 leaf(의존성 0) — `KST`·`parse_ts`·월/기간 경계·영업일 산술. 두 집계 모듈이 모두 아래로 import한다.
- `tokenomy/pricing.py`, `config/pricing.json`: 모델명 매칭으로 토큰을 USD로 환산한다. `cost_usd`는 캐시값이라 단가가 바뀌면 `ingest`가 핑거프린트로 감지해 기존 행을 자동 재계산한다(`db.maybe_reprice`).
- `tokenomy/web/app.py`: FastAPI 라우트. 얇게 유지하고 입력 검증과 라우팅만 둔다.
- `tokenomy/web/views.py`: 화면 데이터 조립 로직을 둔다.
- `tokenomy/launcher.py`: exe 진입점. ingest 1회 후 로컬 서버와 pywebview/브라우저를 띄운다.
- `tokenomy/paths.py`: 실행 형태별 데이터/config 경로를 중앙에서 해석한다.

## 데이터와 경로

- 소스 실행 시 데이터는 repo 루트의 `data/`, `config/` 아래에 쌓인다.
- exe 실행 시 데이터는 `~/.tokenomy/` 아래에 쌓인다.
- `TOKENOMY_DATA`로 데이터 디렉터리 전체를 override할 수 있다.
- `TOKENOMY_CONFIG`로 config 파일 경로를 override할 수 있다.
- `TOKENOMY_SKIP_UPDATE_CHECK`가 설정되면 업데이트 네트워크 조회를 건너뛴다.
- `TOKENOMY_SKIP_OFFICIAL_FETCH`가 설정되면 공식 사용량 라이브 취득을 항상 건너뛴다(오프라인/CI/테스트).

## 핵심 규칙

- 전체 프롬프트나 대화 본문을 DB에 저장하지 않는다.
- Codex 세션 식별용 발췌는 첫 사용자 프롬프트 기준 120자 선을 유지한다.
- raw JSONL 원문은 `archive.py`가 약 30일 임시 보존하는 흐름을 유지한다.
- 웹 대시보드는 `127.0.0.1`에만 바인딩한다. 외부 네트워크 노출을 만들지 않는다.
- 쿼리 파라미터는 화이트리스트 fallback 패턴을 유지한다.
- 증분 파싱은 `scan_offsets`의 byte-offset 기반이다. append 로그 가정을 깨지 않는다.
- dedup 키는 `(provider, message_id, request_id)` 의미를 유지한다. 리트라이는 별개 과금으로 보존한다.

## 공식 사용량·한도 로직

- 한도와 리셋 주기의 정본은 공식 API 응답(`resets_at`, 버킷 잔여)이다. 수동 예산 입력·`budget_start` clamp는 없다.
- Claude는 월간 리셋, Codex(엔터프라이즈)도 월간 리셋이다(옛 "월÷4 주간 한도" 조직 정책은 폐지 — Claude와 동일한 월 예산). 개인 구독제 Codex는 별도 공식 rate-window(7일 등)를 가진다. 리셋 시각은 공식 API가 제공한다.
- `tracked_providers` 목록에 있는 provider만 공식 API 호출 대상이 된다. 첫 실행 시 크레덴셜 파일 존재로 자동 시드.
- 공식 데이터가 없으면(취득 skip·미성공·한도 미제공 계정) **사용량 전용 view**로 폴백한다.
- 날짜/월 경계 계산은 KST 기준으로 맞춘다. 저장 timestamp가 UTC라면 집계에서 변환한다.

## 프론트엔드

- 템플릿은 Jinja2, 부분 갱신은 htmx를 사용한다.
- `static/vendor/`의 vendored asset을 우선 사용한다. 오프라인 실행성을 유지한다.
- CSS는 Tailwind standalone 빌드 결과인 `tokenomy/web/static/app.css`를 커밋한다.
- CSS 원본은 `tokenomy/web/static/src/input.css`다.
- 템플릿 클래스나 CSS 원본 변경 후에는 `.\build_css.ps1`를 실행한다.

## 테스트 지침

- 기본 검증은 `.venv\Scripts\python -m pytest`다.
- 웹 테스트는 `TOKENOMY_CONFIG`, `TOKENOMY_SKIP_UPDATE_CHECK`를 사용해 로컬 상태와 네트워크 조회를 격리한다.
- 집계, 파싱, DB, 경로 해석, 웹 라우트 변경은 해당 단위 테스트를 추가하거나 갱신한다.
- 릴리스 관련 변경은 `tokenomy/__init__.py`의 `__version__`과 git 태그 `v<버전>` 일치 규칙을 고려한다.

## 코드 스타일

- 기존 계층 분리를 유지한다: route(`app.py`) -> view assembly(`views.py`) -> aggregate(`aggregate.py`·`official_aggregate.py`) -> persistence(`db.py`).
- 모든 모듈 상단의 `from __future__ import annotations` 관례를 유지한다.
- 주석과 docstring은 한국어 톤을 따른다.
- 표준 라이브러리를 우선 사용하고 런타임 의존성은 `requirements.txt`에 최소로 추가한다.
- 변경 범위는 요청과 직접 관련된 파일로 제한한다. 무관한 리팩터링은 피한다.
