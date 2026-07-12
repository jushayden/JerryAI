from config import load_config, load_profile, platform_status


def test_empty_env_all_platforms_unavailable(tmp_config):
    status = platform_status(tmp_config)
    assert set(status) == {"x", "youtube", "instagram"}
    for platform, (ready, reason) in status.items():
        assert ready is False, platform
        assert reason  # every unavailable platform explains itself
    assert "SOCIAL_SETUP.md" in status["youtube"][1]
    assert "setup_instagram.py" in status["instagram"][1]


def test_defaults(tmp_config):
    assert tmp_config.ollama_model == "qwen2.5"
    assert tmp_config.ollama_num_ctx == 8192
    assert tmp_config.approval_timeout_s == 300
    assert tmp_config.telegram_owner_id is None


def test_dirs_created(tmp_config):
    for d in (tmp_config.workspace_dir, tmp_config.sessions_dir,
              tmp_config.downloads_dir, tmp_config.state_dir):
        assert d.is_dir()


def test_env_vars_read(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "12345")
    monkeypatch.setenv("YOUTUBE_API_KEY", "ytkey")
    cfg = load_config(env_file=tmp_path / "none.env", root=tmp_path)
    assert cfg.telegram_owner_id == 12345
    status = platform_status(cfg)
    assert status["youtube"][0] is True
    assert status["x"][0] is False


def test_bad_owner_id_is_none(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "not-a-number")
    cfg = load_config(env_file=tmp_path / "none.env", root=tmp_path)
    assert cfg.telegram_owner_id is None


def test_instagram_ready_when_stamp_exists(tmp_config):
    ig_dir = tmp_config.sessions_dir / "instagram"
    ig_dir.mkdir(parents=True)
    (ig_dir / ".login_ok").write_text("ok", encoding="utf-8")
    assert platform_status(tmp_config)["instagram"][0] is True


def test_load_profile_missing_returns_empty(tmp_path):
    assert load_profile(tmp_path / "nope.yaml") == {}


def test_load_profile_valid(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text("owner:\n  name: Arjun\nsocial:\n  x: {handle: arjun}\n", encoding="utf-8")
    prof = load_profile(p)
    assert prof["owner"]["name"] == "Arjun"
