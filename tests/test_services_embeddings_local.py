import pytest

from app.services import embeddings_local
from app.services.embeddings_local import EMBEDDING_DIM, embed_text


async def test_embed_text_returns_correct_dimension():
    """First call loads the model; result must be EMBEDDING_DIM floats."""
    vec = await embed_text("hello world")
    assert isinstance(vec, list)
    assert len(vec) == EMBEDDING_DIM
    assert all(isinstance(x, float) for x in vec)


async def test_embed_text_handles_german():
    """Multilingual MiniLM must produce a non-zero vector for German."""
    vec = await embed_text("Hallo Welt, das ist ein Test.")
    assert len(vec) == EMBEDDING_DIM
    # Smoke check: not all zeros (would mean tokenization died silently).
    assert any(abs(x) > 1e-6 for x in vec)


async def test_embed_text_rejects_empty():
    with pytest.raises(ValueError):
        await embed_text("")
    with pytest.raises(ValueError):
        await embed_text("   ")


async def test_embed_text_singleton_reuses_model():
    """Two calls share the same loaded model — no second download."""
    from app.services import embeddings_local
    # Reset the singleton to force a "first load" if the test runs in
    # isolation; subsequent assert proves the second call hits the cache.
    embeddings_local._model = None
    await embed_text("warm up")
    first_id = id(embeddings_local._model)
    await embed_text("again")
    second_id = id(embeddings_local._model)
    assert first_id == second_id


async def test_embed_text_similar_strings_have_similar_vectors():
    """Sanity: 'cat' and 'kitten' should be closer than 'cat' and 'banking'.

    Cosine similarity, computed manually because we don't want a numpy
    dep just for tests.
    """
    a = await embed_text("cat")
    b = await embed_text("kitten")
    c = await embed_text("banking")

    def cos(u: list[float], v: list[float]) -> float:
        dot = sum(x * y for x, y in zip(u, v, strict=True))
        nu = sum(x * x for x in u) ** 0.5
        nv = sum(x * x for x in v) ** 0.5
        return dot / (nu * nv)

    assert cos(a, b) > cos(a, c)


async def test_embed_text_returns_unit_length_vector():
    """Embeddings must be normalized to unit length so L2 distance is a
    monotonic stand-in for cosine distance in the vec0 KNN."""
    vec = await embed_text("the quick brown fox")
    norm = sum(x * x for x in vec) ** 0.5
    assert abs(norm - 1.0) < 1e-3


# ---------------------------------------------------------------------------
# Part C: fast-fail when the model is not cached locally
# ---------------------------------------------------------------------------

async def test_embed_text_fast_fails_when_uncached(monkeypatch):
    """When the model is NOT cached locally, embed_text must raise RuntimeError
    immediately — no network attempt, no DNS-retry hang.

    Forces the uncached path by: resetting the singleton to None (so no
    already-loaded model can be returned) and patching _model_is_cached to
    return False (so the gate fires regardless of real cache state).
    """
    monkeypatch.setattr(embeddings_local, "_model", None)
    monkeypatch.setattr(embeddings_local, "_model_is_cached", lambda: False)

    with pytest.raises(RuntimeError, match="not cached"):
        await embeddings_local.embed_text("hello")


async def test_embed_text_loads_when_cached(monkeypatch):
    """When the model IS cached and the singleton is None, embed_text must
    attempt to load (the gate must NOT block a legitimate cached load).

    We patch _model_is_cached=True and inject a fake model (via a stubbed
    _load_model_sync) so no real network or model file is needed — the point
    is only that the cached path is NOT blocked by the fast-fail gate.
    """
    class _FakeModel:
        """Minimal SentenceTransformer stand-in."""
        def encode(self, text, **kw):
            import array
            # Return a tiny valid-enough float sequence (tolist() compatible).
            return array.array("f", [0.5, 0.5])

    monkeypatch.setattr(embeddings_local, "_model", None)
    monkeypatch.setattr(embeddings_local, "_model_is_cached", lambda: True)

    def _patched_load_sync():
        # Set the singleton exactly as the real _load_model_sync would, but
        # without importing/loading SentenceTransformer.
        embeddings_local._model = _FakeModel()
        return embeddings_local._model

    monkeypatch.setattr(embeddings_local, "_load_model_sync", _patched_load_sync)

    vec = await embeddings_local.embed_text("hello")
    assert isinstance(vec, list)
    # The fake encode returns two floats
    assert len(vec) == 2


def test_model_loader_forces_local_files_only(monkeypatch):
    """A partial cache must never make SentenceTransformer contact HF."""
    seen = {}

    class _FakeModel:
        pass

    def fake_constructor(model_name, **kwargs):
        seen["model_name"] = model_name
        seen.update(kwargs)
        return _FakeModel()

    monkeypatch.setattr(embeddings_local, "_model", None)
    monkeypatch.setattr(embeddings_local, "_model_is_cached", lambda: True)
    monkeypatch.setattr(
        "sentence_transformers.SentenceTransformer", fake_constructor,
    )

    assert isinstance(embeddings_local._load_model_sync(), _FakeModel)
    assert seen["model_name"] == embeddings_local.MODEL_NAME
    assert seen["local_files_only"] is True
