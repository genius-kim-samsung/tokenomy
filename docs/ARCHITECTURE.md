# Tokenomy 아키텍처

- 최종 갱신: 2026-06-17 (v0.1.8 — 단가 커버리지 진단 + 단가 변경 시 비용 자동 재계산)
- 범위: 시스템 구조·데이터 흐름·핵심 설계 결정. 로컬 전용 설계 메모(커밋 제외).
- 관련: [PRD.md](PRD.md) · [ROADMAP.md](ROADMAP.md) · [DATA-MODEL.md](DATA-MODEL.md)

## 한 줄 요약

Claude Code / Codex CLI의 **로컬 세션 로그(JSONL)** 를 증분 파싱해 토큰 usage **메타만**
추출 → SQLite에 적재 → 예산 대비 번다운(Claude 월간·Codex 주간 누적)·프로젝트/세션별 비용·캐시 효율로 집계 →
CLI 리포트 또는 로컬 웹 대시보드로 노출한다. 전 과정 로컬, 1머신 1사용자.

## 데이터 파이프라인 (단방향)

```
 입력(읽기 전용, 홈)              정규화            적재             집계            표현
┌────────────────────────┐
│ ~/.claude/projects/     │── parser.py ─────┐
│   **/*.jsonl            │                  │
│ (메시지별 usage)         │                  ▼
├────────────────────────┤            UsageRecord ──► db.py ──► messages  ──► aggregate.py ──┬─► cli.py (터미널 리포트)
│ ~/.codex/sessions/      │── codex_parser.py┘   (dataclass)  (SQLite)   sessions             │
│   **/rollout-*.jsonl    │                                   scan_offsets  pricing.py          └─► web/ (FastAPI+Jinja2)
│ (세션 누적)              │                                                budget.py                 ├ overview (/)
└────────────────────────┘                                                freshness.py              ├ 내역(/history) / 차원별(/analysis)
                                                                                                    ├ session 상세
        └────────────── archive.py (raw 원문 byte 증분 복사 → data/archive/) ──────────────┘        └ settings
```

- **입력은 절대 쓰지 않는다.** `~/.claude`, `~/.codex`는 각 파서가 홈에서 직접 읽기만 한다.
- **출력 데이터**(DB·archive·config)는 `paths.data_dir()` 아래로만 쓴다.

## 계층 / 모듈 책임

| 계층 | 모듈 | 책임 |
|---|---|---|
| 입력 정규화 | `parser.py` | Claude Code transcript → `UsageRecord`. usage 블록 유무로 "과금 라인" 판정. byte-offset 증분. |
| | `codex_parser.py` | Codex rollout(세션 누적) → 세션당 1 `UsageRecord`. 마지막 `token_count`가 세션 총량. |
| 적재 | `db.py` | SQLite. `messages`(메시지별, `dedup_key` UNIQUE) / `sessions`(메타+요약+턴수) / `session_day_turns`(세션×날짜 턴 수) / `scan_offsets`(증분) / `meta`. 스키마 ALTER 마이그레이션. |
| 비용 | `pricing.py` + `config/pricing.json` | 모델명 `contains` 매칭 → 토큰×단가. 5m/1h 캐시 분리 과금. `pricing_overrides` 적용(없는 모델은 새 항목 prepend로 자가 추가). `cost_usd`는 캐시값이라 단가 변경 시 핑거프린트로 자동 재계산(`maybe_reprice`). |
| 예산 | `budget.py` | provider별 월 예산 + 도입일(`budget_start`) 로드. Codex 주간 한도(월÷4) 헬퍼. config 없으면 0(추적 전용). |
| 집계 | `aggregate.py` | 번다운(Claude 월간·Codex 주간 누적)·기간 경계(KST)·프로젝트/세션/차원(모델·스킬·브랜치)별 집계·토큰 구성비(`token_composition`)·서브에이전트 분리(`sidechain_split`)·효율 코치(insights)·단가 커버리지 진단(`pricing_coverage`)·일별 누적. `budget_start` clamp. |
| 보존 | `archive.py` | raw JSONL 원문을 `data/archive/`로 byte 증분 복사(30일 휘발 대비). |
| 신선도 | `freshness.py` | 마지막 ingest 경과 + 가장 오래된 raw 나이(vs 30일 cleanup) → 유실 위험 경고. |
| 업데이트 | `update.py` | GitHub Releases 최신 태그 vs `__version__`. 1일 1회, 실패는 조용히 무시. |
| 경로 | `paths.py` | 데이터(쓰기) vs 리소스(읽기) 경로 중앙 해석. 실행 형태별 분기. |
| 표현(CLI) | `cli.py` | `ingest` / `report` / `all`. 빠른 검증·복기용. |
| 표현(웹) | `web/app.py` | FastAPI 라우트 — **얇게**(라우팅 + 입력 화이트리스트만; `provider`/`sort`/`period`/`dim`). 레거시 `/projects`·`/sessions`·`/models`는 301 리다이렉트. |
| | `web/views.py` | DB → 화면용 dict 조립(라우트와 집계 분리). |
| | `web/templates/`, `static/` | 서버 렌더 Jinja2 + htmx 부분 갱신. vendored(htmx·Chart.js) + `tree.js`(내역 트리 토글). CSS는 Tailwind 빌드 산출 `app.css` 커밋(런타임 무빌드). |
| 셸 | `launcher.py` | exe 진입점. ingest 1회 → 포트 탐색 → uvicorn → pywebview 창(없으면 브라우저). |

## 핵심 설계 결정 (왜 이렇게)

1. **프라이버시 경계 — 메타만 저장.** 파서는 `usage` 블록(토큰 수·모델·시각·프로젝트)만
   추출하고 `content`(프롬프트/응답 원문)는 읽지 않는다. DB·archive 어디에도 대화 원문이
   들어가지 않는다. 로컬 도구지만 이 경계를 코드 수준에서 강제한다. (`parser.py:1`)

2. **byte-offset 증분 파싱.** 세션 파일은 append-only로 자란다. mtime만으로는 "어디가
   늘었는지" 알 수 없어 파일별 마지막 파싱 위치를 `scan_offsets`에 저장하고 다음 ingest 때
   신규 바이트만 읽는다. 단 **ai-title**(세션 제목)은 종료 시 갱신될 수 있어 매번 전체 스캔한다.
   (`parser.py:121`, `parser.py:145`)

3. **dedup은 ccusage와 동형.** `dedup_key = (provider, message_id, request_id)`. 같은 메시지의
   리트라이(다른 request_id)는 별개 과금이라 별개 행으로 보존한다. 충돌 시 ① 비sidechain(부모)이
   sidechain replay를 이기고 ② 같은 sidechain이면 토큰 총합이 큰 쪽(완전 기록)을 남긴다.
   스트리밍 중복·부분 기록을 안전하게 흡수한다. (`db.py:133`, `db.py:143`)

4. **월 경계는 KST.** transcript ts는 UTC(ISO8601)인데 예산은 한국 시간 월 단위로 관리한다.
   `parse_ts`가 UTC→KST 변환 후 `[start, nxt)` 반-개구간으로 버킷팅한다. 일/주/월 기간은
   `period_bounds`로 일반화(주는 월요일 시작). (`aggregate.py:23`, `aggregate.py:44`)

5. **raw 휘발 대비 이중 보존.** Claude Code raw 로그는 약 30일 후 정리된다. ① `archive.py`가
   원문을 `data/archive/`에 byte 단위로 보존하고 ② 세션 요약(aiTitle)은 휘발 전 `sessions.summary`에
   영구 캐시해 raw가 사라져도 리포트에 제목이 남는다. (`archive.py:1`, `db.py:225`)

6. **provider 플러그인 구조.** 새 도구 지원 = `discover → parse → UsageRecord yield → ingest_records`
   모듈 하나 추가. `codex_parser.py`가 레퍼런스 구현. provider 추가 시 `PROVIDERS` 튜플 +
   `Budget` 필드 + `pricing.json` 항목만 보강. (`aggregate.py:16`, [DATA-MODEL.md](DATA-MODEL.md))

7. **단가 = contains 매칭 + override + 자동 재계산.** `pricing.json`의 `match[]`를 위에서부터
   순회하며 모델 문자열에 부분일치하는 첫 단가를 쓴다. 미일치는 비용 미산정(`priced=False`)으로
   두고 "단가 미식별 N건" 경고로 노출(조용한 누락 금지). 사용자 청구가 다르면 `pricing_overrides`로
   코드 변경 없이 덮어쓰고, 없는 모델은 새 항목으로 prepend해 자가 추가한다. 1시간 캐시 생성은
   input 단가×2로 과금. `cost_usd`는 (토큰×단가)의 **캐시값**이라 단가 입력이 바뀌면 `maybe_reprice`가
   단가 핑거프린트 변화를 감지해 raw 재적재 없이 전체 행을 자동 재계산한다(증분 적재·dedup 가드가
   옛 행을 다시 안 건드리므로 이 경로가 필수). 매칭 신뢰도(미식별·버전 경계 의심·거친 매칭)는 **단가
   커버리지 진단**으로 settings 카드·overview 경고·CLI에 노출한다. (`pricing.py`, `db.maybe_reprice`)

8. **1머신 1사용자 + 127.0.0.1 전용.** 팀 집계는 비목표 — 각자 자기 머신에서 돌린다(프라이버시상
   자연스러움). 웹은 `127.0.0.1`만 바인딩하고 인증이 없다. 쿼리 파라미터는 화이트리스트
   폴백으로 잘못된 입력에도 크래시하지 않는다. (`web/app.py:35`, `launcher.py:83`)

9. **데이터 위치가 실행 형태로 분기.** `paths.data_dir()`: 소스 실행 → repo 루트(`data/`,
   `config/` — 개발 호환), exe(frozen) → `~/.tokenomy/`. `TOKENOMY_DATA`로 전체 override.
   읽기 전용 리소스(`pricing.json`·템플릿)는 `resource_path()` — PyInstaller onefile이면
   `_MEIPASS`, 소스면 repo 루트. (`paths.py:20`, `paths.py:44`)

10. **단발(single-shot) 데스크톱 셸.** exe는 ingest 1회 → 빈 포트 탐색(8765~) → uvicorn 데몬
    스레드 → pywebview 창이 메인 스레드 점유. 창을 닫으면 프로세스 종료. pywebview 미가용
    환경은 기본 브라우저로 폴백. (`launcher.py:103`)

11. **예산 주기는 provider별로 다르다(주기 모델 일반화).** Claude는 월간 한도(월말까지). Codex는
    예산 정책상 **주간 누적**: 주간 한도(월÷4)를 월요일마다 새로 충전하고 미사용분은 월 내 이월,
    월이 바뀌면 소멸한다. `_compute_burndown`을 기간 `[start, end)`를 받는 순수 함수로 일반화해
    Claude(월간, 도입일 clamp)와 `codex_burndown`(분모=W×N, 분자=월 누적 지출, 이번 주 가용=차액)이
    공유한다. 도입일 `budget_start`(설정, 조직 공통 단일 날짜)는 도입 첫 달의 기간 시작을 그 날짜로
    clamp — **미설정 시 달력 월 1일(완전 하위호환)**. 내역·모델별은 주/월 토글 + 사용자 지정 날짜
    구간 조회. (`aggregate.py:_compute_burndown`/`codex_burndown`, `web/views.py:_resolve_range`)

## 실행 모드

| 모드 | 진입점 | 데이터 위치 | 용도 |
|---|---|---|---|
| CLI | `python -m tokenomy.cli {ingest\|report\|all}` | repo 루트 | 빠른 검증·복기, 자동화 |
| 웹(개발) | `uvicorn tokenomy.web.app:app` (127.0.0.1:8765) | repo 루트 | 대시보드 개발 |
| exe | `Tokenomy.exe` (`launcher.main`) | `~/.tokenomy/` | 배포(비개발자 더블클릭) |

ingest는 멱등(증분·dedup)이라 어느 모드로 몇 번 돌려도 안전하다. 웹의 `POST /ingest`,
exe 기동 시 `_safe_ingest()`, `start_tokenomy.bat`이 각각 ingest를 트리거한다.

## 테스트 전략

- `pytest` — 모듈별 단위 + 웹은 FastAPI `TestClient`. DB는 `connect(":memory:")` 또는 tmp 파일.
- **시간/환경 주입**: `now_kst`를 인자로 받아(`aggregate`/`freshness`) 현재 시각 비의존 테스트.
  웹 테스트는 `TOKENOMY_CONFIG`(config 격리)·`TOKENOMY_SKIP_UPDATE_CHECK`(네트워크 차단) 환경변수.
- `test_launcher`의 포트 테스트 2건은 앱이 8765 점유 중이면 실패 — 환경 의존, 회귀 아님.
