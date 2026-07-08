"""절약 카탈로그 로더 + 적용 상태 감지 + 전이 기록(ADR 0026)."""
from __future__ import annotations

import json

from tokenomy import savers
from tokenomy.db import connect


# ─── 가짜 홈 픽스처(감지 함수용) ──────────────────────────────────────────────

def _claude_home(tmp_path, *, active_marker=False, enabled=None, hooks=None, settings=True):
    """~/.claude 흔적을 가진 가짜 홈을 만든다. 이 기기 실측(cavemankorean)을 재현.

    hooks={"SessionStart": [{"hooks": [{"command": "…caveman…"}]}]} → settings.json hooks 섹션
    (스크립트 설치 경로 신호 재현).
    """
    claude = tmp_path / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    if active_marker:
        (claude / ".caveman-active").write_text("full", encoding="utf-8")
    if settings:
        body = {}
        if enabled is not None:
            body["enabledPlugins"] = enabled
        if hooks is not None:
            body["hooks"] = hooks
        (claude / "settings.json").write_text(json.dumps(body), encoding="utf-8")
    return tmp_path


def _codex_home(tmp_path, *, plugins=None, config=True):
    """~/.codex 흔적을 가진 가짜 홈. 이 기기 실측(config.toml의 [plugins."…"] enabled)을 재현.

    plugins={"caveman@caveman-repo": True} → config.toml에 해당 플러그인 섹션(enabled=true).
    config=False면 config.toml 자체를 안 만든다(설정 부재).
    """
    codex = tmp_path / ".codex"
    codex.mkdir(parents=True, exist_ok=True)
    if config:
        lines = []
        for key, val in (plugins or {}).items():
            lines.append(f'[plugins."{key}"]')
            lines.append(f"enabled = {'true' if val else 'false'}")
        (codex / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


# ─── Layer 1: 카탈로그 로더/스키마 ─────────────────────────────────────────────

def test_load_catalog_returns_entries_with_required_fields():
    entries = savers.load_saver_catalog()
    assert entries, "번들 카탈로그가 비어 있으면 안 됨"
    for e in entries:
        assert e["id"]
        assert e["type"] in ("installable", "advisory")
        assert e["name"]
        assert e["summary"]
        assert isinstance(e["providers"], list) and e["providers"]


def test_catalog_has_cavemankorean_installable_entry():
    by_id = {e["id"]: e for e in savers.load_saver_catalog()}
    caveman = by_id["cavemankorean"]
    assert caveman["type"] == "installable"
    assert "claude" in caveman["providers"]
    # 설치 스텝은 담지 않는다 — 저장소 링크로 안내(유지보수 부담 회피).
    assert "install" not in caveman
    assert caveman["repo_url"], "저장소 링크가 있어야 함"
    # 주장 절감률(제작자 주장) 텍스트
    assert caveman["claimed_saving"]


def test_catalog_cavemankorean_supports_codex():
    # ADR 0026 결정③: provider 추가로 흡수. Codex도 Caveman 플러그인 감지 대상.
    caveman = {e["id"]: e for e in savers.load_saver_catalog()}["cavemankorean"]
    assert "codex" in caveman["providers"]


def test_load_catalog_missing_file_returns_empty(tmp_path):
    assert savers.load_saver_catalog(tmp_path / "nope.json") == []


def test_load_catalog_broken_json_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    assert savers.load_saver_catalog(p) == []


def test_load_catalog_skips_entries_missing_id_or_type(tmp_path):
    p = tmp_path / "cat.json"
    p.write_text(json.dumps({"savers": [
        {"id": "ok", "type": "advisory", "name": "n", "summary": "s", "providers": ["claude"]},
        {"type": "advisory", "name": "no-id"},          # id 없음 → 스킵
        {"id": "bad-type", "type": "nonsense", "name": "x", "summary": "y", "providers": ["claude"]},
    ]}), encoding="utf-8")
    ids = [e["id"] for e in savers.load_saver_catalog(p)]
    assert ids == ["ok"]


# ─── Layer 2: caveman/claude 감지(파일 읽기만·3상태) ──────────────────────────

def test_detect_caveman_marker_only_is_not_applied(tmp_path):
    # stale `.caveman-active` 마커만 남고 설치 근거 없음(마켓플레이스 언인스톨 잔재) → 미적용.
    # 마커는 설치가 아니라 모드 상태 플래그일 뿐이라 영구 오탐을 냈다(ADR 0026 개정 2026-07-08).
    home = _claude_home(tmp_path, active_marker=True, enabled={"other@x": True})
    assert savers._detect_caveman_claude(home) == savers.NOT_APPLIED


def test_detect_caveman_applied_via_enabled_plugin(tmp_path):
    # 이 기기 실측: settings.json enabledPlugins에 "caveman@caveman": true
    home = _claude_home(tmp_path, enabled={"caveman@caveman": True, "other@x": True})
    assert savers._detect_caveman_claude(home) == savers.APPLIED


def test_detect_caveman_applied_via_hooks(tmp_path):
    # 스크립트 설치 경로(install.ps1): settings.json hooks에 caveman command 등록 →
    # enabledPlugins가 없어도 적용됨.
    home = _claude_home(tmp_path, hooks={
        "SessionStart": [{"hooks": [
            {"type": "command", "command": "node ~/.claude/plugins/caveman/caveman-mode-tracker.js"},
        ]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo hi"}]}],
    })
    assert savers._detect_caveman_claude(home) == savers.APPLIED


def test_detect_caveman_unrelated_hooks_is_not_applied(tmp_path):
    # hooks에 caveman 참조가 없으면(다른 훅만) 미적용 — hooks 존재 자체가 신호가 아니다.
    home = _claude_home(tmp_path, hooks={
        "PreToolUse": [{"matcher": "Skill", "hooks": [
            {"type": "command", "command": "npx -y ccstatusline@latest --hook"},
        ]}],
    })
    assert savers._detect_caveman_claude(home) == savers.NOT_APPLIED


def test_detect_caveman_malformed_hooks_does_not_crash(tmp_path):
    # 유효 JSON이지만 hooks 구조가 깨진 경우(내부 hooks가 null/비-리스트) → 크래시 없이 미적용.
    # 감지는 읽어서만·malformed면 폴백(ADR 0026 결정④, _read_json과 같은 관용).
    home = _claude_home(tmp_path, hooks={
        "SessionStart": [{"hooks": None}],   # null → for-loop 대상 아님
        "UserPromptSubmit": 42,              # 비-리스트 그룹
        "PreToolUse": [{"hooks": "caveman"}],  # 문자열(dict 아님) → command 없음
    })
    assert savers._detect_caveman_claude(home) == savers.NOT_APPLIED


def test_detect_caveman_not_applied_when_claude_present_but_no_signal(tmp_path):
    home = _claude_home(tmp_path, enabled={"other@x": True})
    assert savers._detect_caveman_claude(home) == savers.NOT_APPLIED


def test_detect_caveman_disabled_plugin_is_not_applied(tmp_path):
    home = _claude_home(tmp_path, enabled={"caveman@caveman": False})
    assert savers._detect_caveman_claude(home) == savers.NOT_APPLIED


def test_detect_caveman_unknown_when_no_claude_dir(tmp_path):
    # ~/.claude 부재 = 판정 근거 없음 → 거짓 "미적용" 대신 "감지 불가"
    assert savers._detect_caveman_claude(tmp_path) == savers.UNKNOWN


# ─── Layer 2: caveman/codex 감지(config.toml TOML·3상태) ──────────────────────

def test_detect_caveman_codex_applied_via_enabled_plugin(tmp_path):
    # 이 기기 실측: config.toml [plugins."caveman@caveman-repo"] enabled=true
    home = _codex_home(tmp_path, plugins={"caveman@caveman-repo": True, "browser@openai-bundled": True})
    assert savers._detect_caveman_codex(home) == savers.APPLIED


def test_detect_caveman_codex_not_applied_when_codex_present_but_no_signal(tmp_path):
    home = _codex_home(tmp_path, plugins={"browser@openai-bundled": True})
    assert savers._detect_caveman_codex(home) == savers.NOT_APPLIED


def test_detect_caveman_codex_disabled_plugin_is_not_applied(tmp_path):
    home = _codex_home(tmp_path, plugins={"caveman@caveman-repo": False})
    assert savers._detect_caveman_codex(home) == savers.NOT_APPLIED


def test_detect_caveman_codex_unknown_when_no_codex_dir(tmp_path):
    # ~/.codex 부재 = 판정 근거 없음 → 거짓 "미적용" 대신 "감지 불가"
    assert savers._detect_caveman_codex(tmp_path) == savers.UNKNOWN


def test_detect_states_covers_registry(tmp_path):
    home = _claude_home(tmp_path, active_marker=True)
    triples = savers.detect_states(home)
    ids = {sid for sid, _prov, _state in triples}
    assert "cavemankorean" in ids
    for _sid, _prov, state in triples:
        assert state in (savers.APPLIED, savers.NOT_APPLIED, savers.UNKNOWN)


def test_detect_states_covers_codex_provider(tmp_path):
    # 레지스트리가 cavemankorean×codex 감지를 포함한다
    home = _codex_home(tmp_path, plugins={"caveman@caveman-repo": True})
    pairs = {(sid, prov): state for sid, prov, state in savers.detect_states(home)}
    assert pairs[("cavemankorean", "codex")] == savers.APPLIED


# ─── Layer 2: 전이 기록(상태 변화 시각만 DB) ─────────────────────────────────

def test_refresh_records_first_observation(tmp_path):
    conn = connect(":memory:")
    home = _claude_home(tmp_path, enabled={"caveman@caveman": True})
    savers.refresh_saver_states(conn, "2026-06-20T12:00:00+09:00", home=home)
    latest = savers.db.latest_saver_states(conn)
    assert latest[("cavemankorean", "claude")][0] == savers.APPLIED


def test_refresh_records_transition_only_on_change(tmp_path):
    conn = connect(":memory:")
    applied = _claude_home(tmp_path / "a", enabled={"caveman@caveman": True})
    savers.refresh_saver_states(conn, "2026-06-20T12:00:00+09:00", home=applied)
    # 같은 상태 재감지 — 새 행 없음. cavemankorean은 claude+codex 두 provider를 감지하므로
    # provider='claude'로 좁혀 "같은 상태 무기록"을 검증한다(codex는 .codex 부재로 UNKNOWN).
    savers.refresh_saver_states(conn, "2026-06-20T13:00:00+09:00", home=applied)
    n1 = conn.execute(
        "SELECT COUNT(*) FROM saver_state_transitions WHERE saver_id='cavemankorean' AND provider='claude'"
    ).fetchone()[0]
    assert n1 == 1
    # 상태 바뀜(applied → not_applied) — 전이 1행 추가
    off = _claude_home(tmp_path / "b", enabled={"caveman@caveman": False})
    savers.refresh_saver_states(conn, "2026-06-20T14:00:00+09:00", home=off)
    rows = conn.execute(
        "SELECT state, changed_at FROM saver_state_transitions "
        "WHERE saver_id='cavemankorean' AND provider='claude' ORDER BY id"
    ).fetchall()
    assert [r[0] for r in rows] == [savers.APPLIED, savers.NOT_APPLIED]
    assert rows[-1][1] == "2026-06-20T14:00:00+09:00"


# ─── Layer 3: 뷰 조립(활성 AI 게이트·적용 상태·설치 스텝) ─────────────────────

from datetime import datetime
from tokenomy.clock import KST
from tokenomy.web import views


def _ctx(conn, tracked, tmp_path, **home_kw):
    home = _claude_home(tmp_path, **home_kw)
    cfg = {"tracked_providers": tracked}
    now = datetime(2026, 6, 20, 12, 0, tzinfo=KST)
    return views.savers_context(conn, cfg, now, home=home)


def test_savers_context_gates_by_active_ai(tmp_path):
    conn = connect(":memory:")
    # codex만 활성 → claude 전용 엔트리(compact-habit)는 숨김,
    # 멀티 provider 엔트리(cavemankorean=claude+codex)는 codex만 활성이어도 노출.
    ctx = _ctx(conn, ["codex"], tmp_path, active_marker=True)
    ids = {e["id"] for e in ctx["entries"]}
    assert "compact-habit" not in ids
    assert "cavemankorean" in ids
    # claude 활성 → claude 전용도 노출
    ctx2 = _ctx(conn, ["claude"], tmp_path, active_marker=True)
    ids2 = {e["id"] for e in ctx2["entries"]}
    assert "compact-habit" in ids2
    assert "cavemankorean" in ids2


def _badges(row) -> dict:
    """설치형 행의 state_badges를 {provider: badge}로. 테스트 편의."""
    return {b["provider"]: b for b in row["state_badges"]}


def test_savers_context_per_provider_badges_mixed_states(tmp_path):
    # claude 미적용 + codex 적용, 둘 다 활성 → provider별 배지가 각자 상태를 가진다
    # (단일 배지로 뭉개지 않음). 감지는 provider별이었으나 표시가 하나였던 회귀 가드.
    conn = connect(":memory:")
    _claude_home(tmp_path)                                         # .claude 존재, 신호 없음 → 미적용
    home = _codex_home(tmp_path, plugins={"caveman@caveman-repo": True})  # codex 적용
    cfg = {"tracked_providers": ["claude", "codex"]}
    now = datetime(2026, 6, 20, 12, 0, tzinfo=KST)
    ctx = views.savers_context(conn, cfg, now, home=home)
    row = next(e for e in ctx["entries"] if e["id"] == "cavemankorean")
    b = _badges(row)
    assert b["claude"]["state"] == savers.NOT_APPLIED
    assert b["claude"]["state_label"] == "미적용"
    assert b["claude"]["provider_label"] == "Claude"
    assert b["codex"]["state"] == savers.APPLIED
    assert b["codex"]["state_label"] == "적용됨"
    assert b["codex"]["provider_label"] == "Codex"


def test_savers_context_codex_applied_state(tmp_path):
    # codex 활성 + config.toml에 caveman enabled → 적용됨(감지 배지)
    conn = connect(":memory:")
    home = _codex_home(tmp_path, plugins={"caveman@caveman-repo": True})
    cfg = {"tracked_providers": ["codex"]}
    now = datetime(2026, 6, 20, 12, 0, tzinfo=KST)
    ctx = views.savers_context(conn, cfg, now, home=home)
    row = next(e for e in ctx["entries"] if e["id"] == "cavemankorean")
    b = _badges(row)["codex"]
    assert b["state"] == savers.APPLIED
    assert b["state_label"] == "적용됨"


def test_savers_context_applied_state_no_install_only_repo(tmp_path):
    conn = connect(":memory:")
    ctx = _ctx(conn, ["claude"], tmp_path, enabled={"caveman@caveman": True})
    row = next(e for e in ctx["entries"] if e["id"] == "cavemankorean")
    b = _badges(row)["claude"]
    assert b["state"] == savers.APPLIED
    assert b["state_label"] == "적용됨"
    assert row["claimed_saving"]
    # 설치 스텝은 노출하지 않고 저장소 링크만 안내한다
    assert "install" not in row
    assert row["repo_url"]


def test_savers_context_not_applied_state(tmp_path):
    conn = connect(":memory:")
    ctx = _ctx(conn, ["claude"], tmp_path, enabled={"caveman@caveman": False})
    row = next(e for e in ctx["entries"] if e["id"] == "cavemankorean")
    b = _badges(row)["claude"]
    assert b["state"] == savers.NOT_APPLIED
    assert b["state_label"] == "미적용"


def test_savers_context_advisory_has_no_state(tmp_path):
    conn = connect(":memory:")
    ctx = _ctx(conn, ["claude"], tmp_path, active_marker=True)
    advisory = [e for e in ctx["entries"] if e["type"] == "advisory"]
    assert advisory, "조언형 엔트리가 있어야 함"
    for e in advisory:
        assert e["state_badges"] is None      # 조언형은 적용 상태 없음


def test_savers_context_has_suggest_url(tmp_path):
    # 제안 접수는 사내 GHE Issues — 사용자 전원이 사내망 임직원(ADR 0026 개정)
    conn = connect(":memory:")
    ctx = _ctx(conn, ["claude"], tmp_path, active_marker=True)
    assert "github.sec.samsung.net/genius-kim/tokenomy/issues/new" in ctx["suggest_url"]


def test_record_transition_conditional_no_dup_same_state(tmp_path):
    # 동시 로드 레이스 방어(codex P1): record는 최신 상태와 같으면 append하지 않는다(원자적).
    from tokenomy import db as _db
    conn = connect(":memory:")
    _db.record_saver_transition(conn, "s", "claude", savers.APPLIED, "t1")
    _db.record_saver_transition(conn, "s", "claude", savers.APPLIED, "t2")   # 같은 최신 → no-op
    rows = conn.execute(
        "SELECT state FROM saver_state_transitions WHERE saver_id='s' ORDER BY id"
    ).fetchall()
    assert [r[0] for r in rows] == [savers.APPLIED]
    _db.record_saver_transition(conn, "s", "claude", savers.NOT_APPLIED, "t3")  # 변화 → append
    _db.record_saver_transition(conn, "s", "claude", savers.APPLIED, "t4")      # 되돌림 → append
    states = [r[0] for r in conn.execute(
        "SELECT state FROM saver_state_transitions WHERE saver_id='s' ORDER BY id"
    ).fetchall()]
    assert states == [savers.APPLIED, savers.NOT_APPLIED, savers.APPLIED]
