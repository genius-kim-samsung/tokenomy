# 설계: 웹 대시보드 (Task 8)

> 작성일: 2026-06-11 · 프로젝트: tokenomy (토큰 가계부)
> 상위 문서: `상위 기획 문서`
> 참고: `docs/ccusage-분석-보고서.md`
> 갱신 2026-06-11: 시각화(추세선 Chart.js)·더블클릭 런처 보강 / export는 후속 분리 — 브레인스토밍 세션 정합(결정 1A·2B)
> 갱신 2026-06-11(2): 데이터 수집·보존 신뢰성(raw 30일 휘발 대응)은 **별도 문서로 분리** → `2026-06-11-data-ingestion-reliability-design.md` (브레인스토밍 결정 D1~D4)

## 1. 목표

이미 완성된 백엔드(`parser`/`codex_parser`/`pricing`/`db`/`tiers`/`aggregate`/`cli`)
위에, CEO 플랜이 정의한 **로컬 웹 대시보드**를 구현한다. CLI `report`가 터미널에
출력하는 내용을 브라우저에서 보고, 업무를 drill-down하고, 효율 신호로 복기한다.

핵심 가치 2개를 화면으로 옮긴다:
1. **번다운** — 티어 한도 대비 소진율 + "이대로면 N일에 소진"
2. **효율 복기** — 업무별 비용 + 캐시 활용 + 자동 진단 카드

## 2. 확정 결정

| # | 결정 | 선택 | 근거 |
|---|------|------|------|
| W1 | 페이지 구조 | **싱글 페이지 스크롤** | PoC 1인 · "한눈에 다 본다" · 라우트 1개로 최소 코드 |
| W2 | 기술 수준 | **Jinja2 SSR + CSS, 추세선만 Chart.js(vendored)** | 막대·게이지·테이블은 CSS로(의존성 최소) · 일별 추세선은 CSS로 불가 → 로컬 번들 Chart.js 1개만. 폐쇄망 대비 **CDN 아닌 vendored**. (W2 완화 — 브레인스토밍 "풍부한 시각화" 동기 반영) |
| W3 | v1 범위 | 번다운+업무별(코어) + 효율코치 + 복기/drill-down + 새로고침 버튼 | 백엔드 준비도 + PoC 가치 |
| W4 | 티어 변경 UI | **보류** | PoC 1인은 `tiers.json` `default_user` 고정으로 충분. 멀티유저 갈 때 S 비용으로 추가 |
| W5 | drill-down | **마스터-디테일 (별 라우트 1개)** | 싱글 페이지는 유지, 상세만 필요 시 로드 |

기술 스택: **FastAPI + Jinja2 + uvicorn** (`requirements.txt`) + 일별 추세선용 **Chart.js
(로컬 vendored — `static/vendor/chart.min.js`, CDN 아님)**. 차트는 추세선 **1곳에만** 쓰고
번다운 게이지·업무별 막대·테이블은 CSS로 유지한다(W2). 빌드 스텝 없음(번들 파일 직접 포함).

## 3. 아키텍처

### 3.1 라우트

```
GET  /                     메인 대시보드 (싱글 페이지)
       ?provider=claude    provider 전환 (기본 claude · 화이트리스트: claude|chatgpt)
       ?sort=cost|sessions|cache   업무별 정렬 (기본 cost · 화이트리스트)
GET  /session/{id}         세션 상세 (drill-down)
POST /ingest               새로고침 → ingest 실행 → / 로 redirect(302)
```

### 3.2 파일 구조

```
tokenomy/web/
  __init__.py
  app.py              FastAPI 앱 + 라우트 (얇게 — 라우팅만)
  views.py            DB → 화면용 dict 조립 (라우트/집계 분리)
  templates/
    base.html         공통 셸 + CSS 링크
    dashboard.html    메인
    session.html      drill-down
  static/
    style.css         CSS 막대바·게이지·테이블
    vendor/
      chart.min.js    Chart.js 로컬 번들 (추세선 전용 · CDN 아님 · 폐쇄망 대비)
```

`app.py`는 라우팅만, 데이터 조립은 `views.py`로 분리해 핸들러가 비대해지지 않게 한다.

### 3.3 데이터 흐름

```
브라우저 ─GET /─▶ app.py ─▶ views.py ─▶ aggregate.py (burndown/by_project/신규)
                                    └─▶ db.py (connect, 세션 쿼리)
                            ◀─ dict ─┘
         ◀─ Jinja2 렌더 ──┘
[↻ 새로고침] ─POST /ingest─▶ cli.cmd_ingest 재사용 ─▶ redirect /
```

## 4. 화면 레이아웃

### 4.1 메인 대시보드

```
┌──────────────────────────────────────────────────────────┐
│ 🪙 Tokenomy     2026-06 (KST)  me · Budget │
│                            [ ↻ 새로고침 ]   마지막 갱신 14:22│
├──────────────────────────────────────────────────────────┤
│  번다운                                 [Claude] [ChatGPT]  │
│  Claude   $128.40 / $223  ███████░░░░░░  57.6%   ✅ OK      │
│  11/30일 경과 · 일평균 $11.67 · 예상 월말 $350  ⚠ 초과예상  │
│  → 이대로면 19일에 소진 (남은 19일)                         │
│  ⓘ 공개 API 단가 기준 추정 · 이 머신 데이터만               │
├──────────────────────────────────────────────────────────┤
│  일별 추세 (Chart.js)    $ ╱─── 누적 실제                   │
│                            ╱  ┄┄ 예산 페이스(한도÷일수×일)  │
│                          └──────────────────────── day      │
├──────────────────────────────────────────────────────────┤
│  ⚠ 효율 코치                                               │
│  • cache_read 22% — 컨텍스트 재구축 낭비 가능성             │
│  • web_search 84회 — 비용 영향 점검 권장                    │
├──────────────────────────────────────────────────────────┤
│  업무별 비용 (이번 달)       [정렬: 비용▼ | 세션 | 캐시]    │
│   $48.20 │ 캐시 71% │ 6 sess │ tokenomy           ▸  │
│   $22.10 │ 캐시 40% │ 3 sess │ (unknown)               ▸  │
├──────────────────────────────────────────────────────────┤
│  복기 — 최근 비싼 세션                                      │
│   06-10 14:00 │ $12.30 │ tokenomy │ [라벨 없음]    ▸  │
│   06-09 09:12 │ $ 8.40 │ project-b     │ [작업 라벨]   ▸  │
└──────────────────────────────────────────────────────────┘
```

### 4.2 세션 상세 (drill-down) `/session/{id}`

```
┌──────────────────────────────────────────────────────────┐
│ ← 대시보드    세션 상세                                     │
│ tokenomy · 06-10 14:00~15:30 · claude · $12.30 · 42msg│
├──────────────────────────────────────────────────────────┤
│ 모델별        claude-opus-4  $11.10   claude-haiku  $1.20  │
│ 토큰 분해     input 1.2M · cache_cr 320K · cache_rd 4.1M   │
│               output 96K · web_search 12 · web_fetch 3     │
└──────────────────────────────────────────────────────────┘
```

### 4.3 provider 처리

Claude·ChatGPT 예산을 둘 다 잡으면 한도가 있으나, 이 머신엔 Codex 로그가 거의 없어
ChatGPT는 대부분 $0이다. 기본은 Claude 표시 + 상단 토글로 전환, ChatGPT는 데이터가
없으면 "(이 머신에 Codex 로그 없음)"으로 명시한다. (CEO 플랜의 "이 머신 데이터만"
경계, undercount 정직 표기와 일치)

## 5. 신규 백엔드

기존 `aggregate.py` 패턴(`@dataclass` 행 + 함수)을 따라 추가한다.

```
by_session(conn, provider, now, limit_n)   복기 뷰용. messages GROUP BY session_id
   → SessionRow(session_id, project, label, cost, first_ts, last_ts, msgs, cache_ratio)
   기본 비용순 정렬, 상위 N

session_detail(conn, session_id)           drill-down용
   → 모델별 (model, cost, in/out/cache_cr/cache_rd 토큰), web_search/fetch 합, 기간

insights(conn, budget, now, provider)       효율 코치 카드 리스트
   → list[Insight(level, text)]   level = info|warn

daily_series(conn, provider, now)           일별 추세선용 (Chart.js)
   → list[DayPoint(day, cumulative_cost)]   1일~오늘 누적 실제 지출
     예산 페이스 라인(limit ÷ days_in_month × day)은 템플릿/뷰에서 계산.
     데이터는 dashboard.html에 JSON으로 임베드 → 클라 Chart.js가 렌더(별도 라우트 불필요).
```

`burndown`에는 `status` 필드 1개 추가 (`ok`/`warn`/`exceeds`). 임계 로직을 집계
쪽에 두어 단위 테스트가 쉽게 한다. (ccusage `blocks.rs`의 ok/warning/exceeds 차용)
경계 정의:

- `exceeds` — `spent >= limit` (이미 한도 소진)
- `warn` — `spent < limit` 이고 `projected > limit` (현 추세면 월말 초과 예상)
- `ok` — 그 외 (`projected <= limit`)

### 5.1 효율 코치 휴리스틱

문구는 신호형("~가능성", "점검 권장"), 임계값은 `aggregate.py` 상단 상수 +
"실데이터 캘리브레이션 전 튜닝값" 주석. (CEO 플랜 R: 단정 금지)

| 조건 | 카드 문구 | level |
|---|---|---|
| cache_read 비율 < 30% | "캐시 활용 N% — 컨텍스트 재구축 낭비 가능성" | warn |
| web_search 합 > 50회/월 | "web_search N회 — 비용 영향 점검 권장" | info |
| unpriced > 0 | "단가 미식별 N건 — 비용 누락 가능" | warn |
| projected > limit | "현 추세 월말 $X 예상 — 한도 초과 가능" | warn |

해당 신호가 없으면 "특이 신호 없음" 1줄(카드가 0개로 비지 않게).

### 5.2 비용 신뢰도 표기

ccusage 분석이 짚은 정확도 갭 + S1(요금 환산율 미확정) 때문에 숫자를 정직하게
표기한다. 번다운 하단에 작은 주석: "공개 API 단가 기준 추정 · 이 머신 데이터만".
`unpriced_count > 0`이면 배지로 노출. (ccusage "추정하지 않는다" 철학과 정합)

## 6. 에러 / 엣지 케이스

- **빈 DB**(ingest 전): 각 섹션 빈 상태 — "데이터 없음 · [↻ 새로고침]을 누르세요"
- **limit=0**(티어 base/미배정): "한도 미설정" 표시. 0 division은 기존 `burndown` 가드로 안전
- **ChatGPT 데이터 없음**: "(이 머신에 Codex 로그 없음)" 명시
- **존재하지 않는 session_id**: 404 페이지 ("세션을 찾을 수 없음 · ← 대시보드")
- **ingest 실패**(POST /ingest): 예외 잡아 대시보드 상단 에러 배너, 기존 데이터 유지
- **잘못된 `sort`/`provider` 쿼리**: 화이트리스트 검증 후 기본값 fallback

## 7. 테스트 (`tests/test_web.py` 신규, FastAPI TestClient)

- 라우트 스모크: `GET /` → 200 + 핵심 섹션 텍스트 / `GET /session/{없는id}` → 404 /
  `POST /ingest` → 302 redirect
- 신규 집계 단위: 인메모리 SQLite fixture → `by_session`·`session_detail`·`insights` 검증
- 엣지: 빈 DB에서 `GET /` 200(크래시 없이 빈 상태) / limit=0 티어

## 8. 실행 / 배포 — 더블클릭 런처

동료가 터미널을 의식하지 않고 쓰도록 서버 기동을 런처로 감싼다.
**"웹앱이지만 앱처럼 더블클릭으로 열린다."** (브레인스토밍 확정: 실행 마찰은 렌더링 기술이
아니라 패키징 문제 → 웹앱 + 런처로 네이티브 앱 UX를 가볍게 얻는다.)

- `start_tokenomy.bat` (저장소 루트) — 더블클릭 시:
  1. 시작 시 1회 증분 `ingest` (최신 상태로 열림)
  2. `uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765` 백그라운드 기동
  3. 기본 브라우저로 `http://127.0.0.1:8765` 자동 오픈
- **바인딩은 `127.0.0.1`(로컬 전용)** — 외부 노출 금지(프라이버시·보안).
- 포트 점유 시 다음 포트 탐색(8765→8766…) 후 그 주소로 오픈.
- 종료: 콘솔 창 닫기(PoC 수준). 트레이 상주는 후속.
- 전제: Python + `pip install -r requirements.txt` 완료(개발자 동료 가정).
  무설치 `.exe`(PyInstaller) 배포는 비개발자 포함 시 후속(§9).
- "상시 대시보드"는 런처로 띄워 두고 탭 유지 + [↻ 새로고침]으로 충족.
  자동 폴링/`meta refresh`는 YAGNI(필요 시 후속).

## 9. 범위 밖 (이번 task 아님)

- **백엔드 엔진 정확도 개선** — ccusage 보고서가 짚은 dedup 키 `(message_id, request_id)`,
  파싱 검증, 캐시 5m/1h 단가, Codex 날짜 귀속 왜곡. 대시보드가 보여주는 *숫자의 정확도*
  이슈이며 UI와 독립. **별도 백엔드 개선 task로 분리**한다. v1 대시보드는 현 집계를
  신뢰하되 §5.2로 한계를 정직하게 표기한다.
- **export / 리포트 굽기** — 요약 리포트(번다운 + 효율 카드)를 파일로 굽기 → 메일
  첨부로 "결과 공유". **산출물 포맷(HTML/PNG vs md·json·csv)은 후속 결정(TBD)** — 데이터
  레이어 문서(`2026-06-11-data-ingestion-reliability-design.md` §7)와 통일. 이번 task는
  *열람*에 집중하고 공유 수단은 **별도 task로 분리**(브레인스토밍 결정 2B). 발송 자체는
  사용자 기존 수동 메일 워크플로로 충분(자동 발송은 더 후속).
- 티어 변경 UI (W4), **추세선 외 차트 확대**(W2 — 게이지·막대까지 차트화), 멀티유저,
  무설치 `.exe`(PyInstaller) 배포, 이메일 자동 발송 — 모두 후속.

> **데이터 수집·보존 신뢰성**(raw 30일 휘발 대응 · 트리거 다중화 · 2계층 모델 · freshness ·
> 원문 아카이브)은 별도 문서로 분리했다 →
> [`2026-06-11-data-ingestion-reliability-design.md`](./2026-06-11-data-ingestion-reliability-design.md)
> (브레인스토밍 결정 D1~D4). 그 문서의 §5(freshness)는 본 문서 §4.1 헤더의 "마지막 갱신"을
> 대체하고 §6 엣지 케이스를 보강하며, §6(메타)은 §5 집계가 쓸 `attribution_skill`을 채운다.
