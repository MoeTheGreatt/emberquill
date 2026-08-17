"""Injecting Arabic into a Gen 3 decomp's string sources.

The decomp build pipes every string literal through ``preproc`` and
``charmap.txt``: each character in a ``_("...")`` or ``.string "..."`` literal
becomes the byte the charmap assigns it. There is no way to write raw bytes in
those literals -- but there does not need to be. Every byte we allocate for an
Arabic glyph already has a charmap character (the accented Latin or kana that
used to live there), so a shaped, reordered Arabic string can be expressed as
the sequence of charmap characters whose bytes are ours.

The C sources end up reading like accented gibberish (``ÀÙÎ...``), which is
fine: the *font sheet* at those cells is Arabic, and the bytes are what matter.
``{PLAYER}``-style macros and ``\\n \\l \\p`` escapes are left for preproc to
handle exactly as it does for English.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import bidi
from . import charmap as cm
from . import pipeline
from .glyphs import Allocation

# Macros that expand to visible text at runtime (player/rival/buffer names).
# They occupy width, participate in reordering, and break cursive joins.
# Everything else -- colour changes, pauses, font switches -- is zero-width.
_ZERO_WIDTH_PREFIXES = (
    "COLOR", "HIGHLIGHT", "SHADOW", "PALETTE", "FONT", "RESET_FONT",
    "PAUSE", "WAIT_SE", "PLAY_BGM", "PLAY_SE", "ESCAPE", "SHIFT_RIGHT",
    "SHIFT_DOWN", "FILL_WINDOW", "CLEAR", "SKIP", "MIN_LETTER_SPACING",
    "JPN", "ENG", "PAUSE_MUSIC", "RESUME_MUSIC", "EMOJI_",
)

_TOKEN_RE = re.compile(r"\{([^}]*)\}|\\([nlp])")


def _is_zero_width(macro: str) -> bool:
    return any(macro.startswith(p) for p in _ZERO_WIDTH_PREFIXES)


def load_byte_chars(charmap_path: str | Path) -> dict[int, str]:
    """byte -> a charmap character that compiles to it.

    A Gen 3 charmap gives many bytes several characters (international and
    Japanese sections). Any of them works -- preproc maps character to byte --
    but plain ASCII is preferred where it exists so the patched sources stay
    as readable as they can be.
    """
    candidates: dict[int, list[str]] = {}
    pat = re.compile(r"^\s*'(.)'\s*=\s*([0-9A-Fa-f]{2})\s*$")
    for raw in Path(charmap_path).read_text(encoding="utf-8").splitlines():
        m = pat.match(raw.split("@")[0])
        if m:
            candidates.setdefault(int(m.group(2), 16), []).append(m.group(1))
    out: dict[int, str] = {}
    for byte, chars in candidates.items():
        ascii_ones = [c for c in chars if ord(c) < 128]
        out[byte] = ascii_ones[0] if ascii_ones else chars[0]
    out.setdefault(0x00, " ")
    return out


def tokenise(text: str) -> list[cm.Line]:
    """Split decomp-flavoured markup into lines of chars and macro atoms.

    Unlike ``Charmap.tokenise`` this passes EVERY ``{MACRO}`` through by name
    -- the decomp's preproc knows hundreds of them and it, not this tool, is
    the authority on their byte encodings.
    """
    lines = [cm.Line()]
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.search(text, pos)
        if not m:
            lines[-1].tokens.extend(text[pos:])
            break
        lines[-1].tokens.extend(text[pos:m.start()])
        pos = m.end()
        if m.group(2):
            lines[-1].separator = {"n": cm.NEWLINE, "l": cm.SCROLL,
                                   "p": cm.PARAGRAPH}[m.group(2)]
            lines.append(cm.Line())
        else:
            name = m.group(1).strip()
            lines[-1].tokens.append(
                cm.Atom(name=name, data=b"", width=not _is_zero_width(name))
            )
    return lines


def convert(
    text: str,
    table: cm.Charmap,
    alloc: Allocation,
    byte_chars: dict[int, str],
) -> str:
    """Arabic with decomp markup -> charmap-character string for the sources.

    Shape, reorder, resolve each character to its byte, then express the byte
    as a charmap character. Macros and separators come out exactly as they
    went in, in their reordered positions.
    """
    out: list[str] = []
    for line in tokenise(text):
        visual = bidi.reorder(pipeline.shape_line(line.tokens))
        for tok in visual:
            if isinstance(tok, bidi.Atom):
                out.append("{" + tok.name + "}")
                continue
            for pre in tok.prefix:
                out.append("{" + pre.name + "}")
            cp = ord(tok.ch)
            byte = alloc.byte_for(cp)
            if byte is None:
                byte = table.to_byte.get(tok.ch)
            if byte is None:
                raise cm.EncodeError(
                    f"no byte for {tok.ch!r} (U+{cp:04X}) -- re-run alloc over "
                    f"the full translation corpus"
                )
            char = byte_chars.get(byte)
            if char is None:
                raise cm.EncodeError(
                    f"byte 0x{byte:02X} has no charmap character to spell it with"
                )
            out.append(char)
        if line.separator is not None:
            out.append({cm.NEWLINE: "\\n", cm.SCROLL: "\\l",
                        cm.PARAGRAPH: "\\p"}[line.separator])
    return "".join(out)


def plain_runs(text: str) -> str:
    """Just the translatable text, markup stripped -- for glyph allocation."""
    parts: list[str] = []
    for line in tokenise(text):
        parts.extend(t for t in line.tokens if isinstance(t, str))
        parts.append(" ")
    return "".join(parts)


# -- source patching -------------------------------------------------------

@dataclass
class PatchReport:
    patched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    files: set[str] = field(default_factory=set)


def patch_c_string(source: str, symbol: str, new_text: str) -> str | None:
    """Replace the ``_( ... )`` literal of ``symbol`` in C source text.

    Handles adjacent-literal concatenation across lines. Returns the patched
    text, or None if the symbol is not defined here.

    ``new_text`` needs no escaping: converted output contains only charmap
    characters (none of which are ``"`` or ``\\``), ``{MACRO}`` tokens, and
    the ``\\n \\l \\p`` sequences preproc expects to see literally.
    """
    pat = re.compile(
        rf"(\b{re.escape(symbol)}\s*\[\s*\]\s*=\s*_\()\s*(\"(?:[^\"\\]|\\.)*\"(?:\s*\"(?:[^\"\\]|\\.)*\")*)\s*(\))",
        re.S,
    )
    m = pat.search(source)
    if not m:
        return None
    return source[:m.start(2)] + f"\"{new_text}\"" + source[m.end(2):]


def patch_inc_string(source: str, symbol: str, new_text: str) -> str | None:
    """Replace the ``.string`` block under ``symbol::`` in an assembly .inc.

    The block is every consecutive ``.string`` line after the label; the
    final line's ``$`` terminator is preserved.
    """
    lines = source.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        if re.match(rf"\s*{re.escape(symbol)}::\s*$", ln):
            start = i + 1
            break
    if start is None:
        return None
    end = start
    while end < len(lines) and re.match(r'\s*\.string\s+"', lines[end]):
        end += 1
    if end == start:
        return None
    indent = re.match(r"\s*", lines[start]).group(0)
    replacement = f'{indent}.string "{new_text}$"\n'
    return "".join(lines[:start]) + replacement + "".join(lines[end:])


def patch_tree(
    repo: str | Path,
    translations: dict[str, str],
    table: cm.Charmap,
    alloc: Allocation,
    byte_chars: dict[int, str],
    *,
    search: tuple[str, ...] = ("src", "data"),
) -> PatchReport:
    """Convert every translation and splice it into the decomp sources."""
    repo = Path(repo)
    report = PatchReport()
    remaining = dict(translations)

    candidates: list[Path] = []
    for sub in search:
        root = repo / sub
        if root.is_dir():
            candidates += [p for p in root.rglob("*.c")]
            candidates += [p for p in root.rglob("*.inc")]

    for path in candidates:
        if not remaining:
            break
        text = path.read_text(encoding="utf-8")
        hits = [s for s in remaining if s in text]
        if not hits:
            continue
        changed = False
        for symbol in hits:
            converted = convert(remaining[symbol], table, alloc, byte_chars)
            patched = (patch_c_string(text, symbol, converted)
                       if path.suffix == ".c"
                       else patch_inc_string(text, symbol, converted))
            if patched is None:
                continue
            text = patched
            changed = True
            report.patched.append(symbol)
            del remaining[symbol]
        if changed:
            path.write_text(text, encoding="utf-8")
            report.files.add(str(path.relative_to(repo)))

    report.missing = sorted(remaining)
    return report
