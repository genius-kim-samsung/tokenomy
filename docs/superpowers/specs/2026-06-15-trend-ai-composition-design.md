# 통합 추세 그래프 — AI별 구성 비중(스택 영역) 설계

- 날짜: 2026-06-15
- 상태: 승인 대기(브레인스토밍 산출 스펙)
- 범위: 대시보드(overview) 통합 추세 차트 1종

## 배경 / 문제

대시보드 "통합 추세" 차트는 `누적 실제`를 **전 AI 합산 단일 라인**으로만 보여준다
(`daily_series(conn, provider=None, …)`). 합계 대비 예산 번다운은 읽히지만, 그 합계에
Claude / Codex가 **각각 얼마씩 기여했는지(비중)** 는 그래프 안에서 알 수 없다.

목표는 합계 번다운 읽기를 깨지 않으면서, 그래프 내에서 AI별 비중을 드러내는 것이다.

## 목표 / 비목표

**목표**
- `누적 실제` 단일 라인을 **AI별 누적 스택 영역**으로 교체. 스택 맨 윗선 = 기존 합계선과 동일.
- 밴드 두께로 각 AI의 그날까지 기여분(절대액)을 표현.
- 툴팁에 AI별 금액 + **% 점유율** + 합계 표시(원래 요구 "비중"의 숫자 충족).
- provider가 늘어도(파서 추가) 자동으로 밴드가 늘도록 N-provider 일반화.

**비목표**
- `예산 페이스`(점선)·`월 예산`(천장선) 기준선 동작 변경 — 그대로 유지.
- 합산/AI별 토글 UI — YAGNI(전면 교체로 충분, 합계는 스택 윗선에 보존).
- 100% 점유율(정규화) 차트 — 이번 범위 아님(절대 스택 + 툴팁 %로 비중 충족).
- Codex 주간/Claude 월간 등 번다운 주기 로직 변경 — 무관.

## 설계 개요

`overview.html`의 `<canvas id="trend">`를 그리는 `_trend_chart.html`에서, 단일
`누적 실제` 데이터셋을 **provider별 누적 스택 영역**으로 바꾼다.

```
$ 누적 비용 (합계 높이는 동일)
│              ╱▓▓▓▓  ← Codex (위 밴드, teal)
│          ╱▓▓▓▓▓▓▓
│      ╱▓▓▓▓░░░░░░░
│  ╱▓▓░░░░░░░░░░░░  ← Claude (아래 밴드, coral)
│╱░░░░░░░░░░░░░░░░
└────────────────── 일
밴드 두께 = 그 AI의 기여분, 맨 윗선 = 전체 합계
```

기준선 2개(`예산 페이스`, `월 예산`)는 영역 위에 그대로 오버레이된다.

## 데이터 계층 (aggregate.py + views.py)

### provider별 시계열
- 기존 합산 호출 `daily = daily_series(conn, None, now, budget_start=bs)`는 **유지** — 라벨
  (`daily_labels`)과 pace/budget 길이(`len(daily)`·`enumerate(daily)`)에 여전히 쓰인다.
  다만 차트가 더는 소비하지 않는 `daily_actual` 배열(합산 누적선)은 컨텍스트에서 제거한다.
- provider별로 `daily_series(conn, "claude", …)`, `daily_series(conn, "codex", …)`를 호출한다.
  동일 날짜 범위(`effective_month_start`~말일)를 쓰므로 **인덱스가 자동 정렬**되고,
  미래 날(`day > now.day`)은 모든 provider에서 동시에 `None` → 선 끊김도 정렬된다.

### 스택 경계값(순수 함수, aggregate.py)
스택 렌더는 "각 밴드의 윗 경계(running cumulative sum)"가 필요하다. 이 변환을
**순수 함수로 분리**해 aggregate.py에 두고 단위테스트한다.

```python
def stacked_trend(per_provider: list[tuple[str, list[DayPoint]]]) -> list[dict]:
    """provider별 누적 시계열을 스택 밴드로 변환.

    입력: [(provider, [DayPoint, …]), …]  (모두 같은 길이·날짜 정렬 가정)
    반환: [{provider, cum: [float|None], top: [float|None]}, …]
      - cum = 그 provider의 누적(원본, 툴팁 표시·비중 계산용)
      - top = 아래 밴드까지 더한 running sum(차트 fill 경계용)
    미래 날(cum=None)은 top=None으로 전파 → 차트에서 끊김.
    """
```

- 불변식: 임의의 과거 날 `i`에서 `마지막 provider의 top[i] == sum(provider별 cum[i])`이며,
  이는 합산 시계열 `daily_series(None)`의 `cumulative_cost[i]`와 같아야 한다.
- None 정렬: 과거 구간은 모든 provider가 숫자, 미래 구간은 모두 None(동일 날짜 범위 보장).

### views.py 조립
`overview` 뷰에서 다음을 컨텍스트에 추가:
- provider→(라벨, 색) **순서 있는 레지스트리**로 "데이터 있는 provider만" 순서대로 수집
  (예: `claude_has_data`, `codex_has_data` 플래그 재사용). 빈 밴드는 미포함.
- 각 provider의 `cum`(툴팁용 raw 배열)과 `top`(fill 경계)을 JSON으로 전달.
- 색 배정: `Claude=#cc785c(coral, 기존 유지)`, `Codex=#5db8a6(teal, DESIGN.md accent-teal)`.
  레지스트리에 없는 신규 provider는 정의된 팔레트(coral→teal→amber…)를 순환 배정.

레이아웃 분리는 기존 원칙 유지: 라우트(app.py) ↔ 조립(views.py) ↔ 집계(aggregate.py).
스택 경계 계산은 집계(순수 함수), 색·라벨·datasets 구성은 조립/템플릿 책임.

## 렌더링 (_trend_chart.html, Chart.js)

**축 stacking을 쓰지 않는다.** Chart.js v4에서 `scales.y.stacked=true`는 라인
데이터셋(pace·budget)까지 밀어올려 기준선이 깨지는 함정이 있다. 대신 서버에서
미리 합산한 `top` 경계 + `fill` 상대참조로 영역을 만든다:

- 밴드0(가장 아래 provider): `data=top0`, `fill:'origin'`, 반투명 fill + 불투명 border.
- 밴드k(k≥1): `data=topk`, `fill:'-1'`(직전 데이터셋까지 채움) → 채워진 영역 = 그 provider 기여분.
- `예산 페이스`·`월 예산`: 기존 그대로, `fill:false` 라인 → 축 stacking 미사용이라 raw 값에 그려짐.

데이터셋 순서(중요): `[밴드0, 밴드1, …, pace, budget]`. `fill:'-1'`이 바로 아래 밴드를 참조하도록 영역 밴드를 연속 배치.

- fill alpha ≈ 0.5 — 위에 깔리는 점선 기준선 가독성 확보.
- 미래 날 `None` → 영역·선 모두 오늘에서 끊김(기존 동작 유지).

## 툴팁 (% 점유율 — 비중 직접 표시)

스택 경계값(top)이 아니라 **provider별 raw 누적(cum) 병렬 배열**을 읽어 표시한다.

```
6월 15일
 Claude  $30.00 (71%)
 Codex   $12.10 (29%)
 ─────────────────
 합계     $42.10
```

- 라벨 콜백: `cum[provider][i]`로 금액, `금액 / 합계`로 % 산출.
- footer 콜백: 그날 합계(= 마지막 밴드 top[i]).
- 합계 0인 날: % 분모 0 → 0%로 처리(0 나눗셈 가드).

## 엣지 케이스

- **한쪽 AI만 사용**: 미사용 provider는 "데이터 있는 provider만 포함" 규칙으로 밴드에서 제외
  (레전드에도 안 뜸). 양쪽 다 있으면 2밴드.
- **예산 미설정**: 기준선 0(기존과 동일), 영역만 표시.
- **데이터 없음(`has_data`=False)**: 차트 자체 미렌더(기존과 동일).
- **신규 provider 추가**: 레지스트리에 (라벨,색) 한 줄 추가하면 밴드 자동 생성.
  팔레트가 골드 예산선과 충돌하기 시작하는 4번째부터는 팔레트 확장을 별도 검토.

## 테스트

- aggregate: `stacked_trend` 순수 함수 단위테스트
  - 불변식(마지막 top == provider별 cum 합 == 합산 시계열)
  - 미래 None 전파·정렬
  - 1 provider / 2 provider / 빈 입력
- 기존 `daily_series`·번다운 테스트 회귀 없이 통과.
- 템플릿/CSS 클래스 변경 없음(차트 설정은 JS 인라인) → `build_css.ps1` 불필요.

## 변경 파일(예상)

- `tokenomy/aggregate.py` — `stacked_trend` 순수 함수 추가(+ 필요 시 provider 수집 헬퍼).
- `tokenomy/web/views.py` — overview 컨텍스트에 provider별 cum/top + 색·라벨 조립.
- `tokenomy/web/templates/_trend_chart.html` — 단일 라인 → 스택 영역 datasets, 툴팁 콜백.
- `tests/test_aggregate.py` — `stacked_trend` 테스트.
- `.gitignore` — 본 스펙 파일 화이트리스트(`!docs/superpowers/specs/2026-06-15-trend-ai-composition-design.md`).

## 미해결 / 향후

- 4번째 이상 provider의 팔레트 충돌(골드 예산선) — 실제 도입 시점에 재검토.
- 100% 정규화(점유율 추이) 보기 — 수요 생기면 토글로 별도 검토(현재 비목표).
