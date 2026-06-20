# 공식 사용량 자동 취득 (멀티버킷 + 예측 렌즈) — 설계

- 작성일: 2026-06-20
- 상태: **codex 리뷰 반영(2026-06-20) · 재확인 대기**
- 마일스톤: v0.2.0 채택 — "공식 사용량 수동 입력"(commit d29cdc9)을 **자동 취득 + 멀티버킷 정확화**로 대체
- 관련: [TODOS.md](../../../TODOS.md) ① · `tokenomy/db.py` · `tokenomy/aggregate.py` · `tokenomy/budget.py` ·
  `tokenomy/web/` · `tokenomy/cli.py` · `tokenomy/launcher.py`
- 근거 데이터: 실측 응답 원문은 **로컬 전용**(`docs/enterprise-usage-api-response.md`, 미추적 — 사내 할당액 노출 방지).
  커밋되는 정본은 **sanitize fixture**(§10, 모양 보존·금액 가짜).

## 0. codex 리뷰 반영 요약 (2026-06-20)

독립 리뷰(codex, 83k tok)에서 나온 유효 지적을 본 개정에 반영했다. 핵심 2건은 경식님 결정:
- **결정 A — 합산 표면 제거**: Codex 헤드라인이 credits가 되면 기존 "Claude+Codex 합산 월총액·결합 번다운·스택
  추세·total pace"(USD+credits 혼합)는 범주 오류. → **provider별 네이티브 표면만** 유지(§9). "합산 금지" 충실.
- **결정 B — fixture sanitize**: public repo이므로 실측 금액($243/$1000/크레딧)을 커밋하지 않는다. **모양만 보존한
  가짜 수치 fixture를 커밋**, 실수치는 로컬 gitignore(§10).

그 외 반영: 리셋 인스턴트 기준 주기 그룹핑(§5/§8), 스냅샷 트랜잭션·UNIQUE(§5), urllib 타임아웃·백오프 제거·
비차단 ingest(§7), throttle 효력 격하(§7), Claude 크레덴셜 만료/스키마 드리프트(§7), 활성버킷·예측렌즈 엣지
명세(§8), fallback 단위 혼동 방지(§9), Phase 1 `import-fixture` 검증 경로(§11).

## 1. 배경 / 문제

commit d29cdc9가 "회사 월 할당" 게이지를 위해 **공식 사용량 수동 입력**을 도입했다
(`official_usage.cumulative_usd` 단일값/월, `POST /official`, `official_merged_burndown`의 `max(공식,CLI)` 병합).
TODOS ①은 후속으로 "회사 포털/사내 API에서 공식 사용량을 **자동 취득**"을 예고했다.

2026-06-20 사내망 실측으로 각 CLI가 보관한 OAuth 토큰으로 **공식 사용량 API를 읽기 전용 단발 호출**하면
실데이터가 온다는 것이 확인됐다(원문은 로컬 전용 문서에 보존):

- **Claude**: `GET https://api.anthropic.com/api/oauth/usage` (Bearer + `anthropic-beta: oauth-2025-04-20`)
- **Codex**: `GET https://chatgpt.com/backend-api/wham/usage` (Bearer JWT + `ChatGPT-Account-Id`)

enterprise 응답이 단일 `cumulative_usd` 모델로는 못 담는 구조를 드러냈다(공식 앱 화면과 1:1):

**Claude (enterprise, 공식 앱 = 막대 3개):**

| 공식 앱 표기 | API 필드(모양) | 실측 의미 | 리셋/만료 |
|---|---|---|---|
| 사용 한도 Enterprise | `extra_usage`(monthly_limit) + `spend`(used/limit amount_minor) | 월 한도 used/limit | **월 1일 09:00 KST** |
| Claude Code·Cowork / 포함된 크레딧 | 코드네임 키(`used_dollars`+`limit_dollars`+`resets_at`) | 일회성 이벤트 크레딧 | **만료일**(분기성) |
| Claude Design / 기본 제공 한도 | 코드네임 키(`utilization`만, 달러 null) | 별도 프로모션 | 초기화 안 됨 |

공식 문구: *"일회성 크레딧으로, **사용 한도 전에 적용**됩니다. 크레딧을 모두 소진하면 **일반 사용량**에서 차감"*
→ **차감 순서: 포함된 크레딧(이벤트) → 월 사용 한도 → 일반 사용량(org 레벨, 미러 범위 밖 §13).**

**Codex (enterprise, 공식 앱 = 막대 1개):** `spend_control.individual_limit` = 월 사용 한도, **단위 credits**
(used/limit/remaining/used_percent + `reset_at` unix, 월간).

**핵심 함정:**
1. **버킷 다중성** — Claude는 동시에 살아있는 3버킷 + 차감 순서.
2. **단위 이질성** — Claude=USD, Codex=**credits**. USD 합산은 범주 오류 → 합산 표면 제거(결정 A).
3. **코드네임 회전** — 이벤트/프로모션 키는 Anthropic 내부 회전 코드네임. 키 이름 하드코딩 금지(모양으로 분류).
4. **모양 분기** — 개발(집/원격) 머신은 **개인 구독**이라 같은 엔드포인트가 달러 버킷 대신 `five_hour`/`seven_day`
   **이용률(%) 창**을 반환(달러 필드 null). enterprise 달러 버킷은 사내망에서만 라이브로 나온다.

## 2. 목표 / 비목표

### 목표
- 공식 사용량을 **자동 취득**해 수동 입력을 **완전히 대체**한다(사람 개입 0).
- **공식 앱이 보여주는 버킷과 숫자가 일치하는 미러**를 보여준다(신뢰). 단 "일반 사용량"(org 레벨)은 개인
  엔드포인트에 안 나오므로 미러 범위 밖임을 명시(§13) — "100% 미러"가 아니라 "공식 앱이 노출하는 버킷의 미러".
- 그 위에 공식 앱에 없는 **예측 렌즈**(소비 속도·소진 예상일·D-day)를 얹는다 — 토크노미 고유 부가가치.
- Claude(USD 멀티버킷)와 Codex(credits 단일버킷)를 **네이티브 단위·별도 패널**로 정확히 표시.
- 개발 머신(개인 구독)에서도 **코드 경로 전체를 라이브로 검증**할 수 있게 한다(§10).

### 비목표
- **수동 입력 유지** — 안 함. `POST /official`·입력 폼·`cumulative_usd` 모델 제거(2026-06-20).
- **USD+credits 합산 표면 유지** — 안 함(결정 A). 결합 월총액·결합 번다운·스택 추세·total pace 제거(§9).
- **백그라운드 폴링** — 안 함. `ingest` 1회 + 수동 새로고침만.
- **토큰 직접 refresh** — 안 함. 크레덴셜 파일 읽기 전용. 만료 → 마지막 값 유지 + 안내(CLI가 갱신).
- **Codex의 USD 환산** — 안 함(pricing.json에 Codex 단가 없음). credits 네이티브 유지.
- **CLI quota 충돌 방지** — 못 함. throttle은 우리 호출 빈도만 제어(§7). 공유 버킷을 CLI와 조율할 수단 없음.
- **이벤트 "남기면 아까움" 경고 / 일반 사용량 가시화** — backlog(§13).

### 설계 원칙 적합성 (v0.2.0 4원칙)
- **자격/강도**: 공식 used는 ground truth → 게이지 본체 자격. 소진 임박만 warn.
- **raw 추출**: 토큰 usage 수치만 추출·저장. **PII(email/user_id/account_id) 저장 금지**.
- **provider parity**: 양쪽 모두 공식 실데이터 존재 → 충족(단위·버킷 수 차이는 표현으로 흡수).
- **cost/value**: 순수 파서 + 작은 네트워크 모듈 + 게이지. 옵트인이라 비활성 시 비용 0.

## 3. 설계 결정 요약 (2026-06-20)

| # | 결정 | 선택 |
|---|------|------|
| 1 | 게이지 의미 | 공식 미러(멀티버킷, 숫자 일치) + 예측 렌즈(소비속도/소진예상) |
| 2 | 데이터 진실원 | 공식 `used`/`limit`을 ground truth로 직접 사용 — `max(공식,CLI)` 병합 폐기 |
| 3 | 수동 입력 | **완전 제거**. 자동만. 데이터 없을 때만 CLI 추정으로 fallback(단위 라벨 명시 §9) |
| 4 | 단위 표시 | provider별 네이티브(Claude USD / Codex credits), **합산 금지**, 별도 패널 |
| 5 | % 표기 | provider 불문 **"사용됨"** 으로 통일(Codex는 `used_percent`/`100−남음` 환산) |
| 6 | Codex 헤드라인 | 공식 월간 크레딧으로 **교체**. 주간-USD-carryover 은퇴(파급: budget.py/settings/combined — §9/§12) |
| 7 | **합산 표면(결정 A)** | 결합 월총액·결합 번다운·스택 추세·total pace **제거**. provider 네이티브만 |
| 8 | **fixture(결정 B)** | sanitize(모양 보존·가짜 수치) 커밋, 실측 원문은 로컬 gitignore |
| 9 | 네트워크 경계 | **옵트인**(config `official_fetch.enabled` 기본 false) + provider별 토글 |
| 10 | 취득 시점 | `ingest` 1회(비차단) + 웹 "새로고침" 버튼. 폴링 없음 |
| 11 | 주기 그룹핑 | 달력월이 아니라 **`resets_at` 인스턴트** 기준(09:00 KST 리셋 정합) |
| 12 | 코드네임 | shape 휴리스틱 분류, 라벨 서술형. raw 코드네임은 보조 식별자로만 보존(다중 충돌 방지 §6) |
| 13 | 파서 범위 | 모양 불문 — enterprise 달러 버킷 + 개인 구독 % 창 둘 다(테스트 자산화) |

## 4. 아키텍처 / 데이터 흐름

```
크레덴셜(읽기전용) ─ official_fetch.py(네트워크, 옵트인, 타임아웃) ─ raw JSON ─┐
                                                                          │
raw JSON ─ official_parser.py(순수, shape 휴리스틱) ─ [OfficialBucket] ─ db.py(스냅샷 트랜잭션 적재)
                                                                            │
                                            aggregate.official_view ─ web/views.py ─ overview 게이지
```

신규 모듈 2개(계층 분리 유지):
- **`tokenomy/official_fetch.py`** — 유일한 아웃바운드. 토큰 읽기·헤더·GET(타임아웃)·throttle·인증/HTTP 에러 처리. 네트워크만.
- **`tokenomy/official_parser.py`** — 순수 함수(`raw dict → list[OfficialBucket]`). 네트워크/DB 없음 → fixture로 완결 테스트.

제거 대상(마이그레이션 §5):
- `aggregate.official_merged_burndown`, `OfficialMergedBurndown`, `max 병합` 경로
- `web/app.py`의 `POST /official`, `web/views.py`의 `_official_notes`/`_gauge`(병합 버전), `overview.html` 입력 폼
- `db.insert_official_snapshot`/`latest_official`/`official_series`(단일값) + 테이블 `official_usage`
- **합산 표면(결정 A)**: views의 결합 월총액·결합 번다운·`combined_burndown`·스택 추세 total·`budget.total` 기반 total pace

## 5. 데이터 모델 — `db.py`

신규 **버킷 단위** 테이블 + 취득 상태를 additive로 추가. 구 `official_usage`는 **사용 중단 후 마이그레이션에서 DROP**.

### 마이그레이션(단순 obsolete 처리 아님 — 코드 경로부터 제거)
DROP 전에 `official_usage`를 쓰는 모든 경로를 먼저 제거해야 한다: `db.SCHEMA`의 CREATE,
`insert_official_snapshot`/`latest_official`/`official_series`(구버전), `aggregate.official_merged_burndown`,
`app.POST /official`. 그 후 마이그레이션 step에서 `DROP TABLE IF EXISTS official_usage`(로컬 단일 사용자, 수동 데이터 obsolete).

```sql
CREATE TABLE IF NOT EXISTS official_buckets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT,          -- 'claude' | 'codex'
    fetched_at TEXT,        -- 스냅샷 as-of (KST ISO). 같은 값 = 한 스냅샷
    bucket_key TEXT,        -- 안정 논리 id('event'|'monthly'|'promo'|'five_hour'|'seven_day'|...)
    raw_key TEXT,           -- 원 API 키(코드네임). 표시 비사용 — 다중 충돌 시 series 보조 분리키
    bucket_kind TEXT,       -- 'event_credit'|'monthly_limit'|'promo'|'rate_window'|'codex_monthly'
    label TEXT,             -- 서술형 표시 라벨(코드네임 비의존)
    unit TEXT,              -- 'usd' | 'credit' | 'percent'
    used REAL,              -- 없으면 NULL(rate_window)
    limit_amount REAL,      -- 'limit'은 예약어 → limit_amount. 없으면 NULL
    remaining REAL,
    utilization REAL,       -- % (0~100)
    resets_at TEXT,         -- ISO 인스턴트(타임존 포함) 또는 NULL
    created_at TEXT,
    UNIQUE(provider, fetched_at, bucket_key)   -- 중복 refresh가 버킷 복제하지 않도록
);

CREATE TABLE IF NOT EXISTS official_fetch_state (
    provider TEXT PRIMARY KEY,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_status TEXT,       -- 'ok'|'throttled'|'auth_error'|'http_error'|'disabled'
    last_error TEXT         -- 짧은 사유(스택/토큰 미포함)
);
```

- **스냅샷 트랜잭션**: 한 fetch의 버킷 전부를 **단일 트랜잭션**으로 insert(부분 스냅샷 방지). 같은 `fetched_at` 재시도는
  UNIQUE로 멱등(중복 무시 또는 교체). `fetched_at`은 **응답의 as-of**(없으면 호출 시각)로, 초 정밀도 충돌 시 UNIQUE가 가드.
- **bucket_key는 안정 논리 id** — 코드네임이 아니라 모양에서 도출(`'event'` 등). 코드네임 회전에도 예측 차분이 안 끊김.
  같은 `bucket_kind`가 **동시에 2개 이상**이면(드묾) `raw_key`로 분리해 series 모호성 제거.
- DB 함수: `insert_official_snapshot(conn, provider, fetched_at, buckets)`(트랜잭션),
  `latest_official_snapshot(conn, provider)`, `official_bucket_series(conn, provider, bucket_key)`(`(fetched_at, used)` 오름차순),
  `get_fetch_state`/`upsert_fetch_state`.
- PII 컬럼 없음. 응답의 email/user_id/account_id는 파서가 **추출조차 안 한다**.

## 6. 파서 — `official_parser.py` (순수, 모양 불문)

```python
@dataclass
class OfficialBucket:
    bucket_key: str
    raw_key: str              # 원 API 키
    bucket_kind: str
    label: str
    unit: str                 # 'usd'|'credit'|'percent'
    used: float | None
    limit: float | None
    remaining: float | None
    utilization: float        # %
    resets_at: datetime | None

def parse_claude(raw: dict) -> list[OfficialBucket]: ...
def parse_codex(raw: dict) -> list[OfficialBucket]: ...
```

**정확한 필드 경로·null·exponent 타입·malformed 처리는 구현 계획에서 커밋된 sanitize fixture로 핀다운**한다
(본 spec은 분류 규칙 수준; 실측 모양은 §10 fixture가 정본).

### Claude 휴리스틱 (코드네임 비의존)
최상위 키를 순회하며 **모양으로 분류**(키 이름 매칭 금지). 누락/null 필드는 안전 스킵:

1. **`extra_usage` + `spend`** → `monthly_limit`. `limit/used = spend.{limit,used}.amount_minor / 10**exponent`,
   `unit='usd'`. 응답에 `resets_at` 없음 → **다음 1일 09:00 KST 인스턴트로 계산**(공식 앱 리셋과 정합).
   `bucket_key='monthly'`, `raw_key='spend'`, label="월 사용 한도".
2. **dict이고 `used_dollars`+`limit_dollars`+`resets_at` 채워진** 키 → `event_credit`. `used/limit/remaining=*_dollars`,
   `unit='usd'`, `resets_at` 그대로(타임존 포함). `bucket_key='event'`, `raw_key`=코드네임, label=f"포함된 크레딧 · {만료일} 만료".
3. **dict이고 `utilization`만(달러 null, `resets_at` null)** → `promo`. `unit='percent'`. `bucket_key='promo'`,
   label="별도 사용량(프로모션)". utilization 0이면 생략 가능.
4. **`five_hour`/`seven_day`/`seven_day_*` dict(개인 구독)** → `rate_window`. `utilization`만, `unit='percent'`,
   `resets_at` 그대로. `bucket_key`=원 키, label="5시간 창"/"7일 창"/"7일·Opus" 등.
- 같은 모양이 여럿이면 모두 방출하되 `bucket_key` 충돌 시 `raw_key`로 접미(예측은 활성 1개만 선택).

### Codex 휴리스틱
1. **`spend_control.individual_limit` dict** → `codex_monthly`. `used/limit/remaining`(문자열→float),
   `utilization=used_percent`, `unit='credit'`, `resets_at=reset_at`(unix→KST 인스턴트). `bucket_key='monthly'`.
2. **`rate_limit.primary_window`/`secondary_window` dict(개인)** → `rate_window`. `utilization=used_percent`,
   `unit='percent'`(used/limit 없음), `resets_at=reset_at`. `bucket_key='primary_window'`/'secondary_window'.

## 7. 취득 — `official_fetch.py` (옵트인, 비차단, 유일한 아웃바운드)

```python
def fetch_provider(provider: str, *, now_kst, config, conn) -> FetchResult: ...
```

- **옵트인**: `config.official_fetch.enabled` false면 즉시 `disabled`(네트워크 없음). provider별 토글도 검사.
  `TOKENOMY_SKIP_OFFICIAL_FETCH` 설정 시 enabled여도 강제 skip(오프라인/CI/테스트).
- **throttle**: `official_fetch_state.last_attempt_at` + `min_interval_minutes`(기본 5) 미달이면 `throttled`(마지막 스냅샷 유지).
  **효력 한계 명시**: 이는 *우리 호출 빈도*만 제어한다. 엔드포인트 quota는 CLI와 공유라 **CLI와의 충돌은 못 막는다**
  (그래서 간격을 보수적으로). Claude는 `/api/oauth/usage`가 ~3회 후 429.
- **HTTP 타임아웃 필수**: `urllib.request`에 **명시적 timeout**(connect+read ≤ 8s). 무한 대기로 `cmd_ingest`/launcher가
  멈추지 않게. **백오프 없음**: 단발 시도, 실패면 즉시 포기(마지막 스냅샷 유지) — UI/startup 블록 회피. 재시도는 다음 throttle 주기에.
- **비차단 보장**: `cmd_ingest`/`launcher` 경로에서 fetch 실패(타임아웃·네트워크·인증)는 **절대 ingest/起動를 막지 않는다**
  (예외 삼킴 → state 기록 → 진행). 옵트인 off가 기본이라 일반 사용자 startup엔 영향 0.
- **토큰 소스(읽기 전용)**:
  - Claude: `~/.claude/.credentials.json` → `claudeAiOauth.accessToken`. 헤더 Bearer + `anthropic-beta: oauth-2025-04-20`
    + `User-Agent: claude-code/<ver>`. **파일 없음/스키마 드리프트/`expiresAt` 만료** → `auth_error`(마지막 값 유지 + "Claude 재로그인" 안내), 호출 안 함.
  - Codex: `~/.codex/auth.json` → `tokens.access_token`, `tokens.account_id`. 헤더 Bearer + `ChatGPT-Account-Id`
    + `User-Agent: codex_cli_rs`. **account_id 누락/스키마 변경** → `auth_error`. 다중 계정은 auth.json의 단일 active만 사용(범위 밖).
- **에러 분류(공통: 마지막 스냅샷 보존)**: 401 → `auth_error`(Codex note="Codex CLI 1회 실행"). 429/5xx/네트워크/사내
  TLS 인터셉트 실패 → `http_error`. 파싱 실패 → `http_error`(원문 미저장).
- **성공 시**: 파서 → `insert_official_snapshot`(트랜잭션) → `upsert_fetch_state(ok)`.
- 호출 지점: `cli.cmd_ingest`(→ launcher) + 웹 `POST /official/refresh`(throttle 가드 안). 표준 라이브러리만.

## 8. 집계 — `aggregate.py` (순수)

```python
@dataclass
class OfficialLens:                 # 예측 렌즈 (한도 있는 활성 버킷에만)
    bucket_key: str
    daily_rate: float | None        # 단위/영업일. 유효 차분 1개 미만이면 None("추세 수집 중")
    exhaust_date: date | None
    days_left_to_reset: int | None  # resets_at 인스턴트까지 영업일
    dday_warning: bool

@dataclass
class OfficialView:
    provider: str
    buckets: list[OfficialBucket]   # 공식 앱 표시 순서
    active_key: str | None
    lens: OfficialLens | None
    fetched_at: datetime | None
    stale_minutes: int | None
    status: str                     # 'ok'|'no_data'|'disabled'|'auth_error'|'throttled'|'http_error'
    note: str | None

def official_view(conn, provider, now_kst) -> OfficialView: ...   # budget_start 안 받음(공식은 자체 리셋 §11)
```

- **표시 순서(공식 앱 미러)**: Claude = `monthly_limit` → `event_credit` → `promo` → `rate_window`. Codex = `codex_monthly` → `rate_window`.
- **활성 버킷 선택(견고화)**: 단순 차감순서 가정에 의존하지 않는다.
  1차: 최근 스냅샷 구간에서 **`used` 증가(양의 차분)가 가장 최근/가장 큰 버킷** = 실제 차감 중. 2차(시계열 부족 시):
  차감 순서 `[event, monthly]` 중 `remaining>0`인 첫 버킷. API가 모순(이벤트 remaining>0인데 monthly used 증가)이면 **실측 차분 우선**.
  Codex=`monthly`. `rate_window`/`promo`는 렌즈 없음(현재값만).
- **예측 렌즈 — 차분 위생**: `official_bucket_series`를 `fetched_at` **정렬·중복 제거** 후 사용. 규칙:
  음수 차분(리셋/만료/버킷 전환)은 **버린다**(직전까지의 단조 구간만), 동일 시각/너무 짧은 간격(예 < 30분)은 스킵,
  활성 버킷이 바뀌면 **이전 버킷 series는 예측에서 제외**. 속도는 영업일 환산(`business_days_between` 재사용,
  date 단위라 sub-day는 소실 — 일 단위 추세엔 충분). 유효 차분이 없으면 `daily_rate=None`.
  `exhaust_date = today + remaining/daily_rate`(영업일). `days_left_to_reset` = `resets_at` **인스턴트**까지 영업일.
- **데이터 없음/비활성**: `latest_official_snapshot` 비면 `status`는 fetch_state 따라(`no_data`/`disabled`/`auth_error`) →
  상위(views)가 CLI 추정으로 fallback(§9, 단위 라벨 명시).

## 9. 표시 — `web/views.py` + 템플릿 (결정 A 반영)

`overview` 컨텍스트:
- `official_view(conn, "claude", now)` / `official_view(conn, "codex", now)`를 **별도 패널**로. **결합/합산 표면 없음.**
- **제거(결정 A)**: 결합 월총액, 결합 번다운(`combined_burndown`), 스택 추세의 total 라인, `budget.total` 기반 total pace.
  추세는 **provider별 네이티브 라인**으로만(USD 라인·credits 라인 분리, 합산 라인 없음).
- 공식 데이터 있으면 **공식 미러 패널**(헤드라인). 버킷 막대: 라벨 + `used/limit`(네이티브) + `NN% 사용됨` + 리셋/만료.
  promo·rate_window는 `utilization%` + 리셋. 예측 렌즈(활성 버킷): 소비속도·소진예상·D-day(`daily_rate=None`→"추세 수집 중").
- **% 표기 "사용됨" 통일**(Codex `used_percent`/`100−remaining_percent`).
- **fallback 단위 혼동 방지**: 공식 없으면 CLI 추정으로 떨어지되, **단위를 절대 조용히 바꾸지 않는다.**
  Claude는 "공식 없음 — 로컬 USD **추정**" 라벨. Codex는 공식이 credits, CLI fallback은 USD이므로 **"공식 크레딧 미취득 —
  로컬 USD 추정"** 을 명시(credits↔USD 무언 전환 금지). "공식 동기화를 켜면 실측" 힌트 + status별 note(예 "Codex 1회 실행").
- **새로고침 버튼**: `POST /official/refresh` → throttle 가드 후 fetch → redirect. "마지막 업데이트 N분 전"(`stale_minutes`).

`web/app.py`: `POST /official` 제거. `POST /official/refresh` 추가(결과 무관 redirect, 백오프 없음).

설정(`settings`): `official_fetch.enabled` + provider 토글 + `min_interval_minutes` 노출/편집. 마지막 취득 상태 표시.
Codex 라벨이 USD→credits로 바뀌므로 settings의 Codex 예산 단위 표기도 갱신(§12).

## 10. 테스트 — 3중 + fixture 정책(결정 B)

문제: enterprise 달러 버킷은 사내망에서만 라이브. 해법: 파서 모양 불문(§6) → 3중으로 닫힌다.

### fixture 정책(결정 B — public repo 안전)
- **커밋되는 정본 = sanitize fixture** `tests/fixtures/official/`: 모양·키·중첩·null·exponent·unix 타임스탬프는
  실측과 동일, **금액/크레딧 수치만 가짜**(예 $243→$100, 5875cr→1000cr). 파서 분류·환산 로직 검증에 충분.
  - `claude_enterprise.json`, `codex_enterprise.json`, `claude_personal.json`, `codex_personal.json`
  - 코드네임 회전 변형: 이벤트 키 이름을 임의 문자열로 바꾼 `claude_enterprise_rotated.json`.
- **실측 원문은 로컬 전용**: `docs/enterprise-usage-api-response.md` 및 `tests/fixtures/official/local/`는 **gitignore**.
  필요 시 로컬에서 실수치로 추가 검증.

### (1) Fixture 단위테스트 — 어디서든
- `test_official_parser.py`: Claude enterprise→`event_credit`+`monthly_limit`+`promo`, Codex enterprise→`codex_monthly`,
  개인→`rate_window`. 회전 변형도 동일 분류. null 스킵·exponent·unix→KST. (금액은 sanitize 값으로 단정.)
- `test_aggregate.py`(official_view): 활성버킷(차분 기반 + remaining fallback + 모순 케이스), 예측 렌즈 차분 위생
  (음수차분/중복시각/버킷전환/유효차분 0), `days_left` 인스턴트 기준, 데이터 없음→status.
- `test_db.py`: 트랜잭션 insert·UNIQUE 멱등·series 정렬·fetch_state 라운드트립·부분 스냅샷 없음.
- `test_web.py`: 공식 패널(enterprise/personal/no_data), "사용됨" 표기, 합산 표면 제거 확인, fallback 단위 라벨, 새로고침 라우트.

### (2) 개인계정 라이브 스모크 — 집/원격에서 코드 경로 검증
파서가 개인 모양도 처리하므로 **fetch→인증→타임아웃/throttle→파싱→트랜잭션 적재→표시** 전 구간을 라이브로 밟는다.
% 창만 나올 뿐 네트워크·에러처리·비차단·throttle 코드가 실제로 도는지 검증. **단, enterprise 달러버킷/exponent/
event·monthly 차감/Codex 크레딧 모양은 검증 못 함**(그건 fixture + (3) 사내망 전용).

### (3) enterprise 라이브 스모크 — 사내망에서만
사내망에서 enabled 1회 fetch → 달러 버킷 실값 적재/표시 최종 확인(분기 1회 수준).

## 11. 단계 (phase)

증분 릴리스. 각 Phase 독립 검증.

- **Phase 1 — 모델 + 파서 + 표시(네트워크 없이 완결)**: 스키마 교체·마이그레이션, `official_parser`,
  `aggregate.official_view`, views/템플릿 공식 패널 + 예측 렌즈, **수동 입력 + 합산 표면 제거**(결정 A).
  **사용자 검증 경로**: `cli.py`에 `official import-fixture <path>` dev 명령 추가 → fixture를 DB에 주입해 **앱에서
  공식 패널을 눈으로 확인** 가능(라이브 fetch 없이도 Phase 1이 user-verifiable).
- **Phase 2 — 라이브 취득**: `official_fetch`(옵트인·타임아웃·비차단·throttle·인증/HTTP 에러), `ingest` 훅,
  `POST /official/refresh`, settings 토글/상태. 개인계정(집) + enterprise(사내망) 라이브 스모크.

## 12. 영향 범위 / 비변경

- **변경**: `db.py`(스키마 교체·마이그레이션 DROP·신규 함수), `aggregate.py`(official_view 신규, official_merged_burndown
  제거, **combined_burndown/total pace 제거**), `budget.py`(Codex USD 주간 의미 은퇴 — `Budget.codex` 단위/역할 재정의),
  `web/views.py`(공식 패널, **합산 표면 제거**), `web/app.py`(POST /official 제거, /official/refresh 추가),
  `web/templates/overview.html`·`settings.html`(Codex 단위 표기·합산 카드 제거), `cli.py`(ingest 훅 + `import-fixture`),
  `config`(official_fetch), 신규 `official_fetch.py`·`official_parser.py`, `.gitignore`(로컬 fixture·실측 문서),
  테스트·fixtures, `CLAUDE.md`(아키텍처/게시/Codex 주기 갱신), README.
- **`launcher.py`**: 코드 변경은 없으나 **ingest 경유 fetch가 비차단·타임아웃(§7)이어야** 起動 영향 0 — 이 보장이 전제.
- **신규 런타임 의존성 없음** — `urllib.request`(stdlib).
- **비변경**: parser.py/codex_parser.py(로컬 로그), pricing 경로, dedup, 증분 offset 스캔. 공식 취득은 로컬 파이프라인과 독립.
- **프라이버시 경계**: 사용량 수치만 저장, PII 미저장. 네트워크 옵트인(기본 off). 실측 금액은 미커밋(결정 B).

## 13. 후속 / backlog

- **일반 사용량(general) 가시화**: 크레딧·월할당 소진 후 차감되는 org 레벨. 개인 엔드포인트 미노출 → 미러 범위 밖.
  "100% 미러"가 아닌 이유. 추후 org/admin 경로 검토.
- **이벤트 "남기면 아까움"**: 이벤트 크레딧(만료성)을 천천히 쓰면 소멸 → 양방향(소진 위험 + 잔여 권장 페이스). 본 spec은 소진 예측만.
- **코드네임 동시 다중 버킷**: 현재 실측은 event 1개뿐(가설적). `raw_key` 분리로 대비는 해두되, 실제 다중 출현 시 표시/렌즈 정책 재검토.
- **CLI quota 충돌**: throttle로 못 막음(§7). 공유 버킷 조율 수단 생기면 재검토.
- **Codex 라벨 매핑 불일치**: API `plan_type:business` ↔ CLI `/status` "Enterprise". 표시 정책 후속.
- **Claude 5h/7d 창(개인) 예측**: 롤링 창이라 D-day 의미 약함 → 현재값만.
