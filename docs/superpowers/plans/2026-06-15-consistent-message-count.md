# 일관된 메시지(사용자 턴) 수 — 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 내역의 "메시지" 수를 모든 provider가 동일하게 "사람이 입력한 프롬프트(사용자 턴)" 수로 표시한다(현재 Codex는 항상 1).

**Architecture:** `sessions.user_turns` 컬럼을 추가하고, Claude는 기존 풀스캔 경로에서, Codex는 `parse_rollout`에서 사용자 턴을 세어 적재한다. 집계는 `COUNT(*)` 대신 `user_turns`를 읽는다. 토큰/비용 집계 경로는 건드리지 않는다.

**Tech Stack:** Python(stdlib: json/sqlite3/pathlib), pytest. 설계 문서: `docs/superpowers/specs/2026-06-15-consistent-message-count-design.md`.

**파일 구조(변경 단위):**
- `tokenomy/parser.py` — Claude 사용자-턴 판별 + 파일 카운트 함수, `UsageRecord.user_turns` 필드.
- `tokenomy/codex_parser.py` — Codex 사용자-턴 카운트(`parse_rollout`).
- `tokenomy/db.py` — 스키마/마이그레이션, sessions upsert, `ingest_user_turns`.
- `tokenomy/cli.py` — 인제스트 파이프라인 배선(`cmd_ingest`).
- `tokenomy/aggregate.py` — `by_session`·`session_detail`가 `user_turns` 사용.
- `tests/` — 각 단위 테스트.

---

## Task 1: Claude 사용자-턴 판별 + 카운트 (`parser.py`)

**Files:**
- Modify: `tokenomy/parser.py` (`UsageRecord` dataclass; 신규 `_is_user_turn`, `count_user_turns`)
- Test: `tests/test_parser.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_parser.py` 끝에 추가:

```python
from tokenomy.parser import count_user_turns, _is_user_turn


def _user_line(content, **over):
    obj = {
        "type": over.get("type", "user"),
        "message": {"role": "user", "content": content},
        "sessionId": over.get("session_id", "sess-1"),
        "timestamp": "2026-06-11T10:00:00Z",
    }
    if "is_sidechain" in over:
        obj["isSidechain"] = over["is_sidechain"]
    if "is_meta" in over:
        obj["isMeta"] = over["is_meta"]
    return obj


def test_is_user_turn_plain_string():
    assert _is_user_turn(_user_line("이거 고쳐줘")) is True


def test_is_user_turn_excludes_tool_result():
    assert _is_user_turn(_user_line([{"type": "tool_result", "content": "x"}])) is False


def test_is_user_turn_counts_text_or_image_blocks():
    assert _is_user_turn(_user_line([{"type": "text", "text": "hi"}])) is True
    assert _is_user_turn(_user_line([{"type": "image"}, {"type": "text", "text": "hi"}])) is True


def test_is_user_turn_excludes_meta_sidechain_commands():
    assert _is_user_turn(_user_line("hi", is_meta=True)) is False
    assert _is_user_turn(_user_line("hi", is_sidechain=True)) is False
    assert _is_user_turn(_user_line("<command-name>/exit</command-name>")) is False
    assert _is_user_turn(_user_line("<local-command-stdout>Bye!</local-command-stdout>")) is False


def test_count_user_turns_groups_by_session(tmp_path):
    f = tmp_path / "sess.jsonl"
    lines = [
        _user_line("첫 프롬프트"),
        _user_line([{"type": "tool_result", "content": "r"}]),   # 제외
        _user_line("둘째 프롬프트"),
        _user_line("<command-name>/clear</command-name>"),        # 제외
        {"type": "assistant", "message": {"role": "assistant", "content": "ok"}},  # 제외
    ]
    f.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    assert count_user_turns(str(f)) == {"sess-1": 2}
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_parser.py -k "user_turn" -v`
Expected: FAIL — `ImportError: cannot import name 'count_user_turns'`

- [ ] **Step 3: 최소 구현**

`tokenomy/parser.py` `UsageRecord` dataclass의 `summary` 필드 바로 아래에 추가:

```python
    user_turns: int | None = None  # 세션 내 사용자 턴 수(Codex는 parse_rollout이 채움; Claude는 count_user_turns).
```

같은 파일에서 `parse_titles` 정의 앞에 함수 두 개 추가:

```python
# 사용자 턴 판별에서 제외할 문자열 content 래퍼(슬래시 명령/로컬 명령 출력 등).
_NON_TURN_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-caveat>",
)


def _is_user_turn(obj: dict) -> bool:
    """raw JSONL 객체가 '사람이 입력한 프롬프트'면 True.

    툴 결과·메타·서브에이전트(sidechain)·슬래시 명령/명령 출력은 제외한다.
    """
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return False
    if obj.get("type") != "user" and msg.get("role") != "user":
        return False
    if obj.get("isSidechain") or obj.get("isMeta"):
        return False
    content = msg.get("content")
    if isinstance(content, str):
        s = content.lstrip()
        return bool(s) and not s.startswith(_NON_TURN_PREFIXES)
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") in ("text", "image") for b in content
        )
    return False


def count_user_turns(path: str) -> dict[str, int]:
    """세션 파일 전체를 풀스캔해 {session_id: 사용자 턴 수}를 반환한다.

    byte-offset 증분이 아닌 전체 스캔(parse_titles와 동일 이유 — 매번 정확한 총량).
    'user' 바이트가 없는 라인은 사용자 턴일 수 없어 건너뛴다(json.loads 회피).
    """
    counts: dict[str, int] = {}
    with open(path, "rb") as f:
        for raw in f:
            if b'"user"' not in raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                continue
            if not isinstance(obj, dict) or not _is_user_turn(obj):
                continue
            sid = obj.get("sessionId") or Path(path).stem
            counts[sid] = counts.get(sid, 0) + 1
    return counts
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_parser.py -k "user_turn" -v`
Expected: PASS (5 passed)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/parser.py tests/test_parser.py
git commit -m "feat(parser): Claude 사용자 턴 판별·카운트 추가"
```

---

## Task 2: Codex 사용자-턴 카운트 (`codex_parser.py`)

**Files:**
- Modify: `tokenomy/codex_parser.py` (`parse_rollout`; 신규 `_is_codex_user_msg`)
- Test: `tests/test_codex_parser.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_codex_parser.py` 끝에 추가:

```python
from tokenomy.codex_parser import _is_codex_user_msg


def test_is_codex_user_msg_filters_environment():
    assert _is_codex_user_msg(
        {"type": "event_msg", "payload": {"type": "user_message", "message": "안녕"}}
    ) is True
    assert _is_codex_user_msg(
        {"type": "event_msg", "payload": {"type": "user_message",
         "message": "<environment_context> ... </environment_context>"}}
    ) is False
    assert _is_codex_user_msg(
        {"type": "event_msg", "payload": {"type": "token_count"}}
    ) is False


def test_parse_rollout_counts_user_turns(tmp_path):
    f = tmp_path / "rollout-turns.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"id": "s-turns", "cwd": "/p", "timestamp": "2026-06-11T12:50:14Z"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "<environment_context>x</environment_context>"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "첫 질문"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "둘째 질문"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 10}}}},
    ]
    f.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    rec = parse_rollout(str(f))
    assert rec is not None
    assert rec.user_turns == 2
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_codex_parser.py -k "user" -v`
Expected: FAIL — `ImportError: cannot import name '_is_codex_user_msg'`

- [ ] **Step 3: 최소 구현**

`tokenomy/codex_parser.py`에서 `parse_rollout` 정의 앞에 헬퍼 추가:

```python
def _is_codex_user_msg(o: dict) -> bool:
    """rollout 이벤트가 사람이 입력한 user_message면 True. 환경 컨텍스트는 제외."""
    if o.get("type") != "event_msg":
        return False
    p = o.get("payload")
    if not isinstance(p, dict) or p.get("type") != "user_message":
        return False
    m = p.get("message")
    if isinstance(m, str) and m.lstrip().startswith("<environment_context"):
        return False
    return True
```

`parse_rollout` 안에서 루프 시작 전에 카운터를 두고, 루프 본문에서 증가시킨 뒤 반환 레코드에 싣는다.

`last_total: dict | None = None` 아래 줄에 추가:

```python
    turns = 0
```

루프 안 `if not isinstance(o, dict): continue` 바로 다음 줄에 추가:

```python
            if _is_codex_user_msg(o):
                turns += 1
```

반환하는 `UsageRecord(...)`에서 `summary=_extract_first_prompt(path),` 아래에 추가:

```python
        user_turns=turns,
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_codex_parser.py -v`
Expected: PASS (기존 테스트 포함 전부 통과)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/codex_parser.py tests/test_codex_parser.py
git commit -m "feat(codex): rollout에서 사용자 턴 수 카운트"
```

---

## Task 3: 스키마·적재 (`db.py`)

**Files:**
- Modify: `tokenomy/db.py` (SCHEMA의 `sessions`, `_MIGRATE_COLS`, `ingest_records` upsert; 신규 `ingest_user_turns`)
- Test: `tests/test_db.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_db.py` 끝에 추가:

```python
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_db.py -k "user_turns" -v`
Expected: FAIL — `ImportError: cannot import name 'ingest_user_turns'`

- [ ] **Step 3: 최소 구현**

(a) SCHEMA의 `CREATE TABLE IF NOT EXISTS sessions`에서 `summary TEXT` 다음 줄에 컬럼 추가:

```python
    summary TEXT,
    user_turns INTEGER
```

(b) `_MIGRATE_COLS["sessions"]`에 항목 추가:

```python
    "sessions": {
        "summary": "TEXT",  # 세션 작업 요약(Claude Code aiTitle 캐시). raw 30일 휘발 대비 영구 보존.
        "user_turns": "INTEGER",  # 세션 내 사용자 턴 수(메시지 수 표시용).
    },
```

(c) `ingest_records`의 sessions upsert를 user_turns 포함으로 교체:

```python
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
```

(d) `ingest_titles` 함수 정의 바로 아래에 추가:

```python
def ingest_user_turns(conn: sqlite3.Connection, root) -> int:
    """root 아래 Claude 세션 파일의 사용자 턴 수를 sessions.user_turns로 반영. 갱신 수 반환.

    ingest_root로 세션 행이 먼저 생성된 뒤 호출한다(UPDATE 대상이 있어야 반영됨).
    parse_titles와 마찬가지로 전체 파일을 풀스캔한다(증분 오프셋 미사용 — 매번 정확한 총량).
    """
    from tokenomy.parser import count_user_turns

    n = 0
    for f in discover_session_files(root):
        for sid, turns in count_user_turns(str(f)).items():
            conn.execute(
                "UPDATE sessions SET user_turns=? WHERE session_id=?", (turns, sid)
            )
            n += 1
    conn.commit()
    return n
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_db.py -v`
Expected: PASS (기존 테스트 포함 전부 통과)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/db.py tests/test_db.py
git commit -m "feat(db): sessions.user_turns 스키마·적재·ingest_user_turns"
```

---

## Task 4: 인제스트 파이프라인 배선 (`cli.py`)

**Files:**
- Modify: `tokenomy/cli.py` (`cmd_ingest`)
- Test: 통합 검증은 Task 5에서. 여기서는 수동 스모크.

- [ ] **Step 1: import 수정**

`from tokenomy.db import connect, ingest_root, ingest_titles` 를 다음으로 교체:

```python
from tokenomy.db import connect, ingest_root, ingest_titles, ingest_user_turns
```

- [ ] **Step 2: `cmd_ingest`에 호출 추가**

`n_titles = ingest_titles(conn, CLAUDE_ROOT)` 다음 줄에 추가:

```python
    n_turns = ingest_user_turns(conn, CLAUDE_ROOT)
```

그리고 `print(...)`의 f-string 마지막 줄을 다음으로 교체(가시성):

```python
        f"archived_files={n_arch}  titles={n_titles}  turns={n_turns}  new records"
```

- [ ] **Step 3: 스모크 — 전체 테스트 + 실제 인제스트**

Run: `.venv\Scripts\python -m pytest -q`
Expected: 전부 통과

Run: `.venv\Scripts\python -m tokenomy.cli ingest`
Expected: `[ingest] ... turns=<N>` 출력(에러 없음). (로컬 로그가 있으면 N>0)

- [ ] **Step 4: 커밋**

```bash
git add tokenomy/cli.py
git commit -m "feat(cli): ingest 파이프라인에 사용자 턴 카운트 배선"
```

---

## Task 5: 집계가 user_turns 사용 (`aggregate.py`)

**Files:**
- Modify: `tokenomy/aggregate.py` (`by_session` 빌더, `session_detail`)
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_aggregate.py` 끝에 추가(파일 상단 import에 맞춰 `connect`, `ingest_records`, `_rec`/`UsageRecord`가 필요하면 가져온다):

```python
from datetime import datetime
from tokenomy.aggregate import KST, by_session, session_detail
from tokenomy.db import connect, ingest_records
from tokenomy.parser import UsageRecord


def _claude_rec(msg_id, session_id="s1", **kw):
    return UsageRecord(
        provider="claude", session_id=session_id, cwd="/p",
        ts="2026-06-11T10:00:00Z", model="claude-opus-4-8",
        input_tokens=1000, output_tokens=0, cache_creation=0, cache_read=0,
        message_id=msg_id,
    )


_PRICING = {"match": [{"contains": "opus", "provider": "claude",
                       "input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50}]}


def test_by_session_msgs_uses_user_turns():
    conn = connect(":memory:")
    # 한 세션에 메시지 행 3개(어시스턴트 응답), 사용자 턴은 2
    ingest_records(conn, [_claude_rec("a"), _claude_rec("b"), _claude_rec("c")], _PRICING)
    conn.execute("UPDATE sessions SET user_turns=2 WHERE session_id='s1'")
    rows = by_session(conn, None, datetime(2026, 6, 11, tzinfo=KST))
    assert len(rows) == 1
    assert rows[0].msgs == 2   # 행 수(3)가 아니라 사용자 턴(2)


def test_session_detail_msgs_uses_user_turns():
    conn = connect(":memory:")
    ingest_records(conn, [_claude_rec("a"), _claude_rec("b")], _PRICING)
    conn.execute("UPDATE sessions SET user_turns=1 WHERE session_id='s1'")
    d = session_detail(conn, "s1")
    assert d is not None
    assert d.msgs == 1


def test_session_detail_null_user_turns_falls_back_to_zero():
    conn = connect(":memory:")
    ingest_records(conn, [_claude_rec("a")], _PRICING)  # user_turns 미설정(NULL)
    d = session_detail(conn, "s1")
    assert d is not None
    assert d.msgs == 0   # 세션은 노출되되 카운트는 0
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -k "user_turns" -v`
Expected: FAIL — `by_session`은 행 수(3)를, `session_detail`은 `COUNT(*)`(2/1)를 반환

- [ ] **Step 3: `by_session` 빌더 수정**

`meta` 딕트 생성을 user_turns 포함으로 교체:

```python
    meta = {
        r["session_id"]: (r["label"], r["summary"], r["provider"], r["user_turns"])
        for r in conn.execute(
            "SELECT session_id, label, summary, provider, user_turns FROM sessions"
        ).fetchall()
    }
```

`agg.setdefault(...)`의 초기 딕트에서 `"msgs": 0,` 제거:

```python
        a = agg.setdefault(
            sid,
            {"project": r["project"], "cost": 0.0,
             "first": r["ts"], "last": r["ts"], "cr": 0, "den": 0},
        )
```

루프 본문의 `a["msgs"] += 1` 줄 삭제.

`SessionRow(...)` 생성부에서 `meta.get(sid, (None, None, None))`를 4-튜플 기본값으로 바꾸고 `msgs`를 user_turns에서 읽도록 교체:

```python
    out = [
        SessionRow(
            session_id=sid, project=a["project"],
            provider=meta.get(sid, (None, None, None, None))[2],
            label=meta.get(sid, (None, None, None, None))[0],
            summary=meta.get(sid, (None, None, None, None))[1],
            cost=round(a["cost"], 4), first_ts=a["first"], last_ts=a["last"],
            msgs=(meta.get(sid, (None, None, None, None))[3] or 0),
            cache_ratio=round(a["cr"] / a["den"], 4) if a["den"] else 0.0,
        )
        for sid, a in agg.items()
    ]
```

- [ ] **Step 4: `session_detail` 수정**

`totals` 쿼리의 `COUNT(*) msgs`를 `COUNT(*) rows`로 바꾸고 가드도 교체:

```python
    totals = conn.execute(
        "SELECT COUNT(*) rows, SUM(cost_usd) cost, SUM(web_search) ws, "
        "SUM(web_fetch) wf, MIN(ts) first_ts, MAX(ts) last_ts, MAX(provider) provider "
        "FROM messages WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if not totals or not totals["rows"]:
        return None
```

`meta` 쿼리에 user_turns 추가:

```python
    meta = conn.execute(
        "SELECT project, provider, label, user_turns FROM sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
```

`SessionDetail(...)` 생성의 `msgs=totals["msgs"],`를 다음으로 교체:

```python
        cost=round(totals["cost"] or 0, 4),
        msgs=(meta["user_turns"] if meta and meta["user_turns"] is not None else 0),
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_aggregate.py -v`
Expected: PASS (기존 테스트 포함 전부 통과)

- [ ] **Step 6: 전체 회귀 + 커밋**

Run: `.venv\Scripts\python -m pytest -q`
Expected: 전부 통과

```bash
git add tokenomy/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): 메시지 수를 user_turns 기준으로 집계"
```

---

## 마무리 검증

- [ ] **실제 데이터로 확인**

```bash
.venv\Scripts\python -m tokenomy.cli ingest
.venv\Scripts\python -m tokenomy.cli report
```

내역/세션에서 Codex 세션의 메시지 수가 1이 아닌 실제 사용자 턴 수로 표시되는지 확인.
(웹: `.venv\Scripts\python -m uvicorn tokenomy.web.app:app --port 8765` 후 내역 탭)

## 참고: 이 plan이 다루지 않는 것

- `views.py:177/190`의 폴더/일자 롤업과 템플릿(`overview.html`, `session.html`, `_history_rows.html`)은 `SessionRow.msgs`를 합산·표시만 하므로 코드 변경 불필요(자동 반영).
- 날짜 범위에 일부만 걸친 세션은 전체 턴수로 표시(설계서에 명시된 허용 트레이드오프).
