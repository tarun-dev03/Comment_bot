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


def test_youtube_api_error_categorization():
    from app.youtube.live_chat import YouTubeAPIError

    e_auth = YouTubeAPIError("Auth error", 401)
    assert e_auth.is_auth is True
    assert e_auth.is_quota is False
    assert e_auth.is_transient is False

    e_quota = YouTubeAPIError("YouTube API daily quota exceeded", 403)
    assert e_quota.is_quota is True
    assert e_quota.is_auth is False
    assert e_quota.is_transient is False

    e_transient = YouTubeAPIError("Service unavailable", 503)
    assert e_transient.is_transient is True
    assert e_transient.is_auth is False
    assert e_transient.is_quota is False


def test_token_cache_invalidation():
    import time
    from app.auth.google_oauth import _access_token_cache, invalidate_token_cache

    _access_token_cache[999] = {"token": "test_token", "expires_at": time.time() + 3600}
    assert 999 in _access_token_cache
    invalidate_token_cache(999)
    assert 999 not in _access_token_cache
def test_extract_cookie_val():
    from app.youtube.live_chat import _extract_cookie_val

    cookie = "VISITOR_INFO1_LIVE=xyz123; SAPISID=abc456hash; SID=my_sid;"
    assert _extract_cookie_val(cookie, "SAPISID") == "abc456hash"
    assert _extract_cookie_val(cookie, "VISITOR_INFO1_LIVE") == "xyz123"
    assert _extract_cookie_val(cookie, "NON_EXISTENT") is None
