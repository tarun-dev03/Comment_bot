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
    from app.deps import ensure_user_sync_token
    from app.models import User
    from sqlalchemy import select

    await init_db()
    async with async_session_factory() as db:
        res = await db.execute(select(User).where(User.email == "sync_test@example.com"))
        user = res.scalar_one_or_none()
        if not user:
            user = User(email="sync_test@example.com")
            db.add(user)
            await db.commit()
            await db.refresh(user)
        sync_token = await ensure_user_sync_token(db, user)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Unauthenticated cookie sync must be rejected (prevents overwriting another user by email)
        unauth_resp = await client.post(
            "/api/auth/cookie",
            json={"cookie": "SAPISID=fake_sync; SID=fake_sid;", "email": "sync_test@example.com"},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        assert unauth_resp.status_code == 401

        # Authenticated via personal sync token succeeds and does NOT issue a web session cookie
        resp = await client.post(
            "/api/auth/cookie",
            json={"cookie": "SAPISID=fake_sync; SID=fake_sid;", "email": "sync_test@example.com"},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {sync_token}",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True
        assert data.get("user") == "sync_test@example.com"
        assert "session" not in resp.cookies


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


async def _create_authenticated_client(app, email: str):
    import httpx
    from sqlalchemy import select
    from app.db import async_session_factory, init_db
    from app.models import User
    from app.security import create_session_cookie

    await init_db()
    async with async_session_factory() as db:
        res = await db.execute(select(User).where(User.email == email))
        user = res.scalar_one_or_none()
        if not user:
            user = User(email=email)
            db.add(user)
            await db.commit()
            await db.refresh(user)

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"session": create_session_cookie(user.id)},
    )
    return client, user


@pytest.mark.anyio
async def test_api_me_endpoint():
    import httpx
    from app.main import app
    from app.db import init_db

    await init_db()
    transport = httpx.ASGITransport(app=app)
    # Unauthenticated client must get authenticated=False and no leaked suggested_email
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as unauth_client:
        resp = await unauth_client.get("/api/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False
        assert data.get("suggested_email") is None

    # Authenticated client gets its own profile
    auth_client, user = await _create_authenticated_client(app, "me_test@example.com")
    async with auth_client as client:
        resp = await client.get("/api/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["email"] == "me_test@example.com"
        assert data["user_id"] == user.id
        assert "stream_url" in data


@pytest.mark.anyio
async def test_stream_url_saved_and_kept_until_updated():
    from app.main import app

    client, _ = await _create_authenticated_client(app, "stream_keep@example.com")
    async with client:
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

        get_resp = await client.get("/api/settings/stream-url")
        assert get_resp.status_code == 200
        assert get_resp.json()["stream_url"] == test_link_1

        me_resp = await client.get("/api/me")
        assert me_resp.status_code == 200
        assert me_resp.json()["stream_url"] == test_link_1

        test_link_2 = "https://www.youtube.com/live/jfKfPfyJRdk"
        update_resp = await client.post(
            "/api/settings/stream-url",
            json={"stream_url": test_link_2},
            headers={"Content-Type": "application/json"},
        )
        assert update_resp.status_code == 200
        assert update_resp.json()["stream_url"] == test_link_2

        get_resp2 = await client.get("/api/settings/stream-url")
        assert get_resp2.status_code == 200
        assert get_resp2.json()["stream_url"] == test_link_2


@pytest.mark.anyio
async def test_stream_url_persists_even_if_bot_start_errors():
    from app.main import app

    client, _ = await _create_authenticated_client(app, "stream_persist_err@example.com")
    async with client:
        offline_stream = "https://www.youtube.com/watch?v=offline1234"
        start_resp = await client.post(
            "/api/bots/start",
            json={"stream_url": offline_stream, "interval_seconds": 15, "mode": "oauth"},
            headers={"Content-Type": "application/json"},
        )
        assert start_resp.status_code == 400

        get_resp = await client.get("/api/settings/stream-url")
        assert get_resp.status_code == 200
        assert get_resp.json()["stream_url"] == offline_stream
        assert get_resp.json()["video_id"] == "offline1234"
        assert get_resp.json()["interval_seconds"] == 15


@pytest.mark.anyio
async def test_runner_settings_persisted():
    from app.main import app

    client, _ = await _create_authenticated_client(app, "runner_settings@example.com")
    async with client:
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
async def test_unauthenticated_visitor_does_not_inherit_other_users_session():
    import httpx
    from app.main import app
    from app.db import init_db

    await init_db()
    # Ensure another user already exists in DB
    existing_client, _ = await _create_authenticated_client(app, "owner_user@example.com")
    async with existing_client:
        await existing_client.post(
            "/api/settings/stream-url",
            json={"stream_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )

    transport = httpx.ASGITransport(app=app)
    # Brand new visitor with NO cookies must see Sign-In screen and receive NO session cookie
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as visitor:
        resp = await visitor.get("/")
        assert resp.status_code == 200
        assert "session" not in resp.cookies
        assert "Sign in with Google" in resp.text
        assert "Live Stream Bot Runner" not in resp.text
        assert "owner_user@example.com" not in resp.text

    # Authenticated user keeps persistent session cookie across visits
    auth_client, _ = await _create_authenticated_client(app, "owner_user@example.com")
    async with auth_client as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "session" in resp.cookies
        assert "Live Stream Bot Runner" in resp.text
        assert "Target YouTube Live Stream URL" in resp.text


@pytest.mark.anyio
async def test_multi_user_isolation_and_account_mismatch_guards():
    from sqlalchemy import select
    from app.db import async_session_factory
    from app.deps import ensure_user_sync_token
    from app.main import app
    from app.models import BotJob, BotJobStatus, User, UserCookie
    from app.security import decrypt_text

    client_a, user_a = await _create_authenticated_client(app, "alice_iso@example.com")
    client_b, user_b = await _create_authenticated_client(app, "bob_iso@example.com")

    async with async_session_factory() as db:
        res_a = await db.execute(select(User).where(User.id == user_a.id))
        u_a = res_a.scalar_one()
        token_a = await ensure_user_sync_token(db, u_a)

        res_b = await db.execute(select(User).where(User.id == user_b.id))
        u_b = res_b.scalar_one()
        token_b = await ensure_user_sync_token(db, u_b)

    assert token_a != token_b

    async with client_a, client_b:
        # Both users save distinct cookies and distinct streams
        r_a = await client_a.post(
            "/api/auth/cookie",
            json={"cookie": "SAPISID=alice_sapi; SID=alice_sid;", "email": "alice_iso@example.com"},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert r_a.status_code == 200

        r_b = await client_b.post(
            "/api/auth/cookie",
            json={"cookie": "SAPISID=bob_sapi; SID=bob_sid;", "email": "bob_iso@example.com"},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert r_b.status_code == 200

        # Mismatched email with Alice's sync token must be rejected with 409!
        mismatch = await client_b.post(
            "/api/auth/cookie",
            json={"cookie": "SAPISID=attacker; SID=attacker;", "email": "bob_iso@example.com"},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert mismatch.status_code == 409

        # Stale tab X-User-Id mismatch guard
        stale_tab = await client_a.post(
            "/api/settings/stream-url",
            json={"stream_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            headers={"X-User-Id": str(user_b.id)},
        )
        assert stale_tab.status_code == 409

        # Verify Alice's cookie was NOT overwritten by Bob's sync or mismatch attempt
        async with async_session_factory() as db:
            ca = (await db.execute(select(UserCookie).where(UserCookie.user_id == user_a.id))).scalar_one()
            cb = (await db.execute(select(UserCookie).where(UserCookie.user_id == user_b.id))).scalar_one()
            assert "alice_sapi" in decrypt_text(ca.cookie_encrypted)
            assert "bob_sapi" in decrypt_text(cb.cookie_encrypted)

        # Verify auto_sync=True does not hijack a running OAuth job's mode
        async with async_session_factory() as db:
            job_a = (await db.execute(select(BotJob).where(BotJob.user_id == user_a.id))).scalar_one_or_none()
            if not job_a:
                job_a = BotJob(
                    user_id=user_a.id,
                    stream_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    video_id="dQw4w9WgXcQ",
                    live_chat_id="lc_alice",
                    mode="oauth",
                    status=BotJobStatus.RUNNING.value,
                )
                db.add(job_a)
            else:
                job_a.mode = "oauth"
                job_a.status = BotJobStatus.RUNNING.value
            await db.commit()

        auto_sync_res = await client_a.post(
            "/api/auth/cookie",
            json={
                "cookie": "SAPISID=alice_sapi_v2; SID=alice_sid;",
                "email": "alice_iso@example.com",
                "auto_sync": True,
            },
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert auto_sync_res.status_code == 200
        async with async_session_factory() as db:
            job_a_after = (await db.execute(select(BotJob).where(BotJob.user_id == user_a.id))).scalar_one()
            assert job_a_after.mode == "oauth"
            job_a_after.status = BotJobStatus.STOPPED.value
            await db.commit()


@pytest.mark.anyio
async def test_google_oauth_callback_blocks_account_mismatch(monkeypatch):
    from sqlalchemy import select
    from app.auth.oauth_state import create_oauth_state
    from app.db import async_session_factory
    from app.main import app
    from app.models import OAuthToken, User
    from app.routes import web
    from app.security import encrypt_text

    client_alice, user_alice = await _create_authenticated_client(app, "alice_oauth@gmail.com")
    _, user_bob = await _create_authenticated_client(app, "bob_oauth@gmail.com")

    async with async_session_factory() as db:
        res = await db.execute(select(OAuthToken).where(OAuthToken.user_id == user_alice.id))
        if not res.scalar_one_or_none():
            db.add(
                OAuthToken(
                    user_id=user_alice.id,
                    google_email="alice_oauth@gmail.com",
                    refresh_token_encrypted=encrypt_text("alice_refresh_token"),
                )
            )
            await db.commit()
        await create_oauth_state(
            db,
            state="mismatch_state_1",
            redirect_uri="http://test/auth/google/callback",
            user_id=user_alice.id,
        )

    async def fake_exchange(code: str, redirect_uri: str) -> dict:
        return {"refresh_token": "bob_refresh_token", "access_token": "bob_access"}

    async def fake_email(access: str) -> str:
        return "bob_oauth@gmail.com"

    monkeypatch.setattr(web, "exchange_code_for_tokens", fake_exchange)
    monkeypatch.setattr(web, "fetch_google_email", fake_email)

    async with client_alice:
        resp = await client_alice.get(
            "/auth/google/callback?code=abc&state=mismatch_state_1",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "auth_error=account_mismatch" in resp.headers["location"]

    # Alice's refresh token must remain untouched!
    async with async_session_factory() as db:
        from app.auth.google_oauth import get_user_refresh_token

        tok = await get_user_refresh_token(db, user_alice.id)
        assert tok == "alice_refresh_token"


@pytest.mark.anyio
async def test_concurrent_multi_user_bots_and_isolated_innertube_cache(monkeypatch):
    import asyncio
    from app.bot.engine import is_bot_running, stop_bot_task
    from app.db import async_session_factory
    from app.main import app
    from app.models import BotJob
    from app.youtube import live_chat
    from sqlalchemy import select

    live_chat.invalidate_innertube_cache()

    client_1, user_1 = await _create_authenticated_client(app, "concurrent_1@example.com")
    client_2, user_2 = await _create_authenticated_client(app, "concurrent_2@example.com")

    sent_log: list[tuple[int | None, str, str]] = []

    async def fake_send_innertube(cookie_string: str, video_id: str, message: str, user_id: int | None = None) -> str:
        sent_log.append((user_id, cookie_string, video_id))
        return cookie_string

    monkeypatch.setattr("app.bot.engine.send_innertube_live_chat_message", fake_send_innertube)

    async with client_1, client_2:
        await client_1.post(
            "/api/auth/cookie",
            json={"cookie": "SAPISID=user1_sapi; SID=user1_sid;"},
        )
        await client_2.post(
            "/api/auth/cookie",
            json={"cookie": "SAPISID=user2_sapi; SID=user2_sid;"},
        )

        # Both users start bots at the same time on the exact same stream URL
        r1 = await client_1.post(
            "/api/bots/start",
            json={"stream_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "interval_seconds": 5, "mode": "cookie"},
        )
        r2 = await client_2.post(
            "/api/bots/start",
            json={"stream_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "interval_seconds": 5, "mode": "cookie"},
        )
        assert r1.status_code == 200
        assert r2.status_code == 200

        await asyncio.sleep(0.25)
        assert is_bot_running(user_1.id) is True
        assert is_bot_running(user_2.id) is True

        # Both users sent comments using ONLY their own respective cookies
        user1_sends = [entry for entry in sent_log if entry[0] == user_1.id]
        user2_sends = [entry for entry in sent_log if entry[0] == user_2.id]
        assert len(user1_sends) >= 1
        assert len(user2_sends) >= 1
        assert all("user1_sapi" in s[1] for s in user1_sends)
        assert all("user2_sapi" in s[1] for s in user2_sends)

        # Stopping user_1 leaves user_2 running!
        await client_1.post("/api/bots/stop")
        assert is_bot_running(user_1.id) is False
        assert is_bot_running(user_2.id) is True

        await stop_bot_task(user_2.id)
        assert is_bot_running(user_2.id) is False

    # Verify DB statuses cleanly stopped
    async with async_session_factory() as db:
        j1 = (await db.execute(select(BotJob).where(BotJob.user_id == user_1.id))).scalar_one()
        j2 = (await db.execute(select(BotJob).where(BotJob.user_id == user_2.id))).scalar_one()
        assert j1.status == "stopped"
        assert j2.status == "stopped"


@pytest.mark.anyio
async def test_innertube_cache_isolated_per_user():
    from app.youtube.live_chat import _innertube_cache, invalidate_innertube_cache

    invalidate_innertube_cache()
    _innertube_cache["u1:vid123:hashA"] = {"params": "p1"}
    _innertube_cache["u2:vid123:hashB"] = {"params": "p2"}

    invalidate_innertube_cache(user_id=1)
    assert "u1:vid123:hashA" not in _innertube_cache
    assert "u2:vid123:hashB" in _innertube_cache
    invalidate_innertube_cache()


def test_conversation_chat_database_progression():
    from app.bot.messages import STAGE_ORDER, MessageGenerator, get_conversation_db_stats

    stats = get_conversation_db_stats()
    assert stats["total_stage_phrases"] >= 100
    assert stats["conversation_arcs"] >= 10
    for stage in STAGE_ORDER:
        assert stats["stage_counts"].get(stage, 0) >= 15

    gen = MessageGenerator()
    first = gen.next_message()
    # Turn 0 must begin with a Hinglish "kaise ho / how are you" greeting
    first_lower = first.lower()
    assert any(
        k in first_lower
        for k in ("kaise ho", "kaise hain", "kya haal", "how are you", "sab kaise", "kaise feel", "kaise chal")
    ), f"Expected opening message to be a Hinglish greeting, got: {first}"

    # Generate 100 messages across all conversation stages with zero repeats
    messages = [first]
    seen = {first.lower()}
    for _ in range(99):
        msg = gen.next_message()
        assert msg.lower() not in seen
        seen.add(msg.lower())
        messages.append(msg)
    assert len(messages) == 100


@pytest.mark.anyio
async def test_conversation_stats_endpoint():
    from app.main import app

    client, _ = await _create_authenticated_client(app, "conv_stats@example.com")
    async with client:
        resp = await client.get("/api/chats/conversation-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "1_how_are_you" in data["stage_counts"]
        assert "5_deepening_and_keeping_alive" in data["stage_counts"]






