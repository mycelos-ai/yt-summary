"""Sanity-check the curated avatar library: every entry maps to a
real PNG file, the validation gate accepts known ids and rejects
arbitrary strings, and the grouped() helper covers every entry."""

from pathlib import Path

from app.services import avatars

AVATARS_DIR = Path("app/static/avatars")


def test_every_avatar_id_has_a_png_on_disk():
    """If we add an entry to AVATARS but forget to drop the PNG,
    the picker would render a broken image. This test catches that
    at boot."""
    missing = [a.id for a in avatars.AVATARS
               if not (AVATARS_DIR / f"{a.id}.png").exists()]
    assert not missing, f"avatar ids without PNG: {missing}"


def test_avatar_ids_are_unique():
    ids = [a.id for a in avatars.AVATARS]
    assert len(ids) == len(set(ids)), "duplicate avatar ids"


def test_is_valid_id_accepts_known():
    assert avatars.is_valid_id("adult-techreviewer-m")
    assert avatars.is_valid_id("kid-dino-girl")


def test_is_valid_id_rejects_unknown_and_traversal():
    """Validation is the gate that prevents `users.avatar_image`
    from holding arbitrary strings — including path-traversal
    attempts. The route layer relies on this; if it ever returned
    True for a non-curated id, a profile could point at any file."""
    assert not avatars.is_valid_id("")
    assert not avatars.is_valid_id("../etc/passwd")
    assert not avatars.is_valid_id("adult-techreviewer-m.png")  # extension
    assert not avatars.is_valid_id("nope")


def test_grouped_includes_every_avatar_exactly_once():
    grouped = avatars.grouped()
    flat: list[str] = []
    for items in grouped.values():
        flat.extend(a.id for a in items)
    assert sorted(flat) == sorted(a.id for a in avatars.AVATARS)


def test_grouped_keeps_declaration_order_within_group():
    """The picker template iterates items per group; the order
    should match how the Avatar list is declared so `Scientist`
    sits before `Tech reviewer`, not in alphabetic order."""
    grouped = avatars.grouped()
    adult_ids = [a.id for a in grouped.get("adult", [])]
    declared_adult_ids = [a.id for a in avatars.AVATARS if a.group == "adult"]
    assert adult_ids == declared_adult_ids


def test_every_avatar_has_a_pastel_bg_color():
    """Per-avatar pastel hex feeds the CSS --avatar-bg variable.
    A missing or malformed value would render an unstyled circle."""
    import re
    hex_re = re.compile(r"^#[0-9a-fA-F]{6}$")
    bad = [a.id for a in avatars.AVATARS if not hex_re.match(a.bg_color)]
    assert not bad, f"avatars with non-hex bg_color: {bad}"


def test_bg_color_for_returns_library_value():
    av = avatars.AVATARS[0]
    assert avatars.bg_color_for(av.id) == av.bg_color


def test_bg_color_for_unknown_returns_default():
    """Defensive: an unknown id falls back to the default pastel
    rather than raising or returning empty (which would render an
    invalid CSS value)."""
    out = avatars.bg_color_for("does-not-exist")
    assert out == avatars.DEFAULT_BG
    assert out.startswith("#")
