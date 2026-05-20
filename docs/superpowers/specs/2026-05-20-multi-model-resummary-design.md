# Multi-model configuration & per-run resummary override

## Problem

Today the app holds exactly one LLM configuration: `settings.llm_model`,
`settings.llm_api_key`, `settings.llm_base_url`. Quick Setup writes
that one slot. Re-summarize re-uses it.

That's painful in two ways:

1. The local Ollama model is cheap and usually fine, but sometimes
   produces a weak summary. The user wants to retry with a stronger
   cloud model **for that one video** without permanently switching
   the global default — which would then bill cloud calls for every
   subsequent auto-import from playlists.
2. A retry today is dumb: same prompt, same model, same result. The
   user wants to add a one-shot instruction ("focus on the named
   frameworks, keep it shorter") on the retry.

## Goal

- Configure N LLM models in Settings, mark one as default.
- Default model drives all background work (submit, playlist
  auto-import). No behaviour change there.
- "Re-summarize" on the video detail page opens an inline panel where
  the user picks a model (default pre-selected) and optionally adds a
  one-shot instruction.
- The chat box on the video detail page gets a compact model selector
  for the same reason (a chat answer can be retried with a stronger
  model).
- MCP exposes the available models by **plain-text label** in tool
  docstrings so Claude knows what `llm_model_id=2` actually means.
- One-shot overrides are not persisted beyond the job they trigger.

## Non-goals

- No fallback chains (if default fails, no automatic retry on a
  different model).
- No per-profile or per-playlist default model — single global
  default for now.
- No summary version history — each new summary overwrites the
  previous one, as today.
- No model override for MCP `ask_video` — overkill for a chat tool;
  MCP-driven chat always uses the default.

## Data model

### New table `llm_models`

```sql
CREATE TABLE llm_models (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  label       TEXT    NOT NULL,                    -- user-chosen, e.g. "Claude Sonnet 4.6"
  provider_id TEXT    NOT NULL,                    -- 'openai' | 'anthropic' | 'gemini' |
                                                   --   'groq' | 'ollama' | 'openrouter' |
                                                   --   'custom'
  model       TEXT    NOT NULL,                    -- e.g. 'anthropic/claude-sonnet-4-6'
  api_key     TEXT    NOT NULL DEFAULT '',         -- empty for ollama
  base_url    TEXT    NOT NULL DEFAULT '',         -- empty unless self-hosted / ollama
  is_default  INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX idx_llm_models_default
  ON llm_models(is_default)
  WHERE is_default = 1;
```

The partial unique index guarantees at most one default row. Setting a
new default is a transaction: `UPDATE llm_models SET is_default=0`
then `UPDATE llm_models SET is_default=1 WHERE id=?`.

### Two new columns on `jobs`

```sql
ALTER TABLE jobs ADD COLUMN llm_model_id     INTEGER REFERENCES llm_models(id) ON DELETE SET NULL;
ALTER TABLE jobs ADD COLUMN additional_prompt TEXT;
```

Both `NULL` for background work (submit, auto-import). `ON DELETE SET
NULL` so deleting a model in Settings doesn't break the FK on
completed-but-still-referenced jobs.

### Settings table

The following keys are removed: `llm_model`, `llm_api_key`,
`llm_base_url`. Everything else stays (`whisper_*`, `summary_language`,
playlist/TTS/embedding config).

## Migration

Added to `app/db.py` `_run_migrations()` as a new feature-gated block
(matching the existing pattern — each migration is gated on a check
like "does this column exist?" / "does this table exist?" so re-runs
are no-ops). Steps:

1. Create `llm_models` and the partial unique index.
2. If `settings.llm_model` exists and is non-empty:
   - Resolve `provider_id` by matching the prefix of `llm_model`
     against `PROVIDER_PRESETS` (same `head.startswith(p.litellm_provider)`
     logic already used in `routes/settings.settings_page` to detect
     the current provider). Default to `'custom'` when nothing matches.
   - `label` defaults to the preset's `name` field (e.g. `"OpenAI"`,
     `"Ollama (local)"`) — or `"Custom"` for the fallback case.
   - Insert one row into `llm_models` with `is_default = 1`.
   - `api_key` and `base_url` copied from `settings.llm_api_key` /
     `settings.llm_base_url`.
3. Delete `settings.llm_model`, `settings.llm_api_key`,
   `settings.llm_base_url`.
4. Add the two columns on `jobs`. (Both nullable, no default needed
   for existing rows — the worker treats `NULL` as "use the default
   model".)

Fresh installs end up with an empty `llm_models` table; Settings
guides the user through adding the first model the same way Quick
Setup did before.

## Component changes

### Repos

New module `app/repos/llm_models.py`:

```python
list_all(db) -> list[LlmModel]                  # ordered: default first, then by label
get(db, id) -> LlmModel | None
get_default(db) -> LlmModel | None              # returns None on fresh install
insert(db, *, label, provider_id, model, api_key, base_url, make_default) -> int
update(db, id, *, label, model, api_key, base_url) -> None
delete(db, id) -> None                           # 409 if id is current default
set_default(db, id) -> None                      # transactional swap
```

`app/repos/jobs.py`:

- `enqueue` gains two optional kwargs: `llm_model_id: int | None`,
  `additional_prompt: str | None`. Both default to `None`. Existing
  callers (`routes/videos.submit_video`, `routes/videos.reindex_video`
  before its rewrite, `playlist_sync`) pass nothing → default behaviour.
- `_row_to_job` reads the two new columns into the dataclass.

`app/repos/settings.py`: unchanged (still owns whisper, language,
playlist refresh, TTS defaults).

### Models (`app/models.py`)

```python
@dataclass
class LlmModel:
    id: int
    label: str
    provider_id: str
    model: str
    api_key: str
    base_url: str
    is_default: bool
    created_at: datetime
    updated_at: datetime

@dataclass
class Job:
    # ... existing fields ...
    llm_model_id: int | None = None
    additional_prompt: str | None = None
```

### Pipeline (`app/pipeline.py`)

`process_video` signature gains the override fields:

```python
async def process_video(
    db, config, video_id, set_step,
    *,
    llm_model_id: int | None = None,
    additional_prompt: str | None = None,
):
```

Resolution:

```python
model_row = (
    await llm_models_repo.get(db, llm_model_id) if llm_model_id is not None
    else await llm_models_repo.get_default(db)
)
if model_row is None:
    await set_step("transcript only (no LLM model configured)")
    return
model = model_row.model
api_key = model_row.api_key
base_url = model_row.base_url or None
```

The `settings.get("llm_model" / "llm_api_key" / "llm_base_url")` reads
are removed. `additional_prompt` is threaded into `summarize(...)`.

### Summarizer (`app/services/summarizer.py`)

`summarize()` and the two prompt-builders accept an
`additional_prompt: str | None`. When non-empty, both
`build_system_prompt` and `build_reduce_prompt` append the block:

```
USER OVERRIDE FOR THIS RUN:
<additional_prompt verbatim>
```

at the very end (after the timestamp instruction). Map-reduce sees the
same override on every chunk and on the reduce step. The block is
omitted entirely when `additional_prompt` is empty/None — no
ceremonial "no override" line.

### Worker (`app/worker.py`)

`run_one` claims a job, reads `llm_model_id` and `additional_prompt`
off the dataclass, passes them as kwargs to `process_video`. No other
logic changes — heartbeat, error handling, rollback all unchanged.

### Routes

#### `app/routes/settings.py`

- `GET /settings` now renders the new "Configured models" card on top.
  Passes `llm_models = await llm_models_repo.list_all(db)` to the
  template. Drops the `current_provider_id` / `preset_chat_models` /
  `preset_chat_models_full` pieces tied to the old Quick Setup.
- The Quick Setup form keeps its provider-tile / preset / model
  dropdown UI but now POSTs to `/settings/llm-models` (add) or
  `/settings/llm-models/{id}` (edit), each writing one row in
  `llm_models`. It gains a `label` field (required, autosuggested
  client-side from provider + model).
- New endpoints:
  - `POST /settings/llm-models` — insert
  - `POST /settings/llm-models/{id}` — update
  - `POST /settings/llm-models/{id}/default` — set default (transactional)
  - `POST /settings/llm-models/{id}/delete` — delete (409 if it's the
    current default)
  - `POST /settings/llm-models/{id}/test` — replaces the old `test-llm`
    endpoint. Probes the row (Ollama reachability check unchanged for
    Ollama rows; otherwise a one-shot litellm call). HTMX target is a
    per-row result span.
  - `GET /settings/llm-models/ollama-models` — replaces the old
    `quick-setup/ollama-models` route (identical body).
- Removed endpoints: `/settings/quick-setup`, `/settings/test-llm`.
- The "Language model" card inside `<details class="settings-advanced">`
  is removed — the Configured-models list above replaces it. The
  `<details>` block now wraps only the Whisper card. (Whisper Quick
  Setup is unaffected because whisper config still lives in `settings`.)

#### `app/routes/videos.py`

- `POST /v/{video_id}/reindex` accepts two optional form fields:
  `llm_model_id: int | None`, `additional_prompt: str` (treated as
  None when whitespace-only). Forwards them to `jobs_repo.enqueue`.
- `GET /v/{video_id}` passes `llm_models` to the template so the
  resummary panel can render the dropdown.

#### `app/routes/chat.py`

- `POST /v/{video_id}/chat` accepts `llm_model_id: int | None` as a
  form field. When None → default model. Service layer
  (`services/chat.py`) resolves via `llm_models_repo` instead of
  reading `settings`.
- `GET /v/{video_id}` (handled in `routes/videos.py`) passes models
  to the chat form too.

#### `app/routes/mcp.py`

- Tool registration now reads `await llm_models_repo.list_all(db)` at
  startup and bakes the list into the docstrings of `submit_url`,
  `resummarize`, `list_models`. Format: `Available llm_model_id
  values: 1='Claude Sonnet 4.6' (default), 2='Ollama Llama 3.1'.`
- New tool `list_models()` returns `[{id, label, model, is_default}]`.
- `submit_url` gains `llm_model_id: int | None = None`,
  `additional_prompt: str = ""`. Both pass through to
  `api_svc.submit_video` which already calls `enqueue` — wired the
  same way as the HTTP route.
- New tool `resummarize(video_id, *, llm_model_id=None,
  additional_prompt="") -> dict` mirroring the HTTP reindex.
- `ask_video` is unchanged — no model override exposed (kept simple).

### Services (`app/services/api.py`)

- `submit_video` gains `llm_model_id` + `additional_prompt` kwargs,
  forwarded to `jobs_repo.enqueue`.
- `reindex_video` gains the same kwargs.
- `chat_about_video` resolves the model via `llm_models_repo` (default
  when not passed) instead of reading `settings`.

## UI details

### Settings: "Configured models" card

Renders above Quick Setup. One row per `llm_models` entry. The default
row has a left accent border (`border-left: 4px solid var(--accent-success)`)
and a `[Default ✓]` badge; others show a `[Make default]` button.

```
┌──────────────────────────────────────────────────────────────┐
│ █ Claude Sonnet 4.6                       [Default ✓]        │
│   anthropic/claude-sonnet-4-6 · Anthropic                    │
│   [Test] [Edit] [Delete (disabled)]                          │
├──────────────────────────────────────────────────────────────┤
│   Ollama Llama 3.1                        [Make default]     │
│   ollama_chat/llama3.1 · http://192.168.0.27:11434           │
│   [Test] [Edit] [Delete]                                     │
└──────────────────────────────────────────────────────────────┘

[+ Add model]   (scrolls to the Quick-Setup wizard below)
```

`[Test]` performs the round-trip and surfaces the result inline in the
row (HTMX target is `#llm-test-{id}` next to the buttons).

`[Edit]` opens the Quick-Setup wizard pre-populated with the row's
values and an `?edit=<id>` query param. The provider tile is
pre-selected; switching it during edit is allowed but rare. Submit
POSTs to `/settings/llm-models/{id}`.

`[Delete]` is disabled (visually + `disabled` attribute + tooltip "Make
another model default first") on the default row. On non-default rows
it submits a tiny POST form with `onsubmit="return confirm(...)"`.

### Quick Setup wizard

Visually identical to today, with two additions:

1. A required `<input name="label">` near the top, autosuggested
   client-side via Alpine from provider name + selected model
   (`Claude Sonnet 4.6`, `Ollama Llama 3.1`). The user can edit it.
2. The submit button label changes from "Apply preset" to
   `Add model` (or `Save changes` in edit mode).

### Video detail: resummary panel

The current `<form action="/v/{id}/reindex">` becomes an Alpine
`x-data="{open:false}"` toggle. The Re-summarize button toggles the
panel; the panel contains:

- `<select name="llm_model_id">` listing all `llm_models` entries
  (default first, marked with ` (Default)` in the label).
- `<textarea name="additional_prompt" rows="3" placeholder="…">`.
- `[Cancel]` (sets `open=false`) and `[Re-summarize now]` (submit).

Submit triggers a normal POST; the route does a 303 redirect back to
the detail page so the user sees the freshly-enqueued state.

### Video detail: chat model selector

Below the chat input (inside the existing chat form, before the Send
button), a small second row:

```
Model: [● Claude Sonnet 4.6 (Default) ▼]
```

`<select name="llm_model_id">` of the same model list. Selection
persists across submits within the same page — the existing
`this.reset()` only clears the text input, not the select. No
persistence beyond page lifetime; reloading the page reverts to
default.

## Error handling

- **No models configured (fresh install)**: pipeline behaves as
  today's "no `llm_model` setting" branch — set step to "transcript
  only (no LLM model configured)" and exit cleanly. Settings page
  renders an empty "Configured models" card with a prominent
  `[+ Add your first model]` CTA pointing at Quick Setup.
- **`llm_model_id` references a deleted model**: `FK ON DELETE SET
  NULL` reduces this to "no override" → falls back to default. If the
  default is also gone (cleared after the FK fired), same handling as
  fresh-install case.
- **Model test fails**: same UX as today — the result span shows
  `⚠ {ErrorClass}: {message}`. Nothing is persisted.
- **Delete-default attempt**: the route returns 409 with a friendly
  message; the UI prevents it client-side via the disabled button, so
  this only fires for direct API calls.

## Testing

Existing test patterns in this repo:
- Unit tests on repos and services (sqlite in-memory).
- Route tests via `TestClient` with the app's lifespan.
- Migration tests that simulate an older DB shape, then run
  `_run_migrations()` and assert the resulting state.

Specific cases worth covering:

- Migration: starting from a pre-migration DB with `settings.llm_model`
  populated produces exactly one `llm_models` row, `is_default=1`,
  and the three settings keys are gone.
- Migration: starting from a pre-migration DB without `llm_model` produces
  no rows.
- Default invariant: `set_default(other_id)` flips both rows in one
  transaction; concurrent calls don't violate the partial unique
  index (single-writer SQLite).
- Pipeline: passing `llm_model_id=X` uses row X's model/key/base_url;
  passing `None` uses the default row.
- Pipeline: `additional_prompt` appears verbatim in the system prompt
  in both single-shot and map-reduce paths.
- Worker: a job with non-NULL override fields runs through pipeline
  with those kwargs.
- Routes: `POST /v/{id}/reindex` with no body fields enqueues a job
  with both override columns NULL; with fields populated enqueues
  with them set.
- MCP: `submit_url` and `resummarize` tools accept the new params and
  forward them; tool docstring contains the model labels at register
  time.
- Chat: `POST /v/{id}/chat` with `llm_model_id` uses that model; with
  `llm_model_id=""` (form-empty) treats as default.
- Settings: add/edit/delete/test/default endpoints work; deleting the
  default row 409s.

## Open questions

None — the brainstorming conversation has resolved scope, storage,
UI, prompt mechanics, and MCP behaviour.

## Out of scope (explicit non-shipping)

- Summary version history / past attempts list.
- Per-profile default model.
- Automatic fallback when the default model fails.
- Per-playlist default model.
- Cost / token accounting per model.

These can be layered on later. The data model accommodates them
without changes: per-profile would add a `user_id` column to
`llm_models`; per-playlist would add `default_llm_model_id` to
`playlists`; cost tracking is an additive `usage` table referencing
`llm_models.id`.
