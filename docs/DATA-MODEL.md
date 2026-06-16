# Tokenomy 데이터 모델 & 파서 추가 가이드

- 최종 갱신: 2026-06-16 (v0.1.7 기준)
- 범위: DB 스키마 · 정규화 모델(`UsageRecord`) · 단가 매칭 · 새 도구 파서 추가법
- 관련: [ARCHITECTURE.md](ARCHITECTURE.md)

## 1. 정규화 모델 — `UsageRecord`

모든 도구의 로그는 공통 `UsageRecord`(dataclass, `parser.py:20`)로 정규화된 뒤 적재된다.
파서가 채워야 할 필드:

| 필드 | 의미 | 비고 |
|---|---|---|
| `provider` | `"claude"` \| `"codex"` | `PROVIDERS` 튜플과 일치 |
| `session_id` | 세션 식별자 | 없으면 파일 stem 폴백 |
| `cwd` | 작업 디렉토리(프로젝트 귀속) | DB `messages.project`로 저장 |
| `ts` | ISO8601 타임스탬프(UTC) | 집계 시 KST 변환 |
| `model` | 모델 id 문자열 | 단가 `contains` 매칭 키 |
| `input_tokens` / `output_tokens` | fresh 입력 / 출력 토큰 | |
| `cache_creation` / `cache_read` | 캐시 쓰기(생성) / 읽기 토큰 | |
| `cache_creation_1h` | 그중 1시간 캐시 분량 | 단가 = input×2 |
| `web_search` / `web_fetch` | server_tool_use 횟수 | 효율 신호 |
| `message_id` / `request_id` | dedup 키 구성요소 | |
| `is_sidechain` | sidechain(서브에이전트) 여부 | dedup 우선순위 |
| `attribution_skill` / `git_branch` | 귀속 메타(스킬·브랜치) | |

`total_tokens`는 `input+output+cache_creation+cache_read` 합산 property.

## 2. DB 스키마 (SQLite)

정의: `db.py:23` (`SCHEMA`). 대화 원문은 어느 테이블에도 저장하지 않는다.

### `messages` — 메시지별 토큰/비용 (핵심 사실 테이블)
- `dedup_key TEXT UNIQUE` — 중복 제거 키(아래 §3).
- `provider, session_id, project, ts, model`
- `input_tokens, output_tokens, cache_creation, cache_read, web_search, web_fetch`
- `cost_usd REAL, priced INTEGER` — 산정 비용 + 단가 식별 여부(미식별=0 → "단가 미식별 N건" 경고).
- `request_id, is_sidechain, attribution_skill, git_branch`
- 인덱스: `(provider, ts)`, `(session_id)`.

### `sessions` — 세션 메타 + 라벨/요약
- `session_id PRIMARY KEY, project, provider, first_ts, last_ts`
- `label` — 수동 귀속 라벨(업무 귀속용, 사용자 입력).
- `summary` — Claude Code aiTitle 캐시. **raw 30일 휘발 후에도 영구 보존**(`db.py:225`).
- `user_turns` — 세션 내 사용자 턴 수(메시지 수 표시용 — 응답 라인이 아닌 사용자 턴 기준).

### `session_day_turns` — 세션×날짜 턴 수
- `(session_id, day) PRIMARY KEY, turns` — 멀티데이 세션을 날짜별로 정확히 카운트(`session_id` 한 줄이 여러 날에 걸칠 때 날짜별 분해). 내역 트리의 날짜별 턴 수 표시·캐시 재구축 신호(`by_day_session`)의 근거.

### `scan_offsets` / `archive_offsets` — 증분 추적
- `path PRIMARY KEY, offset INTEGER` — 파일별 마지막 파싱(또는 아카이브) byte 위치.
  append-only 로그에서 신규 바이트만 읽기 위함.

### `meta` — 키/값 상태
- `last_ingest_ts`(신선도), `last_update_check`(업데이트 1일 1회 캐시).

### `users` — 현재 미사용
- 멀티유저 확장용 발판. 현재 쓰지 않는다(PRD Non-goal: 1머신 1사용자).

### 마이그레이션
`CREATE TABLE IF NOT EXISTS`는 기존 테이블에 신규 컬럼을 더하지 않는다. 그래서 `connect()`가
`_MIGRATE_COLS`(`db.py:79`)를 돌며 빠진 컬럼을 `ALTER TABLE ADD COLUMN`으로 보강한 뒤
스키마/인덱스를 적용한다. **새 컬럼 추가 시 `SCHEMA`와 `_MIGRATE_COLS` 양쪽에 넣어야** 기존 DB도 갱신된다.

## 3. dedup 규칙 (`db.py:133`)

```
message_id 있으면:  "{provider}:{message_id}:{request_id}"
없으면(폴백):       "{provider}:{session_id}:{ts}:{model}:{total_tokens}"
```

- 같은 메시지 리트라이(다른 `request_id`)는 **별개 과금** → 별개 행 보존.
- 충돌(ON CONFLICT) 시 교체 우선순위(`_REPLACE_WHEN`, ccusage `should_replace_deduped_entry`):
  1. 비sidechain(부모)이 sidechain replay를 이긴다.
  2. 같은 sidechain이면 토큰 총합이 큰 쪽(부분 기록 < 완전 기록).

## 4. 단가 매칭 (`pricing.py` + `config/pricing.json`)

- 단위: **USD per 1,000,000 tokens**.
- `match[]`를 위에서부터 순회, `contains`가 모델 id의 부분문자열인 **첫 항목** 사용(`pricing.py:32`).
- 미일치 → `priced=False`, 비용 0, 경고로 노출(조용한 누락 금지).
- 비용 = `(input·input + output·output + cache_5m·cache_write + cache_1h·input·2 + cache_read·cache_read) / 1e6`.
- `pricing_overrides`(config)로 항목별 `input/output/cache_write/cache_read`를 코드 변경 없이 덮어쓴다(`pricing.py:75`).
- **단가 항목 순서 주의**: 더 구체적인 `contains`를 위에 둔다(부분일치 첫 매칭이라 순서가 곧 우선순위).

## 5. 새 도구 파서 추가하는 법

목표: 어떤 CLI든 로그를 `UsageRecord`로 바꿔 `ingest_records`에 넘기면 기존 db/집계/대시보드를 그대로 재사용.

1. **모듈 생성** `tokenomy/<tool>_parser.py`. `codex_parser.py`를 레퍼런스로.
2. **탐색 함수** — 그 도구의 로그 파일을 찾는다. 예: `discover_rollouts(root) -> list[Path]`.
3. **파싱 함수** — 파일/라인 → `UsageRecord`(들). 토큰 usage 메타만 추출, **원문 content는 절대 읽지 않는다**.
   - 누적값 로그(Codex처럼)면 세션당 1레코드로 정규화, `message_id=session_id`로 두면 dedup이 REPLACE 처리.
   - append-only 라인 로그(Claude처럼)면 byte-offset 증분(`scan_offsets`)을 쓴다.
4. **ingest 진입점** — 시그니처 관례 `ingest_<tool>(conn, root, pricing) -> int`(적재 세션/레코드 수).
   내부에서 `db.ingest_records(conn, records, pricing)` 호출.
5. **배선**:
   - `aggregate.PROVIDERS`에 새 키 추가(`aggregate.py:16`).
   - `budget.Budget`에 예산 필드 추가 + `limit_for` 분기(`budget.py:17`).
   - `config/pricing.json`에 해당 provider 모델 단가 항목 추가.
   - `cli.cmd_ingest`(`cli.py:24`)와 필요 시 웹 표시에 새 ingest 호출 추가.
6. **테스트** — `tests/test_codex_parser.py` 패턴: 샘플 라인 → 기대 `UsageRecord` 필드 검증.

### Codex 파서 매핑 (레퍼런스)
- 위치: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, 세션당 1파일.
- 라인: `{timestamp, type, payload}`. `session_meta`(id/cwd/ts), `turn_context`(model),
  `token_count`(payload.info.`total_token_usage` = **누적**).
- 마지막 `token_count`가 세션 총량. 매핑: `fresh = input - cached`, `cache_read = cached`,
  `output = output_tokens`, `cache_creation = 0`(Codex는 캐시 쓰기 구분 없음). (`codex_parser.py:62`)
