"""토큰 절약 카탈로그 — 로더 + 적용 상태 감지 + 전이 기록(ADR 0026).

배포 카탈로그(config/saver_catalog.json)는 절약 수단의 큐레이션 목록이자 **신뢰 경계**다
(bucket_catalog.json·pricing.json과 같은 결 — 원격 취득 없음). 설치형 엔트리의 적용 상태는
도구별 감지 함수가 로컬 설정 파일을 **읽어서만**(subprocess 없이) 3상태로 판정하고, 상태가
바뀐 시각만 DB에 남긴다(읽은 내용 미적재 — 발췌선 불변). aggregate와 같은 계층(views가
위에서 호출), db·config·paths를 아래로 import.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

from tokenomy import db

# 적용 상태 3값(ADR 0026 결정④).
APPLIED = "applied"
NOT_APPLIED = "not_applied"
UNKNOWN = "unknown"

_VALID_TYPES = ("installable", "advisory")


def load_saver_catalog(path: str | Path | None = None) -> list[dict]:
    """배포 절약 카탈로그(config/saver_catalog.json)를 검증된 엔트리 리스트로 로드.

    pricing.json·bucket_catalog.json과 동형으로 `resource_path`로 번들(repo 커밋 + exe
    동봉 + 릴리스 배포). `{"savers": [...]}` 래퍼 또는 평면 리스트 모두 허용. 파일 없거나
    깨지면 빈 리스트(카탈로그 없음 = 안전 폴백). id/type/name/summary/providers를 못 채운
    엔트리는 조용히 스킵한다.
    """
    if path is None:
        from tokenomy.paths import resource_path
        path = resource_path("config/saver_catalog.json")
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    raw = data.get("savers", data) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    return [e for e in raw if _valid_entry(e)]


def _valid_entry(e: object) -> bool:
    return (
        isinstance(e, dict)
        and bool(e.get("id"))
        and e.get("type") in _VALID_TYPES
        and bool(e.get("name"))
        and bool(e.get("summary"))
        and isinstance(e.get("providers"), list)
        and bool(e.get("providers"))
    )


# ─── 적용 상태 감지(파일 읽기 한정·subprocess 금지, ADR 0026 결정④) ────────────
# 감지는 마커 기반으로 OS 중립하게 — 특정 .ps1 경로가 아니라 플러그인/settings 흔적으로
# 판정한다(cavemankorean 설치 산출물이 OS별로 다름). Path.home() 기준 상대 경로만 사용.

def _read_json(p: Path) -> dict:
    """JSON 파일을 dict로. 없거나 깨지면 빈 dict(감지는 흔적이 있으면 읽고, 없으면 없는 대로)."""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_toml(p: Path) -> dict:
    """TOML 파일을 dict로. 없거나 깨지면 빈 dict(_read_json과 대칭 — Codex config.toml용)."""
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def _detect_caveman_claude(home: Path) -> str:
    """Caveman(cavemankorean) 플러그인의 Claude Code 적용 상태를 3상태로.

    강한 신호(활성):
      - `~/.claude/.caveman-active` 존재(런타임 활성 마커 — 훅이 기록).
      - `~/.claude/settings.json`의 `enabledPlugins`에 "caveman" 포함 키가 truthy(활성 플러그인).
    `~/.claude`가 아예 없으면 판정 근거가 없어 UNKNOWN(거짓 미적용 방지). 설정은 읽히지만
    두 신호가 없으면 NOT_APPLIED. 읽은 내용은 버리고 상태만 반환한다.
    """
    claude = home / ".claude"
    if not claude.exists():
        return UNKNOWN
    if (claude / ".caveman-active").exists():
        return APPLIED
    enabled = _read_json(claude / "settings.json").get("enabledPlugins", {})
    if isinstance(enabled, dict):
        for key, val in enabled.items():
            if "caveman" in str(key).lower() and val:
                return APPLIED
    return NOT_APPLIED


def _detect_caveman_codex(home: Path) -> str:
    """Caveman 플러그인의 Codex 적용 상태를 3상태로.

    강한 신호(활성): `~/.codex/config.toml`의 `[plugins."caveman@…"]` 섹션이 truthy `enabled`.
    Codex 플러그인 시스템으로 설치하면(repo clone 후 `/plugins`에서 활성화) 활성 플래그가
    글로벌 config.toml에 남는다(마켓플레이스는 workspace-local이라 남지 않아도 무방).
    `~/.codex`가 아예 없으면 UNKNOWN(거짓 미적용 방지). config는 읽히지만 신호 없으면
    NOT_APPLIED. Claude 감지(settings.json enabledPlugins)와 대칭 — 포맷만 JSON→TOML.
    """
    codex = home / ".codex"
    if not codex.exists():
        return UNKNOWN
    plugins = _read_toml(codex / "config.toml").get("plugins", {})
    if isinstance(plugins, dict):
        for key, val in plugins.items():
            if "caveman" in str(key).lower() and isinstance(val, dict) and val.get("enabled"):
                return APPLIED
    return NOT_APPLIED


# 감지 레지스트리 — (saver_id, provider, 감지 함수). 설치형 엔트리 중 감지 함수를 가진 것만.
# 감지 함수 없는 설치형/조언형은 뷰에서 UNKNOWN/None으로 폴백(카탈로그가 정본, 여긴 코드 감지).
DETECTORS: list[tuple[str, str, object]] = [
    ("cavemankorean", "claude", _detect_caveman_claude),
    ("cavemankorean", "codex", _detect_caveman_codex),
]


def detect_states(home: Path | None = None) -> list[tuple[str, str, str]]:
    """레지스트리 전체를 감지해 `[(saver_id, provider, state), ...]` 반환."""
    home = home or Path.home()
    return [(sid, prov, fn(home)) for sid, prov, fn in DETECTORS]


def refresh_saver_states(conn, now_iso: str, home: Path | None = None) -> list[tuple[str, str, str]]:
    """감지를 돌리고 **상태가 바뀐 것만** 전이로 기록한다. 감지 결과 triples를 반환.

    대시보드 로드마다·"토큰 절약" 화면 진입 시 호출(ADR 0026 구현 합의③). 최초 관측은
    직전 상태가 없으므로 항상 기록(없음 → state). 실패는 조용히 흡수하지 않는다(호출부가 방어).
    """
    current = detect_states(home)
    latest = db.latest_saver_states(conn)
    for sid, prov, state in current:
        prev = latest.get((sid, prov))
        if prev is None or prev[0] != state:
            db.record_saver_transition(conn, sid, prov, state, now_iso)
    conn.commit()
    return current
