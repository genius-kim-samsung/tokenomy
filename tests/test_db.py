import json

from tokenomy.db import connect, ingest_records, ingest_root, ingest_titles, set_user
from tokenomy.parser import UsageRecord

PRICING = {
    "match": [
        {"contains": "opus", "provider": "claude", "input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    ]
}


def _rec(msg_id, **kw):
    return UsageRecord(
        provider=kw.get("provider", "claude"), session_id=kw.get("session_id", "s1"), cwd="/p",
        ts=kw.get("ts", "2026-06-11T10:00:00Z"), model="claude-opus-4-8",
        input_tokens=kw.get("input_tokens", 1_000_000), output_tokens=0,
        cache_creation=0, cache_read=0, message_id=msg_id,
        request_id=kw.get("request_id"), is_sidechain=kw.get("is_sidechain", False),
        summary=kw.get("summary"),
    )


def test_roundtrip_and_cost():
    conn = connect(":memory:")
    ingest_records(conn, [_rec("m1")], PRICING)
    row = conn.execute("SELECT input_tokens, cost_usd, priced FROM messages").fetchone()
    assert row["input_tokens"] == 1_000_000
    assert row["cost_usd"] == 15.0
    assert row["priced"] == 1


def test_dedup_by_message_id():
    conn = connect(":memory:")
    # same message_id three times (streaming duplicate) → 1 row, not 3
    ingest_records(conn, [_rec("dup"), _rec("dup"), _rec("dup")], PRICING)
    count = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
    total = conn.execute("SELECT SUM(cost_usd) s FROM messages").fetchone()["s"]
    assert count == 1
    assert total == 15.0


def test_dedup_distinguishes_request_id():
    conn = connect(":memory:")
    # 같은 message_id, 다른 request_id = 리트라이/별개 과금 → 2행 유지
    ingest_records(conn, [
        _rec("m", request_id="r1"),
        _rec("m", request_id="r2"),
    ], PRICING)
    count = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
    assert count == 2


def test_dedup_keeps_larger_token_entry():
    conn = connect(":memory:")
    # 같은 (msg, req)가 부분기록(작게) 먼저, 완전기록(크게) 나중 → 완전기록 유지
    ingest_records(conn, [_rec("m", request_id="r", input_tokens=10)], PRICING)
    ingest_records(conn, [_rec("m", request_id="r", input_tokens=1_000_000)], PRICING)
    row = conn.execute("SELECT COUNT(*) c, MAX(input_tokens) t FROM messages").fetchone()
    assert row["c"] == 1
    assert row["t"] == 1_000_000


def test_dedup_smaller_does_not_replace_larger():
    conn = connect(":memory:")
    # 완전기록(크게) 먼저, 부분기록(작게) 나중 → 완전기록 유지(작은 게 덮지 못함)
    ingest_records(conn, [_rec("m", request_id="r", input_tokens=1_000_000)], PRICING)
    ingest_records(conn, [_rec("m", request_id="r", input_tokens=10)], PRICING)
    row = conn.execute("SELECT input_tokens FROM messages").fetchone()
    assert row["input_tokens"] == 1_000_000


def test_dedup_prefers_non_sidechain_parent():
    conn = connect(":memory:")
    # 같은 키: 비sidechain(부모, 작은 토큰) 먼저, sidechain replay(큰 토큰) 나중
    # → 토큰이 더 커도 비sidechain(부모)을 유지해야 한다
    ingest_records(conn, [_rec("m", request_id="r", input_tokens=10, is_sidechain=False)], PRICING)
    ingest_records(conn, [_rec("m", request_id="r", input_tokens=1_000_000, is_sidechain=True)], PRICING)
    row = conn.execute("SELECT input_tokens, is_sidechain FROM messages").fetchone()
    assert row["input_tokens"] == 10
    assert row["is_sidechain"] == 0


def test_session_first_last_ts():
    conn = connect(":memory:")
    ingest_records(conn, [
        _rec("a", ts="2026-06-11T10:00:00Z"),
        _rec("b", ts="2026-06-11T12:00:00Z"),
        _rec("c", ts="2026-06-11T08:00:00Z"),
    ], PRICING)
    s = conn.execute("SELECT first_ts, last_ts FROM sessions WHERE session_id='s1'").fetchone()
    assert s["first_ts"] == "2026-06-11T08:00:00Z"
    assert s["last_ts"] == "2026-06-11T12:00:00Z"


def test_ingest_root_incremental(tmp_path):
    conn = connect(":memory:")
    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    f = root / "sess.jsonl"
    line = json.dumps({
        "message": {"id": "x1", "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 1_000_000, "output_tokens": 0,
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
        "sessionId": "sess", "ts": "2026-06-11T10:00:00Z", "cwd": "/p",
    })
    f.write_text(line + "\n", encoding="utf-8")

    n1 = ingest_root(conn, tmp_path / "projects", PRICING)
    assert n1 == 1
    # second run with no new lines → 0 new (offset remembered)
    n2 = ingest_root(conn, tmp_path / "projects", PRICING)
    assert n2 == 0


def test_set_user():
    conn = connect(":memory:")
    set_user(conn, "test-user", "pro", None)
    row = conn.execute("SELECT tier FROM users WHERE user_id='test-user'").fetchone()
    assert row["tier"] == "pro"


def test_stores_attribution_skill_and_branch():
    conn = connect(":memory:")
    rec = _rec("m1")
    rec.attribution_skill = "tdd"
    rec.git_branch = "main"
    ingest_records(conn, [rec], PRICING)
    row = conn.execute(
        "SELECT attribution_skill, git_branch FROM messages"
    ).fetchone()
    assert row["attribution_skill"] == "tdd"
    assert row["git_branch"] == "main"


def test_meta_set_get():
    from tokenomy.db import set_meta, get_meta
    conn = connect(":memory:")
    assert get_meta(conn, "k") is None
    set_meta(conn, "k", "v")
    assert get_meta(conn, "k") == "v"
    set_meta(conn, "k", "v2")  # upsert
    assert get_meta(conn, "k") == "v2"


def test_sessions_has_summary_column():
    conn = connect(":memory:")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "summary" in cols


def _title_file(tmp_path, session_id, title):
    root = tmp_path / "projects" / "p"
    root.mkdir(parents=True)
    f = root / "sess.jsonl"
    usage = json.dumps({
        "message": {"id": "m1", "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 1, "output_tokens": 0,
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
        "sessionId": session_id, "ts": "2026-06-11T10:00:00Z", "cwd": "/p",
    })
    ai = json.dumps({"type": "ai-title", "aiTitle": title, "sessionId": session_id})
    f.write_text(usage + "\n" + ai + "\n", encoding="utf-8")
    return tmp_path / "projects"


def test_ingest_titles_sets_session_summary(tmp_path):
    conn = connect(":memory:")
    root = _title_file(tmp_path, "sess", "세션 요약 제목")
    ingest_root(conn, root, PRICING)          # 세션 행 먼저 생성
    n = ingest_titles(conn, root)
    assert n == 1
    row = conn.execute("SELECT summary FROM sessions WHERE session_id='sess'").fetchone()
    assert row["summary"] == "세션 요약 제목"


def test_ingest_titles_noop_when_session_absent(tmp_path):
    # usage가 없어 세션 행이 없으면 UPDATE는 0행 반영 (에러 없이 통과)
    conn = connect(":memory:")
    root = tmp_path / "projects" / "p"
    root.mkdir(parents=True)
    (root / "s.jsonl").write_text(
        json.dumps({"type": "ai-title", "aiTitle": "고아 제목", "sessionId": "ghost"}) + "\n",
        encoding="utf-8",
    )
    ingest_titles(conn, tmp_path / "projects")  # 예외 없이 동작
    row = conn.execute("SELECT * FROM sessions WHERE session_id='ghost'").fetchone()
    assert row is None


def test_migration_adds_summary_to_legacy_sessions(tmp_path):
    import sqlite3
    db = tmp_path / "legacy.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, project TEXT)")
    c.execute("INSERT INTO sessions (session_id, project) VALUES ('s','/p')")
    c.commit()
    c.close()
    conn = connect(str(db))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "summary" in cols
    row = conn.execute("SELECT project FROM sessions WHERE session_id='s'").fetchone()
    assert row["project"] == "/p"


def test_migration_adds_columns_to_legacy_db(tmp_path):
    import sqlite3
    db = tmp_path / "legacy.db"
    c = sqlite3.connect(str(db))
    # 구 스키마: 신규 컬럼이 하나도 없는 messages
    c.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "dedup_key TEXT UNIQUE, provider TEXT)"
    )
    c.execute("INSERT INTO messages (dedup_key, provider) VALUES ('k','claude')")
    c.commit()
    c.close()

    conn = connect(str(db))  # connect가 _migrate를 돌려야 한다
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    for col in ("request_id", "is_sidechain", "attribution_skill", "git_branch"):
        assert col in cols
    # 기존 행은 보존
    row = conn.execute("SELECT provider FROM messages WHERE dedup_key='k'").fetchone()
    assert row["provider"] == "claude"


def test_connect_default_uses_paths_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENOMY_DATA", str(tmp_path))
    conn = connect()  # 인자 없음 → paths.db_path()
    conn.execute("INSERT INTO meta (key, value) VALUES ('x', '1')")
    conn.commit()
    assert (tmp_path / "data" / "tokenomy.db").exists()


def test_codex_summary_persisted_to_sessions():
    conn = connect(":memory:")
    ingest_records(conn, [_rec("c1", session_id="s1", provider="codex",
                               summary="codex 첫 프롬프트")], PRICING)
    row = conn.execute("SELECT summary FROM sessions WHERE session_id='s1'").fetchone()
    assert row["summary"] == "codex 첫 프롬프트"


def test_summary_none_does_not_overwrite_existing():
    # Claude 경로 재현: ingest_titles의 UPDATE로 채운 aiTitle을 이후 None 적재가 덮지 않음
    conn = connect(":memory:")
    ingest_records(conn, [_rec("m1", session_id="s2", summary=None)], PRICING)
    conn.execute("UPDATE sessions SET summary='aiTitle 요약' WHERE session_id='s2'")
    ingest_records(conn, [_rec("m2", session_id="s2", summary=None,
                               ts="2026-06-12T00:00:00Z")], PRICING)
    row = conn.execute("SELECT summary FROM sessions WHERE session_id='s2'").fetchone()
    assert row["summary"] == "aiTitle 요약"


def test_codex_summary_updates_on_reingest():
    # 재인제스트로 발췌가 바뀌면 새 값으로 갱신(excluded 우선)
    conn = connect(":memory:")
    ingest_records(conn, [_rec("c1", session_id="s1", provider="codex", summary="A")], PRICING)
    ingest_records(conn, [_rec("c1", session_id="s1", provider="codex", summary="B")], PRICING)
    row = conn.execute("SELECT summary FROM sessions WHERE session_id='s1'").fetchone()
    assert row["summary"] == "B"


import sqlite3
from tokenomy.db import ingest_user_turns


def test_migrate_adds_user_turns_to_old_db(tmp_path):
    # user_turns 컬럼이 없던 구버전 DB
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, project TEXT, "
                "provider TEXT, first_ts TEXT, last_ts TEXT, label TEXT, summary TEXT)")
    raw.commit(); raw.close()
    conn = connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "user_turns" in cols


def test_codex_record_persists_user_turns():
    conn = connect(":memory:")
    rec = _rec("cx", provider="codex", session_id="cx-1")
    rec.user_turns = 4
    ingest_records(conn, [rec], PRICING)
    row = conn.execute("SELECT user_turns FROM sessions WHERE session_id='cx-1'").fetchone()
    assert row["user_turns"] == 4


def test_claude_none_preserves_existing_user_turns():
    conn = connect(":memory:")
    # 먼저 user_turns=2로 적재
    r1 = _rec("m1", session_id="s9"); r1.user_turns = 2
    ingest_records(conn, [r1], PRICING)
    # user_turns=None인 후속 레코드는 기존 값을 덮지 않는다(COALESCE)
    r2 = _rec("m2", session_id="s9")  # user_turns 기본 None
    ingest_records(conn, [r2], PRICING)
    row = conn.execute("SELECT user_turns FROM sessions WHERE session_id='s9'").fetchone()
    assert row["user_turns"] == 2


def test_ingest_user_turns_updates_sessions(tmp_path):
    conn = connect(":memory:")
    # 세션 행 선생성(ingest_root가 하던 역할 대체)
    ingest_records(conn, [_rec("m1", session_id="sess-1")], PRICING)
    # 사용자 턴 2개짜리 파일
    f = tmp_path / "sess.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "a"}, "sessionId": "sess-1"},
        {"type": "user", "message": {"role": "user", "content": "b"}, "sessionId": "sess-1"},
    ]
    f.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    ingest_user_turns(conn, tmp_path)
    row = conn.execute("SELECT user_turns FROM sessions WHERE session_id='sess-1'").fetchone()
    assert row["user_turns"] == 2
