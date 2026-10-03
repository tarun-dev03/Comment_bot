import logging
import re
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger(__name__)

YOUTUBE_API = "https://www.googleapis.com/youtube/v3"


class YouTubeAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.is_auth = (status_code == 401)
        self.is_quota = (status_code == 403 and "quota" in message.lower())
        self.is_transient = (status_code in (429, 500, 502, 503, 504) or status_code is None)


def parse_video_id(url: str) -> str | None:
    url = url.strip()
    if not url:
        return None

    if re.fullmatch(r"[\w-]{11}", url):
        return url

    parsed = urlparse(url if "://" in url else f"https://{url}")

    if parsed.hostname in ("youtu.be", "www.youtu.be"):
        vid = parsed.path.lstrip("/").split("/")[0]
        return vid if len(vid) == 11 else None

    if parsed.hostname and "youtube.com" in parsed.hostname:
        if parsed.path == "/watch":
            qs = parse_qs(parsed.query)
            vid = qs.get("v", [None])[0]
            return vid if vid and len(vid) == 11 else None
        match = re.match(r"^/(live|embed|shorts)/([\w-]{11})", parsed.path)
        if match:
            return match.group(2)

    return None


async def fetch_live_chat_id(access_token: str, video_id: str) -> tuple[str, str]:
    """Returns (live_chat_id, video_title)."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{YOUTUBE_API}/videos",
                params={"part": "liveStreamingDetails,snippet", "id": video_id},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code == 401:
                raise YouTubeAPIError("YouTube authorization expired. Reconnect Google.", 401)
            if resp.status_code == 403:
                err = _api_error_message(resp)
                if any(k in err.lower() for k in ("quota", "limit")):
                    raise YouTubeAPIError(
                        "YouTube API daily quota exceeded. Request a quota increase or try tomorrow.",
                        403,
                    )
                raise YouTubeAPIError(err, 403)
            if resp.status_code != 200:
                raise YouTubeAPIError(_api_error_message(resp), resp.status_code)

            items = resp.json().get("items") or []
            if not items:
                raise YouTubeAPIError("Video not found. Check the URL.")

            item = items[0]
            title = item.get("snippet", {}).get("title") or video_id
            live_chat_id = (item.get("liveStreamingDetails") or {}).get("activeLiveChatId")
            if not live_chat_id:
                raise YouTubeAPIError(
                    "This stream is not live or live chat is not available. "
                    "Use an active live stream URL."
                )
            return live_chat_id, title
    except httpx.RequestError as exc:
        raise YouTubeAPIError(f"Network error connecting to YouTube: {exc}", 503) from exc


async def insert_live_chat_message(
    access_token: str, live_chat_id: str, message: str
) -> None:
    message = message.strip()[:200]
    if not message:
        raise YouTubeAPIError("Empty message.")

    body = {
        "snippet": {
            "liveChatId": live_chat_id,
            "type": "textMessageEvent",
            "textMessageDetails": {"messageText": message},
        }
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{YOUTUBE_API}/liveChat/messages",
                params={"part": "snippet"},
                json=body,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 401:
                raise YouTubeAPIError("YouTube authorization expired. Reconnect Google.", 401)
            if resp.status_code == 403:
                err = _api_error_message(resp)
                if any(k in err.lower() for k in ("quota", "limit")):
                    raise YouTubeAPIError(
                        "YouTube API daily quota exceeded (10,000 units/day limit).",
                        403,
                    )
                raise YouTubeAPIError(err, 403)
            if resp.status_code not in (200, 201):
                raise YouTubeAPIError(_api_error_message(resp), resp.status_code)
    except httpx.RequestError as exc:
        raise YouTubeAPIError(f"Network error communicating with YouTube: {exc}", 503) from exc


def _api_error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error", {})
        msg = err.get("message") or str(data)
        errors = err.get("errors") or []
        if errors:
            reason = errors[0].get("reason") or errors[0].get("message")
            if reason:
                return f"{msg} ({reason})"
        return msg
    except Exception:
        return resp.text or f"HTTP {resp.status_code}"


def _extract_cookie_val(cookie_str: str, name: str) -> str | None:
    for item in cookie_str.split(";"):
        if "=" in item:
            parts = item.strip().split("=", 1)
            if parts[0].strip() == name:
                return parts[1].strip().strip('"')
    return None


def _find_send_chat_params(data: any) -> str | None:
    """Recursively search for sendLiveChatMessageEndpoint params in ytInitialData."""
    if isinstance(data, dict):
        if "sendLiveChatMessageEndpoint" in data and isinstance(data["sendLiveChatMessageEndpoint"], dict):
            p = data["sendLiveChatMessageEndpoint"].get("params")
            if p:
                return p
        for v in data.values():
            res = _find_send_chat_params(v)
            if res:
                return res
    elif isinstance(data, list):
        for item in data:
            res = _find_send_chat_params(item)
            if res:
                return res
    return None


def _check_chat_restrictions(data: any) -> str | None:
    """Check if stream participation is restricted (subscribers-only, members-only, etc.)."""
    if isinstance(data, dict):
        if "liveChatRestrictedParticipationRenderer" in data:
            rend = data["liveChatRestrictedParticipationRenderer"]
            msg = rend.get("message", {}).get("runs", [])
            text = "".join(r.get("text", "") for r in msg if isinstance(r, dict))
            return text or "Chat participation is restricted (e.g. subscribers-only, members-only, or channel required)."
        for v in data.values():
            res = _check_chat_restrictions(v)
            if res:
                return res
    elif isinstance(data, list):
        for item in data:
            res = _check_chat_restrictions(item)
            if res:
                return res
    return None


async def send_innertube_live_chat_message(
    cookie_string: str, video_id: str, message: str
) -> None:
    import hashlib
    import json
    import time
    import urllib.parse
    import uuid

    cookie_string = " ".join(cookie_string.splitlines()).strip()
    if not cookie_string:
        raise YouTubeAPIError("YouTube Cookie is empty. Please enter your YouTube cookie in settings.", 401)

    sapisid = _extract_cookie_val(cookie_string, "SAPISID") or _extract_cookie_val(cookie_string, "__Secure-3PAPISID")
    if not sapisid:
        raise YouTubeAPIError(
            "YouTube Cookie missing SAPISID. Make sure to copy the full 'Cookie:' header from DevTools > Network tab (do not use document.cookie).",
            401,
        )

    has_sid = bool(
        _extract_cookie_val(cookie_string, "SID")
        or _extract_cookie_val(cookie_string, "__Secure-1PSID")
        or _extract_cookie_val(cookie_string, "__Secure-3PSID")
    )
    has_hsid = bool(_extract_cookie_val(cookie_string, "HSID"))
    if not has_sid or not has_hsid:
        logger.warning(
            "Cookie string appears to be missing SID or HSID (often caused by copying from Console/document.cookie)."
        )

    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    )

    url = f"https://www.youtube.com/live_chat?v={video_id}"
    headers = {
        "User-Agent": user_agent,
        "Cookie": cookie_string,
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code in (401, 403):
                raise YouTubeAPIError("YouTube Cookie expired or unauthorized (HTTP 401/403). Please update your YouTube Cookie.", 401)
            if resp.status_code != 200:
                raise YouTubeAPIError(f"Failed to load YouTube live chat page (HTTP {resp.status_code})", resp.status_code)

            html = resp.text
            if "Sign in to chat" in html:
                raise YouTubeAPIError(
                    "YouTube rejected the cookie session (page says 'Sign in to chat'). "
                    "This happens when cookies are copied from Console/document.cookie instead of DevTools Network tab, "
                    "omitting required HttpOnly cookies (HSID/SSID/LOGIN_INFO). "
                    "Please copy the full 'Cookie:' header from DevTools > Network tab > Request Headers.",
                    401,
                )

            api_key_match = re.search(r'"INNERTUBE_API_KEY":\s*"([^"]+)"', html)
            client_ver_match = re.search(r'"INNERTUBE_CLIENT_VERSION":\s*"([^"]+)"', html)

            if not api_key_match or not client_ver_match:
                # Fallback to main watch page
                alt_url = f"https://www.youtube.com/watch?v={video_id}"
                resp_alt = await client.get(alt_url, headers=headers)
                if resp_alt.status_code == 200:
                    html_alt = resp_alt.text
                    api_key_match = api_key_match or re.search(r'"INNERTUBE_API_KEY":\s*"([^"]+)"', html_alt)
                    client_ver_match = client_ver_match or re.search(r'"INNERTUBE_CLIENT_VERSION":\s*"([^"]+)"', html_alt)
                    if api_key_match and client_ver_match:
                        html = html_alt

            if not api_key_match or not client_ver_match:
                raise YouTubeAPIError("Could not parse YouTube parameters from stream. Make sure the video is a valid live stream.")

            api_key = api_key_match.group(1)
            client_version = client_ver_match.group(1)

            params = None
            yt_initial_data = None
            match = re.search(r'window\["ytInitialData"\]\s*=\s*(\{.*?\});</script>', html) or \
                    re.search(r'var ytInitialData\s*=\s*(\{.*?\});</script>', html)
            if match:
                try:
                    yt_initial_data = json.loads(match.group(1))
                    params = _find_send_chat_params(yt_initial_data)
                    if not params:
                        restriction = _check_chat_restrictions(yt_initial_data)
                        if restriction:
                            raise YouTubeAPIError(f"Cannot chat on this live stream: {restriction}", 400)
                except YouTubeAPIError:
                    raise
                except Exception:
                    pass

            if not params:
                endpoint_match = re.search(r'"sendLiveChatMessageEndpoint":\s*\{.*?"params":\s*"([^"]+)"', html, re.DOTALL) or \
                                 re.search(r'"liveChatRenderer":\s*\{.*?"params":\s*"([^"]+)"', html, re.DOTALL)
                if endpoint_match:
                    params = endpoint_match.group(1)

            if not params:
                raise YouTubeAPIError(
                    "Could not extract live chat submission parameters. "
                    "Make sure your YouTube cookie has active channel permissions and the stream allows live chat.",
                    429,
                )

            params = urllib.parse.unquote(params)

            now_ts = int(time.time())
            origin = "https://www.youtube.com"
            raw_str = f"{now_ts} {sapisid} {origin}"
            sha1 = hashlib.sha1(raw_str.encode("utf-8")).hexdigest()
            auth_header = f"SAPISIDHASH {now_ts}_{sha1}"

            session_index = None
            delegated_session_id = None
            for m in re.finditer(r"ytcfg\.set\(\s*(\{.*?\})\s*\);", html, re.DOTALL):
                try:
                    cfg = json.loads(m.group(1))
                    if "SESSION_INDEX" in cfg and cfg["SESSION_INDEX"] is not None:
                        session_index = str(cfg["SESSION_INDEX"])
                    if "DELEGATED_SESSION_ID" in cfg and cfg["DELEGATED_SESSION_ID"]:
                        delegated_session_id = str(cfg["DELEGATED_SESSION_ID"])
                except Exception:
                    pass

            if session_index is None:
                m_si = re.search(r'"SESSION_INDEX":\s*"?([0-9]+)"?', html)
                if m_si:
                    session_index = m_si.group(1)
                elif "authuser=" in cookie_string:
                    m_auth = re.search(r'authuser=(\d+)', cookie_string)
                    if m_auth:
                        session_index = m_auth.group(1)

            if not delegated_session_id:
                m_del = re.search(r'"DELEGATED_SESSION_ID":\s*"([^"]+)"', html)
                if m_del:
                    delegated_session_id = m_del.group(1)

            post_headers = {
                "User-Agent": user_agent,
                "Cookie": cookie_string,
                "Content-Type": "application/json",
                "X-YouTube-Client-Name": "1",
                "X-YouTube-Client-Version": client_version,
                "Origin": "https://www.youtube.com",
                "Referer": f"https://www.youtube.com/live_chat?v={video_id}",
                "Authorization": auth_header,
            }
            if session_index is not None:
                post_headers["X-Goog-AuthUser"] = session_index
            if delegated_session_id:
                post_headers["X-Goog-PageId"] = delegated_session_id

            client_msg_id = str(uuid.uuid4())
            send_url = f"https://www.youtube.com/youtubei/v1/live_chat/send_message?key={api_key}"
            payload = {
                "context": {
                    "client": {
                        "clientName": "WEB",
                        "clientVersion": client_version,
                        "hl": "en",
                        "gl": "US",
                    }
                },
                "params": params,
                "clientMessageId": client_msg_id,
                "richMessage": {
                    "textSegments": [
                        {
                            "text": message.strip()[:200]
                        }
                    ]
                }
            }

            post_resp = await client.post(send_url, headers=post_headers, json=payload)
            if post_resp.status_code in (401, 403):
                err_detail = ""
                try:
                    err_json = post_resp.json()
                    err_detail = err_json.get("error", {}).get("message", "")
                except Exception:
                    pass
                msg = f"YouTube Cookie expired or unauthorized (HTTP {post_resp.status_code})."
                if err_detail:
                    msg += f" YouTube error: {err_detail}."
                msg += " Please update your YouTube Cookie from DevTools > Network tab."
                raise YouTubeAPIError(msg, 401)
            if post_resp.status_code == 429:
                raise YouTubeAPIError(
                    "YouTube Web Chat rate limit/slow mode (HTTP 429). Temporary cooldown active.",
                    429,
                )
            if post_resp.status_code not in (200, 201):
                raise YouTubeAPIError(f"InnerTube request failed (HTTP {post_resp.status_code}): {post_resp.text[:150]}", post_resp.status_code)

            res_data = post_resp.json()
            error_msg = res_data.get("error", {}).get("message")
            if error_msg:
                raise YouTubeAPIError(f"YouTube chat error: {error_msg}", 429)

            actions = res_data.get("actions", [])
            has_chat_item = any("addChatItemAction" in act for act in actions if isinstance(act, dict))
            if not has_chat_item:
                if any("runAttestationCommand" in act for act in actions if isinstance(act, dict)):
                    raise YouTubeAPIError(
                        "YouTube bot attestation challenge triggered. Open this live stream in your browser and send one manual comment to verify your session.",
                        429,
                    )
                logger.warning("YouTube send_message succeeded with 200 but returned no addChatItemAction: %s", res_data)
                act_keys = [list(a.keys())[0] for a in actions if isinstance(a, dict) and a.keys()]
                if not actions:
                    raise YouTubeAPIError(
                        "Comment sent, but YouTube silently filtered it (empty actions). "
                        "Check if your account has a YouTube channel created (go to youtube.com -> Your channel) "
                        "or try simpler custom phrases.",
                        400,
                    )
                else:
                    raise YouTubeAPIError(
                        f"Comment sent, but YouTube returned action: {act_keys}. Message was not confirmed in chat.",
                        400,
                    )
    except httpx.RequestError as exc:
        raise YouTubeAPIError(f"Network error sending InnerTube message: {exc}", 503) from exc
