# 단가 커버리지 신뢰성 진단 — 설계

- 작성일: 2026-06-17
- 상태: 승인 대기(spec 리뷰)
- 마일스톤: v0.2.0 채택 #1 ("단가 미식별 가시화"를 "단가 커버리지 신뢰성 진단"으로 확장)
- 관련: [ROADMAP.md](../../ROADMAP.md) · `tokenomy/pricing.py` · `tokenomy/aggregate.py` · `tokenomy/web/`

## 1. 배경 / 문제

비용은 `pricing.json`의 `match[]`를 위에서부터 순회하며 모델명에 `contains`가 **부분일치**하는
첫 단가로 산정한다(`pricing.find_rate`). 매칭 실패 시 `cost=0, priced=False`로 적재된다.

현재 노출은 효율 코치 카드의 **미식별 건수**(`Burndown.unpriced_count`, `aggregate.py:707`)
한 줄뿐이다. 이 지표는 v0.2.0 그릴에서 "actionable 최강"으로 분류됐으나, 실측은 다른 그림을 보여준다.

### 실측 (개발 DB, 메시지 17,135건, 2026-06-17)

- **미식별(`priced=0`) = 0건.** 메인테이너가 `pricing.json`을 관리하면 본인 머신에선 미식별이 실제로 0이다.
- 그러나 distinct 모델에 `gpt-5.5`(codex 5건)가 있고, 이는 `{ "contains": "gpt-5" }`에 **부분일치**해
  `priced=True`로 통과한다 — `gpt-5.5`의 실제 단가가 `gpt-5`와 다르면 **조용히 틀린 비용**이 잡히고,
  미식별 지표로는 **영영 안 잡힌다**.

### 두 가지 구조적 한계

1. **오매칭** — `contains` 부분일치가 새 모델을 "그럴듯하게 틀리게" 잡는다(`gpt-5.5`→`gpt-5`).
2. **거친 매칭** — 한 항목(`opus`)이 여러 버전(`opus-4-7`·`opus-4-8`)을 **한 단가로 뭉갠다**.
   현재는 단가가 같아 우연히 맞지만, 버전별 단가가 갈리면 틀린다.

단가는 **시점(`ts`)을 전혀 보지 않는다**(`compute_cost(record, pricing)` — `ts` 미사용,
`pricing.json`에 날짜 필드 없음). 단, 단가 변동은 대개 **모델명 변경을 동반**하므로(`opus-4-7`→`4-8`),
모델명이 사실상 시점 프록시다 → 시간 차원 도입 없이 **모델명 해상도**로 흡수한다. 순수 "동명 단가변동"은
드물고 backlog로 둔다.

## 2. 목표 / 비목표

### 목표
- `pricing.json`이 실제 사용 모델을 얼마나 정확히 매칭하는지 진단해, **미식별·오매칭 의심·거친 매칭**을 드러낸다.
- 메인테이너가 `pricing.json`을 정밀화하도록 유도한다(미식별 새 모델의 1차 해결 주체).
- **`pricing_overrides`를 확장**해 사용자(exe 포함)도 `tokenomy.config.json` 편집만으로 **새 모델 단가까지**
  자가 추가·조정할 수 있게 한다(앱 업데이트 불필요) → 진단이 사용자에게도 actionable해진다.
- 일반 사용자에겐 "이 집계가 일부 모델 비용을 누락/오산할 수 있음"을 투명하게 알린다.

### 비목표
- 시점별 단가(`effective_date`) 본격 도입 — backlog. 진단 각주로 한계만 투명 표기.
- 단가 자동 갱신/네트워크 조회 — 안 함. 수동 `pricing.json`/`overrides` 유지.
- **단가 편집 GUI** — *본 spec 범위 밖, 후속 spec으로 분해*(2026-06-17 결정). 본 spec은 진단 + overrides
  백엔드(JSON 편집)까지 다룬다 — GUI는 이 백엔드 위에 얹는 별도 작업이다(§11 참조). 지금은
  `tokenomy.config.json`의 `pricing_overrides`(JSON 직접 편집) 경로를 **확장**해서 쓴다. 처음 그릴에서 컷한
  "사용자 입력 authoring"은 *세션 라벨* 같은 새 의미 부여를 말하며, 단가 override는 raw 비용 계산의 보정이라 별개다.

### 설계 원칙 적합성 (v0.2.0 4원칙)
- **자격/강도**: 미식별=비용 누락(명백) → 경고 자격. 오매칭 의심=불확실 → 약한 주의(info).
- **raw 추출**: 이미 적재된 `model`·토큰만 사용. 새 수집·파서 변경 없음.
- **provider parity**: 미식별·오매칭·거친 매칭 모두 Claude·Codex 양쪽에서 발생(예: `gpt-5.5`=codex). 충족.
- **cost/value**: 작은 순수 함수 + 카드 1개. 싸고 actionable.

## 3. 설계 결정 요약 (2026-06-17 합의)

| # | 결정 | 선택 |
|---|------|------|
| 1 | 지표 범위 | 미식별 단건 → "단가 커버리지 신뢰성 진단"으로 확장 |
| 2 | 오매칭 판정 | 투명 매핑(모델→매칭 항목) + 버전경계 휴리스틱 보조 |
| 3 | 노출 위치 | settings 진단 카드(주) + overview 경고(보조) |
| 4 | 표현 강도 | 미식별=warn(overview+settings), 의심=info(settings만), 미식별 0이면 overview 무표시 |
| 5 | 시점별 단가 | 모델명 해상도로 흡수 — 거친 매칭 가시화. 동명 단가변동은 backlog + 각주 |
| 6 | 측정 단위 | 토큰 비중(주). 건수는 표에서 생략 |
| 7 | overrides 확장 | `apply_pricing_overrides`가 새 `contains` 항목 추가 지원(prepend) → 사용자가 GUI 없이 config로 새 모델 자가해결 |

## 4. 데이터 계층 — `aggregate.py` (순수 함수)

집계는 `aggregate.py`의 순수 함수로, 화면 조립은 `web/views.py`로, 라우트는 `app.py`(얇게)로 분리한다(기존 계층 유지).

### dataclass

```python
@dataclass
class CoverageModel:
    provider: str
    model: str
    matched_contains: str | None   # 매칭된 pricing 항목의 contains. None이면 미식별
    status: str                    # "ok" | "suspect" | "unpriced"
    tokens: int                    # input+output+cache_creation+cache_read 합
    token_share: float             # tokens / 전체 tokens (0~1)

@dataclass
class CoverageReport:
    models: list[CoverageModel]        # 토큰 내림차순
    total_tokens: int
    unpriced_count: int                # status=="unpriced" 모델 "종" 수 (메시지 건수 아님)
    unpriced_token_share: float        # 미식별 모델 토큰 합 / 전체
    suspect_count: int                 # status=="suspect" 모델 "종" 수
    coarse_contains: list[str]         # 2개 이상 distinct 모델이 매칭된 contains 목록
```

### 함수

```python
def pricing_coverage(conn, pricing: dict) -> CoverageReport:
    """distinct (provider, model)별 토큰 집계 + 단가 매칭 진단.

    - 각 모델을 find_rate로 매칭, 매칭 항목의 contains를 보존.
    - status: rate None → "unpriced", 버전경계 의심 → "suspect", 그 외 "ok".
    - token_share = 모델 토큰 / 전체 토큰.
    - coarse_contains: 같은 contains에 매칭된 distinct 모델이 2개 이상인 항목.
    """
```

- 매칭 항목(contains)은 `find_rate`가 이미 반환하는 항목 dict의 `entry["contains"]`로 그대로 얻는다.
  **신규 헬퍼 불필요** — `find_rate(model, pricing)`를 재사용한다(시그니처·동작 보존).

### 의심 휴리스틱

```python
def _is_version_boundary(model: str, contains: str) -> bool:
    """매칭된 contains 토큰 직후 문자가 숫자나 '.'이면 버전 경계 의심."""
    idx = model.find(contains)
    if idx < 0:
        return False
    nxt = model[idx + len(contains): idx + len(contains) + 1]
    return nxt.isdigit() or nxt == "."
```

- `gpt-5` + `gpt-5.5` → 직후 `.` → True(의심). `opus` + `claude-opus-4-8` → 직후 `-` → False(정상).
- 미식별(`rate None`)은 의심 판정하지 않는다(이미 `unpriced`).
- **거친 매칭**은 status가 아니라 그룹 속성이다 — settings에서 항목별 그룹핑으로 시각 표현하고,
  `coarse_contains`는 참고용 요약일 뿐 경고를 띄우지 않는다(같은 단가면 실무상 정상).

## 5. settings 진단 카드 (주 화면)

`web/views.py`에 `coverage_card_context(conn, pricing)` 추가, `settings_get`에서 호출해 템플릿에 전달.
`settings.html`에 카드 섹션(기존 카드 패턴 답습) 추가.

pricing 항목 기준 **역방향 매핑**으로 거친 매칭이 자연히 드러난다:

```
┌─ 단가 커버리지 ───────────────────────────────────────┐
│ ⓘ pricing.json이 사용 모델을 정확히 매칭하는지 진단.    │
│   상태: ⚠ 확인 필요 1건                                │
│                                                        │
│  단가 항목          매칭된 모델           토큰    비중   │
│  ──────────────────────────────────────────────────── │
│  opus  $15/$75      claude-opus-4-7      0.9B    50%   │
│                     claude-opus-4-8      0.7B    39%   │
│  sonnet $3/$15      claude-sonnet-4-6    0.1B     8%   │
│  haiku $1/$5        claude-haiku-4-5     20M      1%   │
│  gpt-5 $1.25/$10    gpt-5.5         ⚠    12K    <1%   │
│                                                        │
│  ⚠ gpt-5.5 → 'gpt-5' 단가로 매칭됨. 다른 모델일 수      │
│    있으니 pricing_overrides에 gpt-5.5 단가를 추가하세요. │
│  ⓘ 단가 추가·조정: tokenomy.config.json > pricing_      │
│    overrides. 새 모델도 가능. 재ingest로 반영됩니다.     │
│  ⓘ 단가는 시점 무관·현재 단일 단가로 계산됩니다.        │
└────────────────────────────────────────────────────────┘
```

- **상태 라벨**(우선순위 unpriced > suspect):
  - `unpriced_count > 0` → "미식별 N건"(warn 색)
  - `suspect_count > 0` → "확인 필요 N건"(info 색)
  - 둘 다 0 → "모든 모델 단가 식별됨 ✓"
- 카드는 **항상 표시**(투명성). 미식별 모델은 '단가 항목' 칸에 `(미식별)`·빨강으로 별도 행, **있을 때만** 노출.
- 토큰은 사람이 읽기 쉬운 단위(K/M/B)로 표기. 비중은 `<1%`까지.
- 의심 모델마다 ⚠ 안내 1줄. 시점 무관 각주 ⓘ 1줄 고정.
- **해결 안내**: 미식별·의심 시 "`tokenomy.config.json`의 `pricing_overrides`에 단가 추가"를 안내(개발자·사용자 공통).
  새 모델 추가는 §6의 overrides 확장으로 가능해진다. 재ingest로 반영됨을 함께 안내.

## 6. `pricing_overrides` 확장 — 새 모델 자가 추가

`pricing.py`의 `apply_pricing_overrides`를 확장해, **기존 `match[]`에 없는 `contains` 키는 새 항목으로 추가**한다.
사용자가 GUI 없이 `tokenomy.config.json` 편집만으로 새 모델 단가를 잡을 수 있다.

```python
def apply_pricing_overrides(pricing, overrides):
    if not overrides:
        return pricing
    existing = {e.get("contains") for e in pricing.get("match", [])}
    # 1) 기존 항목 단가 교체 (현행 동작 보존)
    for entry in pricing["match"]:
        ov = overrides.get(entry.get("contains"))
        if ov:
            for k in _OVERRIDABLE:
                if k in ov:
                    entry[k] = ov[k]
    # 2) 신규 항목 추가 (기존에 없는 contains 키)
    new = [
        {"contains": c, "provider": ov.get("provider"),
         "input": ov.get("input", 0.0), "output": ov.get("output", 0.0),
         "cache_write": ov.get("cache_write", 0.0), "cache_read": ov.get("cache_read", 0.0)}
        for c, ov in overrides.items() if c not in existing
    ]
    # 더 구체적인 사용자 항목이 기존 거친 항목보다 먼저 매칭되도록 prepend
    pricing["match"] = new + pricing["match"]
    return pricing
```

- **prepend 이유**: `find_rate`는 위에서부터 첫 부분일치를 쓴다. 새 `gpt-5.5` 항목이 기존 `gpt-5`보다
  앞서야 `gpt-5.5` 모델에 정확 매칭된다(뒤에 두면 `gpt-5`가 먼저 잡힘).
- 신규 항목 config 예:
  ```json
  "pricing_overrides": {
    "gpt-5.5": { "provider": "codex", "input": 1.25, "output": 10.0, "cache_read": 0.125 }
  }
  ```
- 누락 단가 필드는 `0.0`(`cache_write` 등). `provider` 누락 시 `None`.
- 기존 키(예 `opus`)는 종전대로 단가 교체만 — 신규/기존 판정은 `contains` 키 존재 여부.
- 적용 지점 변경 없음 — `cmd_ingest`가 이미 `apply_pricing_overrides(load_pricing(), ...)`를 Claude·Codex 공통 적용(cli.py:25,28).
  새 단가는 **재ingest**로 반영된다(이미 적재된 cost는 고정).
- README의 `pricing_overrides` 안내에 "새 모델 추가" 예시를 보강한다.

## 7. overview 경고 (보조)

`aggregate.py`의 효율 코치 카드(`insights`)에서 기존 미식별 경고를 확장:

```python
# 기존: if bd.unpriced_count: Insight("warn", f"단가 미식별 {bd.unpriced_count}건 — 비용 누락 가능")
#   → bd.unpriced_count는 미식별 "메시지 건수"였다.
# 변경: coverage 기반 "모델 종 수" + 토큰 비중 + 설정 안내
if cov.unpriced_count:
    pct = cov.unpriced_token_share * 100
    Insight("warn", f"단가 미식별 {cov.unpriced_count}종(토큰 {pct:.0f}%) — 비용 누락, 설정에서 확인")
```

- 경고 단위가 **메시지 건수 → 모델 종 수**로 바뀐다("몇 개를 pricing.json에 추가할지"가 더 actionable).
  `Burndown.unpriced_count`(메시지 건수) 필드 자체는 보존하되, overview 경고 문구만 coverage 기반으로 **교체**한다.
- **의심은 overview에 띄우지 않는다**(settings에서만 info).
- 미식별 0이면 무표시(노이즈 제로).
- overview 컨텍스트에서 coverage를 1회 계산해 카드 생성에 사용(provider별 burndown과 별개로 전역 1회).

## 8. CLI `report` (보조)

`cli.py`의 `report` 출력 끝에 한 줄 추가(메인테이너가 터미널에서 바로 확인):

- 미식별/의심이 있으면: `단가 커버리지: 미식별 N건 · 확인 필요 M건 (설정/pricing.json 확인)`
- 둘 다 0이면: `단가 커버리지: 정상`

## 9. 테스트

### `tests/test_aggregate.py` — `pricing_coverage`
- 정상(`opus` → `claude-opus-4-8`, status="ok")
- 미식별(매칭 없는 모델 → status="unpriced", `unpriced_count`/`unpriced_token_share` 반영)
- 버전경계 의심(`gpt-5.5` → `gpt-5`, status="suspect", `suspect_count`)
- 거친 매칭(`opus-4-7`+`opus-4-8` → `coarse_contains`에 `opus`)
- `token_share` 합 ≈ 1.0, 빈 DB(total_tokens=0, 0 division 안전)

### `tests/test_pricing.py` — 휴리스틱 + overrides 확장
- `_is_version_boundary("gpt-5.5", "gpt-5") is True`
- `_is_version_boundary("claude-opus-4-8", "opus") is False`
- `_is_version_boundary("gpt-5", "gpt-5") is False`(직후 없음)
- overrides에 신규 `contains` 키 → `match[]`에 새 항목 추가, **기존 항목보다 앞(prepend)**
- 신규 항목으로 미식별 모델이 `priced=True`·정확 단가로 계산됨(`compute_cost` 통합)
- 기존 키(`opus`) override는 종전대로 단가만 교체(회귀 없음)
- 누락 단가 필드 `0.0`, `provider` 누락 시 `None`

### `tests/test_web.py`
- settings 렌더에 진단 카드 존재(미식별/의심/정상 상태 각각)
- overview 경고에 토큰 비중 포함, 미식별 0이면 경고 미노출

## 10. 영향 범위 / 비변경

- **변경**: `pricing.py`(`apply_pricing_overrides` 확장 — 신규 항목 추가), `aggregate.py`(진단 함수+overview 경고),
  `web/views.py`(카드 컨텍스트), `web/templates/settings.html`(카드), `cli.py`(report 한 줄),
  `README`(overrides 새 모델 예시), 테스트.
- **비변경**: `pricing.json` 스키마, `compute_cost`/`find_rate` 시그니처, `db.py` 스키마, 적재 파이프라인 구조.
  진단(§4·§7·§8)은 **읽기 전용 집계**다. overrides 확장(§6)은 ingest 시 **메모리상 pricing dict만** 바꾼다 —
  단가 계산식·DB 스키마는 그대로다.

## 11. 후속 / backlog

- **단가 편집 GUI (바로 다음 sub-project)** — 본 spec의 진단 테이블 + overrides 확장 백엔드 위에 얹는 별도 spec.
  모델/단가를 settings에서 직접 **확인·추가·수정**(폼·검증·저장)하고, 단가 변경 시 **기존 적재 cost 재계산 UX**
  (재ingest 트리거 또는 rebuild)를 설계한다. 본 spec 완료 후 별도 brainstorming → spec → plan으로 진행.
- 시점별 단가(`effective_date`, `ts` 기반 단가 선택, 과거단가 히스토리) — 드문 "동명 단가변동" 대비. 후속.
- `pricing.json` 갱신 보조 도구 — ROADMAP "단가 최신화 워크플로"와 통합 검토.
- `provider="chatgpt"`의 `gpt-5.5` 1건(코덱스 아닌 출처) — 별개 파서 이슈, 본 spec 범위 밖.
