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

    raw_url = "postgres://user:pass@host:5432/dbname?ssl=true&sslmode=require&channel_binding=require"
    s = Settings(database_url=raw_url)
    assert s.database_url == "postgresql+asyncpg://user:pass@host:5432/dbname"

    args = _postgres_connect_args(s.database_url)
    assert "ssl" in args
    assert args["statement_cache_size"] == 0
    assert args["ssl"].check_hostname is False
    assert args["ssl"].verify_mode == 0


def test_empty_database_url_fallback_to_sqlite():
    from app.config import Settings
    from app.db import _postgres_connect_args

    for empty_val in ["", "   ", None]:
        s = Settings(database_url=empty_val)
        assert s.database_url == "sqlite+aiosqlite:///./comment_bot.db"
        assert _postgres_connect_args(s.database_url) == {}


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


def test_merge_cookies():
    from app.youtube.live_chat import _merge_cookies

    initial = "SID=sid123; HSID=hsid456; YSC=old_ysc;"
    updated = {"YSC": "new_ysc", "VISITOR_INFO1_LIVE": "visitor_new"}
    merged = _merge_cookies(initial, updated)
    assert "YSC=new_ysc" in merged
    assert "SID=sid123" in merged
    assert "HSID=hsid456" in merged
    assert "VISITOR_INFO1_LIVE=visitor_new" in merged


@pytest.mark.anyio
async def test_save_cookie_json_endpoint():
    import httpx
    from app.main import app
    from app.db import init_db, async_session_factory
    from app.models import User, UserCookie
    from sqlalchemy import select

    await init_db()
    async with async_session_factory() as db:
        res = await db.execute(select(User).where(User.email == "sync_test@example.com"))
        user = res.scalar_one_or_none()
        if not user:
            user = User(email="sync_test@example.com")
            db.add(user)
            await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/auth/cookie",
            json={"cookie": "SAPISID=fake_sync; SID=fake_sid;", "email": "sync_test@example.com"},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True
        assert data.get("user") == "sync_test@example.com"


@pytest.mark.anyio
async def test_innertube_session_caching_and_no_repeated_get(monkeypatch):
    import httpx
    from app.youtube.live_chat import send_innertube_live_chat_message, invalidate_innertube_cache

    invalidate_innertube_cache()
    get_count = 0
    post_count = 0

    class MockGetResp:
        status_code = 200
        text = '''<html>
        <script>var ytInitialData = {"sendLiveChatMessageEndpoint": {"params": "cached_token_123"}};</script>
        "INNERTUBE_API_KEY": "cached_api_key",
        "INNERTUBE_CLIENT_VERSION": "2.20261002.01.00"
        </html>'''

    class MockPostResp:
        status_code = 200
        def json(self):
            return {
                "actions": [
                    {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {}}}}
                ]
            }

    async def mock_get(self, url, **kwargs):
        nonlocal get_count
        get_count += 1
        return MockGetResp()

    async def mock_post(self, url, **kwargs):
        nonlocal post_count
        post_count += 1
        return MockPostResp()

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    cookie = "SAPISID=test_sapisid; SID=test_sid; HSID=test_hsid;"
    # First message: fetches page and caches parameters
    await send_innertube_live_chat_message(cookie, "cache_vid_1", "Message 1")
    assert get_count == 1
    assert post_count == 1

    # Second message: should reuse cached session and NOT perform a GET request!
    await send_innertube_live_chat_message(cookie, "cache_vid_1", "Message 2")
    assert get_count == 1  # No additional GET!
    assert post_count == 2


@pytest.mark.anyio
async def test_innertube_params_override_sign_in_text(monkeypatch):
    """If valid chat params are found in ytInitialData, presence of 'Sign in to chat' in unrelated text does not cause false error."""
    import httpx
    from app.youtube.live_chat import send_innertube_live_chat_message, invalidate_innertube_cache

    invalidate_innertube_cache()

    class MockGetResp:
        status_code = 200
        text = '''<html>
        <div>Sign in to chat</div> <!-- e.g. unrelated UI bundle or suggested stream banner -->
        <script>var ytInitialData = {"sendLiveChatMessageEndpoint": {"params": "valid_token_abc"}};</script>
        "INNERTUBE_API_KEY": "api_key_abc",
        "INNERTUBE_CLIENT_VERSION": "2.20261002.01.00"
        </html>'''

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
        return MockPostResp()

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    cookie = "SAPISID=test_sapisid; SID=test_sid; HSID=test_hsid;"
    res = await send_innertube_live_chat_message(cookie, "valid_vid", "Testing valid chat")
    assert "test_sapisid" in res


@pytest.mark.anyio
async def test_api_me_endpoint():
    import httpx
    from app.main import app
    from app.db import init_db

    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/me")
        assert resp.status_code == 200
        data = resp.json()
        assert "authenticated" in data
        assert data["authenticated"] is True
        assert "stream_url" in data


@pytest.mark.anyio
async def test_stream_url_saved_and_kept_until_updated():
    import httpx
    from app.main import app
    from app.db import init_db, async_session_factory
    from app.models import BotJob, User
    from sqlalchemy import select

    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        test_link_1 = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        save_resp = await client.post(
            "/api/settings/stream-url",
            json={"stream_url": test_link_1},
            headers={"Content-Type": "application/json"},
        )
        assert save_resp.status_code == 200
        assert save_resp.json()["ok"] is True
        assert save_resp.json()["stream_url"] == test_link_1
        assert save_resp.json()["video_id"] == "dQw4w9WgXcQ"

        # Verify GET returns the saved link
        get_resp = await client.get("/api/settings/stream-url")
        assert get_resp.status_code == 200
        assert get_resp.json()["stream_url"] == test_link_1

        # Verify /api/me also returns the saved link
        me_resp = await client.get("/api/me")
        assert me_resp.status_code == 200
        assert me_resp.json()["stream_url"] == test_link_1

        # Now update the link
        test_link_2 = "https://www.youtube.com/live/jfKfPfyJRdk"
        update_resp = await client.post(
            "/api/settings/stream-url",
            json={"stream_url": test_link_2},
            headers={"Content-Type": "application/json"},
        )
        assert update_resp.status_code == 200
        assert update_resp.json()["stream_url"] == test_link_2

        # Verify new link is now kept
        get_resp2 = await client.get("/api/settings/stream-url")
        assert get_resp2.status_code == 200
        assert get_resp2.json()["stream_url"] == test_link_2


@pytest.mark.anyio
async def test_stream_url_persists_even_if_bot_start_errors():
    import httpx
    from app.main import app
    from app.db import init_db

    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        offline_stream = "https://www.youtube.com/watch?v=offline1234"
        # Starting with an offline stream / unconfigured OAuth throws 400
        start_resp = await client.post(
            "/api/bots/start",
            json={"stream_url": offline_stream, "interval_seconds": 15, "mode": "oauth"},
            headers={"Content-Type": "application/json"},
        )
        # It errors because Google OAuth is not connected
        assert start_resp.status_code == 400

        # But the stream URL must STILL be saved in the database and retrieved!
        get_resp = await client.get("/api/settings/stream-url")
        assert get_resp.status_code == 200
        assert get_resp.json()["stream_url"] == offline_stream
        assert get_resp.json()["video_id"] == "offline1234"
        assert get_resp.json()["interval_seconds"] == 15


@pytest.mark.anyio
async def test_runner_settings_persisted():
    import httpx
    from app.main import app
    from app.db import init_db

    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/settings/runner",
            json={"mode": "cookie", "interval_seconds": 10},
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "cookie"
        assert data["interval_seconds"] == 10

        get_resp = await client.get("/api/settings/stream-url")
        assert get_resp.json()["mode"] == "cookie"
        assert get_resp.json()["interval_seconds"] == 10


@pytest.mark.anyio
async def test_persistent_session_and_credentials_never_forgotten():
    import httpx
    from app.main import app
    from app.db import init_db

    await init_db()
    transport = httpx.ASGITransport(app=app)
    # Brand new client with NO cookies
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Visiting home page without cookies
        resp = await client.get("/")
        assert resp.status_code == 200
        # A persistent session cookie must be issued to the client
        assert "session" in resp.cookies
        # The page must NOT be blocked by the empty login screen
        assert "Live Stream Bot Runner" in resp.text
        assert "Target YouTube Live Stream URL" in resp.text






