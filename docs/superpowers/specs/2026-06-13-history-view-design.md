# 내역(History) 화면 설계 — 가계부식 일별 토큰 지출

- 날짜: 2026-06-13
- 상태: 승인됨(구현 대기)
- 범위: `/history` 신규 페이지 1개. 적재(parser/db)는 손대지 않음 — 읽기 경로만 추가.

## 배경 / 목적

가계부 앱의 "내역" 화면처럼 토큰 지출을 **일별 흐름**으로 보여주는 화면을 추가한다.
가계부는 "거래 1건 = 1행"이지만 Tokenomy의 세션은 여러 날에 걸칠 수 있어,
**한 행 = (날짜 × 세션)** 단위로 쪼갠다. 같은 세션이 여러 날 이어지면 날짜마다 한 행씩 등장한다.

기존 `/sessions`(복기 — 세션 1행, "이 세션 전체 얼마")와 용도가 다르다.
`/history`는 "이 **날** 뭐에 얼마 썼나"에 답한다. 둘은 공존한다.

### 세션 비용 메커니즘 — 이 화면이 드러내려는 것

"세션을 오래 유지하면 비용"이라는 직관은 절반만 맞다.

- 세션을 그냥 열어두는 것(idle)은 0원. 토큰은 메시지를 주고받을 때만 과금된다.
- 진짜 비용 동인은 **컨텍스트 크기**다. 세션이 길어지면 매 턴 누적 대화가 입력 토큰으로
  다시 들어가지만, 프롬프트 캐싱(`cache_read`, 정가의 ~10%)으로 재전송분은 싸게 처리된다.
- "한참 뒤 재개하면 다시 읽어오는 비용"은 **캐시 만료** 얘기다. Anthropic 프롬프트 캐시
  TTL은 기본 5분(확장 1시간). 만료 후 같은 (큰) 세션을 재개하면 캐시가 깨져 전체 컨텍스트를
  `cache_creation`(정가의 1.25배)로 다시 올린다 — 이때 비용이 튄다.

결론: 시간 자체가 아니라 **"큰 컨텍스트 × 캐시 미스"**가 비용. Tokenomy는 `cache_creation`/
`cache_read`를 메시지별로 이미 추적하므로, 날짜+세션으로 쪼개면 "세션 첫 등장일의 정상 캐시
쓰기"와 "이어진 날의 재개 캐시미스"를 구분해 드러낼 수 있다. 이게 단순 가계부 베끼기를 넘는
Tokenomy다운 가치다.

## 핵심 결정 요약

| 항목 | 결정 | 비고 |
|------|------|------|
| 행 단위 | **날짜 × 세션** | 세션이 N일 걸치면 N행 |
| 화면 배치 | **신규 페이지 `/history`** | `/sessions` 복기는 그대로 |
| 캐시 강조 | **신호로만** | `cache_creation`을 "낭비"로 단정하지 않음 |
| 날짜 표현 | **날짜별 그룹 + 일별 소계** | 평면 표보다 한 발 더 |
| AI 선택 | **드롭다운 필터 + 부분 갱신** | 탭의 전체 리로드 깜빡임 제거 |
| 기간 토글 | **제거(월 고정)** | 일별 소계가 이미 있음. 주간 소계는 향후 |
| 정렬 | **5종 토글** | 2종 그룹 유지 + 2종 평면 전환 |
| 집계 위치 | **`aggregate.py` Python 집계** | 기존 방식과 일관(SQL 이전은 규모 커지면) |

## §1 아키텍처 / 데이터 흐름

기존 단방향 파이프라인에 **읽기 경로 하나만** 추가한다. `messages`에 메시지별
`ts·session_id·cache_*`가 이미 다 있으므로 집계만 새로 짠다.

```
messages (ts, session_id, cost, cache_creation, cache_read)
  └─ aggregate.by_day_session(conn, provider, start, nxt)   ← 신규
        · 메시지를 (KST날짜, session_id)로 버킷팅
        · 세션별 "최초 등장일"(messages 전체 MIN(ts)) 계산 → 이어짐(↩) 판정
        · 이어진 행 & 캐시율 낮음 → 캐시미스(⚠) 플래그
     → list[DaySessionRow]
  └─ views.history_context(...)                              ← 신규
        · 그룹 정렬(date_desc/date_asc/day_cost): 날짜별 DayGroup + 일별 소계
        · 평면 정렬(cost/cache): 그룹 해제, 단일 정렬 리스트
  └─ app.py  GET /history                                   ← 신규 라우트(얇게: provider/sort/anchor/partial 검증)
  └─ templates/history.html + _history_rows.html(fragment)  ← 신규 템플릿
  └─ 대시보드 세션 미리보기 옆 "전체 내역 보기 →" 링크         ← 진입점
```

### 신규 데이터클래스

```python
@dataclass
class DaySessionRow:        # 한 행 = (날짜 × 세션)
    date: str               # "2026-06-13" (KST)
    session_id: str
    provider: str | None
    summary: str | None     # 작업요약(aiTitle 캐시)
    project: str | None
    label: str | None
    cost: float
    msgs: int
    cache_ratio: float
    is_continued: bool      # 세션 최초등장일보다 이후 날짜인가 → ↩
    cache_miss: bool        # is_continued AND cache_ratio < 임계 → ⚠

@dataclass
class DayGroup:             # 날짜별 묶음(그룹 모드 전용)
    date: str
    weekday: str            # '금'
    subtotal: float
    rows: list[DaySessionRow]
```

계층 분리(라우트 얇게 ↔ views 조립 ↔ aggregate 집계)와 KST 경계 규칙(`parse_ts`)을 그대로 따른다.

## §2 화면 구성

```
🪙 Tokenomy · 2026-06 (KST) · me        데이터 최신: 06-13 14:22  [⚙설정] [↻새로고침]
─────────────────────────────────────────────────────────────────────
[전체 ▾]                                          [정렬: 날짜 최신순 ▾]

  내역 — 일별 토큰 지출
  ‹ 이전        2026-06        다음 ›
  2026-06 · 12건 · 합계 $14.80

  ━ 06-13 (금) ───────────────────────────────────── 합계 $2.50
     AI      작업요약          프로젝트    라벨    비용    메시지  캐시%
     claude  세션표 칸 추가    tokenomy   업무    $2.10    48     78%      ▸
     codex   리팩터 검토       project-b       —       $0.40    12     55%      ▸
  ━ 06-12 (목) ───────────────────────────────────── 합계 $1.30
     claude  세션표 칸 추가 ↩  tokenomy   업무    $1.30    9      40% ⚠    ▸
  ━ 06-11 (수) ───────────────────────────────────── 합계 $0.90
     claude  파서 버그 수정    tokenomy   업무    $0.90    21     81%      ▸
```

### 칸 매핑 (가계부 → Tokenomy)

| 가계부 | Tokenomy | 비고 |
|--------|----------|------|
| 날짜 | 그룹 헤더 `━ 06-13 (금)` + 소계 | KST. 평면 모드에선 한 칸으로 부활 |
| 자산 | AI(provider) | 돈이 나간 곳 |
| 분류 | 프로젝트 | 경로 마지막 폴더명 |
| 내용 | 작업요약(summary) | 가장 넓은 칸, `↩` = 이어진 세션 |
| 메모 | 라벨(수동 귀속) | 없으면 흐린 `—` |
| 금액 | 비용 | 우측정렬 강조 |
| — | 메시지 / 캐시% | 보조 신호, `⚠` = 재개 캐시미스 |
| — | `▸` | `/session/{id}` 상세 드릴다운 |

`sessions.html`이 이미 좁은 화면 반응형(라벨/메시지 칸 우선순위 낮춤)을 하므로 동일 규칙을 따른다.

### AI 드롭다운 + 부분 갱신

탭은 매번 전체 페이지를 다시 그려 깜빡인다. 드롭다운 + 약간의 vanilla JS로 표만 교체한다.

- 드롭다운(`전체/Claude/Codex`)·정렬 셀렉트의 `onchange`
  → `fetch('/history?provider=...&sort=...&anchor=...&partial=1')`
- 서버는 `partial=1`이면 **표 영역 fragment만**(`_history_rows.html`) 렌더
- JS가 표 컨테이너 `innerHTML`만 교체 + `history.pushState`로 URL 동기화(북마크·뒤로가기 유지)
- 라이브러리 없음(HTMX 등 미도입) — CLAUDE.md "의존성 최소" 유지
- JS 비활성/직접 URL 진입 시에도 동작하도록, 같은 라우트가 `partial` 없으면 전체 페이지를 렌더(점진적 향상)

### 정렬 (5종)

| 정렬 값 | 모드 | 화면 |
|---------|------|------|
| `date_desc` | 그룹 | 날짜 최신순 (기본) |
| `date_asc` | 그룹 | 날짜 오래된순 |
| `day_cost` | 그룹 | 일별 소계 큰 날 위로 |
| `cost` | 평면 | 그룹 해제 → 날짜 칸 부활, 세션 비용 내림차순 |
| `cache` | 평면 | 그룹 해제 → 캐시% 낮은 세션 위로(재개 미스 색안) |

템플릿은 `is_grouped` 플래그로 그룹/평면을 분기한다. 월 범위는 모든 정렬에서 동일.
**그룹 모드의 그룹 내부 행은 항상 비용 내림차순**(그룹 순서만 정렬 값에 따라 바뀜).
**월 네비(`‹ 이전 · 다음 ›`)는 전체 페이지 링크**(부분 갱신 아님) — `fetch` 부분 갱신은
`provider`·`sort` 변경에만 적용한다(월 이동은 빈도가 낮고 URL 전체가 바뀌므로 단순 링크가 깔끔).

## §3 판정 규칙 / 엣지 케이스

1. **이어짐(↩):** 세션의 최초 등장일(그 세션 **전체 messages**의 `MIN(ts)`를 KST 날짜로)보다
   현재 행 날짜가 이후면 `is_continued=True`. 조회 범위(이번 달)가 아닌 **전체**에서 `MIN(ts)`를
   구해야 지난달 시작→이번 달 이어짐을 "오늘 시작"으로 오판하지 않는다.
2. **캐시미스(⚠):** `is_continued AND cache_ratio < 0.30`(`INSIGHT_CACHE_READ_MIN` 재사용).
   **첫 등장일은 캐시율이 낮아도 절대 ⚠ 안 함**(첫 캐시 쓰기는 정상). 단정이 아닌 **의심 신호**.
   툴팁: "이어진 세션인데 캐시 재사용률 낮음 — 재개로 컨텍스트 재구축 가능성".
3. **비용 귀속:** 각 날짜 행 비용 = 그날 메시지 `cost` 합. 세션 통합 비용은 날짜 행 합으로
   자연히 떨어짐 — **인위적 분배 없음**(실제 메시지 `ts` 기준).
4. **날짜 버킷팅:** `ts`(UTC) → `parse_ts`로 KST 변환 후 `.date()`. 자정 근처도 KST로 정확히 귀속.
5. **결측:** 작업요약 없음 → `—`, 프로젝트 없음 → `(unknown)`, 라벨 없음 → 흐린 `—`.
6. **입력 검증:** `sort` 화이트리스트(`date_desc/date_asc/day_cost/cost/cache`), 밖이면 `date_desc`
   폴백. `provider`는 `claude/codex`, 그 외 전체. `partial`은 `1`만 인정. (기존 라우트 검증 패턴.)
7. **빈 데이터:** 그룹/행 없으면 "이 기간 데이터 없음".

## §4 테스트

기존 패턴(`pytest` + in-memory DB + `TOKENOMY_CONFIG` 격리) 그대로.

- **`aggregate.by_day_session`**
  1. 2일 걸친 세션 → 2행, 2일차 `is_continued=True`
  2. 첫날은 캐시율 낮아도 `cache_miss=False`
  3. 이어진 날 캐시율<0.30 → `cache_miss=True`
  4. 월 경계: 지난달 시작 세션의 이번 달 행이 `is_continued=True`
  5. KST 날짜 버킷팅(UTC 자정 넘김)
  6. provider 필터
  7. 빈 결과
- **`views.history_context`**: 그룹 모드 소계 정확 / 평면 모드(`cost`,`cache`) 그룹 해제 + 정렬 순서 / 5종 정렬 분기.
- **`app /history`**: `sort` 폴백, `partial=1` → fragment만 응답(전체 페이지 chrome 없음), provider 검증.

## 비목표 (Non-goals)

- 적재 경로(parser/db) 변경 없음.
- 주간/월간 소계 헤더(향후 옵션).
- 메시지 단위 행, 대화 원문 표기(프라이버시 경계 — 토큰 메타만).
- 한 행의 비용을 세션 전체로 분배/재계산(실제 ts 귀속만).

## 향후 / 미해결

- 주간 소계 그룹(요청 시 추가).
- `/history`로의 상단 화면 네비(현재는 대시보드 링크 진입) — 화면 종류가 4개가 되면
  공통 화면 네비 도입 검토.
