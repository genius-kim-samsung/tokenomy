---
status: accepted
---

# 수동 예산을 제거하고 공식 사용량을 한도의 정본으로 삼는다

공식 사용량 라이브 취득(공식 API 단발 GET)을 도입한 뒤, 수동으로 입력하던 월 예산은 두 사용 형태 모두에서 잉여가 됐다. **엔터프라이즈/종량제**는 공식 API가 실제 USD 한도를 주므로 수동 예산과 중복되고(같은 화면에 `$지출/$한도` 막대가 공식·수동 두 벌로 떠 혼란), **개인 구독제**는 정액제라 월 USD 예산이 애초에 허구의 숫자였다(actionable 신호는 5h/7d rate-window 잔여). 그래서 수동 예산 입력과 그에 딸린 번다운 엔진(`burndown`·`codex_burndown`·`combined_burndown`·`budget_start` 클램프)을 제거하고, 예측(일일 소비속도·소진예상·리셋 D-day)은 공식 데이터 기반 `official_view`+`lens` 단일 엔진으로 통일한다. 이에 맞춰 공식 취득을 default-on으로 승격한다.

## 고려한 대안

- **번다운을 공식 버킷 분모로 재배선**(예산만 공식 한도로 교체): 거부. `official_view`+`lens`가 이미 같은 예측을 하므로 예측 엔진이 둘이 되어, 제거하려던 "두 패널의 숫자가 미묘하게 다른" 혼란이 재발한다.
- **예산을 자기-상한(self-cap)으로 축소 존치**: 거부. 단순화 목표에 역행하고, 공식 한도가 정본인 이상 별도 목표 한도는 또 하나의 헷갈리는 노브가 된다.

## 결과

- 공식 취득이 off(`TOKENOMY_SKIP_OFFICIAL_FETCH`)이거나 한 번도 성공 못 한 사용자는 한도/번다운 없이 **사용량 전용 view**로 격하된다 — 의식적으로 수용한 트레이드오프.
- 공식 취득 default-on은 "전 과정 로컬" 원칙을 완화한다. 설정 UI의 on/off 노브는 없애되, `TOKENOMY_SKIP_OFFICIAL_FETCH` env 비상구와 `min_interval_minutes` throttle(공유 429 쿼터 보호)은 유지한다.
- 어떤 provider를 호출/표시할지는 새 `tracked_providers`(리스트, 신규 AI 확장 용이)가 게이트한다.
- `load_config`는 레거시 config 키(`budget`·`budget_start`·`official_fetch.enabled`)가 남아 있어도 에러 없이 무시한다.
- Codex 엔터프라이즈는 Phase 1에서 일일 소비속도 lens가 없다(주간/월간 게이지가 리셋 표시를 담당) — interim 한계로 수용.
