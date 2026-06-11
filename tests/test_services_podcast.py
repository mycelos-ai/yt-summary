"""Tests for the podcast feed builder (Part B).

build_feed_xml is a pure function: (profile name, token, episodes,
base_url) -> RSS 2.0 + iTunes XML string. We parse it back with
xml.etree to assert structure.
"""

import xml.etree.ElementTree as ET

from app.services.podcast import build_feed_xml

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def _episode(**kw):
    base = dict(
        job_id=7,
        title="Deep dive on agents",
        description="A long talk about agents and evaluation.",
        source="summary",
        target_language="en",
        translated=False,
        duration_seconds=754.0,
        byte_length=1_200_000,
        thumbnail_url="https://host/thumbnails/v1.jpg",
    )
    base.update(kw)
    return base


def _parse(xml: str):
    return ET.fromstring(xml)


def test_feed_has_channel_metadata():
    xml = build_feed_xml(
        profile_name="Stefan", token="tok123",
        episodes=[_episode()], base_url="https://yts.example.com",
    )
    root = _parse(xml)
    assert root.tag == "rss"
    channel = root.find("channel")
    assert channel is not None
    assert "Stefan" in channel.findtext("title")
    assert channel.findtext("link") == "https://yts.example.com"


def test_feed_item_has_enclosure_and_guid():
    xml = build_feed_xml(
        profile_name="P", token="tok123",
        episodes=[_episode(job_id=42, byte_length=999, duration_seconds=65.0)],
        base_url="https://h",
    )
    item = _parse(xml).find("channel/item")
    assert item is not None
    enc = item.find("enclosure")
    assert enc is not None
    assert enc.get("type") == "audio/mpeg"
    assert enc.get("length") == "999"
    # Enclosure URL is the token-gated episode endpoint.
    assert enc.get("url") == "https://h/podcast/tok123/episode/42.mp3"
    # Stable, non-URL guid.
    assert item.findtext("guid") == "yts-tts-42"
    # iTunes duration in seconds.
    assert item.findtext(f"{ITUNES}duration") == "65"


def test_feed_transcript_episode_titled():
    xml = build_feed_xml(
        profile_name="P", token="t",
        episodes=[_episode(title="My Vid", source="transcript")],
        base_url="https://h",
    )
    item = _parse(xml).find("channel/item")
    assert "transcript" in item.findtext("title").lower()


def test_feed_escapes_xml_in_titles():
    xml = build_feed_xml(
        profile_name="P", token="t",
        episodes=[_episode(title="A & B <weird>")],
        base_url="https://h",
    )
    # Must remain parseable (no raw & or < breaking the document).
    item = _parse(xml).find("channel/item")
    assert item.findtext("title").startswith("A & B <weird>")


def test_feed_empty_episodes_still_valid_channel():
    xml = build_feed_xml(
        profile_name="P", token="t", episodes=[], base_url="https://h",
    )
    root = _parse(xml)
    assert root.find("channel") is not None
    assert root.find("channel/item") is None
