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


# ---------------------------------------------------------------------------
# Task 5: stream_speaker_reply
# ---------------------------------------------------------------------------
from unittest.mock import AsyncMock, MagicMock, patch


def _stream_chunks(*texts: str):
    async def gen():
        for t in texts:
            choice = MagicMock()
            choice.delta.content = t
            chunk = MagicMock()
            chunk.choices = [choice]
            yield chunk
    return gen()


async def test_stream_speaker_reply_yields_tokens():
    from app.services.speaker_chat import stream_speaker_reply
    with patch(
        "app.services.speaker_chat.litellm.acompletion",
        AsyncMock(return_value=_stream_chunks("As ", "I ", "argued")),
    ):
        out: list[str] = []
        async for tok in stream_speaker_reply(
            speaker=_speaker(), source_context="ctx", claims=_CLAIMS,
            history=[], user_message="what about SPACs?",
            seed_ts=None, seed_quote=None,
            model="openai/gpt-4o", api_key="k", base_url=None,
        ):
            out.append(tok)
        assert "".join(out) == "As I argued"


# ---------------------------------------------------------------------------
# Code-review findings: evidence_text in render + unattributed-claim hedge
# ---------------------------------------------------------------------------

def test_render_claims_includes_evidence_text():
    from app.services.speaker_chat import build_speaker_system_prompt

    claim_with_evidence = {
        "claim": "markets will correct",
        "topic": "macro",
        "source_title": "All-In Ep 5",
        "evidence_start_s": 120,
        "attribution_method": "explicit_name",
        "attribution_confidence": 0.9,
        "review_status": "accepted",
        "evidence_text": "we expect cuts by Q3",
    }
    claim_no_evidence = {
        "claim": "rates are sticky",
        "topic": "macro",
        "source_title": "All-In Ep 6",
        "evidence_start_s": 300,
        "attribution_method": "explicit_name",
        "attribution_confidence": 0.85,
        "review_status": "accepted",
        "evidence_text": None,
    }
    p = build_speaker_system_prompt(
        speaker=_speaker(),
        claims=[claim_with_evidence, claim_no_evidence],
        source_context="ctx",
    )
    # evidence text must appear in the prompt so the model can actually cite it
    assert "we expect cuts by Q3" in p
    # a claim with no evidence_text must NOT leave an empty evidence clause or
    # imply evidence is present (no trailing empty quotes / no bare "evidence:")
    no_ev_section = p[p.index("rates are sticky"):]
    assert '""' not in no_ev_section.split("\n")[0]
    assert "evidence: " not in no_ev_section.split("\n")[0].lower() or "we expect" not in no_ev_section.split("\n")[0]


def test_unattributed_claim_is_hedged():
    from app.services.speaker_chat import build_speaker_system_prompt

    unattributed = {
        "claim": "deficits will balloon",
        "topic": "fiscal",
        "source_title": "Pod X",
        "evidence_start_s": None,
        "attribution_method": None,
        "attribution_confidence": None,
        "review_status": "unreviewed",
        "evidence_text": None,
    }
    p = build_speaker_system_prompt(
        speaker=_speaker(), claims=[unattributed], source_context="ctx")

    # The rendered claim line must carry a marker that the prompt's hedge rule names,
    # so the model knows to speak tentatively about it.
    # We check that the rendered claim line contains "unattributed" or "llm_inferred"
    # (whichever marker the hedge sentence references).
    claim_line = next(
        line for line in p.splitlines() if "deficits will balloon" in line
    )
    hedge_markers = ("unattributed", "llm_inferred")
    assert any(m in claim_line for m in hedge_markers), (
        f"claim line must contain a hedge marker {hedge_markers!r}, got: {claim_line!r}"
    )
    # The prompt's hedge instruction must also name that same marker so the rule
    # is reachable for unattributed claims.
    marker_used = next(m for m in hedge_markers if m in claim_line)
    assert marker_used in p, (
        f"prompt hedge sentence must reference the marker '{marker_used}'"
    )


async def test_stream_speaker_reply_passes_system_prompt_and_history():
    from app.services.speaker_chat import build_speaker_system_prompt, stream_speaker_reply
    from app.models import ChatMessage
    from datetime import datetime

    captured: dict = {}

    async def fake_acompletion(**kw):
        captured.update(kw)
        return _stream_chunks("ok")

    hist = [ChatMessage(id=1, video_id="v1", role="user", content="hi",
                        created_at=datetime.now()),
            ChatMessage(id=2, video_id="v1", role="assistant", content="hello",
                        created_at=datetime.now())]
    with patch("app.services.speaker_chat.litellm.acompletion", side_effect=fake_acompletion):
        async for _ in stream_speaker_reply(
            speaker=_speaker(), source_context="ctx", claims=_CLAIMS,
            history=hist, user_message="now?", seed_ts=None, seed_quote=None,
            model="m", api_key="k", base_url=None,
        ):
            pass
    msgs = captured["messages"]
    # [system] + 2 history turns + new user message (build_messages ordering)
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == build_speaker_system_prompt(
        speaker=_speaker(), claims=_CLAIMS, source_context="ctx")
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[-1]["content"] == "now?"
    assert captured["stream"] is True


# ---------------------------------------------------------------------------
# Speculative-persona: speaker not linked to the per-episode source
# ---------------------------------------------------------------------------

def test_prompt_marks_speculative_when_not_present():
    """build_speaker_system_prompt with speaker_present=False must inject
    a speculative-framing clause into the prompt."""
    from app.services.speaker_chat import build_speaker_system_prompt
    p = build_speaker_system_prompt(
        speaker=_speaker(), claims=_CLAIMS, source_context="some transcript",
        speaker_present=False,
    )
    # The speculative clause must be present
    assert "did NOT actually appear" in p


def test_prompt_normal_when_present():
    """build_speaker_system_prompt with speaker_present=True (the default) must
    NOT inject the speculative clause — normal linked behaviour is unchanged."""
    from app.services.speaker_chat import build_speaker_system_prompt
    # Explicit True
    p_explicit = build_speaker_system_prompt(
        speaker=_speaker(), claims=_CLAIMS, source_context="some transcript",
        speaker_present=True,
    )
    assert "did NOT actually appear" not in p_explicit
    # Default (omitted param) must also be unchanged
    p_default = build_speaker_system_prompt(
        speaker=_speaker(), claims=_CLAIMS, source_context="some transcript",
    )
    assert "did NOT actually appear" not in p_default
