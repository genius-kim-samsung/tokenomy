# 전체 현황 대시보드 탭 설계

- 날짜: 2026-06-12
- 상태: 설계 승인 (구현 계획 대기)

## 배경 / 문제

현재 Tokenomy 웹 대시보드는 단일 페이지(`/`) 안의 **provider 토글**(`/?provider=claude` ↔
`/?provider=codex`)로 한 번에 한 AI만 보여준다. 여러 AI를 함께 쓰는 사용자는 "이번 달 전체
지출이 한도 안인가?"를 한눈에 볼 수 없고, AI를 번갈아 토글해 머릿속으로 합산해야 한다.

**목표:** `전체 · Claude · Codex` 상단 탭바를 신설하고, **전체** 탭을 기본 메인 화면으로
삼아 전 AI를 합산한 "관제탑" 뷰를 제공한다. Claude/Codex 탭은 기존 상세 화면을 유지한다.

핵심 제약: 사용자가 "클로드, 코덱스 **등** 여러 AI"라고 명시 → 구조는 N-provider로 확장
가능해야 한다(단 이번 구현은 Claude+Codex만 실제 동작, 아래 "확장성" 참조).

## 비목표 (이번 범위 아님)

- `Budget` 모델의 dict 일반화(provider→한도 맵)와 설정 화면 동적 생성. → 향후 3번째
  provider 추가 시점에 별도 작업.
- 새 파서 추가(예: Gemini CLI). DB는 이미 provider 무관이지만 파서는 별도.
- 기간 선택(월 외), provider별 예산 외 추가 설정.

## 데이터 기반 (현황 확인)

- `messages.provider` / `sessions.provider`는 임의 문자열. DB는 이미 provider 무관이며
  `idx_messages_provider_ts` 인덱스 존재 → 합산 쿼리에 추가 스키마 변경 불필요.
- `aggregate.py`의 집계 함수(`by_project`, `by_session`, `daily_series`, `insights`)는
  전부 내부적으로 `_month_rows(conn, provider, now)`를 거친다. 따라서 `_month_rows`가
  "전체"를 지원하면 나머지가 자동으로 합산을 지원한다.
- `burndown()`은 provider 인자로 한 AI를 필터·산술. provider별 카드는 이를 루프로 돌려
  자연스럽게 얻는다.
- `Budget`은 `claude`/`codex` 필드 하드코딩(`limit_for(provider)` 포함). 이번엔 유지.

## 구현 접근법

**A안 — 기존 집계 함수에 `provider=None`(=전체) 지원 + 얇은 overview 조립부 〔채택〕**

`_month_rows()`에서 `provider=None`이면 `WHERE provider=?` 필터를 빼면, 그 위 집계 함수들이
그대로 전 AI 합산이 된다. AI별 카드는 provider별 `burndown()` 루프로 얻는다. 변경 최소,
검증된 로직 재사용.

대안 B(순수 합성)는 Top-N 프로젝트/세션을 provider별 Top-N의 합으로 만들 수 없어(전체 리스트
재정렬 필요) 병합 코드가 늘고, 대안 C(GROUP BY 신규 SQL)는 기존 "Python 필터" PoC 스타일과
어긋나 로직이 중복된다. → A안 채택.

## 설계

### 1. 라우팅 / 탭

| 경로 | 화면 | 템플릿 |
| --- | --- | --- |
| `/` (provider 파라미터 없음) | **전체(overview)** — 기본 메인 | `overview.html` |
| `/?provider=claude` | Claude 상세 | `dashboard.html` |
| `/?provider=codex` | Codex 상세 | `dashboard.html` |

- 상단 탭바(`전체 · Claude · Codex`)는 공유 partial `_tabs.html`로 분리 →
  세 화면이 동일한 헤더(로고·데이터 최신·⚙ 설정·↻ 새로고침) + 탭 + 활성 표시를 공유.
- 기존 번다운 카드 안의 `[Claude][Codex]` 토글은 상단 탭바와 중복이므로 **제거**.
  프로젝트 정렬 토글(cost/sessions/cache)은 유지.
- `↻ 새로고침`(POST `/ingest`) 후 리다이렉트 대상은 `/`(전체).

### 2. 전체 탭 구성 (위 → 아래)

1. **통합 번다운 바**
   - 한도 = `Σ(한도>0인 provider의 예산)`
   - 지출 = `Σ(그 한도 있는 provider의 이번 달 지출)` — 분자/분모 범위를 일치시킨다.
   - 라벨에 `(한도 설정한 AI 합산)`을 명시해 범위를 분명히 한다.
   - 일평균·예상 월말·소진 예상일·상태(OK/초과예상/초과)는 기존 `burndown()` 산술과 동일.
2. **AI별 카드** — `PROVIDERS`의 각 provider마다 `burndown()` 결과를 카드로.
   - 한도 있음 → `$지출/$한도 · % · 상태 · 예상월말`
   - 한도 없음(0) → `사용량만($지출)`
   - 데이터 없음 → `(이 머신에 ○○ 로그 없음)`
3. **통합 추세 그래프** — 전 AI 합산 일별 누적(`실제`) + 예산 페이스 라인(통합 한도 기준,
   한도 0이면 페이스 라인 숨김). Chart.js, 기존 trend와 동일 스타일.
4. **통합 효율 코치** — 전 AI 합산 신호(캐시 활용·단가 미식별·한도 초과 예상).
5. **통합 프로젝트별 비용 Top 10** — provider 무관 합산. cost/sessions/cache 정렬
   토글 적용 후 상위 10개. (전체 목록은 각 provider 상세 탭에서.)
6. **최근 비싼 세션 Top 10** — provider 무관, 비용순 상위 10개. 세션 상세
   (`/session/{id}`)로 이동.

### 3. 컴포넌트 변경

**`tokenomy/aggregate.py`**
- `_month_rows(conn, provider, now)`: `provider=None`이면 전체(조건부 WHERE — 필터 생략).
- burndown 산술을 순수 헬퍼 `_compute_burndown(spent, limit, unpriced, now, provider_label)`로
  추출 → `burndown()`과 신규 `combined_burndown()`이 공유(중복 제거).
- `combined_burndown(cards, now)`: 입력은 provider별 `Burndown` 리스트.
  - 한도 있는(`limit>0`) 카드만 spent·limit·unpriced 합산 후 `_compute_burndown` 호출.
  - 한도 있는 카드가 하나도 없으면 limit=0(사용량만, spent=전 AI 합산).
- 중앙 상수 `PROVIDERS = ("claude", "codex")` 신설. (확장 시 이 한 곳 + `Budget` 필드 +
  파서 + 단가만 추가하면 overview/탭바가 자동 반영되도록 설계.)

**`tokenomy/web/views.py`**
- `overview_context(conn, sort, now_kst=None)` 신설:
  - `cards = [burndown(conn, budget, now, p) for p in PROVIDERS]`
  - `combined = combined_burndown(cards, now)`
  - `projects = by_project(conn, None, now)` → `sort` 정렬 → 상위 10개(`[:10]`)
  - `sessions = by_session(conn, None, now, limit_n=10)`
  - `daily = daily_series(conn, None, now)`; `pace`는 `combined.limit` 기준
  - `insights = insights(conn, combined, now, None)`
  - `last_ts = MAX(ts)` (전체), `has_data`
- `dashboard_context`는 시그니처/로직 유지 + 활성탭 컨텍스트(`active_tab="claude"|"codex"`)만 추가.

**`tokenomy/web/app.py`**
- `PROVIDERS`를 aggregate에서 import.
- `GET /`: `provider`가 `PROVIDERS`에 있으면 `dashboard.html`(detail), 아니면(없음/기타)
  `overview.html`. `notice`·`update_tag`는 두 화면 공통 전달.
- `POST /ingest` 리다이렉트는 `/`.

**템플릿**
- `_tabs.html` 신설: 공유 헤더 + 탭바(활성 표시 `active_tab`).
- `overview.html` 신설: `_tabs.html` include + 위 2절 구성.
- `dashboard.html`: 상단 `_tabs.html` include 추가, 번다운 카드 내 provider 토글 제거.
- `settings.html`: `← 대시보드` 링크 그대로(`/`=전체).

**`tokenomy/budget.py`**
- 변경 없음(구조만 준비 결정 반영). overview는 `budget.limit_for(p)`로 provider별 한도를 읽음.

### 4. 데이터 흐름 (전체 탭)

```
GET /  (provider 없음)
  └→ app.dashboard → overview_context(conn, sort)
       ├ cards   = [burndown(conn, budget, now, p) for p in PROVIDERS]
       ├ combined= combined_burndown(cards, now)        # 한도 있는 것만 합산
       ├ projects= by_project(conn, None, now) → sort → [:10]
       ├ sessions= by_session(conn, None, now, 10)
       ├ daily   = daily_series(conn, None, now); pace ← combined.limit
       └ insights= insights(conn, combined, now, None)
     → render overview.html (_tabs active=전체)
```

### 5. 엣지 케이스

- **데이터 전무**: `데이터 없음 · [↻ 새로고침]` 안내(기존 동작 준용).
- **예산 전무**(전 provider 한도 0): 통합은 사용량만 — 바·% 없이 `지출 $X` + `예산 설정`
  안내, 추세 페이스 라인 숨김.
- **혼합**(한쪽만 한도): 통합 바는 한도 있는 provider만 합산(라벨 명시). 한도 없는 provider는
  카드에서 사용량만. 추세/프로젝트/세션 표는 전 AI 합산. → **둘 다 한도 거는 일반 케이스에선
  통합 바와 추세가 완전 일치.**
- **단가 미식별**: 통합 unpriced_count = 한도 있는 provider 합. 배지로 표시.
- **한 provider만 로그 존재**(예: Codex 로그 없음): 해당 카드 `(이 머신에 Codex 로그 없음)`,
  통합은 나머지로 정상 동작.

### 6. 테스트 전략

- `tests` (aggregate):
  - `_month_rows(conn, None, now)`가 전 provider 행 반환.
  - `combined_burndown`: 둘 다 한도(합산 정확) / 예산 전무(사용량만) / 혼합(한도 있는 것만).
  - 기존 per-provider 집계 테스트 무회귀(시그니처 유지).
- `tests` (views): `overview_context`가 PROVIDERS 카드 + 통합 bd/추세/프로젝트/세션 반환.
- `tests` (app/web): `GET /` → overview(200, 탭바·"통합" 문자열 포함),
  `GET /?provider=claude` → detail, `POST /ingest` → `/` 리다이렉트.

## 확장성 메모 (3번째 AI 추가 시)

1. 파서 모듈 작성(README "Adding a parser for another tool") + ingest 연결.
2. `aggregate.PROVIDERS`에 provider 키 추가.
3. `Budget`에 해당 필드 + `limit_for`/`total` 갱신(또는 이 시점에 dict 일반화).
4. `config/pricing.json`에 단가 추가, 설정 화면에 입력 필드 추가.
→ overview·탭바·통합 집계는 (1)(2)만으로 자동 반영(루프·`provider=None` 기반).
```
