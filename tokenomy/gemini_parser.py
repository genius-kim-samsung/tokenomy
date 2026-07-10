"""Gemini CLI 세션 로그(.json) 파서 — 플러그인.

Gemini CLI는 완료된 세션을 단일 JSON 문서로 저장한다(Claude/Codex와 또 다르다):
- 위치: ~/.gemini/tmp/<프로젝트>/chats/session-<ts>-<id>.json (세션당 1파일)
- 최상위: {sessionId, projectHash, startTime, lastUpdated, messages[], kind}
- messages[]: 각 {id(uuid), timestamp, type("user"|"gemini"), content, ...}.
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

    session_path = tmp/<프로젝트>/chats/session-*.json → 조부모가 <프로젝트> 디렉터리.
    .project_root가 없으면(구 세션·해시 디렉터리) 디렉터리명으로 폴백.
    """
    project_dir = session_path.parent.parent
    marker = project_dir / ".project_root"
    try:
        root = marker.read_text(encoding="utf-8").strip()
        if root:
            return root
    except OSError:
        pass
    return project_dir.name


def parse_session_file(path: str) -> list[UsageRecord]:
    """세션 .json 1개 → gemini 메시지별 UsageRecord 리스트. 손상/토큰없음이면 []."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(doc, dict):
        return []
    messages = doc.get("messages")
    if not isinstance(messages, list):
        return []

    session_id = doc.get("sessionId") or Path(path).stem
    cwd = _project_root(Path(path))
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


def discover_sessions(root: str | Path = GEMINI_ROOT) -> list[Path]:
    """root 아래 완료 세션 .json 나열(정렬). 라이브 .jsonl은 제외."""
    root = Path(root).expanduser()
    if not root.exists():
        return []
    return sorted(root.glob("*/chats/*.json"))


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
