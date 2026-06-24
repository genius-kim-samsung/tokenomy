from tokenomy import paths


def test_data_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENOMY_DATA", str(tmp_path / "custom"))
    d = paths.data_dir()
    assert d == tmp_path / "custom"
    assert d.exists()


def test_data_dir_frozen_uses_home_dot_tokenomy(tmp_path, monkeypatch):
    monkeypatch.delenv("TOKENOMY_DATA", raising=False)
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
    d = paths.data_dir()
    assert d == tmp_path / ".tokenomy"
    assert d.exists()


def test_data_dir_source_is_repo_root(monkeypatch):
    monkeypatch.delenv("TOKENOMY_DATA", raising=False)
    monkeypatch.setattr(paths.sys, "frozen", False, raising=False)
    d = paths.data_dir()
    assert (d / "tokenomy" / "__init__.py").exists()  # repo 루트 표지


def test_path_helpers_under_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENOMY_DATA", str(tmp_path))
    assert paths.db_path() == tmp_path / "data" / "tokenomy.db"
    assert paths.archive_root() == tmp_path / "data" / "archive"
    assert paths.config_path() == tmp_path / "config" / "tokenomy.config.json"


def test_resource_path_source_finds_real_file(monkeypatch):
    monkeypatch.delattr(paths.sys, "_MEIPASS", raising=False)
    p = paths.resource_path("config/pricing.json")
    assert p.name == "pricing.json"
    assert p.exists()  # 소스 실행: repo의 실제 파일


def test_runtime_path_under_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENOMY_DATA", str(tmp_path))
    from tokenomy import paths
    assert paths.runtime_path() == tmp_path / "data" / "runtime.json"


# ── 미니 뷰 가용 플랫폼(ADR 0013) — Windows 전용 게이트 ────────────────────────
def test_mini_view_available_true_on_windows():
    # 미니 뷰는 frameless·on_top·절대좌표에 의존 → Windows에서만 제공.
    assert paths.mini_view_available("win32") is True


def test_mini_view_available_false_on_linux():
    # Wayland(GNOME)는 절대좌표·항상위를 막아 미니 뷰가 깨진다 → Linux 제외.
    assert paths.mini_view_available("linux") is False


def test_mini_view_available_false_on_macos():
    assert paths.mini_view_available("darwin") is False


def test_mini_view_available_defaults_to_current_platform(monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "linux")
    assert paths.mini_view_available() is False
    monkeypatch.setattr(paths.sys, "platform", "win32")
    assert paths.mini_view_available() is True
