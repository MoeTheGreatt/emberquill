"""Arabic joining and contextual shaping.

The GBA text engine has no shaper: one byte selects one fixed glyph. So the
cursive form of every letter has to be decided here, at build time, and baked
into the byte stream. This module turns logical Arabic (the text a translator
types) into Arabic Presentation Forms-B codepoints, which are exactly "one
codepoint per drawable glyph" and therefore map 1:1 onto font slots.
"""

from __future__ import annotations

# Joining types, per Unicode ArabicShaping.txt.
U = "U"  # non-joining
R = "R"  # right-joining: accepts a join from the previous letter only
D = "D"  # dual-joining
C = "C"  # join-causing (tatweel, ZWJ)
T = "T"  # transparent (combining marks) - skipped when resolving neighbours

ISOLATED, FINAL, INITIAL, MEDIAL = "isolated", "final", "initial", "medial"

# Ordered exactly as the Arabic Presentation Forms-B block (U+FE80..U+FEF4):
# the block lays each letter's forms out consecutively -- 1 slot for hamza,
# 2 for right-joining letters (isolated, final) and 4 for dual-joining ones
# (isolated, final, initial, medial). Deriving the table from this list rather
# than transcribing 117 codepoints by hand means any slip shows up as the
# assertion at the bottom of this file failing, not as a wrong glyph in game.
_FORMS_B_ORDER: tuple[tuple[int, str], ...] = (
    (0x0621, U),                                          # hamza
    (0x0622, R), (0x0623, R), (0x0624, R), (0x0625, R),   # alef madda .. alef hamza below
    (0x0626, D), (0x0627, R), (0x0628, D), (0x0629, R),   # yeh hamza, alef, beh, teh marbuta
    (0x062A, D), (0x062B, D), (0x062C, D), (0x062D, D), (0x062E, D),
    (0x062F, R), (0x0630, R), (0x0631, R), (0x0632, R),   # dal, thal, reh, zain
    (0x0633, D), (0x0634, D), (0x0635, D), (0x0636, D),
    (0x0637, D), (0x0638, D), (0x0639, D), (0x063A, D),
    (0x0641, D), (0x0642, D), (0x0643, D), (0x0644, D),
    (0x0645, D), (0x0646, D), (0x0647, D),
    (0x0648, R),                                          # waw
    (0x0649, R),                                          # alef maksura -- see note below
    (0x064A, D),                                          # yeh
)

# Note on U+0649 ALEF MAKSURA: ArabicShaping.txt classes it D, but Forms-B only
# provides isolated and final slots for it (its initial/medial forms live in the
# Uighur/Kazakh range at U+FBE8/U+FBE9). For Arabic text, final/isolated is the
# correct behaviour anyway, so it is treated as R here.

_SLOT_COUNT = {U: 1, R: 2, D: 4}
_SLOT_ORDER = (ISOLATED, FINAL, INITIAL, MEDIAL)

FORMS_B_START = 0xFE80

# Combining-mark ranges that are transparent to joining.
_TRANSPARENT_RANGES = (
    (0x0610, 0x061A), (0x064B, 0x065F), (0x0670, 0x0670),
    (0x06D6, 0x06DC), (0x06DF, 0x06E4), (0x06E7, 0x06E8),
    (0x06EA, 0x06ED), (0x08D3, 0x08FF), (0xFE00, 0xFE0F),
)


def _build_form_table() -> tuple[dict[int, dict[str, int]], dict[int, str]]:
    forms: dict[int, dict[str, int]] = {}
    jt: dict[int, str] = {}
    slot = FORMS_B_START
    for cp, join in _FORMS_B_ORDER:
        n = _SLOT_COUNT[join]
        forms[cp] = {name: slot + i for i, name in enumerate(_SLOT_ORDER[:n])}
        jt[cp] = join
        slot += n
    return forms, slot


FORMS, _NEXT_SLOT = _build_form_table()

# If this fails, _FORMS_B_ORDER no longer matches the Unicode block.
assert _NEXT_SLOT == 0xFEF5, f"Forms-B table ends at {_NEXT_SLOT:#06x}, expected 0xFEF5"

JOINING: dict[int, str] = {cp: join for cp, join in _FORMS_B_ORDER}
JOINING[0x0640] = C   # tatweel
JOINING[0x200D] = C   # ZWJ
JOINING[0x200C] = U   # ZWNJ

# Mandatory lam-alef ligatures. A bare lam followed by any alef variant must be
# drawn as a single glyph; leaving them separate is the classic tell of a broken
# Arabic hack. Keyed by the alef that follows the lam.
LAM = 0x0644
LAM_ALEF: dict[int, tuple[int, int]] = {
    0x0622: (0xFEF5, 0xFEF6),   # lam + alef madda      (isolated, final)
    0x0623: (0xFEF7, 0xFEF8),   # lam + alef hamza above
    0x0625: (0xFEF9, 0xFEFA),   # lam + alef hamza below
    0x0627: (0xFEFB, 0xFEFC),   # lam + alef
}

# Arabic-script punctuation that is not a letter but must survive shaping.
ARABIC_PUNCT = {0x060C, 0x061B, 0x061F, 0x066A, 0x066B, 0x066C, 0x066D, 0x06D4}


def joining_type(ch: str) -> str:
    """Joining type of a single character."""
    cp = ord(ch)
    if cp in JOINING:
        return JOINING[cp]
    for lo, hi in _TRANSPARENT_RANGES:
        if lo <= cp <= hi:
            return T
    return U


def is_arabic(ch: str) -> bool:
    cp = ord(ch)
    if 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F:
        return True
    return 0xFB50 <= cp <= 0xFDFF or 0xFE70 <= cp <= 0xFEFF


def is_mark(ch: str) -> bool:
    return joining_type(ch) == T


def strip_marks(text: str) -> str:
    """Drop harakat and other combining marks.

    A one-byte-per-glyph engine cannot stack a mark over a base letter, and
    undiacritised text is the norm for Arabic game translations anyway.
    """
    return "".join(c for c in text if not is_mark(c))


def _pick_form(joins_prev: bool, joins_next: bool) -> str:
    if joins_prev and joins_next:
        return MEDIAL
    if joins_prev:
        return FINAL
    if joins_next:
        return INITIAL
    return ISOLATED


def shape(
    text: str,
    *,
    drop_marks: bool = True,
    prev_type: str | None = None,
    next_type: str | None = None,
) -> str:
    """Shape logical Arabic into Presentation Forms-B.

    Non-Arabic characters pass through untouched, so mixed Arabic/Latin strings
    are safe to hand in whole.

    ``prev_type``/``next_type`` supply the joining type of context that sits
    outside this string. The pipeline uses them so that a zero-width control
    code embedded mid-word (a colour change, say) does not break the cursive
    join across it.
    """
    if drop_marks:
        text = strip_marks(text)
    src = list(text)
    types = [joining_type(c) for c in src]
    n = len(src)

    def prev_visible(i: int) -> str | None:
        for j in range(i - 1, -1, -1):
            if types[j] != T:
                return types[j]
        return prev_type

    def next_visible(i: int) -> str | None:
        for j in range(i + 1, n):
            if types[j] != T:
                return types[j]
        return next_type

    out: list[str] = []
    i = 0
    while i < n:
        ch, cp, jt = src[i], ord(src[i]), types[i]

        if jt == T:                     # a mark survived drop_marks=False
            out.append(ch)
            i += 1
            continue

        if cp not in FORMS:             # not a shapeable Arabic letter
            out.append(ch)
            i += 1
            continue

        joins_prev = jt in (D, R) and prev_visible(i) in (D, C)

        # Lam-alef ligature takes priority over ordinary lam shaping.
        if cp == LAM:
            k = i + 1
            while k < n and types[k] == T:
                k += 1
            if k < n and ord(src[k]) in LAM_ALEF:
                iso, fin = LAM_ALEF[ord(src[k])]
                out.append(chr(fin if joins_prev else iso))
                i = k + 1
                continue

        joins_next = jt in (D, C) and next_visible(i) in (D, C, R)
        form = _pick_form(joins_prev, joins_next)
        out.append(chr(FORMS[cp][form]))
        i += 1

    return "".join(out)


# Reverse lookup: shaped glyph -> the form it is. Ligatures are FINAL when the
# slot is the even (joining) variant, ISOLATED otherwise.
_FORM_OF: dict[int, str] = {}
for _cp, _table in FORMS.items():
    for _name, _slot in _table.items():
        _FORM_OF[_slot] = _name
for _iso, _fin in LAM_ALEF.values():
    _FORM_OF[_iso] = ISOLATED
    _FORM_OF[_fin] = FINAL
del _cp, _table, _name, _slot, _iso, _fin


def form_of(cp: int) -> str | None:
    """Form of a Presentation Forms-B glyph, or None for anything else."""
    return _FORM_OF.get(cp)


def describe(text: str) -> list[tuple[str, str, str]]:
    """(source char, shaped glyph, form name) triples -- for CLI inspection."""
    shaped = shape(text)
    rows: list[tuple[str, str, str]] = []
    for g in shaped:
        cp = ord(g)
        if 0xFEF5 <= cp <= 0xFEFC:
            base, form = "لا", FINAL if cp % 2 == 0 else ISOLATED
        else:
            base, form = g, "-"
            for src_cp, table in FORMS.items():
                for name, slot in table.items():
                    if slot == cp:
                        base, form = chr(src_cp), name
        rows.append((base, g, form))
    return rows
