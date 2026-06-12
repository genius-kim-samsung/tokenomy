"""수집 신선도 — 트리거가 다 실패해도 유실 위험을 사람에게 노출(설계 §5).

마지막 ingest 경과 + 디스크상 가장 오래된 raw 파일 나이(vs 30일 cleanup).
now는 주입받는다(테스트 가능 — aggregate.parse_ts와 동일 원칙).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tokenomy.aggregate import parse_ts
from tokenomy.db import get_meta, set_meta
from tokenomy.parser import discover_session_files

CLEANUP_DAYS = 30
WARN_AGE_DAYS = 25  # 가장 오래된 raw가 이 나이를 넘으면 경고(휘발 임박)
LAST_INGEST_KEY = "last_ingest_ts"


def record_ingest(conn, now: datetime) -> None:
    set_meta(conn, LAST_INGEST_KEY, now.isoformat())


@dataclass
class Freshness:
    last_ingest_ts: str | None
    hours_since_ingest: float | None
    oldest_raw_age_days: float | None
    level: str  # "ok" | "warn"


def freshness(conn, root: str | Path, now: datetime) -> Freshness:
    last = get_meta(conn, LAST_INGEST_KEY)
    hours = None
    dt = parse_ts(last) if last else None
    if dt is not None:
        hours = (now - dt).total_seconds() / 3600

    oldest_age = None
    files = discover_session_files(root)
    if files:
        oldest_mtime = min(f.stat().st_mtime for f in files)
        oldest_age = (now.timestamp() - oldest_mtime) / 86400

    level = "warn" if (oldest_age is not None and oldest_age >= WARN_AGE_DAYS) else "ok"
    return Freshness(
        last_ingest_ts=last,
        hours_since_ingest=hours,
        oldest_raw_age_days=oldest_age,
        level=level,
    )
