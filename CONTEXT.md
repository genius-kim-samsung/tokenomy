# Tokenomy

AI 코딩 토큰 지출을 추적하는 로컬 가계부. 이 글로서리는 프로젝트 고유의 도메인 용어만 정의한다(구현 세부는 CLAUDE.md / docs 참조).

## 사용 형태(usage mode)

사용자는 계정 성격에 따라 둘 중 하나의 형태로 앱을 쓴다. 형태는 공식 사용량 응답의 모양으로 판정한다.

**엔터프라이즈/종량제**:
공식 API가 provider-강제 **USD 한도**(`spend.limit`·달러 크레딧 창·Codex 크레딧 한도)를 노출하는 형태. 토큰/달러 단위로 실제 과금되며, 공식 한도가 곧 기간 예산이다.
_Avoid_: PAYG, 유료 플랜

**개인 구독제**:
정액제라 토큰당 과금이 없는 형태. 공식 API는 USD 한도 대신 rate-window(%)만 준다. 월 USD 예산은 이 형태에 의미가 없다.
_Avoid_: 무료, Pro/Max(특정 플랜명으로 부르지 말 것)

## 한도와 사용량

**공식 사용량**:
각 CLI의 로컬 OAuth 토큰으로 공식 API를 읽어 얻는, provider가 강제하는 실제 소진·한도·리셋. 한도/잔여의 **정본(source of truth)**.
_Avoid_: 예산, budget, 수동 한도 — "사용자가 정한 목표 한도"라는 옛 개념은 더 이상 모델에 없다.

**rate-window**:
개인 구독제에서 공식 API가 주는 회전 시간창(5시간·7일)의 이용률(%). 개인 구독제 사용자의 핵심 actionable 신호.
_Avoid_: 이용률 창(코드 라벨일 뿐, 도메인 용어로는 rate-window)

**사용량 전용 view**:
공식 데이터가 없을 때(공식 취득 off·미성공, 또는 한도를 안 주는 계정) 보여주는 폴백 화면. 로컬 JSONL에서 추정한 사용량 차트만 있고 한도/번다운은 없다.
_Avoid_: 추적 전용 모드(옛 "예산 0" 상태를 가리키던 말 — 폐기)

## 추적 대상

**tracked providers**:
사용자가 "내가 쓴다"고 선언한 AI 도구의 집합. 공식 API 호출 대상과 대시보드 카드 가시성을 게이트한다. 첫 실행 시 크레덴셜 파일 존재로 시드한다.
_Avoid_: enabled providers, 활성 provider

**provider**:
하나의 AI 도구/로그 출처(claude·codex 등). 새 도구 추가는 provider 하나를 더하는 것과 같다.
_Avoid_: tool, AI, 벤더(코드 전반에서 provider로 통일)
