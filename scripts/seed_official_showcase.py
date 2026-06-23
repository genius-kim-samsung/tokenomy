"""공식 사용량 카드 재설계(ADR 0002)의 시각 상태를 눈으로 확인하기 위한 dev 쇼케이스 시드.

상태가 상호 배타라(폴백은 공식이 없어야, 에러는 공식이 있어야, rate-window는 개인 구독제)
세 격리 DB(=세 시나리오)를 만든다:

  A(엔터프라이즈 게이지):  Claude 멀티버킷(녹/앰버/적 3색 + active 버킷 고스트 예측·월간/이벤트),
                            Codex(공식 월간 USD 게이지)
  B(에러+폴백):           Claude(직전 스냅샷 스탈 게이지 + auth_error → ⚠갱신실패),
                            Codex(공식 없음 → 사용량 전용 폴백: 추정 + 스파크라인)
  C(개인 구독제 rate-window): Claude 5시간/7일(All)/7일(Sonnet), Codex 5시간/7일(All).
                            미니 뷰 % 뒤 리셋 카운트다운(· N단위)·라벨 변형 확인용(ADR 0012).

전 과정 로컬·네트워크 없음(official_fetch 우회). 개인 DB를 건드리지 않게 별도 디렉토리에 적재한다.

사용:
    .venv\\Scripts\\python scripts\\seed_official_showcase.py
    # → 세 포트 실행 안내 출력(8801=A, 8802=B, 8803=C). 미니는 /mini.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)


def _bucket(key, kind, label, used, limit, util, resets_at=None, unit="usd"):
    from tokenomy.official_parser import OfficialBucket
    return OfficialBucket(
        bucket_key=key, raw_key=key, bucket_kind=kind, label=label, native_unit=unit,
        used_native=used, limit_native=limit, remaining_native=max(limit - used, 0),
        used_usd=used, limit_usd=limit, remaining_usd=max(limit - used, 0),
        utilization=util, resets_at=resets_at,
    )


def _msg(conn, provider, key, ts, cost):
    conn.execute(
        "INSERT INTO messages (dedup_key,provider,session_id,project,ts,model,cost_usd,priced) "
        "VALUES (?,?,?,?,?,?,?,1)",
        (key, provider, "s_" + key, "showcase", ts, "model-x", cost),
    )


def _fresh_db(data_dir: Path):
    """격리 데이터 디렉토리의 빈 DB 커넥션. 기존 파일은 지운다(시나리오 재현 결정성)."""
    from tokenomy.db import connect
    db = data_dir / "data" / "tokenomy.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    return connect(str(db)), db


def seed_gauges(data_dir: Path) -> Path:
    """시나리오 A — 임계 3색 + 고스트 예측 + 추정 해치."""
    from tokenomy.db import insert_official_buckets
    conn, db = _fresh_db(data_dir)

    from tokenomy.official_parser import OfficialBucket
    expiry = datetime(2026, 9, 10, tzinfo=KST)              # 일회성 크레딧 만료일(sub에 '만료 …')
    reset_month = NOW + timedelta(days=20)
    old = (NOW - timedelta(days=5)).isoformat()             # 고스트용 이전 스냅샷(fetched_at=문자열)
    new = NOW.isoformat()

    # 실제 API promo는 달러값 없이 utilization(%)만 가진다 — exceeds(적색) 시연용 percent 버킷.
    promo = OfficialBucket(
        bucket_key="promo", raw_key="promo", bucket_kind="promo", label="별도/프로모션",
        native_unit="percent", used_native=None, limit_native=None, remaining_native=None,
        used_usd=None, limit_usd=None, remaining_usd=None, utilization=95.0, resets_at=reset_month,
    )

    # 이전 스냅샷 — active(event) 버킷만 낮은 used로(차분 → lens 일속도 + active 선정)
    insert_official_buckets(conn, provider="claude", fetched_at=old, created_at=old, buckets=[
        _bucket("event", "event_credit", "이벤트", 700.0, 1000.0, 70.0, expiry),
    ])
    # 최신 스냅샷 — 녹 39%(월간) / 앰버 82%+고스트(이벤트) / 적 95%(별도 프로모션)
    insert_official_buckets(conn, provider="claude", fetched_at=new, created_at=new, buckets=[
        _bucket("monthly", "monthly_limit", "월간", 95.0, 243.0, 39.1, reset_month),
        _bucket("event", "event_credit", "이벤트", 820.0, 1000.0, 82.0, expiry),
        promo,
    ])
    # Codex — 공식 월간(18% 녹). 크레딧 기반이라 USD는 환산값(×0.04) — 원본 크레딧(1,074/5,875)을 캡션에 병기.
    # 로컬 메시지는 lens(소비속도) 근거로 남긴다(추정 주간 게이지는 제거됨, ADR 0012).
    insert_official_buckets(conn, provider="codex", fetched_at=new, created_at=new, buckets=[
        OfficialBucket(
            bucket_key="monthly", raw_key="individual_limit", bucket_kind="codex_monthly",
            label="월간", native_unit="credit",
            used_native=1074.0, limit_native=5875.0, remaining_native=4801.0,
            used_usd=42.96, limit_usd=235.0, remaining_usd=192.04,
            utilization=18.3, resets_at=reset_month,
        ),
    ])
    for i, (d, c) in enumerate([(2, 12.0), (1, 10.0), (0, 13.0)]):
        _msg(conn, "codex", f"a{i}", (NOW - timedelta(days=d)).isoformat(), c)  # lens 소비속도 근거
    conn.commit()
    conn.close()
    return db


def seed_error_fallback(data_dir: Path) -> Path:
    """시나리오 B — 스탈 게이지+auth_error / 공식 없는 폴백(추정+스파크라인)."""
    from tokenomy.db import insert_official_buckets, upsert_fetch_state
    conn, db = _fresh_db(data_dir)

    new = NOW.isoformat()
    reset_month = NOW + timedelta(days=20)
    # Claude — 공식 스냅샷 있음(직전값) + 마지막 fetch 실패(auth_error) → 스탈 게이지 + ⚠
    insert_official_buckets(conn, provider="claude", fetched_at=new, created_at=new, buckets=[
        _bucket("monthly", "monthly_limit", "월간", 110.0, 243.0, 45.3, reset_month),
    ])
    upsert_fetch_state(conn, "claude", last_attempt_at=new, last_success_at=new,
                       last_status="auth_error", last_error="HTTP 401")
    # Codex — 공식 없음, 로컬만(여러 날) → 사용량 전용 폴백(추정 + 스파크라인)
    for i, (d, c) in enumerate([(12, 4.0), (9, 7.5), (6, 3.0), (3, 9.0), (1, 5.5)]):
        _msg(conn, "codex", f"b{i}", (NOW - timedelta(days=d)).isoformat(), c)
    conn.commit()
    conn.close()
    return db


def _rate_window(key, label, util, resets):
    """개인 구독제 rate-window 버킷(percent, USD 없음)."""
    from tokenomy.official_parser import OfficialBucket
    return OfficialBucket(
        bucket_key="rate_window", raw_key=key, bucket_kind="rate_window", label=label,
        native_unit="percent", used_native=None, limit_native=None, remaining_native=None,
        used_usd=None, limit_usd=None, remaining_usd=None, utilization=util, resets_at=resets,
    )


def seed_personal_rate_windows(data_dir: Path) -> Path:
    """시나리오 C — 개인 구독제 rate-window. 미니 카운트다운(· N단위)·라벨 변형 확인용."""
    from tokenomy.db import insert_official_buckets
    conn, db = _fresh_db(data_dir)
    new = NOW.isoformat()
    # Claude — 5시간(2h35m 후)·7일(All)(3d12h 후)·7일(Sonnet)(5d2h 후)
    insert_official_buckets(conn, provider="claude", fetched_at=new, created_at=new, buckets=[
        _rate_window("five_hour", "5시간", 42.0, NOW + timedelta(hours=2, minutes=35)),
        _rate_window("seven_day", "7일(All)", 18.0, NOW + timedelta(days=3, hours=12)),
        _rate_window("seven_day_sonnet", "7일(Sonnet)", 9.0, NOW + timedelta(days=5, hours=2)),
    ])
    # Codex — 5시간(40분 후, 분 단위 시연)·7일(All)(4일 후)
    insert_official_buckets(conn, provider="codex", fetched_at=new, created_at=new, buckets=[
        _rate_window("primary_window", "5시간", 60.0, NOW + timedelta(minutes=40)),
        _rate_window("secondary_window", "7일(All)", 33.0, NOW + timedelta(days=4)),
    ])
    conn.commit()
    conn.close()
    return db


def main() -> None:
    home = Path.home()
    a_dir = home / ".tokenomy-showcase-a"
    b_dir = home / ".tokenomy-showcase-b"
    c_dir = home / ".tokenomy-showcase-c"
    a_db = seed_gauges(a_dir)
    b_db = seed_error_fallback(b_dir)
    c_db = seed_personal_rate_windows(c_dir)

    print("쇼케이스 시드 완료(로컬·격리, 개인 DB 미오염).")
    print(f"  A 엔터프라이즈 게이지 → {a_db}")
    print(f"  B 에러+폴백          → {b_db}")
    print(f"  C 개인 rate-window   → {c_db}")
    print("\n세 포트로 실행해 확인(SKIP_OFFICIAL_FETCH 필수, 미니는 /mini):")
    for label, d, port in [("A 엔터프라이즈 게이지", a_dir, 8801), ("B 에러+폴백", b_dir, 8802),
                           ("C 개인 rate-window", c_dir, 8803)]:
        print(f"\n# {label}")
        print(f'  $env:TOKENOMY_DATA = "{d}"; $env:TOKENOMY_SKIP_OFFICIAL_FETCH = "1"; '
              f'.venv\\Scripts\\python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port {port}')
        print(f"  → http://127.0.0.1:{port}")


if __name__ == "__main__":
    main()
