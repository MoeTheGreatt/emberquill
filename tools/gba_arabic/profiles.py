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


# --- FireRed, verified against the ROM and pokefirered ---------------------
#
# Everything below was checked rather than assumed, against BPRE rev 1
# (sha1 dd5945db9b930750cb39d00c84da8571feebf417, the base ROM pokefirered
# expects) and against the decomp itself:
#
#   * 0x01-0x77 is accented Latin and symbols in graphics/fonts/latin_normal.png
#     (À Á Â Ç È É ... œ ù ú û ñ, plus Lv, ♪, PK, PM and friends). Scanning every
#     string in the ROM found no English text using any of it, with two
#     exceptions below, so the region is reusable.
#   * 0x1B is 'é' -- the é in POKéMON, in 1234 strings. Confirmed three ways:
#     charmap.txt line 26, cell (1,11) of the font sheet, and the ROM's own text.
#   * 0x2D appears in three real strings ("R & D Room", "Skip ... Chomp!").
#     Cheap to leave alone, so it is excluded too.
#   * Bytes seen after 0xFC (0x02..0x09 and similar) are control-code arguments,
#     never font lookups, so they do not cost a glyph slot.
#   * 0x87-0x9F holds kana that only the Japanese font draws: the cells are
#     blank in every latin_*.png, no CHAR_* define touches the range (the
#     arrows sit at 0x79-0x7C and SUPER_E at 0x84, both excluded), and the
#     ROM-wide scan found no English text using it. 25 extra slots.
FIRERED_FREE = ((0x01, 0x1A), (0x1C, 0x2C), (0x2E, 0x77), (0x87, 0x9F))

FIRERED_PLACEHOLDERS = {
    "PLAYER": 0x01, "STR_VAR_1": 0x02, "STR_VAR_2": 0x03, "STR_VAR_3": 0x04,
    "KUN": 0x05, "RIVAL": 0x06,
}

PROFILES: dict[str, Profile] = {
    "emerald": Profile(
        key="emerald",
        name="Pokemon Emerald (U)",
        game_code="BPEE",
        decomp="pokeemerald",
        notes="Defaults are UNVERIFIED -- run `verify` against pokeemerald's "
              "charmap.txt before building. Edit string sources and rebuild "
              "rather than patching bytes.",
    ),
    "firered": Profile(
        key="firered",
        name="Pokemon FireRed (U/E) rev 1",
        game_code="BPRE",
        free_ranges=FIRERED_FREE,
        placeholders=FIRERED_PLACEHOLDERS,
        # 16x16 glyph cells, 16 per row, indexed directly by byte value --
        # measured from latin_normal.png (256x512). The engine draws only 14
        # rows of each cell (gGlyphInfo.height = 14 in src/text.c), so ink
        # below row 13 vanishes in game: size and baseline here are measured
        # so the full Arabic form set fits that box in DejaVu Sans (ascent 11
        # + descent 3 at 11pt), with the baseline on row 11 -- the same row
        # Latin sits on, so mixed lines align. The CLI re-derives both via
        # autofit for whatever face it is given (Noto Naskh, for example,
        # only fits the box at 9pt).
        font=FontSpec(cell_w=16, cell_h=16, columns=16, size=11, baseline=11),
        decomp="pokefirered",
        notes="Verified against BPRE rev 1 and pokefirered. 117 free glyph "
              "slots in 0x01-0x77 plus 0x87-0x9F with Latin kept. "
              "Note the font is near-monospace: most glyphs are 6px, so 'm' "
              "and 'w' are no wider than 'A'.",
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
