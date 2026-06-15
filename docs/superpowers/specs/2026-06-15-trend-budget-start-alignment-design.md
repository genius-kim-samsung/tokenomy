# 통합 추세 그래프 — 예산 도입일 정합 · 말일 확장 · 월 예산 가로선 — 설계

작성일: 2026-06-15
상태: 설계 승인 대기

## 배경 / 문제

예산 주기 차별화 기능(2026-06-15, `budget_cycle-and-date-range`)에서 번다운 카드는
`budget_start`(예산 도입일)를 반영하도록 일반화됐다. 그러나 대시보드의 **통합 추세
그래프**는 그때 의도적으로 손대지 않았고(`views.py`의 "추세는 전 AI 합산·달력 월 기준
유지" 주석), 그 결과 카드와 그래프가 어긋난다.

도입일을 6/12로 설정했을 때:

1. **도입일 미반영** — 번다운 카드는 6/12부터 합산하지만, 추세 그래프는 달력 월
   (6/1~7/1) 고정이라 6/1~6/11 지출까지 누적선에 포함된다. 카드와 그래프가 불일치.
2. **페이스선 기울기 왜곡** — 페이스선은 `claude_bd.limit / claude_bd.days_in_month
   * p.day`인데, `days_in_month`는 이미 clamp된 기간 길이(6/12~7/1 = 19일)이지만
   `p.day`는 달력 날짜(1~15)라 분자/분모 기준이 어긋나 기울기가 틀어진다.
3. **x축이 오늘까지만** — 그래프가 오늘 날짜에서 끝나 예산 페이스선이 말일까지
   보이지 않는다.
4. **월 예산 한도를 시각적으로 알 수 없음** — 한도 높이를 나타내는 기준선이 없다.

또한 기존 페이스선은 **Claude 한도만** 쓰는데, 추세의 실제선은 **전 AI 합산**
(Claude+Codex)이라 분자(실제=전 AI)와 기준(페이스=Claude만)이 처음부터 어긋나 있었다.
이번에 가로선을 추가하는 김에 이 기준을 통합 예산으로 바로잡는다.

## 구현 접근 (확정: A — `daily_series`가 `budget_start`를 받아 내부 clamp)

추세 그래프는 번다운 카드의 **시각적 쌍**이다. 둘 다 `budget_start`로 파라미터화해
항상 같은 기간을 쓰도록 묶는다. 카드의 `burndown(...)`이 이미 이 패턴(`budget_start`
인자 → 내부에서 `effective_month_start` 호출)이므로 `daily_series`도 동일하게 맞춘다.
`budget_start=None` 기본값으로 완전 하위호환.

> 대안 B(views가 명시적 `start`/`nxt` 범위를 계산해 전달, `by_*` 패밀리 패턴)는
> 테스트 격리에 약간 유리하나, 카드와 추세가 서로 다른 인자 패턴이 되고 기간 계산이
> 두 곳으로 분산되어 채택하지 않는다.

## 변경 1 — `aggregate.daily_series`: 도입일 clamp + 말일까지 확장

```python
def daily_series(conn, provider, now_kst, *, budget_start=None):
    period_start = effective_month_start(now_kst, budget_start)   # 예: 6/12 (미설정 시 6/1)
    _, period_end = month_bounds(now_kst)                         # 7/1
    last_day = (period_end - timedelta(days=1)).day               # 30 (말일)
    rows = _range_rows(conn, provider, period_start, period_end)  # 6/12~ 만 합산
    per_day = {}                                                  # day → 비용 합
    for r in rows:
        dt = parse_ts(r["ts"])
        if dt:
            per_day[dt.day] = per_day.get(dt.day, 0.0) + (r["cost_usd"] or 0)
    out = []
    cumulative = 0.0
    for d in range(period_start.day, last_day + 1):   # 12..30
        if d <= now_kst.day:                          # 오늘까지만 실제 누적값
            cumulative += per_day.get(d, 0.0)
            out.append(DayPoint(day=d, cumulative_cost=round(cumulative, 4)))
        else:
            out.append(DayPoint(day=d, cumulative_cost=None))   # 미래 → null (선 끊김)
    return out
```

- `DayPoint.cumulative_cost` 타입을 `float | None`로 확장. 미래 구간은 `None`
  (JSON `null` → Chart.js `spanGaps:false`로 실제선이 거기서 끊김).
- x축이 기간 전체(12~30)를 덮는다. `_range_rows`가 `period_start` 이전 행을 제외하므로
  6/1~6/11 지출은 누적에서 빠진다.
- 기존 시그니처(`daily_series(conn, provider, now)`)는 `budget_start=None` 기본으로
  호출부·테스트 하위호환. 단 반환 형태가 "오늘까지"→"말일까지(+None 꼬리)"로 바뀌므로
  기존 테스트 `test_daily_series_cumulative`는 갱신한다.

## 변경 2 — `views.overview_context`: 통합 예산 기준 페이스 + 가로선

```python
daily = daily_series(conn, None, now, budget_start=bs)   # bs 전달 추가
period_days = len(daily)                                  # 기간 일수(=clamp된 19일)
limit = budget.total                                      # ★ Claude+Codex 통합 월 예산
...
"daily_labels": [p.day for p in daily],                          # 12..30 (달력 날짜)
"daily_actual": [p.cumulative_cost for p in daily],              # 실제(오늘 이후 null)
"daily_pace":   [round(limit / period_days * (i + 1), 4) if limit else 0.0
                 for i, p in enumerate(daily)],                  # 0→limit, 말일에 limit
"daily_budget": [limit if limit else 0.0 for p in daily],       # ★ 가로선(NEW)
```

- **기준 예산 = `budget.total`(Claude + Codex 월 예산).** Codex 월 예산은 정해진 값이고
  그걸 주별로 나눠 쓰는 것이므로, `budget.total`은 근사치가 아니라 정확한 통합 월 예산이다.
  실제선(전 AI 합산)과 분자/기준이 일치하고, 기존 "Claude만 쓰던 페이스" 불일치도 해소.
- 페이스선 말일값 = `limit / period_days * period_days = limit` → **가로선과 말일에서
  수렴**. 페이스선=도달 목표 대각선, 가로선=한도 천장, 둘이 말일에서 만난다.
- 분모를 `len(daily)`로 써서 `claude_bd`(이름이 Claude 한정처럼 보임) 의존을 제거한다.
  `len(daily)` = clamp된 기간 일수와 동일.
- 인덱스를 `enumerate`로 잡아 일자 연속성에 의존하지 않는다(첫 점=1일차).

> **페이스 0-앵커 안 함:** 첫 점(12일)의 페이스 = `limit/19 * 1`(1일치 예산)로,
> 정확히 (12, 0)에서 시작하지는 않는다. 이는 기존 코드의 "end-of-day = d일치 예산"
> 관례를 그대로 유지한 것이다(실제선도 12일 점 = 12일 종료시점 누적). 누적 기준선이
> 6/12에서 0으로 리셋된다는 의미이지, 리터럴 0 점을 추가하지는 않는다.

## 변경 3 — 템플릿 `_trend_chart.html`: 데이터셋 1개 추가

```js
const trendBudget = {{ daily_budget|tojson }};
new Chart(..., {
  data: { labels: trendLabels, datasets: [
    { label: '누적 실제', data: trendActual, borderColor: '#cc785c', ... },   // 기존
    { label: '예산 페이스', data: trendPace, borderColor: '#a09d96',
      borderDash: [5,4], pointRadius: 0 },                                     // 기존
    { label: '월 예산', data: trendBudget, borderColor: '#b9472e',
      borderDash: [2,2], pointRadius: 0 },                                     // NEW 가로선
  ]},
});
```

- 가로선 색은 한도 천장을 나타내는, 기존 실제(`#cc785c` 클레이)·페이스(`#a09d96` 웜그레이)와
  시각적으로 구분되는 warm 계열 빨강. 기본값 `#b9472e`(구현 시 디자인 토큰
  `static/src/input.css`과 대조해 미세조정 가능).
- 실제선의 `null` 꼬리는 Chart.js 기본(`spanGaps:false`)으로 자동 끊김 — 추가 옵션 불필요.

## 데이터 흐름

```
overview_context
  ├ daily_series(conn, None, now, budget_start=bs)   # 전 AI 합산, 도입일 clamp, 말일까지
  │    └ DayPoint(day=달력날짜, cumulative_cost=누적|None)
  └ budget.total                                      # 통합 월 예산
        → daily_labels / daily_actual / daily_pace / daily_budget
            → _trend_chart.html (3개 데이터셋)
```

## 하위호환 · 엣지 케이스

- **`budget_start` 미설정** → `effective_month_start`가 달력 월 1일 반환 → 1일~말일 전체.
  (x축 말일 확장 + 월 예산 가로선은 도입일 사용자뿐 아니라 **모든 사용자에 적용되는 개선**.)
- **`budget_start`가 다른 달/과거·미래 달** → `effective_month_start`가 무시(1일) → 기존 동작.
- **예산 0(추적 전용)** → `limit=0` → 페이스선·가로선 모두 0(바닥 평선). 기존과 동일.
- **도입일이 이번 달이지만 오늘보다 미래**(예: 오늘 6/15, 도입일 6/20) →
  `range(20, 31)`의 모든 `d`가 `d <= 15`를 만족하지 못해 실제선 전부 `null`,
  페이스선·가로선만 표시. 크래시 없음. 번다운 카드의 기존 엣지 동작과 일관(별도 처리 안 함).

## 범위 밖 (이번에 안 건드림)

- 내역·모델별 페이지의 주/월 토글·임의 구간 조회(`views._resolve_range`)는 사용자
  주도 조회라 `budget_start`와 무관 — 그대로 둔다.
- Codex 주간 carryover 모델 자체(번다운 카드)는 변경 없음. 통합 추세는 고수준
  개요이므로 월 합산 기준만 쓴다(per-provider 카드가 정밀한 주간 뷰 담당).

## 테스트

- `test_daily_series_clamps_to_budget_start` — 도입일 6/12: 첫 점 `day==12`,
  6/1~6/11 지출이 누적에서 제외, 말일(30)까지 확장, 오늘 이후 점은 `cumulative_cost is None`.
- `test_daily_series_extends_to_month_end` — `daily[-1].day == 말일`,
  `len(daily) == 말일 - period_start.day + 1`.
- `test_daily_series_no_budget_start` — 1일 시작·말일까지(기존 `test_daily_series_cumulative`
  를 말일 확장 + `None` 꼬리에 맞게 갱신/대체).
- 웹(`test_web.py`) — `daily_pace[-1] == budget.total`(부동소수 허용오차),
  `daily_budget` 전부 동일값(`== budget.total`), `daily_actual`의 오늘 이후 원소가 `null`.

## 영향 파일

- `tokenomy/aggregate.py` — `daily_series` 시그니처/로직, `DayPoint.cumulative_cost` 타입.
- `tokenomy/web/views.py` — `overview_context`의 추세 컨텍스트(`daily_*` 4개).
- `tokenomy/web/templates/_trend_chart.html` — `월 예산` 데이터셋 추가.
- `tests/test_aggregate.py`, `tests/test_web.py` — 위 테스트.
