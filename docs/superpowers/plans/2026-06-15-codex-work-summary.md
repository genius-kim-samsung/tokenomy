# Codex 작업요약(첫 프롬프트 발췌) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Codex 세션의 내역 "작업요약" 열에 첫 사용자 프롬프트 발췌(120자)를 표시한다.

**Architecture:** `codex_parser.parse_rollout`이 rollout의 첫 `user_message`(없으면 환경 컨텍스트 아닌 첫 user 메시지)를 120자로 발췌해 `UsageRecord.summary`에 싣고, `db.ingest_records`가 `sessions.summary`에 `COALESCE`로 반영한다. Claude의 `aiTitle` 경로(`ingest_titles`)와 표시 템플릿은 손대지 않는다.

**Tech Stack:** Python 3 (stdlib: json/pathlib), SQLite(sqlite3), pytest. 소스 실행 데이터는 repo 루트 `data/`·`config/`.

**Spec:** `docs/superpowers/specs/2026-06-15-codex-work-summary-design.md`

**작업 위치:** 워크트리 `C:\projects\tokenomy\.claude\worktrees\codex-work-summary` (브랜치 `worktree-codex-work-summary`). 모든 명령은 이 디렉토리에서 실행. Python은 `.venv\Scripts\python` (워크트리에 .venv가 없으면 부모 `C:\projects\tokenomy\.venv\Scripts\python` 사용).

---

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `tokenomy/parser.py` | `UsageRecord` 데이터 모델 | `summary` 필드 1개 추가 |
| `tokenomy/codex_parser.py` | Codex rollout → `UsageRecord` | `_truncate`/`_extract_first_prompt` 추가, `parse_rollout`에서 summary 채움, docstring 보강 |
| `tokenomy/db.py` | SQLite 적재 | `sessions` upsert에 `summary`(COALESCE) 반영 + 주석 문구 |
| `tests/test_codex_parser.py` | Codex 파서 단위 테스트 | 발췌 테스트 추가 |
| `tests/test_db.py` | 적재 단위 테스트 | summary 반영/COALESCE 테스트 추가 |
| `tests/test_web.py` | 웹 응답 테스트 | settings 프라이버시 문구 단언 동기화 |
| `tokenomy/web/templates/settings.html`, `CLAUDE.md`, `README.md`, `README.ko.md` | 프라이버시 안내 문구 | 발췌 예외 반영해 재정의 |

**비목표(구현하지 않음):** 전체 대화 원문 저장, Codex 턴별 분해, Claude aiTitle fallback. 내역 템플릿(`_history_rows.html`)은 이미 `s.summary`를 표시하므로 변경 없음(Task 4에서 눈으로만 확인).

---

## Task 1: 첫 프롬프트 발췌 추출 (codex_parser + UsageRecord 필드)

**Files:**
- Modify: `tokenomy/parser.py` (UsageRecord — 약 라인 20~37 사이 필드 블록 끝)
- Modify: `tokenomy/codex_parser.py` (헬퍼 추가 + `parse_rollout` return)
- Test: `tests/test_codex_parser.py`

- [ ] **Step 1: 발췌 테스트 4개를 작성(실패 예정)**

`tests/test_codex_parser.py` 파일 끝에 아래를 추가한다. (기존 `_write_rollout`, `parse_rollout` import 재사용)

```python
def _rollout_with_messages(tmp_path, msgs, name="rollout-msg.jsonl"):
    """session_meta + token_count 뒤에 msgs 라인을 붙인 rollout 파일을 만든다."""
    f = tmp_path / name
    lines = [
        {"type": "session_meta", "payload": {"id": "sess-m", "cwd": "/proj",
                                             "timestamp": "2026-06-11T12:50:14Z"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 100, "cached_input_tokens": 40,
                                  "output_tokens": 10}}}},
    ]
    lines.extend(msgs)
    f.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    return f


def test_summary_from_user_message(tmp_path):
    # 첫 message(role=user)는 environment_context, 그 뒤 user_message가 실제 입력
    f = _rollout_with_messages(tmp_path, [
        {"type": "response_item", "payload": {"type": "message", "role": "user",
            "content": [{"type": "input_text",
                         "text": "<environment_context>\n  <cwd>/proj</cwd>\n</environment_context>"}]}},
        {"type": "event_msg", "payload": {"type": "user_message",
                                          "message": "내역에 codex 요약 추가해줘"}},
    ])
    rec = parse_rollout(str(f))
    assert rec.summary == "내역에 codex 요약 추가해줘"


def test_summary_fallback_skips_environment_context(tmp_path):
    # user_message가 없을 때, environment_context는 건너뛰고 다음 user 메시지를 쓴다
    f = _rollout_with_messages(tmp_path, [
        {"type": "response_item", "payload": {"type": "message", "role": "user",
            "content": [{"type": "input_text",
                         "text": "<environment_context>\n  <cwd>/proj</cwd>\n</environment_context>"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "실제 사용자 입력"}]}},
    ])
    rec = parse_rollout(str(f))
    assert rec.summary == "실제 사용자 입력"


def test_summary_truncated_and_normalized(tmp_path):
    f = _rollout_with_messages(tmp_path, [
        {"type": "event_msg", "payload": {"type": "user_message",
                                          "message": "줄1\n줄2   여러   공백"}},
    ])
    assert parse_rollout(str(f)).summary == "줄1 줄2 여러 공백"

    f2 = _rollout_with_messages(tmp_path, [
        {"type": "event_msg", "payload": {"type": "user_message", "message": "가" * 200}},
    ], name="rollout-long.jsonl")
    assert len(parse_rollout(str(f2)).summary) == 120


def test_summary_none_when_no_user_input(tmp_path):
    # _write_rollout에는 user 입력이 없다 → summary None
    assert parse_rollout(str(_write_rollout(tmp_path))).summary is None
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_codex_parser.py -q`
Expected: FAIL — `AttributeError: 'UsageRecord' object has no attribute 'summary'` (또는 `summary` 미정의로 4개 실패)

- [ ] **Step 3: `UsageRecord`에 `summary` 필드 추가**

`tokenomy/parser.py`의 `UsageRecord` dataclass에서 `git_branch: str | None = None` 바로 다음 줄에 추가:

```python
    git_branch: str | None = None
    summary: str | None = None  # 세션 식별용 첫 프롬프트 발췌(Codex). Claude는 None(aiTitle 별도 경로).
```

- [ ] **Step 4: `codex_parser.py`에 발췌 헬퍼 추가**

`tokenomy/codex_parser.py`에서 `CODEX_ROOT = ...` 정의 다음, `parse_rollout` 정의 앞에 추가:

```python
def _truncate(text: str, limit: int = 120) -> str:
    """개행→공백, 연속 공백을 접고 limit자로 자른다."""
    return " ".join(text.split())[:limit]


def _extract_first_prompt(path: str, limit: int = 120) -> str | None:
    """rollout에서 첫 사용자 프롬프트를 limit자로 발췌. 없으면 None.

    1순위: payload.type == 'user_message'의 message(환경 컨텍스트가 빠진 순수 입력).
    2순위: message(role=user) content의 첫 텍스트 중 '<environment_context'로
           시작하지 않는 것. (user_message가 전무한 세션 대비 fallback)
    rollout은 세션당 1파일이라 작아 parse_rollout과 별도로 한 번 더 읽어도 무방.
    """
    fallback: str | None = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(o, dict):
                continue
            p = o.get("payload")
            p = p if isinstance(p, dict) else {}
            if p.get("type") == "user_message":
                msg = p.get("message")
                if isinstance(msg, str) and msg.strip():
                    return _truncate(msg, limit)
            elif fallback is None and p.get("role") == "user":
                content = p.get("content")
                txt = None
                if isinstance(content, str):
                    txt = content
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"].strip():
                            txt = c["text"]
                            break
                if txt and not txt.lstrip().startswith("<environment_context"):
                    fallback = _truncate(txt, limit)
    return fallback
```

- [ ] **Step 5: `parse_rollout` return에 summary 추가**

`tokenomy/codex_parser.py`의 `parse_rollout` 마지막 `return UsageRecord(...)`에서 `message_id=session_id,` 다음 줄에 추가:

```python
        message_id=session_id,  # 세션당 1레코드 → dedup_key = session_id
        summary=_extract_first_prompt(path),
    )
```

- [ ] **Step 6: 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_codex_parser.py -q`
Expected: PASS (기존 4개 + 신규 4개)

- [ ] **Step 7: 커밋**

```bash
git add tokenomy/parser.py tokenomy/codex_parser.py tests/test_codex_parser.py
git commit -m "feat(codex): 첫 사용자 프롬프트를 작업요약으로 발췌"
```

---

## Task 2: db.py — sessions upsert에 summary 반영(COALESCE)

**Files:**
- Modify: `tokenomy/db.py:184-192` (`ingest_records`의 sessions upsert)
- Test: `tests/test_db.py`

- [ ] **Step 1: 적재 테스트 작성(실패 예정)**

`tests/test_db.py`에 아래 테스트를 추가한다. 기존 import에 `from tokenomy.parser import UsageRecord`, `from tokenomy.db import connect, ingest_records`가 없다면 추가(파일 상단 기존 import 형태에 맞춘다).

```python
def _rec(session_id, summary=None, ts="2026-06-11T00:00:00Z", provider="codex"):
    return UsageRecord(
        provider=provider, session_id=session_id, cwd="/proj", ts=ts,
        model="gpt-5.5", input_tokens=10, output_tokens=1,
        cache_creation=0, cache_read=0, message_id=session_id, summary=summary,
    )


def test_codex_summary_persisted_to_sessions():
    conn = connect(":memory:")
    ingest_records(conn, [_rec("s1", summary="codex 첫 프롬프트")], {})
    row = conn.execute("SELECT summary FROM sessions WHERE session_id='s1'").fetchone()
    assert row["summary"] == "codex 첫 프롬프트"


def test_summary_none_does_not_overwrite_existing():
    # aiTitle(별도 경로)로 채워진 summary를, 뒤이은 summary=None 적재가 덮지 않아야 한다
    conn = connect(":memory:")
    ingest_records(conn, [_rec("s2", summary="기존 요약", provider="claude")], {})
    ingest_records(conn, [_rec("s2", summary=None, provider="claude",
                               ts="2026-06-12T00:00:00Z")], {})
    row = conn.execute("SELECT summary FROM sessions WHERE session_id='s2'").fetchone()
    assert row["summary"] == "기존 요약"
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_db.py -q -k "summary"`
Expected: FAIL — `test_codex_summary_persisted_to_sessions`에서 `row["summary"]` is None (upsert가 summary를 안 넣음)

- [ ] **Step 3: sessions upsert 수정**

`tokenomy/db.py`의 `ingest_records` 안 sessions `conn.execute(...)` 블록(현재 184-192행)을 아래로 교체:

```python
        conn.execute(
            """INSERT INTO sessions (session_id, project, provider, first_ts, last_ts, summary)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                   last_ts = MAX(sessions.last_ts, excluded.last_ts),
                   first_ts = MIN(sessions.first_ts, excluded.first_ts),
                   project = COALESCE(sessions.project, excluded.project),
                   summary = COALESCE(excluded.summary, sessions.summary)""",
            (r.session_id, r.cwd, r.provider, r.ts, r.ts, r.summary),
        )
```

> 주: `summary = COALESCE(excluded.summary, sessions.summary)` 덕분에 `summary=None`(Claude
> 레코드)은 `ingest_titles`가 채운 기존 값을 덮지 않는다. Codex는 매 재파싱마다 같은 발췌로 갱신.

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_db.py -q -k "summary"`
Expected: PASS (2개)

- [ ] **Step 5: 커밋**

```bash
git add tokenomy/db.py tests/test_db.py
git commit -m "feat(db): sessions.summary에 Codex 발췌 반영(COALESCE)"
```

---

## Task 3: 프라이버시 문구 재정의

발췌 저장이 "대화 원문은 저장하지 않는다"는 기존 단언과 모순되므로 재정의한다(완전 삭제 아님 — 전체 대화 비저장은 여전히 사실). 새 문장 원칙:

> 토큰 메타 + 세션 식별용 첫 프롬프트 발췌(약 120자)만 저장하며, 전체 대화 기록은 저장하지 않는다.

**Files:**
- Test: `tests/test_web.py:122`
- Modify: `tokenomy/web/templates/settings.html:20`, `CLAUDE.md:6`, `CLAUDE.md:57-58`, `README.md:18-19`, `README.ko.md:17`

- [ ] **Step 1: test_web.py 단언을 새 문구로 변경(실패 예정)**

`tests/test_web.py:122`:

```python
    assert "전체 대화 기록은 저장하지 않습니다" in r.text
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -q -k "settings or privacy or 대화"`
Expected: FAIL — settings.html이 아직 옛 문구라 새 문자열 미존재
(테스트 이름을 모르면 `.venv\Scripts\python -m pytest tests/test_web.py -q` 전체 실행 후 해당 단언 실패 확인)

- [ ] **Step 3: settings.html 문구 교체**

`tokenomy/web/templates/settings.html:20` 의 `<p class="muted">...</p>` 를 교체:

```html
  <p class="muted">모든 처리는 로컬에서 이뤄지며 토큰 사용 메타와 <strong>세션 식별용 첫 프롬프트 발췌</strong>만 저장합니다 — <strong>전체 대화 기록은 저장하지 않습니다</strong>.</p>
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -q`
Expected: PASS

- [ ] **Step 5: 나머지 문서 문구 교체 (테스트 무관, 일괄)**

`CLAUDE.md:6` (파일 상단 한 줄 설명):
```
**대화 원문은 저장하지 않는다(토큰 메타 + 세션 식별용 첫 프롬프트 발췌만)**.
```

`CLAUDE.md:57-58` (프라이버시 경계 게시):
```
- **프라이버시 경계 — 발췌선을 지킬 것.** 파서는 토큰 usage 메타를 추출하고, Codex는
  세션 식별용으로 **첫 사용자 프롬프트만 120자 발췌**해 `sessions.summary`에 저장한다.
  그 외 content/프롬프트/대화 본문 전체는 DB에 절대 넣지 않는다.
```

`README.md:18-19` (영문 Privacy):
```
- Parses token **metadata** (tokens, time, project, model) plus a **short excerpt
  of the first user prompt** (for session identification). **Full conversation
  content is never stored.**
```

`README.ko.md:17`:
```
- 토큰 **메타데이터**(토큰/시간/프로젝트/모델)와 **세션 식별용 첫 프롬프트 발췌**만 저장한다. **전체 대화 기록은 저장하지 않는다.**
```

- [ ] **Step 6: codex_parser.py docstring 보강**

`tokenomy/codex_parser.py` 상단 docstring의 매핑 설명 끝(닫는 `"""` 직전)에 한 줄 추가:

```
캐시 효율과 별개로, 첫 사용자 프롬프트를 120자 발췌해 summary(작업요약)로 싣는다.
```

- [ ] **Step 7: 회귀 없는지 확인 후 커밋**

Run: `.venv\Scripts\python -m pytest tests/test_web.py -q`
Expected: PASS

```bash
git add CLAUDE.md README.md README.ko.md tokenomy/web/templates/settings.html tokenomy/codex_parser.py tests/test_web.py
git commit -m "docs(privacy): 첫 프롬프트 발췌 저장을 반영해 경계 문구 재정의"
```

---

## Task 4: 통합 검증

**Files:** (변경 없음 — 확인만)

- [ ] **Step 1: 전체 테스트**

Run: `.venv\Scripts\python -m pytest -q`
Expected: PASS (기존 메모리상 `test_launcher` 포트 충돌 2건은 앱이 8765 포트를 점유 중일 때만 실패하는 환경 이슈 — 회귀 아님. 그 경우 앱을 닫고 재실행)

- [ ] **Step 2: 실제 Codex 데이터로 ingest 후 작업요약 확인**

```bash
.venv\Scripts\python -m tokenomy.cli ingest
.venv\Scripts\python -m tokenomy.cli report
```
그 다음 대시보드로 내역 화면 확인:
```bash
.venv\Scripts\python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765
```
Expected: 내역(History) 화면의 Codex(`[codex]` 뱃지) 행 "작업요약" 열에 첫 프롬프트 발췌가 보인다(기존엔 `—`). Claude 행의 aiTitle 요약은 그대로 유지.

- [ ] **Step 3: 템플릿 무변경 확인**

`tokenomy/web/templates/_history_rows.html`에서 `s.summary`가 작업요약 `<td class="col-sum">`에 출력되고 `title="{{ s.summary or '' }}"`로 전문이 붙는지 눈으로 확인(코드 수정 불필요).

- [ ] **Step 4: (이상 시) 발췌 길이/소스 조정**

발췌가 너무 길/짧거나 환경 컨텍스트가 새면 `_truncate`의 `limit` 또는 `_extract_first_prompt`의 fallback 조건을 조정하고 Task 1 테스트를 갱신해 재검증.

---

## Self-Review (작성자 점검 완료)

- **Spec coverage:** §3.1 추출→Task1, §3.2 모델→Task1 Step3, §3.3 적재→Task2, §3.4 표시 무변경→Task4 Step3, §3.5 문구→Task3, §5 엣지(None/120자/정규화/fallback)→Task1 테스트, §6 테스트→Task1·2·3. 누락 없음.
- **Placeholder scan:** TBD/TODO/"적절히 처리" 없음. 모든 코드 step에 실제 코드 포함.
- **Type consistency:** `summary` 필드명·`_extract_first_prompt`/`_truncate` 시그니처가 Task1·2 전반에서 일치. `COALESCE(excluded.summary, sessions.summary)` 동일 표기.
- **참고:** `DESIGN.md`는 미추적이라 워크트리에 없어 범위에서 제외(메인 로컬 파일). `tokenomy/parser.py:4`·`archive.py`의 "원문 보존" 문구는 Claude 파서/raw archive의 사실 그대로라 수정하지 않는다(발췌는 codex_parser 한정).
