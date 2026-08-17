"""Per-game settings.

Everything here is a *default*, not a verified constant. Gen 3 hacks move
glyphs, repoint text and change font sheets, so the honest thing for a tool to
do is make these overridable and say so loudly. Verify against your target
before you build: pull the charmap from the decomp if you have one, and check
the font cell size against the actual sheet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .charmap import DEFAULT_FREE_SLOTS, DEFAULT_PLACEHOLDERS
from .fontgen import FontSpec


@dataclass
class Profile:
    key: str
    name: str
    game_code: str
    free_ranges: tuple[tuple[int, int], ...] = DEFAULT_FREE_SLOTS
    placeholders: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_PLACEHOLDERS))
    font: FontSpec = field(default_factory=FontSpec)
    decomp: str | None = None
    notes: str = ""


PROFILES: dict[str, Profile] = {
    "emerald": Profile(
        key="emerald",
        name="Pokemon Emerald (U)",
        game_code="BPEE",
        decomp="pokeemerald",
        notes="Best-supported decomp. Strongly the easiest target: edit the "
              "string sources and rebuild rather than patching bytes.",
    ),
    "firered": Profile(
        key="firered",
        name="Pokemon FireRed (U)",
        game_code="BPRE",
        decomp="pokefirered",
        notes="Placeholder IDs differ from Emerald's -- override placeholders "
              "from the decomp's charmap before encoding.",
    ),
    "ruby": Profile(
        key="ruby",
        name="Pokemon Ruby (U)",
        game_code="AXVE",
        decomp="pokeruby",
        notes="Same text engine as Emerald; fewer scripting conveniences.",
    ),
}


def get(key: str) -> Profile:
    try:
        return PROFILES[key]
    except KeyError:
        raise SystemExit(
            f"unknown profile {key!r}; available: {', '.join(sorted(PROFILES))}"
        ) from None


def parse_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Parse ``0x01-0x77,0x80-0x9F`` into range pairs."""
    out: list[tuple[int, int]] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        a = int(lo, 16)
        b = int(hi, 16) if hi else a
        if not (0 <= a <= b <= 0xFF):
            raise SystemExit(f"bad byte range {part!r}")
        out.append((a, b))
    return tuple(out)
