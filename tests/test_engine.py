from app.services.crawler_engine import _build_command


def test_media_crawler_command_uses_persistent_login_and_comments():
    platform, command = _build_command({
        "platform": "xhs",
        "mode": "search",
        "keywords": "AI工具",
        "max_posts": 10,
        "max_comments_per_post": 80,
        "include_replies": True,
    })
    assert platform == "xhs"
    assert "--lt" in command and command[command.index("--lt") + 1] == "qrcode"
    assert "--headless" in command and command[command.index("--headless") + 1] == "false"
    assert "--get_comment" in command
    assert "--get_sub_comment" in command
    assert "--keywords" in command and command[command.index("--keywords") + 1] == "AI工具"


def test_media_crawler_rejects_empty_search():
    try:
        _build_command({"platform": "douyin", "mode": "search", "keywords": ""})
    except ValueError as exc:
        assert "关键词" in str(exc)
    else:
        raise AssertionError("empty keyword should be rejected")
