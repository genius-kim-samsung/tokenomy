"""CLI — 빠른 검증/복기용.

  python -m tokenomy.cli ingest   # 세션 로그 파싱 → DB (증분)
  python -m tokenomy.cli report   # 터미널 요약(번다운 + Top 프로젝트)
  python -m tokenomy.cli all      # ingest 후 report
  python -m tokenomy.cli official import-fixture <claude|codex> <path>
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from tokenomy.aggregate import KST, by_project, by_session, month_spend, official_view, parse_ts, pricing_coverage
from tokenomy.codex_parser import CODEX_ROOT, ingest_codex
from tokenomy.archive import archive_tree
from tokenomy.db import connect, ingest_root, ingest_titles, ingest_user_turns, maybe_reprice, insert_official_buckets
from tokenomy.freshness import CLEANUP_DAYS, freshness, record_ingest
from tokenomy.pricing import apply_pricing_overrides, load_pricing
from tokenomy.config import load_config, user_label, credit_to_usd, forecast_settings, tracked_providers
from tokenomy.official_parser import parse_claude, parse_codex

CLAUDE_ROOT = Path.home() / ".claude" / "projects"


def cmd_ingest(conn) -> None:
    config = load_config()
    pricing = apply_pricing_overrides(load_pricing(), config.get("pricing_overrides"))
    n_claude = ingest_root(conn, CLAUDE_ROOT, pricing, provider="claude")
    n_arch = archive_tree(CLAUDE_ROOT, conn, provider="claude")
    n_codex = ingest_codex(conn, CODEX_ROOT, pricing)
    archive_tree(CODEX_ROOT, conn, provider="codex")
    # 세션 작업 요약(aiTitle)을 휘발 전 L1에 캐시. Codex엔 ai-title이 없어 claude만.
    n_titles = ingest_titles(conn, CLAUDE_ROOT)
    n_turns = ingest_user_turns(conn, CLAUDE_ROOT)
    # 단가(pricing.json/overrides)가 바뀌었으면 기존 행 cost_usd를 자동 재계산.
    repriced = maybe_reprice(conn, pricing)
    record_ingest(conn, datetime.now(KST))
    msg = (
        f"[ingest] claude={n_claude}  codex={n_codex}  "
        f"archived_files={n_arch}  titles={n_titles}  turns={n_turns}  new records"
    )
    if repriced:
        msg += f"\n[reprice] 단가 변경 감지 — 기존 {repriced}행 비용 재계산"
    print(msg)
    # 수집은 순수 — 공식 갱신은 트리거하지 않는다(웹 대시보드 로드 시 hx-trigger="load"가 첫 갱신,
    # 이후 '자동 갱신 간격'마다 폴링 / 수동 갱신 버튼 / 설정 변경이 담당).


def cmd_official_import(conn, provider: str, path: str, *, now_kst=None,
                        credit_to_usd_value: float | None = None) -> int:
    """fixture/실측 raw JSON을 파서→DB로 주입하는 dev 명령(라이브 없이 검증용). 적재 버킷 수 반환.

    provider: 'claude' | 'codex'. now_kst/credit_to_usd_value는 테스트 주입용(미지정 시 실값).
    """
    now = now_kst or datetime.now(KST)
    ctu = credit_to_usd_value if credit_to_usd_value is not None else credit_to_usd(load_config())
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    parse = parse_claude if provider == "claude" else parse_codex
    buckets = parse(raw, credit_to_usd=ctu)
    ts = now.isoformat()
    return insert_official_buckets(conn, provider=provider, fetched_at=ts,
                                   buckets=buckets, created_at=ts)


def cmd_report(conn) -> None:
    config = load_config()
    ctu = credit_to_usd(config)
    weeks = forecast_settings(config)["rate_window_weeks"]
    now = datetime.now(KST)

    print(f"=== Tokenomy — {now:%Y-%m} (KST, 이 머신 데이터만) ===")
    print(f"User: {user_label(config)}")
    fr = freshness(conn, CLAUDE_ROOT, now)
    if fr.level == "warn":
        print(
            f"  [!] 수집 신선도: 가장 오래된 raw {fr.oldest_raw_age_days:.0f}일째 — "
            f"{CLEANUP_DAYS}일 경과 전 ingest 필요(미수집분 유실 위험)"
        )
    elif fr.hours_since_ingest is not None:
        print(f"  수집 최신: {fr.hours_since_ingest:.0f}h 전")

    for prov in tracked_providers(config):
        spent = month_spend(conn, prov, now)
        ov = official_view(conn, prov, now, ctu, weeks)
        line = f"\n[{prov}] 이번 달 총지출 ${spent:,.2f}"
        if ov.period_limit_usd:
            line += f" · 공식 ${ov.period_used_usd:,.2f}/${ov.period_limit_usd:,.0f}"
        print(line)
        rows = by_project(conn, prov, now, 12)
        if rows:
            print("  Top 프로젝트:")
            for p in rows:
                name = p.project or "(unknown)"
                print(
                    f"    ${p.cost:8.2f}  cache {p.cache_ratio * 100:4.1f}%  "
                    f"{p.sessions:3d} sess  {name}"
                )
                # 그 프로젝트의 비용 상위 세션 한 줄 요약(aiTitle)
                for s in by_session(conn, prov, now, 3, project=name, order="cost"):
                    title = s.summary or "(요약 없음)"
                    print(f"          ${s.cost:7.2f}  {title}")

    _print_recent_sessions(conn, now)

    pricing = apply_pricing_overrides(load_pricing(), config.get("pricing_overrides"))
    cov = pricing_coverage(conn, pricing)
    if cov.unpriced_count or cov.suspect_count:
        print(f"\n단가 커버리지: 미식별 {cov.unpriced_count}종 · 확인 필요 {cov.suspect_count}종 "
              f"(설정/pricing.json 확인)")
    else:
        print("\n단가 커버리지: 정상")


def _print_recent_sessions(conn, now, top_n: int = 10) -> None:
    """프로바이더 합산, 최근 세션 타임라인(시간순)."""
    recents: list = []
    for prov in ("claude", "codex"):
        recents += by_session(conn, prov, now, order="recent")
    recents.sort(key=lambda x: x.last_ts or "", reverse=True)
    recents = recents[:top_n]
    if not recents:
        return
    print("\n최근 세션 (시간순):")
    for s in recents:
        dt = parse_ts(s.last_ts)
        when = f"{dt:%m-%d %H:%M}" if dt else "  -  "
        title = s.summary or "(요약 없음)"
        proj = Path(s.project).name if s.project else "(unknown)"
        print(f"  {when}  ${s.cost:7.2f}  {title}  · {proj}")


def main(argv: list[str] | None = None) -> None:
    # Windows 콘솔에서도 한글/기호가 깨지지 않도록 (PYTHONIOENCODING 불필요)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "report"
    conn = connect()
    if cmd == "ingest":
        cmd_ingest(conn)
    elif cmd == "report":
        cmd_report(conn)
    elif cmd == "all":
        cmd_ingest(conn)
        cmd_report(conn)
    elif cmd == "official" and len(argv) >= 4 and argv[1] == "import-fixture":
        provider = argv[2] if argv[2] in ("claude", "codex") else "claude"
        n = cmd_official_import(conn, provider, argv[3])
        print(f"[official] {provider} 버킷 {n}개 적재")
    else:
        print("usage: python -m tokenomy.cli [ingest|report|all|official import-fixture <claude|codex> <path>]")


if __name__ == "__main__":
    main()
