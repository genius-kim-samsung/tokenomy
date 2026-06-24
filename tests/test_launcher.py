import socket
import sys

import pytest

from tokenomy import launcher


def test_version_flag(capsys):
    launcher.main(["--version"])
    out = capsys.readouterr().out.strip()
    assert out == launcher.__version__


def test_find_free_port_returns_bindable():
    port = launcher.find_free_port(8765)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))  # 반환된 포트는 실제로 bind 가능


def test_find_free_port_skips_occupied():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occ:
        occ.bind(("127.0.0.1", 8765))
        port = launcher.find_free_port(8765)
        assert port != 8765
        assert 8765 < port < 8785


def test_find_free_port_raises_when_exhausted():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 8765))
        with pytest.raises(RuntimeError, match="빈 포트"):
            launcher.find_free_port(8765, tries=1)


def test_api_open_external_opens_http(monkeypatch):
    opened = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda u: opened.append(u))
    launcher.Api().open_external("https://example.com/x")
    assert opened == ["https://example.com/x"]


def test_api_open_external_ignores_non_http(monkeypatch):
    opened = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda u: opened.append(u))
    launcher.Api().open_external("javascript:alert(1)")
    launcher.Api().open_external("/settings")
    launcher.Api().open_external(None)
    assert opened == []


def test_wait_until_ready_true_when_serving():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        assert launcher._wait_until_ready(port, timeout=1.0) is True


def test_wait_until_ready_false_when_closed():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # 위 with 블록 종료로 포트는 닫힘 — listen하는 곳이 없음
    assert launcher._wait_until_ready(port, timeout=0.5) is False


def test_main_uses_window_when_webview_available(monkeypatch):
    calls = {}
    monkeypatch.setattr(launcher, "_safe_ingest", lambda: None)
    monkeypatch.setattr(launcher, "find_free_port", lambda: 9999)
    monkeypatch.setattr(launcher, "_webview_available", lambda: True)
    monkeypatch.setattr(launcher, "_existing_instance_port", lambda: None)
    monkeypatch.setattr(launcher, "_write_runtime", lambda port: calls.__setitem__("runtime", port))
    monkeypatch.setattr(launcher, "_clear_runtime", lambda: calls.__setitem__("cleared", True))
    monkeypatch.setattr(launcher, "_wait_until_ready", lambda port, **k: True)
    monkeypatch.setattr(launcher, "_launch_window",
                        lambda port: calls.__setitem__("window", port))
    monkeypatch.setattr(launcher, "_open_browser_when_ready",
                        lambda port: calls.__setitem__("browser", port))

    class FakeThread:
        def __init__(self, target=None, args=(), **kw):
            calls["thread_target"] = target
            calls["thread_args"] = args
            calls["thread_kwargs"] = kw

        def start(self):
            calls["thread_started"] = True

    monkeypatch.setattr(launcher.threading, "Thread", FakeThread)

    launcher.main([])
    # 서버는 데몬 스레드로 기동, 창은 메인 스레드에서 직접
    assert calls.get("thread_target") is launcher._serve
    assert calls.get("thread_args") == (9999,)
    assert calls.get("thread_started") is True
    assert calls.get("thread_kwargs", {}).get("daemon") is True
    assert calls.get("window") == 9999
    assert "browser" not in calls
    assert calls.get("runtime") == 9999
    assert calls.get("cleared") is True


def test_main_signals_existing_instance_and_exits(monkeypatch):
    calls = {}
    monkeypatch.setattr(launcher, "_webview_available", lambda: True)
    monkeypatch.setattr(launcher, "_existing_instance_port", lambda: 8765)
    monkeypatch.setattr(launcher, "_signal_show", lambda port: calls.__setitem__("signaled", port))
    monkeypatch.setattr(launcher, "_safe_ingest", lambda: calls.__setitem__("ingested", True))
    monkeypatch.setattr(launcher, "_launch_window", lambda port: calls.__setitem__("window", port))
    launcher.main([])
    assert calls.get("signaled") == 8765       # 기존 창 복원 신호
    assert "ingested" not in calls             # 두 번째 인스턴스는 수집 안 함
    assert "window" not in calls               # 창도 안 띄움


def test_main_falls_back_to_browser_when_no_webview(monkeypatch):
    calls = {}
    monkeypatch.setattr(launcher, "_safe_ingest", lambda: None)
    monkeypatch.setattr(launcher, "find_free_port", lambda: 9999)
    monkeypatch.setattr(launcher, "_webview_available", lambda: False)
    monkeypatch.setattr(launcher, "_serve", lambda port: calls.__setitem__("serve", port))
    monkeypatch.setattr(launcher, "_launch_window",
                        lambda port: calls.__setitem__("window", port))

    class FakeThread:
        def __init__(self, target=None, args=(), **kw):
            calls["thread_target"] = target
            calls["thread_args"] = args
            calls["thread_kwargs"] = kw

        def start(self):
            calls["thread_started"] = True

    monkeypatch.setattr(launcher.threading, "Thread", FakeThread)

    launcher.main([])
    # 브라우저 오프너는 데몬 스레드, _serve는 메인 스레드 블로킹
    assert calls.get("thread_target") is launcher._open_browser_when_ready
    assert calls.get("thread_args") == (9999,)
    assert calls.get("thread_started") is True
    assert calls.get("thread_kwargs", {}).get("daemon") is True
    assert calls.get("serve") == 9999
    assert "window" not in calls


def test_ensure_std_streams_replaces_none(monkeypatch):
    monkeypatch.setattr(launcher.sys, "stdout", None)
    monkeypatch.setattr(launcher.sys, "stderr", None)
    launcher._ensure_std_streams()
    assert launcher.sys.stdout is not None
    assert launcher.sys.stderr is not None


def test_ensure_std_streams_keeps_existing(monkeypatch):
    import io
    fake = io.StringIO()
    monkeypatch.setattr(launcher.sys, "stdout", fake)
    launcher._ensure_std_streams()
    assert launcher.sys.stdout is fake  # 살아 있으면 그대로 둔다


def test_runtime_roundtrip_and_clear(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_DATA", str(tmp_path))
    launcher._write_runtime(8765)
    assert launcher._read_runtime() == {"port": 8765, "pid": __import__("os").getpid()}
    launcher._clear_runtime()
    assert launcher._read_runtime() is None


def test_existing_instance_none_when_no_file(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_DATA", str(tmp_path))
    assert launcher._existing_instance_port() is None


def test_existing_instance_returns_port_when_ping_marker_matches(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_DATA", str(tmp_path))
    launcher._write_runtime(8765)

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"app": "tokenomy"}'
    monkeypatch.setattr(launcher.urllib.request, "urlopen", lambda url, **k: FakeResp())
    assert launcher._existing_instance_port() == 8765


def test_existing_instance_none_when_ping_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_DATA", str(tmp_path))
    launcher._write_runtime(8765)

    def boom(url, **k): raise OSError("refused")
    monkeypatch.setattr(launcher.urllib.request, "urlopen", boom)
    assert launcher._existing_instance_port() is None


def test_signal_show_posts_to_show_endpoint(monkeypatch):
    seen = {}
    def fake_urlopen(url, data=None, timeout=None):
        seen["url"] = url
        seen["is_post"] = data is not None
        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()
    monkeypatch.setattr(launcher.urllib.request, "urlopen", fake_urlopen)
    launcher._signal_show(8765)
    assert seen["url"] == "http://127.0.0.1:8765/app/show"
    assert seen["is_post"] is True


# ──────────────────────────────────────────────
# Task 4: 상주 수명주기 핸들러 테스트
# ──────────────────────────────────────────────

class _FakeWindow:
    def __init__(self): self.calls = []
    def hide(self): self.calls.append("hide")
    def show(self): self.calls.append("show")
    def destroy(self): self.calls.append("destroy")
    def evaluate_js(self, js): self.calls.append(("js", js))
    def resize(self, w, h): self.calls.append(("resize", w, h))


class _FakeIcon:
    def __init__(self): self.notes = []
    def notify(self, message, title=None): self.notes.append((title, message))


def _reset_tray_state(monkeypatch, window=None, icon=None, quitting=False):
    monkeypatch.setattr(launcher, "_tray_state",
                        {"window": window, "icon": icon, "quitting": quitting})


def test_on_closing_hides_and_cancels_when_not_quitting(monkeypatch):
    w = _FakeWindow()
    _reset_tray_state(monkeypatch, window=w)
    monkeypatch.setattr(launcher, "_maybe_first_time_notice", lambda: None)
    assert launcher._on_closing() is False     # 닫기 취소
    assert "hide" in w.calls


def test_on_closing_allows_close_when_quitting(monkeypatch):
    w = _FakeWindow()
    _reset_tray_state(monkeypatch, window=w, quitting=True)
    assert launcher._on_closing() is True       # 진짜 종료 → 닫기 허용
    assert "hide" not in w.calls


def test_on_quit_sets_flag_and_destroys(monkeypatch):
    w = _FakeWindow()
    _reset_tray_state(monkeypatch, window=w)
    launcher._on_quit()
    assert launcher._tray_state["quitting"] is True
    assert "destroy" in w.calls


def test_show_window_shows_and_spawns_reingest(monkeypatch):
    w = _FakeWindow()
    _reset_tray_state(monkeypatch, window=w)
    spawned = {}
    class FakeThread:
        def __init__(self, target=None, daemon=None, **k):
            spawned["target"] = target; spawned["daemon"] = daemon
        def start(self): spawned["started"] = True
    monkeypatch.setattr(launcher.threading, "Thread", FakeThread)
    launcher._show_window()
    assert "show" in w.calls
    assert spawned["target"] is launcher._reingest_and_maybe_reload
    assert spawned["daemon"] is True and spawned["started"] is True


def test_reingest_reloads_only_when_changed(monkeypatch):
    w = _FakeWindow()
    _reset_tray_state(monkeypatch, window=w)
    monkeypatch.setattr("tokenomy.db.connect", lambda: object())
    monkeypatch.setattr("tokenomy.cli.cmd_ingest", lambda conn: 3)   # 신규 3건
    launcher._reingest_and_maybe_reload()
    assert ("js", "window.location.reload()") in w.calls


def test_reingest_no_reload_when_unchanged(monkeypatch):
    w = _FakeWindow()
    _reset_tray_state(monkeypatch, window=w)
    monkeypatch.setattr("tokenomy.db.connect", lambda: object())
    monkeypatch.setattr("tokenomy.cli.cmd_ingest", lambda conn: 0)   # 변화 없음
    launcher._reingest_and_maybe_reload()
    assert all(c[0] != "js" for c in w.calls if isinstance(c, tuple))


def test_first_time_notice_fires_once_and_persists(monkeypatch, tmp_path):
    cfg = tmp_path / "tokenomy.config.json"
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    icon = _FakeIcon()
    _reset_tray_state(monkeypatch, icon=icon)
    launcher._maybe_first_time_notice()
    assert len(icon.notes) == 1                  # 첫 호출 — 알림
    launcher._maybe_first_time_notice()
    assert len(icon.notes) == 1                  # 두 번째 — 안 띄움(영속 플래그)
    import json
    assert json.loads(cfg.read_text(encoding="utf-8"))["tray_notice_seen"] is True


def test_first_time_notice_mentions_mini_view(monkeypatch, tmp_path):
    """첫 안내가 미니 뷰 발견성을 위해 '미니 뷰'를 한 줄 언급해야 한다(ADR 0008 발견성)."""
    cfg = tmp_path / "tokenomy.config.json"
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    icon = _FakeIcon()
    _reset_tray_state(monkeypatch, icon=icon)
    launcher._maybe_first_time_notice()
    title, message = icon.notes[0]
    assert "미니 뷰" in (message + (title or ""))


def test_build_tray_uses_default_open_and_quit_items(monkeypatch):
    items = []
    class FakeMenuItem:
        def __init__(self, text, action, checked=None, default=False, **kw):
            items.append((text, action, checked, default))
    class FakeMenu:
        def __init__(self, *menuitems): self.menuitems = menuitems
    class FakeIconCls:
        def __init__(self, name, image, title, menu=None):
            self.name, self.image, self.title, self.menu = name, image, title, menu
    fake_pystray = type("M", (), {"Menu": FakeMenu, "MenuItem": FakeMenuItem, "Icon": FakeIconCls})
    monkeypatch.setitem(__import__("sys").modules, "pystray", fake_pystray)
    monkeypatch.setattr(launcher, "_tray_image", lambda: "IMG")
    icon = launcher._build_tray()
    by_text = {t: (a, c, d) for (t, a, c, d) in items}
    assert by_text["열기"][0] is launcher._on_open and by_text["열기"][2] is True   # default
    assert by_text["종료"][0] is launcher._on_quit
    assert icon.title == "Tokenomy"


def test_build_tray_has_no_mini_toggle(monkeypatch):
    """배타 전환 — 트레이엔 '미니 뷰' 토글이 없다('열기'=마지막 뷰, 전환은 창 버튼)."""
    items = []
    class FakeMenuItem:
        def __init__(self, text, action, checked=None, default=False, **kw):
            items.append(text)
    class FakeMenu:
        def __init__(self, *menuitems): pass
    class FakeIconCls:
        def __init__(self, name, image, title, menu=None): self.title = title
    fake_pystray = type("M", (), {"Menu": FakeMenu, "MenuItem": FakeMenuItem, "Icon": FakeIconCls})
    monkeypatch.setitem(__import__("sys").modules, "pystray", fake_pystray)
    monkeypatch.setattr(launcher, "_tray_image", lambda: "IMG")
    launcher._build_tray()
    assert items == ["열기", "종료"]


def test_on_open_restores_last_view(monkeypatch):
    """트레이 '열기'/기본 클릭 → 마지막 본 뷰 복원(_restore_last_view)."""
    called = []
    monkeypatch.setattr(launcher, "_restore_last_view", lambda: called.append(True))
    launcher._on_open()
    assert called == [True]


class _Slot:
    """pywebview window.events 슬롯 페이크 — += 핸들러를 로그에 (이름, 핸들러)로 기록."""
    def __init__(self, name, log): self._name = name; self._log = log
    def __iadd__(self, handler): self._log.append((self._name, handler)); return self


class _LaunchWin:
    def __init__(self, log, kind):
        self.kind = kind; self.calls = []
        self.events = type("E", (), {})()
        self.events.closing = _Slot("closing", log)
        self.events.moved = _Slot("moved", log)
    def show(self): self.calls.append("show")
    def hide(self): self.calls.append("hide")


def _install_fake_webview(monkeypatch, log, created):
    """create_window을 큰 창/미니로 구분해 페이크 창을 돌려주고 호출 kwargs를 기록.
    screens 속성은 일부러 없음 → _resolve_mini_xy가 (None,None)로 폴백(테스트 결정성)."""
    def create_window(title, url=None, **k):
        kind = "mini" if (url and url.endswith("/mini")) else "main"
        created.append({"kind": kind, "url": url, "kw": k})
        return _LaunchWin(log, kind)
    def start(cb=None):
        log.append(("start",))
        if cb is not None:
            cb()                       # GUI 루프 시작 직후 콜백(_on_gui_start) 실행 모사
    fake = type("W", (), {
        "create_window": staticmethod(create_window),
        "start": staticmethod(start),
    })
    monkeypatch.setitem(sys.modules, "webview", fake)
    return fake


def _isolate_config(monkeypatch, tmp_path, body="{}"):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(body, encoding="utf-8")
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    return cfg


def _fresh_state(quitting=False):
    """_launch_window용 초기 _tray_state(배타 전환 — current_view/port 포함, 미니 미생성)."""
    return {"window": None, "icon": None, "quitting": quitting,
            "mini": None, "current_view": "main", "mini_visible": False, "port": None}


def test_launch_window_wires_tray_and_stops_on_exit(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    log, created = [], []
    _install_fake_webview(monkeypatch, log, created)

    class FakeIcon:
        def __init__(self): self.stopped = False
        def run(self): pass
        def stop(self): self.stopped = True
    icon = FakeIcon()
    monkeypatch.setattr(launcher, "_build_tray", lambda: icon)
    monkeypatch.setattr(launcher, "_tray_state", _fresh_state(quitting=True))

    class FakeThread:
        def __init__(self, target=None, daemon=None, **k): self.target = target
        def start(self):
            if self.target is icon.run: icon.run()
    monkeypatch.setattr(launcher.threading, "Thread", FakeThread)
    monkeypatch.setattr("tokenomy.web.control.set_show_callback", lambda fn: log.append(("cb", fn)))

    launcher._launch_window(9999)
    assert ("closing", launcher._on_closing) in log          # 큰 창 닫기 핸들러
    assert ("cb", launcher._restore_last_view) in log        # 복원 콜백 = 마지막 뷰
    assert ("start",) in log                                  # GUI 루프 진입
    assert icon.stopped is True


def test_launch_window_does_not_create_mini_at_startup(monkeypatch, tmp_path):
    """배타 전환 — 시작 시 미니 창을 만들지 않는다(흰 창 차단; lazy create)."""
    _isolate_config(monkeypatch, tmp_path)            # last_view 미설정 → main
    log, created = [], []
    _install_fake_webview(monkeypatch, log, created)
    monkeypatch.setattr(launcher, "_build_tray", lambda: type("I", (), {"run": lambda s: None, "stop": lambda s: None})())
    monkeypatch.setattr(launcher, "_tray_state", _fresh_state(quitting=True))
    monkeypatch.setattr(launcher.threading, "Thread", type("T", (), {"__init__": lambda s, **k: None, "start": lambda s: None}))
    monkeypatch.setattr("tokenomy.web.control.set_show_callback", lambda fn: None)

    launcher._launch_window(9999)
    assert all(c["kind"] != "mini" for c in created)  # 미니 미생성
    assert launcher._tray_state["mini"] is None
    assert launcher._tray_state["current_view"] == "main"


def test_launch_window_starts_in_mini_when_last_view_mini(monkeypatch, tmp_path):
    """last_view='mini' → GUI 시작 콜백(_on_gui_start)이 미니로 전환(큰 창 숨김 + 미니 lazy 생성)."""
    _isolate_config(monkeypatch, tmp_path, '{"mini_view": {"last_view": "mini"}}')
    log, created = [], []
    _install_fake_webview(monkeypatch, log, created)
    monkeypatch.setattr(launcher, "_build_tray", lambda: type("I", (), {"run": lambda s: None, "stop": lambda s: None})())
    monkeypatch.setattr(launcher, "_tray_state", _fresh_state(quitting=True))
    monkeypatch.setattr(launcher.threading, "Thread", type("T", (), {"__init__": lambda s, **k: None, "start": lambda s: None}))
    monkeypatch.setattr("tokenomy.web.control.set_show_callback", lambda fn: None)
    monkeypatch.setattr(launcher, "_persist_mini", lambda **k: None)

    launcher._launch_window(9999)
    assert any(c["kind"] == "mini" for c in created)            # 미니 lazy 생성됨
    assert launcher._tray_state["current_view"] == "mini"


def test_launch_window_degrades_to_single_shot_when_tray_unavailable(monkeypatch, tmp_path):
    """_build_tray가 실패하면(pystray/Pillow 미가용) closing 핸들러도 미니 창도 만들지 않아
    X=종료가 유지된다(복원 불가 함정 방지). webview.start는 여전히 호출."""
    _isolate_config(monkeypatch, tmp_path)
    log, created = [], []
    _install_fake_webview(monkeypatch, log, created)

    def boom():
        raise RuntimeError("pystray 미가용")
    monkeypatch.setattr(launcher, "_build_tray", boom)
    monkeypatch.setattr(launcher, "_tray_state", _fresh_state(quitting=True))
    monkeypatch.setattr("tokenomy.web.control.set_show_callback",
                        lambda fn: log.append(("cb", fn)))

    launcher._launch_window(9999)
    assert ("start",) in log                          # GUI 루프는 여전히 진입
    assert not any(e[0] == "closing" for e in log)    # closing 미부착 → X=종료 유지
    assert not any(e[0] == "cb" for e in log)          # show 콜백 미등록
    assert all(c["kind"] != "mini" for c in created)  # 미니 창도 안 만듦(강등)


# ──────────────────────────────────────────────
# Task 5: 미니 뷰(ADR 0008) — 위치 계산·영속·핸들러·브리지
# ──────────────────────────────────────────────

def _reset_mini_state(monkeypatch, window=None, mini=None, current_view="main",
                      quitting=False, port=9999, mini_visible=False):
    monkeypatch.setattr(launcher, "_tray_state",
                        {"window": window, "icon": None, "quitting": quitting,
                         "mini": mini, "current_view": current_view,
                         "mini_visible": mini_visible, "port": port})


# ── 위치 계산(순수) ──────────────────────────────────────────────────────────
def test_clamp_position_in_bounds_unchanged():
    assert launcher._clamp_position(100, 200, 1920, 1080, 300, 160) == (100, 200)


def test_clamp_position_off_screen_pulled_in():
    # 우/하단 밖 → 창이 화면에 들어오게 당김
    assert launcher._clamp_position(1900, 1070, 1920, 1080, 300, 160) == (1620, 920)
    # 음수 → 0
    assert launcher._clamp_position(-50, -10, 1920, 1080, 300, 160) == (0, 0)


def test_clamp_position_none_when_unset():
    assert launcher._clamp_position(None, 5, 1920, 1080, 300, 160) is None
    assert launcher._clamp_position(5, None, 1920, 1080, 300, 160) is None


def test_default_mini_position_bottom_right():
    # 기본 위치 = 우하단(마진 16) — 작업표시줄 위 코너
    assert launcher._default_mini_position(1920, 1080, 300, 160, margin=16) == (1604, 904)


# ── 설정 영속 ────────────────────────────────────────────────────────────────
def test_persist_mini_writes_last_view(monkeypatch, tmp_path):
    cfg = tmp_path / "c.json"
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    launcher._persist_mini(last_view="mini")
    import json
    assert json.loads(cfg.read_text(encoding="utf-8"))["mini_view"]["last_view"] == "mini"


def test_persist_mini_merges_position_keeping_last_view(monkeypatch, tmp_path):
    cfg = tmp_path / "c.json"
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    launcher._persist_mini(last_view="mini")
    launcher._persist_mini(x=10, y=20)          # 위치만 저장해도 last_view 보존
    import json
    mv = json.loads(cfg.read_text(encoding="utf-8"))["mini_view"]
    assert mv == {"last_view": "mini", "x": 10, "y": 20}


def test_persist_mini_ignores_none_fields(monkeypatch, tmp_path):
    cfg = tmp_path / "c.json"
    monkeypatch.setenv("TOKENOMY_CONFIG", str(cfg))
    launcher._persist_mini(last_view="mini", x=5, y=6)
    launcher._persist_mini(x=None, y=99)        # None은 무시, 99만 반영(merge None-filter)
    import json
    mv = json.loads(cfg.read_text(encoding="utf-8"))["mini_view"]
    assert mv == {"last_view": "mini", "x": 5, "y": 99}


# ── lazy 생성 ────────────────────────────────────────────────────────────────
def test_ensure_mini_lazy_creates_visible_frameless(monkeypatch, tmp_path):
    """미니 없으면 보이는 프레임리스 on_top 창으로 lazy 생성(hidden 아님 — 흰 창 회피)."""
    _isolate_config(monkeypatch, tmp_path)
    log, created = [], []
    _install_fake_webview(monkeypatch, log, created)
    _reset_mini_state(monkeypatch, mini=None, port=9999)
    monkeypatch.setattr(launcher, "_persist_mini", lambda **k: None)
    mini = launcher._ensure_mini()
    c = next(c for c in created if c["kind"] == "mini")
    assert c["url"].endswith("/mini")
    assert c["kw"].get("frameless") is True and c["kw"].get("on_top") is True
    assert c["kw"].get("easy_drag") is True
    assert "hidden" not in c["kw"]                       # 보이게 생성 → 흰 창 차단
    assert launcher._tray_state["mini"] is mini
    assert ("closing", launcher._on_mini_closing) in log  # 닫기=트레이숨김 와이어링
    assert ("moved", launcher._save_mini_position) in log # 위치 저장 와이어링


def test_ensure_mini_reuses_existing(monkeypatch):
    """이미 있으면 재생성하지 않고 그대로 반환(webview.create_window 미호출)."""
    w = _FakeWindow()
    _reset_mini_state(monkeypatch, mini=w)
    assert launcher._ensure_mini() is w


# ── 재표시 시 강제 갱신(stale 방지) ───────────────────────────────────────────
def test_show_mini_window_refreshes_content_on_reshow(monkeypatch):
    """이미 생성된 미니를 다시 보일 땐 내용을 강제 재요청해야 한다(stale 방지).
    htmx load 트리거는 최초 lazy 생성 1회만 발동하므로, 배타 전환·트레이 복원으로
    재표시할 때 명시적 재요청이 없으면 공식 사용량이 숨겨진 동안의 옛 스냅샷으로 굳는다."""
    w_mini = _FakeWindow()
    _reset_mini_state(monkeypatch, mini=w_mini, current_view="mini")
    refreshed = []
    monkeypatch.setattr(launcher, "_refresh_mini_content", lambda m: refreshed.append(m))
    launcher._show_mini_window()
    assert "show" in w_mini.calls
    assert launcher._tray_state["mini_visible"] is True
    assert refreshed == [w_mini]                       # 재표시 → 내용 강제 갱신


def test_show_mini_window_skips_refresh_on_first_create(monkeypatch, tmp_path):
    """최초 lazy 생성 시엔 load 트리거가 첫 렌더를 담당하므로 강제 재요청을 하지 않는다
    (htmx 미준비 상태에서의 eval 회피)."""
    _isolate_config(monkeypatch, tmp_path)
    log, created = [], []
    _install_fake_webview(monkeypatch, log, created)
    _reset_mini_state(monkeypatch, mini=None, port=9999)
    monkeypatch.setattr(launcher, "_persist_mini", lambda **k: None)
    refreshed = []
    monkeypatch.setattr(launcher, "_refresh_mini_content", lambda m: refreshed.append(m))
    launcher._show_mini_window()
    assert launcher._tray_state["mini"] is not None      # lazy 생성됨
    assert refreshed == []                               # 최초 생성 — 재요청 없음


def test_refresh_mini_content_requests_section_via_htmx(monkeypatch):
    """미니 내용 강제 갱신 = #mini-section을 htmx로 재요청(DB만 다시 읽어도 메인이 받아 둔
    최신 공식값이 반영된다). evaluate_js로 /mini/section 재요청을 지시해야 한다."""
    w = _FakeWindow()
    launcher._refresh_mini_content(w)
    js_calls = [c[1] for c in w.calls if isinstance(c, tuple) and c[0] == "js"]
    assert any("/mini/section" in js for js in js_calls)


def test_refresh_mini_content_swallows_eval_errors(monkeypatch):
    """evaluate_js 실패(창 미준비 등)는 조용히 삼킨다 — 표시 흐름을 깨면 안 된다."""
    class _Boom:
        def evaluate_js(self, js): raise RuntimeError("not ready")
    launcher._refresh_mini_content(_Boom())             # 예외 전파 없이 반환


# ── 전환: to_mini / to_main / hide_to_tray / 복원 ────────────────────────────
def test_to_mini_hides_main_shows_mini_persists(monkeypatch):
    w_main, w_mini = _FakeWindow(), _FakeWindow()
    _reset_mini_state(monkeypatch, window=w_main, mini=w_mini, current_view="main")
    saved = []
    monkeypatch.setattr(launcher, "_persist_mini", lambda **k: saved.append(k))
    launcher._to_mini()
    assert "hide" in w_main.calls and "show" in w_mini.calls
    assert launcher._tray_state["current_view"] == "mini"
    assert launcher._tray_state["mini_visible"] is True
    assert {"last_view": "mini"} in saved


def test_to_main_hides_mini_restores_main_persists(monkeypatch):
    w_main, w_mini = _FakeWindow(), _FakeWindow()
    _reset_mini_state(monkeypatch, window=w_main, mini=w_mini, current_view="mini")
    saved, shown = [], []
    monkeypatch.setattr(launcher, "_persist_mini", lambda **k: saved.append(k))
    monkeypatch.setattr(launcher, "_show_window", lambda: shown.append(True))
    launcher._to_main()
    assert "hide" in w_mini.calls
    assert launcher._tray_state["current_view"] == "main"
    assert launcher._tray_state["mini_visible"] is False
    assert {"last_view": "main"} in saved
    assert shown == [True]                               # 큰 창 복원(ingest 1회 동반)


def test_resize_mini_ignored_after_returning_to_main(monkeypatch):
    """미니 내부 폴링의 늦은 resize 요청이 일반뷰 상태에서 숨긴 미니창을 건드리면 안 된다."""
    w_main, w_mini = _FakeWindow(), _FakeWindow()
    _reset_mini_state(monkeypatch, window=w_main, mini=w_mini, current_view="mini")
    monkeypatch.setattr(launcher, "_persist_mini", lambda **k: None)
    monkeypatch.setattr(launcher, "_show_window", lambda: None)

    launcher._to_main()
    launcher._resize_mini(222)

    assert w_mini.calls == ["hide"]


def test_hide_mini_to_tray_keeps_view_mini(monkeypatch):
    """미니 ✕/X → 트레이 숨김. current_view는 'mini' 유지 → 다음 복원도 미니."""
    w_mini = _FakeWindow()
    _reset_mini_state(monkeypatch, mini=w_mini, current_view="mini", mini_visible=True)
    monkeypatch.setattr(launcher, "_maybe_first_time_notice", lambda: None)
    launcher._hide_mini_to_tray()
    assert "hide" in w_mini.calls
    assert launcher._tray_state["current_view"] == "mini"
    assert launcher._tray_state["mini_visible"] is False


def test_on_mini_closing_hides_to_tray_and_cancels(monkeypatch):
    w_mini = _FakeWindow()
    _reset_mini_state(monkeypatch, mini=w_mini, current_view="mini")
    monkeypatch.setattr(launcher, "_maybe_first_time_notice", lambda: None)
    assert launcher._on_mini_closing() is False         # 파괴 취소(트레이 숨김)
    assert "hide" in w_mini.calls


def test_on_mini_closing_allows_close_when_quitting(monkeypatch):
    w_mini = _FakeWindow()
    _reset_mini_state(monkeypatch, mini=w_mini, current_view="mini", quitting=True)
    assert launcher._on_mini_closing() is True          # 앱 종료 중 → 진짜 닫기
    assert "hide" not in w_mini.calls


def test_restore_last_view_main(monkeypatch):
    _reset_mini_state(monkeypatch, current_view="main")
    seen = []
    monkeypatch.setattr(launcher, "_show_window", lambda: seen.append("main"))
    monkeypatch.setattr(launcher, "_show_mini_window", lambda: seen.append("mini"))
    launcher._restore_last_view()
    assert seen == ["main"]


def test_restore_last_view_mini(monkeypatch):
    _reset_mini_state(monkeypatch, current_view="mini")
    seen = []
    monkeypatch.setattr(launcher, "_show_window", lambda: seen.append("main"))
    monkeypatch.setattr(launcher, "_show_mini_window", lambda: seen.append("mini"))
    launcher._restore_last_view()
    assert seen == ["mini"]


def test_resize_mini_uses_mini_width(monkeypatch):
    w = _FakeWindow()
    _reset_mini_state(monkeypatch, mini=w, current_view="mini", mini_visible=True)
    launcher._resize_mini(150)
    assert ("resize", launcher.MINI_WIDTH, 150) in w.calls


def test_resize_mini_ignores_bad_height(monkeypatch):
    w = _FakeWindow()
    _reset_mini_state(monkeypatch, mini=w)
    launcher._resize_mini(0)
    launcher._resize_mini("x")
    assert not any(isinstance(c, tuple) and c[0] == "resize" for c in w.calls)


def test_save_mini_position_persists_xy(monkeypatch):
    _reset_mini_state(monkeypatch)
    saved = []
    monkeypatch.setattr(launcher, "_persist_mini", lambda **k: saved.append(k))
    launcher._save_mini_position(640, 480)
    assert {"x": 640, "y": 480} in saved


# ── JS 브리지(Api) ───────────────────────────────────────────────────────────
def test_api_to_mini_delegates(monkeypatch):
    called = []
    monkeypatch.setattr(launcher, "_to_mini", lambda: called.append(True))
    launcher.Api().to_mini()
    assert called == [True]


def test_api_to_main_delegates(monkeypatch):
    called = []
    monkeypatch.setattr(launcher, "_to_main", lambda: called.append(True))
    launcher.Api().to_main()
    assert called == [True]


def test_api_hide_to_tray_delegates(monkeypatch):
    called = []
    monkeypatch.setattr(launcher, "_hide_mini_to_tray", lambda: called.append(True))
    launcher.Api().hide_to_tray()
    assert called == [True]


def test_api_resize_mini_delegates(monkeypatch):
    seen = []
    monkeypatch.setattr(launcher, "_resize_mini", lambda h: seen.append(h))
    launcher.Api().resize_mini(222)
    assert seen == [222]


# ──────────────────────────────────────────────
# ADR 0013: 미니 뷰 비가용 플랫폼(Linux) 게이트
# ──────────────────────────────────────────────

def test_to_mini_noop_when_mini_view_unavailable(monkeypatch):
    """Linux 등 미니뷰 비가용 플랫폼 — _to_mini는 완전 no-op(큰 창 유지·전환·영속 없음).
    Wayland에서 미니 창을 띄우면 핵심 속성이 깨지므로 진입 자체를 막는다(ADR 0013)."""
    w_main, w_mini = _FakeWindow(), _FakeWindow()
    _reset_mini_state(monkeypatch, window=w_main, mini=w_mini, current_view="main")
    monkeypatch.setattr(launcher, "mini_view_available", lambda: False)
    saved = []
    monkeypatch.setattr(launcher, "_persist_mini", lambda **k: saved.append(k))
    launcher._to_mini()
    assert w_main.calls == []                           # 큰 창 안 숨김
    assert w_mini.calls == []                           # 미니 안 보임
    assert launcher._tray_state["current_view"] == "main"
    assert saved == []                                 # 영속 없음


def test_to_mini_still_works_when_available(monkeypatch):
    """가용 플랫폼(Windows)에선 기존대로 전환된다 — 게이트가 정상 경로를 막지 않는지 회귀 가드."""
    w_main, w_mini = _FakeWindow(), _FakeWindow()
    _reset_mini_state(monkeypatch, window=w_main, mini=w_mini, current_view="main")
    monkeypatch.setattr(launcher, "mini_view_available", lambda: True)
    monkeypatch.setattr(launcher, "_persist_mini", lambda **k: None)
    launcher._to_mini()
    assert "hide" in w_main.calls and "show" in w_mini.calls
    assert launcher._tray_state["current_view"] == "mini"


def test_tray_icon_name_windows_uses_ico():
    assert launcher._tray_icon_name("win32") == "assets/tokenomy.ico"


def test_tray_icon_name_linux_uses_png():
    # pystray AppIndicator(Linux, ADR 0013)는 .png를 쓴다(.ico 비호환 회피).
    assert launcher._tray_icon_name("linux") == "assets/tokenomy.png"


def test_tray_icon_name_macos_uses_png():
    assert launcher._tray_icon_name("darwin") == "assets/tokenomy.png"


def test_tray_icon_name_defaults_to_current_platform(monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    assert launcher._tray_icon_name() == "assets/tokenomy.png"
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    assert launcher._tray_icon_name() == "assets/tokenomy.ico"


def test_tray_image_uses_platform_icon_name(monkeypatch):
    """_tray_image는 _tray_icon_name()이 고른 리소스를 PIL로 연다(플랫폼 분기 와이어링)."""
    import types
    monkeypatch.setattr(launcher, "_tray_icon_name", lambda: "assets/tokenomy.png")
    opened = {}
    fake_Image = types.SimpleNamespace(open=lambda p: opened.setdefault("path", p))
    monkeypatch.setitem(sys.modules, "PIL", types.SimpleNamespace(Image=fake_Image))
    monkeypatch.setattr("tokenomy.paths.resource_path", lambda rel: f"/RES/{rel}")
    launcher._tray_image()
    assert opened["path"] == "/RES/assets/tokenomy.png"


def _tray_branch_fakes(monkeypatch, tmp_path, platform):
    """플랫폼별 트레이 기동 분기 테스트 공용 셋업 — FakeIcon(run/run_detached/stop) 반환."""
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setattr(launcher.sys, "platform", platform)
    log, created = [], []
    _install_fake_webview(monkeypatch, log, created)

    class FakeIcon:
        def __init__(self): self.ran = False; self.detached = False; self.stopped = False
        def run(self): self.ran = True
        def run_detached(self): self.detached = True
        def stop(self): self.stopped = True
    icon = FakeIcon()
    monkeypatch.setattr(launcher, "_build_tray", lambda: icon)
    monkeypatch.setattr(launcher, "_tray_state", _fresh_state(quitting=True))
    monkeypatch.setattr(launcher, "_start_background_poll", lambda: None)
    monkeypatch.setattr("tokenomy.web.control.set_show_callback", lambda fn: None)
    return icon, log


def test_launch_window_tray_detached_on_linux(monkeypatch, tmp_path):
    """Linux(GTK) — pystray와 pywebview가 같은 GLib 기본 메인 컨텍스트를 공유해야 한다.
    icon.run()을 데몬 스레드로 돌리면 메인 스레드 webview GTK 루프와 충돌해(GLib-GIO-CRITICAL:
    can not acquire the default main context) 창이 안 뜬다 → run_detached()로 메인 스레드에서
    루프 없이 붙이고, 트레이용 데몬 스레드는 만들지 않는다(ADR 0013)."""
    icon, log = _tray_branch_fakes(monkeypatch, tmp_path, "linux")
    tray_targets = []
    class FakeThread:
        def __init__(self, target=None, daemon=None, **k): self.target = target; tray_targets.append(target)
        def start(self):
            if self.target == icon.run: icon.run()   # 바인드 메서드는 ==로 비교(is는 매 접근 새 객체)
    monkeypatch.setattr(launcher.threading, "Thread", FakeThread)

    launcher._launch_window(9999)
    assert icon.detached is True              # run_detached로 붙임
    assert icon.ran is False                  # 데몬 스레드 icon.run 안 함
    assert icon.run not in tray_targets       # 트레이용 스레드 미생성
    assert ("start",) in log                  # webview 루프 진입(창 표시)
    assert icon.stopped is True               # 종료 시 정리


def test_launch_window_tray_thread_on_windows(monkeypatch, tmp_path):
    """Windows — 트레이는 자체 Win32 메시지 루프라 기존대로 데몬 스레드에서 icon.run (run_detached 미사용)."""
    icon, log = _tray_branch_fakes(monkeypatch, tmp_path, "win32")
    class FakeThread:
        def __init__(self, target=None, daemon=None, **k): self.target = target
        def start(self):
            if self.target == icon.run: icon.run()   # 바인드 메서드는 ==로 비교(is는 매 접근 새 객체)
    monkeypatch.setattr(launcher.threading, "Thread", FakeThread)

    launcher._launch_window(9999)
    assert icon.ran is True                   # 데몬 스레드에서 icon.run
    assert icon.detached is False             # run_detached 미사용


def test_launch_window_forces_main_when_mini_unavailable(monkeypatch, tmp_path):
    """미니뷰 비가용(Linux) — config last_view='mini'여도 큰 창으로 시작하고 미니를 안 만든다.
    current_view 시드를 'main'으로 clamp해 트레이 '열기'·GUI 시작 콜백이 미니로 새지 않게 한다."""
    _isolate_config(monkeypatch, tmp_path, '{"mini_view": {"last_view": "mini"}}')
    log, created = [], []
    _install_fake_webview(monkeypatch, log, created)
    monkeypatch.setattr(launcher, "mini_view_available", lambda: False)
    monkeypatch.setattr(launcher, "_build_tray", lambda: type("I", (), {"run": lambda s: None, "stop": lambda s: None})())
    monkeypatch.setattr(launcher, "_tray_state", _fresh_state(quitting=True))
    monkeypatch.setattr(launcher.threading, "Thread", type("T", (), {"__init__": lambda s, **k: None, "start": lambda s: None}))
    monkeypatch.setattr("tokenomy.web.control.set_show_callback", lambda fn: None)
    monkeypatch.setattr(launcher, "_persist_mini", lambda **k: None)

    launcher._launch_window(9999)
    assert launcher._tray_state["current_view"] == "main"   # 비가용 → main으로 clamp
    assert all(c["kind"] != "mini" for c in created)         # 미니 미생성
