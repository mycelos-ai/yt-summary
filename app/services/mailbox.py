"""IMAP newsletter ingestion.

A newsletter issue is "a web article that arrived by email." We pull
unprocessed messages from a dedicated mailbox over IMAP, clean the
newsletter cruft out of the HTML, and hand the plain-text body to the
same summarize/embed/chat pipeline that web articles use.

Cleaning runs in two structural layers here (the LLM is the third,
applied later by the summarizer):

  Layer 1 — pre-clean the raw HTML (strip <style>/<script>, hidden
            pre-header spam, tracking pixels, spacer images) with lxml,
            then run trafilatura to extract the body text.
  Layer 2 — text heuristics: cut everything below the first
            unsubscribe/footer marker, strip zero-width padding, and
            collapse blank-line runs.

IMAP is a *pull* protocol, which is the whole reason we model
newsletters this way: yt-summary is LAN-only and never exposes an
inbound webhook. `imap-tools` is synchronous, so the one blocking call
is wrapped in a thread — same pattern as reader.py / playlist cookies.
"""

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime

import trafilatura
from lxml import html as lxml_html

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImapConfig:
    host: str
    port: int
    ssl: bool
    username: str
    password: str
    folder: str = "INBOX"


@dataclass(frozen=True)
class MailMessage:
    uid: int
    message_id: str
    sender_name: str
    sender_addr: str
    subject: str
    date: datetime | None
    body: str  # cleaned plain text
    web_url: str | None  # "view in browser" link if found


def mail_id_from_message_id(message_id: str) -> str:
    """Stable item id derived from the RFC-822 Message-ID.

    `mail-<11 hex chars of sha256>`, mirroring web_id_from_url so a
    glance at the id reveals the kind. The hash-as-id makes
    re-ingesting the same message idempotent through upsert_metadata.
    """
    digest = hashlib.sha256(message_id.encode("utf-8")).digest()
    return "mail-" + digest.hex()[:11]


# Markers below which the body is reliably footer boilerplate. Cut at
# the EARLIEST occurrence of any of these (case-insensitive). Ordered
# list so it's trivial to extend as real senders reveal new patterns.
_FOOTER_MARKERS: tuple[str, ...] = (
    "unsubscribe",
    "abbestellen",
    "vom newsletter abmelden",
    "newsletter abbestellen",
    "view in browser",
    "view this email in your browser",
    "im browser ansehen",
    "im browser anzeigen",
    "manage preferences",
    "manage your preferences",
    "update your preferences",
    "update preferences",
    "you received this email because",
    "you are receiving this email because",
    "diese e-mail wurde an",
    "sie erhalten diese e-mail",
    "add us to your address book",
    "© ",
    "all rights reserved",
)

# Zero-width / soft-hyphen / BOM characters used as invisible pre-header
# padding by Mailchimp et al. Strip them so they don't bloat the body.
_ZERO_WIDTH_RE = re.compile(r"[​‌‍‎‏­﻿]")
_MANY_BLANK_LINES_RE = re.compile(r"\n{3,}")

_VIEW_IN_BROWSER_RE = re.compile(
    r'<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>([^<]*)</a>',
    re.IGNORECASE | re.DOTALL,
)
_BROWSER_TEXT_RE = re.compile(
    r"view\s+(?:this\s+email\s+)?in\s+(?:your\s+)?browser|im\s+browser\s+(?:ansehen|anzeigen)",
    re.IGNORECASE,
)


def _extract_web_url(raw_html: str) -> str | None:
    """Best-effort: pull the 'view in browser' link out of the HTML."""
    for href, text in _VIEW_IN_BROWSER_RE.findall(raw_html):
        if _BROWSER_TEXT_RE.search(text):
            return href.strip()
    return None


def _is_hidden_style(style: str) -> bool:
    s = style.lower().replace(" ", "")
    return any(
        marker in s
        for marker in (
            "display:none",
            "visibility:hidden",
            "font-size:0",
            "max-height:0",
            "mso-hide:all",
            "opacity:0",
        )
    )


def _preclean_html(raw_html: str) -> str:
    """Strip non-content and invisible nodes before trafilatura sees them.

    Newsletter HTML hides a lot of junk the eye never sees: pre-header
    spam padded with zero-width chars, 1×1 tracking pixels, spacer
    cells. trafilatura keys off visible structure and would otherwise
    fold some of that into the body text. We drop it here so only real
    content survives. Falls back to the raw HTML if parsing fails.
    """
    try:
        root = lxml_html.fromstring(raw_html)
    except Exception:
        return raw_html

    for el in root.xpath("//script | //style | //head | //title | //noscript"):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)

    for el in root.xpath("//*[@style]"):
        if _is_hidden_style(el.get("style") or ""):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)

    for img in root.xpath("//img"):
        w = (img.get("width") or "").strip().rstrip("px")
        h = (img.get("height") or "").strip().rstrip("px")
        if w in ("0", "1") or h in ("0", "1"):
            parent = img.getparent()
            if parent is not None:
                parent.remove(img)

    return str(lxml_html.tostring(root, encoding="unicode"))


def _lxml_text(cleaned_html: str) -> str:
    """Block-aware plain-text fallback when trafilatura collapses.

    trafilatura is tuned for articles and occasionally discards the
    body of a short, table-heavy newsletter as 'boilerplate'. When that
    happens we fall back to lxml's text_content, inserting newlines at
    block boundaries so the footer-cut heuristic still has line
    structure to work with.
    """
    try:
        root = lxml_html.fromstring(cleaned_html)
    except Exception:
        return ""
    for block in root.xpath("//p | //div | //tr | //h1 | //h2 | //h3 | //li | //br"):
        block.tail = (block.tail or "") + "\n"
    return str(root.text_content())


def _cut_footer(text: str) -> str:
    """Truncate the body at the first footer marker.

    Footer boilerplate (unsubscribe links, legal lines, "view in
    browser") is reliably *below* the content, so cutting at the
    earliest marker keeps the substance and drops the noise.
    """
    lowered = text.lower()
    cut = len(text)
    for marker in _FOOTER_MARKERS:
        idx = lowered.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return text[:cut].rstrip()


def clean_body(*, html: str | None, plain: str | None) -> str:
    """Turn an email's HTML (preferred) or plain part into clean text.

    Layer 1 (HTML pre-clean + trafilatura) then Layer 2 (footer cut +
    zero-width strip + blank-line collapse). Returns "" when there's
    nothing usable to extract.
    """
    extracted: str | None = None
    if html and html.strip():
        cleaned_html = _preclean_html(html)
        # favor_recall (not precision): newsletters are short and
        # table-based, and precision mode routinely discards their body
        # as boilerplate. Recall keeps the content; the footer-cut and
        # pre-clean handle the noise precision would have removed.
        extracted = trafilatura.extract(
            cleaned_html,
            output_format="txt",
            include_comments=False,
            include_tables=False,
            include_links=False,
            favor_recall=True,
        )
        # Fallback: only when trafilatura clearly collapsed — its output
        # is near-empty while there's substantial visible text. We don't
        # fall back on a merely-shorter result, since that's usually
        # trafilatura legitimately stripping nav/footer noise.
        body_len = len((extracted or "").strip())
        lxml_text = _lxml_text(cleaned_html)
        if body_len < 40 and len(lxml_text.strip()) > 120:
            extracted = lxml_text
    if not extracted or not extracted.strip():
        extracted = plain or ""

    text = _ZERO_WIDTH_RE.sub("", extracted)
    text = _cut_footer(text)
    text = _MANY_BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def _require_imap_tools():
    """Import imap-tools, or raise a ValueError the UI can show verbatim.

    imap-tools is an optional-at-runtime dependency: the module imports
    fine without it, and only the actual fetch/test paths need it. When
    it's missing (e.g. a source install done before it was added to
    pyproject), we want the Test button to say so plainly instead of
    bubbling a raw ModuleNotFoundError up as a 500.
    """
    try:
        import imap_tools
    except ImportError as e:
        raise ValueError(
            "The 'imap-tools' package isn't installed. Install it with "
            "`pip install imap-tools` (or reinstall the app) and restart "
            "the server."
        ) from e
    return imap_tools


def _fetch_sync(cfg: ImapConfig, since_uid: int, batch_limit: int) -> list[MailMessage]:
    imap_tools = _require_imap_tools()

    box_cls = imap_tools.MailBox if cfg.ssl else imap_tools.MailBoxUnencrypted
    out: list[MailMessage] = []
    try:
        with box_cls(cfg.host, port=cfg.port).login(
            cfg.username, cfg.password, initial_folder=cfg.folder
        ) as mailbox:
            # UID range `<since+1>:*` — only messages newer than the
            # stored cursor. mark_seen so a glance at the mailbox shows
            # what's been handled; dedup still relies on the id + cursor.
            for msg in mailbox.fetch(
                imap_tools.AND(uid=f"{since_uid + 1}:*"),
                mark_seen=True,
                bulk=True,
                limit=batch_limit,
            ):
                try:
                    uid = int(msg.uid) if msg.uid else 0
                except (TypeError, ValueError):
                    uid = 0
                message_id = (msg.headers.get("message-id", ("",)) or ("",))[0].strip()
                if not message_id:
                    # Rare: synthesise a stable id from sender+subject+date.
                    basis = f"{msg.from_}|{msg.subject}|{msg.date_str}"
                    message_id = hashlib.sha256(basis.encode("utf-8")).hexdigest()
                body = clean_body(html=msg.html, plain=msg.text)
                web_url = _extract_web_url(msg.html or "")
                out.append(
                    MailMessage(
                        uid=uid,
                        message_id=message_id,
                        sender_name=(msg.from_values.name if msg.from_values else "")
                        or msg.from_
                        or "Newsletter",
                        sender_addr=msg.from_ or "",
                        subject=msg.subject or "(no subject)",
                        date=msg.date,
                        body=body,
                        web_url=web_url,
                    )
                )
    except Exception as e:
        # Surface a readable message to the Settings "Test" button and
        # the scheduler log. imap-tools raises a grab-bag of exception
        # types (auth, TLS, socket); we don't want to leak those raw.
        raise ValueError(
            f"IMAP connection failed ({type(e).__name__}: {e})"
        ) from e
    return out


def _check_sync(cfg: ImapConfig) -> int:
    imap_tools = _require_imap_tools()

    box_cls = imap_tools.MailBox if cfg.ssl else imap_tools.MailBoxUnencrypted
    try:
        with box_cls(cfg.host, port=cfg.port).login(
            cfg.username, cfg.password, initial_folder=cfg.folder
        ) as mailbox:
            status = mailbox.folder.status(cfg.folder)
            return int(status.get("MESSAGES", 0))
    except Exception as e:
        raise ValueError(
            f"IMAP connection failed ({type(e).__name__}: {e})"
        ) from e


async def check_connection(cfg: ImapConfig) -> int:
    """Log in, select the folder, and return its message count.

    Used by the Settings 'Test' button. Raises ValueError with a
    user-readable message on any auth/TLS/connection failure.
    """
    return await asyncio.to_thread(_check_sync, cfg)


async def fetch_new_messages(
    cfg: ImapConfig, since_uid: int, *, batch_limit: int = 50
) -> list[MailMessage]:
    """Fetch up to `batch_limit` messages with UID greater than `since_uid`.

    Synchronous `imap-tools` work is offloaded to a thread so the event
    loop keeps serving requests. Raises ValueError with a user-readable
    message on any connection/auth failure.
    """
    return await asyncio.to_thread(_fetch_sync, cfg, since_uid, batch_limit)
