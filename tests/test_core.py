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

    cookie = 'VISITOR_INFO1_LIVE="xyz123"; SAPISID=abc456hash; SID=my_sid;'
    assert _extract_cookie_val(cookie, "SAPISID") == "abc456hash"
    assert _extract_cookie_val(cookie, "VISITOR_INFO1_LIVE") == "xyz123"
    assert _extract_cookie_val(cookie, "NON_EXISTENT") is None


@pytest.mark.anyio
async def test_send_innertube_missing_sapisid_raises_auth_error():
    from app.youtube.live_chat import YouTubeAPIError, send_innertube_live_chat_message

    cookie = "VISITOR_INFO1_LIVE=xyz123; SID=my_sid;"
    with pytest.raises(YouTubeAPIError) as exc_info:
        await send_innertube_live_chat_message(cookie, "dQw4w9WgXcQ", "Hello world")
    assert exc_info.value.is_auth is True
    assert "SAPISID" in str(exc_info.value)


def test_user_cookie_string_extraction():
    from app.youtube.live_chat import _extract_cookie_val

    user_cookie = (
        'wide=0; APISID=TBYvfa_d9AA2vkSwAxJyYP1oxsR; SAPISID=IFSOiPVeak-u96Mh4GJY794; '
        '__Secure-1PAPISID=I/ASak-u96Mh4GJY794; __Secure-3PAPISID94; '
        'SID=g.a0ZsngSsauareTVL_AW9gDIit8X2y8rgE5AACgYKAbgSARESFQHGX2MiBk4gBd-C8k8SL6uAJ0sz8RoVAUF8yKqsT8sk5C-GktYy4VvZL7s00076;'
    )
    sapisid = _extract_cookie_val(user_cookie, "SAPISID")
    assert sapisid == "IFSOiPVeak-u96Mh4GJY794"


@pytest.mark.anyio
async def test_innertube_sign_in_to_chat_detected(monkeypatch):
    import httpx
    from app.youtube.live_chat import YouTubeAPIError, send_innertube_live_chat_message

    class MockResp:
        status_code = 200
        text = '<html><div id="content">Sign in to chat</div></html>'

    async def mock_get(self, url, **kwargs):
        return MockResp()

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    cookie = "SAPISID=test_sapisid; SID=test_sid;"
    with pytest.raises(YouTubeAPIError) as exc_info:
        await send_innertube_live_chat_message(cookie, "dQw4w9WgXcQ", "Hello")
    assert "Sign in to chat" in str(exc_info.value)
    assert exc_info.value.is_auth is True


def test_find_send_chat_params_and_restrictions():
    from app.youtube.live_chat import _find_send_chat_params, _check_chat_restrictions

    nested_data = {
        "contents": {
            "liveChatRenderer": {
                "actionPanel": {
                    "liveChatMessageInputRenderer": {
                        "sendButton": {
                            "buttonRenderer": {
                                "serviceEndpoint": {
                                    "sendLiveChatMessageEndpoint": {
                                        "params": "my_exact_chat_param_token"
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    assert _find_send_chat_params(nested_data) == "my_exact_chat_param_token"

    restricted_data = {
        "actionPanel": {
            "liveChatRestrictedParticipationRenderer": {
                "message": {"runs": [{"text": "Subscribers only (10 minutes minimum)"}]}
            }
        }
    }
    assert _check_chat_restrictions(restricted_data) == "Subscribers only (10 minutes minimum)"


@pytest.mark.anyio
async def test_innertube_successful_chat_post(monkeypatch):
    import httpx
    from app.youtube.live_chat import send_innertube_live_chat_message

    class MockGetResp:
        status_code = 200
        text = '''<html>
        <script>var ytInitialData = {"sendLiveChatMessageEndpoint": {"params": "valid_token"}};</script>
        "INNERTUBE_API_KEY": "test_key",
        "INNERTUBE_CLIENT_VERSION": "2.20261002.01.00"
        </html>'''

    captured_payload = {}

    class MockPostResp:
        status_code = 200
        def json(self):
            return {
                "actions": [
                    {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {}}}}
                ]
            }

    async def mock_get(self, url, **kwargs):
        return MockGetResp()

    async def mock_post(self, url, **kwargs):
        nonlocal captured_payload
        captured_payload = kwargs.get("json", {})
        return MockPostResp()

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    cookie = "SAPISID=test_sapisid; SID=test_sid; HSID=test_hsid;"
    await send_innertube_live_chat_message(cookie, "dQw4w9WgXcQ", "Hello chat")

    assert captured_payload["richMessage"]["textSegments"][0]["text"] == "Hello chat"
    assert "clientMessageId" in captured_payload
    assert captured_payload["params"] == "valid_token"


