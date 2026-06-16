# 데이터 수집·보존 신뢰성 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** raw JSONL이 30일 후 삭제되기 전에 수집을 보증하고(트리거 다중화·freshness 경고), 원문을 로컬에 아카이브하며(L0), 효율 복기용 메타를 넉넉히 적재한다.

**Architecture:** 기존 idempotent `ingest`(offset 증분 + dedup) 위에 ① raw 원문 바이트 복사 아카이브(`archive.py`), ② 신선도 계산/경고(`freshness.py`), ③ 메타 컬럼 2종 + 누락된 마이그레이션을 더한다. 2계층(L0 raw archive = 진실의 원천 / L1 SQLite = 파생 집계).

**Tech Stack:** Python 3.14 stdlib (sqlite3, pathlib, json, datetime, dataclasses) + pytest. 신규 의존성 없음.

**설계 출처:** `docs/superpowers/specs/2026-06-11-data-ingestion-reliability-design.md` (결정 D1~D4)

**Worktree:** `.claude/worktrees/data-ingestion`, 브랜치 `worktree-data-ingestion`, base `86bdc63`. 모든 경로는 repo 루트 상대.

---

## 파일 구조

| 파일 | 책임 | 작업 |
|---|---|---|
| `tokenomy/parser.py` | raw 라인 → UsageRecord. 메타 2종 추출 추가 | Modify |
| `tokenomy/db.py` | 스키마·적재. 메타 컬럼 + 마이그레이션 + meta 테이블 | Modify |
| `tokenomy/archive.py` | L0 raw 원문 증분 바이트 복사 (신규) | Create |
| `tokenomy/freshness.py` | 수집 신선도 계산 + 마지막 ingest 기록 (신규) | Create |
| `tokenomy/cli.py` | ingest에 아카이브·기록 통합, report에 경고 표시 | Modify |
| `tests/test_parser.py` | 메타 추출 테스트 | Modify |
| `tests/test_db.py` | 메타 저장 + 마이그레이션 테스트 | Modify |
| `tests/test_archive.py` | 아카이브 동작 (신규) | Create |
| `tests/test_freshness.py` | 신선도 계산 (신규) | Create |
| `~/.claude/settings.json` | SessionEnd hook (D1 트리거) | 설정 |

**구현 순서(의존성):** 메타 추출(T1) → 메타 저장·마이그레이션(T2) → L0 아카이브 모듈(T3) → ingest 통합(T4) → 신선도(T5) → hook(T6).

---

### Task 1: parser가 `attribution_skill`·`git_branch` 추출

raw 최상위 객체에 `attributionSkill`(어떤 스킬이 토큰을 태웠나)·`gitBranch`가 있다. 휘발 전에 잡지 않으면 영영 못 본다(설계 §6, D4).

**Files:**
- Modify: `tokenomy/parser.py` (UsageRecord 필드 2개 + parse_usage_line 추출)
- Test: `tests/test_parser.py` (헬퍼 확장 + 테스트 2개)

- [ ] **Step 1: 테스트 헬퍼 `_assistant_line`에 두 필드 주입 분기 추가**

`tests/test_parser.py`의 `_assistant_line` 함수에서, `if "version" in over:` 블록 바로 아래에 추가:

```python
    if "attribution_skill" in over:
        obj["attributionSkill"] = over["attribution_skill"]
    if "git_branch" in over:
        obj["gitBranch"] = over["git_branch"]
```

- [ ] **Step 2: 실패 테스트 작성**

`tests/test_parser.py` 끝에 추가:

```python
def test_extracts_attribution_skill_and_git_branch():
    rec = parse_usage_line(
        _assistant_line(attribution_skill="brainstorming", git_branch="feat/x")
    )
    assert rec.attribution_skill == "brainstorming"
    assert rec.git_branch == "feat/x"


def test_attribution_and_branch_default_none():
    rec = parse_usage_line(_assistant_line())
    assert rec.attribution_skill is None
    assert rec.git_branch is None
```

- [ ] **Step 3: 실행 → 실패 확인**

Run: `python -m pytest tests/test_parser.py::test_extracts_attribution_skill_and_git_branch -v`
Expected: FAIL — `AttributeError: 'UsageRecord' object has no attribute 'attribution_skill'`

- [ ] **Step 4: UsageRecord에 필드 추가**

`tokenomy/parser.py`의 `UsageRecord` dataclass에서 `cache_creation_1h` 줄 아래에 추가:

```python
    attribution_skill: str | None = None
    git_branch: str | None = None
```

- [ ] **Step 5: parse_usage_line 반환에 추출 추가**

`tokenomy/parser.py`의 `parse_usage_line` `return UsageRecord(...)`에서 `cache_creation_1h=cache_1h,` 줄 아래에 추가:

```python
        attribution_skill=obj.get("attributionSkill"),
        git_branch=obj.get("gitBranch"),
```

- [ ] **Step 6: 실행 → 통과 확인**

Run: `python -m pytest tests/test_parser.py -v`
Expected: PASS (기존 + 신규 2개 모두)

- [ ] **Step 7: 커밋**

```bash
git add tokenomy/parser.py tests/test_parser.py
git commit -m "feat(parser): attribution_skill·git_branch 메타 추출"
```

---

### Task 2: messages 메타 컬럼 + 마이그레이션 + 저장

신규 컬럼을 적재하고, **기존 DB도 깨지지 않게** 마이그레이션을 추가한다. `connect()`엔 현재 마이그레이션이 없어 `b2db0e2`가 넣은 `request_id`/`is_sidechain`조차 기존 DB엔 안 생긴다 — 이 마이그레이션이 그것까지 함께 메운다.

**Files:**
- Modify: `tokenomy/db.py` (SCHEMA 컬럼 2개, `_migrate`, `connect`, `ingest_records`)
- Test: `tests/test_db.py` (저장 + 마이그레이션)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_db.py` 끝에 추가:

```python
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
```

- [ ] **Step 2: 실행 → 실패 확인**

Run: `python -m pytest tests/test_db.py::test_stores_attribution_skill_and_branch tests/test_db.py::test_migration_adds_columns_to_legacy_db -v`
Expected: FAIL — `sqlite3.OperationalError: no such column: attribution_skill` (저장), 마이그레이션 테스트는 컬럼 없음 assert 실패

- [ ] **Step 3: SCHEMA에 컬럼 2개 추가**

`tokenomy/db.py`의 `SCHEMA` 안 `messages` 테이블에서 `is_sidechain INTEGER DEFAULT 0` 줄 뒤에 컬럼을 추가 (콤마 주의):

```sql
    is_sidechain INTEGER DEFAULT 0,
    attribution_skill TEXT,
    git_branch TEXT
```

- [ ] **Step 4: `_migrate` 함수 추가**

`tokenomy/db.py`에서 `SCHEMA` 문자열 정의 바로 아래에 추가:

```python
# connect 시 기존 DB에 빠진 컬럼을 보강한다. CREATE TABLE IF NOT EXISTS는 기존
# 테이블 스키마를 바꾸지 않으므로(신규 컬럼 누락), 여기서 ALTER로 메운다.
_MIGRATE_COLS = {
    "request_id": "TEXT",
    "is_sidechain": "INTEGER DEFAULT 0",
    "attribution_skill": "TEXT",
    "git_branch": "TEXT",
}


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    for col, decl in _MIGRATE_COLS.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {decl}")
    conn.commit()
```

- [ ] **Step 5: `connect`에서 `_migrate` 호출**

`tokenomy/db.py`의 `connect` 함수에서 `conn.executescript(SCHEMA)` 줄 바로 아래에 추가:

```python
    _migrate(conn)
```

- [ ] **Step 6: `ingest_records` INSERT에 컬럼 반영**

`tokenomy/db.py`의 `ingest_records` 안 `messages` INSERT를 아래로 교체 (컬럼 목록·VALUES·ON CONFLICT SET·params 4곳 모두 반영):

```python
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
```

- [ ] **Step 7: 실행 → 전체 통과 확인**

Run: `python -m pytest tests/test_db.py -v`
Expected: PASS (기존 dedup 테스트 + 신규 2개)

- [ ] **Step 8: 커밋**

```bash
git add tokenomy/db.py tests/test_db.py
git commit -m "feat(db): 메타 컬럼 2종 + 누락 마이그레이션(request_id·is_sidechain 포함)"
```

---

### Task 3: L0 Raw Archive 모듈

raw 원문을 parser 미경유 바이트 복사로 `data/archive/<provider>/<상대경로>`에 증분 보관한다. 진실의 원천(LLM 복기 입력), 로컬 고정(설계 §4, D2).

**Files:**
- Create: `tokenomy/archive.py`
- Test: `tests/test_archive.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_archive.py` 생성:

```python
from tokenomy.db import connect
from tokenomy.archive import archive_tree


def _seed(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_archive_copies_raw_lines(tmp_path):
    conn = connect(":memory:")
    root = tmp_path / "projects"
    _seed(root, "proj/s.jsonl", '{"a":1}\n')
    archive_dir = tmp_path / "archive"
    n = archive_tree(root, conn, provider="claude", archive_root=archive_dir)
    assert n == 1
    dest = archive_dir / "claude" / "proj" / "s.jsonl"
    assert dest.read_text(encoding="utf-8") == '{"a":1}\n'


def test_archive_incremental_appends_only_new(tmp_path):
    conn = connect(":memory:")
    root = tmp_path / "projects"
    f = _seed(root, "p/s.jsonl", '{"a":1}\n')
    archive_dir = tmp_path / "archive"
    archive_tree(root, conn, archive_root=archive_dir)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write('{"b":2}\n')
    archive_tree(root, conn, archive_root=archive_dir)
    dest = archive_dir / "claude" / "p" / "s.jsonl"
    assert dest.read_text(encoding="utf-8") == '{"a":1}\n{"b":2}\n'


def test_archive_second_run_no_dup(tmp_path):
    conn = connect(":memory:")
    root = tmp_path / "projects"
    _seed(root, "p/s.jsonl", '{"a":1}\n')
    archive_dir = tmp_path / "archive"
    archive_tree(root, conn, archive_root=archive_dir)
    n2 = archive_tree(root, conn, archive_root=archive_dir)  # 새 바이트 없음
    dest = archive_dir / "claude" / "p" / "s.jsonl"
    assert dest.read_text(encoding="utf-8") == '{"a":1}\n'  # 두 배 안 됨
    assert n2 == 0
```

- [ ] **Step 2: 실행 → 실패 확인**

Run: `python -m pytest tests/test_archive.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tokenomy.archive'`

- [ ] **Step 3: archive.py 구현**

`tokenomy/archive.py` 생성:

```python
"""L0 Raw Archive — raw JSONL 원문을 휘발(기본 30일) 전에 보존.

ingest(L1 파싱)와 별개로, raw 라인을 data/archive/<provider>/<상대경로>로
증분 바이트 복사한다. parser를 거치지 않으므로 원문이 손실 없이 남는다.
미래 LLM 고도화 복기의 진실의 원천. 로컬 고정(반출 금지 — 설계 D3).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from tokenomy.parser import discover_session_files

ARCHIVE_ROOT = Path("data/archive")


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
    archive_root=ARCHIVE_ROOT,
) -> int:
    """root 아래 모든 *.jsonl을 증분 아카이브. 새 바이트가 복사된 파일 수 반환."""
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
```

- [ ] **Step 4: 실행 → 통과 확인**

Run: `python -m pytest tests/test_archive.py -v`
Expected: PASS (3개)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/archive.py tests/test_archive.py
git commit -m "feat(archive): L0 raw 원문 증분 바이트 복사"
```

---

### Task 4: ingest에 아카이브 통합 (cli)

`cmd_ingest`가 파싱(L1)과 함께 아카이브(L0)를 돌리게 한다. Codex rollout도 raw이므로 함께 아카이브한다.

**Files:**
- Modify: `tokenomy/cli.py` (`cmd_ingest`)

- [ ] **Step 1: import 추가**

`tokenomy/cli.py` 상단 import 블록에서 `from tokenomy.db import connect, ingest_root` 줄을 아래로 교체:

```python
from tokenomy.archive import archive_tree
from tokenomy.db import connect, ingest_root
```

- [ ] **Step 2: `cmd_ingest` 교체**

`tokenomy/cli.py`의 `cmd_ingest`를 아래로 교체:

```python
def cmd_ingest(conn) -> None:
    pricing = load_pricing()
    n_claude = ingest_root(conn, CLAUDE_ROOT, pricing, provider="claude")
    n_arch = archive_tree(CLAUDE_ROOT, conn, provider="claude")
    n_codex = ingest_codex(conn, CODEX_ROOT, pricing)
    archive_tree(CODEX_ROOT, conn, provider="chatgpt")
    print(f"[ingest] claude={n_claude}  codex={n_codex}  archived_files={n_arch}  new records")
```

- [ ] **Step 3: 회귀 + 수동 검증**

Run: `python -m pytest -q`
Expected: PASS (전체)

수동 검증 (실제 데이터):

```bash
python -m tokenomy.cli ingest
ls data/archive/claude   # 프로젝트별 폴더가 생겼는지
```

Expected: `[ingest] ... archived_files=N ...` 출력 + `data/archive/claude/` 아래 `.jsonl` 생성

- [ ] **Step 4: 커밋**

```bash
git add tokenomy/cli.py
git commit -m "feat(cli): ingest에 L0 아카이브 통합(claude·codex)"
```

---

### Task 5: 수집 신선도(freshness) + 경고

마지막 ingest 시각을 기록하고, 디스크상 가장 오래된 raw 나이로 유실 위험을 경고한다(설계 §5). 트리거가 다 실패해도 사람이 인지하는 안전벨트.

**Files:**
- Modify: `tokenomy/db.py` (`meta` 테이블 + `set_meta`/`get_meta`)
- Create: `tokenomy/freshness.py`
- Modify: `tokenomy/cli.py` (`cmd_ingest`에 기록, `cmd_report`에 경고)
- Test: `tests/test_freshness.py`

- [ ] **Step 1: db에 meta 테이블 + 접근자 (실패 테스트 먼저)**

`tests/test_db.py` 끝에 추가:

```python
def test_meta_set_get():
    from tokenomy.db import set_meta, get_meta
    conn = connect(":memory:")
    assert get_meta(conn, "k") is None
    set_meta(conn, "k", "v")
    assert get_meta(conn, "k") == "v"
    set_meta(conn, "k", "v2")  # upsert
    assert get_meta(conn, "k") == "v2"
```

Run: `python -m pytest tests/test_db.py::test_meta_set_get -v`
Expected: FAIL — `ImportError: cannot import name 'set_meta'`

- [ ] **Step 2: db에 meta 구현**

`tokenomy/db.py`의 `SCHEMA` 안 `scan_offsets` 테이블 정의 뒤(닫는 `"""` 앞)에 추가:

```sql

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

그리고 `set_user` 함수 아래에 추가:

```python
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
```

Run: `python -m pytest tests/test_db.py::test_meta_set_get -v` → PASS

- [ ] **Step 3: freshness 실패 테스트 작성**

`tests/test_freshness.py` 생성:

```python
import os
from datetime import datetime, timedelta

from tokenomy.aggregate import KST
from tokenomy.db import connect
from tokenomy.freshness import freshness, record_ingest, WARN_AGE_DAYS


def _seed(root, age_days, now):
    p = root / "p" / "s.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}\n", encoding="utf-8")
    t = (now - timedelta(days=age_days)).timestamp()
    os.utime(p, (t, t))
    return p


def test_record_and_hours_since(tmp_path):
    conn = connect(":memory:")
    now = datetime(2026, 6, 12, 10, 0, 0, tzinfo=KST)
    record_ingest(conn, now)
    fr = freshness(conn, tmp_path, now + timedelta(hours=3))
    assert fr.last_ingest_ts == now.isoformat()
    assert abs(fr.hours_since_ingest - 3) < 0.01


def test_old_raw_triggers_warn(tmp_path):
    conn = connect(":memory:")
    now = datetime(2026, 6, 12, 12, 0, 0, tzinfo=KST)
    _seed(tmp_path / "projects", 27, now)
    fr = freshness(conn, tmp_path / "projects", now)
    assert fr.oldest_raw_age_days >= WARN_AGE_DAYS
    assert fr.level == "warn"


def test_recent_raw_is_ok(tmp_path):
    conn = connect(":memory:")
    now = datetime(2026, 6, 12, 12, 0, 0, tzinfo=KST)
    _seed(tmp_path / "projects", 2, now)
    fr = freshness(conn, tmp_path / "projects", now)
    assert fr.level == "ok"
```

Run: `python -m pytest tests/test_freshness.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tokenomy.freshness'`

- [ ] **Step 4: freshness.py 구현**

`tokenomy/freshness.py` 생성:

```python
"""수집 신선도 — 트리거가 다 실패해도 유실 위험을 사람에게 노출(설계 §5).

마지막 ingest 경과 + 디스크상 가장 오래된 raw 파일 나이(vs 30일 cleanup).
now는 주입받는다(테스트 가능 — aggregate.parse_ts와 동일 원칙).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

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


def freshness(conn, root, now: datetime) -> Freshness:
    last = get_meta(conn, LAST_INGEST_KEY)
    hours = None
    if last:
        try:
            dt = datetime.fromisoformat(last)
            hours = (now - dt).total_seconds() / 3600
        except ValueError:
            pass

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
```

Run: `python -m pytest tests/test_freshness.py -v` → PASS

- [ ] **Step 5: cli ingest가 마지막 ingest 시각을 기록**

`tokenomy/cli.py` import 블록에 추가:

```python
from tokenomy.freshness import freshness, record_ingest
```

`cmd_ingest`의 마지막 `print(...)` 줄 **앞**에 추가:

```python
    record_ingest(conn, datetime.now(KST))
```

(`datetime`·`KST`는 cli.py에 이미 import되어 있다.)

- [ ] **Step 6: cmd_report 상단에 신선도 경고 표시**

`tokenomy/cli.py`의 `cmd_report`에서 `print(f"User: ...")` 줄 아래에 추가:

```python
    fr = freshness(conn, CLAUDE_ROOT, now)
    if fr.level == "warn":
        print(
            f"  [!] 수집 신선도: 가장 오래된 raw {fr.oldest_raw_age_days:.0f}일째 — "
            f"30일 경과 전 ingest 필요(미수집분 유실 위험)"
        )
    elif fr.hours_since_ingest is not None:
        print(f"  수집 최신: {fr.hours_since_ingest:.0f}h 전")
```

- [ ] **Step 7: 회귀 확인**

Run: `python -m pytest -q`
Expected: PASS (전체)

- [ ] **Step 8: 커밋**

```bash
git add tokenomy/db.py tokenomy/freshness.py tokenomy/cli.py tests/test_db.py tests/test_freshness.py
git commit -m "feat(freshness): 수집 신선도 계산 + report 경고 + 마지막 ingest 기록"
```

---

### Task 6: SessionEnd hook (D1 트리거 ①)

세션 종료 시 자동 ingest를 건다(설계 §3 트리거 ①). **코드가 아니라 설정**이며, 조직 managed settings가 막을 수 있으므로 검증 스파이크를 동반한다. 막혀도 웹/CLI 기동 ingest(②, 웹 task 소관)가 커버한다.

**Files:**
- 설정: `~/.claude/settings.json`

- [ ] **Step 1: 현재 hook 구조 확인**

Run: `python -c "import json,os; p=os.path.expanduser('~/.claude/settings.json'); d=json.load(open(p,encoding='utf-8')); print(json.dumps(d.get('hooks',{}), ensure_ascii=False, indent=2))"`
Expected: 기존 hooks 구조 출력(없으면 `{}`). 기존 형식이 있으면 그 형식을 따른다.

- [ ] **Step 2: SessionEnd hook 추가**

`~/.claude/settings.json`의 `hooks`에 SessionEnd 항목을 추가(기존 hook이 있으면 병합). 정확한 스키마 적용은 **`update-config` 스킬 사용을 권장**(settings.json 스키마를 안다). 직접 편집 시 형식:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "cd C:/projects/tokenomy && python -m tokenomy.cli ingest"
          }
        ]
      }
    ]
  }
}
```

주의: hook이 도는 작업 디렉토리는 worktree가 아니라 **메인 체크아웃**(`C:/projects/tokenomy`)이어야 한다 — worktree는 임시다. 메인에 구현이 머지된 뒤 활성화한다.

- [ ] **Step 3: 검증 스파이크**

1. 아무 디렉토리에서 Claude Code 세션을 열고 짧은 작업 후 종료.
2. `python -c "import sqlite3,os; c=sqlite3.connect('data/tokenomy.db'); print(c.execute('SELECT value FROM meta WHERE key=\"last_ingest_ts\"').fetchone())"` 로 `last_ingest_ts`가 방금 시각으로 갱신됐는지 확인.

- [ ] **Step 4: 막힘 시 폴백 기록**

hook이 동작하지 않으면(managed settings 차단 등) `docs/` 또는 README에 한 줄 남기고 Windows Task Scheduler(N시간마다 `python -m tokenomy.cli ingest`)로 대체. 트리거 다중화이므로 hook 부재가 치명적이지 않다.

- [ ] **Step 5: 커밋**

설정 파일(`~/.claude/settings.json`)은 repo 밖이라 커밋 대상이 아니다. 검증 결과·폴백 메모를 docs에 남겼다면 그것만 커밋:

```bash
git add docs/
git commit -m "docs: SessionEnd hook 설정·검증 메모(D1 트리거)"
```

---

## 완료 기준

- [ ] `python -m pytest -q` 전체 통과 (기존 55 + 신규 약 11)
- [ ] `python -m tokenomy.cli ingest` 실행 시 `data/archive/claude/`에 원문 `.jsonl` 생성
- [ ] `python -m tokenomy.cli report`에 신선도 줄 표시
- [ ] 기존 DB(구 스키마)로 `connect()` 시 마이그레이션으로 신규 컬럼 생성·기존 행 보존

## 범위 밖 (설계 §7·§8 — 후속 task)

- export(공유) — 포맷 TBD
- LLM 고도화 복기 파이프라인 (입력 = L0 archive)
- 중앙화 / 멀티유저 push
- 웹/CLI 기동 시 자동 ingest(트리거 ②) — 웹 대시보드 task 소관
- 아카이브 압축(.gz)
