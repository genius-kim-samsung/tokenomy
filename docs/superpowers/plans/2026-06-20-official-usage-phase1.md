# 공식 사용량 자동 취득 — Phase 1 구현 계획 (모델 + 파서 + 표시, 네트워크 없음)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 공식 사용량을 멀티버킷(USD 통일) 모델로 저장·표시하고, 수동 입력을 제거하며, fixture 주입으로 라이브 없이 user-verifiable하게 만든다. 실제 네트워크 취득은 Phase 2.

**Architecture:** 순수 파서(`official_parser.py`, raw JSON → `[OfficialBucket]`, USD 환산) → `db.py`(스냅샷 트랜잭션 적재) → `aggregate.official_view`(Claude 버킷 + Codex 월간/주간 2게이지 + 예측 렌즈) → `web/views.py`+템플릿(공식 미러 패널 + 합산 USD). 주간 used는 로컬 CLI 첫-사용 7일 윈도우(추정), 월간은 공식 ground truth. `cli.py`의 `official import-fixture`로 fixture를 주입해 검증.

**Tech Stack:** Python 3 stdlib(sqlite3/json/dataclasses/datetime), FastAPI+Jinja2, pytest. 신규 런타임 의존성 없음.

## Global Constraints

설계 스펙(`docs/superpowers/specs/2026-06-20-official-usage-auto-fetch-design.md`)의 프로젝트 전역 규칙. 모든 태스크에 암묵 적용:

- **네트워크 없음(Phase 1)** — 본 계획의 어떤 코드도 아웃바운드 호출을 하지 않는다. 데이터는 fixture/import-fixture로만 들어온다.
- **USD 1차 단위** — 표시·합산은 USD. 크레딧/버킷은 보조. 크레딧→USD 환산은 `credit_to_usd`(기본 `0.04`) 단일 상수.
- **`credit_to_usd`는 `tokenomy.config.json`**(pricing.json 아님). 토큰 cost 경로(`pricing_fingerprint`/`maybe_reprice`)에 절대 비접촉.
- **PII 미저장** — email/user_id/account_id는 파서가 추출도 저장도 하지 않는다. 사용량 수치만.
- **코드네임 하드코딩 금지** — 이벤트/프로모션 버킷은 shape 휴리스틱으로 분류(키 이름 의존 금지). `five_hour`/`seven_day*`만 안정 키로 매칭 허용.
- **리셋 주기** — Claude 월별(+이벤트 버킷 자체 만료), Codex 주별(월 한도÷4, 로컬 첫-사용 7일 앵커).
- **월 경계 = KST**. ts는 UTC라 `parse_ts`로 변환.
- **계층 분리** — 라우트(app.py 얇게) ↔ 화면(views.py) ↔ 집계(aggregate.py) ↔ 적재(db.py) ↔ 순수 파서(official_parser.py).
- **모든 모듈 상단** `from __future__ import annotations`. docstring·주석 한국어.
- **수치는 전부 예시(가짜)** — 실제 한도/금액 미커밋. fixture는 모양만 진짜, 금액 가짜.
- **로컬 단일 사용자** — 마이그레이션에 데이터 이관 없음(구 `official_usage` DROP 가능).

---

## Task 1: Config — `credit_to_usd` 접근자

크레딧→USD 환산 상수를 config에서 읽는 단일 접근자. 이후 파서/뷰가 주입받는다.

**Files:**
- Modify: `tokenomy/budget.py:51-64` (`load_config` 기본값에 `credit_to_usd` 추가), 파일 끝에 접근자 추가
- Modify: `config/tokenomy.config.example.json`
- Test: `tests/test_budget.py`

**Interfaces:**
- Produces: `credit_to_usd(config: dict) -> float` (기본 0.04, 음수/비숫자 → 0.04)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_budget.py` 끝에 추가:

```python
from tokenomy.budget import credit_to_usd


def test_credit_to_usd_default_when_missing():
    assert credit_to_usd({}) == 0.04


def test_credit_to_usd_reads_config():
    assert credit_to_usd({"credit_to_usd": 0.05}) == 0.05


def test_credit_to_usd_rejects_bad_values():
    assert credit_to_usd({"credit_to_usd": -1}) == 0.04
    assert credit_to_usd({"credit_to_usd": "x"}) == 0.04
    assert credit_to_usd({"credit_to_usd": None}) == 0.04
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_budget.py -k credit_to_usd -v`
Expected: FAIL — `ImportError: cannot import name 'credit_to_usd'`

- [ ] **Step 3: 최소 구현**

`tokenomy/budget.py`의 `load_config` 기본값 dict(`base`)에 키 추가:

```python
    base = {"user_label": _default_label(),
            "budget": {"claude": 0.0, "codex": 0.0},
            "budget_start": None,
            "credit_to_usd": 0.04,
            "pricing_overrides": {}}
```

파일 끝(`budget_start_kst` 아래)에 접근자 추가:

```python
def credit_to_usd(config: dict) -> float:
    """크레딧→USD 환산 단가(크레딧 단위가격, 고정 청구 상수). 모델 무관 단일 상수.

    빈값·음수·비숫자는 모두 기본 0.04로 폴백한다(오설정으로 환산이 깨지지 않게).
    토큰 cost 경로(pricing.json)와 분리 — 여기서만 official 버킷 크레딧 환산에 쓴다.
    """
    raw = config.get("credit_to_usd")
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return 0.04
    return f if f > 0 else 0.04
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_budget.py -k credit_to_usd -v`
Expected: PASS (3개)

- [ ] **Step 5: example config 갱신**

`config/tokenomy.config.example.json`을 다음으로 교체:

```json
{
  "user_label": "me",
  "budget": {
    "claude": 100,
    "codex": 50
  },
  "budget_start": null,
  "credit_to_usd": 0.04,
  "pricing_overrides": {}
}
```

- [ ] **Step 6: 커밋**

```bash
git add tokenomy/budget.py config/tokenomy.config.example.json tests/test_budget.py
git commit -m "feat(config): credit_to_usd 접근자(크레딧→USD 환산 상수, 기본 0.04)"
```

---

## Task 2: 순수 파서 `official_parser.py` + sanitize fixtures

raw API JSON을 shape 휴리스틱으로 `[OfficialBucket]`로 정규화(USD 환산 포함). 네트워크/DB 없음.

**Files:**
- Create: `tokenomy/official_parser.py`
- Create: `tests/fixtures/official/claude_enterprise.json`
- Create: `tests/fixtures/official/claude_enterprise_rotated.json`
- Create: `tests/fixtures/official/claude_personal.json`
- Create: `tests/fixtures/official/codex_enterprise.json`
- Create: `tests/fixtures/official/codex_personal.json`
- Test: `tests/test_official_parser.py`

**Interfaces:**
- Produces:
  - `OfficialBucket` 데이터클래스(필드: bucket_key, raw_key, bucket_kind, label, native_unit, used_native, limit_native, remaining_native, used_usd, limit_usd, remaining_usd, utilization, resets_at)
  - `parse_claude(raw: dict, *, credit_to_usd: float) -> list[OfficialBucket]`
  - `parse_codex(raw: dict, *, credit_to_usd: float) -> list[OfficialBucket]`

- [ ] **Step 1: sanitize fixtures 작성(모양 진짜·금액 가짜)**

`tests/fixtures/official/claude_enterprise.json`:

```json
{
  "five_hour": null,
  "seven_day": null,
  "seven_day_opus": null,
  "omelette_promotional": {
    "utilization": 0.0, "resets_at": null,
    "limit_dollars": null, "used_dollars": null, "remaining_dollars": null
  },
  "cinder_cove": {
    "utilization": 25.0, "resets_at": "2026-09-10T01:47:22.420960+00:00",
    "limit_dollars": 500, "used_dollars": 125.0, "remaining_dollars": 375.0
  },
  "amber_ladder": null,
  "extra_usage": {
    "is_enabled": true, "monthly_limit": 10000, "used_credits": 0.0,
    "utilization": null, "currency": "USD", "decimal_places": 2
  },
  "limits": [],
  "spend": {
    "used": {"amount_minor": 3000, "currency": "USD", "exponent": 2},
    "limit": {"amount_minor": 10000, "currency": "USD", "exponent": 2},
    "percent": 30, "severity": "normal", "enabled": true
  }
}
```

`tests/fixtures/official/claude_enterprise_rotated.json` (코드네임 회전 — 키 이름만 다르고 모양 동일):

```json
{
  "five_hour": null,
  "seven_day": null,
  "tangelo": {
    "utilization": 0.0, "resets_at": null,
    "limit_dollars": null, "used_dollars": null, "remaining_dollars": null
  },
  "maple_harbor": {
    "utilization": 25.0, "resets_at": "2026-09-10T01:47:22.420960+00:00",
    "limit_dollars": 500, "used_dollars": 125.0, "remaining_dollars": 375.0
  },
  "extra_usage": {
    "is_enabled": true, "monthly_limit": 10000, "used_credits": 0.0,
    "currency": "USD", "decimal_places": 2
  },
  "limits": [],
  "spend": {
    "used": {"amount_minor": 3000, "currency": "USD", "exponent": 2},
    "limit": {"amount_minor": 10000, "currency": "USD", "exponent": 2},
    "percent": 30, "severity": "normal", "enabled": true
  }
}
```

`tests/fixtures/official/claude_personal.json` (개인 구독 — % 창):

```json
{
  "five_hour": {"utilization": 42.0, "resets_at": "2026-06-20T15:00:00+00:00"},
  "seven_day": {"utilization": 18.0, "resets_at": "2026-06-25T00:00:00+00:00"},
  "seven_day_opus": {"utilization": 30.0, "resets_at": "2026-06-25T00:00:00+00:00"},
  "cinder_cove": null,
  "extra_usage": null,
  "spend": null
}
```

`tests/fixtures/official/codex_enterprise.json` (PII는 `[redacted]` — 파서가 무시):

```json
{
  "user_id": "[redacted]",
  "account_id": "[redacted]",
  "email": "[redacted]",
  "plan_type": "business",
  "rate_limit": null,
  "credits": {"has_credits": true, "unlimited": false, "overage_limit_reached": false},
  "spend_control": {
    "reached": false,
    "individual_limit": {
      "source": "group_based_spend_controls",
      "limit": "2000", "used": "500.0", "remaining": "1500.0",
      "used_percent": 25, "remaining_percent": 75,
      "reset_after_seconds": 982330, "reset_at": 1782864001
    }
  },
  "promo": null
}
```

`tests/fixtures/official/codex_personal.json`:

```json
{
  "plan_type": "plus",
  "rate_limit": {
    "primary_window": {"used_percent": 55.0, "window_minutes": 300, "resets_at": 1782864001},
    "secondary_window": {"used_percent": 20.0, "window_minutes": 10080, "resets_at": 1783000000}
  },
  "spend_control": null,
  "credits": {"has_credits": false}
}
```

- [ ] **Step 2: 실패하는 테스트 작성**

`tests/test_official_parser.py`:

```python
import json
from datetime import datetime
from pathlib import Path

from tokenomy.official_parser import OfficialBucket, parse_claude, parse_codex

FIX = Path(__file__).parent / "fixtures" / "official"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _by_kind(buckets):
    return {b.bucket_kind: b for b in buckets}


def test_claude_enterprise_three_buckets():
    buckets = parse_claude(_load("claude_enterprise.json"), credit_to_usd=0.04)
    kinds = _by_kind(buckets)
    # 월 사용 한도(spend) + 이벤트 크레딧 + 프로모션(util 0이면 생략 → 여기선 0.0이라 제외)
    assert "monthly_limit" in kinds
    assert "event_credit" in kinds
    m = kinds["monthly_limit"]
    assert m.native_unit == "usd"
    assert m.used_usd == 30.0          # amount_minor 3000 / 10**2
    assert m.limit_usd == 100.0        # amount_minor 10000 / 10**2
    assert m.bucket_key == "monthly"
    e = kinds["event_credit"]
    assert e.used_usd == 125.0 and e.limit_usd == 500.0
    assert e.bucket_key == "event" and e.raw_key == "cinder_cove"
    assert isinstance(e.resets_at, datetime)


def test_claude_promo_zero_util_skipped():
    buckets = parse_claude(_load("claude_enterprise.json"), credit_to_usd=0.04)
    assert all(b.bucket_kind != "promo" for b in buckets)  # utilization 0.0 → 생략


def test_claude_rotated_codenames_same_classification():
    buckets = parse_claude(_load("claude_enterprise_rotated.json"), credit_to_usd=0.04)
    kinds = _by_kind(buckets)
    assert "monthly_limit" in kinds and "event_credit" in kinds
    assert kinds["event_credit"].raw_key == "maple_harbor"   # 코드네임 회전에도 분류 동일


def test_claude_personal_rate_windows():
    buckets = parse_claude(_load("claude_personal.json"), credit_to_usd=0.04)
    rw = [b for b in buckets if b.bucket_kind == "rate_window"]
    assert {b.raw_key for b in rw} == {"five_hour", "seven_day", "seven_day_opus"}
    for b in rw:
        assert b.native_unit == "percent"
        assert b.used_usd is None       # % 창은 USD 없음
        assert b.utilization > 0


def test_codex_enterprise_credit_to_usd():
    buckets = parse_codex(_load("codex_enterprise.json"), credit_to_usd=0.04)
    assert len(buckets) == 1
    b = buckets[0]
    assert b.bucket_kind == "codex_monthly" and b.bucket_key == "monthly"
    assert b.native_unit == "credit"
    assert b.used_native == 500.0 and b.limit_native == 2000.0
    assert b.used_usd == 20.0           # 500 * 0.04
    assert b.limit_usd == 80.0          # 2000 * 0.04
    assert b.utilization == 25
    assert isinstance(b.resets_at, datetime)


def test_codex_personal_rate_windows():
    buckets = parse_codex(_load("codex_personal.json"), credit_to_usd=0.04)
    kinds = {b.raw_key for b in buckets}
    assert kinds == {"primary_window", "secondary_window"}
    for b in buckets:
        assert b.bucket_kind == "rate_window" and b.native_unit == "percent"
        assert b.used_usd is None


def test_no_pii_extracted():
    buckets = parse_codex(_load("codex_enterprise.json"), credit_to_usd=0.04)
    blob = repr(buckets)
    assert "redacted" not in blob       # email/user_id/account_id 미추출
```

- [ ] **Step 3: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_official_parser.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tokenomy.official_parser'`

- [ ] **Step 4: 파서 구현**

`tokenomy/official_parser.py`:

```python
"""공식 사용량 응답 → 정규화 버킷(순수, 모양 불문, USD 환산).

네트워크/DB 의존 없음 — raw dict만 받아 [OfficialBucket]을 반환한다. enterprise
달러 버킷·개인 구독 % 창을 모두 처리한다. 코드네임(cinder_cove 등)은 회전하므로
키 이름이 아니라 dict 모양으로 분류한다(five_hour/seven_day*만 안정 키로 매칭).

프라이버시: 사용량 수치만 추출. email/user_id/account_id 등 PII는 건드리지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

# 개인 구독 rate-limit 창의 안정 키(코드네임 아님 — 이름 매칭 허용).
_RATE_WINDOW_PREFIXES = ("five_hour", "seven_day")


@dataclass
class OfficialBucket:
    """공식 앱 막대 1개에 대응하는 정규화 버킷. USD는 환산 결과를 함께 보관한다."""
    bucket_key: str          # 안정 논리 id: 'monthly'|'event'|'promo'|'rate_window'
    raw_key: str             # 원 API 키(코드네임/창 이름) — series 보조 분리키
    bucket_kind: str         # 'monthly_limit'|'event_credit'|'promo'|'rate_window'|'codex_monthly'
    label: str               # 서술형 라벨(코드네임 비의존)
    native_unit: str         # 'usd'|'credit'|'percent'
    used_native: float | None
    limit_native: float | None
    remaining_native: float | None
    used_usd: float | None
    limit_usd: float | None
    remaining_usd: float | None
    utilization: float       # 0~100
    resets_at: datetime | None


def _parse_iso(value) -> datetime | None:
    """ISO8601 문자열 → datetime(타임존 보존). 실패 시 None."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_unix(value) -> datetime | None:
    """unix epoch(초) → KST datetime. 실패 시 None."""
    try:
        return datetime.fromtimestamp(float(value), tz=KST)
    except (TypeError, ValueError, OSError):
        return None


def _to_float(value) -> float | None:
    """문자열/숫자 → float. None/실패 시 None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_claude(raw: dict, *, credit_to_usd: float) -> list[OfficialBucket]:
    """Claude enterprise/개인 응답을 버킷으로. native_unit은 enterprise=usd, 개인=percent."""
    out: list[OfficialBucket] = []

    # 1) 월 사용 한도 = spend(used/limit amount_minor) — extra_usage와 같은 버킷.
    spend = raw.get("spend")
    if isinstance(spend, dict) and isinstance(spend.get("limit"), dict):
        lim = spend["limit"]
        used = spend.get("used") or {}
        exp = lim.get("exponent", 0) or 0
        limit_usd = (lim.get("amount_minor") or 0) / (10 ** exp)
        used_usd = (used.get("amount_minor") or 0) / (10 ** exp)
        util = (used_usd / limit_usd * 100) if limit_usd > 0 else 0.0
        out.append(OfficialBucket(
            bucket_key="monthly", raw_key="spend", bucket_kind="monthly_limit",
            label="월 사용 한도", native_unit="usd",
            used_native=used_usd, limit_native=limit_usd,
            remaining_native=round(limit_usd - used_usd, 6),
            used_usd=used_usd, limit_usd=limit_usd,
            remaining_usd=round(limit_usd - used_usd, 6),
            utilization=round(util, 4), resets_at=None,   # 월 경계는 집계에서 계산
        ))

    # 2) 코드네임 dict들 — 모양으로 분류(키 이름 무시).
    for key, val in raw.items():
        if key in ("spend", "extra_usage", "limits") or not isinstance(val, dict):
            continue
        if key.startswith(_RATE_WINDOW_PREFIXES):
            # 개인 구독 % 창
            util = val.get("utilization")
            if util is None:
                continue
            out.append(OfficialBucket(
                bucket_key="rate_window", raw_key=key, bucket_kind="rate_window",
                label="이용률 창", native_unit="percent",
                used_native=None, limit_native=None, remaining_native=None,
                used_usd=None, limit_usd=None, remaining_usd=None,
                utilization=round(float(util), 4), resets_at=_parse_iso(val.get("resets_at")),
            ))
        elif val.get("used_dollars") is not None and val.get("limit_dollars") is not None:
            # 이벤트 크레딧(일회성, 자체 만료)
            used = float(val["used_dollars"]); limit = float(val["limit_dollars"])
            rem = val.get("remaining_dollars")
            rem = float(rem) if rem is not None else round(limit - used, 6)
            resets = _parse_iso(val.get("resets_at"))
            label = "포함된 크레딧"
            if resets is not None:
                label += f" · {resets.date().isoformat()} 만료"
            out.append(OfficialBucket(
                bucket_key="event", raw_key=key, bucket_kind="event_credit",
                label=label, native_unit="usd",
                used_native=used, limit_native=limit, remaining_native=rem,
                used_usd=used, limit_usd=limit, remaining_usd=rem,
                utilization=round(used / limit * 100, 4) if limit > 0 else 0.0,
                resets_at=resets,
            ))
        elif val.get("utilization"):   # 0/None이면 생략
            # 별도 프로모션(달러 null, util만)
            out.append(OfficialBucket(
                bucket_key="promo", raw_key=key, bucket_kind="promo",
                label="별도/프로모션", native_unit="percent",
                used_native=None, limit_native=None, remaining_native=None,
                used_usd=None, limit_usd=None, remaining_usd=None,
                utilization=round(float(val["utilization"]), 4),
                resets_at=_parse_iso(val.get("resets_at")),
            ))
    return out


def parse_codex(raw: dict, *, credit_to_usd: float) -> list[OfficialBucket]:
    """Codex enterprise(크레딧→USD 환산)/개인(% 창) 응답을 버킷으로."""
    out: list[OfficialBucket] = []

    sc = raw.get("spend_control")
    indiv = sc.get("individual_limit") if isinstance(sc, dict) else None
    if isinstance(indiv, dict):
        used = _to_float(indiv.get("used")) or 0.0
        limit = _to_float(indiv.get("limit")) or 0.0
        rem = _to_float(indiv.get("remaining"))
        if rem is None:
            rem = round(limit - used, 6)
        out.append(OfficialBucket(
            bucket_key="monthly", raw_key="individual_limit", bucket_kind="codex_monthly",
            label="월간 크레딧 한도", native_unit="credit",
            used_native=used, limit_native=limit, remaining_native=rem,
            used_usd=round(used * credit_to_usd, 6),
            limit_usd=round(limit * credit_to_usd, 6),
            remaining_usd=round(rem * credit_to_usd, 6),
            utilization=float(indiv.get("used_percent") or 0),
            resets_at=_parse_unix(indiv.get("reset_at")),
        ))

    rl = raw.get("rate_limit")
    if isinstance(rl, dict):
        for key, val in rl.items():
            if not isinstance(val, dict) or val.get("used_percent") is None:
                continue
            out.append(OfficialBucket(
                bucket_key="rate_window", raw_key=key, bucket_kind="rate_window",
                label="이용률 창", native_unit="percent",
                used_native=None, limit_native=None, remaining_native=None,
                used_usd=None, limit_usd=None, remaining_usd=None,
                utilization=round(float(val["used_percent"]), 4),
                resets_at=_parse_unix(val.get("resets_at")),
            ))
    return out
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_official_parser.py -v`
Expected: PASS (7개)

- [ ] **Step 6: 커밋**

```bash
git add tokenomy/official_parser.py tests/fixtures/official/ tests/test_official_parser.py
git commit -m "feat(official): 순수 파서 + sanitize fixtures(USD 환산, 코드네임 내성)"
```

---

## Task 3: DB — `official_buckets` + `official_fetch_state` (additive) + 함수

신규 테이블 2개를 SCHEMA에 추가(구 `official_usage`는 아직 유지 — Task 9에서 제거)하고, 스냅샷 트랜잭션 적재/조회 함수를 추가한다.

**Files:**
- Modify: `tokenomy/db.py:23-93` (SCHEMA에 테이블 2개 추가), 파일 끝에 함수 추가
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `tokenomy.official_parser.OfficialBucket` (Task 2)
- Produces:
  - `insert_official_buckets(conn, *, provider: str, fetched_at: str, buckets: list, created_at: str) -> int`
  - `latest_official_snapshot(conn, provider: str) -> list` (최신 fetched_at의 행들)
  - `official_bucket_series(conn, provider: str, bucket_key: str) -> list` (`(fetched_at, used_usd, used_native)` 오름차순)
  - `get_fetch_state(conn, provider: str)` / `upsert_fetch_state(conn, provider, *, last_attempt_at, last_success_at, last_status, last_error)`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_db.py` 끝에 추가:

```python
from tokenomy.db import (
    insert_official_buckets, latest_official_snapshot,
    official_bucket_series, get_fetch_state, upsert_fetch_state,
)
from tokenomy.official_parser import OfficialBucket


def _bucket(key, used_usd, limit_usd, raw_key="r"):
    return OfficialBucket(
        bucket_key=key, raw_key=raw_key, bucket_kind="monthly_limit", label="L",
        native_unit="usd", used_native=used_usd, limit_native=limit_usd,
        remaining_native=limit_usd - used_usd, used_usd=used_usd, limit_usd=limit_usd,
        remaining_usd=limit_usd - used_usd, utilization=0.0, resets_at=None,
    )


def test_official_buckets_insert_and_latest():
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-20T10:00:00+09:00",
                            buckets=[_bucket("monthly", 30.0, 100.0), _bucket("event", 125.0, 500.0, "cinder")],
                            created_at="2026-06-20T10:00:00+09:00")
    rows = latest_official_snapshot(conn, "claude")
    assert len(rows) == 2
    assert {r["bucket_key"] for r in rows} == {"monthly", "event"}


def test_official_buckets_latest_picks_newest_fetch():
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-20T10:00:00+09:00",
                            buckets=[_bucket("monthly", 30.0, 100.0)], created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-20T12:00:00+09:00",
                            buckets=[_bucket("monthly", 40.0, 100.0)], created_at="x")
    rows = latest_official_snapshot(conn, "claude")
    assert len(rows) == 1
    assert rows[0]["used_usd"] == 40.0     # 최신 스냅샷만


def test_official_buckets_idempotent_refresh():
    conn = connect(":memory:")
    for _ in range(2):  # 같은 fetched_at·bucket_key 재삽입 → 멱등(중복 행 없음)
        insert_official_buckets(conn, provider="claude", fetched_at="2026-06-20T10:00:00+09:00",
                                buckets=[_bucket("monthly", 30.0, 100.0)], created_at="x")
    count = conn.execute("SELECT COUNT(*) c FROM official_buckets").fetchone()["c"]
    assert count == 1


def test_official_bucket_series_ordered():
    conn = connect(":memory:")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-20T12:00:00+09:00",
                            buckets=[_bucket("monthly", 40.0, 100.0)], created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-20T10:00:00+09:00",
                            buckets=[_bucket("monthly", 30.0, 100.0)], created_at="x")
    series = official_bucket_series(conn, "claude", "monthly")
    assert [r["used_usd"] for r in series] == [30.0, 40.0]   # fetched_at 오름차순


def test_fetch_state_roundtrip():
    conn = connect(":memory:")
    assert get_fetch_state(conn, "claude") is None
    upsert_fetch_state(conn, "claude", last_attempt_at="t1", last_success_at="t1",
                       last_status="ok", last_error=None)
    st = get_fetch_state(conn, "claude")
    assert st["last_status"] == "ok"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_db.py -k official_buckets -v`
Expected: FAIL — `ImportError: cannot import name 'insert_official_buckets'`

- [ ] **Step 3: SCHEMA에 테이블 추가**

`tokenomy/db.py`의 `SCHEMA` 문자열 끝(`idx_official_provider_month` 인덱스 다음, 닫는 `"""` 앞)에 추가:

```sql
CREATE TABLE IF NOT EXISTS official_buckets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT,
    fetched_at TEXT,        -- 스냅샷 as-of(로컬 fetch 완료 시각, KST ISO). 같은 값 = 한 스냅샷
    bucket_key TEXT,        -- 'monthly'|'event'|'promo'|'rate_window'
    raw_key TEXT,           -- 원 API 키(코드네임/창 이름) — 다중 충돌 시 분리키
    bucket_kind TEXT,       -- 'monthly_limit'|'event_credit'|'promo'|'rate_window'|'codex_monthly'
    label TEXT,
    native_unit TEXT,       -- 'usd'|'credit'|'percent'
    used_native REAL, limit_native REAL, remaining_native REAL,
    used_usd REAL, limit_usd REAL, remaining_usd REAL,
    utilization REAL,
    resets_at TEXT,
    created_at TEXT,
    UNIQUE(provider, fetched_at, bucket_key, raw_key)
);
CREATE INDEX IF NOT EXISTS idx_official_buckets_lookup
    ON official_buckets(provider, fetched_at);

CREATE TABLE IF NOT EXISTS official_fetch_state (
    provider TEXT PRIMARY KEY,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_status TEXT,
    last_error TEXT
);
```

- [ ] **Step 4: DB 함수 구현**

`tokenomy/db.py` 끝(`official_series` 아래)에 추가:

```python
def _iso(dt) -> str | None:
    """datetime → ISO 문자열, None은 그대로(official_buckets.resets_at 저장용)."""
    return dt.isoformat() if dt is not None else None


def insert_official_buckets(conn, *, provider: str, fetched_at: str,
                            buckets: list, created_at: str) -> int:
    """한 스냅샷의 버킷 전부를 단일 트랜잭션으로 적재. 적재한 버킷 수 반환.

    UNIQUE(provider, fetched_at, bucket_key, raw_key) + INSERT OR REPLACE로
    같은 스냅샷 재취득(새로고침)이 멱등하게 처리된다(부분 스냅샷·중복 방지).
    buckets는 official_parser.OfficialBucket 리스트(duck-typed).
    """
    with conn:   # 트랜잭션(예외 시 롤백)
        for b in buckets:
            conn.execute(
                "INSERT OR REPLACE INTO official_buckets "
                "(provider, fetched_at, bucket_key, raw_key, bucket_kind, label, native_unit, "
                " used_native, limit_native, remaining_native, used_usd, limit_usd, remaining_usd, "
                " utilization, resets_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (provider, fetched_at, b.bucket_key, b.raw_key, b.bucket_kind, b.label,
                 b.native_unit, b.used_native, b.limit_native, b.remaining_native,
                 b.used_usd, b.limit_usd, b.remaining_usd, b.utilization,
                 _iso(b.resets_at), created_at),
            )
    return len(buckets)


def latest_official_snapshot(conn, provider: str) -> list:
    """provider의 가장 최근 fetched_at에 속한 버킷 행 전부(없으면 빈 리스트)."""
    row = conn.execute(
        "SELECT MAX(fetched_at) m FROM official_buckets WHERE provider=?", (provider,)
    ).fetchone()
    if not row or not row["m"]:
        return []
    return conn.execute(
        "SELECT * FROM official_buckets WHERE provider=? AND fetched_at=? ORDER BY id",
        (provider, row["m"]),
    ).fetchall()


def official_bucket_series(conn, provider: str, bucket_key: str) -> list:
    """provider·bucket_key의 (fetched_at, used_usd, used_native) 시계열(오름차순).

    예측 렌즈의 used 차분 계산용. 같은 bucket_key 다중(raw_key)이면 합산 없이 전부 반환.
    """
    return conn.execute(
        "SELECT fetched_at, used_usd, used_native FROM official_buckets "
        "WHERE provider=? AND bucket_key=? ORDER BY fetched_at ASC, id ASC",
        (provider, bucket_key),
    ).fetchall()


def get_fetch_state(conn, provider: str):
    return conn.execute(
        "SELECT * FROM official_fetch_state WHERE provider=?", (provider,)
    ).fetchone()


def upsert_fetch_state(conn, provider: str, *, last_attempt_at: str | None,
                       last_success_at: str | None, last_status: str,
                       last_error: str | None) -> None:
    conn.execute(
        "INSERT INTO official_fetch_state "
        "(provider, last_attempt_at, last_success_at, last_status, last_error) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(provider) DO UPDATE SET "
        "  last_attempt_at=excluded.last_attempt_at, "
        "  last_success_at=COALESCE(excluded.last_success_at, official_fetch_state.last_success_at), "
        "  last_status=excluded.last_status, last_error=excluded.last_error",
        (provider, last_attempt_at, last_success_at, last_status, last_error),
    )
    conn.commit()
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_db.py -k "official_buckets or fetch_state or bucket_series" -v`
Expected: PASS (5개)

- [ ] **Step 6: 전체 DB 테스트 회귀 확인**

Run: `.venv\Scripts\python -m pytest tests/test_db.py -v`
Expected: PASS (구 official_usage 테스트 포함 전부 — additive라 기존 깨지지 않음)

- [ ] **Step 7: 커밋**

```bash
git add tokenomy/db.py tests/test_db.py
git commit -m "feat(db): official_buckets/official_fetch_state 테이블 + 스냅샷 트랜잭션 함수"
```

---

## Task 4: 집계 — Codex 주간 윈도우 헬퍼

로컬 CLI 메시지 ts로 Codex 주간 윈도우(첫-사용 앵커 7일, 유휴 후 재앵커)를 계산하는 순수 헬퍼.

**Files:**
- Modify: `tokenomy/aggregate.py` (`codex_burndown` 위에 헬퍼 추가)
- Test: `tests/test_aggregate.py`

**Interfaces:**
- Produces: `codex_weekly_window(conn, now_kst: datetime) -> tuple[datetime, datetime] | None` — 현재(가장 최근) 7일 윈도우 `[start, end)`. Codex 사용 없으면 None.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_aggregate.py` 끝에 추가(파일 상단 import에 `codex_weekly_window` 추가):

```python
from tokenomy.aggregate import codex_weekly_window
from datetime import timedelta


def test_codex_weekly_window_anchors_on_first_use():
    conn = connect(":memory:")
    _insert(conn, "2026-06-08T01:00:00Z", 5.0, provider="codex", session="a")  # 첫 사용
    _insert(conn, "2026-06-10T01:00:00Z", 5.0, provider="codex", session="b")
    now = datetime(2026, 6, 11, 12, 0, tzinfo=KST)
    start, end = codex_weekly_window(conn, now)
    assert start.date().isoformat() == "2026-06-08"   # 첫 사용 KST 날짜(+9 → 10:00)
    assert (end - start) == timedelta(days=7)


def test_codex_weekly_window_reanchors_after_idle():
    conn = connect(":memory:")
    _insert(conn, "2026-06-01T01:00:00Z", 5.0, provider="codex", session="a")
    _insert(conn, "2026-06-12T01:00:00Z", 5.0, provider="codex", session="b")  # 11일 뒤(>7) → 재앵커
    now = datetime(2026, 6, 13, 12, 0, tzinfo=KST)
    start, _ = codex_weekly_window(conn, now)
    assert start.date().isoformat() == "2026-06-12"   # 마지막 사용으로 재앵커


def test_codex_weekly_window_none_without_usage():
    conn = connect(":memory:")
    _insert(conn, "2026-06-08T01:00:00Z", 5.0, provider="claude", session="a")  # claude만
    now = datetime(2026, 6, 11, 12, 0, tzinfo=KST)
    assert codex_weekly_window(conn, now) is None
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k codex_weekly_window -v`
Expected: FAIL — `ImportError: cannot import name 'codex_weekly_window'`

- [ ] **Step 3: 헬퍼 구현**

`tokenomy/aggregate.py`의 `def codex_burndown(` 정의 바로 위에 추가:

```python
def codex_weekly_window(conn, now_kst: datetime) -> tuple[datetime, datetime] | None:
    """Codex 주간 윈도우 [start, end) — 로컬 CLI 첫 사용 앵커 + 유휴 후 재앵커.

    메시지 ts(KST)를 오름차순 순회하며, 현재 윈도우(start)에서 7일 이상 벗어난 첫
    메시지마다 그 시점으로 재앵커한다. 연속 사용이면 7일마다 타일링되고, 7일+ 유휴면
    다음 사용일이 새 앵커가 된다(유휴 기간은 윈도우를 소비하지 않음). end = start + 7일.
    Codex 사용이 전혀 없으면 None. 공식 누적 스냅샷은 cadence가 희소해 앵커 관측에
    부적합하므로 로컬 메시지 ts를 1차 근거로 쓴다.
    """
    rows = conn.execute(
        "SELECT ts FROM messages WHERE provider='codex' ORDER BY ts ASC"
    ).fetchall()
    ws: datetime | None = None
    for r in rows:
        dt = parse_ts(r["ts"])
        if dt is None:
            continue
        if ws is None or dt >= ws + timedelta(days=7):
            ws = dt
    if ws is None:
        return None
    return ws, ws + timedelta(days=7)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k codex_weekly_window -v`
Expected: PASS (3개)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): Codex 주간 윈도우 헬퍼(첫-사용 앵커, 유휴 재앵커)"
```

---

## Task 5: 집계 — `official_view` (Claude 버킷 + Codex 2게이지 + 예측 렌즈)

최신 스냅샷 + 로컬 주간 윈도우를 합쳐 화면용 `OfficialView`를 만든다. 구 `official_merged_burndown`과 공존(제거는 Task 9).

**Files:**
- Modify: `tokenomy/aggregate.py` (dataclass + `official_view` 추가, `codex_weekly_window` 아래)
- Test: `tests/test_aggregate.py`

**Interfaces:**
- Consumes: `latest_official_snapshot`, `official_bucket_series`, `get_fetch_state` (Task 3); `codex_weekly_window` (Task 4); `Budget` (`budget.codex`)
- Produces:
  - `OfficialLens(bucket_key, daily_rate_usd, exhaust_date, days_left_to_reset, dday_warning)`
  - `OfficialView(provider, buckets, active_key, lens, period_used_usd, period_limit_usd, weekly_used_usd, weekly_limit_usd, weekly_estimated, weekly_window_end, fetched_at, stale_minutes, status, note)` — `buckets`는 표시용 dict 리스트
  - `official_view(conn, provider, now_kst, budget, credit_to_usd, *, budget_start=None) -> OfficialView`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_aggregate.py` 끝에 추가(상단 import에 `official_view`, `OfficialView` 추가):

```python
from tokenomy.aggregate import official_view, OfficialView
from tokenomy.db import insert_official_buckets
from tokenomy.official_parser import OfficialBucket


def _ob(key, kind, used_usd, limit_usd, raw="r", unit="usd", util=0.0, resets=None):
    return OfficialBucket(
        bucket_key=key, raw_key=raw, bucket_kind=kind, label=key, native_unit=unit,
        used_native=used_usd, limit_native=limit_usd,
        remaining_native=(limit_usd - used_usd) if limit_usd else None,
        used_usd=used_usd, limit_usd=limit_usd,
        remaining_usd=(limit_usd - used_usd) if limit_usd else None,
        utilization=util, resets_at=resets,
    )


def test_official_view_no_data_status():
    conn = connect(":memory:")
    v = official_view(conn, "claude", NOW, Budget(claude=100, codex=50), 0.04)
    assert isinstance(v, OfficialView)
    assert v.status == "no_data"
    assert v.buckets == []


def test_official_view_claude_monthly_period():
    conn = connect(":memory:")
    insert_official_buckets(
        conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
        buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend"),
                 _ob("event", "event_credit", 125.0, 500.0, raw="cinder")],
        created_at="2026-06-10T09:00:00+09:00",
    )
    v = official_view(conn, "claude", NOW, Budget(claude=100, codex=50), 0.04)
    assert v.status == "ok"
    assert v.period_used_usd == 30.0 and v.period_limit_usd == 100.0
    assert {b["bucket_key"] for b in v.buckets} == {"monthly", "event"}
    # 월 버킷 resets_at은 다음 달 경계로 채워짐
    monthly = next(b for b in v.buckets if b["bucket_key"] == "monthly")
    assert monthly["resets_at"].startswith("2026-07-01")


def test_official_view_codex_weekly_from_local():
    conn = connect(":memory:")
    # 로컬 Codex 사용(주간 used 근거)
    _insert(conn, "2026-06-09T01:00:00Z", 12.0, provider="codex", session="a")
    # 공식 월간 한도(주간 한도 = 80/4 = 20)
    insert_official_buckets(
        conn, provider="codex", fetched_at="2026-06-10T09:00:00+09:00",
        buckets=[_ob("monthly", "codex_monthly", 20.0, 80.0, raw="individual_limit",
                     unit="credit", util=25.0)],
        created_at="2026-06-10T09:00:00+09:00",
    )
    now = datetime(2026, 6, 11, 12, 0, tzinfo=KST)
    v = official_view(conn, "codex", now, Budget(claude=100, codex=50), 0.04)
    assert v.period_used_usd == 20.0 and v.period_limit_usd == 80.0  # 월간(공식)
    assert v.weekly_limit_usd == 20.0      # 공식 월 한도 80 ÷ 4
    assert v.weekly_used_usd == 12.0       # 로컬 윈도우 합(첫 사용 6/9~)
    assert v.weekly_estimated is True


def test_official_view_codex_weekly_fallback_budget():
    conn = connect(":memory:")
    _insert(conn, "2026-06-09T01:00:00Z", 5.0, provider="codex", session="a")
    now = datetime(2026, 6, 11, 12, 0, tzinfo=KST)
    # 공식 없음 → 주간 한도 = budget.codex(50) ÷ 4 = 12.5, 월간은 no_data
    v = official_view(conn, "codex", now, Budget(claude=100, codex=50), 0.04)
    assert v.weekly_limit_usd == 12.5
    assert v.weekly_used_usd == 5.0
    assert v.period_used_usd is None       # 공식 월간 없음


def test_official_view_lens_from_series():
    conn = connect(":memory:")
    # 두 스냅샷(차분 → 일일 소비속도). 6/8 used 10 → 6/10 used 30, 2영업일 차분 20 → 10/영업일
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-08T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 10.0, 100.0, raw="spend")],
                            created_at="x")
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[_ob("monthly", "monthly_limit", 30.0, 100.0, raw="spend")],
                            created_at="x")
    v = official_view(conn, "claude", NOW, Budget(claude=100, codex=50), 0.04)
    assert v.lens is not None
    assert v.lens.daily_rate_usd == 10.0   # (30-10) / 2 영업일
    assert v.active_key == "monthly"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k official_view -v`
Expected: FAIL — `ImportError: cannot import name 'official_view'`

- [ ] **Step 3: dataclass + `official_view` 구현**

`tokenomy/aggregate.py`의 `codex_weekly_window` 정의 아래에 추가:

```python
# 공식 미러 패널의 버킷 표시 순서(공식 앱 미러).
_BUCKET_ORDER = {"monthly_limit": 0, "codex_monthly": 0, "event_credit": 1, "promo": 2, "rate_window": 3}


@dataclass
class OfficialLens:
    """예측 렌즈 — 활성 버킷의 소비 속도/소진예상/리셋 D-day."""
    bucket_key: str
    daily_rate_usd: float | None    # USD/영업일. 유효 차분 1개 미만이면 None
    exhaust_date: date | None
    days_left_to_reset: int | None  # 현재 주기 리셋까지 영업일
    dday_warning: bool


@dataclass
class OfficialView:
    """공식 미러 패널 1개(provider별) — 버킷 + 주기 USD + 예측 렌즈 + 상태."""
    provider: str
    buckets: list[dict]                 # 표시용(버킷 행 dict, 표시 순서)
    active_key: str | None
    lens: OfficialLens | None
    period_used_usd: float | None       # 월간(공식). 없으면 None
    period_limit_usd: float | None
    weekly_used_usd: float | None       # Codex 주간(로컬 추정). Claude=None
    weekly_limit_usd: float | None
    weekly_estimated: bool
    weekly_window_end: date | None
    fetched_at: str | None
    stale_minutes: int | None
    status: str                         # "ok" | "no_data" | fetch_state.last_status
    note: str | None


def _row_to_bucket_dict(r) -> dict:
    """official_buckets 행 → 표시용 dict(resets_at은 ISO 문자열)."""
    d = dict(r)
    return {
        "bucket_key": d["bucket_key"], "raw_key": d["raw_key"], "bucket_kind": d["bucket_kind"],
        "label": d["label"], "native_unit": d["native_unit"],
        "used_native": d["used_native"], "limit_native": d["limit_native"],
        "remaining_native": d["remaining_native"],
        "used_usd": d["used_usd"], "limit_usd": d["limit_usd"], "remaining_usd": d["remaining_usd"],
        "utilization": d["utilization"], "resets_at": d["resets_at"],
    }


def _lens_from_series(conn, provider: str, bucket_key: str, now_kst: datetime,
                      limit_usd: float | None, used_usd: float | None,
                      reset_date: date | None) -> OfficialLens | None:
    """official_bucket_series의 단조 증가 차분으로 일일 소비속도·소진예상·리셋 D-day 산출.

    음수 차분(리셋/만료)·30분 미만 간격은 버린다. 유효 차분이 없으면 daily_rate=None.
    """
    from tokenomy.db import official_bucket_series

    series = official_bucket_series(conn, provider, bucket_key)
    pts = []
    for r in series:
        dt = parse_ts(r["fetched_at"])
        if dt is not None and r["used_usd"] is not None:
            pts.append((dt, r["used_usd"]))
    pts.sort(key=lambda x: x[0])

    rate: float | None = None
    if len(pts) >= 2:
        first_dt, first_u = pts[0]
        last_dt, last_u = pts[-1]
        delta = last_u - first_u
        bdays = business_days_between(first_dt.date(), last_dt.date())
        if delta > 0 and bdays > 0 and (last_dt - first_dt) >= timedelta(minutes=30):
            rate = round(delta / bdays, 4)

    exhaust_date: date | None = None
    if rate and limit_usd and used_usd is not None and limit_usd > used_usd:
        need = math.ceil((limit_usd - used_usd) / rate)
        exhaust_date = add_business_days(now_kst.date(), need)

    days_left = business_days_between(now_kst.date(), reset_date) if reset_date else None
    dday = bool((exhaust_date is not None and reset_date is not None and exhaust_date < reset_date)
                or (limit_usd and used_usd is not None and used_usd / limit_usd >= 0.80))
    return OfficialLens(bucket_key=bucket_key, daily_rate_usd=rate, exhaust_date=exhaust_date,
                        days_left_to_reset=days_left, dday_warning=dday)


def official_view(conn, provider: str, now_kst: datetime, budget: Budget,
                  credit_to_usd: float, *, budget_start: datetime | None = None) -> OfficialView:
    """공식 미러 패널 컨텍스트. 최신 스냅샷(공식) + 로컬 주간 윈도우(Codex)를 합친다.

    - period_used/limit = 월간 버킷(공식 ground truth). 없으면 None.
    - Codex weekly_used = 로컬 CLI 첫-사용 7일 윈도우 합(추정), weekly_limit = 공식 월÷4 또는 budget.codex÷4.
    - Claude 월 버킷 resets_at None은 다음 달 경계(KST)로 채운다.
    - 활성 버킷 = series 양의 차분이 가장 큰 것(동률은 [event,monthly] tie-break),
      차분 없으면 차감 순서 remaining>0 첫 버킷. promo/rate_window/stale은 후보 제외.
    """
    from tokenomy.db import latest_official_snapshot, get_fetch_state

    rows = latest_official_snapshot(conn, provider)
    fetched_at = rows[0]["fetched_at"] if rows else None
    _, next_month = month_bounds(now_kst)

    buckets = [_row_to_bucket_dict(r) for r in rows]
    # Claude 월 버킷 resets_at 보강(다음 달 경계)
    for b in buckets:
        if b["bucket_kind"] in ("monthly_limit", "codex_monthly") and not b["resets_at"]:
            b["resets_at"] = next_month.isoformat()
    buckets.sort(key=lambda b: _BUCKET_ORDER.get(b["bucket_kind"], 9))

    monthly = next((b for b in buckets if b["bucket_kind"] in ("monthly_limit", "codex_monthly")), None)
    period_used = monthly["used_usd"] if monthly else None
    period_limit = monthly["limit_usd"] if monthly else None

    # staleness(분)
    stale_minutes = None
    if fetched_at:
        dt = parse_ts(fetched_at)
        if dt is not None:
            stale_minutes = max(0, int((now_kst - dt).total_seconds() // 60))

    # 상태
    if rows:
        status = "ok"
    else:
        st = get_fetch_state(conn, provider)
        status = st["last_status"] if st else "no_data"

    # Codex 주간(로컬 추정)
    weekly_used = weekly_limit = None
    weekly_estimated = False
    weekly_end: date | None = None
    if provider == "codex":
        win = codex_weekly_window(conn, now_kst)
        if win is not None:
            ws, we = win
            wrows = _range_rows(conn, "codex", ws, we)
            weekly_used = round(sum((r["cost_usd"] or 0) for r in wrows), 4)
            weekly_estimated = True
            weekly_end = we.date()
        # 주간 한도 = 공식 월 한도÷4(있으면) 아니면 budget.codex÷4
        if period_limit:
            weekly_limit = round(period_limit / 4, 4)
        elif budget.codex:
            weekly_limit = round(budget.codex / 4, 4)

    # 활성 버킷 + 렌즈
    active_key = None
    lens = None
    candidates = [b for b in buckets if b["bucket_kind"] in
                  ("monthly_limit", "event_credit", "codex_monthly")]
    if candidates:
        # 차감 순서 tie-break: event 먼저, 그다음 monthly
        order = {"event_credit": 0, "monthly_limit": 1, "codex_monthly": 1}
        candidates.sort(key=lambda b: order.get(b["bucket_kind"], 9))
        active = next((b for b in candidates if (b["remaining_usd"] or 0) > 0), candidates[0])
        active_key = active["bucket_key"]
        reset_date = parse_ts(active["resets_at"]).date() if active["resets_at"] else None
        if provider == "codex":
            reset_date = weekly_end or reset_date
        lens = _lens_from_series(conn, provider, active_key, now_kst,
                                 active["limit_usd"], active["used_usd"], reset_date)

    note = None if rows else "공식 미취득 — 로컬 추정(USD)"
    return OfficialView(
        provider=provider, buckets=buckets, active_key=active_key, lens=lens,
        period_used_usd=period_used, period_limit_usd=period_limit,
        weekly_used_usd=weekly_used, weekly_limit_usd=weekly_limit,
        weekly_estimated=weekly_estimated, weekly_window_end=weekly_end,
        fetched_at=fetched_at, stale_minutes=stale_minutes, status=status, note=note,
    )
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k official_view -v`
Expected: PASS (5개)

- [ ] **Step 5: 집계 전체 회귀 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -v`
Expected: PASS (기존 + 신규)

- [ ] **Step 6: 커밋**

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): official_view(Claude 버킷+Codex 2게이지+예측 렌즈)"
```

---

## Task 6: 뷰 — `overview_context`를 공식 미러로 전환

히어로 게이지(구 max-병합)를 제거하고 공식 미러 패널(Claude/Codex `official_view`)로 교체. 합산은 월 누적 유지.

**Files:**
- Modify: `tokenomy/web/views.py:1-151` (import, `_gauge`/`_official_notes` 제거, `overview_context` 재배선)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `official_view`, `credit_to_usd` (Task 1/5)
- Produces: `overview_context`가 컨텍스트에 `claude_official: OfficialView`, `codex_official: OfficialView` 추가. `gauge`/`official_notes` 키 제거.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_web.py` 끝에 추가:

```python
def test_overview_has_official_panels(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    conn.execute("INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,cost_usd,priced) "
                 "VALUES ('a','codex','s1','p','2026-06-10T10:00:00Z','gpt-5.5',5.0,1)")
    conn.commit()
    r = client.get("/")
    assert r.status_code == 200
    # 구 수동 입력 폼/문구 제거 확인
    assert 'action="/official"' not in r.text
    assert "공식 사용량 입력" not in r.text


def test_overview_context_keys(tmp_path, monkeypatch):
    from tokenomy.web.views import overview_context
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    ctx = overview_context(conn, "cost")
    assert "claude_official" in ctx and "codex_official" in ctx
    assert "gauge" not in ctx and "official_notes" not in ctx
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -k "official_panels or context_keys" -v`
Expected: FAIL — `assert "claude_official" in ctx` (KeyError 부재) / 폼 문구 잔존

- [ ] **Step 3: import 교체**

`tokenomy/web/views.py` 상단 aggregate import에서 `official_merged_burndown` 제거, `official_view` 추가:

```python
from tokenomy.aggregate import (
    KST, DIM_COLUMNS, DateGroup, DaySessionRow, FolderGroup, burndown,
    by_day_session, by_dimension, by_project, by_session, codex_burndown,
    combined_burndown, daily_series, insights, month_bounds,
    official_view, period_bounds,
    pricing_coverage, session_detail, sidechain_split, stacked_trend,
    token_composition,
)
from tokenomy.budget import (
    budget_from_config, budget_start_kst, credit_to_usd, load_config, user_label,
)
```

- [ ] **Step 4: `_gauge`/`_official_notes` 제거**

`tokenomy/web/views.py:38-73`의 `_official_notes`·`_gauge` 두 함수를 통째로 삭제한다.

- [ ] **Step 5: `overview_context` 재배선**

`overview_context`에서 `claude_merged`/`gauge`/`official_notes` 관련 줄을 교체한다. 다음 블록을:

```python
    # 게이지(히어로) = Claude 회사 월 할당 기준. 공식 사용량을 max 병합해 웹/앱 누락을 줄인다.
    # 공식 입력은 Claude-only(Codex 공식 기능 미출시) — Codex는 아래 별도 섹션.
    claude_merged = official_merged_burndown(conn, budget, now, "claude", budget_start=bs)
    claude_bd = claude_merged.burndown
    codex_bd = codex_burndown(conn, budget, now, budget_start=bs)
    month_total = round(claude_bd.spent + codex_bd.spent, 4)
```

다음으로 바꾼다:

```python
    # 카드 번다운(로컬 추정). 공식 ground truth는 아래 공식 미러 패널이 별도로 보여준다.
    claude_bd = burndown(conn, budget, now, "claude", budget_start=bs)
    codex_bd = codex_burndown(conn, budget, now, budget_start=bs)
    month_total = round(claude_bd.spent + codex_bd.spent, 4)

    # 공식 미러 패널(provider별) — USD 1차. Claude=버킷, Codex=월간+주간 2게이지.
    ctu = credit_to_usd(config)
    claude_official = official_view(conn, "claude", now, budget, ctu, budget_start=bs)
    codex_official = official_view(conn, "codex", now, budget, ctu, budget_start=bs)
```

그리고 반환 dict에서 `gauge`/`official_notes` 줄을:

```python
        "gauge": _gauge(claude_merged), "official_notes": _official_notes(claude_merged),
```

다음으로 바꾼다:

```python
        "claude_official": claude_official, "codex_official": codex_official,
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -k "official_panels or context_keys" -v`
Expected: PASS (2개)

(주: Step 1의 폼 부재 검사는 Task 7에서 템플릿을 고쳐야 완전 통과. 이 태스크에서 폼 문구가 잔존하면 `test_overview_has_official_panels`가 실패할 수 있다 → 그 검사는 Task 7로 미룬다. 본 태스크 Step 1에서는 `test_overview_context_keys`만 추가하고, 폼 부재 검사는 Task 7 Step 1에 둔다.)

- [ ] **Step 7: 커밋**

```bash
git add tokenomy/web/views.py tests/test_web.py
git commit -m "feat(views): overview를 공식 미러 패널(official_view)로 전환, 구 게이지 제거"
```

---

## Task 7: 템플릿 — 공식 미러 패널(Claude 버킷 + Codex 2게이지) + 설정/배너 정리

수동 입력 폼·구 게이지 섹션을 공식 미러 패널로 교체. `credit_to_usd`를 설정에 노출.

**Files:**
- Modify: `tokenomy/web/templates/overview.html:27-55`
- Modify: `tokenomy/web/templates/base.html:15-18` (official 배너 제거)
- Modify: `tokenomy/web/templates/settings.html` (credit_to_usd 표시)
- Modify: `tokenomy/web/views.py` (settings 컨텍스트에 credit_to_usd 추가 — app.py 경유) 또는 `tokenomy/web/app.py:settings_get`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `claude_official`/`codex_official` (Task 6)

- [ ] **Step 1: 실패하는 테스트 작성(폼 부재 + 패널 표시 + 설정)**

`tests/test_web.py` 끝에 추가:

```python
def test_overview_official_panel_renders(tmp_path, monkeypatch):
    client, conn_factory = _client(tmp_path, monkeypatch)
    conn = conn_factory()
    from tokenomy.db import insert_official_buckets
    from tokenomy.official_parser import OfficialBucket
    conn_b = OfficialBucket(
        bucket_key="monthly", raw_key="spend", bucket_kind="monthly_limit", label="월 사용 한도",
        native_unit="usd", used_native=30.0, limit_native=100.0, remaining_native=70.0,
        used_usd=30.0, limit_usd=100.0, remaining_usd=70.0, utilization=30.0, resets_at=None,
    )
    insert_official_buckets(conn, provider="claude", fetched_at="2026-06-10T09:00:00+09:00",
                            buckets=[conn_b], created_at="2026-06-10T09:00:00+09:00")
    r = client.get("/")
    assert r.status_code == 200
    assert 'action="/official"' not in r.text     # 수동 입력 폼 제거
    assert "공식" in r.text                         # 공식 미러 패널 노출


def test_settings_shows_credit_to_usd(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "credit_to_usd" in r.text or "크레딧" in r.text
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -k "official_panel_renders or credit_to_usd" -v`
Expected: FAIL — 구 폼 잔존 / credit_to_usd 미노출

- [ ] **Step 3: overview.html 공식 패널로 교체**

`tokenomy/web/templates/overview.html`의 `{% if claude_bd.limit > 0 %}` ~ 대응 `{% endif %}`(line 27~55) 블록 전체를 다음으로 교체:

```html
{# 공식 미러 패널 — USD 1차. 공식 ground truth + 토크노미 예측 렌즈 #}
<section class="card">
  <h2>공식 사용량 <span class="muted">· 공식 앱 미러 + 예측</span></h2>

  {# Claude — 버킷(월 한도 → 이벤트 → 프로모션 → 이용률 창) #}
  {% if claude_official.status == "ok" %}
  <div class="official-provider">
    <h3>Claude</h3>
    {% for b in claude_official.buckets %}
    <div class="official-bucket">
      <div class="ai-name">{{ b.label }}
        {% if b.native_unit == "percent" %}<span class="muted">· {{ '%.0f'|format(b.utilization) }}% 사용됨</span>{% endif %}</div>
      {% if b.used_usd is not none and b.limit_usd %}
        <div class="ai-num">${{ '{:,.2f}'.format(b.used_usd) }} <span class="ai-denom">/ ${{ '{:,.0f}'.format(b.limit_usd) }}</span></div>
        <span class="bar"><span class="fill s-ok" style="width: {{ [b.utilization, 100]|min }}%"></span></span>
        <div class="muted">{{ '%.0f'|format(b.utilization) }}% 사용됨{% if b.resets_at %} · 리셋 {{ b.resets_at[:10] }}{% endif %} · <span class="src-official">공식</span></div>
      {% endif %}
    </div>
    {% endfor %}
    {% if claude_official.lens and claude_official.lens.daily_rate_usd is not none %}
    <p class="muted">예측: ${{ '%.2f'|format(claude_official.lens.daily_rate_usd) }}/영업일{% if claude_official.lens.exhaust_date %} · 소진 예상 {{ claude_official.lens.exhaust_date }}{% endif %}</p>
    {% endif %}
  </div>
  {% else %}
  <p class="muted">Claude 공식 미취득 — 로컬 추정 ${{ '{:,.2f}'.format(claude_bd.spent) }} <span class="src-est">추정</span></p>
  {% endif %}

  {# Codex — 월간(공식) + 주간(예측) 2게이지 #}
  <div class="official-provider">
    <h3>Codex</h3>
    {% if codex_official.period_limit_usd %}
    <div class="official-bucket">
      <div class="ai-name">월간 한도 <span class="muted">· 공식</span></div>
      <div class="ai-num">${{ '{:,.2f}'.format(codex_official.period_used_usd) }} <span class="ai-denom">/ ${{ '{:,.0f}'.format(codex_official.period_limit_usd) }}</span></div>
      <span class="bar"><span class="fill s-ok" style="width: {{ [codex_official.period_used_usd / codex_official.period_limit_usd * 100, 100]|min if codex_official.period_limit_usd else 0, 100]|min }}%"></span></span>
      <div class="muted"><span class="src-official">공식</span></div>
    </div>
    {% endif %}
    {% if codex_official.weekly_limit_usd %}
    <div class="official-bucket">
      <div class="ai-name">이번 주 <span class="muted">· 예측(월÷4)</span></div>
      <div class="ai-num">${{ '{:,.2f}'.format(codex_official.weekly_used_usd or 0) }} <span class="ai-denom">/ ${{ '{:,.0f}'.format(codex_official.weekly_limit_usd) }}</span></div>
      <span class="bar"><span class="fill s-ok" style="width: {{ [(codex_official.weekly_used_usd or 0) / codex_official.weekly_limit_usd * 100, 100]|min }}%"></span></span>
      <div class="muted">{% if codex_official.weekly_window_end %}리셋 {{ codex_official.weekly_window_end }} · {% endif %}<span class="src-est">추정</span></div>
    </div>
    {% endif %}
    {% if not codex_official.period_limit_usd and not codex_official.weekly_limit_usd %}
    <p class="muted">Codex 공식·로컬 데이터 없음</p>
    {% endif %}
  </div>
  <p class="disclaimer">ⓘ <span class="src-official">공식</span>=API ground truth · <span class="src-est">추정</span>=로컬 CLI 토큰단가</p>
</section>
```

- [ ] **Step 4: base.html 구 official 배너 제거**

`tokenomy/web/templates/base.html:15-18`의 다음 두 `{% elif %}` 블록을 삭제:

```html
      {% elif notice == "official-saved" %}
      <div class="banner">공식 사용량을 저장했습니다.</div>
      {% elif notice == "official-invalid" %}
      <div class="banner error">공식 사용량 입력값이 올바르지 않습니다 — 0 이상 숫자를 입력하세요.</div>
```

- [ ] **Step 5: settings에 credit_to_usd 노출**

`tokenomy/web/app.py`의 `settings_get` 반환 컨텍스트에 `credit_to_usd` 추가. 다음 줄:

```python
        {"claude": budget.claude, "codex": budget.codex,
         "budget_start": config.get("budget_start") or "",
```

을:

```python
        {"claude": budget.claude, "codex": budget.codex,
         "budget_start": config.get("budget_start") or "",
         "credit_to_usd": config.get("credit_to_usd", 0.04),
```

으로 바꾸고, `tokenomy/web/templates/settings.html`의 예산 입력 근처에 표시 블록 추가(예산 `budget_start` 필드 아래):

```html
    <label>credit_to_usd <span class="muted">(크레딧→USD 환산, 기본 0.04)</span>
      <input type="number" step="0.001" min="0" name="credit_to_usd" value="{{ credit_to_usd }}"></label>
```

그리고 `tokenomy/web/app.py`의 `settings_post` 시그니처·본문에 반영:

```python
@app.post("/settings")
def settings_post(claude: str = Form(""), codex: str = Form(""),
                  budget_start: str = Form(""), credit_to_usd: str = Form("")):
    config = load_config()
    config["budget"]["claude"] = _to_float(claude)
    config["budget"]["codex"] = _to_float(codex)
    config["budget_start"] = _valid_date_or_none(budget_start)
    ctu = _to_float(credit_to_usd)
    config["credit_to_usd"] = ctu if ctu > 0 else 0.04
    save_config(config)
    return RedirectResponse("/", status_code=303)
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -v`
Expected: PASS (신규 + 기존 — 단, POST /official 관련 기존 테스트가 있으면 Task 8/9에서 정리)

- [ ] **Step 7: CSS 클래스 추가(선택) 후 빌드**

`src/input.css`에 `.official-provider`/`.official-bucket`/`.src-official`/`.src-est` 스타일이 필요하면 `@layer components`에 추가하고 빌드. 최소 동작엔 불필요(기존 `.card`/`.bar`/`.muted` 재사용).

Run(클래스 추가 시에만): `.\build_css.ps1`

- [ ] **Step 8: 커밋**

```bash
git add tokenomy/web/templates/ tokenomy/web/app.py tests/test_web.py
git commit -m "feat(web): 공식 미러 패널 템플릿(Claude 버킷+Codex 2게이지)+credit_to_usd 설정"
```

---

## Task 8: CLI `official import-fixture` + 로컬 fixture gitignore

fixture JSON을 파서→DB로 주입하는 dev 명령(라이브 없이 user-verifiable 경로). 실측 원문은 gitignore.

**Files:**
- Modify: `tokenomy/cli.py` (import + `main` 디스패치 + `cmd_official_import`)
- Modify: `.gitignore`
- Test: `tests/test_cli.py` (신규)

**Interfaces:**
- Consumes: `parse_claude`/`parse_codex` (Task 2), `insert_official_buckets` (Task 3), `credit_to_usd` (Task 1)
- Produces: `python -m tokenomy.cli official import-fixture <provider> <path>`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_cli.py`:

```python
import json
from datetime import datetime

from tokenomy.aggregate import KST
from tokenomy.cli import cmd_official_import
from tokenomy.db import connect, latest_official_snapshot


def test_official_import_claude(tmp_path):
    raw = {
        "spend": {"used": {"amount_minor": 3000, "exponent": 2},
                  "limit": {"amount_minor": 10000, "exponent": 2}},
        "extra_usage": {"monthly_limit": 10000},
    }
    p = tmp_path / "c.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    conn = connect(":memory:")
    n = cmd_official_import(conn, "claude", str(p), now_kst=datetime(2026, 6, 10, 9, tzinfo=KST),
                           credit_to_usd_value=0.04)
    assert n == 1
    rows = latest_official_snapshot(conn, "claude")
    assert rows[0]["used_usd"] == 30.0 and rows[0]["limit_usd"] == 100.0


def test_official_import_codex(tmp_path):
    raw = {"spend_control": {"individual_limit": {
        "limit": "2000", "used": "500.0", "remaining": "1500.0",
        "used_percent": 25, "reset_at": 1782864001}}}
    p = tmp_path / "x.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    conn = connect(":memory:")
    n = cmd_official_import(conn, "codex", str(p), now_kst=datetime(2026, 6, 10, 9, tzinfo=KST),
                           credit_to_usd_value=0.04)
    assert n == 1
    rows = latest_official_snapshot(conn, "codex")
    assert rows[0]["used_usd"] == 20.0   # 500 * 0.04
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_cli.py -v`
Expected: FAIL — `ImportError: cannot import name 'cmd_official_import'`

- [ ] **Step 3: CLI 구현**

`tokenomy/cli.py` 상단 import에 추가:

```python
import json
from tokenomy.official_parser import parse_claude, parse_codex
from tokenomy.db import connect, ingest_root, ingest_titles, ingest_user_turns, maybe_reprice, insert_official_buckets
from tokenomy.budget import budget_from_config, load_config, user_label, credit_to_usd
```

(기존 `from tokenomy.db import ...`·`from tokenomy.budget import ...` 줄을 위 두 줄로 대체 — 중복 import 금지.)

`cmd_ingest` 아래에 추가:

```python
def cmd_official_import(conn, provider: str, path: str, *, now_kst=None,
                        credit_to_usd_value: float | None = None) -> int:
    """fixture/실측 raw JSON을 파서→DB로 주입하는 dev 명령(라이브 없이 검증용). 적재 버킷 수 반환.

    provider: 'claude' | 'codex'. now_kst/credit_to_usd_value는 테스트 주입용(미지정 시 실값).
    """
    now = now_kst or datetime.now(KST)
    ctu = credit_to_usd_value if credit_to_usd_value is not None else credit_to_usd(load_config())
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    parse = parse_claude if provider == "claude" else parse_codex
    buckets = parse(raw, credit_to_usd=ctu)
    ts = now.isoformat()
    return insert_official_buckets(conn, provider=provider, fetched_at=ts,
                                   buckets=buckets, created_at=ts)
```

`main`의 디스패치에 분기 추가(`elif cmd == "all":` 다음, `else:` 앞):

```python
    elif cmd == "official" and len(argv) >= 4 and argv[1] == "import-fixture":
        provider = argv[2] if argv[2] in ("claude", "codex") else "claude"
        n = cmd_official_import(conn, provider, argv[3])
        print(f"[official] {provider} 버킷 {n}개 적재")
```

그리고 `else:` 사용법 문자열 갱신:

```python
        print("usage: python -m tokenomy.cli [ingest|report|all|official import-fixture <claude|codex> <path>]")
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_cli.py -v`
Expected: PASS (2개)

- [ ] **Step 5: .gitignore에 로컬 fixture/실측 추가**

`.gitignore`의 `*.local.json` 줄 아래에 추가:

```
# 공식 사용량 실측 원문·로컬 fixture(가짜 아님) — 커밋 금지(커밋 정본은 tests/fixtures/official/*.json)
tests/fixtures/official/local/
```

- [ ] **Step 6: 수동 검증(user-verifiable 경로)**

Run:
```bash
.venv\Scripts\python -m tokenomy.cli official import-fixture claude tests/fixtures/official/claude_enterprise.json
.venv\Scripts\python -m tokenomy.cli official import-fixture codex tests/fixtures/official/codex_enterprise.json
```
Expected: `[official] claude 버킷 N개 적재` / `[official] codex 버킷 1개 적재`. 이후 `python -m uvicorn tokenomy.web.app:app --port 8765`로 대시보드에서 공식 미러 패널(Claude 버킷 + Codex 2게이지)이 보이는지 눈으로 확인.

- [ ] **Step 7: 커밋**

```bash
git add tokenomy/cli.py tests/test_cli.py .gitignore
git commit -m "feat(cli): official import-fixture(파서→DB 주입, 라이브 없이 검증)"
```

---

## Task 9: 정리 — 구 `official_usage`/`POST /official`/`official_merged_burndown` 제거 + 마이그레이션 DROP + 문서

수동 입력 경로의 잔재를 모두 제거하고 구 테이블을 DROP. 기존 테스트 정리 + CLAUDE.md/README 갱신.

**Files:**
- Modify: `tokenomy/web/app.py` (`POST /official`, 미사용 import 제거)
- Modify: `tokenomy/aggregate.py` (`official_merged_burndown`, `OfficialMergedBurndown` 제거)
- Modify: `tokenomy/db.py` (SCHEMA에서 `official_usage` 제거, 구 함수 3개 제거, `_migrate`에 DROP 추가)
- Modify: `tests/test_db.py`, `tests/test_aggregate.py` (구 official_usage/merged 테스트 제거)
- Modify: `CLAUDE.md`, `README.md`
- Test: 전체 스위트

**Interfaces:**
- Removes: `db.insert_official_snapshot`(구 단일값), `db.latest_official`, `db.official_series`, `aggregate.official_merged_burndown`, `aggregate.OfficialMergedBurndown`, `POST /official`

- [ ] **Step 1: 구 테스트 제거(먼저 — green 유지)**

`tests/test_db.py`에서 구 official_usage를 쓰는 테스트와 import를 제거한다:
- import 줄에서 `insert_official_snapshot`(구), `latest_official`, `official_series` 삭제(Task 3에서 추가한 `insert_official_buckets` 등은 유지).
- 이 심볼들을 쓰는 테스트 함수 삭제(`grep`으로 확인: `latest_official(`/`official_series(`/구 `insert_official_snapshot(provider=..., target_month=...`).

`tests/test_aggregate.py`에서:
- import의 `official_merged_burndown`, `OfficialMergedBurndown` 삭제.
- `official_merged_burndown(`/`insert_official_snapshot(` 사용 테스트 삭제.

`tests/test_web.py`에서 `/official`(POST) 관련 테스트가 있으면 삭제.

확인:
```bash
git grep -n "official_merged_burndown\|OfficialMergedBurndown\|latest_official(\|official_series(\|insert_official_snapshot" tests/
```
Expected: 매치 없음(또는 신규 `insert_official_buckets`만).

- [ ] **Step 2: app.py 구 라우트/임포트 제거**

`tokenomy/web/app.py`:
- `from tokenomy.db import connect, insert_official_snapshot` → `from tokenomy.db import connect` 로.
- `@app.post("/official")` 함수(`official_post`)와 `_nonneg_float_or_none` 헬퍼 삭제(다른 곳에서 안 쓰면).

확인: `git grep -n "insert_official_snapshot\|official_post\|_nonneg_float_or_none" tokenomy/` → 매치 없음.

- [ ] **Step 3: aggregate.py 구 병합 제거**

`tokenomy/aggregate.py`에서 `@dataclass class OfficialMergedBurndown:` 블록과 `def official_merged_burndown(...)` 함수 전체를 삭제(line ~285-340).

- [ ] **Step 4: db.py 구 함수/스키마 제거 + 마이그레이션 DROP**

`tokenomy/db.py`:
- SCHEMA에서 `official_usage` CREATE TABLE + `idx_official_provider_month` 인덱스 블록(line 83-92) 삭제.
- 구 함수 `insert_official_snapshot`(단일값), `latest_official`, `official_series`(line 378-423) 삭제. (Task 3에서 추가한 `insert_official_buckets`/`latest_official_snapshot`/`official_bucket_series`는 유지 — 이름이 달라 충돌 없음.)
- `_migrate`에 DROP 단계 추가. `_migrate` 함수 본문 끝(`conn.commit()` 앞)에:

```python
    # 구 단일값 official_usage 폐기(멀티버킷 official_buckets로 대체). 로컬 단일 사용자라 이관 없음.
    # _migrate는 executescript(SCHEMA)보다 먼저 실행되고 SCHEMA에서 CREATE를 뺐으므로 재생성되지 않는다.
    conn.execute("DROP TABLE IF EXISTS official_usage")
```

- [ ] **Step 5: 전체 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest -q`
Expected: PASS (전체). 실패 시 잔존 참조를 `git grep`으로 찾아 제거.

- [ ] **Step 6: 마이그레이션 수동 확인(구 DB에 official_usage가 있어도 안전)**

Run:
```bash
.venv\Scripts\python -c "import sqlite3, tokenomy.db as d; c=sqlite3.connect(':memory:'); c.execute('CREATE TABLE official_usage(id INTEGER)'); c.commit(); d._migrate(c); c.executescript(d.SCHEMA); print('tables:', [r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")])"
```
Expected: 출력 테이블 목록에 `official_usage` 없음, `official_buckets`/`official_fetch_state` 있음.

- [ ] **Step 7: 문서 갱신**

`CLAUDE.md`:
- 아키텍처/db.py 설명에서 `official_usage`(단일값) 언급을 `official_buckets`(멀티버킷 USD) + `official_fetch_state`로 갱신.
- 게시(gotchas)에 한 줄 추가: "공식 사용량은 멀티버킷(USD 통일) — Claude 버킷/Codex 월간+주간(월÷4, 로컬 첫-사용 앵커). `credit_to_usd`(config, 기본 0.04)로 크레딧 환산, 토큰 cost 경로와 분리."

`README.md`(있으면): 공식 사용량 수동 입력 → 자동(Phase 2)·fixture 주입(`official import-fixture`) 설명으로 갱신. "Adding a parser" 인접에 official_parser 한 줄.

- [ ] **Step 8: 커밋**

```bash
git add tokenomy/ tests/ CLAUDE.md README.md
git commit -m "refactor(official): 구 official_usage/POST /official/merged 제거 + 테이블 DROP 마이그레이션"
```

---

## Self-Review

**1. Spec coverage (Phase 1 항목 §11):**
- 스키마 교체·마이그레이션 → Task 3(추가) + Task 9(DROP). ✓
- `official_parser`(USD 환산) → Task 2. ✓
- `aggregate.official_view`(리셋 주기·주간 윈도우·예측) → Task 4(윈도우) + Task 5(view/lens). ✓
- views/템플릿 공식 패널 + 합산 USD + 예측 렌즈 → Task 6(context, 합산은 기존 combined 유지) + Task 7(템플릿). ✓
- 수동 입력 제거 → Task 7(폼) + Task 9(라우트/테이블). ✓
- `official import-fixture` → Task 8. ✓
- sanitize fixture 커밋 + 로컬 gitignore → Task 2(커밋) + Task 8(gitignore). ✓
- credit_to_usd(config, 토큰 경로 분리) → Task 1. ✓
- UNIQUE raw_key / fetched_at 의미 / 활성버킷 결정성 / 소스 플래그 → Task 3(UNIQUE) / Task 5(active+lens) / Task 7(공식·추정 배지). ✓
- 테스트(희소/월경계/재앵커) → Task 4(재앵커) + Task 5(series/no_data). 희소-스냅샷은 Task 5 lens가 차분<2면 None으로 처리(테스트 `test_official_view_no_data_status`/`lens_from_series`로 커버). ✓

**2. Placeholder scan:** "TBD/적절히 처리/등등" 없음. 모든 코드 step에 완전한 코드. ✓

**3. Type consistency:**
- `OfficialBucket` 필드 = Task 2 정의 ↔ Task 3 insert ↔ Task 5 `_row_to_bucket_dict` 일치. ✓
- `official_view(conn, provider, now_kst, budget, credit_to_usd, *, budget_start=None)` 시그니처 = Task 5 정의 ↔ Task 6 호출(`official_view(conn, "claude", now, budget, ctu, budget_start=bs)`) 일치. ✓
- `cmd_official_import(conn, provider, path, *, now_kst, credit_to_usd_value)` = Task 8 정의 ↔ 테스트 호출 일치. `main` 디스패치는 기본 인자로 호출. ✓
- 신규 DB 함수명(`insert_official_buckets`)이 구 `insert_official_snapshot`과 달라 Task 3~8 공존 기간에 충돌 없음. Task 9에서 구만 제거. ✓

주의(구현자 참고): Task 6 Step 1의 폼-부재 단언은 Task 7에서 템플릿을 고쳐야 통과하므로, 그 단언은 Task 7 Step 1에 두고 Task 6에서는 컨텍스트 키 검사만 한다(본문에 명시). 두 태스크를 연속 실행하면 자연 해소.
