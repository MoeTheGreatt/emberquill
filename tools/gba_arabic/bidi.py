"""Visual reordering for a left-to-right engine.

The GBA engine draws bytes in the order it reads them, so right-to-left text
has to be stored pre-reversed ("visual order"). Latin words and numbers
embedded in Arabic still run left-to-right, so a plain string reversal is
wrong -- this is a cut-down Unicode Bidi Algorithm: classify, resolve
neutrals, reverse the line, then un-reverse each left-to-right run.

Only what a game script actually needs is implemented: one base direction per
line, no explicit embedding controls, no nesting beyond a single level. That
covers dialogue; it would not cover arbitrary prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import shaping

# Direction classes.
RTL = "R"
LTR = "L"
NUM = "EN"      # European digits: weak, render left-to-right
NEUTRAL = "N"


@dataclass
class Atom:
    """An opaque, unsplittable, zero-width run of raw bytes.

    Control codes (colour changes, name placeholders) must never be reversed
    internally, and must stay attached to the text they apply to.
    """

    name: str
    data: bytes = b""
    direction: str = NEUTRAL
    # A placeholder such as {PLAYER} expands to real text at runtime, so it
    # occupies width and participates in ordering. A formatting code does not.
    width: bool = False
    # Formatting atoms ride along with the token that follows them.
    binds_next: bool = True


@dataclass
class Char:
    ch: str
    direction: str = NEUTRAL
    prefix: list[Atom] = field(default_factory=list)


Token = Char | Atom


def classify(ch: str) -> str:
    cp = ord(ch)
    if shaping.is_arabic(ch):
        return NEUTRAL if cp in shaping.ARABIC_PUNCT else RTL
    if ch.isdigit():
        return NUM
    if ch.isalpha():
        return LTR
    if 0x0590 <= cp <= 0x05FF:      # Hebrew, in case a hack mixes scripts
        return RTL
    return NEUTRAL


def _strong(direction: str) -> str | None:
    if direction in (LTR, NUM):
        return LTR
    if direction == RTL:
        return RTL
    return None


def resolve(tokens: list[Token], base: str = RTL) -> list[str]:
    """Resolve every token to LTR or RTL.

    Neutrals between two runs of the same direction join that direction;
    otherwise they fall back to the base direction. That is what keeps
    "Lv 100" intact inside an Arabic sentence: the space sits between two
    left-to-right items and so becomes left-to-right itself.
    """
    n = len(tokens)
    dirs: list[str | None] = []
    for tok in tokens:
        d = tok.direction if isinstance(tok, Atom) else classify(tok.ch)
        # Zero-width formatting atoms are transparent to ordering.
        if isinstance(tok, Atom) and not tok.width and d == NEUTRAL:
            dirs.append(None)
        else:
            dirs.append(_strong(d))

    out: list[str] = []
    for i in range(n):
        if dirs[i] is not None:
            out.append(dirs[i])          # type: ignore[arg-type]
            continue
        before = next((dirs[j] for j in range(i - 1, -1, -1) if dirs[j]), None)
        after = next((dirs[j] for j in range(i + 1, n) if dirs[j]), None)
        out.append(before if before and before == after else base)
    return out


def _bind_atoms(tokens: list[Token]) -> list[Token]:
    """Fold zero-width formatting atoms onto the token they modify.

    Bound this way, a colour code stays with its word through the reversal
    instead of drifting to the far end of the line.
    """
    pending: list[Atom] = []
    out: list[Token] = []
    for tok in tokens:
        if isinstance(tok, Atom) and not tok.width and tok.binds_next:
            pending.append(tok)
            continue
        if isinstance(tok, Char) and pending:
            tok = Char(tok.ch, tok.direction, [*pending, *tok.prefix])
            pending = []
        elif pending:
            out.extend(pending)
            pending = []
        out.append(tok)
    if pending and out:
        # Nothing followed them; hang them off the last token instead.
        last = out[-1]
        if isinstance(last, Char):
            out[-1] = Char(last.ch, last.direction, [*last.prefix, *pending])
        else:
            out.extend(pending)
    elif pending:
        out.extend(pending)
    return out


def reorder(tokens: list[Token], base: str = RTL) -> list[Token]:
    """Return ``tokens`` in the visual order the engine should draw."""
    tokens = _bind_atoms(tokens)
    if not tokens:
        return []
    dirs = resolve(tokens, base)

    pairs = list(zip(tokens, dirs))
    if base == RTL:
        pairs.reverse()
        flip = LTR
    else:
        flip = RTL

    # Un-reverse each maximal run that opposes the base direction.
    out: list[Token] = []
    i = 0
    while i < len(pairs):
        if pairs[i][1] == flip:
            j = i
            while j < len(pairs) and pairs[j][1] == flip:
                j += 1
            out.extend(tok for tok, _ in reversed(pairs[i:j]))
            i = j
        else:
            out.append(pairs[i][0])
            i += 1
    return out


def reorder_string(text: str, base: str = RTL) -> str:
    """Convenience wrapper for plain strings (no atoms)."""
    toks: list[Token] = [Char(c) for c in text]
    return "".join(t.ch for t in reorder(toks, base) if isinstance(t, Char))


def shape_and_reorder(text: str, base: str = RTL) -> str:
    """Full text transform: logical Arabic in, visual glyph order out."""
    return reorder_string(shaping.shape(text), base)
