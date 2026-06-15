# 예산 주기 차별화 · 도입일 · 임의 구간 조회 — 설계

작성일: 2026-06-15
상태: 설계 승인 대기

## 배경 / 문제

현재 Tokenomy는 모든 번다운·조회를 **달력 월(1일~말일)** 단일 모델로 처리한다. 실제 운영에서 세 가지가 어긋난다.

1. **예산 도입일** — 조직 예산이 2026-06-12부터 공식 도입됐다. 6/1~6/11 지출이 6월 예산에 섞여 들어가 일평균·예상 월말을 부풀린다.
2. **provider별 예산 주기 차이** — Claude는 월간 한도(월말까지)이지만, Codex는 **월 한도를 4로 나눈 금액이 주간 한도**이고 매주 초(월요일) 새로 설정된다. 못 쓴 크레딧은 다음 주로 **이월·누적**되고, **월이 바뀌면 소멸**한다.
3. **임의 구간 조회** — 월 중간~말일 등 특정 구간 사용량을 보고 싶은 경우가 있다.

세 요구는 결국 **"번다운/조회의 기간 모델을 일반화"** 한 축으로 수렴한다.

## 구현 접근 (확정: A — 설정 최소화 + 규칙은 코드)

config에는 provider별 월 한도 + 전역 `budget_start`(도입일)만 둔다. "Codex 주간 한도 = 월한도÷4, 월 내 누적 carryover, 월요일 주 경계"는 코드(`budget.py`/`aggregate.py`)에 담는다. 정책이 사실상 고정이므로 `cycle`/`divisor`/`carryover` 같은 메타를 설정으로 빼지 않는다(YAGNI). 정책이 실제로 바뀌면 그때 일반화한다.

## 1. 설정 (config 스키마)

`tokenomy.config.json`에 도입일 한 필드 추가.

```json
{
  "budget": { "claude": 200.0, "codex": 40.0 },
  "budget_start": "2026-06-12"
}
```

- `budget_start`는 **provider 공통**(조직 도입). 일회성이라 첫 달만 실제 영향.
- 미설정/빈 문자열이면 **완전 하위호환** — 기존처럼 달력 월 1일 기준.
- 형식은 `YYYY-MM-DD`(KST). 파싱 실패/빈값은 미설정으로 취급.
- Settings 페이지에 날짜 입력 필드 + 저장 추가.

## 2. 번다운 모델 (핵심)

`_compute_burndown`을 **"기간 시작(effective_start) + 기간 총일수 + 기간 내 경과일"을 받는 순수 함수**로 일반화하고, provider별로 다르게 호출한다.

### 공통 정의

- KST 기준 이번 달 `[month_start, month_end)`.
- `effective_start = max(month_start, budget_start)` — budget_start가 이번 달에 속할 때만 당겨지고, 아니면 `month_start`.

### Claude (월간, 도입일 clamp)

- spent = `effective_start ~ 오늘` 누적 지출
- limit = 월 한도 전액
- 경과일·남은일·일평균·예상 월말을 **effective_start 기준**으로 계산
  - `days_in_period = (month_end - effective_start).days`
  - `day_of_period = (오늘 자정 - effective_start).days + 1`
  - `days_left = days_in_period - day_of_period`
  - `daily_avg = spent / day_of_period`
  - `projected = daily_avg * days_in_period`
- 6월 예: 기간 = 6/12~6/30(19일), 일평균 = spent / (6/12~오늘 경과일).
- status(ok/warn/exceeds)·exhaust_day 로직은 기존과 동일하되 위 기간 값을 사용.

### Codex (주간 누적 carryover)

- `W = codex_월한도 ÷ 4` (주간 충전액)
- `N` = effective_start가 속한 주를 1주차로 하여, **오늘까지 지난 주 시작(월요일) 횟수**
  - effective_start가 속한 주의 월요일 = `mon0`
  - 오늘이 속한 주의 월요일 = `mon_now`
  - `N = (mon_now - mon0).days // 7 + 1`
  - 오늘은 항상 이번 달에 속하므로 N은 effective_start 주(1주차)부터 오늘 주까지의 주 수. `mon0`이 전월일 수 있으나(예: 7/1=수 → mon0=6/29) effective_start가 month_start로 clamp되므로 1주차 기준점은 일관된다. 다음 달로 넘어가는 주는 오늘이 그 주에 도달했을 때 새 달의 1주차로 재시작한다(월 리셋).
- **분모(누적 충전 한도) `limit_to_date = W × N`**
- **분자 `spent` = effective_start ~ 오늘 누적 지출**(이번 달 내)
- **이번 주 가용 잔액 = limit_to_date − spent** (못 쓴 만큼 자동 이월되는 효과)
- `pct = spent / limit_to_date`
- 월이 바뀌면 분자·분모 모두 리셋(이월 소멸)
- **예상 월말은 산출하지 않는다** — 주간 리셋+이월이라 월말 투영이 부정확. Codex 카드는 "이번 주 가용/소진"에 집중.

#### Codex 예시 (월한도 $40, W=$10, 6월, budget_start 6/12)

2026-06-01이 월요일이라 6월은 주/월 경계가 정렬된다(1주차 6/1~7, 2주차 6/8~14, 3주차 6/15~21, 4주차 6/22~28, 5주차 6/29~7/5).

- 도입 6/12 → 2주차. 1주차 충전 없음(effective_start가 2주차).
- 오늘 6/15 → 3주차. `N = 2`(2·3주차) → 분모 = `W×2 = $20`.
- 누적 지출 $6이면 → **이번 주(3주차) 가용 = $20 − $6 = $14** (3주차 새 $10 + 2주차 미사용 $4 이월), pct 30%.

## 3. 대시보드 구조

- 기존 거대한 "통합 번다운 단일 바" → **provider별 카드 2개를 주인공으로 승격.**
  - **Claude 카드**: 이번 달 지출 / 월 한도 · 예상 월말(도입일 기준) · status
  - **Codex 카드**: 이번 주(N주차) 지출 / 누적 한도(W×N) · "이번 주 $X 남음(이월 $Y 포함)" · status
- 기존 통합 바는 **"이번 달 총지출" 금액 요약으로 격하**: `이번 달 전 AI 총지출 $X (Claude $a + Codex $b)`. 한도 비율 합산(의미 약함)은 제거하고 금액 정보만 유지.
- 추세 차트 · 효율 코치 · 프로젝트/세션 Top은 유지(전 AI 합산, 이번 달 기준).

## 4. 내역 / 모델별 — 구간 조회

기간 컨트롤을 통일한다.

```
[‹ 이전]  [ 주간 | 월간 ]  6/15 ~ 6/21  [다음 ›]    [사용자 지정 ▾  from▢ to▢]
```

- `period_bounds(period, anchor)`(이미 존재, 주=월요일 시작) 재활용.
- 라우트 파라미터:
  - `period` ∈ {`week`, `month`} — 화이트리스트, 기본 `month`.
  - 사용자 지정 시 `start`/`end`(`YYYY-MM-DD`) — 둘 다 있고 유효하며 `start ≤ end`일 때만 적용. `[start, end+1day)`로 변환.
  - 검증 실패 시 안전 폴백(기존 anchor 월).
- 집계 계층(`by_day_session`/`by_model`/`by_project`/`by_session`)은 이미 임의 `[start, nxt)`를 지원 → **신규 집계 코드 거의 불필요**.
- 라우트는 얇게(검증만), 기간 해석·조립은 views.

## 5. Edge cases

- **월 경계 걸친 주**(예: 6/29~7/5): 지출은 KST 날짜의 월로 귀속, 주차 카운트는 월 내 월요일 기준. 6/29~30 = 6월 5주차, 7/1~ = 7월 새 1주차. 월에서 칼같이 분리(월 리셋).
- **1일이 월요일 아닌 달**(예: 7/1=수): Codex 1주차 = 7/1~7/5(부분주, **풀 W**), 이후 매 월요일 충전. `mon0`은 7/1이 속한 주 월요일(6/29)이지만 effective_start(7/1)가 속한 주를 1주차로 카운트.
- **budget_start 미설정 / 과거 달**: 기존 동작, 영향 없음.
- **budget_start가 미래**: 해당 월 도달 전에는 미설정과 동일하게 취급(추적만).
- **한도 0(추적 전용)**: 현재처럼 사용량만 표시, 비율/예상 생략.

## 6. 코드 변경 위치 (계층 분리 유지)

- `budget.py` — config `budget_start` 파싱, `W = 월한도 ÷ 4` 헬퍼.
- `aggregate.py` — `_compute_burndown` 파라미터화(기간 시작/총일수/경과일), `codex_burndown`(주간 누적) 신규, 주차 카운트(`N`) 헬퍼.
- `views.py` — `overview_context`(provider별 카드 주기 반영 + 총지출 요약), `history_context`/`models_context`(period/custom range).
- `app.py` — 라우트에 `period`/`start`/`end` 파라미터 검증 추가(얇게).
- `templates/` — `overview.html`(카드), `_history_body.html`·`models.html`(기간 토글+date picker), `settings.html`(도입일 입력).
- 스타일 변경 시 `build_css.ps1` 재빌드 후 `static/app.css` 커밋.

## 7. 테스트

`now_kst`/`budget_start`를 주입해 **결정적**으로 검증(2026-06은 6/1=월이라 경계가 정렬돼 케이스 작성이 깔끔).

- Codex carryover: 주차별 분모(W×N)/분자/이번주 가용. 경계 — 도입 주, 월 첫 부분주(7/1), 월 경계 걸친 주(6/29~7/5).
- Claude 도입일 clamp: effective_start 기준 경과일·일평균·예상 월말.
- 통합 "이번 달 총지출" 금액 = Claude + Codex 합.
- 내역/모델별 custom range·주/월 토글 집계.
- `budget_start` 미설정 시 기존 동작 회귀(하위호환).

## 비범위 (YAGNI)

- provider별 정책 메타(`cycle`/`divisor`/`carryover`)의 설정화.
- 주간 외 임의 주기(격주/분기 등).
- 예산 도입일의 provider별 분리(현재 조직 공통 단일 값).
- Codex 월말 예측(주간 모델에 부정확).
