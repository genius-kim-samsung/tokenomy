"""CLI — 빠른 검증/복기용.

  python -m tokenomy.cli ingest   # 세션 로그 파싱 → DB (증분)
  python -m tokenomy.cli report   # 터미널 요약(번다운 + Top 업무)
  python -m tokenomy.cli all      # ingest 후 report
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from tokenomy.aggregate import KST, burndown, by_project, by_session, parse_ts
from tokenomy.codex_parser import CODEX_ROOT, ingest_codex
from tokenomy.archive import archive_tree
from tokenomy.db import connect, ingest_root, ingest_titles
from tokenomy.freshness import CLEANUP_DAYS, freshness, record_ingest
from tokenomy.pricing import apply_pricing_overrides, load_pricing
from tokenomy.budget import budget_from_config, load_config, user_label

CLAUDE_ROOT = Path.home() / ".claude" / "projects"


def cmd_ingest(conn) -> None:
    pricing = apply_pricing_overrides(load_pricing(), load_config().get("pricing_overrides"))
    n_claude = ingest_root(conn, CLAUDE_ROOT, pricing, provider="claude")
    n_arch = archive_tree(CLAUDE_ROOT, conn, provider="claude")
    n_codex = ingest_codex(conn, CODEX_ROOT, pricing)
    archive_tree(CODEX_ROOT, conn, provider="codex")
    # 세션 작업 요약(aiTitle)을 휘발 전 L1에 캐시. Codex엔 ai-title이 없어 claude만.
    n_titles = ingest_titles(conn, CLAUDE_ROOT)
    record_ingest(conn, datetime.now(KST))
    print(
        f"[ingest] claude={n_claude}  codex={n_codex}  "
        f"archived_files={n_arch}  titles={n_titles}  new records"
    )


def _bar(pct: float, width: int = 20) -> str:
    fill = int(min(max(pct, 0.0), 1.0) * width)
    return "[" + "#" * fill + "-" * (width - fill) + "]"


def cmd_report(conn) -> None:
    config = load_config()
    budget = budget_from_config(config)
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

    for prov in ("claude", "codex"):  # codex = Codex CLI
        bd = burndown(conn, budget, now, prov)
        status = "OK" if bd.on_track else "[!] OVER"
        print(
            f"\n[{prov}] limit ${bd.limit:.0f}  spent ${bd.spent:.2f}  "
            f"({bd.pct * 100:.1f}%) {_bar(bd.pct)} {status}"
        )
        print(
            f"  {bd.day_of_month}/{bd.days_in_month} days  "
            f"daily-avg ${bd.daily_avg:.2f}  projected ${bd.projected_month:.2f}"
        )
        if bd.pct >= 1.0:
            print(f"  [!] 이미 한도 초과 — 절감 필요 (한도 대비 {bd.pct * 100:.0f}%)")
        elif bd.exhaust_day:
            print(f"  [!] 이대로면 {bd.exhaust_day}일에 소진 (남은 {bd.days_left}일)")
        if bd.unpriced_count:
            print(f"  (단가 미식별 메시지 {bd.unpriced_count}건 — 비용 누락)")

        rows = by_project(conn, prov, now, 12)
        if rows:
            print("  Top 업무(프로젝트):")
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
    else:
        print("usage: python -m tokenomy.cli [ingest|report|all]")


if __name__ == "__main__":
    main()
