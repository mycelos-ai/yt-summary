import pytest

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
