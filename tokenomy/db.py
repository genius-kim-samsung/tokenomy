"""SQLite 적재.

- messages: 메시지별 토큰/비용 (dedup_key UNIQUE → 스트리밍 중복 제거)
- sessions: 세션 메타 + 수동 라벨(업무 귀속)
- users:    현재 미사용 — 향후 멀티유저 확장용 발판 테이블
- scan_offsets: 파일별 마지막 파싱 byte-offset (증분 스캔)

대화 본문 전체는 저장하지 않는다. Codex는 첫 사용자 프롬프트 발췌(≤120자)를 sessions.summary에 저장한다.
"""
from __future__ import annotations

import sqlite3
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

CREATE TABLE IF NOT EXISTS official_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT,
    target_month TEXT,        -- "YYYY-MM" (KST 기준 어느 달의 누적인가) — 월 리셋 매칭
    cumulative_usd REAL,      -- 그 시점 회사 화면의 이번 달 누적 지출($)
    snapshot_ts TEXT,         -- 누적값의 as-of 시점(KST ISO). 수동 입력이면 입력 시각
    created_at TEXT           -- DB 행 생성 시각(KST ISO)
);
CREATE INDEX IF NOT EXISTS idx_official_provider_month
    ON official_usage(provider, target_month);
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
        "SELECT id, provider, model, input_tokens, output_tokens, "
        "cache_creation, cache_creation_1h, cache_read, cost_usd, priced FROM messages"
    ).fetchall()
    changed = 0
    for r in rows:
        rec = UsageRecord(
            provider=r["provider"], session_id="", cwd="", ts="", model=r["model"],
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


def insert_official_snapshot(
    conn,
    *,
    provider: str,
    target_month: str,
    cumulative_usd: float,
    snapshot_ts: str,
    created_at: str,
) -> int:
    """회사 공식 사용량 스냅샷을 append한다(누적값의 시점 기록). 생성된 id 반환.

    같은 달에 여러 번 입력 가능 — 매 입력이 한 행. 최신값(latest_official)이 게이지에
    쓰이고, 전체 시계열(official_series)은 일별 추이의 마커/계단 표시에 쓰인다.
    """
    cur = conn.execute(
        "INSERT INTO official_usage "
        "(provider, target_month, cumulative_usd, snapshot_ts, created_at) "
        "VALUES (?,?,?,?,?)",
        (provider, target_month, cumulative_usd, snapshot_ts, created_at),
    )
    conn.commit()
    return cur.lastrowid


def latest_official(conn, provider: str, target_month: str):
    """provider·target_month의 가장 최근 스냅샷 행(snapshot_ts 최신). 없으면 None.

    max 병합(spent = max(공식누적_최신, CLI_월누적))의 '공식누적_최신' 항.
    """
    return conn.execute(
        "SELECT * FROM official_usage WHERE provider=? AND target_month=? "
        "ORDER BY snapshot_ts DESC, id DESC LIMIT 1",
        (provider, target_month),
    ).fetchone()


def official_series(conn, provider: str, target_month: str) -> list:
    """provider·target_month의 전체 스냅샷을 snapshot_ts 오름차순으로 반환.

    일별 추이 차트의 공식 마커/계단 + 구간 사용량(인접 스냅샷 차분) 표시용.
    """
    return conn.execute(
        "SELECT * FROM official_usage WHERE provider=? AND target_month=? "
        "ORDER BY snapshot_ts ASC, id ASC",
        (provider, target_month),
    ).fetchall()
