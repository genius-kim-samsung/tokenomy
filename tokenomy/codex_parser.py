"""Codex CLI rollout(JSONL) 파서 — 플러그인.

Codex는 Claude와 구조가 다르다:
- 위치: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (세션당 1파일)
- 각 줄: {timestamp, type, payload}
- 토큰: event_msg/token_count 의 payload.info.total_token_usage = **누적**
  (마지막 token_count가 세션 총량 = state_5.sqlite threads.tokens_used 와 일치)
- 메타: session_meta(id/cwd/timestamp), turn_context(model)

세션당 1개의 UsageRecord로 정규화 → Claude와 동일한 db/집계/대시보드 재사용.

매핑:
  fresh input = input_tokens - cached_input_tokens
  cache_read  = cached_input_tokens
  output      = output_tokens (reasoning_output_tokens 포함)
  cache_write = 0  (Codex는 캐시 쓰기 구분 없음)
캐시 효율과 별개로, 첫 사용자 프롬프트를 120자 발췌해 summary(작업요약)로 싣는다.
"""
from __future__ import annotations

import json
from pathlib import Path

from tokenomy.parser import UsageRecord, kst_day

CODEX_ROOT = Path.home() / ".codex" / "sessions"


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
            elif fallback is None and o.get("type") == "response_item" and p.get("role") == "user":
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


def parse_rollout(path: str) -> UsageRecord | None:
    """rollout 파일 1개 → 세션 총량 UsageRecord. token_count 없으면 None."""
    session_id = cwd = ts = model = None
    last_total: dict | None = None
    turns_by_day: dict[str, int] = {}

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
            if _is_codex_user_msg(o):
                day = kst_day(o.get("timestamp")) or ""
                turns_by_day[day] = turns_by_day.get(day, 0) + 1

            t = o.get("type")
            payload = o.get("payload")
            payload = payload if isinstance(payload, dict) else {}

            if t == "session_meta":
                session_id = payload.get("id") or session_id
                cwd = payload.get("cwd") or cwd
                ts = payload.get("timestamp") or ts
            elif t == "turn_context":
                if payload.get("model"):
                    model = payload.get("model")
            elif payload.get("type") == "token_count":
                info = payload.get("info") or {}
                total = info.get("total_token_usage")
                if isinstance(total, dict):
                    last_total = total

    if last_total is None:
        return None
    if not session_id:
        session_id = Path(path).stem

    input_t = int(last_total.get("input_tokens") or 0)
    cached = int(last_total.get("cached_input_tokens") or 0)
    fresh = max(input_t - cached, 0)

    return UsageRecord(
        provider="codex",
        session_id=session_id,
        cwd=cwd,
        ts=ts,
        model=model,
        input_tokens=fresh,
        output_tokens=int(last_total.get("output_tokens") or 0),
        cache_creation=0,
        cache_read=cached,
        message_id=session_id,  # 세션당 1레코드 → dedup_key = session_id
        summary=_extract_first_prompt(path),
        user_turns=sum(turns_by_day.values()),
        user_turns_by_day=turns_by_day,
    )


def discover_rollouts(root: str | Path = CODEX_ROOT) -> list[Path]:
    root = Path(root).expanduser()
    if not root.exists():
        return []
    return sorted(root.rglob("rollout-*.jsonl"))


def ingest_codex(conn, root: str | Path = CODEX_ROOT, pricing: dict | None = None) -> int:
    """모든 rollout을 파싱·적재. 세션 수 반환.

    누적값이라 진행 중 세션은 다시 읽어 갱신(dedup_key=session_id로 REPLACE).
    rollout 수가 적어 전체 재파싱해도 충분(필요 시 mtime 스킵으로 최적화).
    """
    from tokenomy.db import ingest_records

    if pricing is None:
        from tokenomy.pricing import load_pricing
        pricing = load_pricing()

    n = 0
    for f in discover_rollouts(root):
        rec = parse_rollout(str(f))
        if rec is not None:
            ingest_records(conn, [rec], pricing)
            n += 1
    conn.commit()
    return n
