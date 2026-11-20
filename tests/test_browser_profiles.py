from app.services import crawler_engine


def test_isolated_profile_patch_disables_cdp_and_keeps_login(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "base_config.py"
    config_file.write_text("ENABLE_CDP_MODE = True\nSAVE_LOGIN_STATE = True\n", encoding="utf-8")
    monkeypatch.setattr(crawler_engine, "ENGINE_ROOT", tmp_path)
    crawler_engine._enable_isolated_profiles()
    result = config_file.read_text(encoding="utf-8")
    assert "ENABLE_CDP_MODE = False" in result
    assert "SAVE_LOGIN_STATE = True" in result


def test_isolated_profile_patch_fails_closed_on_unknown_config(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "base_config.py").write_text("ENABLE_CDP_MODE = maybe\n", encoding="utf-8")
    monkeypatch.setattr(crawler_engine, "ENGINE_ROOT", tmp_path)
    try:
        crawler_engine._enable_isolated_profiles()
    except RuntimeError as exc:
        assert "CDP" in str(exc)
    else:
        raise AssertionError("unknown upstream config must fail closed")
