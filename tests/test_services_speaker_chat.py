# tests/test_services_speaker_chat.py
from app.models import Speaker


def _speaker(**kw):
    base = dict(
        id=1, user_id=1, known_speaker_id=None, name="Chamath",
        name_key="chamath", role="investor", avatar_id="adult-techreviewer-m",
        avatar_photo_path=None, style_note="blunt, fast-moving investor tone",
        is_active=True, created_at=None, updated_at=None,
    )
    base.update(kw)
    return Speaker(**base)


_CLAIMS = [
    {"claim": "SPACs are mispriced", "topic": "markets", "source_title": "All-In Ep 1",
     "evidence_start_s": 42, "attribution_method": "explicit_name",
     "attribution_confidence": 0.95, "review_status": "accepted"},
    {"claim": "rates stay higher for longer", "topic": "macro", "source_title": "All-In Ep 9",
     "evidence_start_s": 600, "attribution_method": "llm_inferred",
     "attribution_confidence": 0.4, "review_status": "unreviewed"},
]


def test_prompt_carries_all_grounding_clauses():
    from app.services.speaker_chat import build_speaker_system_prompt
    p = build_speaker_system_prompt(
        speaker=_speaker(), claims=_CLAIMS, source_context="some transcript text")
    # in-character + simulation boundary
    assert "Chamath" in p
    assert "NOT the real" in p
    assert "first person" in p.lower()
    # viewer-language
    assert "SAME language" in p
    # extracted-from-sources framing, NOT "actually said"
    assert "extracted from" in p.lower()
    assert "actually said" not in p.lower()
    # attribution tags + low-confidence hedge
    assert "attribut" in p.lower()
    assert "tentativ" in p.lower() or "hedge" in p.lower() or "more tentatively" in p.lower()
    # transcript = context only
    assert "context" in p.lower() and "ONLY" in p
    # anti-other-speaker rule
    assert "other speakers" in p.lower()
    # contradictions handled honestly
    assert "contradiction" in p.lower()
    # never invent
    assert "invent" in p.lower()
    # do NOT self-disclaim as AI in the reply
    assert "break character" in p.lower()
    assert "style_note" not in p           # the label, not the literal placeholder
    assert "blunt, fast-moving investor tone" in p
    # the claims are rendered with their attribution + source
    assert "SPACs are mispriced" in p
    assert "All-In Ep 1" in p


def test_prompt_seed_block_only_when_seeded():
    from app.services.speaker_chat import build_speaker_system_prompt
    p_no = build_speaker_system_prompt(
        speaker=_speaker(), claims=[], source_context="ctx")
    assert "12:04" not in p_no
    p_seed = build_speaker_system_prompt(
        speaker=_speaker(), claims=[], source_context="ctx",
        seed_ts="12:04", seed_quote="the quote")
    assert "12:04" in p_seed
    assert "the quote" in p_seed


def test_hedge_instruction_has_numeric_threshold():
    from app.services.speaker_chat import build_speaker_system_prompt
    p = build_speaker_system_prompt(
        speaker=_speaker(), claims=_CLAIMS, source_context="ctx")
    assert "0.7" in p
