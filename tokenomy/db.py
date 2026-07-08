"""SQLite 적재.

- messages: 메시지별 토큰/비용 (dedup_key UNIQUE → 스트리밍 중복 제거)
- sessions: 세션 메타 + 수동 라벨(업무 귀속)
- users:    현재 미사용 — 향후 멀티유저 확장용 발판 테이블
- scan_offsets: 파일별 마지막 파싱 byte-offset (증분 스캔)

대화 본문 전체는 저장하지 않는다. Codex는 첫 사용자 프롬프트 발췌(≤120자)를 sessions.summary에 저장한다.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from tokenomy.parser import (
    UsageRecord,
    discover_session_files,
    parse_file,
    parse_titles,
)
from tokenomy.pricing import compute_cost, pricing_fingerprint

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT UNIQUE,
    provider TEXT,
    session_id TEXT,
    project TEXT,
    ts TEXT,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_creation INTEGER DEFAULT 0,
    cache_creation_1h INTEGER DEFAULT 0,
    cache_read INTEGER DEFAULT 0,
    web_search INTEGER DEFAULT 0,
    web_fetch INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    priced INTEGER DEFAULT 0,
    request_id TEXT,
    is_sidechain INTEGER DEFAULT 0,
    attribution_skill TEXT,
    git_branch TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_provider_ts ON messages(provider, ts);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project TEXT,
    provider TEXT,
    first_ts TEXT,
    last_ts TEXT,
    label TEXT,
    summary TEXT,
    user_turns INTEGER
);

CREATE TABLE IF NOT EXISTS session_day_turns (
    session_id TEXT,
    day TEXT,
    turns INTEGER,
    PRIMARY KEY (session_id, day)
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    tier TEXT,
    provider_choice TEXT
);

CREATE TABLE IF NOT EXISTS scan_offsets (
    path TEXT PRIMARY KEY,
    offset INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

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

-- 절약 수단(설치형)의 적용 상태 전이 이력(ADR 0026). 감지 함수가 로컬 설정을 읽어 3상태
-- (applied/not_applied/unknown)로 판정하고, 상태가 바뀐 시각만 append한다(읽은 내용 미적재
-- — 발췌선). "언제부터 적용했나"의 근거 + 후속 전/후 비교의 경계 시각으로 쌓는다.
CREATE TABLE IF NOT EXISTS saver_state_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    saver_id TEXT,
    provider TEXT,
    state TEXT,             -- 'applied'|'not_applied'|'unknown'
    changed_at TEXT         -- 전이 감지 시각(KST ISO)
);
CREATE INDEX IF NOT EXISTS idx_saver_transitions_lookup
    ON saver_state_transitions(saver_id, provider, id);

-- 공식 API raw 응답 디버그 포착(ADR 0014). PII 스크럽된 원문을 fetch마다 보관(7일 롤링).
-- official_buckets와 (provider, fetched_at)로 1:1 정렬. 디버그 보조라 버킷 영구이력과 분리.
CREATE TABLE IF NOT EXISTS official_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT,
    fetched_at TEXT,        -- 스냅샷 as-of(official_buckets와 동일 키)
    status TEXT,            -- 'ok'|'parse_error'|'http_error'
    http_code INTEGER,      -- HTTP 상태코드(네트워크 에러 등은 NULL)
    raw_text TEXT,          -- PII 스크럽 + 8KB cap된 응답 원문
    byte_len INTEGER,       -- 저장된 raw_text 바이트 길이
    created_at TEXT,
    UNIQUE(provider, fetched_at)
);
CREATE INDEX IF NOT EXISTS idx_official_raw_lookup
    ON official_raw(provider, fetched_at);
"""


# connect 시 기존 DB에 빠진 컬럼을 보강한다. CREATE TABLE IF NOT EXISTS는 기존
# 테이블 스키마를 바꾸지 않으므로(신규 컬럼 누락), 여기서 ALTER로 메운다.
# 순서대로 보강해야 인덱스 생성(executescript)이 성공한다.
_MIGRATE_COLS = {
    "messages": {
        "session_id": "TEXT",
        "project": "TEXT",
        "ts": "TEXT",
        "model": "TEXT",
        "input_tokens": "INTEGER DEFAULT 0",
        "output_tokens": "INTEGER DEFAULT 0",
        "cache_creation": "INTEGER DEFAULT 0",
        "cache_creation_1h": "INTEGER DEFAULT 0",
        "cache_read": "INTEGER DEFAULT 0",
        "web_search": "INTEGER DEFAULT 0",
        "web_fetch": "INTEGER DEFAULT 0",
        "cost_usd": "REAL DEFAULT 0",
        "priced": "INTEGER DEFAULT 0",
        "request_id": "TEXT",
        "is_sidechain": "INTEGER DEFAULT 0",
        "attribution_skill": "TEXT",
        "git_branch": "TEXT",
    },
    "sessions": {
        "summary": "TEXT",  # 세션 작업 요약(Claude Code aiTitle 캐시). raw 30일 휘발 대비 영구 보존.
        "user_turns": "INTEGER",  # 세션 내 사용자 턴 수(메시지 수 표시용).
    },
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, decls in _MIGRATE_COLS.items():
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not cols:
            # 테이블 자체가 없으면(신규 DB) SCHEMA가 생성하므로 스킵
            continue
        for col, decl in decls.items():
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    # 구 단일값 official_usage 폐기(멀티버킷 official_buckets로 대체). 로컬 단일 사용자라 이관 없음.
    # _migrate는 executescript(SCHEMA)보다 먼저 실행되고 SCHEMA에서 CREATE를 뺐으므로 재생성되지 않는다.
    conn.execute("DROP TABLE IF EXISTS official_usage")
    conn.commit()


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    if db_path is None:
        from tokenomy.paths import db_path as _default_db_path
        p = _default_db_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        target = str(p)
    else:
        target = str(db_path)
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    # 동시성(ADR 0023) — busy_timeout을 먼저 깔아 이후 PRAGMA/DDL도 잠금 대기로 보호하고,
    # 파일 DB는 WAL로 전환해 서빙 읽기가 백그라운드 수집 쓰기와 충돌하지 않게 한다.
    # :memory:는 WAL 불가(journal_mode=memory가 정상)라 건드리지 않는다.
    conn.execute("PRAGMA busy_timeout=5000")
    if target != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    _migrate(conn)
    conn.executescript(SCHEMA)
    return conn


def _dedup_key(r: UsageRecord) -> str:
    # (provider, message_id, request_id) 조합 — 같은 메시지의 리트라이(다른 request_id)는
    # 별개 과금이므로 별개 행으로 보존한다. ccusage의 (messageId, requestId) dedup과 동형.
    if r.message_id:
        return f"{r.provider}:{r.message_id}:{r.request_id or ''}"
    return f"{r.provider}:{r.session_id}:{r.ts}:{r.model}:{r.total_tokens}"


# 같은 dedup_key가 중복 기록될 때 어느 쪽을 남길지(ccusage should_replace_deduped_entry).
# ① 비sidechain(부모)이 sidechain replay를 이긴다  ② 같은 sidechain이면 토큰 총합이 큰 쪽(부분기록<완전기록).
_REPLACE_WHEN = """
    excluded.is_sidechain < messages.is_sidechain
    OR (
        excluded.is_sidechain = messages.is_sidechain
        AND (excluded.input_tokens + excluded.output_tokens
             + excluded.cache_creation + excluded.cache_read)
          > (messages.input_tokens + messages.output_tokens
             + messages.cache_creation + messages.cache_read)
    )
"""


def _write_day_turns(conn: sqlite3.Connection, session_id: str, by_day: dict) -> None:
    """{KST날짜: 턴수}를 session_day_turns에 UPSERT(덮어쓰기). 날짜 미상('') 키는 스킵."""
    for day, turns in by_day.items():
        if not day:
            continue
        conn.execute(
            """INSERT INTO session_day_turns (session_id, day, turns) VALUES (?,?,?)
               ON CONFLICT(session_id, day) DO UPDATE SET turns = excluded.turns""",
            (session_id, day, turns),
        )


def ingest_records(conn: sqlite3.Connection, records: list[UsageRecord], pricing: dict) -> int:
    """records를 적재(dedup). 적재 시도한 레코드 수 반환."""
    for r in records:
        cost = compute_cost(r, pricing)
        conn.execute(
            f"""INSERT INTO messages
               (dedup_key, provider, session_id, project, ts, model,
                input_tokens, output_tokens, cache_creation, cache_creation_1h, cache_read,
                web_search, web_fetch, cost_usd, priced, request_id, is_sidechain,
                attribution_skill, git_branch)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(dedup_key) DO UPDATE SET
                   provider=excluded.provider, session_id=excluded.session_id,
                   project=excluded.project, ts=excluded.ts, model=excluded.model,
                   input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
                   cache_creation=excluded.cache_creation, cache_creation_1h=excluded.cache_creation_1h,
                   cache_read=excluded.cache_read,
                   web_search=excluded.web_search, web_fetch=excluded.web_fetch,
                   cost_usd=excluded.cost_usd, priced=excluded.priced,
                   request_id=excluded.request_id, is_sidechain=excluded.is_sidechain,
                   attribution_skill=excluded.attribution_skill, git_branch=excluded.git_branch
               WHERE {_REPLACE_WHEN}""",
            (
                _dedup_key(r), r.provider, r.session_id, r.cwd, r.ts, r.model,
                r.input_tokens, r.output_tokens, r.cache_creation, r.cache_creation_1h, r.cache_read,
                r.web_search, r.web_fetch, cost.cost_usd, int(cost.priced),
                r.request_id, int(r.is_sidechain),
                r.attribution_skill, r.git_branch,
            ),
        )
        conn.execute(
            """INSERT INTO sessions (session_id, project, provider, first_ts, last_ts, summary, user_turns)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                   last_ts = MAX(sessions.last_ts, excluded.last_ts),
                   first_ts = MIN(sessions.first_ts, excluded.first_ts),
                   project = COALESCE(sessions.project, excluded.project),
                   -- summary: 새 발췌 우선(재인제스트 시 갱신), NULL이면 기존 aiTitle 유지
                   summary = COALESCE(excluded.summary, sessions.summary),
                   -- user_turns: 새 카운트 우선, NULL이면 기존 값 유지(Claude는 별도 경로로 채움)
                   user_turns = COALESCE(excluded.user_turns, sessions.user_turns)""",
            (r.session_id, r.cwd, r.provider, r.ts, r.ts, r.summary, r.user_turns),
        )
        if r.user_turns_by_day:
            _write_day_turns(conn, r.session_id, r.user_turns_by_day)
    conn.commit()
    return len(records)


# 직전 적재 때 사용한 효과 단가의 핑거프린트. 값이 바뀌면 기존 cost_usd가 stale.
PRICING_FINGERPRINT_KEY = "pricing_fingerprint"


def reprice_all(conn: sqlite3.Connection, pricing: dict) -> int:
    """저장된 토큰 × 현행 pricing으로 모든 messages.cost_usd/priced를 재계산한다.

    raw JSONL 재적재 없이 DB 내부 토큰만으로 비용을 다시 매긴다(cache_creation_1h 컬럼
    덕에 5m/1h 분리도 정확). 비용·priced가 실제로 바뀐 행 수를 반환한다.
    """
    rows = conn.execute(
        "SELECT id, provider, model, ts, input_tokens, output_tokens, "
        "cache_creation, cache_creation_1h, cache_read, cost_usd, priced FROM messages"
    ).fetchall()
    changed = 0
    for r in rows:
        rec = UsageRecord(
            # ts는 날짜유효 단가(dated rates)가 행별 유효 구간을 고르는 근거 —
            # 재계산에서도 반드시 실제 ts를 넘긴다(빈 값이면 dated 행이 표준가로 덮인다).
            provider=r["provider"], session_id="", cwd="", ts=r["ts"], model=r["model"],
            input_tokens=r["input_tokens"] or 0, output_tokens=r["output_tokens"] or 0,
            cache_creation=r["cache_creation"] or 0, cache_read=r["cache_read"] or 0,
            cache_creation_1h=r["cache_creation_1h"] or 0,
        )
        res = compute_cost(rec, pricing)
        if abs((r["cost_usd"] or 0.0) - res.cost_usd) > 1e-9 or (r["priced"] or 0) != int(res.priced):
            conn.execute(
                "UPDATE messages SET cost_usd=?, priced=? WHERE id=?",
                (res.cost_usd, int(res.priced), r["id"]),
            )
            changed += 1
    conn.commit()
    return changed


def maybe_reprice(conn: sqlite3.Connection, pricing: dict) -> int:
    """단가 핑거프린트가 직전 적재 때와 다르면(또는 미기록) 전체 재계산.

    cost_usd는 (토큰 × 단가)의 캐시값이라 단가 입력(pricing.json/overrides)이 바뀌면
    기존 행이 stale해진다. 증분 적재는 옛 라인을 다시 안 읽고 dedup 가드도 동일 토큰
    재적재를 막으므로, 여기서 핑거프린트 변화를 감지해 자동 재계산한다(매 ingest 호출).
    구버전 DB(키 미기록)는 prev=None이라 첫 실행에 1회 재계산되어 기존 오단가도 자동 정정.
    반환: 비용이 바뀐 행 수(0이면 변화 없음/스킵).
    """
    fp = pricing_fingerprint(pricing)
    if get_meta(conn, PRICING_FINGERPRINT_KEY) == fp:
        return 0
    changed = reprice_all(conn, pricing)
    set_meta(conn, PRICING_FINGERPRINT_KEY, fp)
    return changed


def get_offset(conn: sqlite3.Connection, path: str) -> int:
    row = conn.execute("SELECT offset FROM scan_offsets WHERE path=?", (path,)).fetchone()
    return row["offset"] if row else 0


def set_offset(conn: sqlite3.Connection, path: str, offset: int) -> None:
    conn.execute(
        """INSERT INTO scan_offsets (path, offset) VALUES (?,?)
           ON CONFLICT(path) DO UPDATE SET offset = excluded.offset""",
        (path, offset),
    )


def ingest_root(conn: sqlite3.Connection, root, pricing: dict, provider: str = "claude") -> int:
    """root 아래 모든 세션 파일을 증분 스캔·적재. 신규 레코드 수 반환."""
    total = 0
    for f in discover_session_files(root):
        p = str(f)
        start = get_offset(conn, p)
        records, end = parse_file(p, start, provider=provider)
        if records:
            ingest_records(conn, records, pricing)
            total += len(records)
        set_offset(conn, p, end)
    conn.commit()
    return total


def ingest_titles(conn: sqlite3.Connection, root) -> int:
    """root 아래 세션 파일의 ai-title을 sessions.summary로 반영. 갱신 시도 수 반환.

    ingest_root로 세션 행이 먼저 생성된 뒤 호출한다(UPDATE 대상이 있어야 반영됨).
    제목은 휘발 전 L1에 캐시되어 raw 정리(30일) 후에도 report에서 보인다.
    """
    n = 0
    for f in discover_session_files(root):
        for sid, title in parse_titles(str(f)).items():
            conn.execute(
                "UPDATE sessions SET summary=? WHERE session_id=?", (title, sid)
            )
            n += 1
    conn.commit()
    return n


def ingest_user_turns(conn: sqlite3.Connection, root) -> int:
    """root 아래 Claude 세션 파일의 사용자 턴 수를 sessions.user_turns + session_day_turns에 반영. 갱신 세션 수 반환.

    ingest_root로 세션 행이 먼저 생성된 뒤 호출한다(UPDATE 대상이 있어야 반영됨).
    parse_titles와 마찬가지로 전체 파일을 풀스캔한다(증분 오프셋 미사용 — 매번 정확한 총량).
    """
    from tokenomy.parser import count_user_turns_by_day

    n = 0
    for f in discover_session_files(root):
        for sid, by_day in count_user_turns_by_day(str(f)).items():
            conn.execute(
                "UPDATE sessions SET user_turns=? WHERE session_id=?",
                (sum(by_day.values()), sid),
            )
            _write_day_turns(conn, sid, by_day)
            n += 1
    conn.commit()
    return n


def set_user(conn, user_id: str, tier: str, provider_choice: str | None = None) -> None:
    conn.execute(
        """INSERT INTO users (user_id, tier, provider_choice) VALUES (?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
               tier = excluded.tier, provider_choice = excluded.provider_choice""",
        (user_id, tier, provider_choice),
    )
    conn.commit()


def set_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


def get_meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


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


def official_buckets_at(conn, provider: str, fetched_at: str) -> list:
    """특정 스냅샷(provider, fetched_at)의 버킷 행 전부 — raw 디버그 페이지의 '파싱 결과'용."""
    return conn.execute(
        "SELECT * FROM official_buckets WHERE provider=? AND fetched_at=? ORDER BY id",
        (provider, fetched_at),
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
    """공식 사용량 취득 상태 조회."""
    return conn.execute(
        "SELECT * FROM official_fetch_state WHERE provider=?", (provider,)
    ).fetchone()


def upsert_fetch_state(conn, provider: str, *, last_attempt_at: str | None,
                       last_success_at: str | None, last_status: str,
                       last_error: str | None) -> None:
    """공식 사용량 취득 상태 갱신. 실패(non-ok) 시 last_success_at을 COALESCE로 보존."""
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


# ---------------------------------------------------------------------------
# 공식 raw 포착(official_raw) — 디버그용 응답 원문 보관(ADR 0014)
# ---------------------------------------------------------------------------

def insert_official_raw(conn, *, provider: str, fetched_at: str, status: str,
                        http_code: int | None, raw_text: str, created_at: str,
                        retain_days: int = 7) -> int:
    """스크럽된 raw 응답 1건을 적재하고 7일 지난 행을 prune. 저장 byte 길이 반환.

    UNIQUE(provider, fetched_at) + INSERT OR REPLACE로 같은 스냅샷 재취득이 멱등.
    prune 기준은 이번 fetched_at − retain_days(모든 KST ISO라 문자열 비교가 유효).
    """
    byte_len = len(raw_text.encode("utf-8")) if raw_text is not None else 0
    with conn:   # 트랜잭션
        conn.execute(
            "INSERT OR REPLACE INTO official_raw "
            "(provider, fetched_at, status, http_code, raw_text, byte_len, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (provider, fetched_at, status, http_code, raw_text, byte_len, created_at),
        )
        try:
            cutoff = (datetime.fromisoformat(fetched_at)
                      - timedelta(days=retain_days)).isoformat()
            conn.execute("DELETE FROM official_raw WHERE fetched_at < ?", (cutoff,))
        except (ValueError, TypeError):
            pass   # fetched_at 파싱 실패 시 prune만 건너뜀(적재는 보존)
    return byte_len


def get_official_raw(conn, provider: str, fetched_at: str):
    """한 스냅샷의 raw 행(없으면 None)."""
    return conn.execute(
        "SELECT * FROM official_raw WHERE provider=? AND fetched_at=?",
        (provider, fetched_at),
    ).fetchone()


def list_official_raw(conn, provider: str) -> list:
    """provider의 보관 중인 raw 스냅샷 행 전부(최신순) — 7일 피커용."""
    return conn.execute(
        "SELECT * FROM official_raw WHERE provider=? ORDER BY fetched_at DESC, id DESC",
        (provider,),
    ).fetchall()


def last_provider_activity_ts(conn: sqlite3.Connection, provider: str) -> str | None:
    """이 기기 로컬 로그 기준 provider의 마지막 메시지 ts(ISO). 활동 없으면 None.

    토큰 자동 갱신 안전망(official_fetch)이 "이 기기에서 최근 CLI를 썼는가" 판정에 쓴다.
    """
    row = conn.execute(
        "SELECT MAX(ts) FROM messages WHERE provider=?", (provider,)
    ).fetchone()
    return row[0] if row and row[0] else None


# ---------------------------------------------------------------------------
# 로컬 롤업 read facade — 세션 메타/최초 등장/일별 턴 조회(aggregate가 위임)
# official facade(latest_official_snapshot 등)와 대칭. 순수 lookup, 도메인 로직 0.
# ---------------------------------------------------------------------------

def session_meta(conn: sqlite3.Connection) -> dict:
    """세션별 메타 행 맵 `{session_id: Row}`. Row는 label/summary/provider/user_turns superset.

    호출부가 필요한 컬럼만 투영한다(맵 컴프리헨션 중복 제거가 목적).
    """
    return {
        r["session_id"]: r
        for r in conn.execute(
            "SELECT session_id, label, summary, provider, user_turns FROM sessions"
        ).fetchall()
    }


def session_first_appearance(conn: sqlite3.Connection) -> dict:
    """세션별 최초 등장 ts `{session_id: MIN(ts) 문자열}`(전체 messages 기준, raw ISO).

    KST 날짜 변환(도메인)은 호출부(aggregate)에 잔류.
    """
    return {
        r["session_id"]: r["m"]
        for r in conn.execute(
            "SELECT session_id, MIN(ts) m FROM messages GROUP BY session_id"
        ).fetchall()
    }


def session_day_turns_map(conn: sqlite3.Connection) -> dict:
    """(세션, KST날짜)별 사용자 턴 수 `{(session_id, day): turns}`."""
    return {
        (r["session_id"], r["day"]): r["turns"]
        for r in conn.execute(
            "SELECT session_id, day, turns FROM session_day_turns"
        ).fetchall()
    }


# ---------------------------------------------------------------------------
# 절약 수단 적용 상태 read/write facade(savers.py가 전이 판정에 사용, ADR 0026)
# 순수 lookup/append, 전이 판정(변화 감지) 도메인 로직은 savers.py에 잔류.
# ---------------------------------------------------------------------------

def latest_saver_states(conn: sqlite3.Connection) -> dict:
    """절약 수단별 최신 적용 상태 `{(saver_id, provider): (state, changed_at)}`.

    같은 (saver_id, provider)의 마지막(=최대 id) 전이 행을 투영한다.
    """
    rows = conn.execute(
        "SELECT saver_id, provider, state, changed_at, id FROM saver_state_transitions"
    ).fetchall()
    latest: dict = {}
    for r in rows:
        key = (r["saver_id"], r["provider"])
        prev = latest.get(key)
        if prev is None or r["id"] > prev[2]:
            latest[key] = (r["state"], r["changed_at"], r["id"])
    return {k: (v[0], v[1]) for k, v in latest.items()}


def record_saver_transition(conn: sqlite3.Connection, saver_id: str, provider: str,
                            state: str, changed_at: str) -> None:
    """적용 상태 전이를 조건부 append — **현재 최신 상태와 다를 때만** 삽입한다.

    변화 판정을 SQL 한 문에 담아, 동시 로드(`/`·`/savers`)가 같은 전이를 각각 읽고
    중복 행을 쓰는 레이스를 막는다(SQLite writer 직렬화 + busy_timeout으로 원자적).
    최신 행이 이미 같은 state면 no-op. 최초 관측(행 없음)은 삽입된다.
    """
    conn.execute(
        """INSERT INTO saver_state_transitions (saver_id, provider, state, changed_at)
           SELECT ?, ?, ?, ?
           WHERE NOT EXISTS (
               SELECT 1 FROM saver_state_transitions t
               WHERE t.saver_id = ? AND t.provider = ?
                 AND t.id = (SELECT MAX(id) FROM saver_state_transitions
                             WHERE saver_id = ? AND provider = ?)
                 AND t.state = ?
           )""",
        (saver_id, provider, state, changed_at,
         saver_id, provider, saver_id, provider, state),
    )

