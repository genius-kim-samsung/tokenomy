"""exe 진입점 — 더블클릭 실행.

데이터 디렉토리 보장 → ingest 1회 → 빈 포트 탐색 → 브라우저 자동 오픈 →
uvicorn 기동(127.0.0.1, 로컬 전용). PyInstaller 엔트리 스크립트.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser

from tokenomy import __version__

WINDOW_TITLE = "Tokenomy"
WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 800


class Api:
    """pywebview JS 브리지 — 외부 링크를 기본 브라우저로 연다."""

    def open_external(self, url: str) -> None:
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            webbrowser.open(url)


def find_free_port(start: int = 8765, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"빈 포트를 찾지 못함 ({start}~{start + tries - 1})")


def _safe_ingest() -> None:
    try:
        from tokenomy.cli import cmd_ingest
        from tokenomy.db import connect
        conn = connect()
        cmd_ingest(conn)
    except Exception as e:  # ingest 실패는 치명적이지 않음 — 기존 데이터로 표시
        print(f"[launcher] ingest 건너뜀: {e}")


def _wait_until_ready(port: int, timeout: float = 10.0, interval: float = 0.25) -> bool:
    """서버가 127.0.0.1:port에서 응답할 때까지 대기. 준비되면 True, 타임아웃이면 False."""
    for _ in range(max(1, int(timeout / interval))):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(interval)
    return False


def _open_browser_when_ready(port: int) -> None:
    for _ in range(40):  # 최대 ~10초 대기
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                webbrowser.open(f"http://127.0.0.1:{port}/")
                return
        time.sleep(0.25)
    print(f"[launcher] 서버가 {port}에서 응답하지 않아 브라우저를 열지 않습니다")


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] in ("--version", "-V"):
        print(__version__)
        return

    _safe_ingest()
    port = find_free_port()
    threading.Thread(target=_open_browser_when_ready, args=(port,), daemon=True).start()

    import uvicorn
    from tokenomy.web.app import app
    print(f"[Tokenomy] http://127.0.0.1:{port}/  (이 창을 닫으면 종료됩니다)")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
