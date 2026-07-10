"""설정 모델.

사용자 config(tokenomy.config.json)에서 앱 설정을 읽는다.
config가 없으면 기본값으로 동작한다. example 파일은 템플릿일 뿐
자동 로드하지 않는다(사용자가 복사해서 tokenomy.config.json을 만든다).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from tokenomy.domain import PROVIDERS, is_pooled_kind
from tokenomy.paths import creds_present

# 프로세스 내 동시 save 직렬화 — 미니 위치 저장(moved)·뷰 전환(last_view)·모드 시드가
# 서로 다른 스레드(UI/트레이/백그라운드)에서 겹쳐 호출되므로 write 임계구역을 잠근다.
_SAVE_LOCK = threading.Lock()

# Windows에서 os.replace는 대상 파일을 다른 스레드/프로세스가 열고 있으면(리더의 read_text 등)
# PermissionError로 막힌다 — 리더의 open 창은 수 ms라 짧게 물러났다 재시도해 흡수한다.
_REPLACE_ATTEMPTS = 25
_REPLACE_BACKOFF = 0.004


def _default_label() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or "me"


def _config_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get("TOKENOMY_CONFIG")
    if env:
        return Path(env)
    from tokenomy.paths import config_path
    return config_path()


def load_config(path: str | Path | None = None) -> dict:
    base = {"user_label": _default_label(),
            "tracked_providers": None,           # None → 첫 호출 시 크레덴셜로 시드
            "account_mode": None,                # None → 첫 공식 취득 성공 때 데이터로 자동 시드(ADR 0015)
            "credit_to_usd": 0.04,
            "official_fetch": {"min_interval_minutes": 10},
            "forecast_settings": {"rate_window_weeks": 2},
            "debug_mode": False,                 # 디버그 관측 게이트(ADR 0014) — 숨겨진 7탭으로 토글
            "bucket_overrides": {},              # 코드네임 버킷 로컬 큐레이션(ADR 0016) — {provider:raw_key:{hidden?,pooled?,label?}}
            "pricing_overrides": {}}
    p = _config_path(path)
    if not p.exists():
        return base
    try:
        loaded = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):             # 손상 JSON('Extra data' 등) / 읽기 실패
        _quarantine_corrupt(p)               # 손상 파일을 브릭시키지 않는다 — 격리 후 기본값 복구
        return base
    if not isinstance(loaded, dict):         # 유효 JSON이나 최상위가 dict 아님(리스트/스칼라) → 손상 취급
        _quarantine_corrupt(p)
        return base
    base.update(loaded)                       # top-level 키 덮어쓰기
    return base


def _quarantine_corrupt(p: Path) -> None:
    """손상된 config를 `*.corrupt`로 격리(원본 바이트는 진단용 보존, 원본은 치운다).

    이렇게 두면 (1) 다음 load가 재손상을 안 보고 기본값으로 뜨며 (2) 다음 save가 새 유효
    config를 써서 자가 회복한다. 격리 자체 실패(권한 등)는 조용히 삼킨다 — 어떤 경우에도
    load_config가 예외로 앱을 브릭하지 않는 것이 이 함수의 유일한 계약이다."""
    try:
        backup = p.with_name(p.name + ".corrupt")
        os.replace(p, backup)                 # 덮어쓰기(직전 손상분 대체) — 원자적 rename
        print(f"[config] 손상된 설정 파일 격리 후 기본값 복구: {p} → {backup}")
    except OSError:
        pass


def save_config(config: dict, path: str | Path | None = None) -> None:
    """config를 원자적으로 파일에 기록 — 부분 기록·동시 쓰기로 인한 손상을 원천 차단한다.

    같은 디렉터리 temp 파일에 완전히 쓴 뒤 `os.replace`로 원자 교체한다. 그래서 리더는 항상
    완전한 파일(옛것 또는 새것)만 보고, 프로세스 종료가 쓰기 도중에 끼어도 원본이 남는다
    (옛 비원자 write_text는 두 스레드가 겹치면 'Extra data'로 파일을 깨 앱을 브릭시켰다).
    `_SAVE_LOCK`으로 프로세스 내 동시 save를 직렬화한다. temp 파일명은 **쓰기 주체별로 고유**
    (PID+스레드) — `_SAVE_LOCK`은 프로세스 로컬이라, 만약 다른 프로세스(예: CLI)가 동시에
    save하면 공유 temp명은 서로의 temp를 덮어/지워 FileNotFoundError·오염을 낼 수 있다.
    토큰 write-back의 `_atomic_write_json`(ADR 0021)과 동형이나, config엔 비밀이 없어 0600은 제외."""
    p = _config_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(config, indent=2, ensure_ascii=False)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with _SAVE_LOCK:
        try:
            tmp.write_text(data, encoding="utf-8")
            for attempt in range(_REPLACE_ATTEMPTS):
                try:
                    os.replace(tmp, p)
                    break
                except PermissionError:       # Windows 리더가 p를 연 순간 — 잠깐 뒤 재시도
                    if attempt == _REPLACE_ATTEMPTS - 1:
                        raise
                    time.sleep(_REPLACE_BACKOFF)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def user_label(config: dict) -> str:
    return config.get("user_label") or _default_label()


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


def debug_mode(config: dict) -> bool:
    """디버그 관측 모드(ADR 0014)가 켜졌는지. 기본 False.

    켜지면 사용 이력(공식) 화면의 raw 링크와 `/official/raw` 라우트가 노출된다(포착 자체는
    이 값과 무관하게 항상 ON). 사이드바 버전 7회 탭으로 토글하고 config에 영속한다.
    """
    return bool(config.get("debug_mode"))


def official_fetch_settings(config: dict) -> dict:
    """공식 사용량 갱신 설정. min_interval_minutes는 '자동 갱신 간격' — 창이 열린 동안
    갱신을 자동 폴링하는 주기이자 자동 호출의 최소 간격(수동 갱신은 무시한다).
    on/off·provider 게이트는 tracked_providers가 담당한다. 누락·오설정은 기본 10분으로 폴백한다.
    auto_refresh_token="auto(안전망)|always(강제)|off", auto_refresh_safety_hours="auto 안전망 임계(시간, 1~168)"."""
    raw = config.get("official_fetch") or {}
    try:
        mi = int(raw.get("min_interval_minutes", 10))
    except (TypeError, ValueError):
        mi = 10
    # background_poll: 상주 모드 중 창 숨김과 무관하게 공식 갱신을 주기 폴할지(ADR 0007).
    # 기본 ON(콜드스타트 방지) — 끄면 갱신은 페이지 폴링·창 복원·수동 버튼에만 일어난다.
    mode = raw.get("auto_refresh_token")
    if mode not in ("auto", "always", "off"):
        mode = "auto"
    try:
        sh = int(raw.get("auto_refresh_safety_hours", 24))
    except (TypeError, ValueError):
        sh = 24
    sh = min(max(sh, 1), 168)
    return {"min_interval_minutes": mi if mi > 0 else 10,
            "background_poll": bool(raw.get("background_poll", True)),
            "auto_refresh_token": mode,
            "auto_refresh_safety_hours": sh}


def forecast_settings(config: dict) -> dict:
    """전망 소비속도(기울기) 설정. rate_window_weeks="트레일링 창 길이(주)" — 기울기를
    추정하는 최근 창의 주(週) 수(ADR 0004 후속: 소비속도=리셋 무관 행동 속성, 트레일링 창).
    1~8주로 clamp, 미설정·오설정은 기본 2주로 폴백한다(오설정으로 전망이 깨지지 않게)."""
    raw = config.get("forecast_settings") or {}
    try:
        w = int(raw.get("rate_window_weeks", 2))
    except (TypeError, ValueError):
        w = 2
    return {"rate_window_weeks": min(max(w, 1), 8)}


def mini_view_settings(config: dict) -> dict:
    """미니 뷰(상주 모드 안의 배타 전환 글랜스 창, ADR 0008) 표시 설정.

    last_view = 마지막 본 뷰("main"|"mini"). 기본 "main"(최초 실행·미설정·알 수 없는 값은
    일반뷰로 안전 복원). 트레이 "열기"·재실행·재시작이 이 값으로 복원한다.
    x/y = 마지막 미니 창 위치(int|None). 미설정·비숫자는 None으로 폴백 → 런처가 기본 코너에 둔다
    (오설정 좌표로 창 배치가 깨지지 않게). 위치 저장은 미니 창의 moved 이벤트가 담당한다.
    """
    raw = config.get("mini_view") or {}

    def _coord(v) -> int | None:
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    last_view = raw.get("last_view")
    if last_view not in ("main", "mini"):
        last_view = "main"
    return {"last_view": last_view,
            "x": _coord(raw.get("x")), "y": _coord(raw.get("y"))}


def tracked_providers(config: dict) -> list[str]:
    """사용자가 앱에서 보기로 켠 provider(=활성 AI) 목록.

    config['tracked_providers']가 리스트면 PROVIDERS 순서로 정규화(알 수 없는 값 제거).
    이때 **빈 리스트는 빈 집합으로 그대로 보존**한다 — "전부 끄기"는 사용자가 명시한
    영속 상태이므로 재시드하지 않는다(다시 켜기 전까지 전 화면에서 모두 숨김).
    키 자체가 없거나 None(미설정)이면 크레덴셜 파일이 있는 provider로 시드한다
    (무설정 첫 실행이 대개 정답). 전부 끄기를 원하면 UI에서 모두 해제하면 된다.
    """
    raw = config.get("tracked_providers")
    if isinstance(raw, list):                          # 명시 설정(빈 리스트 포함) → 그대로 정규화
        return [p for p in PROVIDERS if p in raw]       # [] → [] 영속(재시드 안 함)
    # None(미설정) → 크레덴셜 파일 존재 기반 시드. 공식 취득 전체 차단은 TOKENOMY_SKIP_OFFICIAL_FETCH.
    return [p for p in PROVIDERS if creds_present(p)]


ACCOUNT_MODES = ("enterprise", "subscription")


def account_mode(config: dict) -> str | None:
    """확정된 계정 사용 형태("enterprise"|"subscription"). 미설정/알 수 없으면 None(ADR 0015).

    공식 USD 예산 한도가 있는 형태(엔터프라이즈/API)와 정액 구독제를 가르는 계정 단위 단일
    토글이다(per-provider/혼합 모드는 만들지 않는다). 미설정(키 없음·None)이거나 오타·알 수 없는
    값이면 None을 돌려준다 — None은 "첫 공식 취득 성공 때 데이터로 자동 시드될 미확정 상태"를 뜻하고,
    오설정으로 모드 게이트가 깨지지 않게 한다(tracked_providers None 시드 패턴과 동형).
    """
    raw = config.get("account_mode")
    return raw if raw in ACCOUNT_MODES else None


def seed_account_mode(config: dict, *, has_usd_budget: bool,
                      path: str | Path | None = None) -> str:
    """미설정이면 공식 데이터로 계정 모드를 자동 시드·영속하고, 명시값이면 존중한다(ADR 0015).

    이미 명시 설정(account_mode가 enterprise|subscription)이면 데이터와 무관하게 그대로
    반환하고 **덮어쓰지 않는다** — 사용자가 토글로 정한 값이 정본(sticky). 미설정이면
    `has_usd_budget`(공식 USD 예산 한도 버킷이 하나라도 왔는가)으로 enterprise/subscription을
    확정해 config dict에 기록하고 `save_config`로 파일에 영속한 뒤 반환한다.
    `has_usd_budget`은 호출자(첫 공식 취득 성공 경로)가 OfficialView.pool_limit_usd로 판별해 넘긴다.
    반환값은 항상 확정 모드("enterprise"|"subscription").
    """
    existing = account_mode(config)
    if existing is not None:                  # 명시값 존중 — 영속하지 않음
        return existing
    mode = "enterprise" if has_usd_budget else "subscription"
    config["account_mode"] = mode
    save_config(config, path)
    return mode


def resolve_bucket_curation(provider: str, raw_key: str, bucket_kind: str,
                            *, catalog: dict | None = None,
                            overrides: dict | None = None) -> dict:
    """버킷 큐레이션 3축 {hidden, pooled, label} 해석(ADR 0016, 순수 함수).

    키는 `provider:raw_key`. 모양 기본값에서 출발해 카탈로그 → 오버라이드 순으로 **축별 부분
    병합**(지정한 축만 덮어쓰고 미지정 축은 하위 레이어/모양값 유지) → 우선순위 오버라이드 >
    카탈로그 > 모양 기본값. 모양 기본값: hidden=False(표시), pooled=안정 월 한도 키만 True
    (회전 코드네임 달러 크레딧은 제외 — opt-in), label=None(parser 라벨 유지).
    """
    result = {"hidden": False,
              "pooled": is_pooled_kind(bucket_kind),
              "label": None}
    key = f"{provider}:{raw_key}"
    for layer in (catalog, overrides):          # 카탈로그 → 오버라이드(나중 레이어가 이김)
        entry = (layer or {}).get(key)
        if not isinstance(entry, dict):
            continue
        if "hidden" in entry:
            result["hidden"] = bool(entry["hidden"])
        if "pooled" in entry:
            result["pooled"] = bool(entry["pooled"])
        if entry.get("label"):                  # 빈 문자열/None은 라벨 미지정으로 본다
            result["label"] = entry["label"]
    return result


def load_bucket_catalog(path: str | Path | None = None) -> dict:
    """배포 버킷 카탈로그(config/bucket_catalog.json) — 코드네임 버킷의 보편 큐레이션 사실.

    pricing.json과 동형으로 `resource_path`로 번들(repo 커밋 + exe 동봉 + 릴리스 배포).
    `{"buckets": {provider:raw_key: {hidden?,pooled?,label?}}}` 래퍼 또는 평면 dict 모두 허용.
    파일 없거나 깨지면 빈 dict(큐레이션 없음 = 모양 기본값으로 안전 폴백).
    """
    if path is None:
        from tokenomy.paths import resource_path
        path = resource_path("config/bucket_catalog.json")
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    buckets = data.get("buckets", data)         # 래퍼 또는 평면 모두 수용
    return buckets if isinstance(buckets, dict) else {}


def bucket_overrides(config: dict) -> dict:
    """로컬 버킷 오버라이드({provider:raw_key: {hidden?,pooled?,label?}}). 미설정/오설정은 {}."""
    raw = config.get("bucket_overrides")
    return raw if isinstance(raw, dict) else {}


def bucket_curation_resolver(config: dict):
    """배포 카탈로그 + 로컬 오버라이드를 묶은 큐레이션 해석기(ADR 0016).

    반환 callable `(provider, raw_key, bucket_kind) -> {hidden,pooled,label}`. 카탈로그는
    한 번 로드해 클로저에 가둔다(호출마다 파일 IO 없음). views가 이걸로 hidden/label(표시)과
    is_pooled(aggregate 풀 멤버십)를 함께 끌어낸다 — 표시·풀 일관(pooled 불변식).
    """
    catalog = load_bucket_catalog()
    overrides = bucket_overrides(config)

    def resolve(provider: str, raw_key: str, bucket_kind: str) -> dict:
        return resolve_bucket_curation(provider, raw_key, bucket_kind,
                                       catalog=catalog, overrides=overrides)
    return resolve


def onboarding_pending(config: dict) -> bool:
    """완전 신규 진입자(빈 시드) 온보딩 상태인지 판정.

    `tracked_providers` 키가 **미설정(None)**이고 — 즉 사용자가 명시적으로 끄지 않았고 —
    크레덴셜 기반 시드 결과가 비어 있으면(활성 AI 0개) True. 이는 Claude Code/Codex를
    한 번도 실행/로그인하지 않은 사용자를 뜻하며, 대시보드를 빈 껍데기 대신 시작 안내로 띄운다.
    명시적 빈 리스트(`[]`)는 '사용자가 전부 끔'이라 온보딩이 아니다 — 기존 "설정에서 켜세요"를 유지.
    """
    if config.get("tracked_providers") is not None:   # 명시 설정(빈 리스트 포함) → 온보딩 아님
        return False
    return tracked_providers(config) == []            # 미설정 + 빈 시드 = 크레덴셜 없음
