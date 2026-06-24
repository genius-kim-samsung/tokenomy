"""Linux(Ubuntu 24.04 LTS) 배포 산출물 계약 테스트(ADR 0013).

셸 스크립트·.desktop 자체는 Windows pytest에서 실행할 수 없으므로, 파일 존재 + 핵심
불변식(apt 의존성·venv --system-site-packages·launcher 기동·.desktop 와이어링)만 검증해
삭제·회귀를 막고 의도를 고정한다. 실제 설치/실행 검증은 사내망 Ubuntu 박스(수동, 검증 항목)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_linux_artifacts_exist():
    for name in ("install.sh", "tokenomy.desktop", "start_tokenomy.sh"):
        p = ROOT / name
        assert p.exists() and p.read_text(encoding="utf-8").strip(), f"{name} 누락/빈 파일"


def test_shell_scripts_use_lf_endings():
    # CRLF로 커밋되면 Ubuntu에서 '#!/usr/bin/env bash\r' → bad interpreter로 깨진다.
    for name in ("install.sh", "start_tokenomy.sh"):
        raw = (ROOT / name).read_bytes()
        assert b"\r\n" not in raw, f"{name}에 CRLF — LF로 저장할 것(.gitattributes)"


def test_install_sh_uses_system_site_packages():
    # PyGObject를 pip로 빌드(libgirepository/cairo 빌드툴)하는 고통 회피 — venv가 apt
    # python3-gi를 보게 하는 핵심 결정(ADR 0013).
    txt = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "--system-site-packages" in txt


def test_install_sh_installs_required_apt_packages():
    txt = (ROOT / "install.sh").read_text(encoding="utf-8")
    for pkg in ("python3-gi", "gir1.2-gtk-3.0", "gir1.2-webkit2-4.1",
                "libwebkit2gtk-4.1-0", "libayatana-appindicator3-1",
                "gir1.2-ayatanaappindicator3-0.1"):
        assert pkg in txt, f"apt 패키지 누락: {pkg}"


def test_install_sh_registers_desktop_entry():
    txt = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "tokenomy.desktop" in txt
    assert "applications" in txt          # ~/.local/share/applications 등록


def test_desktop_entry_is_application_with_launcher_and_icon():
    txt = (ROOT / "tokenomy.desktop").read_text(encoding="utf-8")
    assert "Type=Application" in txt
    assert "start_tokenomy.sh" in txt     # Exec가 실행 스크립트를 가리킴
    assert "tokenomy.png" in txt          # 아이콘 = Linux용 png(ADR 0013)


def test_start_script_runs_launcher_via_venv():
    txt = (ROOT / "start_tokenomy.sh").read_text(encoding="utf-8")
    assert "tokenomy.launcher" in txt     # 네이티브 창+트레이 진입점(브라우저 아님)
    assert ".venv" in txt                 # venv python으로 실행
