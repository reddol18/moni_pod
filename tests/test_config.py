from moni_pod import config


def test_env_var_wins(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("RUNPOD_API_KEY=from-file\n")
    monkeypatch.setenv("RUNPOD_API_KEY", "from-env")
    assert config.load_api_key() == "from-env"


def test_dotenv_in_cwd(tmp_path):
    (tmp_path / ".env").write_text('# comment\nOTHER=1\nexport RUNPOD_API_KEY="quoted-value"\n')
    assert config.load_api_key() == "quoted-value"


def test_dotenv_in_project_dir(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".env").write_text("RUNPOD_API_KEY=proj-value\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    assert config.load_api_key() == "proj-value"


def test_missing_or_empty(tmp_path):
    assert config.load_api_key() is None
    (tmp_path / ".env").write_text("RUNPOD_API_KEY=\n")
    assert config.load_api_key() is None


def test_home_override(isolated_home):
    assert config.home_dir() == isolated_home


def test_package_root_env_found_from_another_cwd(tmp_path, monkeypatch):
    """Task 0002 bug: run from another project folder, moni_pod's own .env must still be found."""
    (config.PACKAGE_ROOT / ".env").write_text("RUNPOD_API_KEY=from-package\n")
    other = tmp_path / "other-project"
    other.mkdir()
    monkeypatch.chdir(other)
    assert config.load_api_key() == "from-package"


def test_plugin_root_first_and_other_project_never_wins(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / ".env").write_text("RUNPOD_API_KEY=from-plugin\n")
    (config.PACKAGE_ROOT / ".env").write_text("RUNPOD_API_KEY=from-package\n")
    (tmp_path / ".env").write_text("RUNPOD_API_KEY=from-cwd-project\n")  # cwd = tmp_path
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
    assert config.load_api_key() == "from-plugin"
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT")
    assert config.load_api_key() == "from-package"


def test_home_env_before_cwd(isolated_home, tmp_path):
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / ".env").write_text("RUNPOD_API_KEY=from-home\n")
    (tmp_path / ".env").write_text("RUNPOD_API_KEY=from-cwd\n")
    assert config.load_api_key() == "from-home"


def test_not_found_message_lists_places(tmp_path):
    msg = config.key_not_found_message()
    assert "RUNPOD_API_KEY not found" in msg and str(config.PACKAGE_ROOT) in msg and "recommended" in msg
