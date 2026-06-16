# Tokenomy 범용 공개(public) 전환 설계

- 날짜: 2026-06-12
- 상태: 설계 승인 대기 → 승인 시 writing-plans로 이행
- 작성: 브레인스토밍 세션(사용자 ↔ Claude)

## 배경

Tokenomy는 Claude Code / Codex CLI의 로컬 세션 로그(JSONL)를 파싱해 **월 토큰 예산을 가계부처럼 관리**하는 로컬 도구다. 현재는 내부 PoC로, 조직의 토큰 예산 등급 정책(`config/tiers.json`)이 코드에 박혀 있다.

이 설계의 목표는 **조직 고유 정보를 코드에서 제거하고, 종량제로 인별 토큰 사용량을 관리해야 하는 전 세계 사용자가 쓸 수 있는 범용 public 레포지토리**로 전환하는 것이다. 핵심 전환점은 "조직 예산 등급 테이블 → 사용자가 입력하는 예산"이다.

## 목표 (Goals)

1. 조직 고유 정보(조직 예산 등급 정책, 요금 환산율, 개인 식별자, 조직 맥락 문서)를 코드·문서·git 히스토리에서 제거한다.
2. 예산을 사용자 입력으로 받는다. provider별(Claude/Codex) 분리 예산을 웹 설정 화면 + config 파일로 관리한다.
3. 조직용/범용용 코드를 **한 벌로 통일**한다. 사용자은 로컬 config(gitignore)에 자기 예산 숫자만 둔다.
4. 파서·단가·번다운·대시보드 등 기존 자산은 최대한 그대로 살린다.

## 비목표 (Non-goals / YAGNI)

- **팀/멀티유저 집계**: 여러 사람의 로그를 한 대시보드에 모으지 않는다. "인별 관리"는 각자 자기 머신에서 돌리는 방식(= 1머신 1사용자). 프라이버시상으로도 자연스럽다.
- **Claude Code·Codex CLI 외 도구의 전체 파서 구현**: 확장점(파서 인터페이스)만 열어두고, 신규 도구 파서는 커뮤니티/후속 과제로 둔다.
- **구독 정액제(Pro/Max/Plus) 전용 기능**(rate-limit/토큰량 관점 UI): 1차 타겟은 종량제. 구독 사용자는 "API 환산 추정치"로 추적만 가능(덤).
- **통화 환산**: 비용은 USD 단일 표기. 환율 변환 없음.

## 핵심 결정 요약

| # | 결정 | 선택 |
|---|---|---|
| 1 | 예산 구조 | provider별 분리(Claude $X / Codex $Y) |
| 2 | 예산 입력 방식 | 웹 대시보드 설정 화면 + 로컬 config 파일 |
| 3 | 범용화 전략 | A. 단일 범용 코드 + 로컬 config(티어 테이블 제거) |
| 4 | 사용자 모델 | 1머신 1사용자 로컬 도구(팀 집계 제외) |
| 5 | 1차 타겟 | 종량제(API 달러 과금) 중심. 구독 사용자는 추정치 추적만 |
| 6 | 문서 언어 | 영문 기본(README.md) + 한글 병기(README.ko.md) |
| 7 | 라이선스 | MIT |
| 8 | provider 키 | 내부 키 `"chatgpt"` → `"codex"`로 통일 |

## 상세 설계

### 1. config 스키마 (`tiers.json` 대체)

```jsonc
// config/tokenomy.config.json        ← .gitignore (개인 정보)
// config/tokenomy.config.example.json ← 커밋 (템플릿)
{
  "user_label": "me",          // 표시용 라벨(선택). 미지정 시 OS username
  "budget": {
    "claude": 100,             // 월 예산 USD. 0 = 한도 없음(추적 전용 모드)
    "codex":  50
  },
  "pricing_overrides": {}      // 선택: 자기 요금제/협상 단가로 모델 단가 덮어쓰기
}
```

- 파일 위치는 레포 `config/` 기본. 추후 `~/.tokenomy/config.json` 같은 홈 경로 지원은 후속 고려(현 PoC는 레포 로컬 유지).
- `tokenomy.config.example.json`을 커밋해 첫 사용자가 복사해서 시작하게 한다.

### 2. `tiers.py` → `budget.py`

- 티어 매핑 로직(`half`/`choice`/`base`, `budget_for`) **전부 삭제**.
- config에서 `budget.claude`/`budget.codex`를 직접 읽어 `Budget(claude, codex)`를 생성하는 단순 로더로 교체.
- `Budget` 데이터클래스(`total`, `limit_for`)는 **유지** — provider별 분리 예산 모델과 그대로 일치. 단 내부 필드 `chatgpt` → `codex`로 리네임해 §6의 provider 키 통일과 정합을 맞춘다(`limit_for("codex")`).
- `load_tiers()` 호출부(`cli.py`, `web/`)를 새 config 로더로 교체.

### 3. 조직정보 분리 변경점

| 항목 | 처리 |
|---|---|
| `config/tiers.json` (조직 예산 등급 정책·한도 달러) | **삭제** |
| `default_user: me` | 제거. example엔 `"me"` |
| `pricing.json` `_meta`의 "요금 환산율" 경고, fable/placeholder 주석, 내부 검증 항목 참조 | 공개 API 단가 기준으로 정리. 조직 맥락 문구 제거 |
| `README.md`의 "조직 플랜 등급", 내부 기획 경로(`~/.gstack/...`), 내부 검증 항목 스파이크 | 범용 문구로 전면 교체(영문화) |
| `docs/ccusage-분석-보고서.md` 등 조직 맥락 문서 | public 노출 전 검토 — 조직 정보 있으면 제거/정리 |
| git 히스토리 속 조직 흔적(내부 기획 경로·티어 값·개인 식별자) | public엔 클린 커밋으로 시작(§7) |

### 4. 웹 설정 화면

- 대시보드에 `설정` 섹션 또는 `/settings` 라우트 추가.
  - `GET`: 현재 예산(Claude/Codex) 표시.
  - `POST`: 입력값으로 `tokenomy.config.json` 갱신(원자적 쓰기).
- 로컬 전용 도구이므로 인증 없음. `127.0.0.1` 바인딩 유지(외부 노출 금지를 README에 명시).
- **첫 실행 온보딩**: config가 없거나 예산이 비면 대시보드 상단 배너 — "월 예산을 설정하세요 →".
- **추적 전용 모드**: 예산 0이면 번다운/소진예상 대신 "사용량·비용 추적만" 화면으로 폴백(0으로 나누기 방지).

### 5. pricing

- 공개 API 단가를 기본 제공(현 `pricing.json`을 정리해 그대로 사용).
- `pricing_overrides`로 사용자가 모델별 단가를 덮어쓴다(자기 요금제/협상가/요금 환산율 등).
- **표시 라벨 명확화**: UI/리포트에서 "비용"은 *공개 API 단가 기준 환산액*임을 명시. 종량제 사용자는 실제 청구액에 근접, 구독 정액제 사용자는 *참고용 추정치*.
- 단가는 변하므로 `pricing.json`을 사용자가 갱신 가능하게 두고, README에 "단가 최신화" 안내(claude-api 스킬/공식 단가표 참조)를 둔다.

### 6. provider 확장성

- 파서 인터페이스(ingest 함수 시그니처: `(conn, root, pricing) -> int`)를 명문화해, **기본 2종(Claude Code `parser.py`, Codex CLI `codex_parser.py`) + 커뮤니티 파서 추가 가능** 구조로 문서화.
- 신규 도구(예: Gemini CLI) 전체 지원은 보류. 확장점과 "파서 추가하는 법" 문서만 제공.
- 내부 provider 키 `"chatgpt"`(현재 Codex를 의미, 혼란 유발) → **`"codex"`로 통일**.
  - 영향: `db`, `aggregate`, `pricing.json`의 `provider` 필드, `cli.py`/`web` 표시. 로컬 DB(`data/tokenomy.db`)는 gitignore라 재수집(`ingest`)으로 해결.
  - `pricing.json`의 chatgpt 단가 항목은 `provider: "codex"`로 변경하고 placeholder 주석 정리.

### 7. 배포

- **라이선스**: MIT (`LICENSE` 추가).
- **README**: `README.md`(영문) 기본 + `README.ko.md`(한글) 병기. 설치/예산 설정/실행/프라이버시(메타만 저장, 127.0.0.1 전용)/단가 최신화/파서 추가 안내 포함.
- **git 히스토리**: A전략이므로 기존 repo를 범용화한 뒤, **public 원격엔 조직 흔적 없는 클린 커밋으로 시작**(squash 또는 `git filter-repo`로 `tiers.json`·내부 기획 경로·개인 식별자 제거). 기존 작업 히스토리는 비공개 private에만 남긴다.
- **네이밍**: "Tokenomy" 유지(조직 맥락 없음). 태그라인은 영문화.
- `.gitignore`에 `config/tokenomy.config.json` 추가 확인. `data/`는 이미 무시됨.

## 영향받는 파일 (개략)

- 삭제: `config/tiers.json`
- 신규: `config/tokenomy.config.example.json`, `tokenomy/budget.py`(또는 `tiers.py` 대체), `LICENSE`, `README.md`(영문화), `README.ko.md`
- 수정: `tokenomy/cli.py`(로더 교체·provider 키), `tokenomy/aggregate.py`(provider 키), `tokenomy/db.py`(provider 키), `tokenomy/pricing.py` + `config/pricing.json`(정리·키), `tokenomy/web/*`(설정 화면·온보딩·추적전용 모드·표시 라벨), `.gitignore`, `start_tokenomy.bat`(필요 시)
- 검토: `docs/ccusage-분석-보고서.md`(조직 정보 점검)
- 테스트: `tests/test_tiers.py` → `test_budget.py`로 교체, provider 키 변경분 반영(`test_pricing.py`, `test_db.py`, `test_aggregate.py`, `test_web.py`)

## 미해결 / 후속

- config 홈 경로(`~/.tokenomy/`) 지원 여부 — 후속.
- 신규 도구 파서(Gemini CLI 등) — 커뮤니티/후속.
- git 히스토리 정리 구체 방식(squash vs filter-repo) — 구현 단계에서 확정.
