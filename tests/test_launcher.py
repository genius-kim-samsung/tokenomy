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

        def start(self):
            calls["thread_started"] = True

    monkeypatch.setattr(launcher.threading, "Thread", FakeThread)

    launcher.main([])
    # 서버는 데몬 스레드로 기동, 창은 메인 스레드에서 직접
    assert calls.get("thread_target") is launcher._serve
    assert calls.get("thread_args") == (9999,)
    assert calls.get("thread_started") is True
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

        def start(self):
            calls["thread_started"] = True

    monkeypatch.setattr(launcher.threading, "Thread", FakeThread)

    launcher.main([])
    # 브라우저 오프너는 데몬 스레드, _serve는 메인 스레드 블로킹
    assert calls.get("thread_target") is launcher._open_browser_when_ready
    assert calls.get("thread_args") == (9999,)
    assert calls.get("thread_started") is True
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
