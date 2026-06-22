"""창 복원 신호용 in-process 콜백 레지스트리.

uvicorn 라우트(데몬 스레드)와 런처(메인 스레드 webview)는 같은 프로세스에 산다.
라우트가 launcher를 직접 import하면 순환이 생기므로, 얇은 레지스트리로 디커플한다.
launcher가 _show_window를 등록(set_show_callback)하고, /app/show 라우트가 request_show로 호출한다.
"""
from __future__ import annotations

from typing import Callable

_show_callback: Callable[[], None] | None = None


def set_show_callback(fn: Callable[[], None] | None) -> None:
    global _show_callback
    _show_callback = fn


def request_show() -> None:
    """등록된 창-복원 콜백을 호출(미등록이면 no-op)."""
    cb = _show_callback
    if cb is not None:
        cb()
