def test_time_window_setting(monkeypatch, tmp_path):
    from app.storage import db
    from app.services.settings import update_recent_hours
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "test.sqlite3")
    result = update_recent_hours(36)
    assert result["recent_hours"] == 36
