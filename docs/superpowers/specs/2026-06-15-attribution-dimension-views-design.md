# 차원별 분석 뷰 (스킬·브랜치 귀속 + 서브에이전트 비중) 설계

- 날짜: 2026-06-15
- 상태: 설계 승인 대기 → 구현
- 관련: v0.2.0 §A (`docs/ROADMAP.md`), `aggregate.py:by_model`(496), `web/views.py:models_context`(126),
  `web/app.py`(/models 75·107), `web/templates/models.html`, `web/templates/_sidebar.html`

## 1. 배경 및 문제

`messages` 테이블에는 귀속 메타 컬럼 `attribution_skill`·`git_branch`·`is_sidechain`이
적재된다(`parser.py`가 Claude 로그에서 채움). 그러나 grep 결과 이 세 컬럼은 **어떤 집계·
화면에도 노출되지 않는다** — `is_sidechain`은 `db.py`의 dedup 우선순위에만, 나머지 둘은
저장만 되고 미사용이다.

v0.2.0 마일스톤(§A 최우선)은 "이미 수집되는데 안 보이는 필드부터 메운다"이다. 이 세 필드는
새 수집 없이 표현 계층만으로 노출 가능해 ROI가 가장 높다.

## 2. 목표 / 비목표

### 목표
- 기존 **모델별(`/models`)** 뷰를 **"차원별"** 으로 일반화. 차원 선택기 `{모델·스킬·브랜치}`로
  비용/토큰 롤업을 같은 화면에서 전환.
- **서브에이전트(`is_sidechain`) 비중** 을 차원별 페이지 상단 요약 카드로 노출
  (부모 vs 서브에이전트 비용·비중%, 현재 기간·provider·구간 필터 반영).
- **미귀속 버킷 명시 노출** — skill/branch가 NULL인 분량을 숨기지 않고 별도 행으로 표시.
- 새 라우트 `/analysis` 신설. `/models`는 `/analysis?dim=model`로 301 리다이렉트
  (기존 `/projects`·`/sessions`→`/history` 레거시 리다이렉트 패턴과 동형).
- 스키마·파서 무변경. `aggregate.py`(순수) + `views.py` + `app.py`(얇게) + 템플릿만.

### 비목표 (이번 작업 범위 밖)
- **server tool 사용량(`web_search`/`web_fetch`) 집계·추이** — A그룹이나 결(횟수 메트릭)이 달라
  별도 후속 spec.
- **차원별 시계열 추세 차트** — 현 `/models`도 기간 합계 테이블뿐. 추세는 차후(통합 추세 차트 패턴 재사용).
- 라벨별 집계(C그룹)·시간패턴/세션형태(B그룹)·세션 라벨 편집 UI.

## 3. 설계

### 3.1 집계 — `aggregate.by_dimension`

`by_model`(496)을 차원 파라미터화한다.

```python
DIM_COLUMNS = {"model": "model", "skill": "attribution_skill", "branch": "git_branch"}

def by_dimension(conn, provider, start, nxt, dim="model") -> list[DimensionRow]:
    col = DIM_COLUMNS[dim]   # 화이트리스트 dict로만 컬럼 결정 — 사용자 입력 SQL 직접 보간 금지
    ...
```

- 로직은 `by_model`과 동일: 전체(또는 provider) SELECT → `parse_ts`로 `[start, nxt)` 필터 →
  `col` 값 키로 cost/sessions/input/output/cache_creation/cache_read 합산 → 비용 내림차순.
  `cache_ratio` 분모는 `(it + cc + cr)`로 기존과 일치.
- 반환 `DimensionRow`는 `ModelUsageRow`와 동형이되 `model` 대신 일반 `key: str | None`.
  NULL 키는 **그대로 None으로 반환**(표시 라벨은 views 책임 — §3.6).
- `by_model`은 `by_dimension(dim="model")`을 호출하는 얇은 래퍼로 남기거나, 호출부를
  `by_dimension`으로 직접 교체. (테스트 영향 최소화 위해 래퍼 유지 권장.)
- `col`이 화이트리스트 dict의 값이므로 SQL 문자열에 안전하게 들어간다. `dim`이 dict에
  없으면 호출 전에 views에서 `model`로 fallback(§3.3).

### 3.2 집계 — `aggregate.sidechain_split`

```python
def sidechain_split(conn, provider, start, nxt) -> SidechainSplit:
    # is_sidechain 0/1로 부모·서브에이전트 비용·토큰 합산
```

- 반환: `parent_cost`, `sub_cost`, `total_cost`, `sub_share`(= sub/total*100, total=0이면 0.0),
  부가로 부모/서브 토큰 합. 기간·provider 필터는 `by_dimension`과 동일 규칙.
- Codex는 항상 `is_sidechain=0`(서브에이전트 개념 없음) → 전량 부모로 집계. §3.6 가용성 주석으로 보완.

### 3.3 화면 조립 — `views.dimension_context`

`models_context`(126)를 일반화.

- 시그니처에 `dim: str = "model"` 추가. **화이트리스트 fallback**: `dim if dim in DIM_COLUMNS else "model"`
  (기존 provider/sort/period fallback과 동형).
- `_resolve_range`(109)로 주/월·사용자 구간 해석 그대로 재사용.
- `rows = by_dimension(conn, provider or None, s, nxt, dim)` → 테이블 dict(비중% `share` 포함).
- `split = sidechain_split(...)` → 상단 카드용 컨텍스트.
- 컨텍스트에 `dim`, `dim_label`(모델/스킬/브랜치), `dim_options`(선택기 렌더용), `split`,
  `claude_only`(= dim in {skill, branch} 또는 split 표시 시 True), `active_nav="analysis"` 추가.
- 기존 `models_context`는 `dimension_context(dim="model")` 위임 또는 제거(/models 리다이렉트로 미사용).

### 3.4 라우트 — `web/app.py`

```python
@app.get("/models")
def models_redirect():
    return RedirectResponse("/analysis?dim=model", status_code=301)

@app.get("/analysis")
def analysis_view(request, anchor=None, provider="", dim="model",
                  period=None, start=None, end=None, notice=None):
    dim = dim if dim in DIM_COLUMNS else "model"
    provider = provider if provider in PROVIDERS else ""
    period = period if period in _PERIODS else "month"
    ...  # dimension_context 호출, analysis.html 렌더
```

- 검증·기본값 패턴은 기존 `models_view`(107) 그대로(정렬 없음 — `by_dimension`이 항상 비용 내림차순).
  `PROVIDERS`·`_PERIODS`·`DIM_COLUMNS`(aggregate에서 import) 상수 재사용.
- 리다이렉트는 파라미터 미보존(레거시 `/projects`·`/sessions` 리다이렉트와 동일 수준 — 북마크 호환만).

### 3.5 내비게이션 & 템플릿

- `_sidebar.html`: `모델별`(`/models`) 항목을 **`차원별`(`/analysis`)** 로 교체. `active_nav` 키 `models`→`analysis`.
- `models.html` → **`analysis.html`** 로 개편(렌더 대상이 /analysis뿐이므로 rename):
  - 상단 **차원 선택기**(세그먼트 컨트롤: 모델 | 스킬 | 브랜치) — 현재 provider/period/start/end를
    유지한 채 `dim`만 바꾸는 링크.
  - 그 아래 **서브에이전트 요약 카드**(부모 vs 서브에이전트 $·%). `split.total_cost==0`이면 숨김.
  - 테이블 구조는 기존과 동일, **첫 열 헤더만 `dim_label`로 동적**("모델"/"스킬"/"브랜치").
  - 기존 디자인 시스템 클래스 재사용, 무빌드 유지(CSS 클래스 추가 시에만 `build_css.ps1`).

### 3.6 미귀속 버킷 & provider 가용성

- **미귀속 버킷**: `key`가 None인 행을 dim별 라벨로 표시 — skill→`(미귀속)`, branch→`(브랜치 없음)`,
  model→`(unknown)`(기존 유지). 숨기지 않고 비용순 정렬에 포함(투명성).
- **provider 가용성**: `attribution_skill`·`git_branch`·`is_sidechain`은 **Claude 파서 전용**
  (codex_parser 미채움). 따라서:
  - dim이 skill/branch이고 provider=codex면 사실상 전량 미귀속 → 테이블 상단에
    "이 차원은 Claude 로그 기준(Codex는 귀속 메타 없음)" 안내.
  - 서브에이전트 카드도 동일 주석. provider 전체(미필터) 조회 시 Codex 분량은 자동으로 부모에 합산됨을 주석으로.

## 4. 데이터 흐름

```
messages (attribution_skill, git_branch, is_sidechain, cost_usd, ts, …)
  ├ by_dimension(dim) ──→ 차원별 비용/토큰 랭킹(미귀속 버킷 포함)
  └ sidechain_split  ──→ 부모 vs 서브에이전트 비중
        │
   dimension_context(dim, provider, 기간/구간) ──→ analysis.html
        │                                            ├ 차원 선택기(모델/스킬/브랜치)
        │                                            ├ 서브에이전트 요약 카드
        │                                            └ 차원별 테이블(동적 헤더)
   /models ──301──→ /analysis?dim=model
```

## 5. 엣지 케이스 & 에러 처리

- **불량 `dim`**(예: `?dim=foo`): 화이트리스트 fallback → `model`. 500 없음.
- **전부 미귀속**(스킬/브랜치 메타가 한 건도 없음): 미귀속 버킷 단일 행 + Claude 안내. 빈 테이블 아님.
- **기간 내 데이터 없음**: 빈 테이블 + total 0, 서브에이전트 카드 숨김(기존 빈 상태와 동일).
- **provider=codex + dim=skill/branch**: 전량 미귀속 → 안내 노출(회귀 아님, 의도된 빈 상태).
- **NULL vs 빈 문자열**: `git_branch`가 `""`로 적재될 수 있으면 None과 같은 버킷으로 정규화(COALESCE/빈문자 처리).
- **`/models` 북마크**: 301로 `/analysis?dim=model` 이동, 파라미터 미보존(레거시 수준 허용).

## 6. 테스트

- `tests/test_aggregate.py`:
  - `by_dimension`이 dim별(model/skill/branch) 정확 그룹핑 + NULL 버킷 + 비용 내림차순 +
    기간 `[start,nxt)` 필터(=기존 `by_model` 동작 동일).
  - `by_model` 래퍼가 `by_dimension(dim="model")`와 동일 결과(회귀 가드).
  - `sidechain_split` 부모/서브 합계·`sub_share`, total=0 분기.
- `tests/test_web.py`:
  - `/analysis?dim=skill` 200 + 테이블 렌더, `dim_label` 반영.
  - 불량 `dim` → model fallback(200, 모델 테이블).
  - `/models` → 301 `/analysis?dim=model`.
  - provider=codex + dim=branch → 안내/미귀속 상태.

## 7. 향후 작업 (범위 밖, 참고)

- **server tool 사용량 뷰**(`web_search`/`web_fetch` 집계·추이) — A그룹 잔여, 다음 spec.
- **차원별 시계열 추세** — 통합 추세 차트(`stacked_trend`) 패턴을 차원 키로 확장.
- **라벨별 집계**(C그룹) — `by_dimension`에 `dim="label"`(sessions 조인) 추가로 자연 확장 가능.
- **Python 필터 → SQL 집계 이전** — 차원 뷰가 늘면 `by_dimension`의 전체 스캔이 성능 enabler 후보.

## 8. 영향 받는 파일

| 파일 | 변경 |
|---|---|
| `tokenomy/aggregate.py` | `DIM_COLUMNS`·`by_dimension`·`DimensionRow`·`sidechain_split` 추가, `by_model` 래퍼화 |
| `tokenomy/web/views.py` | `dimension_context`(= `models_context` 일반화), dim fallback·split·가용성 메타 |
| `tokenomy/web/app.py` | `/analysis` 라우트 신설, `/models` 301 리다이렉트 |
| `tokenomy/web/templates/analysis.html` | `models.html` 개편 — 차원 선택기 + 서브에이전트 카드 + 동적 헤더 |
| `tokenomy/web/templates/_sidebar.html` | 나브 `모델별`→`차원별`(`/analysis`), `active_nav` 키 |
| `tokenomy/web/templates/models.html` | 제거(analysis.html로 대체) |
| `tests/test_aggregate.py`, `tests/test_web.py` | 위 §6 |
| `static/app.css` | 선택기/카드용 클래스 추가 시에만 재빌드 |
