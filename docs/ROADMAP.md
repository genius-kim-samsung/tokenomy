# Tokenomy 로드맵 / 향후 계획

- 최종 갱신: 2026-06-17 (v0.2.0 스코프 그릴 확정 — 채택 6개·provider parity·증분 릴리스)
- 상태: **초안** — 코드/spec의 "미해결·후속" 신호 + 사용자 비전을 종합.
- 관련: [PRD.md](PRD.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [DATA-MODEL.md](DATA-MODEL.md)

## 현재 상태 (v0.1.8)

- Claude Code + Codex CLI 증분 수집(byte-offset), SQLite 적재, raw archive(30일 휘발 대비)
- provider별 예산 번다운(Claude 월간 / Codex 주간 누적·이월) + 예산 도입일(`budget_start`) + 소진 예상일, 효율 코치 카드
- 웹 대시보드 페이지: overview(`/`) / 내역(`/history`) / 차원별(`/analysis`) / 세션 상세 / settings.
  과거 `/projects`·`/sessions`·`/models` 탭은 `/history`·`/analysis`로 301 리다이렉트(레거시)
- overview: 이번 달 총지출 요약 + Claude/Codex provider별 번다운 카드 + 통합 추세 차트
- 통합 추세 차트: AI별 스택 영역(구성 비중) + 월 예산 가로선·예산 도입일 정합 + 끝점 라벨로 AI별 구성 상시 표시(금액·%)
- **차원별 분석 뷰(`/analysis`)** — 모델·스킬·브랜치 귀속 비용 롤업(차원 선택기) + 서브에이전트(sidechain) 비중 카드 + 미귀속 버킷 명시. 기존 모델별(`/models`)을 일반화·흡수 (v0.1.6)
- **토큰 구성비·캐시 재구축 신호 (v0.1.7)** — 오버뷰 토큰 구성 미니바(input/output/cache_wr/cache_rd 4종 비중, **토큰량 기준** — 비용≠토큰 주석) + 차원별 테이블 토큰 4분할(cache_wr 칸) + 효율 코치 "캐시 재구축" 카드(이어지는 세션인데 캐시를 못 읽은 고유 세션 수, 달력 월 기준)
- **단가 커버리지 진단 + 단가 변경 자동 재계산 (v0.1.8)** — 모델별 단가 매칭 신뢰도(미식별·버전경계 의심·거친 매칭)를 settings 카드·overview 경고·CLI report로 노출 + `pricing_overrides`로 새 모델 단가 자가 추가(prepend). 단가(pricing.json/overrides) 변경 시 핑거프린트로 기존 비용 자동 재계산(`maybe_reprice` — `cache_creation_1h` 분리 저장으로 5m/1h도 정확). v0.2.0 채택 세트 #1 (아래)
- 내역 화면: 날짜→폴더→세션 계위 트리(접기·펼치기) — 기존 5탭 대체
- 내역·차원별 주/월 토글 + 사용자 지정 날짜 구간 조회(`period`/`start`/`end`)
- 메시지 수를 사용자 턴(`user_turns`) 기준 집계 + 멀티데이 세션 날짜별 정확 카운트(`session_day_turns`)
- Codex 세션 식별용 첫 사용자 프롬프트 120자 발췌(`sessions.summary`) — 프라이버시 발췌선 유지
- Windows onefile exe(pywebview 네이티브 창) + 인앱 업데이트 배너
- 조직 예산 등급 정책 제거 → 사용자 입력 예산으로 범용화 완료

## 다음 마일스톤 — v0.2.0: 수집된 raw 데이터를 빠짐없이 가공해 보여주기

> 사용자 확정(2026-06-15): v0.2.0의 핵심은 **raw 데이터를 가공/계산/집계해, 사용자가 궁금해할
> 유용한 정보를 빠짐없이 보여주는 것**. Insight 추출(코칭·이상탐지·절감 제안·예측)은 그 이후로 분리.
> **스코프·원칙·채택 세트는 2026-06-17 그릴(grill-me)로 확정** — 아래.

### 설계 원칙 (2026-06-17 확정)

1. **자격/강도 위계** — "사용자가 궁금해하고 해석 가능"하면 *표시 자격*. 추가로 "사용자가 개선 행동 가능 +
   높/낮음이 오해 없이 좋/나쁨으로 읽힘"이면 *표현 강도*(경고·강조 허용). 강도 미달이면 경고 없는
   **중립 투명성 수치**로만 표시. (cf. 1h 캐시 프리미엄 = 자격·강도 둘 다 미달 → 제외.)
2. **raw 추출 허용** — 이미 ingest하는 로그(Claude `~/.claude` · Codex `~/.codex`)에서 유용하면
   DB 컬럼이 없어도 파서·스키마를 바꿔 추출한다. **단 새 *소스*(Gemini 등)·사용자 *입력*(라벨)은 제외**
   — 전자는 별도 축(후속), 후자는 raw 추출이 아닌 authoring. *(옛 "새 수집 없이" 원칙 폐기.)*
3. **provider parity** — Claude·Codex **양쪽 다 의미 있는 값이 나와야** 채택(분해·정밀도 차이는 허용).
   한쪽만 지원되면 왠만하면 제외.
4. **cost/value 필터** — 위를 통과해도 작업량 대비 가치가 낮으면 뺀다.
5. 구현 계층 — 가공/집계 `aggregate.py`(순수 함수) ↔ 화면 `views.py` ↔ 라우트 `app.py`(얇게).
   KST 월/주 경계·예산 도입일 clamp 일관 적용.

> 참고 — **귀속 3종(`attribution_skill`·`git_branch`·`is_sidechain`)은 v0.1.6 출시 완료**
> (spec/plan: `docs/superpowers/{specs,plans}/2026-06-15-attribution-dimension-views*`). Claude 로그 기반이라
> 차원별 뷰는 provider별 가용성 분기로 "Claude 로그 기준" 안내를 노출한다.

### 채택 세트 (6개) — 시퀀싱 순

배치: nav 5개 = overview(`/`) / 차원별(`/analysis`) / **패턴(`/patterns`, 신규)** / 내역(`/history`) / settings.
릴리스: **증분**(항목별 작은 패치/마이너), **v0.2.0은 세트 완성 시점의 완료 마커**.

- [x] **1. 단가 커버리지 신뢰성 진단** (`priced=0`+오매칭+거친매칭) — 모델별 매칭 상태를 settings 카드·
  overview 경고·CLI report로 노출. `pricing_overrides` 확장으로 사용자가 새 모델 단가까지 자가 추가.
  *(단가 편집 GUI는 후속 sub-project로 분해 — spec/plan: `2026-06-17-pricing-coverage-diagnostics*`.)*
- [ ] **2. 입출력 토큰 비율** — 세션·프로젝트 단위 토큰 구성. 기존 4분할 컴포넌트를 새 결로 확장
  (Codex는 cache_wr=0 → 3분할). 세션 상세 + `/analysis`.
- [ ] **3. 캐시 절감액 추정** — `순절감 = cache_read×(input−cache_read) − cache_creation×(cache_creation−input)`
  = "캐시 안 썼다면(매 턴 컨텍스트 재전송)"의 반사실과 일치. **Claude는 3줄 분해**(읽기회수 / 생성프리미엄 /
  순절감 — 음수면 캐시 재구축 낭비와 연동), **Codex는 프리미엄=0이라 1줄**. overview 카드.
  *(1h/5m 분리 비율은 제외 — actionability 기각, cf. metric-selection-actionability 메모.)*
- [ ] **4. 프로젝트/모델별 시계열** — `/analysis`에 시간축 추가(현재 dim 롤업을 추세로). 주/월 토글 답습.
- [ ] **5. Codex 시간데이터 enabler** — Codex rollout의 per-event timestamp + 누적 token_count로 시간대별
  토큰 델타 추출. **세션당 1레코드 구조 유지**, `session_day_turns` 선례를 따른 **가벼운 위성 테이블**로 적재
  (1→N 레코드 재설계 안 함). 6번의 선행 조건.
- [ ] **6. 시간 패턴 + 세션 형태** — **패턴 페이지**(신규):
  - 시간 패턴 — 시간대(hour)·요일 히트맵(지출/활동 토글). **중립 투명성**(경고 없음).
    요일축 양쪽 정확, hour축은 Codex 근사(각주).
  - 세션 형태 — 지속시간·턴 분포·턴당 비용(패턴 페이지 + 내역/세션상세 칸). Codex 지속시간은 위성테이블 event ts로.

### 제외 (그릴에서 명시적 컷 — 근거)

- **캐시 1h/5m 분리 비율** — TTL은 클라이언트 자동결정(통제 불가), 프리미엄=낭비 아닌 보험료.
  자격·강도 둘 다 미달.
- **server tool 집계** (`web_search`/`web_fetch`) — Codex엔 해당 도구 부재(function_call = `shell_command`/`apply_patch`뿐).
  **provider parity 실패** → backlog(아래 후속 과제).
- **Codex 도구활동 횟수** — parity는 가능하나 actionable하지 않고 도구집합 비대칭 → value 낮음.
- **세션 라벨별 집계 + 라벨 편집 UI** — 라벨은 raw 추출이 아닌 **사용자 입력**(authoring) → backlog(아래 후속 과제).

## 후속 과제 (코드/spec에서 도출)

각 항목 끝의 출처는 근거 위치다. 우선순위는 사용자 확정 전 잠정.

### 배포 / 공개 전환
- [ ] **공개 원격 분리** — 조직 흔적(과거 티어 값·개인 식별자·내부 기획 경로) 없는 클린 커밋으로
  public 레포 시작. squash vs `git filter-repo` 방식은 실행 단계에서 확정.
  *(출처: public-generalization spec §7 배포)*
- [ ] **docs 조직정보 점검** — `ccusage-분석-보고서.md` 등 공개 전 조직 맥락 제거/정리.
  *(출처: spec §3 표)*
- [ ] **macOS / Linux 배포** — 현재 Windows exe만. 타 OS 패키징은 미정.

### 수집 신뢰성
- [ ] **자동 수집 트리거** — 현재 ingest는 수동/앱 기동 시 1회. SessionEnd hook 등으로 백그라운드
  자동화. *(주의: 메모리상 기존 hook이 깨진 경로를 가리켜 미동작 — 재설계 필요)*
- [ ] **부분 라인(미완성 flush) 처리 강화** — 현재 PoC는 라인 단위 flush 가정. *(출처: `parser.py:121` 주석)*

### 확장
- [ ] **신규 도구 파서** — Gemini CLI 등. `codex_parser.py` 패턴으로 커뮤니티/후속.
  *(출처: PRD Non-goals, [DATA-MODEL.md](DATA-MODEL.md) 파서 가이드)*
- [ ] **config 홈 경로 정리** — exe는 이미 `~/.tokenomy/`. 소스 실행도 옵션으로 홈 경로 지원할지 검토.
  *(출처: spec §1 "후속")*

### 집계 / 성능
- [ ] **Python 필터 → SQL 집계 이전** — 데이터 규모가 커지면. 현재는 메시지를 Python에서 필터.
  v0.2.0의 신규 뷰가 늘면 enabler로 우선순위 상승. *(출처: `aggregate.py:4` 주석)*

### 기능
- [ ] **세션 수동 라벨(업무 귀속) 편집 UI + 라벨별 집계** — `sessions.label` 컬럼·표시는 있으나 편집 화면 미구현.
  라벨은 raw 추출이 아닌 사용자 입력이라 v0.2.0에서 제외(2026-06-17 그릴) — 입력 UI와 `dim="label"` 집계를 함께.
- [ ] **server tool(web_search/web_fetch) 집계·추이** — Claude 로그엔 있으나 Codex 도구 부재로 parity 실패,
  v0.2.0 제외(2026-06-17 그릴). provider 단일(Claude) 지표로 낼지 추후 판단.
- [ ] **단가 최신화 워크플로** — 단가 변동 시 `pricing.json` 갱신을 쉽게(README 안내 + 가능하면 보조 도구).
- [ ] **단가 정밀도 — long-context tiered + cost-mode** *(2026-06-17 가격 검토 보류, 출처: ccusage 보고서 §3.3)*:
  - **200K tiered 단가** — Sonnet 계열은 입력 200K 초과 시 long-context 프리미엄(input·output 단가 상향)이 있으나
    현재 `pricing.json`은 단일 단가라 미표현. **실데이터 영향 0**(전체 max input 134,770 · `>200K` 0행 · sonnet max 10,174 —
    Claude Code는 컨텍스트를 `cache_read`로 흘리고 fresh `input_tokens`만 남겨 200K를 거의 못 넘음).
    1M 컨텍스트 상용화로 fresh input이 커지면 `pricing.json`에 `above`/threshold 필드 + `compute_cost` tiered 분기 추가.
    *(opus·haiku는 long-context 차등 없음. 트리거가 "총 컨텍스트(cache 포함)>200K"인지 "fresh input>200K"인지는 공식 문서 재확인 필요.)*
  - **cost-mode 분리(display 감사)** — 현재 항상 calculate(토큰×단가, `maybe_reprice`와 정합). 로그의 공식 `costUSD`와
    자체 계산을 나란히 보여주는 display 모드는 감사용으로 유용하나, Claude Code가 `costUSD`를 잘 안 남겨 실익 낮음 → 보류.
  - **Codex `speed=fast` 배수** — fast 모드 별도 과금 시 단가 배수 필요. 현재 codex 데이터 미미(6행)·GPT-5.x 적용 여부 불확실 → 인지만.

## v0.2.0 이후 (고도화 — Insight 추출)

> raw 데이터 가시화가 자리 잡은 뒤. "정보를 보여주기"를 넘어 "해석·제안"하는 단계.

- [ ] **효율 분석·코칭 강화** — 현재 효율 코치 카드를 넘어선 패턴 진단.
- [ ] **효율 코치 휴리스틱 캘리브레이션** — `INSIGHT_CACHE_READ_MIN`(0.30)·`INSIGHT_WEB_SEARCH_MAX`(50)는
  실데이터 보정 전 튜닝값. *(출처: `aggregate.py:18`)*
- [ ] **이상 탐지 / 절감 제안 / 비용 예측** — 급증 감지, 모델·캐시 사용 최적화 제안 등.

## 의도적 비목표 (재확인 — 당분간 안 함)

- 팀/멀티유저 집계 (1머신 1사용자 유지)
- 구독 정액제 전용 UI (종량제 우선)
- 통화 환산 / 클라우드 동기화 / 계정·인증
