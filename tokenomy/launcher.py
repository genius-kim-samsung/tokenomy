"""exe 진입점 — 더블클릭 실행.

ingest 1회 → 빈 포트 탐색 → uvicorn 기동(127.0.0.1, 로컬 전용) →
WebView 가용 시 자체 앱 창, 미가용 시 기본 브라우저로 fallback.
PyInstaller 엔트리 스크립트.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request
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


def _ensure_std_streams() -> None:
    """windowed(PyInstaller noconsole) 실행에서 sys.stdout/stderr가 None이면
    devnull로 대체 — print/로깅이 AttributeError로 죽지 않게 한다.
    CLI 파이프로 실행될 때는 stdout이 살아 있으므로 건드리지 않는다."""
    import os
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _write_runtime(port: int) -> None:
    """실행 중 인스턴스의 port/pid를 런타임 파일에 기록(단일 인스턴스 감지용)."""
    from tokenomy.paths import runtime_path
    rt = runtime_path()
    rt.parent.mkdir(parents=True, exist_ok=True)
    rt.write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")


def _clear_runtime() -> None:
    """런타임 파일 제거(종료 시). 없으면 무시."""
    from tokenomy.paths import runtime_path
    try:
        runtime_path().unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _read_runtime() -> dict | None:
    from tokenomy.paths import runtime_path
    rt = runtime_path()
    if not rt.exists():
        return None
    try:
        return json.loads(rt.read_text(encoding="utf-8"))
    except Exception:
        return None


def _existing_instance_port() -> int | None:
    """런타임 파일이 가리키는 포트가 우리 앱(/app/ping 마커)으로 응답하면 그 포트, 아니면 None.
    포트가 비었거나(crash 후) 다른 앱이 점유 중이면 None → 본인이 첫 인스턴스로 진행."""
    data = _read_runtime()
    if not data:
        return None
    try:
        port = int(data["port"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/app/ping", timeout=1) as r:
            body = json.loads(r.read().decode("utf-8"))
        return port if body.get("app") == "tokenomy" else None
    except Exception:
        return None


def _signal_show(port: int) -> None:
    """기존 인스턴스에 창 복원을 신호(POST /app/show). 예외는 삼킨다."""
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/app/show", data=b"", timeout=2)
    except Exception:
        pass


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


def _webview_available() -> bool:
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


def _serve(port: int) -> None:
    """uvicorn 기동(블로킹). 데몬 스레드 또는 메인 스레드에서 호출."""
    import uvicorn
    from tokenomy.web.app import app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def _launch_window(port: int) -> None:
    """pywebview 창을 띄운다(블로킹). 창을 닫으면 반환된다."""
    import webview
    webview.create_window(
        WINDOW_TITLE, f"http://127.0.0.1:{port}/",
        width=WINDOW_WIDTH, height=WINDOW_HEIGHT, js_api=Api(),
    )
    webview.start()


def _open_browser_when_ready(port: int) -> None:
    if _wait_until_ready(port):
        webbrowser.open(f"http://127.0.0.1:{port}/")
    else:
        print(f"[launcher] 서버가 {port}에서 응답하지 않아 브라우저를 열지 않습니다")


def main(argv: list[str] | None = None) -> None:
    _ensure_std_streams()
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] in ("--version", "-V"):
        print(__version__)
        return

    _safe_ingest()
    port = find_free_port()

    if _webview_available():
        # 서버는 데몬 스레드, 창이 메인 스레드를 점유 → 창 닫으면 프로세스 종료(단발 앱)
        threading.Thread(target=_serve, args=(port,), daemon=True).start()
        if not _wait_until_ready(port):
            print(f"[Tokenomy] 서버가 {port}에서 응답하지 않습니다")
            return
        _launch_window(port)
    else:
        # WebView 미가용(구형 환경) — 기존 방식: 브라우저 + uvicorn 메인 블로킹
        threading.Thread(
            target=_open_browser_when_ready, args=(port,), daemon=True
        ).start()
        print(f"[Tokenomy] http://127.0.0.1:{port}/  (이 창을 닫으면 종료됩니다)")
        _serve(port)


if __name__ == "__main__":
    main()
