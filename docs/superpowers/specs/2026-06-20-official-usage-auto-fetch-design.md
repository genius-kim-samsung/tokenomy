# 공식 사용량 자동 취득 (멀티버킷 + 예측 렌즈) — 설계

- 작성일: 2026-06-20
- 상태: **codex 회귀 리뷰 2차 반영(2026-06-20) · 재확인 대기**
- 마일스톤: v0.2.0 채택 — "공식 사용량 수동 입력"(commit d29cdc9)을 **자동 취득 + 멀티버킷 정확화**로 대체
- 관련: [TODOS.md](../../../TODOS.md) ① · `tokenomy/db.py` · `tokenomy/aggregate.py` · `tokenomy/budget.py` ·
  `tokenomy/pricing.py` · `config/pricing.json` · `tokenomy/web/` · `tokenomy/cli.py` · `tokenomy/launcher.py`
- **수치 주의**: 본 문서의 한도 금액·크레딧 수치는 전부 **예시(가짜)** 다. 실제 한도/사용량은 런타임에 공식 API에서
  읽는다. 변환/주기 기본값(`credit_to_usd`, 주간=월÷4, 리셋 시각 등)은 **config 기본값**으로 두어 환경별 조정 가능.
  실측 응답 원문은 로컬 전용(미커밋), 커밋 정본은 sanitize fixture(§10).

## 0. 주요 개정 요약 (2026-06-20)

독립 리뷰(codex) + 단가/리셋 정책 입수로 핵심 전제가 바뀌어 대폭 개정.

**(가장 큰 변화) USD 통일.** 크레딧↔토큰↔USD 변환이 전부 정의됐다: Claude 토큰→USD(`pricing.json`),
Codex 토큰→USD(`pricing.json`에 이미 존재) **및** 크레딧→USD(`credit_to_usd` 기본 **0.04**). 토크노미는 **가계부**이므로
**USD를 1차 기준 단위**로 삼는다.
- **합산 표면 복원**: Claude+Codex 결합 월총액·추세를 USD로(범주 오류 해소). 이전 개정의 "합산 제거"는 철회.
- **Codex 패널 = USD 1차 + 크레딧/버킷 보조**(공식 앱이 크레딧 표기라 미러로 크레딧도 병기).
- **틀린 전제 삭제**: "pricing.json에 Codex 단가 없음"·"Codex USD 환산 불가"·"공식이 Codex 유일 신뢰 신호"는
  사실이 아님(§1). `pricing.json`은 이미 USD로 정합(Codex 포함).

**리셋 주기 provider별 상이(신규 반영).** Claude=**월별**, Codex=**주별(월 한도÷4, 첫 사용 앵커)**. §8에 정식 모델.

**codex 리뷰 반영(유지):** 리셋 인스턴트 그룹핑, 스냅샷 트랜잭션·UNIQUE, urllib 타임아웃·백오프 제거·비차단 ingest,
throttle 효력 격하, 크레덴셜 만료/드리프트, 활성버킷 견고화·예측렌즈 차분 위생, fallback 단위 라벨, Phase1 import-fixture.

**(2차 회귀 리뷰 반영, 2026-06-20)** — codex 재리뷰의 진짜 결함만 반영(도메인 정보 부족 오지적은 제외):
- **Codex 주간 used 소스 역전**: 주간 슬라이스는 **로컬 CLI 첫-사용 7일 윈도우 합(USD)을 1차**로, 공식 API는 월 한도·월 누적·`reset_at`의 authoritative 소스로. 공식 스냅샷 차분은 보조/교차검증(희소 스냅샷·월 경계로 단독 신뢰 불가 — codex Severity 1 해소).
- **Codex 카드 = 게이지 2개**: 월간(공식 미러, used/limit) + 주간(예측, 월÷4). 각각 렌즈.
- **결합 표면 = "이번 달 현금 지출"**: 양쪽 **월 누적** USD 합산·추세만(게이지 아님). 게이지는 provider별 자기 주기 유지. Codex는 결합에 월 누적 사용(주간 슬라이스 제외) — codex Severity 2 해소.
- **`credit_to_usd`는 `tokenomy.config.json`**(pricing.json 아님): 크레딧 단위가격(고정 청구 상수, 모델 무관). 토큰 cost 경로(`pricing_fingerprint`/`maybe_reprice`)에 비접촉.
- **起動 비차단 강화**: 取得 1차 경로=웹 새로고침. ingest/launcher 경로는 백그라운드 또는 ≤3s 타임아웃·실패 즉시 포기(`_safe_ingest` 동기 호출 블록 금지).
- **작은 수정**: `UNIQUE`에 `raw_key` 포함, `fetched_at` 의미 구분, 마이그레이션 순서(`_migrate` vs SCHEMA), 숫자별 소스 플래그(공식/추정), 활성버킷 결정성(동률/stale/다중), 테스트에 희소-스냅샷·월-경계·재앵커.

**수동 입력 완전 제거**(자동만, 데이터 없으면 CLI 추정 fallback) — 유지.

## 1. 배경 / 문제

commit d29cdc9가 "월 할당" 게이지를 위해 **공식 사용량 수동 입력**을 도입했다(`official_usage.cumulative_usd`
단일값/월, `POST /official`, `max(공식,CLI)` 병합). TODOS ①은 후속으로 공식 사용량 **자동 취득**을 예고했다.

2026-06-20 실측으로 각 CLI가 보관한 OAuth 토큰으로 **공식 사용량 API를 읽기 전용 단발 호출**하면 실데이터가
온다는 것이 확인됐다(원문은 로컬 전용 보존):

- **Claude**: `GET https://api.anthropic.com/api/oauth/usage` (Bearer + `anthropic-beta: oauth-2025-04-20`)
- **Codex**: `GET https://chatgpt.com/backend-api/wham/usage` (Bearer JWT + `ChatGPT-Account-Id`)

enterprise 티어 응답이 단일 `cumulative_usd` 모델로는 못 담는 구조를 드러냈다(공식 앱 화면과 1:1, 수치는 예시):

**Claude (공식 앱 = 막대 3개):**

| 공식 앱 표기 | API 필드(모양) | 의미 | 리셋/만료 |
|---|---|---|---|
| 사용 한도 | `extra_usage`(monthly_limit) + `spend`(used/limit amount_minor) | 월 한도 used/limit(USD) | **월별** |
| 포함된 크레딧 | 코드네임 키(`used_dollars`+`limit_dollars`+`resets_at`) | 일회성 이벤트 크레딧(USD) | 자체 **만료일** |
| 별도/프로모션 | 코드네임 키(`utilization`만, 달러 null) | 별도 프로모션 | 초기화 안 됨 |

차감 순서: **포함된 크레딧(이벤트) → 월 사용 한도 → 일반 사용량(org 레벨, 미러 범위 밖 §13).**

**Codex (공식 앱 = 막대 1개):** `spend_control.individual_limit` = **월간** 크레딧 한도(used/limit/remaining/used_percent
+ `reset_at` unix). 단위 credits. **단, 실효 제약은 주간**(월 한도÷4) — §8.

**전제 정정(과거 spec/조사 결론의 오류):**
- `pricing.json`에는 **Codex 단가가 이미 존재**하고(공개 단가), USD로 정합한다. → Codex USD 추정은 가능.
- 크레딧↔USD는 `credit_to_usd`(기본 0.04)로 정확 환산 → 공식 크레딧(예 1,000cr=$40)도 USD로 표기 가능.
- `credit_to_usd`는 **크레딧 단위가격(고정 청구 상수)** — 모델별로 달라지는 건 토큰→크레딧 변환이지 크레딧→달러가 아니다(API가 이미 크레딧을 주므로 토큰→크레딧은 불필요). 따라서 단일 상수(0.04)가 맞고, 토큰 단가 경로(`pricing.json`)와 분리해 `tokenomy.config.json`에 둔다.
- 따라서 단위는 **USD로 통일**(가계부 기준). 크레딧/버킷은 보조 표시.

**핵심 함정(유지):**
1. **버킷 다중성** — Claude 3버킷 + 차감 순서.
2. **코드네임 회전** — 이벤트/프로모션 키는 회전 코드네임. 키 이름 하드코딩 금지(모양으로 분류).
3. **모양 분기** — 개발(집/원격) 머신은 개인 구독이라 달러 버킷 대신 `five_hour`/`seven_day` **% 창** 반환.
4. **리셋 주기 상이** — Claude 월별 / Codex 주별(월÷4). §8.

## 2. 목표 / 비목표

### 목표
- 공식 사용량을 **자동 취득**해 수동 입력을 **완전 대체**한다(사람 개입 0).
- **USD를 1차 단위**로 통일(가계부). 공식 앱 버킷과 숫자 일치하는 미러 + 합산 USD 표면.
- 공식 앱에 없는 **예측 렌즈**(소비속도·소진예상·D-day)를 얹는다.
- provider별 **리셋 주기**(Claude 월 / Codex 주=월÷4)를 정확히 반영.
- 개발 머신(개인 구독)에서도 **코드 경로 전체를 라이브로 검증**(§10).

### 비목표
- **수동 입력 유지** — 안 함(`POST /official`·폼·`cumulative_usd` 제거).
- **백그라운드 폴링** — 안 함(`ingest` 1회 + 새로고침).
- **토큰 직접 refresh** — 안 함(읽기 전용, 만료 시 마지막 값 + 안내).
- **일반 사용량(org) 가시화 / 이벤트 "남기면 아까움"** — backlog(§13).
- **CLI quota 충돌 방지** — 못 함(throttle은 우리 호출 빈도만, §7).
- **`pricing.json` 모델 정합 전반(예 `gpt-5.2` 오매칭)** — 별도 "단가 커버리지" 영역. 본 spec은 `credit_to_usd`만 추가.

### 설계 원칙 적합성 (v0.2.0 4원칙)
- **자격/강도**: 공식 used=ground truth → 게이지 본체. 소진 임박만 warn.
- **raw 추출**: 사용량 수치만 저장. **PII 저장 금지**.
- **provider parity**: 양쪽 공식 실데이터 존재 + USD 통일로 동등 비교 가능.
- **cost/value**: 순수 파서 + 작은 네트워크 모듈 + 게이지. 옵트인이라 비활성 시 비용 0.

## 3. 설계 결정 요약 (2026-06-20)

| # | 결정 | 선택 |
|---|------|------|
| 1 | 기준 단위 | **USD 통일**(가계부). 크레딧/버킷은 보조. `credit_to_usd` 기본 0.04 — **`tokenomy.config.json`**(토큰 단가 경로와 분리), 크레딧 단위가격(고정 청구 상수, 모델 무관) |
| 2 | 데이터 진실원 | 공식 `used`/`limit`을 ground truth로 직접 사용 — `max(공식,CLI)` 병합 폐기 |
| 3 | 수동 입력 | **완전 제거**. 자동만. 데이터 없으면 CLI 추정 fallback(단위 라벨 명시 §9) |
| 4 | 합산 표면 | **"이번 달 현금 지출"**(USD). 양쪽 **월 누적** 합산·추세(주간 슬라이스 제외). 게이지 아님 — 숫자/추세만. provider별 게이지는 자기 주기 유지 |
| 5 | % 표기 | provider 불문 **"사용됨"** 통일 |
| 6 | Codex 주기/단위 | 카드에 **게이지 2개**: 월간(공식 ground truth, used/limit) + 주간(월÷4, 예측). 주간 used=**로컬 CLI 첫-사용 7일 윈도우**(추정), 월간=공식 직접(§8) |
| 7 | Claude 주기 | 월별 + 이벤트 버킷 자체 만료 |
| 8 | 네트워크 경계 | **옵트인**(`official_fetch.enabled` 기본 false) + provider별 토글 |
| 9 | 취득 시점 | `ingest` 1회(비차단·타임아웃) + 웹 "새로고침". 폴링 없음 |
| 10 | 주기 그룹핑 | 달력월 아님 — **`resets_at` 인스턴트**(provider별 리셋 시각 정합) |
| 11 | 코드네임 | shape 휴리스틱 분류, 라벨 서술형. raw 코드네임은 보조 식별자로 보존(§6) |
| 12 | 파서 범위 | 모양 불문 — enterprise 버킷 + 개인 구독 % 창 둘 다 |
| 13 | fixture | sanitize(모양 보존·가짜 수치) 커밋, 실측 원문 로컬 gitignore |

## 4. 아키텍처 / 데이터 흐름

```
크레덴셜(읽기전용) ─ official_fetch.py(네트워크, 옵트인, 타임아웃) ─ raw JSON ─┐
                                                                          │
raw JSON ─ official_parser.py(순수, shape 휴리스틱, USD 환산) ─ [OfficialBucket] ─ db.py(스냅샷 트랜잭션)
                                                                            │
                                            aggregate.official_view ─ web/views.py ─ overview 게이지/합산
```

신규 모듈 2개(계층 분리):
- **`tokenomy/official_fetch.py`** — 유일한 아웃바운드. 토큰 읽기·헤더·GET(타임아웃)·throttle·에러 처리.
- **`tokenomy/official_parser.py`** — 순수(`raw dict → [OfficialBucket]`). USD 환산 포함(`credit_to_usd` 주입). fixture로 완결 테스트.

제거 대상(마이그레이션 §5): `official_merged_burndown`·`max 병합`, `POST /official`·입력 폼·`_official_notes`/`_gauge`(병합 버전),
`db`의 단일값 official 함수 + 테이블 `official_usage`. (합산 표면은 **유지/복원** — 제거 안 함.)

## 5. 데이터 모델 — `db.py`

신규 버킷 단위 테이블 + 취득 상태를 additive로 추가. 구 `official_usage`는 **사용 경로 제거 후 마이그레이션에서 DROP**.

### 마이그레이션
DROP 전 `official_usage` 사용 경로를 먼저 제거(`db.SCHEMA`의 CREATE 삭제, 구 insert/latest/series,
`aggregate.official_merged_burndown`, `app.POST /official`). `db.connect`는 `_migrate()`(db.py:126)를
`executescript(SCHEMA)`(db.py:151)보다 **먼저** 실행하므로, `DROP TABLE IF EXISTS official_usage`는
`_MIGRATE_COLS`(ALTER 전용)가 아니라 `_migrate` 내 별도 단계로 두고, SCHEMA에서 CREATE를 뺀 뒤
신규 테이블 CREATE는 SCHEMA가 담당하게 순서를 맞춘다(구/신 DB 모두 부분 스키마 없이 수렴). 로컬 단일 사용자라 데이터 이관 없음.

```sql
CREATE TABLE IF NOT EXISTS official_buckets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT,          -- 'claude' | 'codex'
    fetched_at TEXT,        -- 스냅샷 as-of (로컬 fetch 완료 시각, KST ISO). 같은 값 = 한 스냅샷. (API 서버 as-of/created_at과 구분 — staleness·차분 속도 기준)
    bucket_key TEXT,        -- 안정 논리 id('event'|'monthly'|'promo'|'five_hour'|...)
    raw_key TEXT,           -- 원 API 키(코드네임). 표시 비사용 — 다중 충돌 시 series 보조 분리키
    bucket_kind TEXT,       -- 'event_credit'|'monthly_limit'|'promo'|'rate_window'|'codex_monthly'
    label TEXT,             -- 서술형 라벨(코드네임 비의존)
    native_unit TEXT,       -- 'usd' | 'credit' | 'percent' (공식 앱 표기 단위)
    used_native REAL, limit_native REAL, remaining_native REAL,  -- 네이티브 값(없으면 NULL)
    used_usd REAL, limit_usd REAL, remaining_usd REAL,           -- USD 환산(percent 창은 NULL)
    utilization REAL,       -- % (0~100)
    resets_at TEXT,         -- ISO 인스턴트(타임존 포함) 또는 NULL
    created_at TEXT,
    UNIQUE(provider, fetched_at, bucket_key, raw_key)   -- raw_key 포함: 같은 kind 이벤트 다중(코드네임만 다름)도 분리 저장
);

CREATE TABLE IF NOT EXISTS official_fetch_state (
    provider TEXT PRIMARY KEY,
    last_attempt_at TEXT, last_success_at TEXT,
    last_status TEXT,       -- 'ok'|'throttled'|'auth_error'|'http_error'|'disabled'
    last_error TEXT
);
```

- **USD 컬럼 병행 저장**: 네이티브(크레딧/USD) + USD 환산을 같이 적재 → 표시·합산 시 재계산 불필요, 환산율 변경 이력도 스냅샷에 고정.
- **스냅샷 트랜잭션**: 한 fetch의 버킷 전부를 단일 트랜잭션 insert(부분 스냅샷 방지). UNIQUE로 중복 refresh 멱등.
- **bucket_key는 안정 논리 id**(코드네임 회전 내성). 같은 kind 동시 다중이면 `raw_key`로 분리.
- DB 함수: `insert_official_snapshot`(트랜잭션), `latest_official_snapshot`, `official_bucket_series(provider, bucket_key)`(`(fetched_at, used_usd, used_native)` 오름차순), `get/upsert_fetch_state`.
- PII 컬럼 없음.

## 6. 파서 — `official_parser.py` (순수, 모양 불문, USD 환산)

```python
@dataclass
class OfficialBucket:
    bucket_key: str; raw_key: str; bucket_kind: str; label: str
    native_unit: str                      # 'usd'|'credit'|'percent'
    used_native: float | None; limit_native: float | None; remaining_native: float | None
    used_usd: float | None; limit_usd: float | None; remaining_usd: float | None
    utilization: float; resets_at: datetime | None

def parse_claude(raw: dict, *, credit_to_usd: float) -> list[OfficialBucket]: ...
def parse_codex(raw: dict, *, credit_to_usd: float) -> list[OfficialBucket]: ...
```

정확한 필드 경로·null·exponent·malformed 처리는 구현 계획에서 커밋된 sanitize fixture로 핀다운(본 spec은 분류 규칙).

### Claude 휴리스틱 (코드네임 비의존, native_unit='usd')
1. **`extra_usage`+`spend`** → `monthly_limit`. used/limit = `spend.{used,limit}.amount_minor / 10**exponent`(USD).
   `resets_at` 없음 → **다음 리셋 인스턴트 계산**(§8 Claude 월별). `bucket_key='monthly'`, `raw_key='spend'`.
2. **dict이고 `used_dollars`+`limit_dollars`+`resets_at`** → `event_credit`. `*_dollars`(USD), `resets_at` 그대로.
   `bucket_key='event'`, `raw_key`=코드네임, label="포함된 크레딧 · {만료일} 만료".
3. **dict이고 `utilization`만(달러 null)** → `promo`. native_unit='percent', USD 컬럼 NULL. utilization 0이면 생략 가능.
4. **`five_hour`/`seven_day`/`seven_day_*` dict(개인)** → `rate_window`. native_unit='percent', USD NULL, `resets_at` 그대로.
- USD 컬럼 = 네이티브 USD 그대로(Claude는 이미 USD).

### Codex 휴리스틱 (native_unit='credit' → USD 환산)
1. **`spend_control.individual_limit` dict** → `codex_monthly`. native used/limit/remaining(크레딧, 문자열→float),
   **USD = 크레딧 × credit_to_usd**, `utilization=used_percent`, `resets_at=reset_at`(unix→KST 인스턴트). `bucket_key='monthly'`.
2. **`rate_limit.primary/secondary_window` dict(개인)** → `rate_window`. native_unit='percent', USD NULL, `resets_at=reset_at`.

## 7. 취득 — `official_fetch.py` (옵트인, 비차단, 유일한 아웃바운드)

```python
def fetch_provider(provider: str, *, now_kst, config, conn) -> FetchResult: ...
```

- **옵트인**: `config.official_fetch.enabled` false면 즉시 `disabled`(네트워크 없음). provider 토글도 검사.
  `TOKENOMY_SKIP_OFFICIAL_FETCH`로 강제 skip(오프라인/CI/테스트).
- **throttle**: `last_attempt_at` + `min_interval_minutes`(기본 5) 미달이면 `throttled`(마지막 스냅샷 유지).
  **효력 한계**: 우리 호출 빈도만 제어한다. 엔드포인트 quota는 CLI와 공유라 **CLI와의 충돌은 못 막음**.
- **HTTP 타임아웃 필수**: `urllib.request` timeout(connect+read **≤ 3s/provider**). **백오프 없음**(단발 시도, 실패 즉시 포기, 마지막 스냅샷 유지).
- **起動 비차단(강화)**: 取得 **1차 경로 = 웹 새로고침**(`POST /official/refresh`). `ingest`/`launcher` 경로에서는 fetch를 **백그라운드 스레드**로 분리(서버 起動 대기 없음)하거나, 동기로 둘 경우 ≤3s 타임아웃·실패 즉시 포기로 최악 지연 상한을 건다. `_safe_ingest`(launcher.py:51)가 서버 시작 전 동기 호출되므로 fetch가 그 안에서 블록되면 안 됨. fetch 실패(타임아웃·네트워크·인증)는 예외 삼킴→state 기록→진행. 옵트인 off 기본이라 일반 startup 영향 0, on이어도 起動 경로는 새로고침과 분리.
- **기존 `check_update`와 별개**: app.py가 페이지 요청 시 `check_update(conn)`로 네트워크를 타는 것과 무관하게, 공식 fetch는 새로고침/백그라운드로 한정해 페이지 렌더를 막지 않는다.
- **토큰 소스(읽기 전용)**: Claude `~/.claude/.credentials.json`→`claudeAiOauth.accessToken`(만료/스키마 드리프트→`auth_error`+재로그인 안내, 호출 안 함). Codex `~/.codex/auth.json`→`tokens.access_token`/`account_id`(account_id 누락/스키마 변경→`auth_error`).
- **에러 분류(마지막 스냅샷 보존)**: 401→`auth_error`(Codex note "Codex CLI 1회 실행"). 429/5xx/네트워크/TLS 인터셉트→`http_error`. 파싱 실패→`http_error`.
- **성공 시**: 파서(`credit_to_usd` 주입) → `insert_official_snapshot`(트랜잭션) → `upsert_fetch_state(ok)`.
- 호출 지점: `cli.cmd_ingest`(→ launcher) + 웹 `POST /official/refresh`. 표준 라이브러리만.

## 8. 집계 + 리셋 주기 — `aggregate.py` (순수)

### 리셋 주기 모델 (provider별, config 기본값)
```
reset_cycle.claude = "monthly"   # 월별: 월말까지, 다음 달 초기화. 이벤트 버킷은 자체 resets_at 만료.
reset_cycle.codex  = "weekly"    # 주별: 주간 한도 = 월 한도 ÷ 4
```
- **Claude(월별)**: 주기 = `resets_at` 인스턴트 기준 월 경계(KST). 기존 month/period 머신 재사용.
- **Codex(2주기 동시 표시)**: 공식 API는 **월간** 한도/누적을 authoritative하게 준다. 실효 제약은 **주간**(월÷4)이라 카드에 게이지 2개를 둔다.
  - **① 월간 게이지(공식 ground truth)**: used/limit = 공식 `spend_control.individual_limit` 직접(크레딧 + USD 환산). 주기 = `reset_at` 인스턴트(월간). 결합 표면(§9)에도 이 월 누적을 쓴다.
  - **② 주간 게이지(예측 렌즈)**: 한도 = (공식 월 한도 있으면 그것, 없으면 로컬 budget) **÷ 4**.
    - **윈도우 앵커 = 로컬 CLI 첫 사용 타임스탬프 기준 7일**. 유휴 7일이면 다음 사용일에 재앵커. (공식 누적 스냅샷은 cadence가 희소해 앵커 관측 불가 — 로컬 로그의 메시지 ts가 첫 사용/유휴를 정확히 드러내므로 앵커는 **로컬 1차**.)
    - **주간 used = 로컬 CLI 메시지의 현재 7일 윈도우 합(USD)**. per-message ts라 희소 스냅샷 문제 없음. 공식 스냅샷에 윈도우 시작 베이스라인이 있으면 공식 차분으로 교차검증(없으면 로컬만).
    - **단위 주의(의도된 차이)**: 주간 used는 로컬 토큰단가 **추정** USD, 월간 게이지는 공식 크레딧환산 USD. 두 게이지는 묻는 질문이 달라(이번 주 소비 속도 vs 공식 월 소진) 베이스가 다르며, 주간엔 "추정" 라벨을 단다. **주/월 윈도우는 서로 독립**(차분하지 않음)이라 7일 윈도우가 달력 월을 넘어도 음수·누락 델타 문제 없음.
    - 기존 `codex_burndown`(월요일 cadence carryover)을 **첫-사용 7일 앵커로 교정**해 재사용(§12).

### dataclass
```python
@dataclass
class OfficialLens:                 # 예측 렌즈
    bucket_key: str
    daily_rate_usd: float | None    # USD/영업일. 유효 차분 1개 미만이면 None
    exhaust_date: date | None
    days_left_to_reset: int | None  # 현재 주기 리셋까지 영업일(Codex=주간 윈도우 만료, Claude=월말/이벤트 만료)
    dday_warning: bool

@dataclass
class OfficialView:
    provider: str; buckets: list[OfficialBucket]
    active_key: str | None; lens: OfficialLens | None  # Codex=주간 게이지 렌즈
    period_used_usd: float | None; period_limit_usd: float | None  # 1차 주기 USD (Claude 월 / Codex 월간 게이지, 공식)
    weekly_used_usd: float | None; weekly_limit_usd: float | None   # Codex 주간 게이지(로컬 추정 / 공식 월÷4). Claude=None
    weekly_estimated: bool                                          # 주간 used가 로컬 토큰단가 추정이면 True(라벨)
    fetched_at: datetime | None; stale_minutes: int | None
    status: str; note: str | None

def official_view(conn, provider, now_kst) -> OfficialView: ...
```

- **표시 순서(공식 앱 미러)**: Claude = `monthly_limit`→`event_credit`→`promo`→`rate_window`. Codex = `codex_monthly`→`rate_window`.
- **활성 버킷(결정적)**: 1차 = 최근 두 스냅샷에서 `used` 양의 차분이 있는 버킷 중 차분 큰 것(**동률이면 차감 순서 `[event, monthly]`로 tie-break**). 2차(시계열 부족·차분 0) = 차감 순서 중 remaining>0 첫 버킷. **`resets_at` 지난 stale 버킷·utilization만 있는 promo/rate_window는 활성 후보 제외**(렌즈 없음). 이벤트 다중(`raw_key` 분리)이면 remaining 큰 것 1개만 활성. API 모순 시 실측 차분 우선. Codex 활성 = **주간 게이지**(예측 대상), 월간 게이지는 미러만(월말 D-day 보조 표기 가능).
- **예측 렌즈 — 차분 위생**: series를 `fetched_at` 정렬·중복 제거. 음수 차분(리셋/만료/버킷·윈도우 전환)은 버림(직전 단조 구간만),
  너무 짧은 간격(<30분) 스킵, 활성 버킷/주간 윈도우 바뀌면 이전 series 제외. 속도는 영업일 환산(`business_days_between`). 유효 차분 없으면 `daily_rate_usd=None`.
- **데이터 없음/비활성**: `latest_official_snapshot` 비면 status(fetch_state 기반) → 상위가 CLI 추정 fallback(§9, 단위 라벨 명시).

## 9. 표시 — `web/views.py` + 템플릿 (USD 통일)

`overview` 컨텍스트:
- **합산 표면 = "이번 달 현금 지출"(USD)**: Claude 월 used + Codex **월 누적** used(공식 ground truth) 합산 + 추세. 양쪽 다 월 기준이라 합산이 의미 있음(주간 슬라이스는 안 넣음). **합산 게이지(혼합 주기)는 안 만듦** — 합산은 "현금 지출" 숫자/추세로만, 예산 게이지로 쓰지 않음. 기존 `combined_burndown`(aggregate.py:343) 재사용, Codex 분자=월 누적.
- **공식 미러 패널(provider별)**:
  - Claude: 버킷 막대(monthly_limit→event_credit→promo→rate_window) = 라벨 + **USD(1차)** + 네이티브(괄호) + `NN% 사용됨` + 리셋/만료.
  - **Codex 카드 = 게이지 2개**: ① **월간**(공식 미러) "월 한도 $XX / $YY (= 1,000 / 1,250 크레딧) · 18% 사용됨 · {reset_at} 리셋", ② **주간**(예측) "이번 주 $A / $B · MM% 사용됨 · {윈도우 만료} · 추정". promo/rate_window는 `utilization%`+리셋.
- **예측 렌즈**: 활성 버킷에 소비속도(USD/영업일)·소진예상·D-day. Codex 활성=**주간 게이지**(월간엔 월말 D-day 보조). `daily_rate_usd=None`→"추세 수집 중".
- **숫자별 소스 플래그**: 모든 표시 숫자에 출처를 단다 — **공식**(API ground truth) / **추정**(로컬 CLI 토큰단가). 공식 버킷 USD 옆에 추정 추세가 붙어도 authoritative를 구분(Codex 주간=추정, 월간=공식).
- **% 표기 "사용됨" 통일**(Codex `used_percent`).
- **데이터 없음**: 공식 미취득이면 패널이 CLI 추정으로 떨어지되 "공식 미취득 — 로컬 **추정**(USD)" 라벨 명시.
- **새로고침 버튼**: `POST /official/refresh` → throttle 가드 후 fetch → redirect. "마지막 업데이트 N분 전".

`web/app.py`: `POST /official` 제거. `POST /official/refresh` 추가(결과 무관 redirect, 백오프 없음).

설정(`settings`): `official_fetch.enabled` + provider 토글 + `min_interval_minutes` + `reset_cycle`(provider별) 노출/편집. **`credit_to_usd`는 `tokenomy.config.json`**(pricing.json 아님 — 토큰 cost 경로와 분리)에서 편집. 마지막 취득 상태 표시.

## 10. 테스트 — 3중 + fixture 정책

문제: enterprise 버킷은 enterprise 계정에서만 라이브(개발 머신은 개인 구독). 해법: 파서 모양 불문(§6) → 3중으로 닫힌다.

### fixture 정책 (public repo 안전 — 모양만 보존, 수치 가짜)
- 커밋 정본 `tests/fixtures/official/`: 키·중첩·null·exponent·unix 타임스탬프는 실측과 동일, **금액/크레딧만 가짜**.
  `claude_enterprise.json`·`codex_enterprise.json`·`claude_personal.json`·`codex_personal.json` + 코드네임 회전 변형 `claude_enterprise_rotated.json`.
- 실측 원문·로컬 fixture(`tests/fixtures/official/local/`)는 **gitignore**.

### (1) Fixture 단위테스트 — 어디서든
- `test_official_parser.py`: Claude→`event_credit`+`monthly_limit`+`promo`(USD), Codex→`codex_monthly`(크레딧 + USD=크레딧×`credit_to_usd`), 개인→`rate_window`. 회전 변형 동일 분류·다중 이벤트 `raw_key` 분리. null 스킵·exponent·unix→KST. **USD 환산 단정**.
- `test_aggregate.py`: 활성버킷(차분 기반+동률 tie-break+remaining fallback+stale/promo 제외+모순), 예측 렌즈 차분 위생, **Codex 2게이지**(월간=공식 직접 / 주간=로컬 첫-사용 7일 윈도우, 월÷4), **희소 스냅샷·유휴 후 재앵커·월 경계 교차**(주/월 윈도우 독립이라 음수 델타 없음 확인), Claude 월 경계, `days_left` 인스턴트, 데이터 없음→status.
- `test_db.py`: 트랜잭션 insert·UNIQUE 멱등·series 정렬·USD/네이티브 병행·fetch_state.
- `test_web.py`: 공식 패널(enterprise/personal/no_data), USD 1차+네이티브 병기, "사용됨", **합산 USD 표면**, fallback 라벨, 새로고침.

### (2) 개인계정 라이브 스모크 — 집/원격에서 코드 경로 검증
파서가 개인 모양도 처리하므로 fetch→인증→타임아웃/throttle→파싱→트랜잭션 적재→표시 전 구간을 라이브로 밟음.
% 창만 나올 뿐 네트워크·에러처리·비차단·throttle 검증. **단, enterprise 버킷/USD 환산/Codex 주간 윈도우/event·monthly 차감은 검증 못 함**(fixture + (3)).

### (3) enterprise 라이브 스모크 — enterprise 계정에서만
enabled 1회 fetch → 버킷 실값 + USD 환산 + 주간 윈도우 적재/표시 최종 확인(분기 1회 수준).

## 11. 단계 (phase)

- **Phase 1 — 모델 + 파서 + 표시(네트워크 없이 완결)**: 스키마 교체·마이그레이션, `official_parser`(USD 환산),
  `aggregate.official_view`(리셋 주기·주간 윈도우·예측), views/템플릿 공식 패널 + **합산 USD 표면** + 예측 렌즈, 수동 입력 제거.
  **검증 경로**: `cli.py`에 `official import-fixture <path>` dev 명령 추가(현 CLI는 ingest|report|all뿐) → sanitize fixture(Phase 1에서 `tests/fixtures/official/`에 커밋)를 주입해 앱에서 패널/합산 눈으로 확인(라이브 없이 user-verifiable).
- **Phase 2 — 라이브 취득**: `official_fetch`(옵트인·타임아웃·비차단·throttle·에러), `ingest` 훅, `POST /official/refresh`, settings 토글/상태/`credit_to_usd`/`reset_cycle`. 개인계정(집) + enterprise 계정 라이브 스모크.

## 12. 영향 범위 / 비변경

- **변경**: `db.py`(스키마 교체·마이그레이션·신규 함수), `aggregate.py`(official_view 신규·리셋 주기·주간 윈도우, official_merged_burndown 제거),
  **`tokenomy.config.json`**(`credit_to_usd` 기본 0.04 — 크레딧 단위가격 상수. **pricing.json·`pricing_fingerprint`·`maybe_reprice`엔 비접촉** — 토큰 cost 경로와 분리. Codex 토큰 단가는 pricing.json에 이미 존재해 변경 없음),
  `budget.py`/`aggregate.py`(Codex 주간 윈도우를 **월요일 cadence carryover → 로컬 CLI 첫-사용 7일 앵커**로 교정, `weekly_codex_limit`·`codex_burndown` 재사용·조정. 월÷4 한도는 공식 월 한도 기준), `web/views.py`(공식 패널 + Codex 2게이지 + 합산 USD), `web/app.py`(POST /official 제거, /official/refresh 추가),
  `web/templates/overview.html`·`settings.html`, `cli.py`(ingest 훅 + import-fixture), `config`(official_fetch·reset_cycle·credit_to_usd),
  `.gitignore`(로컬 fixture·실측), 신규 `official_fetch.py`·`official_parser.py`, 테스트·fixtures, `CLAUDE.md`(아키텍처/게시/주기 갱신), README.
- **`launcher.py`**: 코드 변경 없으나 ingest 경유 fetch가 **비차단·타임아웃(§7)**이어야 起動 영향 0 — 전제.
- **신규 런타임 의존성 없음** — `urllib.request`(stdlib).
- **비변경**: parser.py/codex_parser.py(로컬 로그), dedup, 증분 offset 스캔, **`pricing.json` Codex 토큰 단가(이미 USD 정합)**.
- **프라이버시 경계**: 사용량 수치만 저장, PII 미저장. 네트워크 옵트인(기본 off). 실측 금액 미커밋(예시는 가짜).

## 13. 후속 / backlog

- **일반 사용량(general) 가시화**: 크레딧·월할당 소진 후 차감되는 org 레벨. 개인 엔드포인트 미노출 → 미러 범위 밖("100% 미러" 아님).
- **이벤트 "남기면 아까움"**: 이벤트 크레딧(만료성) 천천히 쓰면 소멸 → 양방향(소진 위험 + 잔여 권장 페이스). 본 spec은 소진 예측만.
- **`pricing.json` 모델 정합**: `gpt-5.2`가 generic `gpt-5`로 오매칭 등 — 별도 "단가 커버리지" spec.
- **주간 윈도우 앵커 정밀화**: 첫 사용 시점/유휴 재앵커를 공식 데이터만으로 못 잡으면 로컬 CLI 첫 사용 추적 보강.
- **코드네임 동시 다중 버킷**: 현재 event 1개(가설적). `raw_key` 분리 대비만, 실제 출현 시 표시/렌즈 정책 재검토.
- **CLI quota 충돌**: throttle로 못 막음. 공유 버킷 조율 수단 생기면 재검토.
- **Gemini**: 일별 리셋 정액제, tokenomy 미지원. 추후 도입 시 별도.
