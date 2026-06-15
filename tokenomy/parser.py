"""Claude Code transcript(JSONL) 파서.

각 줄이 JSON 객체인 세션 로그에서 토큰 usage 메타만 추출한다.
대화 원문(content)은 추출하지 않는다 (프라이버시 경계).

증분 파싱: 세션 파일은 append되므로 byte-offset을 추적해 신규 라인만 읽는다.
(mtime만으로는 '어디가 늘었는지' 알 수 없어 offset을 쓴다.)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# version 필드는 "x.y.z..."(semver) 형태여야 정상 로그로 본다. 그 외(손상/미지원 스키마)는 폐기.
_SEMVER_PREFIX = re.compile(r"^\d+\.\d+\.\d+")


@dataclass
class UsageRecord:
    provider: str
    session_id: str
    cwd: str | None
    ts: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    cache_creation: int
    cache_read: int
    web_search: int = 0
    web_fetch: int = 0
    message_id: str | None = None
    request_id: str | None = None
    is_sidechain: bool = False
    cache_creation_1h: int = 0  # cache_creation 중 1시간 캐시 분량(단가 = input × 2). 나머지는 5분 캐시.
    attribution_skill: str | None = None
    git_branch: str | None = None
    summary: str | None = None  # 세션 식별용 첫 프롬프트 발췌(Codex). Claude는 None(aiTitle 별도 경로).
    user_turns: int | None = None  # 세션 내 사용자 턴 수(Codex는 parse_rollout이 채움; Claude는 count_user_turns).
    user_turns_by_day: dict | None = None  # {KST날짜: 턴수}. Codex는 parse_rollout이 채움.

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation
            + self.cache_read
        )


def parse_usage_line(
    line: str, provider: str = "claude", source_path: str | None = None
) -> UsageRecord | None:
    """한 JSONL 라인에서 UsageRecord를 추출. usage가 없으면 None.

    usage 블록의 존재로 '비용이 발생한 라인'을 판단한다(type에 의존하지 않음).
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None

    message = obj.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None

    # 유효성 검사: 손상/미지원 스키마 라인을 집계 전에 폐기한다(ccusage is_valid_usage_entry).
    version = obj.get("version")
    if version is not None and not _SEMVER_PREFIX.match(str(version)):
        return None
    # 빈 문자열 필드는 손상 라인 신호. None(부재)은 허용한다.
    if message.get("model") == "" or message.get("id") == "":
        return None

    stu = usage.get("server_tool_use") or {}
    if not isinstance(stu, dict):
        stu = {}

    # 캐시 생성 토큰: cache_creation breakdown(5m/1h)이 있으면 그 합을 총량으로 쓰고
    # 1h 분량을 따로 추적한다(1h 캐시는 단가가 input의 2배). 없으면 flat 필드로 폴백.
    breakdown = usage.get("cache_creation")
    if isinstance(breakdown, dict):
        cache_1h = _int(breakdown.get("ephemeral_1h_input_tokens"))
        cache_creation = _int(breakdown.get("ephemeral_5m_input_tokens")) + cache_1h
    else:
        cache_creation = _int(usage.get("cache_creation_input_tokens"))
        cache_1h = 0

    session_id = obj.get("sessionId")
    if not session_id and source_path:
        session_id = Path(source_path).stem

    return UsageRecord(
        provider=provider,
        session_id=session_id or "unknown",
        cwd=obj.get("cwd"),
        ts=obj.get("timestamp"),
        model=message.get("model"),
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cache_creation=cache_creation,
        cache_read=_int(usage.get("cache_read_input_tokens")),
        web_search=_int(stu.get("web_search_requests")),
        web_fetch=_int(stu.get("web_fetch_requests")),
        message_id=message.get("id"),
        request_id=obj.get("requestId"),
        is_sidechain=bool(obj.get("isSidechain", False)),
        cache_creation_1h=cache_1h,
        attribution_skill=obj.get("attributionSkill"),
        git_branch=obj.get("gitBranch"),
    )


def parse_file(
    path: str, start_offset: int = 0, provider: str = "claude"
) -> tuple[list[UsageRecord], int]:
    """start_offset(byte) 이후 라인만 파싱. (records, end_offset) 반환.

    offset을 저장해두면 다음 호출 때 신규 라인만 읽는다.
    주의: 마지막 라인이 개행 없이 쓰이는 중이면 그 라인까지 읽을 수 있다.
    PoC 단계에서는 라인 단위 flush를 가정한다.
    """
    records: list[UsageRecord] = []
    with open(path, "rb") as f:
        f.seek(start_offset)
        for raw in f:
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            rec = parse_usage_line(line, provider=provider, source_path=path)
            if rec is not None:
                records.append(rec)
        end_offset = f.tell()
    return records, end_offset


_KST = timezone(timedelta(hours=9))


def kst_day(ts: str | None) -> str | None:
    """ISO 타임스탬프(UTC 가정)를 KST 날짜 문자열 'YYYY-MM-DD'로 변환.

    aggregate.parse_ts(ts).date().isoformat()와 동일 규칙 — 두 경로의 날짜 키가
    어긋나면 by_day_session 버킷과 session_day_turns가 안 맞으므로 반드시 동기화 유지.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_KST).date().isoformat()


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
    # type 또는 role 둘 중 하나가 "user"면 통과(한쪽 필드 누락 대비).
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


def count_user_turns_by_day(path: str) -> dict[str, dict[str, int]]:
    """세션 파일 전체를 풀스캔해 {session_id: {KST날짜: 사용자 턴 수}}를 반환한다.

    byte-offset 증분이 아닌 전체 스캔(parse_titles와 동일 이유 — 매번 정확한 총량).
    'user' 바이트가 없는 라인은 사용자 턴일 수 없어 건너뛴다(json.loads 회피).
    ts가 없는 턴은 '' 키로 묶는다(세션 총량엔 포함, 날짜 트리엔 미반영).
    """
    out: dict[str, dict[str, int]] = {}
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
            day = kst_day(obj.get("timestamp")) or ""
            days = out.setdefault(sid, {})
            days[day] = days.get(day, 0) + 1
    return out


def count_user_turns(path: str) -> dict[str, int]:
    """{session_id: 사용자 턴 총수}. count_user_turns_by_day의 날짜별 합."""
    return {sid: sum(days.values()) for sid, days in count_user_turns_by_day(path).items()}


def parse_titles(path: str) -> dict[str, str]:
    """ai-title 라인에서 {session_id: aiTitle}을 추출한다.

    aiTitle은 Claude Code가 세션마다 생성하는 한 줄 제목(원문 content가 아닌 메타).
    usage가 없는 라인이라 parse_file은 무시하므로 별도로 스캔한다.
    같은 세션에 여러 ai-title이 있으면 파일 뒤쪽(최신)이 앞쪽을 덮는다.
    제목은 세션 종료 시 갱신될 수 있어 byte-offset 증분이 아닌 전체 스캔으로 읽는다.
    """
    titles: dict[str, str] = {}
    with open(path, "rb") as f:
        for raw in f:
            if b'"ai-title"' not in raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                continue
            if not isinstance(obj, dict) or obj.get("type") != "ai-title":
                continue
            sid = obj.get("sessionId")
            title = obj.get("aiTitle")
            if sid and title:
                titles[sid] = title
    return titles


def discover_session_files(root: str | Path) -> list[Path]:
    """root 아래 모든 *.jsonl 세션 파일 (정렬)."""
    root = Path(root).expanduser()
    if not root.exists():
        return []
    return sorted(root.rglob("*.jsonl"))


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
