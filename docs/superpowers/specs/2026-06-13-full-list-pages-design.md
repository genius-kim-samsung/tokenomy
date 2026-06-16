# 전체 목록 페이지 + 기간 선택기 설계

- 날짜: 2026-06-13
- 상태: 승인 (구현 대기)
- 작성: 브레인스토밍 세션 (`/superpowers:brainstorming`)

## 배경 / 문제

대시보드의 일부 표가 Top 10으로 잘려 있어 전체를 볼 수 없다.

| 위치 | 표 | 현재 | 근거 |
|---|---|---|---|
| overview (`overview.html`) | 통합 프로젝트별 비용 | `projects[:10]` | `views.py:80` |
| overview | 복기 — 최근 비싼 세션 | `by_session(limit_n=10)` | `views.py:81` |
| AI별 상세 (`dashboard.html`) | 복기 — 최근 비싼 세션 | `by_session(limit_n=10)` | `views.py:27` |
| AI별 상세 | 프로젝트별 비용 | 전체(제한 없음) | `views.py:26` |

또한 모든 집계가 **"현재 월" 하드코딩**이라 지난 기간을 회고할 수 없다
(`month_bounds(now)` → `_month_rows()` → 모든 집계 함수).

## 목표

1. 잘려 있는 3곳을 전체로 볼 수 있는 **별도 전용 페이지** 제공 (`/projects`, `/sessions`).
2. 전용 페이지에 **기간 선택기**(일간/주간/월간 + 과거 탐색)를 붙여 과거 회고 지원.
3. 회고 동선 강화: 프로젝트→세션 드릴다운, 세션 정렬(비용순/최신순), 합계·건수 요약.

## 비목표 (YAGNI)

- 번다운/예산에 일·주 단위 적용 — 예산은 본질적으로 월 한도라 부적합. 메인 대시보드의 번다운은 **월 단위 그대로 유지**.
- 검색 박스(이번 범위 미선택), 페이지네이션, 임의 날짜 범위(start–end picker).
- 월 네비게이션을 메인 대시보드에 추가 — 전용 페이지에만 둔다.

## 사용자 결정 사항

- 인터랙션: **별도 전용 페이지** (인라인 토글/JS 더보기 아님).
- 적용 범위: 잘린 3곳 모두.
- 추가 기능: 드릴다운 ✅, 세션 정렬 토글 ✅, 합계 헤더 ✅, 검색 박스 ❌(미선택).
- 날짜 모델: **기간 토글 + 과거 탐색**, 기본값 = 이번 달.
- 주 시작: **월요일(ISO)**.
- AI별 상세 탭의 프로젝트 표: **Top 10 미리보기 + 링크로 변경**(현행 전체 표시에서).

## 라우트 설계

```
GET /projects?period=month&anchor=2026-06-13&provider=&sort=cost
GET /sessions?period=month&anchor=2026-06-13&provider=&order=cost&project=
```

| 파라미터 | 값 | 기본 | 비고 |
|---|---|---|---|
| `period` | `day` \| `week` \| `month` | `month` | 화이트리스트 밖 → 기본 |
| `anchor` | `YYYY-MM-DD` | 오늘(KST) | "그 날짜가 속한 일/주/월". 파싱 실패 → 오늘 |
| `provider` | `claude` \| `codex` \| (빈값) | 빈값=전 AI 합산 | 진입 탭 맥락 유지 |
| `sort` | `cost` \| `sessions` \| `cache` | `cost` | projects 전용, 기존 재사용 |
| `order` | `cost` \| `recent` | `cost` | sessions 전용 |
| `project` | 문자열 | 없음 | sessions 드릴다운 필터 |

- 모든 잘못된 입력은 화이트리스트 fallback (기존 `app.py`의 `_SORTS`/`provider in PROVIDERS` 패턴 답습, `test_dashboard_bad_query_falls_back` 동일 철학).
- 네비게이션 링크는 현재 `period`·`provider`·`sort`/`order`·`project`를 승계하고 `anchor`만 ±1 기간 이동.

## 집계 계층 리팩터링 (`aggregate.py`)

핵심: "현재 월" 하드코딩을 **임의 기간 `[start, nxt)`** 로 일반화하되, 기존 호출부(번다운·추세·코치)는 하위호환으로 무변경.

1. **신규 `period_bounds(period, anchor_kst) -> (start, nxt, label)`**
   - `day`: `start` = anchor 00:00 KST, `nxt` = +1일. label `2026-06-13 (금)`.
   - `week`: `start` = anchor가 속한 ISO주의 월요일 00:00 KST, `nxt` = +7일. label `2026-06-09 ~ 06-15`.
   - `month`: 기존 `month_bounds(anchor)`를 그대로 호출해 `(start, nxt)` 획득. label `2026-06`.
   - `month_bounds`는 **변경하지 않는다**(기존 호출부 보호). `period_bounds`가 월 케이스에서 내부 호출만 한다.
2. **`_range_rows(conn, provider, start, nxt)`** — 현재 `_month_rows` 본체(컬럼 SELECT + KST 변환 + 기간 필터)를 추출.
   - `_month_rows(conn, provider, now)` = `_range_rows(conn, provider, *month_bounds(now)[:2])` 로 재정의 → `burndown`/`daily_series`/`insights`는 **무변경**.
3. **`by_project` / `by_session`에 선택적 `start`/`nxt` 추가**
   - 둘 다 주어지면 `_range_rows(conn, provider, start, nxt)` 사용, 아니면 기존 `_month_rows(conn, provider, now)`.
   - 기존 시그니처/호출부 영향 없음(하위호환).
   - `by_session`은 이미 `order`(`cost`/`recent`)와 `project` 필터를 지원하므로 그대로 활용(`aggregate.py:184-235`).

## 기간 모델 세부

- 타임존 **KST** (앱 관례), 일 경계 = KST 자정.
- 주 = **월요일 시작**, 월–일 7일.
- `다음 ›`는 anchor의 기간이 **현재 기간 이상이면 숨김**(미래 데이터 없음). `‹ 이전`으로 과거 회고.
- 데이터 없는 과거 기간 → 표 본문에 "이 기간 데이터 없음" (기존 `{% else %}` 빈행 패턴 재사용).

## UI / 템플릿

### 신규 페이지
- `templates/projects.html`, `templates/sessions.html` — `base.html` 확장, `_tabs.html` 포함(상단 탭바·새로고침·설정 일관).
- 상단 컨트롤 영역:
  - `[일간 | 주간 | 월간]` 토글 (현재 `period` 활성 표시)
  - `[‹ 이전 | <label> | 다음 ›]` 기간 네비게이션
  - **합계 헤더**: `2026-06 · 전체 23개 프로젝트 · 합계 $X` (기간·필터 반영 총합)
- 표 구조는 기존 overview/dashboard의 `table.grid`와 동일한 컬럼 유지(일관성).
- `/sessions`: 추가로 `[비용순 | 최신순]` 정렬 토글(`order`).
- `/projects`: 프로젝트명을 `/sessions?project=<name>`(+현재 `period`/`anchor`/`provider` 승계) 링크로.

### 기존 대시보드 표 변경 (미리보기 + 링크로 통일)
- overview 프로젝트(Top 10) → `전체 보기 →` `/projects`
- overview 세션(Top 10) → `전체 보기 →` `/sessions`
- AI별 세션(10개) → `전체 보기 →` `/sessions?provider=X`
- **AI별 프로젝트 표**: 현행 전체 표시 → **Top 10 미리보기 + `전체 보기 →` `/projects?provider=X`** (`views.py:26`을 `by_project(..., limit_n=10)`로 변경 — 함수가 이미 `limit_n` 지원)

## 추가 기능 상세

- **드릴다운**: `/projects` 행의 프로젝트명 클릭 → 해당 프로젝트 세션만. 백엔드 `by_session(project=...)` 이미 지원.
- **세션 정렬 토글**: 비용순(기본) ↔ 최신순. 백엔드 `order` 이미 지원.
- **합계·건수 요약 헤더**: 현재 기간·`provider`·`project` 필터 적용 후의 행 수와 비용 합계.

## 검증 / 엣지 케이스

- `period`/`provider`/`sort`/`order` 화이트리스트 밖 → 기본값 fallback, 200 유지.
- `anchor` 파싱 실패/빈값 → 오늘(KST).
- 월말·연말 경계: `period_bounds` 월간이 12월→1월 롤오버 처리(기존 `month_bounds` 로직).
- 주간이 월/연 경계를 가로지르는 경우(예: 6/30 속한 주) 라벨/집계 정확.
- 빈 기간(데이터 0) → 빈행 안내, 합계 $0.00, `다음 ›` 규칙 정상.

## 테스트 계획

`test_aggregate.py`:
- `period_bounds` 일/주/월 경계 단위 테스트(월요일 시작, 월말/연말 롤오버, 주가 월 경계 가로지름).
- `by_project`/`by_session`에 `start`/`nxt` 전달 시 해당 기간만 집계.
- 기존 월 단위 호출부 회귀 없음(`_month_rows` 동치).

`test_web.py`:
- `/projects`·`/sessions` 200 (빈 DB / 데이터 있음).
- 잘못된 `period`/`provider` → fallback 200.
- 과거 `anchor` → 과거 기간 데이터 노출, 현재 기간서 `다음 ›` 부재.
- 드릴다운 링크(`/sessions?project=`) 렌더, 세션 정렬 토글 양방향, 합계 헤더 값 정확.
- 대시보드 표에 `전체 보기` 링크 존재, AI별 프로젝트 표가 Top 10으로 제한.

## 영향받는 파일

- `tokenomy/aggregate.py` — `period_bounds` 신규, `_range_rows` 추출, `by_project`/`by_session` 기간 파라미터.
- `tokenomy/web/views.py` — `projects_context`/`sessions_context` 신규, overview/dashboard 컨텍스트에 미리보기 제한·링크.
- `tokenomy/web/app.py` — `/projects`·`/sessions` 라우트 + 입력 검증.
- `tokenomy/web/templates/projects.html`, `sessions.html` — 신규.
- `tokenomy/web/templates/overview.html`, `dashboard.html` — `전체 보기` 링크, AI별 프로젝트 Top 10.
- `tests/test_aggregate.py`, `tests/test_web.py` — 위 테스트.
