# Newsletter Aggregation — Design Spec (MVP)

**Date:** 2026-05-23
**Status:** Draft for review
**Owner:** Stefan
**Builds on:** [yt-summary core](2026-05-05-yt-summary-design.md),
[web articles](2026-05-06-web-articles-design.md),
[playlists](2026-05-06-playlists-design.md)

## Purpose

Point yt-summary at a dedicated mailbox, have it pull every newsletter
that lands there over IMAP, strip the boilerplate, summarize the
substance, and surface it in the same library / search / chat as
YouTube videos and web articles.

The use case: you create a throwaway address (or a catch-all on your
own domain), subscribe all your newsletters to it, and yt-summary
becomes the place you *skim* them — the LLM does the "is any of this
worth my attention?" triage for you.

This is the email-shaped sibling of two features that already exist:

- **Web articles** — a newsletter issue is "a web article that arrived
  by email instead of by URL." It reuses the whole
  body → summarize → embed → chat pipeline unchanged.
- **Playlists** — a mailbox is "a subscription we poll on a schedule."
  It reuses the existing `PlaylistScheduler` tick.

So the bulk of the work is **plumbing IMAP into the front door** plus
**aggressively cleaning newsletter cruft**. The back half of the
pipeline is free.

## Scope

In scope:

- One IMAP account **per profile**, configured in Settings (host, port,
  user, app-password, folder, SSL on/off, enable toggle).
- Fully automatic ingestion: the existing background scheduler polls
  the mailbox every interval — no user action per newsletter.
- HTML/multipart email → clean plain text, with newsletter boilerplate
  (tracking pixels, "view in browser", unsubscribe footers, ad blocks,
  social-icon rows, legal disclaimers) removed.
- A newsletter-tuned summarizer prompt that ignores the remaining cruft
  and, for digest-style mails, breaks out the individual stories so you
  can decide what's worth reading in full.
- Sender surfaced as a **tag** (reusing the existing tag-pill UI) so the
  library filters by newsletter for free.
- "Fetch now" button wired to the scheduler's existing `request_tick()`.

Out of scope (explicitly, deferred):

- Sending / replying / any outbound mail.
- OAuth (Gmail XOAUTH2). V1 is plain IMAP + app-password. Easiest path
  for the user is a dedicated mailbox (mailbox.org / Posteo / catch-all
  domain) where an app password is one click — see the recommendation
  at the end of this spec.
- Per-sender allow/block lists (V1 trusts that only newsletters arrive
  in the dedicated mailbox; the sender tag makes manual culling easy).
- Grouping a sender's issues into a dedicated collection page (the
  sender tag covers 80% of this).
- Attachments (PDF/ICS). Body text only.
- Cross-profile dedup of identical newsletters (each profile has its own
  mailbox; no shared-transcript path like YouTube has).
- Real-time push (IMAP IDLE). Polling on the scheduler interval is
  enough for newsletters.

## Why IMAP (pull), not inbound webhooks (push)

yt-summary is a LAN-only, self-hosted app. Inbound email-parsing
services (Mailgun/Postmark/SendGrid inbound) require a **publicly
reachable webhook**, which the app deliberately doesn't have. IMAP is
*pull* — the container reaches out, nothing needs to be exposed. This is
the decisive reason to model newsletters as a polled source rather than
an HTTP endpoint.

## Data Model

### `videos` discriminator

`VideoKind` gains a third value, `email`; `TranscriptSource` gains
`email`. Mirrors exactly how `web` was added (see web-articles spec).

Migration in `app/db.py::_run_migrations`, alongside the existing
`kind` handling. The `kind` column was already added without a CHECK
constraint for migrated installs (SQLite can't `ALTER ADD` a CHECK), so
no constraint change is needed for upgrades; only the fresh-install
`CREATE TABLE ... CHECK(kind IN (...))` in `SCHEMA` and the
`transcript_source` CHECK get `'email'` appended.

```sql
-- fresh-install SCHEMA only; migrated DBs already lack the CHECK
CHECK(kind IN ('youtube','web','email'))
CHECK(transcript_source IN ('manual_subs','auto_subs','whisper','web','email'))
```

ID format: `mail-<11 chars from sha256(message_id)>`, profile-scoped as
`{user_id}:mail-...` — same shape as `web-...`. `message_id` is the
RFC-822 `Message-ID` header; if a (rare) newsletter omits it, fall back
to `sha256(from + subject + date)`. The hash-as-id makes re-ingesting
the same message idempotent through the existing `upsert_metadata`.

`videos.transcript` holds the cleaned body. `transcript_segments` is
NULL (no time concept, exactly like web). `duration_seconds` NULL.
`url` holds the newsletter's "view in browser" / canonical link if one
is present, else empty — the detail page degrades to showing the sender
instead of an "open original" link.

### IMAP cursor (settings)

Per-profile config lives in the existing key/value `settings` table
(PK is `(user_id, key)`):

| key | meaning |
|---|---|
| `imap_enabled` | `"1"` / `""` master toggle |
| `imap_host` | e.g. `imap.mailbox.org` |
| `imap_port` | default `993` |
| `imap_ssl` | `"1"` (default) / `""` |
| `imap_username` | full email address |
| `imap_password` | app password (stored as-is; see Security) |
| `imap_folder` | default `INBOX` |
| `imap_last_uid` | highest IMAP UID processed in `imap_folder` |

`imap_last_uid` is the incremental cursor: each tick fetches only
`UID <last+1>:*`, so we never re-download the whole folder. The
Message-ID-derived id is the second line of defence against dupes (UID
validity resets, folder moves). Processed messages are also marked
`\Seen` so a glance at the mailbox shows what's been handled.

No new table in V1 — settings + the `videos` row carry everything.
(A `mail_sources` table for multiple accounts per profile is the
obvious V2 generalization but isn't needed now.)

## Components

### `app/services/mailbox.py` (new)

```python
@dataclass(frozen=True)
class MailMessage:
    message_id: str
    sender_name: str       # display name, e.g. "Heise Newsletter"
    sender_addr: str
    subject: str
    date: datetime
    body: str              # cleaned plain text (see cleaning below)
    web_url: str | None    # "view in browser" link if found
    uid: int

@dataclass(frozen=True)
class ImapConfig:
    host: str; port: int; ssl: bool
    username: str; password: str; folder: str

async def fetch_new_messages(
    cfg: ImapConfig, since_uid: int
) -> list[MailMessage]
```

Implementation: `imap-tools` (sync, batteries-included MIME parsing)
run via `asyncio.to_thread`, matching the threaded-blocking-IO pattern
already used in `reader.py` and the cookies resolution in
`playlist_sync.py`. New dependency in `pyproject.toml`:
`imap-tools>=1.5`.

Connection errors (auth failure, host unreachable, TLS) raise
`ValueError` with a user-readable message, surfaced by the Settings
"Test" button — same contract as the reader's HTTP errors.

### Email → clean text (the "Schrott raus" part)

Newsletters are the noisiest input this app will ever see. Two layers:

**Layer 1 — structural extraction.** Prefer the `text/html` MIME part
and run it through **trafilatura** (already a dependency), which is
purpose-built to drop boilerplate and is what the web-article path
uses. Fall back to the `text/plain` part if there's no HTML. This alone
removes most navigation, image alt-text, and layout scaffolding.

**Layer 2 — newsletter-specific heuristics** in `mailbox.py`, applied
to the extracted text:

- Drop everything after the first unsubscribe/footer marker
  (`unsubscribe`, `abbestellen`, `view in browser`, `im browser
  ansehen`, `manage preferences`, `update your preferences`,
  `you received this email because`, `diese e-mail wurde an … gesendet`).
  These are reliably *below* the content.
- Strip tracking-pixel artefacts and zero-width / `&zwnj;` padding
  (the invisible pre-header spam Mailchimp et al. inject).
- Collapse runs of `>`-quote, repeated separators, and 3+ blank lines.
- Pull the "view in browser" href into `MailMessage.web_url` before
  discarding the footer.

The markers live in a small ordered list so they're easy to extend as
real-world senders reveal new patterns. This is deliberately heuristic,
not a parser — the LLM (Layer 3) is the safety net.

**Layer 3 — the summarizer.** A newsletter-tuned system prompt does the
final signal extraction:

> You are summarizing a newsletter issue. Ignore advertising,
> sponsorships, tracking notices, unsubscribe/footer boilerplate, and
> "view in browser" links. If the issue contains multiple distinct
> stories or links, list each as a one-line headline + one-sentence
> "why it matters", so the reader can decide what to open in full.
> Lead with the single most noteworthy item.

Selected by `kind == email` in `pipeline.py`, the same way the language
and segment handling already branch on kind. A profile's
`custom_summary_prompt`, if set, still wins — power users can tune the
triage to their taste.

### `app/pipeline.py` (modify)

The email body is stored at *ingest* time (like web), so `needs_fetch`
is False and the fetch branch is skipped. Add an explicit
`kind == email` guard to `_segments_for_summarizer` (returns None,
matching web) and select the newsletter prompt. No new fetch path
inside the pipeline.

### `app/services/mail_sync.py` (new)

Mirrors `playlist_sync.py`:

```python
async def sync_mailbox(db, config, user_id: int) -> MailSyncResult
```

Per profile with `imap_enabled`: read `ImapConfig` + `imap_last_uid`
from settings, `fetch_new_messages`, then for each message:
`upsert_metadata(kind=EMAIL, …)`, `set_transcript(body, EMAIL)`,
`set_tags_for_video([sender_name])`, `jobs_repo.enqueue(...)`. Advance
`imap_last_uid` to the max UID seen. Returns counts for the heartbeat /
diagnostics page. One bad message is logged and skipped, never fails the
batch (same resilience contract as `_process_entries`).

### `app/scheduler.py` (modify)

The existing tick already loops playlists. Add a mailbox pass in the
same tick: for each profile with `imap_enabled`, call `sync_mailbox`,
wrapped in the same per-source try/except so a flaky IMAP server can't
stall playlist sync or re-embedding. Heartbeat step string
`"fetching mail (uid N)"`. The "Jetzt prüfen" diagnostics button
already calls `request_tick()`, so manual refresh works for free.

Note: the scheduler is user-1-scoped today for playlists; the mailbox
pass should iterate **all** profiles that have IMAP configured, since
the settings are per-user. This is a small, contained widening of the
tick's scope.

## Settings UI

New "Newsletter / IMAP" card on `/settings`, following the existing card
+ Test-button convention (cf. the Whisper / Embedding cards):

- Fields: enable toggle, host, port, SSL, username, app-password,
  folder.
- **Test** button round-trips a real `fetch_new_messages(..., since_uid
  = very-high)` (lists folder, fetches nothing) and reports
  "Connected, N messages in INBOX" or the auth/TLS error verbatim.
- A short helper line: "Use a dedicated mailbox and an app-specific
  password — see the docs." Links to the recommendation below.

## Templates (modify)

- `video_card.html`: `kind`-pill shows `MAIL`; placeholder glyph ✉️ when
  there's no thumbnail (newsletters rarely carry an og:image).
- `video_detail.html`: "Open original ↗" only when `web_url` is present;
  otherwise show "From: {sender}". Transcript disclosure labelled
  "Newsletter body" for email kind (cf. "Article" for web).

## Security

The user's stated threat model is "public newsletters only, security not
a concern," so V1 stores the IMAP password in the `settings` table as
plaintext, exactly like the existing LLM/Whisper API keys
(`llm_models.api_key` is already plaintext at rest). We do **not**
introduce a new secret-handling mechanism for this feature alone — that
would be inconsistent with how every other credential in the app is
stored today. The Settings card masks the password input and the field
is never echoed back in full once saved (show a `••••` placeholder),
matching the API-key fields. A note in the card recommends an
app-specific password so the user can revoke access without touching
their main account password.

## Tests

- `tests/test_services_mailbox.py` — parse a recorded multipart MIME
  fixture (HTML + plain), boilerplate stripping (unsubscribe-footer
  cut, tracking-pixel removal, view-in-browser extraction), Message-ID
  fallback, sender-name parsing.
- `tests/test_services_mail_sync.py` — incremental UID cursor advances;
  dedup via Message-ID hash; per-message failure is skipped not fatal;
  sender becomes a tag; job enqueued only for new items.
- `tests/test_pipeline.py` — `kind == email` selects the newsletter
  prompt, segments None, no fetch branch entered.
- `tests/test_db.py` — fresh-install CHECK includes `email`; migrated DB
  accepts `kind='email'` rows.
- `tests/test_routes_settings.py` — IMAP fields persist per-user;
  password is masked on read-back.

All against fixtures — no live IMAP server, mirroring the
"no live LLM calls in tests" policy.

~14 new tests.

## Where to get the mailbox (user-facing recommendation)

Captured here so the Settings helper text and README can point at it:

- **Easiest, scales best:** your own domain with a **catch-all** at
  mailbox.org or Migadu. Every newsletter can get its own
  `something@yourdomain` with zero per-newsletter setup, and yt-summary
  configures exactly one IMAP account.
- **No domain:** a dedicated **mailbox.org / Posteo** account (~1 €/mo,
  IMAP-native, one-click app passwords, EU/DE-hosted).
- **Free, more friction:** Gmail with an app password (needs 2FA + app
  password + IMAP enabled; Google is progressively deprecating basic
  IMAP, so treat as the fallback, not the default).

## Open questions

1. Poll **all** IMAP-enabled profiles in the shared scheduler tick
   (proposed) vs. keep parity with the current user-1-only playlist
   scan? Proposed: all profiles, since settings are per-user.
2. Should the sender tag be auto-created hidden, or appear in the normal
   tag-pill filter row like YouTube tags? Proposed: normal row — it's
   the whole point of the filtering UX.
3. Digest newsletters can be very long (50k+ chars). The map-reduce
   summarizer already handles long transcripts, so no special-casing —
   but worth confirming token budgets on the smallest configured models.
