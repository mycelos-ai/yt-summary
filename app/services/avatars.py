"""Curated avatar library.

Each entry maps a stable id (used as the filename under
`app/static/avatars/`) to a human-readable label, a logical group
for the picker grid, and a pastel background color. The PNGs are
transparent line-art; the pastel circle behind each character is
drawn by CSS using the per-avatar `bg_color`. This means the
visual identity of an avatar is the line-art plus its color — and
both are owned by us, no per-render Photoshop step.

Adding a new avatar: one Avatar() entry plus a transparent line-art
PNG drop-in. To change the palette: edit the hex values here. To
let a user pick a custom color per profile (Phase 3): expose
`bg_color` as a settable form field and pipe it through to the
template's inline `style="--avatar-bg: ..."`.

Color palette: the values were sampled from the original generated
quartets, so each avatar keeps the pastel it was conceived with.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Avatar:
    id: str
    label: str
    group: str            # 'adult' or 'kid'
    bg_color: str         # pastel hex for the CSS circle behind the line-art


# Pastel palette used across the library. Centralised so theme tweaks
# stay coherent — change `_BLUE` once and every Scientist re-skins.
# These are tuned to be ~25% more saturated than soft pastel so they
# stay visible on big screens (5K monitors absorb very-pale tints
# into the surrounding white) without becoming aggressive.
_BLUE   = "#c5dff2"  # cool, scientific
_GREY   = "#dcdce0"  # neutral, urban (tech reviewer)
_PEACH  = "#fdd9a8"  # warm, outdoorsy (athlete / cars)
_SAGE   = "#d3e3c0"  # earthy (explorer / researcher)
_LILAC  = "#d4cae8"  # creative, gaming
_CREAM  = "#fde0b3"  # warm yellow (maker / dino)


AVATARS: list[Avatar] = [
    # Adults — 8 themes × 2 (m/f) = 16
    Avatar("adult-scientist-m",     "Scientist",      "adult", _BLUE),
    Avatar("adult-scientist-f",     "Scientist",      "adult", _BLUE),
    Avatar("adult-techreviewer-m",  "Tech reviewer",  "adult", _GREY),
    Avatar("adult-techreviewer-f",  "Tech reviewer",  "adult", _GREY),
    Avatar("adult-researcher-m",    "Researcher",     "adult", _CREAM),
    Avatar("adult-researcher-f",    "Researcher",     "adult", _CREAM),
    Avatar("adult-maker-m",         "Maker",          "adult", _SAGE),
    Avatar("adult-maker-f",         "Maker",          "adult", _SAGE),
    Avatar("adult-gamer-m",         "Gamer",          "adult", _LILAC),
    Avatar("adult-gamer-f",         "Gamer",          "adult", _LILAC),
    Avatar("adult-athlete-m",       "Athlete",        "adult", _BLUE),
    Avatar("adult-athlete-f",       "Athlete",        "adult", _BLUE),
    Avatar("adult-explorer-m",      "Explorer",       "adult", _SAGE),
    Avatar("adult-explorer-f",      "Explorer",       "adult", _SAGE),
    Avatar("adult-cars-m",          "Car enthusiast", "adult", _PEACH),
    Avatar("adult-cars-f",          "Car enthusiast", "adult", _PEACH),
    # Kids — 4 themes × 2 (boy/girl) = 8
    Avatar("kid-gamer-boy",         "Young gamer",    "kid",   _LILAC),
    Avatar("kid-gamer-girl",        "Young gamer",    "kid",   _LILAC),
    Avatar("kid-dino-boy",          "Dino fan",       "kid",   _SAGE),
    Avatar("kid-dino-girl",         "Dino fan",       "kid",   _SAGE),
    Avatar("kid-soccer-boy",        "Soccer kid",     "kid",   _BLUE),
    Avatar("kid-soccer-girl",       "Soccer kid",     "kid",   _BLUE),
    Avatar("kid-explorer-boy",      "Young explorer", "kid",   _CREAM),
    Avatar("kid-explorer-girl",     "Young explorer", "kid",   _CREAM),
]


_BY_ID: dict[str, Avatar] = {a.id: a for a in AVATARS}


def is_valid_id(avatar_id: str) -> bool:
    """Used by the profile-update route to reject arbitrary strings —
    only ids in the curated library are accepted, so the avatar_image
    column never holds something like '../etc/passwd'."""
    return avatar_id in _BY_ID


def get(avatar_id: str) -> Avatar | None:
    return _BY_ID.get(avatar_id)


# Default fallback bg used when an avatar id is not in the library
# (which shouldn't happen because is_valid_id() gates input — but
# defensive). Matches the CSS default in app.css.
DEFAULT_BG = "#e7f1fa"


def bg_color_for(avatar_id: str) -> str:
    """Return the CSS background hex for the given avatar id.

    Resilient: unknown id → DEFAULT_BG. Templates can use this from
    a Jinja filter to set `style="--avatar-bg: ..."` on the avatar
    element without dropping into Python.
    """
    av = _BY_ID.get(avatar_id)
    return av.bg_color if av else DEFAULT_BG


def grouped() -> dict[str, list[Avatar]]:
    """Return avatars partitioned by group, in declaration order.

    The picker template iterates this dict to render a section per
    group ('Adults' / 'Kids') instead of one flat grid.
    """
    out: dict[str, list[Avatar]] = {}
    for av in AVATARS:
        out.setdefault(av.group, []).append(av)
    return out
