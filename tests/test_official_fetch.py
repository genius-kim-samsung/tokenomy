"""공식 사용량 라이브 취득 — 토큰 리더 + fetch_provider 전 경로(stub transport)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tokenomy.official_fetch import (
    AuthError, _read_claude_token, _read_codex_auth,
)


def test_read_claude_token_ok(tmp_path):
    p = tmp_path / "creds.json"
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-abc"}}), encoding="utf-8")
    assert _read_claude_token(p) == "sk-abc"


def test_read_claude_token_missing_file(tmp_path):
    with pytest.raises(AuthError):
        _read_claude_token(tmp_path / "nope.json")


def test_read_claude_token_bad_schema(tmp_path):
    p = tmp_path / "creds.json"
    p.write_text(json.dumps({"other": {}}), encoding="utf-8")
    with pytest.raises(AuthError):
        _read_claude_token(p)


def test_read_claude_token_empty(tmp_path):
    p = tmp_path / "creds.json"
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": ""}}), encoding="utf-8")
    with pytest.raises(AuthError):
        _read_claude_token(p)


def test_read_codex_auth_ok(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"tokens": {"access_token": "jwt-x", "account_id": "acc-1"}}),
                 encoding="utf-8")
    assert _read_codex_auth(p) == ("jwt-x", "acc-1")


def test_read_codex_auth_missing_account_id(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"tokens": {"access_token": "jwt-x"}}), encoding="utf-8")
    with pytest.raises(AuthError):
        _read_codex_auth(p)
