"""컨텍스트 문서(CLAUDE.md 등) 경로 참조 회귀 가드.

scripts/check_context_paths.py를 그대로 실행한다 — CI(context-check 워크플로)와
로컬 pytest가 같은 판정을 공유한다. stale 경로는 agent의 잘못된
path-following을 유발하므로 머지 전에 잡는다.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_context_paths.py"


def test_context_path_refs_resolve():
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert proc.returncode == 0, f"\n{proc.stdout}\n{proc.stderr}"
