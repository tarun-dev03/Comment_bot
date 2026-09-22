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
