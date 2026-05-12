def test_chunker_returns_single_chunk_for_short_text():
    from app.services.translation import chunk_text
    text = "Short paragraph one.\n\nShort paragraph two."
    chunks = chunk_text(text, target_words=1500)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_chunker_splits_on_paragraph_boundaries():
    from app.services.translation import chunk_text
    para = ("word " * 1000).strip()
    text = "\n\n".join([para, para, para])  # ~3000 words
    chunks = chunk_text(text, target_words=1500)
    assert len(chunks) == 2
    # Each chunk should be one of the three paragraphs (or two of them)
    for c in chunks:
        words = c.split()
        assert 800 <= len(words) <= 2200


def test_chunker_splits_paragraph_too_large_on_sentences():
    """A single 3000-word paragraph must split into multiple chunks
    at sentence boundaries, not be passed through whole."""
    from app.services.translation import chunk_text
    sentence = "This is a sentence with about ten words in it. "
    paragraph = sentence * 300  # 3000 words, single paragraph
    chunks = chunk_text(paragraph.strip(), target_words=1500)
    assert len(chunks) >= 2
    # No chunk is dramatically larger than target
    for c in chunks:
        assert len(c.split()) <= 1800  # 1500 target + tolerance


def test_chunker_preserves_total_word_count():
    """The chunker must not drop or duplicate words."""
    from app.services.translation import chunk_text
    text = (
        "Paragraph one with several words.\n\n"
        + ("Filler sentence to grow the text. " * 400)
        + "\n\nFinal paragraph."
    )
    chunks = chunk_text(text.strip(), target_words=1500)
    total = sum(len(c.split()) for c in chunks)
    assert total == len(text.split())


def test_build_translation_prompt_includes_context_sections():
    from app.services.translation import build_prompt
    prompt = build_prompt(
        source_lang="English",
        target_lang="German",
        chunk="Translate this.",
        previous_tail="Last words of the prior chunk.",
        next_head="First words of the next chunk.",
    )
    assert "English" in prompt and "German" in prompt
    assert "<TRANSLATE>" in prompt
    assert "Translate this." in prompt
    assert "<CONTEXT_BEFORE>" in prompt
    assert "Last words of the prior chunk." in prompt
    assert "<CONTEXT_AFTER>" in prompt
    assert "First words of the next chunk." in prompt


def test_build_translation_prompt_omits_context_when_none():
    from app.services.translation import build_prompt
    prompt = build_prompt(
        source_lang="English", target_lang="German",
        chunk="x", previous_tail=None, next_head=None,
    )
    assert "<CONTEXT_BEFORE>" not in prompt
    assert "<CONTEXT_AFTER>" not in prompt


def test_overlap_tail_extracts_last_n_words():
    from app.services.translation import overlap_tail
    text = " ".join(f"word{i}" for i in range(100))
    tail = overlap_tail(text, n=10)
    assert tail.split() == [f"word{i}" for i in range(90, 100)]


def test_overlap_head_extracts_first_n_words():
    from app.services.translation import overlap_head
    text = " ".join(f"word{i}" for i in range(100))
    head = overlap_head(text, n=10)
    assert head.split() == [f"word{i}" for i in range(10)]


async def test_translate_calls_completer_once_per_chunk():
    from app.services.translation import translate

    calls: list[str] = []

    async def fake_complete(prompt: str) -> str:
        calls.append(prompt)
        # Echo the chunk back so we can assert ordering
        body = prompt.split("<TRANSLATE>\n", 1)[1].split("\n</TRANSLATE>", 1)[0]
        return f"[de]{body}"

    text = "Paragraph one.\n\n" + ("filler. " * 800) + "\n\nParagraph three."
    out = await translate(
        text, source_language="en", target_language="de",
        complete=fake_complete, target_words=500,
    )
    assert len(calls) >= 2
    assert out.startswith("[de]Paragraph one.")
    assert out.endswith("Paragraph three.")  # trailing chunk preserved


async def test_translate_skips_when_languages_match():
    from app.services.translation import translate

    async def boom(_: str) -> str:
        raise AssertionError("complete() should not be called")

    out = await translate(
        "anything", source_language="de", target_language="de",
        complete=boom,
    )
    assert out == "anything"


def test_chunker_hard_splits_unpunctuated_long_paragraph():
    """An auto-caption paragraph with no sentence-ending punctuation
    must still be split — the hard word-count fallback prevents an
    unbounded chunk from sneaking past."""
    from app.services.translation import chunk_text
    text = ("word " * 3000).strip()
    chunks = chunk_text(text, target_words=1000)
    assert len(chunks) >= 3
    for c in chunks:
        assert len(c.split()) <= 1000


def test_build_prompt_escapes_user_supplied_fence_tags():
    """A transcript that contains literal <TRANSLATE> must not
    break the prompt's tag-based structure."""
    from app.services.translation import build_prompt
    chunk = "We use <TRANSLATE>cdata</TRANSLATE> in XML."
    prompt = build_prompt(
        source_lang="English", target_lang="German",
        chunk=chunk, previous_tail=None, next_head=None,
    )
    # The structural <TRANSLATE> open tag appears twice: once in the
    # instruction prose and once as the actual fence delimiter.
    # The </TRANSLATE> close tag appears exactly once (only as fence).
    assert prompt.count("<TRANSLATE>") == 2
    assert prompt.count("</TRANSLATE>") == 1
    # The user-supplied occurrences were rewritten.
    assert "⟨TRANSLATE⟩" in prompt
    assert "⟨/TRANSLATE⟩" in prompt
