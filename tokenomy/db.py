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
from tokenomy.pricing import compute_cost

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
    summary TEXT
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


def ingest_records(conn: sqlite3.Connection, records: list[UsageRecord], pricing: dict) -> int:
    """records를 적재(dedup). 적재 시도한 레코드 수 반환."""
    for r in records:
        cost = compute_cost(r, pricing)
        conn.execute(
            f"""INSERT INTO messages
               (dedup_key, provider, session_id, project, ts, model,
                input_tokens, output_tokens, cache_creation, cache_read,
                web_search, web_fetch, cost_usd, priced, request_id, is_sidechain,
                attribution_skill, git_branch)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(dedup_key) DO UPDATE SET
                   provider=excluded.provider, session_id=excluded.session_id,
                   project=excluded.project, ts=excluded.ts, model=excluded.model,
                   input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
                   cache_creation=excluded.cache_creation, cache_read=excluded.cache_read,
                   web_search=excluded.web_search, web_fetch=excluded.web_fetch,
                   cost_usd=excluded.cost_usd, priced=excluded.priced,
                   request_id=excluded.request_id, is_sidechain=excluded.is_sidechain,
                   attribution_skill=excluded.attribution_skill, git_branch=excluded.git_branch
               WHERE {_REPLACE_WHEN}""",
            (
                _dedup_key(r), r.provider, r.session_id, r.cwd, r.ts, r.model,
                r.input_tokens, r.output_tokens, r.cache_creation, r.cache_read,
                r.web_search, r.web_fetch, cost.cost_usd, int(cost.priced),
                r.request_id, int(r.is_sidechain),
                r.attribution_skill, r.git_branch,
            ),
        )
        conn.execute(
            """INSERT INTO sessions (session_id, project, provider, first_ts, last_ts, summary)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                   last_ts = MAX(sessions.last_ts, excluded.last_ts),
                   first_ts = MIN(sessions.first_ts, excluded.first_ts),
                   project = COALESCE(sessions.project, excluded.project),
                   -- summary: 새 발췌 우선(재인제스트 시 갱신), NULL이면 기존 aiTitle 유지
                   summary = COALESCE(excluded.summary, sessions.summary)""",
            (r.session_id, r.cwd, r.provider, r.ts, r.ts, r.summary),
        )
    conn.commit()
    return len(records)


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
