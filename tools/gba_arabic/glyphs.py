"""Glyph slot allocation.

There are only 256 byte values and roughly 20 of them are spoken for by control
codes, so glyph slots are the scarcest resource in this project. Fully shaped
Arabic wants around 130 of them (22 dual-joining letters x 4 forms, 12
right-joining x 2, hamza forms, the four lam-alef ligatures, Arabic
punctuation).

The allocator is therefore corpus-driven: it only spends a slot on a
presentation form the translated script actually contains, and it spends the
low slots on the most frequent forms so a hand-pixelled font can be worked on
in priority order. Allocation is deterministic -- same script in, same bytes
out -- so builds stay reproducible and diffable.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import charmap as cm
from . import shaping

# Latin glyphs worth keeping even in an Arabic build: Pokemon names, trainer
# names and version strings routinely stay Latin.
LATIN_KEEP = frozenset(
    ord(c) for c in
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,!?-:/'"
)


@dataclass
class Allocation:
    """Result of assigning byte values to glyphs."""

    glyph_to_byte: dict[int, int] = field(default_factory=dict)
    frequency: Counter = field(default_factory=Counter)
    reused: dict[int, int] = field(default_factory=dict)      # glyph -> existing byte
    overflow: list[int] = field(default_factory=list)         # glyphs with no slot
    freed: list[int] = field(default_factory=list)            # bytes reclaimed from Latin
    slots_total: int = 0

    @property
    def slots_used(self) -> int:
        return len(self.glyph_to_byte)

    @property
    def ok(self) -> bool:
        return not self.overflow

    def byte_for(self, glyph: int) -> int | None:
        return self.glyph_to_byte.get(glyph, self.reused.get(glyph))

    def to_json(self) -> str:
        return json.dumps(
            {
                "glyphs": [
                    {
                        "codepoint": f"U+{g:04X}",
                        "char": chr(g),
                        "byte": f"0x{b:02X}",
                        "count": self.frequency[g],
                    }
                    for g, b in sorted(self.glyph_to_byte.items(), key=lambda kv: kv[1])
                ],
                "reused": {f"U+{g:04X}": f"0x{b:02X}" for g, b in sorted(self.reused.items())},
                "overflow": [f"U+{g:04X}" for g in self.overflow],
                "freed": [f"0x{b:02X}" for b in self.freed],
                "slots_used": self.slots_used,
                "slots_total": self.slots_total,
            },
            ensure_ascii=False,
            indent=2,
        )


def collect(texts: Iterable[str], *, drop_marks: bool = True) -> Counter:
    """Count the glyphs a translated corpus needs, after shaping.

    Script markup is stripped first so ``{PLAYER}`` and ``\\n`` are not counted
    as text.
    """
    counts: Counter = Counter()
    table = cm.Charmap()
    for text in texts:
        for line in table.tokenise(text):
            run = "".join(t for t in line.tokens if isinstance(t, str))
            for ch in shaping.shape(run, drop_marks=drop_marks):
                counts[ord(ch)] += 1
    return counts


def allocate(
    texts: Iterable[str],
    *,
    table: cm.Charmap | None = None,
    free_ranges: tuple[tuple[int, int], ...] = cm.DEFAULT_FREE_SLOTS,
    keep_latin: bool = True,
    equivalent: Callable[[int, int], bool] | None = None,
    drop_marks: bool = True,
) -> Allocation:
    """Assign byte values to every glyph the corpus needs.

    ``keep_latin`` preserves the Latin alphabet and digits; turn it off to
    reclaim ~62 extra slots once you are certain no Latin text survives.

    ``equivalent(a, b)`` lets two glyphs share one byte when they are visually
    identical at the font's size -- at 8-10 pixels tall, several isolated and
    final forms genuinely are. Pass ``fontgen.bitmap_equivalence(...)`` to
    decide this from rendered pixels rather than by guesswork.
    """
    table = table or cm.Charmap()
    counts = collect(texts, drop_marks=drop_marks)

    alloc = Allocation(frequency=counts)

    # Characters the ROM can already draw cost nothing.
    pending: list[int] = []
    for glyph in counts:
        existing = table.to_byte.get(chr(glyph))
        if existing is not None:
            alloc.reused[glyph] = existing
        else:
            pending.append(glyph)

    keep = LATIN_KEEP if keep_latin else frozenset()
    kept_bytes = frozenset(
        b for b, ch in table.to_char.items() if ch and ord(ch) in keep
    ) if keep_latin else frozenset()

    slots = table.free_slots(free_ranges, keep=kept_bytes)

    if not keep_latin:
        # Latin byte values become available; record what we took.
        extra = sorted(
            b for b, ch in table.to_char.items()
            if b not in cm.CONTROL_BYTES and b != cm.SPACE and b not in slots
        )
        alloc.freed = extra
        slots = sorted(set(slots) | set(extra))

    alloc.slots_total = len(slots)

    # Most frequent first, codepoint as tiebreaker for determinism.
    pending.sort(key=lambda g: (-counts[g], g))

    cursor = 0
    for glyph in pending:
        if equivalent is not None:
            twin = next(
                (g for g in alloc.glyph_to_byte if equivalent(g, glyph)), None
            )
            if twin is not None:
                alloc.reused[glyph] = alloc.glyph_to_byte[twin]
                continue
        if cursor >= len(slots):
            alloc.overflow.append(glyph)
            continue
        alloc.glyph_to_byte[glyph] = slots[cursor]
        cursor += 1

    return alloc


def report(alloc: Allocation) -> str:
    """Human-readable budget summary."""
    lines = [
        f"glyphs needed : {alloc.slots_used + len(alloc.reused) + len(alloc.overflow)}",
        f"already in ROM: {len(alloc.reused)}",
        f"newly assigned: {alloc.slots_used} of {alloc.slots_total} free slots",
    ]
    if alloc.freed:
        lines.append(f"reclaimed     : {len(alloc.freed)} byte(s) from Latin glyphs")
    if alloc.overflow:
        lines.append("")
        lines.append(f"OVERFLOW: {len(alloc.overflow)} glyph(s) have no slot:")
        lines.append("  " + " ".join(chr(g) for g in alloc.overflow))
        lines.append("")
        lines.append("  Options, cheapest first:")
        lines.append("   - pass --no-keep-latin if the script has no Latin text left")
        lines.append("   - widen --free-ranges once you have verified spare bytes in the ROM")
        lines.append("   - pass --dedupe to share bytes between forms that render identically")
        lines.append("   - drop rare forms by rewording those strings")
    else:
        lines.append("")
        lines.append(f"OK - {alloc.slots_total - alloc.slots_used} slot(s) spare")

    if alloc.glyph_to_byte:
        lines.append("")
        lines.append("Assignments (most frequent first):")
        ordered = sorted(alloc.glyph_to_byte.items(), key=lambda kv: (-alloc.frequency[kv[0]], kv[0]))
        for glyph, byte in ordered:
            forms = shaping.describe(chr(glyph))
            label = f"{forms[0][0]} {forms[0][2]}" if forms else ""
            lines.append(
                f"  0x{byte:02X}  U+{glyph:04X}  {chr(glyph)}  "
                f"{alloc.frequency[glyph]:>6}x  {label}"
            )
    return "\n".join(lines)
