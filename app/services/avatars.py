"""Curated avatar library.

Each entry maps a stable id (used as the filename under
`app/static/avatars/`) to a human-readable label and a logical group
for the picker grid. Adding a new avatar is a one-liner here plus a
PNG drop-in — no DB migration needed.

Stylistic note: the line art was generated locally and is the
property of the project. The PNGs ship with their pastel circle
backgrounds intact for now; a future iteration may switch to
transparent line art so the background becomes a CSS variable
(`--avatar-bg`) and users can recolor per-profile. The CSS already
declares the variable so that change lands as a pure asset swap.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Avatar:
    id: str
    label: str
    group: str  # 'adult' or 'kid'


AVATARS: list[Avatar] = [
    # Adults — 8 themes × 2 (m/f) = 16
    Avatar("adult-scientist-m",     "Scientist",     "adult"),
    Avatar("adult-scientist-f",     "Scientist",     "adult"),
    Avatar("adult-techreviewer-m",  "Tech reviewer", "adult"),
    Avatar("adult-techreviewer-f",  "Tech reviewer", "adult"),
    Avatar("adult-researcher-m",    "Researcher",    "adult"),
    Avatar("adult-researcher-f",    "Researcher",    "adult"),
    Avatar("adult-maker-m",         "Maker",         "adult"),
    Avatar("adult-maker-f",         "Maker",         "adult"),
    Avatar("adult-gamer-m",         "Gamer",         "adult"),
    Avatar("adult-gamer-f",         "Gamer",         "adult"),
    Avatar("adult-athlete-m",       "Athlete",       "adult"),
    Avatar("adult-athlete-f",       "Athlete",       "adult"),
    Avatar("adult-explorer-m",      "Explorer",      "adult"),
    Avatar("adult-explorer-f",      "Explorer",      "adult"),
    Avatar("adult-cars-m",          "Car enthusiast","adult"),
    Avatar("adult-cars-f",          "Car enthusiast","adult"),
    # Kids — 4 themes × 2 (boy/girl) = 8
    Avatar("kid-gamer-boy",         "Young gamer",   "kid"),
    Avatar("kid-gamer-girl",        "Young gamer",   "kid"),
    Avatar("kid-dino-boy",          "Dino fan",      "kid"),
    Avatar("kid-dino-girl",         "Dino fan",      "kid"),
    Avatar("kid-soccer-boy",        "Soccer kid",    "kid"),
    Avatar("kid-soccer-girl",       "Soccer kid",    "kid"),
    Avatar("kid-explorer-boy",      "Young explorer","kid"),
    Avatar("kid-explorer-girl",     "Young explorer","kid"),
]


_BY_ID: dict[str, Avatar] = {a.id: a for a in AVATARS}


def is_valid_id(avatar_id: str) -> bool:
    """Used by the profile-update route to reject arbitrary strings —
    only ids in the curated library are accepted, so the avatar_image
    column never holds something like '../etc/passwd'."""
    return avatar_id in _BY_ID


def get(avatar_id: str) -> Avatar | None:
    return _BY_ID.get(avatar_id)


def grouped() -> dict[str, list[Avatar]]:
    """Return avatars partitioned by group, in declaration order.

    The picker template iterates this dict to render a section per
    group ('Adults' / 'Kids') instead of one flat grid.
    """
    out: dict[str, list[Avatar]] = {}
    for av in AVATARS:
        out.setdefault(av.group, []).append(av)
    return out
