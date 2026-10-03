import re
from urllib.parse import parse_qs, urlparse

import httpx

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


async def send_innertube_live_chat_message(
    cookie_string: str, video_id: str, message: str
) -> None:
    import hashlib
    import json
    import time

    cookie_string = " ".join(cookie_string.splitlines()).strip()
    if not cookie_string:
        raise YouTubeAPIError("YouTube Cookie is empty. Please enter your YouTube cookie in settings.", 401)

    sapisid = _extract_cookie_val(cookie_string, "SAPISID") or _extract_cookie_val(cookie_string, "__Secure-3PAPISID")
    if not sapisid:
        raise YouTubeAPIError(
            "YouTube Cookie missing SAPISID or __Secure-3PAPISID. Ensure you copy the complete Cookie header from browser developer tools.",
            401,
        )

    url = f"https://www.youtube.com/live_chat?v={video_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
            # Specific endpoints first
            endpoint_match = re.search(r'"sendLiveChatMessageEndpoint":\s*\{[^}]*"params":\s*"([^"]+)"', html) or \
                             re.search(r'"liveChatRenderer":\s*\{[^}]*"params":\s*"([^"]+)"', html)
            if endpoint_match:
                params = endpoint_match.group(1)

            if not params:
                match = re.search(r'window\["ytInitialData"\]\s*=\s*(\{.*?\});</script>', html) or \
                        re.search(r'var ytInitialData\s*=\s*(\{.*?\});</script>', html)
                if match:
                    try:
                        data_json = json.loads(match.group(1))
                        data_str = json.dumps(data_json)
                        params_matches = re.findall(r'"params":\s*"([^\"]+)"', data_str)
                        if params_matches:
                            params = params_matches[0]
                    except Exception:
                        pass

            if not params:
                params_match = re.search(r'"params":\s*"([a-zA-Z0-9%_-]{20,})"', html)
                if params_match:
                    params = params_match.group(1)

            if not params:
                raise YouTubeAPIError(
                    "Could not extract live chat submission parameters. "
                    "Make sure your YouTube cookie is valid and the stream is actively live.",
                    429,
                )

            now_ts = int(time.time())
            origin = "https://www.youtube.com"
            raw_str = f"{now_ts} {sapisid} {origin}"
            sha1 = hashlib.sha1(raw_str.encode("utf-8")).hexdigest()
            auth_header = f"SAPISIDHASH {now_ts}_{sha1}"

            post_headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Cookie": cookie_string,
                "Content-Type": "application/json",
                "X-YouTube-Client-Name": "1",
                "X-YouTube-Client-Version": client_version,
                "Origin": "https://www.youtube.com",
                "Referer": f"https://www.youtube.com/live_chat?v={video_id}",
                "Authorization": auth_header,
            }

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
                "richMessage": {
                    "textMessageEvent": {
                        "messageText": message.strip()[:200]
                    }
                }
            }

            post_resp = await client.post(send_url, headers=post_headers, json=payload)
            if post_resp.status_code in (401, 403):
                raise YouTubeAPIError("YouTube Cookie expired or unauthorized (HTTP 401/403). Please update your YouTube Cookie.", 401)
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
    except httpx.RequestError as exc:
        raise YouTubeAPIError(f"Network error sending InnerTube message: {exc}", 503) from exc
