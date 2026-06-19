# 공식 사용량 자동 취득 (멀티버킷 + 예측 렌즈) — 설계

- 작성일: 2026-06-20
- 상태: 승인 대기(spec 리뷰)
- 마일스톤: v0.2.0 채택 — "공식 사용량 수동 입력"(commit d29cdc9)을 **자동 취득 + 멀티버킷 정확화**로 대체
- 관련: [TODOS.md](../../../TODOS.md) ① · [enterprise-usage-api-response.md](../../enterprise-usage-api-response.md) ·
  `tokenomy/db.py` · `tokenomy/aggregate.py` · `tokenomy/web/` · `tokenomy/cli.py` · `tokenomy/launcher.py`

## 1. 배경 / 문제

commit d29cdc9가 "회사 월 할당" 게이지를 위해 **공식 사용량 수동 입력**을 도입했다
(`official_usage.cumulative_usd` 단일값/월, `POST /official`, `official_merged_burndown`의 `max(공식,CLI)` 병합).
TODOS ①은 후속으로 "회사 포털/사내 API에서 공식 사용량을 **자동 취득**"을 예고했다.

2026-06-20 사내망 실측([enterprise-usage-api-response.md](../../enterprise-usage-api-response.md))으로
각 CLI가 보관한 OAuth 토큰으로 **공식 사용량 API를 읽기 전용 단발 호출**하면 실데이터가 온다는 것이 확인됐다:

- **Claude**: `GET https://api.anthropic.com/api/oauth/usage` (Bearer + `anthropic-beta: oauth-2025-04-20`)
- **Codex**: `GET https://chatgpt.com/backend-api/wham/usage` (Bearer JWT + `ChatGPT-Account-Id`)

그런데 enterprise 응답이 단일 `cumulative_usd` 모델로는 못 담는 구조를 드러냈다(공식 앱 화면과 1:1):

**Claude (enterprise, 공식 앱 = 막대 3개):**

| 공식 앱 표기 | API 필드 | 실측값 | 리셋/만료 |
|---|---|---|---|
| 사용 한도 Enterprise | `extra_usage`(monthly_limit 24300) + `spend`(limit 24300) | $0 / $243 | **월 1일** (7/1 09:00 KST) |
| Claude Code·Cowork / 포함된 크레딧 | `cinder_cove`(used_dollars 393.10 / limit_dollars 1000) | 39.3% | **9/10 만료**(일회성) |
| Claude Design / 기본 제공 한도 | `omelette_promotional`(utilization 0) | 0% | 초기화 안 됨 |

공식 문구: *"일회성 크레딧으로, **사용 한도 전에 적용**됩니다. 크레딧을 모두 소진하면 **일반 사용량**에서 차감"*
→ **차감 순서: 포함된 크레딧(이벤트) → 월 사용 한도($243) → 일반 사용량.**

**Codex (enterprise, 공식 앱 = 막대 1개):**

| 공식 앱 표기 | API 필드 | 실측값 | 리셋 |
|---|---|---|---|
| 월 사용 한도 | `spend_control.individual_limit` | 1,074 / 5,875 **크레딧** (18% 사용) | **월** (7/1 09:00 KST) |

**핵심 함정:**
1. **버킷 다중성** — Claude는 동시에 살아있는 3버킷(이벤트/월할당/별도) + 차감 순서.
2. **단위 이질성** — Claude=USD, Codex=**credits**. 하나의 USD 합계로 더하면 범주 오류.
3. **코드네임 회전** — `cinder_cove`/`omelette_promotional` 등은 Anthropic 내부 회전 코드네임. 키 이름 하드코딩 금지.
4. **모양 분기** — 개발(집/원격) 머신은 **개인 구독**이라 같은 엔드포인트가 달러 버킷 대신 `five_hour`/`seven_day`
   **이용률(%) 창**을 반환한다(달러 필드 전부 null). enterprise 달러 버킷은 사내망에서만 라이브로 나온다.

## 2. 목표 / 비목표

### 목표
- 공식 사용량을 **자동 취득**해 수동 입력을 **완전히 대체**한다(사람 개입 0).
- 공식 앱과 **숫자가 100% 일치하는 멀티버킷 미러**를 보여준다(신뢰).
- 그 위에 공식 앱에 없는 **예측 렌즈**(소비 속도·소진 예상일·D-day)를 얹는다 — 토크노미 고유 부가가치.
- Claude(USD 멀티버킷)와 Codex(credits 단일버킷)를 **네이티브 단위·별도 패널**로 정확히 표시.
- 개발 머신(개인 구독)에서도 **코드 경로 전체를 라이브로 검증**할 수 있게 한다(§10).

### 비목표
- **수동 입력 유지** — 안 함. `POST /official`·입력 폼·`cumulative_usd` 모델 제거(2026-06-20 결정: "입력할 사람 없음").
- **백그라운드 폴링** — 안 함. `ingest` 1회 + 수동 새로고침만(단발 앱 성격, throttle 위험 회피).
- **토큰 직접 refresh** — 안 함. 크레덴셜 파일 읽기 전용. 만료/401 → 마지막 값 유지 + 안내(CLI가 갱신).
- **Codex의 USD 환산** — 안 함(pricing.json에 Codex 단가 없음, 섞으면 범주 오류). credits 네이티브 유지.
- **이벤트 "남기면 아까움" 경고** — backlog(§13). 본 spec은 소진 예측만.

### 설계 원칙 적합성 (v0.2.0 4원칙)
- **자격/강도**: 공식 used는 ground truth(추정 아님) → 게이지 본체 자격. 소진 임박만 warn, 그 외 정보.
- **raw 추출**: 토큰 usage 수치만 추출·저장. **PII(email/user_id/account_id) 저장 금지**(발췌선 준수).
- **provider parity**: Claude·Codex 양쪽 모두 공식 실데이터 존재 → 충족(단위·버킷 수 차이는 표현으로 흡수).
- **cost/value**: 순수 파서 + 작은 네트워크 모듈 + 게이지. 옵트인이라 비활성 시 비용 0.

## 3. 설계 결정 요약 (2026-06-20 합의)

| # | 결정 | 선택 |
|---|------|------|
| 1 | 게이지 의미 | 공식 미러(멀티버킷, 숫자 일치) + 예측 렌즈(소비속도/소진예상) |
| 2 | 데이터 진실원 | 공식 `used`/`limit`을 ground truth로 직접 사용 — `max(공식,CLI)` 병합 폐기 |
| 3 | 수동 입력 | **완전 제거**. 자동만. 데이터 없을 때만 CLI 추정으로 fallback |
| 4 | 단위 표시 | provider별 네이티브(Claude USD / Codex credits), **합산 금지**, 별도 패널 |
| 5 | % 표기 | provider 불문 **"사용됨"** 으로 통일(Codex는 `used_percent`/`100−남음` 환산) |
| 6 | Codex 헤드라인 | 공식 월간 크레딧으로 **교체**. 주간-USD-carryover 번다운 은퇴(CLI는 토큰량 분해용으로만 잔존) |
| 7 | 네트워크 경계 | **옵트인**(config `official_fetch.enabled` 기본 false) + provider별 토글 |
| 8 | 취득 시점 | `ingest` 1회 + 웹 "새로고침" 버튼. 폴링 없음 |
| 9 | 코드네임 | **shape 휴리스틱**으로 분류, 키 이름 하드코딩 금지. 라벨은 서술형(모양 기반) |
| 10 | 파서 범위 | **모양 불문** — enterprise 달러 버킷 + 개인 구독 % 창 둘 다 처리(테스트 자산화) |
| 11 | 테스트 | 두 모양 fixture 단위테스트 + 개인계정 라이브 스모크(집) + enterprise 라이브 스모크(사내망) |

## 4. 아키텍처 / 데이터 흐름

```
크레덴셜(읽기전용) ─ official_fetch.py(네트워크, 옵트인) ─ raw JSON ─┐
                                                                  │
raw JSON ─ official_parser.py(순수, shape 휴리스틱) ─ [OfficialBucket] ─ db.py(스냅샷 적재)
                                                                            │
                                            aggregate.official_view ─ web/views.py ─ overview 게이지
```

신규 모듈 2개(계층 분리 유지):
- **`tokenomy/official_fetch.py`** — 유일한 아웃바운드. 토큰 읽기·헤더·GET·throttle·백오프·401/429 처리. 네트워크만.
- **`tokenomy/official_parser.py`** — 순수 함수(`raw dict → list[OfficialBucket]`). 네트워크/DB 없음 → fixture로 완결 테스트.

제거 대상:
- `aggregate.official_merged_burndown`, `OfficialMergedBurndown`, `_compute_burndown`의 `max 병합` 경로
- `web/app.py`의 `POST /official`, `web/views.py`의 `_official_notes`/`_gauge`(병합 버전), `overview.html` 입력 폼
- `db.insert_official_snapshot`/`latest_official`/`official_series`(단일값 버전) → 멀티버킷 버전으로 교체

## 5. 데이터 모델 — `db.py`

기존 단일값 `official_usage`(manual)는 코드에서 **사용 중단**하고, 신규 **버킷 단위** 테이블 + 취득 상태를
additive(`CREATE TABLE IF NOT EXISTS`)로 추가한다. 구 `official_usage`는 로컬 수동 데이터가 obsolete이므로
마이그레이션 단계에서 **DROP**(CLAUDE.md의 `_MIGRATE_COLS` ALTER 관례와 별개의 1회성 정리):

```sql
CREATE TABLE IF NOT EXISTS official_buckets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT,          -- 'claude' | 'codex'
    fetched_at TEXT,        -- 스냅샷 as-of (KST ISO). 같은 값 = 한 스냅샷
    bucket_key TEXT,        -- 안정 논리 id('event'|'monthly'|'promo'|'five_hour'|'seven_day'|'seven_day_opus'...)
    bucket_kind TEXT,       -- 'event_credit'|'monthly_limit'|'promo'|'rate_window'|'codex_monthly'
    label TEXT,             -- 서술형 표시 라벨(코드네임 비의존)
    unit TEXT,              -- 'usd' | 'credit' | 'percent'
    used REAL,              -- 없으면 NULL(rate_window)
    limit_amount REAL,      -- 'limit'은 SQL 예약어 → limit_amount. 없으면 NULL
    remaining REAL,
    utilization REAL,       -- % (0~100)
    resets_at TEXT,         -- ISO 또는 NULL
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_official_buckets_lookup
    ON official_buckets(provider, bucket_key, fetched_at);

CREATE TABLE IF NOT EXISTS official_fetch_state (
    provider TEXT PRIMARY KEY,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_status TEXT,       -- 'ok'|'throttled'|'auth_error'|'http_error'|'disabled'
    last_error TEXT         -- 짧은 사유(스택/토큰 미포함)
);
```

- **스냅샷** = 같은 `(provider, fetched_at)`를 공유하는 버킷 행들. fetch 1회가 버킷마다 한 행.
- **bucket_key는 안정 논리 id** — 코드네임(cinder_cove)이 아니라 모양에서 도출한 `'event'` 같은 키.
  코드네임이 회전해도 **예측 차분(같은 `bucket_key`의 `used` 시계열)이 끊기지 않는다.**
- DB 함수:
  - `insert_official_snapshot(conn, provider, fetched_at, buckets)` — 버킷 리스트 일괄 insert.
  - `latest_official_snapshot(conn, provider)` — 최신 `fetched_at`의 버킷 전부.
  - `official_bucket_series(conn, provider, bucket_key)` — 해당 버킷의 `(fetched_at, used)` 오름차순(예측 차분용).
  - `get_fetch_state`/`upsert_fetch_state`.
- PII 컬럼 없음. 응답의 email/user_id/account_id는 파서가 **추출조차 안 한다**.

## 6. 파서 — `official_parser.py` (순수, 모양 불문)

```python
@dataclass
class OfficialBucket:
    bucket_key: str
    bucket_kind: str          # 'event_credit'|'monthly_limit'|'promo'|'rate_window'|'codex_monthly'
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

### Claude 휴리스틱 (코드네임 비의존)
최상위 키를 순회하며 **모양으로 분류**한다(키 이름 매칭 금지):

1. **`extra_usage` + `spend`** → `monthly_limit` 버킷. `limit = spend.limit.amount_minor / 10**exponent`($243),
   `used = spend.used.amount_minor / 10**exponent`, `unit='usd'`. 응답에 `resets_at`이 없으므로 **다음 달 1일로 계산**
   (표시 시각은 공식 앱 "09:00 GMT+9" 관례를 따르되, 번다운은 date 단위라 시각은 표시용). `bucket_key='monthly'`, label="월 사용 한도".
2. **값이 dict이고 `used_dollars`+`limit_dollars`+`resets_at`이 채워진** 키 → `event_credit` 버킷.
   `used/limit/remaining=*_dollars`, `unit='usd'`, `resets_at` 그대로. `bucket_key='event'`,
   label=f"포함된 크레딧 · {resets_at:%-m/%-d} 만료".
3. **값이 dict이고 `utilization`은 있으나 달러 필드가 null, `resets_at`도 null** → `promo` 버킷.
   `utilization`만, `unit='percent'`. `bucket_key='promo'`, label="별도 사용량(프로모션)". utilization 0이면 **생략 가능**.
4. **`five_hour`/`seven_day`/`seven_day_*`가 dict로 채워진 경우(개인 구독)** → 각각 `rate_window` 버킷.
   `utilization`만, `unit='percent'`, `resets_at` 그대로. `bucket_key`=원 키('five_hour' 등),
   label="5시간 창"/"7일 창"/"7일·Opus" 등.
- null 값 키는 건너뛴다. 같은 모양 버킷이 여럿이면 모두 방출(예측 렌즈는 활성 1개만 선택).

### Codex 휴리스틱
1. **`spend_control.individual_limit`이 dict** → `codex_monthly` 버킷.
   `used/limit/remaining` 그대로(문자열 → float), `utilization = used_percent`, `unit='credit'`,
   `resets_at = reset_at`(unix → KST ISO). `bucket_key='monthly'`, label="월 사용 한도".
2. **`rate_limit.primary_window`/`secondary_window`가 dict(개인 구독)** → `rate_window` 버킷.
   `utilization = used_percent`, `unit='percent'`(used/limit 없음 — 개인 구독 창은 % 만 노출), `resets_at = reset_at`.
   `bucket_key='primary_window'`/'secondary_window', label="5시간 창"/"7일 창".

## 7. 취득 — `official_fetch.py` (옵트인, 유일한 아웃바운드)

```python
def fetch_provider(provider: str, *, now_kst, config, conn) -> FetchResult: ...
```

- **옵트인**: `config.official_fetch.enabled`가 false면 즉시 `disabled` 반환(네트워크 호출 없음).
  provider별 `official_fetch.claude`/`codex` 토글도 검사.
- **throttle**: `official_fetch_state.last_attempt_at` + `config.official_fetch.min_interval_minutes`(기본 5).
  간격 미달이면 `throttled` 반환(마지막 스냅샷 유지). Claude는 `/api/oauth/usage`가 ~3회 후 429
  (CLI와 버킷 공유, Retry-After 없음)라 이 가드가 특히 중요.
- **토큰 소스(읽기 전용)**:
  - Claude: `~/.claude/.credentials.json` → `claudeAiOauth.accessToken`.
    헤더 `Authorization: Bearer …` + `anthropic-beta: oauth-2025-04-20` + `User-Agent: claude-code/<ver>`.
  - Codex: `~/.codex/auth.json` → `tokens.access_token`, `tokens.account_id`.
    헤더 `Authorization: Bearer …` + `ChatGPT-Account-Id: <account_id>` + `User-Agent: codex_cli_rs`.
- **에러 처리(마지막 스냅샷 보존)**:
  - 401(Codex 토큰 만료) → `auth_error`, note="Codex CLI를 1회 실행하면 토큰이 갱신됩니다".
  - 429(Claude throttle) → 지수 백오프 후에도 실패면 `http_error`, 마지막 스냅샷 유지.
  - 네트워크/프록시(사내 TLS 인터셉트) 실패 → `http_error`, 마지막 값 유지.
- **성공 시**: 파서로 버킷화 → `insert_official_snapshot` → `upsert_fetch_state(ok)`.
- 표준 라이브러리만(`urllib.request`) — 신규 런타임 의존성 없음.
- 호출 지점: `cli.cmd_ingest`(따라서 `launcher`의 ingest 1회에도 포함) + 웹 `POST /official/refresh`(throttle 가드 안).
- **환경변수**: `TOKENOMY_SKIP_UPDATE_CHECK`처럼 오프라인/CI/테스트용으로 enabled여도 강제 skip하는
  `TOKENOMY_SKIP_OFFICIAL_FETCH`를 둔다.

## 8. 집계 — `aggregate.py` (순수)

```python
@dataclass
class OfficialLens:                 # 예측 렌즈 (한도 있는 활성 버킷에만)
    bucket_key: str
    daily_rate: float | None        # 단위/영업일(스냅샷 used 차분). 스냅샷 2개 미만이면 None
    exhaust_date: date | None       # today + remaining/daily_rate(영업일)
    days_left_to_reset: int | None  # resets_at/만료까지 영업일
    dday_warning: bool              # 소진 예상일이 리셋/만료보다 충분히 앞서면 True

@dataclass
class OfficialView:
    provider: str
    buckets: list[OfficialBucket]   # 공식 앱 표시 순서
    active_key: str | None          # 예측 렌즈 대상(현재 차감 중)
    lens: OfficialLens | None
    fetched_at: datetime | None
    stale_minutes: int | None
    status: str                     # 'ok'|'no_data'|'disabled'|'auth_error'|'throttled'|'http_error'
    note: str | None

def official_view(conn, provider, now_kst, *, budget_start=None) -> OfficialView: ...
```

- **표시 순서(공식 앱 미러)**: Claude = `monthly_limit` → `event_credit` → `promo` → `rate_window`.
  Codex = `codex_monthly` → `rate_window`.
- **활성 버킷 선택(차감 순서)**: Claude = [`event`, `monthly`] 중 `remaining>0`인 첫 버킷(이벤트 우선,
  소진되면 monthly). Codex = `monthly`. `rate_window`/`promo`는 예측 대상 아님(렌즈 없음, 현재값만 표시).
- **예측 렌즈**: `official_bucket_series(provider, active_key)`의 인접 `used` 차분 → 영업일 소비속도
  (`business_days_between` 재사용) → `exhaust_date`. 스냅샷 2개 미만이면 `daily_rate=None`("추세 수집 중").
  `days_left_to_reset` = `resets_at`까지 영업일.
- **데이터 없음/비활성**: `latest_official_snapshot`이 비면 `status` = 취득 상태에 따라
  `no_data`/`disabled`/`auth_error` 등. 이때 **상위(views)에서 CLI 추정 번다운으로 fallback**.

## 9. 표시 — `web/views.py` + 템플릿

`overview` 컨텍스트 조립:
- `official_view(conn, "claude", now)` / `official_view(conn, "codex", now)`를 각각 패널 컨텍스트로.
- 공식 데이터 있으면 **공식 미러 패널**(헤드라인), 없으면 **CLI 추정 번다운 fallback** + "공식 동기화를 켜면
  실측으로 바뀝니다" 힌트(`status`에 따라 "Codex 1회 실행" 등 note).
- **% 표기는 "사용됨"으로 통일**(Codex `used_percent`/`100−remaining_percent`로 환산).
- 버킷 막대: 라벨 + `used/limit`(네이티브 단위) + `NN% 사용됨` + 리셋/만료일. promo·rate_window는 `utilization%` + 리셋.
- 예측 렌즈(활성 버킷): 소비속도·소진 예상일·D-day 경고. `daily_rate=None`이면 "추세 수집 중".
- **새로고침 버튼**: `POST /official/refresh` → throttle 가드 후 fetch → redirect. "마지막 업데이트 N분 전"(`stale_minutes`).

`web/app.py`:
- `POST /official` **제거**. `POST /official/refresh` 추가(throttle 안에서 `fetch_provider` 호출, 결과 무관 redirect).

설정(`settings`):
- `official_fetch.enabled` + provider 토글 + `min_interval_minutes`를 settings에서 노출/편집.
- 마지막 취득 상태(`official_fetch_state`) 표시(성공 시각/실패 사유).

## 10. 테스트 — 3중 (개인 구독을 자산화)

문제: enterprise 달러 버킷은 사내망에서만 라이브로 나옴 → 집/원격(개인 구독)에선 그 경로를 라이브로 못 밟음.
해법: **파서를 모양 불문으로** 만들어 두 모양 다 처리(§6) → 테스트가 3중으로 닫힌다.

### (1) Fixture 단위테스트 — 어디서든
`tests/fixtures/official/`에 두 모양 모두 기록(PII 마스킹):
- `claude_enterprise.json` — [enterprise-usage-api-response.md](../../enterprise-usage-api-response.md)의 응답.
- `codex_enterprise.json` — 동 문서.
- `claude_personal.json` — 개인 구독 응답(five_hour/seven_day/seven_day_sonnet 이용률 창).
- `codex_personal.json` — 개인 구독 응답(rate_limit.primary/secondary_window).

`tests/test_official_parser.py`:
- Claude enterprise → `event_credit`(used 393.10/limit 1000/리셋 9/10) + `monthly_limit`($0/$243/월1일) +
  `promo`(0%) 방출. **달러 버킷 단정은 여기서만 가능.**
- Codex enterprise → `codex_monthly`(1073.94/5875 credit, used_percent 18, 리셋 7/1).
- Claude personal → `rate_window`×N(이용률만, 달러 없음). Codex personal → `rate_window`×2.
- 코드네임 회전 내성: 키 이름을 `cinder_cove`→임의 문자열로 바꾼 변형 fixture로도 동일 분류.
- null 값 키 스킵, exponent 환산, unix→KST 변환.

`tests/test_aggregate.py`(official_view):
- 활성 버킷 선택(이벤트 remaining>0 → active='event'; 이벤트 0으로 변형 → active='monthly').
- 예측 렌즈: 스냅샷 2개(used 증가) → daily_rate/exhaust_date 산출; 1개면 None.
- 데이터 없음 → status='no_data'(+ fallback 신호).

`tests/test_db.py`: insert/latest/series/fetch_state 라운드트립, 스냅샷 그룹핑.

`tests/test_web.py`: 공식 패널 렌더(enterprise/personal/no_data 각), % "사용됨" 표기, 새로고침 라우트.

### (2) 개인계정 라이브 스모크 — 집/원격에서 코드 경로 검증
파서가 개인 모양도 처리하므로 **fetch→인증(크레덴셜 읽기)→throttle→파싱→적재→표시 전 구간을 라이브로** 밟을 수 있다.
결과가 달러 버킷이 아닌 % 창일 뿐, **네트워크·에러처리·throttle 코드가 실제로 도는지** 검증된다.
- 수동 스모크 스크립트(테스트 아님, opt-in): enabled로 1회 fetch → 개인 % 창 버킷이 DB에 적재되는지 확인.
- 이것이 "집에선 enterprise 테스트가 어렵다"는 우려의 직접 해답.

### (3) enterprise 라이브 스모크 — 사내망에서만
사내망에서 enabled로 1회 fetch → 달러 버킷 실값 적재/표시 최종 확인(분기 1회 수준).

## 11. 단계 (phase)

증분 릴리스. 각 Phase는 독립 검증 가능.

- **Phase 1 — 모델 + 파서 + 표시(fixture로 완결)**: 스키마 교체, `official_parser`, `aggregate.official_view`,
  views/템플릿 공식 패널 + 예측 렌즈, 수동 입력 제거. **네트워크 없이** fixture로 전부 테스트.
  (이 단계에서 데이터 주입은 fixture/스모크 스크립트로.)
- **Phase 2 — 라이브 취득**: `official_fetch`(옵트인·throttle·401/429), `ingest` 훅, `POST /official/refresh`,
  settings 토글/상태. 개인계정(집) + enterprise(사내망) 라이브 스모크.

## 12. 영향 범위 / 비변경

- **변경**: `db.py`(스키마 교체·신규 함수), `aggregate.py`(official_view 신규, official_merged_burndown 제거),
  `web/views.py`(공식 패널 조립), `web/app.py`(POST /official 제거, /official/refresh 추가),
  `web/templates/overview.html`·`settings.html`, `cli.py`(ingest 훅), `launcher.py`(영향 없음 — ingest 경유),
  `config`(official_fetch), 신규 `official_fetch.py`·`official_parser.py`, 테스트·fixtures, `CLAUDE.md`(아키텍처/게시 갱신),
  README.
- **신규 런타임 의존성 없음** — `urllib.request`(stdlib).
- **비변경**: parser.py/codex_parser.py(로컬 로그 파이프라인), pricing 경로, dedup, 증분 offset 스캔.
  공식 취득은 로컬 파이프라인과 **독립**(별도 테이블·별도 모듈).
- **프라이버시 경계**: 사용량 수치만 저장, PII 미저장. 네트워크는 옵트인(기본 off) — "전 과정 로컬" 기본값 보존.

## 13. 후속 / backlog

- **이벤트 "남기면 아까움"**: 이벤트 크레딧(9/10 만료)은 너무 천천히 쓰면 만료로 소멸 → "만료 전 소진 가능?
  잔여 권장 페이스" 양방향 신호. 본 spec은 소진 예측만, 양방향은 후속.
- **일반 사용량(general) 가시화**: 크레딧·월할당 소진 후 차감되는 org 레벨 사용량. 개인 게이지 범위 밖, 후속 검토.
- **Codex 라벨 매핑 불일치**: API `plan_type:business` ↔ CLI `/status` "Enterprise". 표시 라벨 정책 후속.
- **Claude 5h/7d 창(개인)의 예측**: 롤링 창이라 D-day 의미 약함 → 현재값만. 필요 시 후속.
