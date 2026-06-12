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
