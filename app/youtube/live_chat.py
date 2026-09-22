import re
from urllib.parse import parse_qs, urlparse

import httpx

YOUTUBE_API = "https://www.googleapis.com/youtube/v3"


class YouTubeAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


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
            if "quota" in err.lower():
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
            if "quota" in err.lower():
                raise YouTubeAPIError(
                    "YouTube API daily quota exceeded. Bot stopped.",
                    403,
                )
            raise YouTubeAPIError(err, 403)
        if resp.status_code not in (200, 201):
            raise YouTubeAPIError(_api_error_message(resp), resp.status_code)


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
