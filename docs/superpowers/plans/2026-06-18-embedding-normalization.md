# Embedding Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make KNN "related items" actually find candidates by normalizing summary embeddings to unit length and expressing the `related.py` distance threshold as a cosine value converted to L2 internally; recompute existing embeddings via the existing scheduler re-embed queue.

**Architecture:** `embed_text` gains `normalize_embeddings=True`. For unit vectors L2 distance is monotonic in cosine distance (`L2² = 2·cosine_distance`), so the vec0 table stays L2 (no schema change) and `related_video_ids` converts a cosine ceiling to an L2 ceiling. A one-time, settings-marker-gated DB migration nulls `summary_embedded_at` for all summarized videos; the existing scheduler drains the re-embed queue in the background.

**Tech Stack:** Python 3.11+, sentence-transformers, sqlite-vec (vec0), aiosqlite, pytest (asyncio_mode=auto).

## Global Constraints

- Do NOT change the `video_embeddings` vec0 table schema or add `distance_metric=cosine`. Normalization + L2 is the chosen approach.
- L2↔cosine relation for unit vectors: `L2_distance² = 2 · cosine_distance`, i.e. `l2_threshold = sqrt(2 · max_cosine_distance)`. Cosine distance ∈ [0, 2]; with the 0.75 default, `l2_threshold = sqrt(1.5) ≈ 1.2247`.
- Reuse the existing re-embed mechanism (`videos_pending_reembed` + scheduler `_reembed_pending_batch`). Do NOT build a new one.
- The migration must be idempotent, gated by a `settings` row `embeddings_normalized = "1"` (user_id=1), mirroring the existing `syntheses_threads_migrated` marker pattern in `app/db.py`.
- `embed_text` keeps raising `ValueError` on empty/whitespace input (unchanged contract).
- Tests use `asyncio_mode = "auto"` — async test functions need NO `@pytest.mark.asyncio` decorator. Run tests with `pytest` from the repo root.
- The `0.75` cosine default is inherited; keep it as the default (tuning against real data is a separate, later concern noted in the spec — not part of this plan).

---

### Task 1: Normalize embeddings at encode time

**Files:**
- Modify: `app/services/embeddings_local.py:44-49` (`_encode_sync`)
- Test: `tests/test_services_embeddings_local.py` (extend)

**Interfaces:**
- Produces: `embed_text(text)` returns a unit-length (L2 norm ≈ 1.0) 384-float list.

- [ ] **Step 1: Write the failing norm test**

Add to `tests/test_services_embeddings_local.py` (real-model style, like the existing tests in that file):

```python
async def test_embed_text_returns_unit_length_vector():
    """Embeddings must be normalized to unit length so L2 distance is a
    monotonic stand-in for cosine distance in the vec0 KNN."""
    vec = await embed_text("the quick brown fox")
    norm = sum(x * x for x in vec) ** 0.5
    assert abs(norm - 1.0) < 1e-3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_services_embeddings_local.py::test_embed_text_returns_unit_length_vector -v`
Expected: FAIL — norm is not ≈ 1.0 (vectors currently un-normalized).

(Note: this file's tests load the real sentence-transformers model. If the model cannot load in the sandbox due to HuggingFace-cache restrictions, run this test on a machine with the model cached — the same constraint already applies to the existing tests in this file.)

- [ ] **Step 3: Add the normalize flag**

In `app/services/embeddings_local.py`, change `_encode_sync` (line ~48):

```python
def _encode_sync(text: str) -> list[float]:
    """Run the actual encode call. Numpy → plain list at the boundary.

    `normalize_embeddings=True` returns unit-length vectors so that the
    vec0 table's L2 distance is a monotonic function of cosine distance
    (L2² = 2·cosine_distance) — see related.related_video_ids.
    """
    model = _load_model_sync()
    # convert_to_numpy=True keeps memory predictable; we tolist() right after.
    arr = model.encode(
        text,
        convert_to_numpy=True,
        show_progress_bar=False,
        normalize_embeddings=True,
    )
    return [float(x) for x in arr]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_services_embeddings_local.py::test_embed_text_returns_unit_length_vector -v`
Expected: PASS.

- [ ] **Step 5: Run the full file (no regression)**

Run: `pytest tests/test_services_embeddings_local.py -v`
Expected: PASS (the existing cosine-similarity / dimension / German / singleton tests still hold).

- [ ] **Step 6: Commit**

```bash
git add app/services/embeddings_local.py tests/test_services_embeddings_local.py
git commit -m "fix(embeddings): normalize vectors to unit length"
```

---

### Task 2: Cosine-distance threshold (converted to L2) in related.py

**Files:**
- Modify: `app/services/related.py` (signature line 23, docstring 25-30, filter line 41; add `import math`)
- Modify: `tests/test_services_related.py` (3 call-sites use `max_distance=`; update to the new param name and keep the assertions meaningful)

**Interfaces:**
- Consumes: vec0 L2 `distance` from `embeddings_repo.search_by_summary_vector` (unchanged).
- Produces: `related_video_ids(db, video, *, user_id, limit=5, max_cosine_distance=0.75)` — parameter renamed from `max_distance`; semantics are a cosine-distance ceiling, converted to L2 internally.

Note on the existing tests: `test_services_related.py` seeds raw vectors via `_vec(x) = [x]*384` (NOT through `embed_text`, so they are not unit-length) and asserts neighbour *ordering* and threshold behaviour. The L2→cosine conversion only changes the numeric ceiling, not ordering. The three call-sites pass `max_distance=0.75` / `max_distance=0.1`; rename the keyword to `max_cosine_distance`. Verify the `max_cosine_distance=0.1` "too far" test still excludes its far neighbour under the converted L2 ceiling `sqrt(2*0.1)=sqrt(0.2)≈0.447`; if the seed values no longer produce the intended inside/outside split, adjust the seed vector values in that test so one neighbour is clearly inside and one clearly outside `0.447` L2 — keep the test's intent (close kept, far dropped).

- [ ] **Step 1: Update the existing related tests to the new param name + meaningful thresholds**

In `tests/test_services_related.py`, replace the three `max_distance=` keywords with `max_cosine_distance=` and reconcile the threshold-behaviour test. Concretely:

- Line ~36 (`test_related_excludes_self_and_ranks_by_similarity`): `max_distance=0.75` → `max_cosine_distance=0.75`. This test only asserts ordering (`ids[0] == "b"`) and self-exclusion, which are unaffected.
- Line ~82 (`max_distance=0.75`): same rename.
- The `test_related_respects_max_distance` test (line ~63, `max_distance=0.1`): rename to `max_cosine_distance=0.1`. With `_vec(x)=[x]*384`, two vectors `[p]*384` and `[q]*384` have L2 distance `sqrt(384)·|p−q|`. The intent is "a far neighbour is excluded by a tight ceiling". Re-seed so the assertion is robust: keep the close pair within and the far one outside the converted L2 ceiling `sqrt(0.2)≈0.447`. Because `sqrt(384)≈19.6`, even tiny coordinate gaps exceed 0.447 — so to preserve the test's INTENT rather than its arithmetic, set the far neighbour far enough to be dropped and assert it is absent, and (if needed) loosen the in-test ceiling to a value that demonstrably keeps the near neighbour. Pick concrete seed values and a ceiling that make ONE neighbour present and ONE absent, and assert exactly that. Show the final test code in Step 3 here.

Write the reconciled test(s). Example shape for the threshold test (fill in seeds that produce a clean split):

```python
async def test_related_respects_max_cosine_distance(db: aiosqlite.Connection):
    await _seed(db, "a", vecval=0.50)
    await _seed(db, "b", vecval=0.50)    # identical → L2 distance 0
    await _seed(db, "c", vecval=0.90)    # far
    a = await videos_repo.get(db, "a")
    ids = await related_svc.related_video_ids(
        db, a, user_id=1, max_cosine_distance=0.1,   # L2 ceiling sqrt(0.2)≈0.447
    )
    assert "b" in ids        # identical vector, distance 0, kept
    assert "c" not in ids    # far vector exceeds the ceiling, dropped
```

(Identical-vector "b" guarantees distance 0 < any positive ceiling; "c" at 0.90 gives L2 `sqrt(384)*0.40 ≈ 7.8 > 0.447`. Clean split, intent preserved.)

- [ ] **Step 2: Run the related tests to verify they fail**

Run: `pytest tests/test_services_related.py -v`
Expected: FAIL — `related_video_ids() got an unexpected keyword argument 'max_cosine_distance'` (param not renamed yet).

- [ ] **Step 3: Rename the param and convert to L2**

In `app/services/related.py`, add `import math` at the top with the other imports, then update the function (lines ~17-42):

```python
async def related_video_ids(
    db: aiosqlite.Connection,
    video: Video,
    *,
    user_id: int,
    limit: int = 5,
    max_cosine_distance: float = 0.75,
) -> list[str]:
    """Ids of items related to `video`, closest first.

    Empty when the item has no embedding. Excludes: the item itself,
    items belonging to other profiles, and other-profile copies of the
    same source (same youtube_id or url). Keeps only neighbours within
    `max_cosine_distance` (cosine distance, [0, 2]); caps at `limit`.

    Embeddings are unit-length (see embeddings_local), so the vec0
    table's L2 distance is a monotonic function of cosine distance:
    L2² = 2·cosine_distance. We convert the cosine ceiling to an L2
    ceiling and compare against the L2 `distance` vec0 returns."""
    own = await embeddings_repo.get_summary_embedding(db, video.id)
    if own is None:
        return []
    l2_threshold = math.sqrt(2 * max_cosine_distance)
    # Over-fetch: the KNN index is global (all profiles), and we filter
    # down to this profile afterwards, so ask for more than `limit`.
    hits = await embeddings_repo.search_by_summary_vector(
        db, own, limit=max(limit * 5, 25),
    )
    candidate_ids = [
        vid for vid, dist in hits
        if vid != video.id and dist <= l2_threshold
    ]
```

(Leave the rest of the function — the active/profile/dedup filtering — unchanged.)

- [ ] **Step 4: Run the related tests to verify they pass**

Run: `pytest tests/test_services_related.py -v`
Expected: PASS.

- [ ] **Step 5: Check no other caller passed the old keyword**

Run: `grep -rn "max_distance" app/ tests/`
Expected: no results (all renamed). If any remain, update them to `max_cosine_distance` and re-run the affected tests. Note: `compute_related_links` in `app/services/related_links.py` calls `related_video_ids` WITHOUT the keyword (uses the default), so it needs no change — confirm via grep.

- [ ] **Step 6: Commit**

```bash
git add app/services/related.py tests/test_services_related.py
git commit -m "fix(related): express KNN threshold as cosine distance, convert to L2"
```

---

### Task 3: One-time re-embed migration

**Files:**
- Modify: `app/db.py` — add a settings-marker-gated block in `init_schema` (next to the `syntheses_threads_migrated` block, ~line 719-729)
- Test: `tests/test_db_migration_embeddings_normalized.py` (create)

**Interfaces:**
- Consumes: `settings` table (marker `embeddings_normalized`), `videos.summary_embedded_at`.
- Produces: after init on an existing DB, every video with a non-NULL `summary` has `summary_embedded_at = NULL` (queued for re-embed) and the marker is set; re-running init is a no-op.

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_db_migration_embeddings_normalized.py`:

```python
import asyncio

import aiosqlite

from app.config import Config
from app.db import connect, init_schema


def test_existing_summaries_queued_for_reembed_once(tmp_path):
    async def scenario():
        cfg = Config(data_dir=tmp_path)
        cfg.ensure_dirs()
        conn = await connect(cfg)
        await init_schema(conn)
        # Seed two videos: one with a summary (already embedded), one without.
        await conn.execute(
            "INSERT INTO videos (id, url, title, summary, summary_embedded_at,"
            " created_at, updated_at) VALUES "
            "('v1','u1','t1','some summary','2026-01-01T00:00:00',"
            " datetime('now'), datetime('now'))"
        )
        await conn.execute(
            "INSERT INTO videos (id, url, title, summary, summary_embedded_at,"
            " created_at, updated_at) VALUES "
            "('v2','u2','t2',NULL,NULL, datetime('now'), datetime('now'))"
        )
        # Pretend the normalization migration hasn't run yet.
        await conn.execute(
            "DELETE FROM settings WHERE user_id=1 AND key='embeddings_normalized'"
        )
        await conn.commit()

        # Re-run init: should null summary_embedded_at for v1, set marker.
        await init_schema(conn)
        cur = await conn.execute(
            "SELECT summary_embedded_at FROM videos WHERE id='v1'"
        )
        v1_embedded = (await cur.fetchone())[0]
        cur = await conn.execute(
            "SELECT value FROM settings WHERE user_id=1 AND key='embeddings_normalized'"
        )
        marker = (await cur.fetchone())[0]

        # Set a fake embedded timestamp on v1, run init again — must be a no-op
        # (marker present, so v1 stays embedded).
        await conn.execute(
            "UPDATE videos SET summary_embedded_at='2026-02-02T00:00:00' WHERE id='v1'"
        )
        await conn.commit()
        await init_schema(conn)
        cur = await conn.execute(
            "SELECT summary_embedded_at FROM videos WHERE id='v1'"
        )
        v1_after_second = (await cur.fetchone())[0]
        await conn.close()
        return v1_embedded, marker, v1_after_second

    v1_embedded, marker, v1_after_second = (
        asyncio.get_event_loop().run_until_complete(scenario())
    )
    assert v1_embedded is None          # queued for re-embed
    assert marker == "1"                # marker set
    assert v1_after_second == "2026-02-02T00:00:00"  # second run = no-op
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db_migration_embeddings_normalized.py -v`
Expected: FAIL — `v1_embedded` is still the seeded timestamp (migration block not present yet).

- [ ] **Step 3: Add the migration block**

In `app/db.py`, `init_schema`, after the `syntheses_threads_migrated` block (~line 729, before the user-seed block), add:

```python
    # Embedding normalization: vectors are now unit-length (see
    # embeddings_local.embed_text). Existing embeddings were computed
    # un-normalized, so their L2 distances are on the wrong scale and the
    # related-items KNN never matched. Null summary_embedded_at for all
    # summarized videos so the scheduler's existing re-embed queue
    # recomputes them. Gated by a settings marker so it runs exactly once.
    cur = await conn.execute(
        "SELECT value FROM settings "
        "WHERE user_id=1 AND key='embeddings_normalized'"
    )
    if await cur.fetchone() is None:
        await conn.execute(
            "UPDATE videos SET summary_embedded_at = NULL "
            "WHERE summary IS NOT NULL"
        )
        await conn.execute(
            "INSERT INTO settings (user_id, key, value) "
            "VALUES (1, 'embeddings_normalized', '1')"
        )
        await conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_db_migration_embeddings_normalized.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_db_migration_embeddings_normalized.py
git commit -m "fix(embeddings): one-time re-embed migration for normalized vectors"
```

---

### Task 4: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the whole suite**

Run: `pytest -q`
Expected: all pass. (If `test_services_embeddings_local.py` / `test_services_model_info.py` fail ONLY due to HuggingFace-cache/SOCKS sandbox restrictions, re-run those outside the sandbox to confirm they pass with the model cached — those failures are environmental, not feature regressions.)

- [ ] **Step 2: Confirm the substrate fix in the app (optional, user-driven)**

After deploying, the scheduler drains the re-embed queue (watch `/diagnostics` or logs for "re-embedded N videos"). Once drained, reindex a video that has genuinely similar neighbours and confirm a non-empty curated "Related" block appears. (This is the real-world payoff; it depends on the user's library actually containing similar items.)

---

## Self-Review

**Spec coverage:**
- Normalize at encode (`normalize_embeddings=True`) → Task 1. ✓
- Cosine threshold converted to L2 in related.py → Task 2. ✓
- One-time settings-marker-gated re-embed migration reusing the scheduler → Task 3. ✓
- Reuse existing re-embed queue (no new mechanism) → Task 3 relies on the existing scheduler; no scheduler change needed (it already drains `videos_pending_reembed`). ✓
- No vec0 schema change / no `distance_metric` → honored (Task 2 keeps L2). ✓
- Tests: norm assertion (Task 1), threshold conversion (Task 2), migration idempotency (Task 3). ✓

**Type/name consistency:** Parameter renamed `max_distance` → `max_cosine_distance` consistently in Task 2 (signature, the 3 test call-sites, and the grep check); `related_links.py` uses the default and is unaffected (Task 2 Step 5 confirms). Marker string `embeddings_normalized` consistent across Task 3 (migration + test). `l2_threshold = math.sqrt(2 * max_cosine_distance)` consistent with the Global Constraints formula.

**Placeholder scan:** No TBD/TODO. Task 2 Step 1 leaves the implementer to pick final seed values for the threshold test but gives a concrete, worked example (identical-vector "b" + far "c") that produces a clean split — that is guidance for a known-tricky arithmetic reconciliation, not a deferred decision; the example code is complete and usable as-is.
