import socket

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


def test_build_tray_uses_default_open_and_quit_items(monkeypatch):
    items = []
    class FakeMenuItem:
        def __init__(self, text, action, default=False):
            items.append((text, action, default))
    class FakeMenu:
        def __init__(self, *menuitems): self.menuitems = menuitems
    class FakeIconCls:
        def __init__(self, name, image, title, menu=None):
            self.name, self.image, self.title, self.menu = name, image, title, menu
    fake_pystray = type("M", (), {"Menu": FakeMenu, "MenuItem": FakeMenuItem, "Icon": FakeIconCls})
    monkeypatch.setitem(__import__("sys").modules, "pystray", fake_pystray)
    monkeypatch.setattr(launcher, "_tray_image", lambda: "IMG")
    icon = launcher._build_tray()
    texts = {t: (a, d) for (t, a, d) in items}
    assert texts["열기"][0] is launcher._on_open and texts["열기"][1] is True   # default
    assert texts["종료"][0] is launcher._on_quit
    assert icon.title == "Tokenomy"
