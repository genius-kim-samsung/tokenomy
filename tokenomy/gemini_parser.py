"""Gemini CLI 세션 로그 파서 — 플러그인. 두 저장 포맷을 모두 지원한다.

Gemini CLI는 세션당 1파일을 ~/.gemini/tmp/<프로젝트>/chats/session-<ts>-<id>.* 에 쓴다.
버전에 따라 포맷이 갈린다(gemini-cli가 봄~여름 2026에 `.json` → `.jsonl`로 전환):
- 옛 `.json`: 단일 JSON 문서 {sessionId, projectHash, startTime, lastUpdated, messages[], kind}.
- 새 `.jsonl`(v0.50~): **append-only 뮤테이션 로그**. 줄마다 레코드 — 메시지(id 보유)·
  `$rewindTo`(되감기)·`$set`(메타/messages 치환)·헤더(sessionId+projectHash). _collect_jsonl_messages가
  **소비 이벤트**를 id별 union(last-wins)한다 — gemini-cli 대화 재구성과 달리 되감기·교체된 턴도
  이미 소비된 토큰이라 계상에서 빼지 않는다(claude/codex 원장 불변식과 동일, ADR 0028).
공통: messages[]의 각 {id(uuid), timestamp, type("user"|"gemini"), content, ...}에서
  gemini 타입 메시지에만 tokens{input,output,cached,thoughts,tool,total}·model이 붙는다.
- 토큰은 메시지별(누적 아님) — gemini 메시지마다 UsageRecord 1개로 정규화.
- 폴더 귀속: ~/.gemini/tmp/<프로젝트>/.project_root 에 실제 절대경로.

매핑(gemini usageMetadata → UsageRecord):
  input(fresh) = tokens.input - tokens.cached   (input이 cached를 포함)
  cache_read   = tokens.cached
  output       = tokens.output + tokens.thoughts (thinking은 output으로 과금)
  cache_write  = 0  (Gemini는 캐시 쓰기 토큰 구분 없음)
불변식: total_tokens == tokens.total (= input + output + thoughts).
프라이버시: 첫 사용자 프롬프트만 120자 발췌해 summary로 싣는다(그 외 본문 미적재).
"""
from __future__ import annotations

import json
from pathlib import Path

from tokenomy.parser import UsageRecord, kst_day

GEMINI_ROOT = Path.home() / ".gemini" / "tmp"


def _truncate(text: str, limit: int = 120) -> str:
    """개행→공백, 연속 공백을 접고 limit자로 자른다(Codex와 동일 규칙)."""
    return " ".join(text.split())[:limit]


def _message_text(content) -> str | None:
    """user 메시지 content에서 텍스트를 뽑는다. content는 문자열 또는 [{text}] 배열."""
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"].strip():
                return c["text"]
    return None


def _extract_first_prompt(messages: list, limit: int = 120) -> str | None:
    """첫 user 메시지의 텍스트를 limit자로 발췌. 없으면 None."""
    for m in messages:
        if isinstance(m, dict) and m.get("type") == "user":
            txt = _message_text(m.get("content"))
            if txt and txt.strip():
                return _truncate(txt, limit)
    return None


def _project_root(session_path: Path) -> str | None:
    """세션 파일의 프로젝트 실제 경로 — tmp/<프로젝트>/.project_root 파일을 읽는다.

    <프로젝트> = `chats` 디렉터리의 부모다. 최상위 세션(`chats/session-*.*`)과
    subagent 중첩 세션(`chats/<parentSessionId>/*.jsonl`)의 깊이가 달라, 경로에서
    `chats` 조상을 찾아 그 부모를 프로젝트로 잡는다(중첩 파일이 "chats"로 오귀속되지 않게).
    .project_root가 없으면(구 세션·해시 디렉터리) 디렉터리명으로 폴백.
    """
    project_dir = next(
        (p.parent for p in session_path.parents if p.name == "chats"),
        session_path.parent.parent,  # 방어적 폴백(chats 조상이 없으면 조부모)
    )
    marker = project_dir / ".project_root"
    try:
        root = marker.read_text(encoding="utf-8").strip()
        if root:
            return root
    except OSError:
        pass
    return project_dir.name


def _collect_jsonl_messages(path: Path) -> tuple[list, str | None]:
    """.jsonl 뮤테이션 로그에서 **소비 이벤트**(메시지)를 id 기준 union한다(last-wins).

    tokenomy는 대화 상태가 아니라 토큰 소비를 추적한다 — 그래서 gemini-cli의
    대화 재구성(loadConversationRecord)과 달리, 되감기·교체로 대화에서 사라진 턴도
    **API는 이미 호출·소비됐으므로 계상에서 빼지 않는다**(claude/codex 원장과 동일
    불변식: 메시지=소비 이벤트, dedup으로 1회, 삭제 없음 — ADR 0028). 규칙(줄 순서):
      - `id`(문자열) 보유 → 메시지 union(last-wins: 같은 id 재기록 시 최신 토큰값 채택).
      - `$set`(객체) → `$set.messages`의 각 메시지를 **clear 없이** union, sessionId 병합.
      - 헤더(`sessionId`+`projectHash`) → sessionId 채택(messages 동반 시 union).
      - `$rewindTo` → 무시(되감아도 소비 이벤트는 유지).
    손상 라인·비dict는 건너뛴다(append-only라 마지막 줄이 잘려 있을 수 있음).
    id별 last-wins라 수집 타이밍과 무관하게 결과가 결정적(전체 재파싱·dedup 멱등).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [], None
    messages: dict[str, dict] = {}  # id → 메시지(union, last-wins, 삭제 없음)
    session_id: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if isinstance(rec.get("id"), str):
            messages[rec["id"]] = rec
        elif isinstance(rec.get("$set"), dict):
            s = rec["$set"]
            msgs = s.get("messages")
            if isinstance(msgs, list):
                for m in msgs:
                    if isinstance(m, dict) and isinstance(m.get("id"), str):
                        messages[m["id"]] = m
            if isinstance(s.get("sessionId"), str):
                session_id = s["sessionId"]
        elif isinstance(rec.get("sessionId"), str) and isinstance(rec.get("projectHash"), str):
            session_id = rec["sessionId"]
            msgs = rec.get("messages")
            if isinstance(msgs, list):
                for m in msgs:
                    if isinstance(m, dict) and isinstance(m.get("id"), str):
                        messages[m["id"]] = m
    return list(messages.values()), session_id


def _records_from_messages(messages: list, session_id: str, cwd: str | None) -> list[UsageRecord]:
    """재구성된 messages[]를 gemini 메시지별 UsageRecord로 정규화(.json/.jsonl 공용)."""
    summary = _extract_first_prompt(messages)

    turns_by_day: dict[str, int] = {}
    for m in messages:
        if isinstance(m, dict) and m.get("type") == "user":
            day = kst_day(m.get("timestamp")) or ""
            turns_by_day[day] = turns_by_day.get(day, 0) + 1
    user_turns = sum(turns_by_day.values())

    records: list[UsageRecord] = []
    first = True
    for m in messages:
        if not isinstance(m, dict) or m.get("type") != "gemini":
            continue
        tokens = m.get("tokens")
        if not isinstance(tokens, dict):
            continue
        inp = _int(tokens.get("input"))
        cached = _int(tokens.get("cached"))
        out = _int(tokens.get("output"))
        thoughts = _int(tokens.get("thoughts"))
        records.append(UsageRecord(
            provider="gemini",
            session_id=session_id,
            cwd=cwd,
            ts=m.get("timestamp"),
            model=m.get("model"),
            input_tokens=max(inp - cached, 0),
            output_tokens=out + thoughts,
            cache_creation=0,
            cache_read=cached,
            message_id=m.get("id"),
            # 세션 메타는 첫 레코드에만 실어도 sessions UPSERT가 COALESCE로 보존.
            summary=summary if first else None,
            user_turns=user_turns if first else None,
            user_turns_by_day=turns_by_day if first else None,
        ))
        first = False
    return records


def parse_session_file(path: str) -> list[UsageRecord]:
    """세션 파일 1개 → gemini 메시지별 UsageRecord 리스트. 손상/토큰없음이면 [].

    옛 `.json`(단일 문서 {messages:[…]})과 새 `.jsonl`(append-only 뮤테이션 로그)을
    모두 지원한다 — 후자는 _collect_jsonl_messages로 소비 이벤트를 모은 뒤 공용 경로로 흘린다.
    """
    p = Path(path)
    if p.suffix == ".jsonl":
        messages, sid = _collect_jsonl_messages(p)
        session_id = sid or p.stem
    else:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(doc, dict):
            return []
        messages = doc.get("messages")
        if not isinstance(messages, list):
            return []
        session_id = doc.get("sessionId") or p.stem

    return _records_from_messages(messages, session_id, _project_root(p))


def discover_sessions(root: str | Path = GEMINI_ROOT) -> list[Path]:
    """root 아래 세션 파일 나열(정렬). 옛 `.json`과 새 `.jsonl`(v0.50~) 둘 다.

    - `*/chats/*.json` — 옛 최상위 세션.
    - `*/chats/**/*.jsonl` — 새 최상위 세션 + subagent 중첩(`chats/<parentSessionId>/*.jsonl`).
      `**`는 0개 이상 하위 디렉터리를 매치하므로 최상위와 중첩을 한 패턴이 함께 잡는다.
    glob `*.json`은 `.jsonl`을 매치하지 않으므로 둘을 각각 모아 합친다(중복 없음).
    """
    root = Path(root).expanduser()
    if not root.exists():
        return []
    return sorted([*root.glob("*/chats/*.json"), *root.glob("*/chats/**/*.jsonl")])


def ingest_gemini(conn, root: str | Path = GEMINI_ROOT, pricing: dict | None = None) -> int:
    """모든 세션 .json을 파싱·적재. 처리 세션 수 반환.

    .json은 재작성되는 단일 문서라 파일 통째 재파싱(dedup_key=gemini:<msg.id>로 멱등).
    """
    from tokenomy.db import ingest_records

    if pricing is None:
        from tokenomy.pricing import load_pricing
        pricing = load_pricing()

    n = 0
    for f in discover_sessions(root):
        recs = parse_session_file(str(f))
        if recs:
            ingest_records(conn, recs, pricing)
            n += 1
    conn.commit()
    return n


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
