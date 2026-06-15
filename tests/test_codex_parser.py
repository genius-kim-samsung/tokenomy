import json

from tokenomy.codex_parser import parse_rollout
from tokenomy.pricing import compute_cost


def _write_rollout(tmp_path, name="rollout-x.jsonl"):
    f = tmp_path / name
    lines = [
        {"type": "session_meta", "payload": {"id": "sess-x", "cwd": "/proj", "timestamp": "2026-06-11T12:50:14Z"}},
        {"type": "turn_context", "payload": {"model": "gpt-5.5", "cwd": "/proj"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 10, "total_tokens": 110}}}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 200, "cached_input_tokens": 80, "output_tokens": 20, "total_tokens": 220}}}},
    ]
    f.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    return f


def test_parse_rollout_uses_last_cumulative(tmp_path):
    rec = parse_rollout(str(_write_rollout(tmp_path)))
    assert rec is not None
    assert rec.provider == "codex"
    assert rec.session_id == "sess-x"
    assert rec.model == "gpt-5.5"
    assert rec.cwd == "/proj"
    assert rec.input_tokens == 120   # last cumulative: 200 input - 80 cached
    assert rec.cache_read == 80
    assert rec.output_tokens == 20
    assert rec.cache_creation == 0
    assert rec.message_id == "sess-x"


def test_codex_record_is_priced_by_gpt5_rule(tmp_path):
    rec = parse_rollout(str(_write_rollout(tmp_path)))
    pricing = {"match": [{"contains": "gpt-5", "provider": "codex",
                          "input": 1.25, "output": 10.0, "cache_write": 0.0, "cache_read": 0.125}]}
    cost = compute_cost(rec, pricing)
    assert cost.priced is True
    assert cost.provider == "codex"
    # 120*1.25 + 20*10 + 80*0.125 = 150 + 200 + 10 = 360 (per million)
    assert cost.cost_usd == round(360 / 1_000_000, 6)


def test_no_token_count_returns_none(tmp_path):
    f = tmp_path / "rollout-empty.jsonl"
    f.write_text(json.dumps({"type": "session_meta", "payload": {"id": "s"}}), encoding="utf-8")
    assert parse_rollout(str(f)) is None


def test_malformed_lines_skipped(tmp_path):
    f = tmp_path / "rollout-bad.jsonl"
    f.write_text(
        "{garbage\n"
        + json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 50, "cached_input_tokens": 0, "output_tokens": 5}}}}) + "\n",
        encoding="utf-8",
    )
    rec = parse_rollout(str(f))
    assert rec is not None
    assert rec.input_tokens == 50


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
    assert parse_rollout(str(_write_rollout(tmp_path))).summary is None


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


def test_parse_rollout_user_turns_by_day(tmp_path):
    f = tmp_path / "rollout-byday.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"id": "s-bd", "cwd": "/p", "timestamp": "2026-06-11T01:00:00Z"}},
        {"type": "event_msg", "timestamp": "2026-06-11T01:00:00Z", "payload": {"type": "user_message", "message": "q1"}},
        {"type": "event_msg", "timestamp": "2026-06-11T16:00:00Z", "payload": {"type": "user_message", "message": "q2"}},
        {"type": "event_msg", "timestamp": "2026-06-11T17:00:00Z", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 10}}}},
    ]
    f.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    rec = parse_rollout(str(f))
    assert rec is not None
    assert rec.user_turns == 2
    assert rec.user_turns_by_day == {"2026-06-11": 1, "2026-06-12": 1}
