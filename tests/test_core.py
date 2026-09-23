import pytest

from app.bot.messages import MessageGenerator
from app.youtube.live_chat import parse_video_id


def test_parse_video_id_watch():
    assert parse_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_parse_video_id_short():
    assert parse_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_parse_video_id_live():
    assert parse_video_id("https://www.youtube.com/live/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_message_generator_no_immediate_repeat():
    gen = MessageGenerator()
    prev = None
    for _ in range(30):
        msg = gen.next_message()
        assert msg != prev
        prev = msg


def test_normalize_database_url_strips_ssl_params():
    from app.config import Settings
    from app.db import _postgres_connect_args

    raw_url = "postgres://user:pass@host:5432/dbname?ssl=true&sslmode=require"
    s = Settings(database_url=raw_url)
    assert s.database_url == "postgresql+asyncpg://user:pass@host:5432/dbname"

    args = _postgres_connect_args(s.database_url)
    assert "ssl" in args
    assert args["ssl"].check_hostname is False
    assert args["ssl"].verify_mode == 0


def test_custom_phrases_randomness_and_uniqueness():
    custom = ["My custom phrase 1", "My custom phrase 2"]
    gen = MessageGenerator(custom_phrases=custom)
    generated = set()
    for _ in range(50):
        msg = gen.next_message()
        assert msg.lower() not in generated, f"Duplicate message generated: {msg}"
        generated.add(msg.lower())


