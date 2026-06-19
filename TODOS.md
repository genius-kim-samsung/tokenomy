# TODOS — D-day 게이지 후속

회사 월 할당 D-day 게이지 + 공식 사용량 수동 입력(1A)의 후속 작업. 우선순위 순.

## ① 공식 사용량 자동 읽기 (스파이크)

현재는 회사 화면의 "이번 달 누적액"을 사람이 직접 입력한다(수동 스냅샷). 회사 포털/대시보드에서
공식 사용량을 자동으로 가져오는 경로를 스파이크한다(로그인/스크래핑/사내 API 가능성 조사).

- 자동 import 시 `official_usage.snapshot_ts`는 입력 시각이 아니라 **공식 데이터의 as-of 시점**을
  채운다(스키마는 `snapshot_ts`/`created_at`을 이미 분리해 둠).
- 프라이버시·사내망 제약 검토 필수(전 과정 로컬 원칙 유지).

## ② 공휴일·연차 제외

영업일 계산은 현재 **주말(토·일)만** 제외한다(stdlib `weekday()`). 공휴일/개인 연차를 빼면
D-day 추세(소진 예측·월말 공백)가 더 정확해진다.

- config에 휴일 목록(`YYYY-MM-DD[]`)을 받아 `business_days_between`/`add_business_days`에서 제외.
- 개인 연차도 같은 목록에 추가 가능하게.

## ③ Codex 회사 사용량 기능 출시 감시

게이지·공식 입력은 현재 **Claude-only**다(Codex는 회사 공식 사용량 제공 기능 미출시). Codex가
회사 사용량을 노출하기 시작하면 `official_usage(provider="codex")` + 게이지를 확장한다.

- `official_usage` 스키마는 이미 `provider` 컬럼을 보유 — Codex 추가 시 입력 폼/게이지만 확장하면 된다.
