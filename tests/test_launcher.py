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
