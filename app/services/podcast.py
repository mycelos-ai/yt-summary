"""Personal podcast RSS feed builder (Part B).

Turns a Profile's done TTS renderings into a standard RSS 2.0 +
iTunes-namespace feed, so "my watch-later list" plays in any podcast
app. Pure function — the route fetches jobs/videos and passes plain
``episode`` dicts; this module only builds XML (correctly escaped via
ElementTree).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

_ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"


def _episode_title(ep: dict) -> str:
    title = ep["title"]
    if ep.get("source") == "transcript":
        title = f"{title} — transcript"
    if ep.get("translated"):
        lang = ep.get("target_language")
        title = f"{title} ({lang})" if lang else title
    return title


def build_feed_xml(
    *,
    profile_name: str,
    token: str,
    episodes: list[dict],
    base_url: str,
    description: str = "Your yt-summary renderings as a podcast.",
    language: str = "en",
) -> str:
    """Build the RSS 2.0 + iTunes feed XML.

    Each episode dict carries: job_id, title, description, source,
    target_language, translated, duration_seconds, byte_length, and an
    optional thumbnail_url. Enclosure URLs point at the token-gated
    episode endpoint so podcast apps (which send no auth headers) can
    fetch them."""
    base = base_url.rstrip("/")
    ET.register_namespace("itunes", _ITUNES)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = f"yt-summary — {profile_name}"
    ET.SubElement(channel, "link").text = base
    ET.SubElement(channel, "description").text = description
    ET.SubElement(channel, "language").text = language
    # App logo as channel cover art.
    ET.SubElement(channel, f"{{{_ITUNES}}}image", {
        "href": f"{base}/static/icon.png",
    })

    for ep in episodes:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = _episode_title(ep)
        if ep.get("description"):
            ET.SubElement(item, "description").text = ep["description"]
        url = f"{base}/podcast/{token}/episode/{ep['job_id']}.mp3"
        ET.SubElement(item, "enclosure", {
            "url": url,
            "type": "audio/mpeg",
            "length": str(int(ep.get("byte_length") or 0)),
        })
        guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
        guid.text = f"yts-tts-{ep['job_id']}"
        dur = ep.get("duration_seconds")
        if dur is not None:
            ET.SubElement(item, f"{{{_ITUNES}}}duration").text = str(int(dur))
        if ep.get("thumbnail_url"):
            ET.SubElement(item, f"{{{_ITUNES}}}image", {
                "href": ep["thumbnail_url"],
            })

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + ET.tostring(rss, encoding="unicode")
    )
