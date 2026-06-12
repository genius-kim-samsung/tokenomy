"""Raw Archive — raw JSONL 원문을 (로그 휘발 전, 기본 30일) 로컬에 보존.

ingest 파싱과 별개로, raw 라인을 data/archive/<provider>/<상대경로>로
증분 바이트 복사한다. parser를 거치지 않으므로 원문이 손실 없이 남는다.
로컬 전용 — 네트워크로 내보내지 않는다.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from tokenomy.parser import discover_session_files


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS archive_offsets "
        "(path TEXT PRIMARY KEY, offset INTEGER DEFAULT 0)"
    )


def get_archive_offset(conn: sqlite3.Connection, path: str) -> int:
    _ensure_table(conn)
    row = conn.execute(
        "SELECT offset FROM archive_offsets WHERE path=?", (path,)
    ).fetchone()
    return row["offset"] if row else 0


def set_archive_offset(conn: sqlite3.Connection, path: str, offset: int) -> None:
    _ensure_table(conn)
    conn.execute(
        "INSERT INTO archive_offsets (path, offset) VALUES (?,?) "
        "ON CONFLICT(path) DO UPDATE SET offset = excluded.offset",
        (path, offset),
    )


def archive_tree(
    root, conn: sqlite3.Connection, provider: str = "claude",
    archive_root=None,
) -> int:
    """root 아래 모든 *.jsonl을 증분 아카이브. 새 바이트가 복사된 파일 수 반환."""
    if archive_root is None:
        from tokenomy.paths import archive_root as _default_archive_root
        archive_root = _default_archive_root()
    root = Path(root).expanduser()
    archive_root = Path(archive_root)
    copied = 0
    for src in discover_session_files(root):
        rel = src.relative_to(root)
        start = get_archive_offset(conn, str(src))
        with open(src, "rb") as fin:
            fin.seek(start)
            chunk = fin.read()
            end = fin.tell()
        if chunk:
            dest = archive_root / provider / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "ab") as fout:
                fout.write(chunk)
            copied += 1
        set_archive_offset(conn, str(src), end)
    conn.commit()
    return copied
