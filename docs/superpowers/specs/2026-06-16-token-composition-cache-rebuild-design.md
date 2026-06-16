# 토큰 구성비 + 캐시 재구축 신호 설계

- 날짜: 2026-06-16
- 상태: 설계 승인 대기 → 구현
- 관련: v0.2.0 "미노출 raw 필드 노출" 마일스톤,
  `aggregate.py:_range_rows`(134)·`by_dimension`/`DimensionRow`(488·500)·`by_day_session`(423, `cache_miss`)·`insights`(636),
  `web/views.py:dimension_context`(131)·`overview_context`(35), `web/templates/analysis.html`·`overview.html`
- 선행 메모: 1h 캐시 프리미엄 작업은 **보류**(액션 불가·오해 소지 — `memory/metric-selection-actionability.md`). 본 작업은
  같은 v0.2.0 인벤토리에서 "사용자가 개선 가능 + 오해 없음" 기준을 통과한 후보로 전환한 것.

## 1. 배경 및 문제

`messages`에는 `input_tokens`·`output_tokens`·`cache_creation`·`cache_read`가 모두 적재된다. 그러나 화면은
**"캐시%"(= cache_read 비중) 단일 지표로만 압축**한다. 구체적으로:
- **`cache_creation`(캐시 생성 = 비싼 토큰, 단가 1.25~2×)** 은 analysis 차원 테이블에서도 빠져 있다
  (현재 input/output/cache_rd 3칸만, cache_wr 누락).
- **전역 토큰 구성**(이 머신: cache_read 94.2% / cache_creation 4.8% / output 0.9% / input 0.14%)은 어디에도 없다.
- "이어지는 세션인데 캐시를 못 읽고 새로 만드는"(prompt caching이 깨지는) 패턴은 `by_day_session`의
  **`cache_miss`** 로 이미 계산돼 `/history` 트리에 ⚠로만 표시되고, 한눈에 보이는 요약이 없다.

스키마·파서 무변경으로 표현 계층만 보강하면 노출 가능해 ROI가 높다(1h 캐시와 달리 마이그레이션 불필요).

## 2. 목표 / 비목표

### 목표
- **차원별(analysis) `cache_wr` 칸 추가** — input/output/**cache_wr**/cache_rd 4분할 완성. `DimensionRow.cache_creation`은
  이미 집계되므로 **노출만**. 어느 모델/스킬/브랜치가 비싼 캐시 생성을 유발하는지 식별(액션 가능).
- **전역 토큰 구성 미니바(오버뷰)** — 4분할 토큰 비중을 한눈에. **반드시 "토큰량 기준" 라벨 + 비용≠토큰
  주석**(§5)으로 오해 차단.
- **캐시 재구축 신호(오버뷰 insight)** — `by_day_session`의 `cache_miss=True` 세션 수를 정보 카드로 승격.
  "이어지는 세션 N개에서 컨텍스트 재빌드 — 세션 유지로 개선 여지". 첫 등장일은 정의상 제외돼 오해 없음.

### 비목표 (이번 작업 범위 밖)
- **토큰 종류별 비용 분해**(output 비용 vs cache_read 비용 등) — 정확하려면 ingest 시점 분해 저장 또는
  집계 시 pricing 재계산(계층 침범)이 필요. 1h에서 피한 복잡도를 다시 부르므로 제외. 토큰 구성은 **토큰량 기준**으로만,
  비용은 기존 `cost_usd` 총액으로 별도 표기.
- **1h/5m 캐시 구분 노출** — 보류된 작업(위 선행 메모).
- **세션 상세 토큰 구성** — YAGNI(차원/오버뷰로 충분).

## 3. 설계

### 3.1 집계 — `aggregate.py`

- **전역 구성** — `token_composition(conn, provider, start, nxt) -> TokenComposition` 신설.
  **자체 SELECT**(주의: `_range_rows`는 `output_tokens`를 select하지 않아 재사용 불가)로
  `input_tokens`·`output_tokens`·`cache_creation`·`cache_read`·`ts`를 읽어 `[start,nxt)` 필터·합산.
  비중은 **0~100 퍼센트값**(예: 94.2 = `round(x/total*100,1)` — 0.942 아님, 템플릿 바 width와 직결).
  반환 dataclass: 4개 합계 + 4개 비중. **`cost_usd`는 담지 않는다**(토큰량 바 옆에 비용을 두면 비용%로 오해 — §5).
  순수 함수, pricing 비의존(계층 유지).
- **차원별** — `by_dimension`/`DimensionRow`는 `cache_creation`을 **이미 집계**(489·526·532)한다. 변경 없음 — views/템플릿에서 꺼내 쓰기만.
- **캐시 재구축 수** — `by_day_session`(423)이 행마다 `cache_miss`(= `is_continued and cache_ratio < INSIGHT_CACHE_READ_MIN`)를
  이미 매긴다. `insights` 내부에서 `by_day_session(conn, provider, start=month_start, nxt=month_nxt)`를
  **달력 월**(`month_bounds(now_kst)` — 기존 insights의 `_month_rows`와 동일 기준; budget_start 미적용은
  "코치는 달력 월" 설계와 일치)로 호출하고, `cache_miss=True` 행의 **고유 `session_id` 수**를 센다
  (세션×날짜 행 수 아님 — 같은 세션이 N일 miss해도 1). 오버뷰는 combined(provider=None)라 `first_day`가
  전 provider MIN(ts) 기준이어도 무해. `insights` 시그니처 무변경. 비용: `by_day_session` full-scan 1회 추가
  (PoC 규모 ~16k 체감 미미 · 규모 시 경량화 후보).

### 3.2 화면 — `views.py` + 템플릿

- **`dimension_context`**(131): `table` dict는 **이미 `cache_creation`을 담고 있다**(147) → views 변경 불필요, 템플릿만 칸 추가.
- **`analysis.html`**: 테이블 헤더 `cache_rd` 앞에 `cache_wr` 칸 추가(8→9칸), 행에 `{{ '{:,}'.format(m.cache_creation) }}`.
  colspan 빈 상태 문구 9로.
- **`overview_context`**(35): `token_composition(conn, None, *month_bounds(now))` 호출 → 컨텍스트에 4분할(합계+비중) 전달.
  재구축 카운트는 `insights` 내부에서 처리(§3.1) → overview_context는 별도 조립 불필요.
- **`overview.html`**: 토큰 구성 **미니 스택바**(4색: input/output/cache_wr/cache_rd) + 비중 수치(퍼센트값).
  **헤더에 "토큰량 기준" 명시**, 바 아래 디스클레이머(§5). **바에는 토큰량만 — `cost_usd`를 바에 붙이지 않는다**(오해 방지).
  기존 `.bar`/`.fill` 컴포넌트 재사용 → CSS 무빌드.

## 4. 데이터 흐름

```
messages (input/output/cache_creation/cache_read, ts, cost_usd, session_id, project)
   │
   ├ token_composition ──→ 오버뷰: 전역 4분할 미니바(토큰량 기준 + 비용≠토큰 주석)
   ├ by_dimension ───────→ analysis: 차원별 4분할 테이블(cache_wr 칸 추가)
   └ by_day_session.cache_miss ──→ insights: "캐시 재구축 N개 세션" 카드(고유 session_id, 첫 등장 제외)
```

## 5. 엣지 케이스 & 에러 처리

- **토큰% ≠ 비용% (핵심 주의)**: `cache_read`는 토큰 비중 94%지만 단가 0.1×라 비용 비중은 ~절반 수준이고,
  `output`은 토큰 0.9%지만 단가가 높아 비용 비중이 큼. 전역 구성 미니바는 **"토큰량 기준"** 임을 라벨로 명시하고,
  바 아래에 "비중은 토큰 수 기준 — 비용 비중과 다름(캐시 읽기 단가 0.1×, 출력 단가 높음)" 디스클레이머를 단다.
  비용은 기존 번다운/세션 카드의 `cost_usd`로 본다(중복 분해하지 않음).
- **토큰 0 기간/세션**: 분모 0 → 비중 0.0, 미니바 빈 상태. 회귀 아님.
- **캐시 재구축 오해 차단**: 첫 등장일 세션은 캐시 생성이 당연하므로 `cache_miss` 정의가 이미 제외(`is_continued`).
  카드는 `warn`이 아니라 정보/개선여지 톤(액션: 세션 유지·컨텍스트 안정화). 0건이면 카드 생략.
- **재구축 카운트 단위**: `cache_miss` 행은 (날짜×세션)이므로 **고유 `session_id`로 집계**(같은 세션 N일 miss → 1세션). "N개 세션" 표기와 일치.
- **비중 단위**: `TokenComposition` 비중은 **0~100 퍼센트값**(94.2). 템플릿은 그대로 출력(추가 ×100 금지) — 기존 `cache_ratio`(0~1, 템플릿서 ×100)와 단위가 달라 혼동 주의. 테스트로 고정(§6).
- **provider 필터**: 전역 구성·오버뷰 재구축은 전 AI 합산(provider=None). `by_day_session.first_day`가 전 provider 기준이나 combined라 무해.
- **Codex**: `cache_creation`=0(캐시 쓰기 구분 없음, codex_parser). 구성에서 자연히 0으로 표시 — 오류 아님.

## 6. 테스트

- `tests/test_aggregate.py`:
  - `token_composition` 4분할 합계·비중 정확(기간 `[start,nxt)` 필터), **비중이 퍼센트값(94.2)인지** 단위 고정, 토큰 0 분기.
  - `by_dimension`이 `cache_creation`을 차원별로 정확 합산(회귀 가드).
  - **캐시 재구축 카운트**: ① 첫 등장일 세션 제외(`is_continued=False`), ② 같은 세션 N일 miss → **1세션**(고유 session_id), ③ KST 월 경계(UTC ts→KST) 정확, ④ 달력 월 기준(budget_start 무관).
- `tests/test_web.py`:
  - `/analysis` 테이블에 `cache_wr` 칸 렌더(헤더+값).
  - 오버뷰에 토큰 구성 미니바 + "토큰량 기준" 라벨 렌더.
  - `cache_miss` 세션이 있을 때 오버뷰 insight 카드 노출, 없을 때 생략.

## 7. 향후 작업 (범위 밖, 참고)

- **토큰 종류별 비용 분해** — 비용%까지 정확히 보려면 ingest 시점 분해 저장 설계가 필요(별도 spec).
- **세션 상세 토큰 구성** — `session_detail`에 미니바 추가(자연 확장).
- **캐시 효율 추세** — cache_read 비중 시계열(통합 추세 차트 패턴 재사용).

## 8. 영향 받는 파일

| 파일 | 변경 |
|---|---|
| `tokenomy/aggregate.py` | `token_composition`+`TokenComposition` 신설, `insights`에 재구축 카드(cache_miss 수) |
| `tokenomy/web/views.py` | `overview_context`에 토큰 구성+재구축 신호 (`dimension_context`는 무변경 — `cache_creation` 이미 포함) |
| `tokenomy/web/templates/analysis.html` | `cache_wr` 칸(헤더+값), colspan 9 |
| `tokenomy/web/templates/overview.html` | 토큰 구성 미니 스택바 + "토큰량 기준" 라벨·디스클레이머 |
| `tests/test_aggregate.py`, `test_web.py` | §6 |
