import json

from tokenomy.gemini_parser import parse_session_file
from tokenomy.pricing import compute_cost


def _write_session(tmp_path, messages, session_id="sess-g", with_root=True,
                   name="session-2026-06-11T12-00-abc.json"):
    """tmp/<proj>/chats/<name>.json 세션 문서를 만든다. with_root면 .project_root도."""
    proj = tmp_path / "myproj"
    (proj / "chats").mkdir(parents=True, exist_ok=True)
    if with_root:
        (proj / ".project_root").write_text("c:\\projects\\myproj", encoding="utf-8")
    f = proj / "chats" / name
    doc = {"sessionId": session_id, "projectHash": "deadbeef",
           "startTime": "2026-06-11T12:00:00.000Z", "lastUpdated": "2026-06-11T12:05:00.000Z",
           "messages": messages, "kind": "main"}
    f.write_text(json.dumps(doc), encoding="utf-8")
    return f


def _gemini_msg(mid, tokens, model="gemini-3.1-pro-preview", ts="2026-06-11T12:01:00.000Z"):
    return {"id": mid, "timestamp": ts, "type": "gemini", "content": "ok",
            "model": model, "tokens": tokens}


def _user_msg(mid, text, ts="2026-06-11T12:00:00.000Z"):
    return {"id": mid, "timestamp": ts, "type": "user", "content": [{"text": text}]}


def test_token_mapping_and_total_invariant(tmp_path):
    f = _write_session(tmp_path, [
        _user_msg("u1", "첫 질문"),
        _gemini_msg("g1", {"input": 1000, "output": 50, "cached": 200,
                           "thoughts": 10, "tool": 0, "total": 1060}),
    ])
    recs = parse_session_file(str(f))
    assert len(recs) == 1
    r = recs[0]
    assert r.provider == "gemini"
    assert r.session_id == "sess-g"
    assert r.model == "gemini-3.1-pro-preview"
    assert r.input_tokens == 800      # fresh = input - cached
    assert r.cache_read == 200
    assert r.output_tokens == 60      # output + thoughts
    assert r.cache_creation == 0
    assert r.message_id == "g1"
    assert r.total_tokens == 1060     # = tokens.total (input+output+thoughts)


def test_project_root_used_as_cwd(tmp_path):
    f = _write_session(tmp_path, [
        _user_msg("u1", "q"),
        _gemini_msg("g1", {"input": 100, "output": 5, "cached": 0, "thoughts": 0, "tool": 0, "total": 105}),
    ])
    r = parse_session_file(str(f))[0]
    assert r.cwd == "c:\\projects\\myproj"


def test_cwd_falls_back_to_dir_name_when_no_marker(tmp_path):
    f = _write_session(tmp_path, [
        _gemini_msg("g1", {"input": 100, "output": 5, "cached": 0, "thoughts": 0, "tool": 0, "total": 105}),
    ], with_root=False)
    r = parse_session_file(str(f))[0]
    assert r.cwd == "myproj"


def test_summary_first_user_prompt_truncated(tmp_path):
    f = _write_session(tmp_path, [
        _user_msg("u1", "줄1\n줄2   여러   공백"),
        _gemini_msg("g1", {"input": 100, "output": 5, "cached": 0, "thoughts": 0, "tool": 0, "total": 105}),
    ])
    recs = parse_session_file(str(f))
    assert recs[0].summary == "줄1 줄2 여러 공백"

    f2 = _write_session(tmp_path, [
        _user_msg("u1", "가" * 200),
        _gemini_msg("g1", {"input": 100, "output": 5, "cached": 0, "thoughts": 0, "tool": 0, "total": 105}),
    ], session_id="sess-long", name="session-long.json")
    assert len(parse_session_file(str(f2))[0].summary) == 120


def test_user_turns_by_day_kst(tmp_path):
    # 2026-06-11T16:00Z = 2026-06-12 01:00 KST → 다음날 버킷
    f = _write_session(tmp_path, [
        _user_msg("u1", "q1", ts="2026-06-11T01:00:00.000Z"),
        _user_msg("u2", "q2", ts="2026-06-11T16:00:00.000Z"),
        _gemini_msg("g1", {"input": 100, "output": 5, "cached": 0, "thoughts": 0, "tool": 0, "total": 105},
                    ts="2026-06-11T16:01:00.000Z"),
    ])
    r = parse_session_file(str(f))[0]
    assert r.user_turns == 2
    assert r.user_turns_by_day == {"2026-06-11": 1, "2026-06-12": 1}


def test_multiple_gemini_messages_each_priced(tmp_path):
    f = _write_session(tmp_path, [
        _user_msg("u1", "q"),
        _gemini_msg("g1", {"input": 100, "output": 5, "cached": 0, "thoughts": 0, "tool": 0, "total": 105}),
        _gemini_msg("g2", {"input": 200, "output": 8, "cached": 50, "thoughts": 2, "tool": 0, "total": 210}),
    ])
    recs = parse_session_file(str(f))
    assert len(recs) == 2
    assert [r.message_id for r in recs] == ["g1", "g2"]
    # 세션 메타(summary/turns)는 첫 레코드에만
    assert recs[0].summary == "q" and recs[1].summary is None
    assert recs[0].user_turns == 1 and recs[1].user_turns is None


def test_user_message_has_no_record(tmp_path):
    f = _write_session(tmp_path, [_user_msg("u1", "q only")])
    assert parse_session_file(str(f)) == []


def test_corrupt_document_returns_empty(tmp_path):
    proj = tmp_path / "p"
    (proj / "chats").mkdir(parents=True)
    f = proj / "chats" / "session-bad.json"
    f.write_text("{not json", encoding="utf-8")
    assert parse_session_file(str(f)) == []


def test_gemini_message_without_tokens_skipped(tmp_path):
    f = _write_session(tmp_path, [
        _user_msg("u1", "q"),
        {"id": "g0", "timestamp": "2026-06-11T12:01:00.000Z", "type": "gemini", "content": "no tokens"},
        _gemini_msg("g1", {"input": 100, "output": 5, "cached": 0, "thoughts": 0, "tool": 0, "total": 105}),
    ])
    recs = parse_session_file(str(f))
    assert [r.message_id for r in recs] == ["g1"]


from tokenomy import domain, paths
from tokenomy.pricing import load_pricing, find_rate


def test_gemini_registered_in_providers():
    assert "gemini" in domain.PROVIDERS


def test_creds_present_gemini(tmp_path, monkeypatch):
    creds = tmp_path / "oauth_creds.json"
    monkeypatch.setattr(paths, "GEMINI_CREDS", creds, raising=False)
    assert paths.creds_present("gemini") is False
    creds.write_text("{}", encoding="utf-8")
    assert paths.creds_present("gemini") is True


def test_pricing_matches_gemini_tiers():
    pricing = load_pricing()
    assert find_rate("gemini-3.1-pro-preview", pricing)["provider"] == "gemini"
    assert find_rate("gemini-3.1-flash-preview", pricing)["provider"] == "gemini"
    # flash-lite는 flash보다 먼저 매칭돼야 한다(first-match)
    lite = find_rate("gemini-3.1-flash-lite-preview", pricing)
    flash = find_rate("gemini-3.1-flash-preview", pricing)
    assert lite["input"] < flash["input"]  # lite가 더 쌈


def test_gemini_record_is_priced(tmp_path):
    f = _write_session(tmp_path, [
        _gemini_msg("g1", {"input": 1000, "output": 50, "cached": 200,
                           "thoughts": 10, "tool": 0, "total": 1060}),
    ])
    r = parse_session_file(str(f))[0]
    cost = compute_cost(r, load_pricing())
    assert cost.priced is True
    assert cost.provider == "gemini"
    assert cost.cost_usd > 0
