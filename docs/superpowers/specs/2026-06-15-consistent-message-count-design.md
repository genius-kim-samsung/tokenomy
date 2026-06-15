# 일관된 메시지(사용자 턴) 수 — 설계

- 날짜: 2026-06-15
- 상태: 설계 승인됨(구현 대기)
- 관련: `parser.py`, `codex_parser.py`, `db.py`, `aggregate.py`, 내역 화면

## 문제

내역(세션 목록·세션 상세)의 "메시지" 수가 **Codex 세션은 항상 1**로 표시된다.
Claude는 정상적으로 여러 건으로 나온다.

원인은 두 파서의 적재 단위가 다른 데 있다.

- **Claude**(`parser.py`): `usage` 블록이 있는 라인 = 모델 응답 1건마다 `messages` 행을 적재.
  집계는 `COUNT(*)`/`+= 1`로 행 수를 센다 → 모델 응답 수가 그대로 표시.
- **Codex**(`codex_parser.py` `parse_rollout`): rollout 로그가 **세션 누적 토큰**
  (`total_token_usage`)을 매 턴 다시 찍는 형식이라, 파서가 마지막 누적값 1건만 취해
  **세션 전체를 1개 `UsageRecord`로 압축**(`message_id = session_id`). 따라서 행이 항상
  1개 → 메시지 수가 늘 1.

토큰/비용 합계는 정확하지만 "메시지" 수만 provider별로 의미가 갈린다.

## 목표

지원하는 **모든 AI가 동일한 의미의 메시지 수**를 갖게 한다.

확정된 정의: **메시지 1건 = 사람이 입력한 프롬프트(사용자 턴) 1개.**
툴 결과·메타·서브에이전트(sidechain)·명령 stdout·환경 컨텍스트는 제외한다.

비목표: 토큰/비용 집계 방식 변경, Codex 누적 로그를 턴별로 분해하는 것.

## 카운트 규칙

### Claude (`parser.py` 신규 카운트 함수, `parse_titles` 풀스캔 경로에서 호출)

한 라인이 아래를 **모두** 만족하면 사용자 턴 +1.

- `type == "user"` 또는 `message.role == "user"`
- `isSidechain`이 아님 (서브에이전트 프롬프트 제외 — 사람 턴만 집계)
- `isMeta`가 아님 (`<local-command-caveat>` 등 시스템 주입 제외)
- 실제 사람 입력:
  - `content`가 **문자열**이면 `<command-name>` / `<command-message>` /
    `<local-command-stdout>` 래퍼로 시작하지 않음
  - `content`가 **리스트**면 `text` 또는 `image` 블록을 포함 (블록이 전부
    `tool_result`인 리스트는 제외)

표본 검증(tokenomy 프로젝트 69개 세션):
`str/plain`(208) + `list:text`/`list:image,text`(비메타)만 잡히고,
`tool_result`(2070)·메타(70)·`<command-*>`(51)·`<local-command-stdout>`(32)은 빠진다.

### Codex (`codex_parser.py` `parse_rollout`)

`type == "event_msg"`이고 `payload.type == "user_message"`인 이벤트 수.
단 `message`가 `<environment_context`로 시작하면 제외(Claude의 메타 제외와 대칭).

표본 검증: 실제 rollout 세션에서 `user_message` 2건(환경 컨텍스트 0) → 2.

## 저장 & 데이터 흐름

### 스키마

`sessions` 테이블에 `user_turns INTEGER` 컬럼 추가.
`db.py`의 `_MIGRATE_COLS["sessions"]`에 `"user_turns": "INTEGER"` 등록
(기존 DB는 `_migrate`의 ALTER로 보강).

### Codex 적재 경로

- `parse_rollout`이 사용자 턴 수를 세어 `UsageRecord.user_turns`(신규 필드, 기본 `None`)로 전달.
- `ingest_records`의 sessions upsert에
  `user_turns = COALESCE(excluded.user_turns, sessions.user_turns)` 추가
  (`summary`와 동일한 "새 값 우선, 없으면 보존" 패턴).
- Codex는 세션당 1레코드 + 매 ingest 전체 재파싱 → 값이 매번 갱신된다.

### Claude 적재 경로

- per-message `UsageRecord`는 세션 총량을 모르므로 `user_turns = None`(보존됨).
- 실제 카운트는 `ingest_titles` 풀스캔 경로를 확장해 기록:
  `parse_titles`가 이미 매 ingest마다 전 파일을 풀스캔(제목이 세션 종료 시 갱신되므로)
  → 같은 스캔에서 사용자 턴도 세어, `summary`와 함께
  `UPDATE sessions SET summary=?, user_turns=? WHERE session_id=?` 로 반영.
- byte-offset 증분 로직을 건드리지 않아도 되고, **재ingest만으로 기존 세션 자동 백필**된다.

> 구현 메모: `parse_titles`는 현재 `b'"ai-title"'` 바이트 프리필터로 대부분 라인을
> 건너뛴다. 사용자 턴 카운트를 위해 `user` 라인도 봐야 하므로,
> `b'"type":"user"'` / `b'"role":"user"'` 프리필터를 추가한 뒤 해당 라인만 `json.loads`한다
> (전 라인 파싱 회피). 반환 타입을 `{sid: (title|None, turns)}` 형태로 확장하거나
> 별도 함수로 분리한다 — plan 단계에서 결정.

### 백필

별도 마이그레이션 스크립트 불필요.
Codex는 전체 재파싱+REPLACE, Claude는 제목 풀스캔으로 **재ingest 시 자동 채워진다.**

## 집계 (`aggregate.py`) — `COUNT(*)` → `user_turns`

- 세션 목록/날짜트리 빌더: `a["msgs"] += 1` 제거. `meta` 딕트에 `user_turns`를 추가해
  `a["msgs"] = user_turns[sid]`로 설정. 폴더/일자 롤업은 세션 합으로 자연히 따라온다.
- `session_detail`: `COUNT(*) msgs` → `sessions.user_turns` 조회.
  세션 존재 가드(`if not totals["msgs"]`)는 토큰 행 존재(비용/행 수)로 대체해,
  `user_turns`가 0/NULL인 레거시 세션도 노출 유지.
- 그 외 `msgs` 소비처는 plan 단계에서 전수 확인(예: 리포트·프로젝트 집계).

## 엣지 케이스 & 결정

- **NULL user_turns**(아직 카운트 안 된 레거시): 표시 시 `0` 또는 `—`로 fallback,
  토큰 행이 있으면 세션 자체는 노출 유지.
- **날짜 범위에 일부만 걸친 세션**: `user_turns`는 세션 전체 속성이라 범위 부분겹침이어도
  전체 턴수로 표시한다. 세션은 보통 하루 안에 끝나 영향이 작아 허용한다.
- **서브에이전트/슬래시 명령 제외**: 사람이 직접 친 본 프롬프트만 센다.
  `/exit`·`/clear`처럼 모델 호출이 없는 명령, 서브에이전트로 보내는 sidechain 프롬프트는 제외.

## 테스트

1. **Claude 카운트 단위테스트** — 고정 JSONL 픽스처(툴결과/메타/`<command-*>`/sidechain/
   이미지+텍스트 혼합) → 기대 N.
2. **Codex 카운트 단위테스트** — `user_message`·`environment_context` 혼합 픽스처 → 기대 N.
3. **통합** — Claude·Codex 세션을 ingest한 뒤 내역(세션 목록·상세)의 메시지 수가 기대값.
4. **백필** — `user_turns` 컬럼 없이 적재된 DB → 재ingest → 값이 채워짐.

## 영향 범위

- 변경 파일: `parser.py`, `codex_parser.py`, `db.py`, `aggregate.py`, 테스트.
- 토큰/비용/번다운/캐시 효율 경로: **무영향**(별도 컬럼, 집계 분리).
- 프라이버시 경계: 카운트는 정수만 저장, 본문 미저장 — 경계 유지.
