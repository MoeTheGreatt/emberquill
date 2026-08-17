"""End-to-end encode: logical Arabic script source -> Gen 3 byte stream.

Order of operations matters and is easy to get wrong. Shaping must happen
before reordering (a letter's cursive form depends on its logical neighbours,
not its drawn position), and reordering must happen before encoding (the
charmap maps drawable glyphs, and only shaped text has those). Lines are
handled independently, because each line is reordered inside its own box.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import bidi
from . import charmap as cm
from . import shaping
from .glyphs import Allocation


@dataclass
class Segment:
    """A run of text, or a single atom, within one line."""

    text: str | None = None
    atom: cm.Atom | None = None


def _segment(tokens: list[str | cm.Atom]) -> list[Segment]:
    segs: list[Segment] = []
    buf: list[str] = []
    for tok in tokens:
        if isinstance(tok, str):
            buf.append(tok)
        else:
            if buf:
                segs.append(Segment(text="".join(buf)))
                buf = []
            segs.append(Segment(atom=tok))
    if buf:
        segs.append(Segment(text="".join(buf)))
    return segs


def _context(segs: list[Segment], index: int, forward: bool) -> str | None:
    """Joining type of the nearest shaping-relevant neighbour of ``segs[index]``.

    Zero-width control codes are transparent, so a colour change dropped into
    the middle of a word must not sever the cursive join. A placeholder does
    interrupt it, because real text will be substituted there at runtime.
    """
    step = 1 if forward else -1
    i = index + step
    while 0 <= i < len(segs):
        seg = segs[i]
        if seg.atom is not None:
            if seg.atom.width:
                return shaping.U
            i += step
            continue
        if seg.text:
            return shaping.joining_type(seg.text[0] if forward else seg.text[-1])
        i += step
    return None


def shape_line(tokens: list[str | cm.Atom], *, drop_marks: bool = True) -> list[bidi.Token]:
    """Shape every text run in a line, keeping atoms in place."""
    segs = _segment(tokens)
    out: list[bidi.Token] = []
    for i, seg in enumerate(segs):
        if seg.atom is not None:
            out.append(
                bidi.Atom(
                    seg.atom.name,
                    seg.atom.data,
                    direction=bidi.RTL if seg.atom.width else bidi.NEUTRAL,
                    width=seg.atom.width,
                )
            )
            continue
        shaped = shaping.shape(
            seg.text or "",
            drop_marks=drop_marks,
            prev_type=_context(segs, i, forward=False),
            next_type=_context(segs, i, forward=True),
        )
        out.extend(bidi.Char(c) for c in shaped)
    return out


def encode_tokens(
    tokens: list[bidi.Token],
    table: cm.Charmap,
    alloc: Allocation | None = None,
) -> bytes:
    """Turn visually-ordered tokens into bytes."""
    out = bytearray()
    for tok in tokens:
        if isinstance(tok, bidi.Atom):
            out += tok.data
            continue
        for pre in tok.prefix:
            out += pre.data
        cp = ord(tok.ch)
        byte = alloc.byte_for(cp) if alloc else None
        if byte is None:
            byte = table.to_byte.get(tok.ch)
        if byte is None:
            raise cm.EncodeError(
                f"no byte for {tok.ch!r} (U+{cp:04X}); it was not in the charmap "
                f"and no glyph slot was allocated for it"
            )
        out.append(byte)
    return bytes(out)


def measure(data: bytes, widths: dict[int, int], default: int = 6) -> int:
    """Pixel width of an encoded line, skipping control sequences."""
    total = 0
    i = 0
    while i < len(data):
        b = data[i]
        if b == cm.SPECIAL and i + 1 < len(data):
            # Variable length -- 0xFC 0x04 spans five bytes, not two.
            i += 1 + cm.ext_ctrl_code_length(data[i + 1])
            continue
        if b == cm.PLACEHOLDER:
            i += 2                      # ID byte is not drawn
            continue
        if b in cm.CONTROL_BYTES:
            i += 1
            continue
        total += widths.get(b, default)
        i += 1
    return total


def encode(
    text: str,
    table: cm.Charmap,
    alloc: Allocation | None = None,
    *,
    base: str = bidi.RTL,
    drop_marks: bool = True,
    terminate: bool = True,
    align_right: int | None = None,
    widths: dict[int, int] | None = None,
) -> bytes:
    """Encode one script entry.

    ``align_right`` pads each line with leading spaces to right-align it in a
    box of that pixel width. The engine draws from the left edge, so without
    padding (or an engine-side alignment change) Arabic text sits flush left,
    which reads as broken. Needs ``widths``: byte -> pixel width.
    """
    out = bytearray()
    for line in table.tokenise(text):
        visual = bidi.reorder(shape_line(line.tokens, drop_marks=drop_marks), base)
        data = encode_tokens(visual, table, alloc)

        if align_right is not None and widths:
            space = widths.get(cm.SPACE, 3) or 3
            pad = max(0, (align_right - measure(data, widths)) // space)
            data = bytes([cm.SPACE]) * pad + data

        out += data
        if line.separator is not None:
            out.append(line.separator)
    if terminate:
        out.append(cm.EOS)
    return bytes(out)
