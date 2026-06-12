import json

from tokenomy.parser import UsageRecord, parse_file, parse_titles, parse_usage_line


def _assistant_line(**over):
    obj = {
        "type": "assistant",
        "message": {
            "model": over.get("model", "claude-opus-4-8"),
            "id": over.get("message_id", "msg-default"),
            "usage": {
                "input_tokens": over.get("input_tokens", 100),
                "output_tokens": over.get("output_tokens", 50),
                "cache_creation_input_tokens": over.get("cache_creation", 200),
                "cache_read_input_tokens": over.get("cache_read", 300),
                "server_tool_use": {
                    "web_search_requests": over.get("web_search", 1),
                    "web_fetch_requests": over.get("web_fetch", 2),
                },
            },
        },
        "timestamp": over.get("ts", "2026-06-11T10:00:00Z"),
        "sessionId": over.get("session_id", "sess-1"),
        "cwd": over.get("cwd", "/c/projects/foo"),
    }
    if "request_id" in over:
        obj["requestId"] = over["request_id"]
    if "is_sidechain" in over:
        obj["isSidechain"] = over["is_sidechain"]
    if "version" in over:
        obj["version"] = over["version"]
    if "attribution_skill" in over:
        obj["attributionSkill"] = over["attribution_skill"]
    if "git_branch" in over:
        obj["gitBranch"] = over["git_branch"]
    if "cache_creation_breakdown" in over:
        obj["message"]["usage"]["cache_creation"] = over["cache_creation_breakdown"]
    return json.dumps(obj)


def test_parse_assistant_line_extracts_usage():
    rec = parse_usage_line(_assistant_line())
    assert isinstance(rec, UsageRecord)
    assert rec.provider == "claude"
    assert rec.model == "claude-opus-4-8"
    assert rec.input_tokens == 100
    assert rec.output_tokens == 50
    assert rec.cache_creation == 200
    assert rec.cache_read == 300
    assert rec.web_search == 1
    assert rec.web_fetch == 2
    assert rec.session_id == "sess-1"
    assert rec.cwd == "/c/projects/foo"
    assert rec.total_tokens == 650


def test_user_line_returns_none():
    assert parse_usage_line(json.dumps({"type": "user", "message": {"content": "hi"}})) is None


def test_assistant_without_usage_returns_none():
    assert parse_usage_line(json.dumps({"type": "assistant", "message": {"model": "x"}})) is None


def test_malformed_json_returns_none():
    assert parse_usage_line("{not valid json") is None


def test_blank_line_returns_none():
    assert parse_usage_line("") is None
    assert parse_usage_line("   \n") is None


def test_missing_usage_fields_default_to_zero():
    line = json.dumps({"message": {"model": "claude-haiku-4-5", "usage": {"input_tokens": 5}}})
    rec = parse_usage_line(line)
    assert rec is not None
    assert rec.input_tokens == 5
    assert rec.output_tokens == 0
    assert rec.cache_creation == 0
    assert rec.cache_read == 0


def test_session_id_falls_back_to_filename(tmp_path):
    f = tmp_path / "abc-123.jsonl"
    line = json.dumps({"message": {"model": "claude-sonnet-4", "usage": {"input_tokens": 1}}})
    f.write_text(line + "\n", encoding="utf-8")
    records, _ = parse_file(str(f))
    assert records[0].session_id == "abc-123"


def test_parse_file_incremental_offset(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(_assistant_line(input_tokens=1) + "\n", encoding="utf-8")

    records, offset = parse_file(str(f), 0)
    assert len(records) == 1
    assert records[0].input_tokens == 1
    assert offset > 0

    # append a new line; re-parse from saved offset should only see the new one
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(_assistant_line(input_tokens=2) + "\n")

    records2, offset2 = parse_file(str(f), offset)
    assert len(records2) == 1
    assert records2[0].input_tokens == 2
    assert offset2 > offset


def test_extracts_message_id_request_id_and_sidechain():
    rec = parse_usage_line(
        _assistant_line(message_id="msg-1", request_id="req-1", is_sidechain=True)
    )
    assert rec.message_id == "msg-1"
    assert rec.request_id == "req-1"
    assert rec.is_sidechain is True


def test_sidechain_defaults_false_and_request_id_none():
    rec = parse_usage_line(_assistant_line())
    assert rec.request_id is None
    assert rec.is_sidechain is False


def test_extracts_cache_creation_breakdown():
    # usage.cache_creation = {5m, 1h} breakdown이 있으면 합을 총량으로, 1h는 별도 추적
    rec = parse_usage_line(
        _assistant_line(
            cache_creation_breakdown={
                "ephemeral_5m_input_tokens": 100,
                "ephemeral_1h_input_tokens": 200,
            }
        )
    )
    assert rec.cache_creation == 300
    assert rec.cache_creation_1h == 200


def test_cache_creation_flat_when_no_breakdown():
    # breakdown이 없으면 flat cache_creation_input_tokens를 그대로, 1h=0
    rec = parse_usage_line(_assistant_line(cache_creation=500))
    assert rec.cache_creation == 500
    assert rec.cache_creation_1h == 0


def test_rejects_non_semver_version():
    # version이 있으나 semver(x.y.z) 형태가 아니면 손상/미지원 스키마로 보고 거부
    assert parse_usage_line(_assistant_line(version="garbage")) is None
    assert parse_usage_line(_assistant_line(version="1.2")) is None


def test_accepts_valid_semver_version():
    assert parse_usage_line(_assistant_line(version="1.2.3")) is not None
    assert parse_usage_line(_assistant_line(version="2.0.14-beta")) is not None


def test_accepts_missing_version():
    # version 부재는 허용 (구 로그 호환)
    assert parse_usage_line(_assistant_line()) is not None


def test_rejects_empty_model_and_message_id():
    # 빈 문자열 필드는 손상 라인 → 거부 (None/부재는 허용)
    assert parse_usage_line(_assistant_line(model="")) is None
    assert parse_usage_line(_assistant_line(message_id="")) is None


def test_parse_file_skips_non_usage_lines(tmp_path):
    f = tmp_path / "mixed.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {"content": "hello"}}),
        _assistant_line(input_tokens=7),
        "{garbage",
        json.dumps({"type": "summary"}),
    ]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    records, _ = parse_file(str(f))
    assert len(records) == 1
    assert records[0].input_tokens == 7


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


def test_parse_titles_extracts_ai_title(tmp_path):
    # ai-title 라인의 aiTitle을 {session_id: 제목}으로 추출 (usage 없는 라인이라 parse_file은 무시함)
    f = tmp_path / "sess.jsonl"
    lines = [
        _assistant_line(session_id="s1"),
        json.dumps({"type": "ai-title", "aiTitle": "토큰 매니저 구현", "sessionId": "s1"}),
    ]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert parse_titles(str(f)) == {"s1": "토큰 매니저 구현"}


def test_parse_titles_keeps_last_per_session(tmp_path):
    # 같은 세션에 ai-title이 여러 번이면 마지막(최신)을 유지
    f = tmp_path / "s.jsonl"
    lines = [
        json.dumps({"type": "ai-title", "aiTitle": "초안 제목", "sessionId": "s1"}),
        json.dumps({"type": "ai-title", "aiTitle": "최종 제목", "sessionId": "s1"}),
    ]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert parse_titles(str(f))["s1"] == "최종 제목"


def test_parse_titles_empty_when_no_title(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(_assistant_line() + "\n", encoding="utf-8")
    assert parse_titles(str(f)) == {}


def test_parse_titles_skips_empty_title_and_missing_session(tmp_path):
    f = tmp_path / "s.jsonl"
    lines = [
        json.dumps({"type": "ai-title", "aiTitle": "", "sessionId": "s1"}),       # 빈 제목 무시
        json.dumps({"type": "ai-title", "aiTitle": "제목만", "sessionId": ""}),    # 세션 없음 무시
        json.dumps({"type": "ai-title", "aiTitle": "유효", "sessionId": "s2"}),
    ]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert parse_titles(str(f)) == {"s2": "유효"}
