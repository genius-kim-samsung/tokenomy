# 내역(History) 화면 설계 — 날짜→폴더→세션 계위 트리

- 날짜: 2026-06-15
- 상태: 승인됨(구현 대기)
- 범위: `/history` 화면을 5탭 멀티뷰 → **단일 계위 트리** 한 화면으로 교체. 적재(parser/db) 무변경 — 읽기 경로만 손댐.
- 선행 설계: [2026-06-13-history-view-design.md](2026-06-13-history-view-design.md)(가계부식 일별 지출 — `by_day_session` 도입). 이 문서는 그 위에 계위 트리 UI를 얹는다.

## 배경 / 목적

현재 `/history`는 상단에 5개 탭(`세션별 / 폴더별 / 일별 / 주별 / 월별`)을 두고 보기를 바꾼다.
탭마다 표 모양·정렬·집계 단위가 달라 인지 부담이 있고, "오늘 어느 프로젝트의 어느 세션에
얼마 썼나"를 보려면 탭을 옮겨다녀야 한다.

가계부의 "날짜 펼치면 그 날의 분류, 분류 펼치면 거래"처럼, **하나의 표 안에서
날짜 → 폴더(프로젝트) → 세션** 계위로 한 번에 보여준다. 처음엔 전부 펼쳐져 있고
날짜·폴더 그룹을 접었다 펼칠 수 있다. 탭 전환·화면 이동 없이 전체 지출 구조를 한눈에 본다.

## 핵심 결정 요약

| 항목 | 결정 | 비고 |
|------|------|------|
| 화면 구조 | **단일 계위 트리** (5탭 전부 제거) | 세션별·폴더별·일별을 트리가 흡수, 주별·월별 롤업은 제거 |
| 계위 | **날짜 → 폴더(프로젝트) → 세션** | 리프 = (날짜 × 세션) |
| 표 레이아웃 | **별도 열 + 그룹 헤더 행** | 날짜/폴더는 접기 토글 달린 그룹행, 세션은 세부행 |
| 열 순서 | **날짜 · 폴더 · 세션ID · 작업요약 · 메시지 · 비용 · 캐시%** | 사용자 지정 |
| 추가 정보 | **AI 배지 · 그룹행 캐시%(가중평균) · 접힘 롤업 프리뷰** | 라벨/비중%는 v1 제외 |
| 접기/펼치기 | **vanilla JS + 이벤트 위임**(의존성 0) | 기본 전부 펼침. `<details>`·Alpine 탈락(근거 §4) |
| 기간 탐색 | **월 네비 유지**(`‹이전 / 2026-06 / 다음›`) | view 파라미터 제거 |
| AI 필터 | **provider 드롭다운 유지** | claude/codex/전체 |
| 정렬 | **3종**(날짜 최신순·오래된순·일별지출많은순) | 폴더·세션 내부는 항상 비용 내림차순 |
| 집계 위치 | **리프=aggregate, 그룹핑·소계=views** | 기존 계층 분리·`_group_by_date` 패턴 계승 |

## §1 아키텍처 / 데이터 흐름

기존 단방향 파이프라인을 그대로 따르되, history 읽기 경로만 트리용으로 바꾼다.
리프 데이터는 **기존 `aggregate.by_day_session()`를 재사용**한다 — 이미
`(KST날짜 × 세션)` 단위로 project·summary·msgs·cost·cache·`is_continued(↩)`·`cache_miss(⚠)`를
산출하므로 신규 집계가 거의 없다.

```
messages (ts, session_id, project, cost, cache_creation, cache_read, input_tokens)
  └─ aggregate.by_day_session(conn, provider, start, nxt)   ← 재사용(+필드 2개 추가)
        · (KST날짜, session_id) 버킷 → DaySessionRow
        · cache_read / cache_den 원시값을 행에 함께 실어 그룹 가중평균을 가능케 함
  └─ views.history_context(...)                              ← 트리 그룹핑으로 재작성
        · DaySessionRow 리스트 → DateGroup[ FolderGroup[ rows ] ] 2단 그룹핑
        · 날짜/폴더 소계(cost/msgs/가중 cache%) + 접힘 롤업 프리뷰 계산
        · 정렬 3종(날짜 그룹 순서만; 폴더·세션 내부는 비용 내림차순 고정)
  └─ app.py  GET /history                                   ← view 디스패치 제거, sort 화이트리스트 단일화
  └─ templates/_history_body.html + _history_rows.html      ← 탭 제거 + 트리 표로 재작성
  └─ static/js: 접기/펼치기 이벤트 위임(신규, 소량)
```

### 데이터클래스 변경

`DaySessionRow`에 그룹 가중평균 캐시% 계산용 원시값 2필드를 추가한다(현재는 비율만 있어 합산 불가).

```python
@dataclass
class DaySessionRow:        # 한 행 = (KST날짜 × 세션)
    date: str
    session_id: str
    provider: str | None
    summary: str | None
    project: str | None
    label: str | None
    cost: float
    msgs: int
    cache_ratio: float
    cache_read: int         # ★신규 — 그룹 소계 가중평균 분자
    cache_den: int          # ★신규 — 분모(input + cache_creation + cache_read)
    is_continued: bool
    cache_miss: bool
```

views.py 신규 그룹 dataclass(기존 `DayGroup`은 트리 도입 후 미사용 → 제거):

```python
@dataclass
class FolderGroup:          # 날짜 안의 폴더(프로젝트) 묶음
    project: str            # 표시용 폴더명((unknown) 포함)
    cost: float
    msgs: int
    cache_ratio: float      # 가중평균 = Σcache_read / Σcache_den
    preview: str            # 접힘 시 노출할 대표 작업요약 2~3개 요약
    rows: list[DaySessionRow]   # 세션 행(비용 내림차순)

@dataclass
class DateGroup:            # 날짜 묶음(최상위)
    date: str               # "2026-06-13"
    weekday: str            # '금'
    cost: float
    msgs: int
    cache_ratio: float      # 가중평균
    preview: str            # 접힘 시 대표 작업요약 요약
    folders: list[FolderGroup]  # 폴더 그룹(비용 내림차순)
```

계층 분리(라우트 얇게 ↔ views 조립 ↔ aggregate 집계)와 KST 경계(`parse_ts`)를 그대로 따른다.

## §2 화면 구성

```
🪙 Tokenomy · 2026-06 (KST) · me                  데이터 최신: 06-15 14:22  [⚙] [↻]
──────────────────────────────────────────────────────────────────────────────
[전체 ▾]   [정렬: 날짜 최신순 ▾]   [⊟ 모두 접기]            ‹ 이전   2026-06   다음 ›
2026-06 · 12건 · 합계 $14.80

 날짜         폴더        세션ID   작업요약            메시지   비용     캐시%
 ──────────────────────────────────────────────────────────────────────────
 ▾ 06-13(금)                                            60    $2.50    74%
    ▾         tokenomy                                  55    $2.30    78%
                        a3f1 ▸  세션표 칸 추가          48    $2.10    78%
                        b7e9 ▸  캐시 분석               7     $0.20    65%
    ▾         project-b                                      5     $0.20    44%
                        c1d4 ▸  리팩터 검토 ↩           5     $0.20    40% ⚠
 ▾ 06-12(목)                                            9     $1.30    40%
    ▾         tokenomy                                  9     $1.30    40%
                        a3f1 ▸  세션표 칸 추가 ↩        9     $1.30    40% ⚠

 (접힘 예시)
 ▸ 06-11(수)  · 세션표 칸 추가, 파서 버그 수정 …       30    $0.90    81%
```

### 행 3종

| 행 유형 | 날짜칸 | 폴더칸 | 세션ID/작업요약 | 메시지·비용·캐시% | 비고 |
|---------|--------|--------|------------------|-------------------|------|
| **날짜 그룹행** | `▾ 06-13(금)`(caret) | — | (접힘 시) 롤업 프리뷰 | 소계(가중 cache%) | 클릭 시 그날 전체 접기 |
| **폴더 그룹행** | — | `▾ tokenomy`(들여쓰기·caret) | (접힘 시) 롤업 프리뷰 | 소계 | 클릭 시 그 폴더 접기 |
| **세션행** | — | — | `a3f1 ▸` + 작업요약(+`↩`/`⚠`) | 행값 | `a3f1` = 짧은 id, `/session/{id}` 링크 |

- **세션ID**: 풀 UUID는 길어 앞 4~6자만 모노스페이스로 표기하고 `/session/{id}` 상세로 링크(`▸`).
- **AI 배지**: 세션행에서 세션ID 옆(작업요약 시작 전)에 `claude`/`codex` 작은 색 배지.
- **신호**: `↩`=이어진 세션(전체 첫 등장일 이후 날짜), `⚠`=재개 캐시미스. 작업요약 옆 인라인(데이터에 이미 있음).
- **결측**: 작업요약 없음 → `—`, 프로젝트 없음 → `(unknown)`.
- **반응형**: `sessions.html`/기존 표의 좁은 화면 규칙(라벨·메시지 칸 우선순위 낮춤)을 동일 적용.

### 그룹 소계 / 캐시% 가중평균

- 날짜·폴더 그룹행의 **비용·메시지**는 자식 합.
- **캐시%는 가중평균** = `Σcache_read / Σcache_den`(자식 행의 원시값 합). 단순 비율 평균이 아니라
  토큰량 가중이라야 "캐시 낮아 비쌌던 날/폴더"를 정확히 드러낸다. (이 때문에 §1에서 원시값 2필드 추가.)

### 접힘 롤업 프리뷰

- 그룹을 **접으면** 헤더 행에 그 그룹의 **대표 작업요약 2~3개**(비용 상위)를 `, `로 이어 흐리게 표시 →
  접은 상태에서도 "뭐에 썼는지" 감을 준다. 펼치면 숨김.
- views에서 그룹별로 `preview` 문자열을 미리 계산(상위 N개 summary, 없으면 `(요약 없음)`).

### 상단 컨트롤 (`_history_body.html`)

- **view-seg 5탭 제거.**
- 월 네비(`‹ 이전 · {period_label} · 다음 ›`) 유지 — view 파라미터 제거, **전체 페이지 링크**(부분 갱신 아님).
- 필터 폼(htmx, `#history-body` swap 유지): `provider` 드롭다운 + `sort` 드롭다운(3종).
- **"모두 접기/펼치기" 토글** 버튼(vanilla JS) — 전체 그룹 일괄 토글.

### 정렬 3종

| 정렬 값 | 화면 |
|---------|------|
| `date_desc` | 날짜 그룹 최신순 (기본) |
| `date_asc` | 날짜 그룹 오래된순 |
| `day_cost` | 일별 소계 큰 날 위로 |

**폴더 그룹·세션 행 내부는 모든 정렬에서 비용 내림차순 고정**(그룹 순서만 정렬 값에 반응). 월 범위는 동일.

## §3 접기/펼치기 (인터랙션)

- **vanilla JS + 이벤트 위임** 채택(의존성 0, htmx 부분 swap 후 재초기화 불필요).
  - 표 컨테이너에 클릭 위임 리스너 1개. 그룹행 클릭 → 해당 그룹의 자식 행에 `hidden` 토글 +
    caret(`▾`↔`▸`) 회전 + 접힘 프리뷰 노출. 행은 `data-date`/`data-folder`로 부모-자식 매칭.
  - 날짜 그룹을 접으면 그 안의 폴더행·세션행 모두 숨김(자식 폴더의 개별 접힘 상태와 무관하게).
- **기본 전부 펼침**(요청). 접힘 상태는 새로고침/필터 변경(swap) 시 펼침으로 초기화 —
  "처음엔 다 펼쳐져"와 일관. localStorage 영속은 v1 제외(YAGNI).
- **탈락한 대안**:
  - `<details>/<summary>`: `<tbody>`를 `<details>`로 감쌀 수 없어 table 행 그룹 토글에 부적합.
  - Alpine.js: CLAUDE.md "실수요 시 추가"에 해당하나 단일 기능에 런타임 의존성 추가는 과함 — vanilla가 가볍다.
- htmx는 기존대로 vendored 사용(필터 swap). 접기 JS는 `static/`에 소량 추가(무빌드 유지).

## §4 라우트 / 링크 정리 (`app.py` + 템플릿)

- `app.py`:
  - `_HISTORY_VIEWS` / `_VIEW_SORTS` / `_VIEW_DEFAULT_SORT` 제거.
  - `/history` 핸들러에서 `view` 파라미터 제거, `sort` 화이트리스트 단일화(`date_desc/date_asc/day_cost`, 기본 `date_desc`), `project` 파라미터 제거.
  - 부분 갱신(HX-Request) 로직·`partial=1` 폴백·`anchor`/`provider` 검증은 유지.
  - `/projects`·`/sessions` 리다이렉트 → `/history`(view 파라미터 제거).
- 템플릿 링크: `overview.html`(3곳: ai-card, "전체 보기", "전체 내역 보기")·`session.html`(뒤로가기) 의 `?view=...` 제거 → `/history`(필요 시 `provider`만).
- `_sidebar.html`: 이미 `/history` — 변경 없음.

## §5 죽은 코드 정리 (이 변경으로 미사용화)

CLAUDE.md의 "건드리는 코드는 정리" 원칙에 따라, 트리 도입으로 더 이상 쓰이지 않는 것만 제거한다.

- **aggregate.py**: `by_week` / `WeekRow`, `by_month` / `MonthRow` 제거(history 전용 — overview 미사용).
  `DayGroup` 제거(트리는 `DateGroup`/`FolderGroup` 사용). `by_session`·`by_project`·`by_day_session`은 유지.
- **views.py**: `history_context`의 month/week/folder/session/day 분기 전부 제거 → 트리 단일 경로. `_group_by_date`는 2단(`DateGroup→FolderGroup`)으로 재작성. `_GROUPED_SORTS` 등 view용 보조 상수 정리.
- **_history_rows.html**: 5분기 제거 → 트리 표.
- `project` 필터 경로 전반 제거(폴더가 트리 레벨로 승격).

(주의: `by_week`/`by_month`가 다른 곳에서 쓰이지 않는지 구현 시 grep으로 재확인 후 제거.)

## §6 판정 규칙 / 엣지 케이스

1. **이어짐(↩)·캐시미스(⚠)**: 기존 `by_day_session` 규칙 그대로(세션 전체 `MIN(ts)`의 KST 날짜보다
   이 행 날짜가 이후면 `is_continued`; `is_continued AND cache_ratio < INSIGHT_CACHE_READ_MIN`이면 `cache_miss`).
   첫 등장일은 캐시율 낮아도 `⚠` 안 함. **의심 신호**일 뿐 단정 아님.
2. **그룹 캐시% 가중평균**: 분모(`Σcache_den`)가 0이면 `0%`로 표기(0 division 방지).
3. **날짜 버킷팅**: `ts`(UTC) → `parse_ts`로 KST 변환 후 `.date()`. 자정 근처도 KST 귀속.
4. **비용 귀속**: 각 날짜×세션 행 비용 = 그날 메시지 cost 합. 인위적 분배 없음(실제 ts 기준).
   날짜/폴더 소계는 자식 합으로 자연히 떨어짐.
5. **정렬 입력검증**: `sort` 화이트리스트 밖이면 `date_desc` 폴백. `provider`는 `claude/codex`, 그 외 전체.
6. **빈 데이터**: 그룹 없으면 "이 기간 데이터 없음".
7. **접힘 상태와 정렬/필터**: swap으로 표가 다시 그려지면 전부 펼침으로 초기화(의도).

## §7 테스트

기존 패턴(`pytest` + in-memory DB + `TOKENOMY_CONFIG` 격리) 그대로.

- **`aggregate.by_day_session`**: 기존 검증(2일 세션 2행/첫날 cache_miss 제외/이어진 날 미스/월경계/KST/provider/빈결과)
  유지 + **신규 `cache_read`·`cache_den` 필드값 정확성**.
- **`views.history_context`(트리)**:
  - 날짜→폴더→세션 2단 그룹핑 구조(폴더가 날짜 안에 정확히 묶임).
  - 날짜·폴더 소계(cost/msgs) 및 **가중 cache%**(분자/분모 합 기반) 정확.
  - 접힘 롤업 프리뷰: 상위 N개 작업요약 / 요약 없을 때 폴백.
  - 정렬 3종(`date_desc`/`date_asc`/`day_cost`) — 그룹 순서만 바뀌고 폴더·세션 내부는 비용 내림차순.
  - 빈 결과.
- **`app /history`**: 트리 렌더(view-seg 탭 없음 어서션) / `sort` 폴백 / `provider` 필터 /
  `partial=1`(또는 HX-Request) → `_history_body` 조각만 / `/projects`·`/sessions` → `/history` 리다이렉트.
- **기존 view별 테스트 갱신·제거**(session/folder/day/week/month 분기, project 필터 테스트).
- 프론트 접기 JS는 로컬 수동 확인(브라우저) — 자동화 범위 밖(무빌드·의존성 최소 유지).

## §8 비목표 (Non-goals)

- 적재 경로(parser/db) 변경 없음.
- 주간·월간 롤업 집계(트리에서 제거 — 필요 시 별도 화면으로 향후).
- 라벨 열·비중%(share) 열(v1 제외).
- 접힘 상태 영속(localStorage).
- 메시지 단위 행·대화 원문(프라이버시 경계 — 토큰 메타만).
- 한 행 비용을 세션 전체로 재분배(실제 ts 귀속만).

## §9 작업 순서(개략)

1. `aggregate.py`: `DaySessionRow`에 `cache_read`/`cache_den` 추가, `by_day_session`에서 채움.
   `by_week`/`WeekRow`/`by_month`/`MonthRow`/`DayGroup` 제거(grep 확인 후).
2. `views.py`: `FolderGroup`/`DateGroup` 추가, `history_context` 트리 단일 경로로 재작성,
   2단 그룹핑+소계+가중 cache%+프리뷰. view 분기·`project`·관련 상수 제거.
3. `app.py`: view/sort/project 검증 정리, 리다이렉트 수정.
4. 템플릿: `_history_body.html`(탭 제거·정렬 3종·접기 버튼), `_history_rows.html`(트리 표),
   `overview.html`·`session.html` 링크 수정.
5. `static/`: 접기/펼치기 JS(이벤트 위임) + `static/src/input.css`에 트리 컴포넌트 클래스 → `build_css.ps1` 재빌드(app.css 커밋).
6. 테스트 갱신/추가. `pytest` 통과.
7. 로컬 브라우저 수동 확인(펼침 기본·접기·필터 swap·반응형).
