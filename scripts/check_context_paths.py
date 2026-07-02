"""컨텍스트 문서(CLAUDE.md·AGENTS.md·루트 README.md)의 파일 경로 참조 검증.

문서 속 경로가 실제 파일을 가리키지 않으면(리네임 후 미갱신·오타) agent가
잘못된 path-following을 한다 — stale reference는 없는 것보다 나쁘다.
판정 기준은 git 파일 목록(tracked + untracked non-ignored)이라 로컬·CI가
같은 결과를 낸다(gitignored 로컬 파일이 "존재"로 오판되는 것을 막는다).

사용:
    python scripts/check_context_paths.py    # exit 0=통과, 1=깨진 참조 존재
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parent.parent

# 문서에 언급되지만 저장소에 없는 게 정상인 경로(런타임 생성·사용자 로컬 파일).
# 추가할 때는 사유 주석 필수.
ALLOWLIST = {
    "data/runtime.json",            # launcher가 실행 중에만 쓰는 단일 인스턴스 락
    "config/tokenomy.config.json",  # 사용자 로컬 설정 — example만 추적
}

# 경로 참조 추출 — 확장자 alternation은 긴 것 우선(.json이 .js로 절단되는 오탐 방지),
# 앞뒤 lookaround로 `~/.claude/...` 같은 홈 경로 부분 매칭을 차단.
RE_PATH_REF = re.compile(
    r"(?<![A-Za-z0-9_/.])"
    r"((?:\./|[A-Za-z0-9_]+/)[A-Za-z0-9_./-]+\.(?:py|tsx|ts|jsx|json|js|md|sql|yaml|yml|toml|html|css|sh|go|rs|java|kt|rb|php))"
    r"(?![A-Za-z0-9])"
)


def _git_files() -> set[str]:
    """추적 파일 + 미추적·비ignore 파일(커밋 예정분) 목록."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True, text=True, encoding="utf-8", cwd=REPO, check=True,
    )
    return set(out.stdout.splitlines())


def _context_docs(files: set[str]) -> list[str]:
    """검증 대상 문서 — 모든 CLAUDE.md/AGENTS.md(도구 로컬 .claude/ 제외) + 루트 README.md."""
    docs = [
        f for f in files
        if PurePosixPath(f).name in ("CLAUDE.md", "AGENTS.md")
        and not f.startswith(".claude/")
    ]
    if "README.md" in files:
        docs.append("README.md")
    return sorted(docs)


def check() -> list[str]:
    files = _git_files()
    problems: list[str] = []
    for doc in _context_docs(files):
        text = (REPO / doc).read_text(encoding="utf-8", errors="ignore")
        base = PurePosixPath(doc).parent
        for ref in sorted(set(RE_PATH_REF.findall(text))):
            candidates = {str(PurePosixPath(ref)), str(base / ref)}
            if candidates & files or ref in ALLOWLIST:
                continue
            problems.append(f"{doc}: {ref}")
    return problems


def main() -> int:
    problems = check()
    if problems:
        print("깨진 컨텍스트 경로 참조:")
        for p in problems:
            print(f"  - {p}")
        print("경로를 실제 파일로 고치거나, 런타임 생성 파일이면 ALLOWLIST에 사유와 함께 추가.")
        return 1
    print("컨텍스트 경로 참조 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
