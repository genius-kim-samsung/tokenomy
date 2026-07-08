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
from tokenomy.paths import data_dir, mini_view_available
from tokenomy.single_instance import SingleInstanceLock

WINDOW_TITLE = "Tokenomy"
WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 800

# 미니 뷰(ADR 0008) — 폭 고정, 높이는 내용에 맞춰 JS가 resize_mini로 조정.
MINI_TITLE = "Tokenomy 미니"
MINI_WIDTH = 300
MINI_HEIGHT = 200       # 첫 표시 높이(로드 후 내용 높이로 교체)


class Api:
    """pywebview JS 브리지 — 외부 링크 열기 + 미니/일반 배타 전환(to_mini/to_main/hide_to_tray/resize_mini)."""

    def open_external(self, url: str) -> None:
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            webbrowser.open(url)

    def to_mini(self) -> None:
        """일반뷰의 '⊟ 미니뷰' — 큰 창 숨기고 미니로 전환."""
        _to_mini()

    def to_main(self) -> None:
        """미니뷰의 '⊞ 일반뷰' — 미니 숨기고 큰 창 복원(수집 1회 동반)."""
        _to_main()

    def hide_to_tray(self) -> None:
        """미니뷰의 '✕' — 미니를 트레이로 숨김(마지막 뷰=미니 유지)."""
        _hide_mini_to_tray()

    def resize_mini(self, height) -> None:
        """미니 내용 높이에 창을 맞춤(폭 고정)."""
        _resize_mini(height)


# 상주 모드 상태 — 큰 창/미니 창/pystray 아이콘 + 종료 플래그 + 현재 뷰(배타 전환)·미니 표시 여부·서버 포트(lazy 생성)·백그라운드 폴 stop(ADR 0007).
_tray_state: dict = {"window": None, "icon": None, "quitting": False,
                     "mini": None, "current_view": "main", "mini_visible": False,
                     "port": None, "poll_stop": None}


def _on_closing() -> bool:
    """창 X — 종료 중이 아니면 창만 숨기고 닫기를 취소(False 반환), 첫 1회 안내."""
    if _tray_state["quitting"]:
        return True
    window = _tray_state["window"]
    if window is not None:
        window.hide()
    _maybe_first_time_notice()
    return False


def _maybe_first_time_notice() -> None:
    """첫 X-닫기 시 트레이 상주 안내를 1회 띄우고 config에 영속(영구히 1회)."""
    from tokenomy.config import load_config, save_config
    config = load_config()
    if config.get("tray_notice_seen"):
        return
    icon = _tray_state["icon"]
    if icon is not None:
        try:
            icon.notify("종료: 트레이 우클릭 → 종료. 사용량을 작게 흘끗 보려면 우클릭 → 미니뷰로 열기.",
                        "Tokenomy는 트레이에서 계속 실행됩니다")
        except Exception:
            pass
    config["tray_notice_seen"] = True
    save_config(config)


def _restore_if_minimized(win) -> None:
    """최소화(Minimized)된 창을 정상 상태로 복원 — pywebview show() 보완.

    pywebview(WinForms)의 show()는 Show()+Activate()만 호출해 WindowState를
    안 건드린다 — Win+D·작업표시줄 클릭 등으로 최소화된 창은 show() 뒤에도
    화면에 안 뜨고 Alt+Tab에만 남는다(프레임리스 미니는 사용자가 복원할 수단도
    없어 영구 '안 뜸'). Windows에서만 IsIconic으로 판정해 SW_RESTORE로 복원한다
    (무조건 restore()하면 큰 창의 최대화 상태를 잃으므로 조건부). native 미준비
    (생성 직후 등) 예외는 조용히 삼킨다 — 최초 생성 창은 최소화일 수 없다."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = win.native.Handle.ToInt32()
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)      # SW_RESTORE — 최대화 이력 보존
    except Exception:
        pass


def _show_window() -> None:
    """숨긴 창을 복원하고, 백그라운드에서 재수집 후 신규 있으면 리로드(조건부)."""
    window = _tray_state["window"]
    if window is None:
        return
    window.show()
    _restore_if_minimized(window)
    threading.Thread(target=_reingest_and_maybe_reload, daemon=True).start()


def _background_ingest(clear_banner: bool = False) -> None:
    """백그라운드 수집(창 우선 기동·창 복원 공용, ADR 0023).

    수집 가드(`begin_ingest`)로 동시 수집을 막고, **finally에서 반드시 해제**한다.
    완료 후: 변경이 있으면 페이지 리로드(데이터 반영 + 배너 제거), 변경이 없더라도
    `clear_banner=True`(시작 지연수집)면 "수집 중" 배너만 제거한다 — 변경 없는 기동에서
    불필요한 전체 리로드 깜빡임을 피우지 않으면서 배너가 영영 안 남게 한다."""
    from tokenomy.web import control
    if not control.begin_ingest():
        return                                  # 이미 수집 중(다른 진입점) — 중복 방지
    changed = 0
    try:
        from tokenomy.cli import cmd_ingest
        from tokenomy.db import connect
        changed = cmd_ingest(connect())
    except Exception as e:
        print(f"[launcher] 수집 건너뜀: {e}")
    finally:
        control.end_ingest()
    window = _tray_state.get("window")
    if window is None:
        return
    try:
        if changed:
            window.evaluate_js("window.location.reload()")
        elif clear_banner:
            window.evaluate_js(
                "var b=document.querySelector('.ingest-banner');if(b)b.remove();")
    except Exception:
        pass


def _reingest_and_maybe_reload() -> None:
    """창 복원 시 1회 수집 — 화면 영향 변경이 있을 때만 페이지 리로드(불필요한 깜빡임 방지)."""
    _background_ingest(clear_banner=False)


def _is_first_run() -> bool:
    """첫 실행(수집 이력 없음) 판정 — messages가 비면 True. 배너 가시성 위해 첫 실행은 일반뷰 강제."""
    try:
        from tokenomy.db import connect
        row = connect().execute("SELECT 1 FROM messages LIMIT 1").fetchone()
        return row is None
    except Exception:
        return False


def _init_db() -> None:
    """서버·수집 스레드 기동 전 DB 1회 연결 — 스키마/마이그레이션을 선행해 첫-수집 레이스 제거(ADR 0023)."""
    try:
        from tokenomy.db import connect
        connect()
    except Exception as e:
        print(f"[launcher] DB 초기화 건너뜀: {e}")


def _on_open(icon=None, item=None) -> None:
    """트레이 좌클릭(숨김 default '열기') — 마지막 본 뷰(일반/미니) 복원."""
    _restore_last_view()


def _on_open_main(icon=None, item=None) -> None:
    """트레이 '일반뷰로 열기' — 일반뷰로 배타 전환(last_view=main 영속, ADR 0008 개정)."""
    _to_main()


def _on_open_mini(icon=None, item=None) -> None:
    """트레이 '미니뷰로 열기' — 미니뷰로 배타 전환(last_view=mini 영속, ADR 0008 개정).
    비가용 플랫폼(Linux)에선 메뉴에서 숨겨지고 _to_mini도 no-op(ADR 0013)."""
    _to_mini()


def _start_background_poll() -> None:
    """상주 모드 백그라운드 공식 갱신 폴 스레드 기동(ADR 0007).

    창 숨김과 무관하게 자동 갱신 간격마다 공식 사용량을 갱신해 스냅샷 이력을 누적한다.
    config의 background_poll가 꺼져 있으면 background_poll_loop가 즉시 반환한다(no-op).
    stop_event는 종료(_on_quit) 시 set되어 sleep(stop_event.wait)을 깨운다.
    """
    from datetime import datetime
    from tokenomy.clock import KST
    from tokenomy.config import load_config
    from tokenomy.db import connect
    from tokenomy.official_fetch import background_poll_loop

    stop_event = threading.Event()
    _tray_state["poll_stop"] = stop_event
    config = load_config()

    def _run() -> None:
        background_poll_loop(
            config,
            conn_factory=connect,
            now_fn=lambda: datetime.now(KST),
            stop_event=stop_event,
            sleep_fn=stop_event.wait,   # 종료 시 즉시 깨어남(블로킹 sleep 대신)
        )
    threading.Thread(target=_run, daemon=True).start()


def _on_quit(icon=None, item=None) -> None:
    """트레이 '종료' — 종료 플래그 후 창 파괴(메인 스레드 GUI 루프 종료).

    미니 창도 함께 파괴한다(살아 있으면 GUI 루프가 안 끝날 수 있음)."""
    _tray_state["quitting"] = True
    stop_event = _tray_state.get("poll_stop")    # 0007: 백그라운드 폴 정지
    if stop_event is not None:
        stop_event.set()
    for key in ("mini", "window"):       # 미니 먼저, 큰 창 마지막
        win = _tray_state.get(key)
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass


# ── 미니 뷰(ADR 0008) — 위치 계산(순수) ──────────────────────────────────────
def _clamp_position(x, y, screen_w, screen_h, win_w, win_h):
    """저장된 미니 창 좌표를 화면 안으로 당긴다(모니터 분리 등으로 화면 밖이면 보정).

    x 또는 y가 None(미저장)이면 None 반환 → 호출부가 기본 위치를 쓴다."""
    if x is None or y is None:
        return None
    cx = min(max(int(x), 0), max(screen_w - win_w, 0))
    cy = min(max(int(y), 0), max(screen_h - win_h, 0))
    return (cx, cy)


def _default_mini_position(screen_w, screen_h, win_w, win_h, margin=16):
    """미저장 시 기본 위치 — 우하단(작업표시줄 위 코너), 마진만큼 띄움."""
    return (max(screen_w - win_w - margin, 0), max(screen_h - win_h - margin, 0))


# ── 미니 뷰 — 설정 영속 ──────────────────────────────────────────────────────
def _persist_mini(**fields) -> None:
    """미니 뷰 설정 일부(last_view·x·y)를 config['mini_view']에 병합 저장.

    None 값은 무시(부분 갱신) — 위치 저장(잦음)과 뷰 전환(last_view)이
    서로를 덮지 않게 기존 키를 보존한다."""
    from tokenomy.config import load_config, save_config
    config = load_config()
    mv = dict(config.get("mini_view") or {})
    mv.update({k: v for k, v in fields.items() if v is not None})
    config["mini_view"] = mv
    save_config(config)


def _save_mini_position(x, y) -> None:
    """미니 창 moved 이벤트 → 위치 영속(원본 저장, 화면 밖 보정은 복원 시 _clamp_position)."""
    _persist_mini(x=x, y=y)


# ── 미니 뷰(배타 전환, ADR 0008) — lazy 생성 / 전환 / 트레이 숨김 / 복원 ──────
def _set_view(view: str) -> None:
    """현재 뷰를 런타임 상태 + config에 영속(트레이 '열기'·재실행·재시작 복원 기준)."""
    _tray_state["current_view"] = view
    _persist_mini(last_view=view)


def _ensure_mini():
    """미니 창을 lazy 생성(보이는 상태 — hidden 미사용, WebView2 흰 창 회피). 이미 있으면 그대로.
    시작 시엔 만들지 않고 첫 미니 전환 때 한 번 만들어 이후 hide/show로 재사용한다."""
    mini = _tray_state.get("mini")
    if mini is not None:
        return mini
    import webview
    from tokenomy.config import load_config, mini_view_settings
    mv = mini_view_settings(load_config())
    mx, my = _resolve_mini_xy(mv)
    port = _tray_state.get("port")
    mini = webview.create_window(
        MINI_TITLE, f"http://127.0.0.1:{port}/mini",
        width=MINI_WIDTH, height=MINI_HEIGHT, x=mx, y=my,
        frameless=True, on_top=True, easy_drag=True, js_api=Api(),
    )
    _tray_state["mini"] = mini
    mini.events.closing += _on_mini_closing      # X/Alt+F4 → 트레이 숨김(파괴 아님)
    mini.events.moved += _save_mini_position      # 드래그 이동 → 위치 영속
    return mini


def _show_mini_window() -> None:
    """미니 창 표시(없으면 lazy 생성). 이미 있던 창을 다시 보일 땐 내용을 강제 갱신한다.

    htmx의 `load` 트리거는 최초 lazy 생성 1회만 발동한다. 배타 전환·트레이 복원으로
    미니를 재표시할 때(숨김→보임) 명시적 재요청이 없으면, 숨겨진 동안 메인이 공유 DB에
    적재해 둔 최신 공식 사용량을 미니가 못 읽고 옛 스냅샷으로 굳는다(메인은 갱신, 미니만 stale).
    최초 생성 때는 `load`가 첫 렌더를 담당하므로(또 htmx 미준비 eval 회피) 재요청을 건너뛴다."""
    existed = _tray_state.get("mini") is not None
    mini = _ensure_mini()
    mini.show()
    _restore_if_minimized(mini)
    _tray_state["mini_visible"] = True
    if existed:
        _refresh_mini_content(mini)


def _refresh_mini_content(mini) -> None:
    """표시된 미니 창에 #mini-section 재요청을 지시(htmx)해 최신 스냅샷으로 다시 렌더한다.

    네트워크가 throttle로 생략돼도 /mini/section은 DB를 다시 읽어 렌더하므로, 메인이 받아 둔
    최신 공식값이 즉시 반영된다. 창이 아직 준비 안 된 경우의 eval 실패는 조용히 삼킨다."""
    try:
        mini.evaluate_js(
            "window.htmx&&htmx.ajax('GET','/mini/section',"
            "{target:'#mini-section',swap:'innerHTML'})")
    except Exception:
        pass


def _to_mini() -> None:
    """일반뷰 → 미니: 큰 창 숨기고 미니 표시 + 마지막 뷰=미니 영속.
    미니뷰 비가용 플랫폼(Linux, ADR 0013)에선 완전 no-op — Wayland에서 깨지는 진입을 막는다."""
    if not mini_view_available():
        return
    window = _tray_state.get("window")
    if window is not None:
        window.hide()
    _show_mini_window()
    _set_view("mini")


def _to_main() -> None:
    """미니뷰 → 일반: 미니 숨기고 큰 창 복원(수집 1회 동반) + 마지막 뷰=일반 영속."""
    mini = _tray_state.get("mini")
    if mini is not None:
        mini.hide()
    _tray_state["mini_visible"] = False
    _set_view("main")
    _show_window()


def _hide_mini_to_tray() -> None:
    """미니 ✕/X — 미니를 트레이로 숨김(파괴 아님). current_view='mini' 유지 → 다음 복원도 미니."""
    mini = _tray_state.get("mini")
    if mini is not None:
        mini.hide()
    _tray_state["mini_visible"] = False
    _maybe_first_time_notice()


def _on_mini_closing() -> bool:
    """미니 창 X/Alt+F4 — 종료 중이 아니면 트레이 숨김(파괴 취소, False), 종료 중이면 닫기 허용."""
    if _tray_state.get("quitting"):
        return True
    _hide_mini_to_tray()
    return False


def _restore_last_view() -> None:
    """트레이 '열기'·단일 인스턴스 재실행(/app/show) — 마지막 본 뷰(일반/미니)로 복원."""
    if _tray_state.get("current_view") == "mini":
        _show_mini_window()
    else:
        _show_window()


def _resize_mini(height) -> None:
    """미니 창을 내용 높이에 맞춘다(폭 MINI_WIDTH 고정). 비숫자·비양수 높이는 무시."""
    win = _tray_state.get("mini")
    try:
        h = int(height)
    except (TypeError, ValueError):
        return
    if win is not None and h > 0 and _tray_state.get("mini_visible"):
        win.resize(MINI_WIDTH, h)


def _tray_icon_name(platform: str | None = None) -> str:
    """트레이 아이콘 리소스명 — Windows는 .ico, 그 외(Linux/macOS)는 .png(ADR 0013).
    pystray의 AppIndicator 백엔드(Ubuntu)는 .png를 쓴다(.ico 비호환 회피).
    platform=None이면 현재 sys.platform을 본다."""
    plat = platform if platform is not None else sys.platform
    return "assets/tokenomy.ico" if plat == "win32" else "assets/tokenomy.png"


def _tray_image():
    """트레이 아이콘 이미지(번들된 아이콘을 PIL로 로드 — 플랫폼별 .ico/.png)."""
    from PIL import Image
    from tokenomy.paths import resource_path
    return Image.open(str(resource_path(_tray_icon_name())))


def _build_tray():
    """pystray 트레이 아이콘 생성 — 좌클릭=숨김 default '열기'(마지막 본 뷰 복원),
    우클릭='일반뷰로 열기'/'미니뷰로 열기'(뷰별 명시 진입) + 구분선 + '종료'(ADR 0008 개정).

    '열기'는 좌클릭 활성화 전용이라 `visible=False` — pystray는 default 항목을 visible과
    무관하게 좌클릭에 매핑하고(Menu.__call__), 우클릭 팝업엔 visible 항목만 띄운다. 미니 토글
    (체크형 스위치) 재도입이 아니라 '열기' 어포던스의 뷰별 확장 — 배타 전환·창 버튼은 불변.
    '미니뷰로 열기'는 비가용 플랫폼(Linux, ADR 0013)에서 숨긴다(안 되는 항목 미노출)."""
    import pystray
    menu = pystray.Menu(
        pystray.MenuItem("열기", _on_open, default=True, visible=False),
        pystray.MenuItem("일반뷰로 열기", _on_open_main),
        pystray.MenuItem("미니뷰로 열기", _on_open_mini, visible=mini_view_available()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("종료", _on_quit),
    )
    return pystray.Icon("tokenomy", _tray_image(), "Tokenomy", menu=menu)


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


def _resolve_mini_xy(mv: dict) -> tuple:
    """미니 창 초기 좌표 — 저장값을 화면 안으로 보정(_clamp_position), 미저장이면 우하단 기본.
    화면 조회 실패(헤드리스/테스트)면 (None, None) → pywebview가 중앙 배치."""
    try:
        import webview
        scr = webview.screens[0]
        sw, sh = int(scr.width), int(scr.height)
    except Exception:
        return (None, None)
    pos = _clamp_position(mv.get("x"), mv.get("y"), sw, sh, MINI_WIDTH, MINI_HEIGHT)
    if pos is None:
        pos = _default_mini_position(sw, sh, MINI_WIDTH, MINI_HEIGHT)
    return pos


def _launch_window(port: int) -> None:
    """pywebview 큰 창 + pystray 트레이(상주). 미니 뷰는 배타 전환·lazy 생성(ADR 0008).
    트레이 미가용 시 단발로 강등(X=종료, 미니 전환 없음)."""
    import webview
    window = webview.create_window(
        WINDOW_TITLE, f"http://127.0.0.1:{port}/",
        width=WINDOW_WIDTH, height=WINDOW_HEIGHT, js_api=Api(),
    )
    _tray_state["window"] = window
    _tray_state["quitting"] = False
    _tray_state["mini"] = None
    _tray_state["mini_visible"] = False
    _tray_state["port"] = port      # 미니 lazy 생성 시 /mini URL 구성에 사용
    icon = None
    try:
        icon = _build_tray()
    except Exception as e:  # pystray/Pillow 미가용 → 단발 강등
        print(f"[launcher] 트레이 비활성(라이브러리 미가용) — 단발 모드: {e}")
    if icon is not None:
        from tokenomy.config import load_config, mini_view_settings
        from tokenomy.web.control import set_show_callback
        _tray_state["icon"] = icon
        # pywebview의 closing은 locking 이벤트라 핸들러의 False 반환이 닫기를 취소한다(hide-on-close의 핵심 의존).
        window.events.closing += _on_closing
        set_show_callback(_restore_last_view)         # 단일 인스턴스 재실행도 마지막 뷰로
        # 마지막 본 뷰를 복원 기준으로 — 미니 창은 시작 시 만들지 않고(흰 창 차단) 첫 전환 때 lazy 생성.
        # 미니뷰 비가용 플랫폼(Linux, ADR 0013)에선 'main'으로 clamp — last_view='mini'(타 OS에서 동기화된
        # config 등)여도 큰 창으로 시작하고, 트레이 '열기'·GUI 시작 콜백이 미니로 새지 않게 한다.
        last_view = mini_view_settings(load_config())["last_view"]
        view = last_view if mini_view_available() else "main"
        if _is_first_run():        # 첫 실행(빈 DB)은 "수집 중" 배너 가시성 위해 일반뷰 강제(ADR 0023)
            view = "main"
        _tray_state["current_view"] = view
        if sys.platform == "win32":
            # Windows: 트레이는 자체 Win32 메시지 루프 → 데몬 스레드에서 독립 실행(기존).
            threading.Thread(target=icon.run, daemon=True).start()
        else:
            # Linux(GTK/WebKit2GTK): pystray와 pywebview가 같은 GLib '기본 메인 컨텍스트'를 공유해야 한다.
            # pystray의 icon.run()은 데몬 스레드에서 기본 컨텍스트에 두 번째 GLib 루프(GLib.MainLoop)를
            # 띄우는데, 메인 스레드의 webview GTK 루프(g_application_run)도 같은 컨텍스트를 잡으려 해
            # 충돌한다(GLib-GIO-CRITICAL: can not acquire the default main context → 창이 안 뜸).
            # run_detached()는 루프를 만들지 않고 초기화만 하므로, 이어지는 webview.start()의 단일 GTK
            # 루프가 창과 트레이를 함께 처리한다. SIGINT 복원이 메인 스레드를 요구해 메인에서 호출(ADR 0013).
            icon.run_detached()
        _start_background_poll()   # 상주 모드에서만 — 단발 강등 시엔 폴 안 함(ADR 0007)
        webview.start(_on_gui_start)  # ← 메인 GUI 루프(블로킹). 시작 직후 콜백이 마지막 뷰 적용.
    else:
        webview.start()
    if icon is not None:
        try:
            icon.stop()
        except Exception:
            pass


def _on_gui_start() -> None:
    """GUI 루프 시작 직후 — 마지막 뷰가 미니면 미니로 전환(큰 창 숨김 + 미니 lazy 생성),
    그리고 **첫 수집을 백그라운드로 기동**한다(창 우선 기동, ADR 0023). 창이 이미 떠 있으므로
    무거운 첫 수집이 창 표시를 막지 않고, 도는 동안 대시보드엔 "수집 중" 배너가 보인다."""
    if _tray_state.get("current_view") == "mini":
        _to_mini()
    threading.Thread(
        target=lambda: _background_ingest(clear_banner=True), daemon=True).start()


def _open_browser_when_ready(port: int) -> None:
    if _wait_until_ready(port):
        webbrowser.open(f"http://127.0.0.1:{port}/")
    else:
        print(f"[launcher] 서버가 {port}에서 응답하지 않아 브라우저를 열지 않습니다")


def _signal_existing_window(retries: int = 12, interval: float = 0.25) -> None:
    """락 점유(다른 인스턴스 활성) — runtime.json+ping으로 포트를 ~3초 내 찾아 /app/show.

    1차 인스턴스가 락 획득~서버 준비 사이(수백 ms)라 즉시 응답 못 할 수 있어 짧게 재시도한다.
    찾으면 기존 창을 복원시키고, 끝내 못 찾으면(1차가 hang 등) 로그만 남기고 조용히 종료한다 —
    **2번째 창은 절대 띄우지 않는다**(락이 점유라 했으니)."""
    for _ in range(max(1, retries)):
        port = _existing_instance_port()
        if port is not None:
            _signal_show(port)
            print(f"[Tokenomy] 이미 실행 중 — 기존 창을 띄웁니다 (포트 {port})")
            return
        time.sleep(interval)
    print("[Tokenomy] 이미 실행 중이지만 기존 창을 찾지 못했습니다 — 잠시 후 다시 시도하세요")


def main(argv: list[str] | None = None) -> None:
    _ensure_std_streams()
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] in ("--version", "-V"):
        print(__version__)
        return

    # 단일 인스턴스 락(ADR 0023) — main 최상단, webview/브라우저 공통. 가계부(data_dir)당 하나.
    # 프로세스 생애 동안 보유(사망 시 OS 자동 해제). runtime.json+ping은 "어느 창 띄울지" 라우팅만.
    lock = SingleInstanceLock(data_dir())
    if not lock.acquire():
        _signal_existing_window()       # 이미 점유 — 기존 창 복원 신호 후 본인 종료
        return
    try:
        if _webview_available():
            # 창 우선 기동(ADR 0023): 서버·런타임·창을 수집보다 **먼저** 띄운다. 무거운 첫 수집은
            # 창 표시 후 _on_gui_start가 백그라운드로 기동 — 창이 즉시 떠 재실행 유인을 끊는다.
            _init_db()                  # 스레드 기동 전 스키마 워밍(첫-수집 레이스 제거)
            port = find_free_port()
            _write_runtime(port)
            threading.Thread(target=_serve, args=(port,), daemon=True).start()
            if not _wait_until_ready(port):
                print(f"[Tokenomy] 서버가 {port}에서 응답하지 않습니다")
                return
            _launch_window(port)        # 블로킹. 시작 직후 콜백이 백그라운드 첫 수집을 기동.
        else:
            # WebView 미가용(구형 환경) — 브라우저 + uvicorn 메인 블로킹. 네이티브 창이 없어
            # 창 우선이 무의미하므로 기존대로 동기 수집 후 서빙(단일 인스턴스는 락이 보장).
            _safe_ingest()
            port = find_free_port()
            _write_runtime(port)
            threading.Thread(
                target=_open_browser_when_ready, args=(port,), daemon=True
            ).start()
            print(f"[Tokenomy] http://127.0.0.1:{port}/  (이 창을 닫으면 종료됩니다)")
            _serve(port)
    finally:
        _clear_runtime()
        lock.release()


if __name__ == "__main__":
    main()
