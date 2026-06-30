"""창 복원 신호용 in-process 콜백 레지스트리.

uvicorn 라우트(데몬 스레드)와 런처(메인 스레드 webview)는 같은 프로세스에 산다.
라우트가 launcher를 직접 import하면 순환이 생기므로, 얇은 레지스트리로 디커플한다.
launcher가 _show_window를 등록(set_show_callback)하고, /app/show 라우트가 request_show로 호출한다.
"""
from __future__ import annotations

import threading
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


# ── 수집 실행 상태(ADR 0023) ──────────────────────────────────────────────────
# 인프로세스 running 플래그(+lock). 두 가지를 겸한다:
#  ① 배너 게이트 — overview 컨텍스트가 is_ingesting()을 읽어 "수집 중" 배너를 건다.
#  ② 동시-수집 가드 — 모든 수집(writer) 진입점(시작 지연수집·창 복원 re-ingest·수동
#     /ingest)이 begin_ingest()로 진입을 직렬화해 같은 SQLite 동시 쓰기를 막는다.
_ingest_state_lock = threading.Lock()
_ingest_in_progress = False


def begin_ingest() -> bool:
    """수집 시작을 표시한다 — 다른 수집이 미진행이면 True(점유), 진행 중이면 False.

    호출부는 True일 때만 수집을 진행하고, **finally에서 반드시 end_ingest()**를 호출한다."""
    global _ingest_in_progress
    with _ingest_state_lock:
        if _ingest_in_progress:
            return False
        _ingest_in_progress = True
        return True


def end_ingest() -> None:
    """수집 종료를 표시한다(멱등 — 미진행 상태에서 불러도 안전)."""
    global _ingest_in_progress
    with _ingest_state_lock:
        _ingest_in_progress = False


def is_ingesting() -> bool:
    """현재 수집이 진행 중인가(배너·공식 갱신 지연 판정에 사용)."""
    with _ingest_state_lock:
        return _ingest_in_progress
