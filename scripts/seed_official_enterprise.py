"""엔터프라이즈 공식 사용량 view를 개인 계정 PC에서 눈으로 확인하기 위한 dev 시드.

개인 구독 계정으로 개발 중이면 공식 API가 % 창(rate_window)만 반환하므로, enterprise
계정의 달러/크레딧 게이지 view를 실제로 띄워볼 수 없다. 이 스크립트는 실측 enterprise
응답(tests/fixtures/official/*_enterprise_real.json)을 파서에 통과시켜 격리 DB에 적재한다.
그 DB로 웹을 띄우면 enterprise 카드가 그대로 렌더된다(개인 DB는 건드리지 않는다).

전 과정 로컬·네트워크 없음. official_fetch(아웃바운드)를 우회하고 파서→DB 경로만 재사용한다.

사용:
    # 1) 격리 DB에 시드(기본 ~/.tokenomy-ent-preview — 개인 data/tokenomy.db와 분리)
    .venv\\Scripts\\python scripts\\seed_official_enterprise.py

    # 2) 같은 데이터 디렉토리로 웹 실행 후 대시보드 확인
    #    SKIP_OFFICIAL_FETCH를 켜야 개인 계정 API가 enterprise 시드를 덮어쓰지 않는다.
    $env:TOKENOMY_DATA = "$HOME\\.tokenomy-ent-preview"
    $env:TOKENOMY_SKIP_OFFICIAL_FETCH = "1"
    .venv\\Scripts\\python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765
    # → 브라우저 http://127.0.0.1:8765

옵션:
    --provider {claude,codex,both}  시드할 provider(기본 both)
    --source   {real,sample}        실측(real) 또는 단순 테스트값(sample) fixture(기본 real)
    --data-dir PATH                 격리 데이터 디렉토리(기본 ~/.tokenomy-ent-preview)
    --reset                         시드 전 기존 official 버킷 삭제(시나리오 전환 시)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
# 스크립트 직접 실행 시 sys.path[0]은 scripts/라 tokenomy를 못 찾는다 → repo 루트를 앞에 추가.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_FIX = _REPO_ROOT / "tests" / "fixtures" / "official"
KST = timezone(timedelta(hours=9))

# (provider, source) → fixture 파일명. real=실측 응답, sample=테스트용 단순값.
_FIXTURES = {
    ("claude", "real"): "claude_enterprise_real.json",
    ("claude", "sample"): "claude_enterprise.json",
    ("codex", "real"): "codex_enterprise_real.json",
    ("codex", "sample"): "codex_enterprise.json",
}


def _seed_provider(conn, provider: str, source: str, fetched_at: str, ctu: float):
    """fixture를 파서에 통과시켜 격리 DB에 적재. (적재 버킷 수, fixture명) 반환."""
    from tokenomy.db import insert_official_buckets
    from tokenomy.official_parser import parse_claude, parse_codex

    fixture = _FIXTURES[(provider, source)]
    raw = json.loads((_FIX / fixture).read_text(encoding="utf-8"))
    parse = parse_claude if provider == "claude" else parse_codex
    buckets = parse(raw, credit_to_usd=ctu)
    n = insert_official_buckets(conn, provider=provider, fetched_at=fetched_at,
                                buckets=buckets, created_at=fetched_at)
    return n, fixture


def main() -> None:
    ap = argparse.ArgumentParser(description="enterprise 공식 사용량 view dev 시드(로컬·격리)")
    ap.add_argument("--provider", choices=["claude", "codex", "both"], default="both")
    ap.add_argument("--source", choices=["real", "sample"], default="real")
    ap.add_argument("--data-dir", default=str(Path.home() / ".tokenomy-ent-preview"))
    ap.add_argument("--reset", action="store_true", help="시드 전 기존 official 버킷 삭제")
    args = ap.parse_args()

    # 격리: 개인 data/tokenomy.db를 건드리지 않도록 TOKENOMY_DATA를 강제 설정.
    # (env 설정을 paths가 읽으므로 tokenomy 임포트보다 먼저 해야 한다.)
    data_dir = str(Path(args.data_dir).expanduser())
    os.environ["TOKENOMY_DATA"] = data_dir

    from tokenomy.budget import credit_to_usd, load_config
    from tokenomy.db import connect

    ctu = credit_to_usd(load_config())
    fetched_at = datetime.now(KST).isoformat()
    providers = ["claude", "codex"] if args.provider == "both" else [args.provider]

    conn = connect()
    if args.reset:
        conn.execute("DELETE FROM official_buckets")
        conn.commit()
        print("기존 official_buckets 삭제")

    for p in providers:
        n, fixture = _seed_provider(conn, p, args.source, fetched_at, ctu)
        print(f"  {p:6s}: {fixture} → {n} buckets")
    conn.close()

    db = Path(data_dir) / "data" / "tokenomy.db"
    print(f"\n시드 완료(credit_to_usd={ctu}) · 격리 DB: {db}")
    print("웹으로 확인(개인 API가 시드를 덮어쓰지 않도록 SKIP_OFFICIAL_FETCH 필수):")
    print(f'  $env:TOKENOMY_DATA = "{data_dir}"')
    print(r'  $env:TOKENOMY_SKIP_OFFICIAL_FETCH = "1"')
    print(r'  .venv\Scripts\python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765')
    print("  → 브라우저 http://127.0.0.1:8765")


if __name__ == "__main__":
    main()
