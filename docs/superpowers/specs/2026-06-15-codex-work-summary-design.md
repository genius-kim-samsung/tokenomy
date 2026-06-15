# Codex 작업요약 (첫 프롬프트 발췌) 설계

- 날짜: 2026-06-15
- 상태: 설계 승인 대기 → 구현
- 관련: 내역 화면(`web/templates/_history_rows.html`), `codex_parser.py`, `db.py`

## 1. 배경 및 문제

내역 화면의 "작업요약" 열은 `sessions.summary`를 그대로 표시한다. 이 값은
**Claude Code가 세션마다 생성하는 한 줄 제목(`aiTitle`)** 을 캐시한 것이다
(`parser.py:parse_titles` → `db.py:ingest_titles`). Codex 세션은 이 값이 항상
비어 있어 작업요약 열에 `—`만 나온다.

근본 원인은 파서 누락이 아니라 **소스 부재**다. Codex rollout 파일에는
`aiTitle` 같은 LLM 생성 제목 메타가 기록되지 않는다. `session_meta` 키는
`base_instructions, cli_version, cwd, git, id, model_provider, originator,
source, thread_source, timestamp` 뿐이다.

따라서 Codex에서 세션 식별 정보를 만들려면 **사용자 첫 프롬프트를 발췌**하는
길밖에 없다.

## 2. 목표 / 비목표

### 목표
- Codex 세션의 내역 "작업요약" 열에 **첫 사용자 프롬프트 발췌(약 120자)** 를 표시.
- 기존 `sessions.summary` 컬럼·내역 템플릿을 그대로 재사용(스키마 마이그레이션 없음).
- Claude의 `aiTitle` 적재 경로(`ingest_titles`)와 충돌하지 않음.

### 비목표 (이번 작업 범위 밖)
- **전체 대화 원문 저장.** 토큰 낭비 분석 고도화에 필요한 것은 자연어 원문이
  아니라 메시지/턴별 메타이며, Codex의 1순위 병목은 "세션당 1행" 구조의 턴 분해다
  (§7). 복기용 원문은 이미 디스크(`~/.codex/sessions`)와 `archive.py`(30일)에
  존재하므로 DB 복제는 중복이다. 원문 영구 저장은 archive 정책 변경을 동반하는
  별도 기능으로, 그때 독립 설계한다.
- Codex 턴별 토큰 분해(현재 누적값 → 세션당 1 `UsageRecord`).
- Claude 세션의 aiTitle 부재 시 첫 프롬프트 fallback(향후 일관성 차원에서 검토 가능).

## 3. 설계

### 3.1 추출 — `codex_parser.py`

`parse_rollout`이 rollout을 스캔하며 **첫 사용자 프롬프트**를 캡처한다.

- **1순위:** `payload.type == "user_message"` 의 첫 레코드 → `payload["message"]`.
  이 필드는 `<environment_context>` 주입이 빠진 순수 사용자 입력이다(검증됨).
- **2순위(fallback):** `user_message`가 하나도 없으면, `response_item`의
  `message`(role=user) 중 텍스트가 `<environment_context`로 **시작하지 않는**
  첫 `content[].text`.
- 캡처한 텍스트는 개행→공백으로 정규화하고 연속 공백을 접은 뒤, **120자**로
  truncate(초과 시 `…` 부가는 표시 단에서 CSS가 처리하므로 저장은 단순 슬라이스).
- 결과를 `UsageRecord.summary`에 싣는다. 추출 실패 시 `None`.

추출은 별도 헬퍼 함수(`_extract_first_prompt(...) -> str | None`)로
분리해 단위 테스트가 입력 라인만으로 검증 가능하게 한다.

### 3.2 데이터 모델 — `parser.py`

`UsageRecord`에 필드 1개 추가:

```python
summary: str | None = None  # 세션 식별용 첫 프롬프트 발췌(Codex). Claude는 None(aiTitle 별도 경로).
```

Claude 파서는 이 필드를 채우지 않는다(기존 `parse_titles`/`ingest_titles` 유지).

### 3.3 적재 — `db.py`

`ingest_records`의 `sessions` upsert에 `summary`를 포함하되, **None이 기존 값을
덮지 않도록** COALESCE 처리한다.

```sql
INSERT INTO sessions (session_id, project, provider, first_ts, last_ts, summary)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(session_id) DO UPDATE SET
  last_ts = MAX(sessions.last_ts, excluded.last_ts),
  first_ts = MIN(sessions.first_ts, excluded.first_ts),
  project = COALESCE(sessions.project, excluded.project),
  summary = COALESCE(excluded.summary, sessions.summary)
```

- Claude 레코드(`summary=None`)는 기존 동작 그대로 → `ingest_titles`가 채움.
- Codex는 누적값이라 재파싱·REPLACE되며 매번 같은 발췌로 덮어도 무방.
- `sessions.summary` 컬럼은 **이미 존재**(`_MIGRATE_COLS`) → 스키마 변경 없음.

순서 주의: Codex는 `ingest_codex`가 record에 summary를 실어 한 번에 반영하므로,
Claude처럼 "행 생성 후 별도 UPDATE"하는 2단계가 필요 없다.

### 3.4 표시 — 템플릿

**코드 변경 없음.** `_history_rows.html`이 이미 `s.summary`를 작업요약 열에
표시하고(`title`에 전문), `[codex]` provider 뱃지도 출력한다. Codex가 칸을
채우면 자동으로 나타난다.

표시 성격 차이(문서화만): Claude `aiTitle`은 LLM이 정제한 제목, Codex는 날것의
첫 프롬프트. 같은 칸에 들어가지만 톤이 다르며 식별 목적은 동일하게 달성된다.

### 3.5 프라이버시 문구 재정의

현재 경계 문구("대화 원문은 저장하지 않는다(토큰 메타만)")는 발췌 저장과
어긋나므로 **재정의**한다(완전 삭제 아님 — 전체 대화 비저장은 여전히 사실).

> 토큰 메타 + **세션 식별용 첫 프롬프트 발췌(약 120자)** 만 저장하며,
> **전체 대화 기록은 저장하지 않는다.** 향후 분석 고도화 시 정책을 재검토한다.

대상: `CLAUDE.md`, `README.md`, `README.ko.md`, `tokenomy/db.py`·
`tokenomy/archive.py`·`tokenomy/parser.py` 주석, `web/templates/settings.html`,
`web/templates/base.html`, `DESIGN.md`(존재 시), 관련 테스트의 문구.

## 4. 데이터 흐름

```
~/.codex/sessions/**/rollout-*.jsonl
  └ parse_rollout
      ├ token_count(누적) ─→ 토큰/비용 (기존)
      └ _extract_first_prompt ─→ summary(≤120자)
                                     │
        UsageRecord(provider=codex, …, summary) ─→ ingest_records
                                                      └ sessions.summary (COALESCE)
                                                           │
                                              내역 화면 "작업요약" 열 (기존 템플릿)
```

## 5. 엣지 케이스 & 에러 처리

- **user_message 없음**(예: 비대화 세션, 첫 입력 전 종료): fallback도 실패 시
  `summary=None` → 화면 `—`. 회귀 아님(현 동작과 동일).
- **environment_context만 있는 세션**: fallback이 `<environment_context` 접두를
  걸러 `None` 반환.
- **매우 짧은 입력**("안녕"): 그대로 표시. 식별 목적상 정상.
- **멀티바이트/이모지**: 120자 슬라이스는 파이썬 문자 단위라 안전(바이트 절단 없음).
- **이미지/첨부만 있는 입력**(`images`/`local_images` 존재, `message` 공백):
  `message`가 비면 `None` 취급.
- **재인제스트 누적 갱신**: Codex REPLACE 시 summary도 함께 갱신되나 값이 안정적이라 무해.

## 6. 테스트

- `tests/test_codex_parser.py`(신규 또는 기존 확장):
  - `user_message` 첫 레코드에서 발췌 추출.
  - `user_message` 부재 시 fallback이 environment_context를 건너뜀.
  - 120자 truncate, 개행/연속공백 정규화.
  - 발췌 소스 전무 시 `None`.
- `tests/` 적재 레벨: Codex record의 summary가 `sessions.summary`에 반영되고,
  뒤이은 Claude 적재(`summary=None`)가 그 값을 덮지 않음(COALESCE 검증).
- 프라이버시 단언이 있는 기존 테스트(`tests/test_parser.py` 등)의 문구가 새 경계와
  일치하도록 갱신. **원문 전체 비저장**을 단언하는 테스트는 유지(발췌≠전체).

## 7. 향후 작업 (범위 밖, 참고)

- **Codex 턴별 분해**: rollout의 중간 `token_count` 다건으로 턴 델타를 산출하면
  "어느 턴이 토큰을 태웠나" 분석이 가능해진다. 토큰 낭비 분석 고도화의 1순위.
- **복기용 원문 접근**: DB 복제 대신 `archive.py` 보존 영구화 + 세션→원본파일
  포인터(경로+offset)로 설계.

## 8. 영향 받는 파일

| 파일 | 변경 |
|---|---|
| `tokenomy/parser.py` | `UsageRecord.summary` 필드 추가 |
| `tokenomy/codex_parser.py` | `_extract_first_prompt` + `parse_rollout`에서 summary 채움 |
| `tokenomy/db.py` | `sessions` upsert에 summary(COALESCE) 반영 + 주석 문구 |
| `tokenomy/archive.py` | 프라이버시 주석 문구 |
| `web/templates/_history_rows.html` | 변경 없음(확인용) |
| `web/templates/settings.html`, `base.html` | 프라이버시 안내 문구 |
| `CLAUDE.md`, `README.md`, `README.ko.md`, `DESIGN.md` | 프라이버시 경계 문구 재정의 |
| `tests/test_codex_parser.py`, `tests/test_parser.py` | 발췌 추출·적재·문구 테스트 |
