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
from typing import Callable
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


# Codes that move the pen to an absolute or relative position: they separate
# layout segments (columns), so text must never be reordered across them.
_POSITIONAL_PREFIXES = ("CLEAR_TO", "CLEAR ", "SKIP", "SHIFT_RIGHT")


def _is_zero_width(macro: str) -> bool:
    return any(macro.startswith(p) for p in _ZERO_WIDTH_PREFIXES)


def _is_positional(macro: str) -> bool:
    return any(macro.startswith(p) for p in _POSITIONAL_PREFIXES)


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


def rtl_eligible(text: str) -> bool:
    """Whether a string can use the engine's RTL printer.

    Runtime placeholders ({B_BUFF1}, {PLAYER}...) expand to buffer contents in
    logical byte order at print time; drawn right-to-left they would come out
    mirrored. Until expansion is direction-aware, only strings without them
    can flip -- which in practice is most narrative dialogue.
    """
    return not any(
        isinstance(tok, cm.Atom) and tok.width
        for line in tokenise(text) for tok in line.tokens
    )


# A fragment is a string spliced into a buffer that is printed as PART of
# another line -- glued onto other content ("<foe> <SPECIES> used <MOVE>").
# It must never carry {RTL}: the code turns the printer right-to-left
# mid-buffer, so everything drawn after it comes out reversed. The signal is
# always the destination, never the string's own length:
#
#   * StringAppend(dst, X)                -- X is concatenated after content.
#   * StringCopy/CopyN(gStringVar1..3, X) -- X fills a {STR_VARn} placeholder
#     that a template later embeds mid-line. (gStringVar4 is the FINAL
#     printed buffer, so a copy into it is a standalone message -- keep it.)
#
# A string printed on its own (StringCopy into gStringVar4, or a table entry
# handed straight to a text printer) is NOT a fragment: it keeps {RTL} so it
# reveals right-to-left and right-aligns, which is the whole point.
_APPEND_RE = re.compile(
    r"\bStringAppend(?:N)?\s*\(\s*[^,]+,\s*([A-Za-z_]\w*)")
_COPY_TO_VAR_RE = re.compile(
    r"\bStringCopy(?:N)?\s*\(\s*(?:gStringVar[123]|textBuff)\s*,\s*([A-Za-z_]\w*)")
# Same two destinations, but indexing a pointer table: StringAppend(dst,
# tbl[i]) / StringCopy(gStringVar2, tbl[i]). Every member of such a table is
# a fragment (stat names, etc.).
_APPEND_TABLE_RE = re.compile(
    r"\bStringAppend(?:N)?\s*\(\s*[^,]+,\s*(\w+)\s*\[")
_COPY_TABLE_RE = re.compile(
    r"\bStringCopy(?:N)?\s*\(\s*gStringVar[123]\s*,\s*(\w+)\s*\[")
_PTR_TABLE_RE = re.compile(
    r"const\s+u8\s*\*\s*const\s+(\w+)\s*\[[^\]]*\]\s*=\s*\{([^;]*?)\}\s*;", re.S)
# A designated-initialiser row inside a pointer table: `[STRINGID_X - Y] = sym,`
_TABLE_ROW_RE = re.compile(r"\[\s*(\w+)\b[^\]]*\]\s*=\s*(\w+)")
# The battle placeholder expander copies fragments inline through a scratch
# pointer (`toCpy = sText_FoePkmnPrefix2; ... while (*toCpy != EOS) ...`),
# which no String* call would reveal. Capture every symbol assigned to it,
# including the arms of `toCpy = cond ? A : B;`.
_INLINE_COPY_RE = re.compile(r"\btoCpy\s*=\s*([^;]+);")
_SYMBOL_RE = re.compile(r"\b((?:sText|gText|gBattleText|g[A-Z]\w*Text)\w*)\b")
# A stringId packed (low + high byte) into a battle scratch buffer:
# `gBattleTextBuff2[i++] = STRINGID_STATROSE;` / `... = STRINGID_X >> 8;` /
# `i = STRINGID_ABOOSTED;`. Its dispatch-table entry is later StringAppend'd
# as a B_BUFF fragment, so that one row must lose {RTL} even though the rest
# of the big table it lives in holds standalone messages that keep it.
_BUFFER_STORE_RE = re.compile(
    r"(?:gBattleTextBuff\d\s*\[[^\]]*\]|\bi)\s*=\s*(STRINGID_\w+)")
# Above this many members, a pointer table indexed by an append/var-copy is
# a dispatch table (mostly standalone messages), handled row-by-row; at or
# below, it is a pure fragment table and every member is a fragment.
_FRAGMENT_TABLE_MAX = 64
# Fixed-grid column labels: drawn at a hard-coded x next to a value at
# another hard-coded x, often with a hard-coded FillWindowPixelRect erasing
# one column before the redraw. {RTL} right-aligns to the window edge instead,
# which puts the glyphs somewhere the layout maths never accounts for:
#
#   * level-up box -- stat name at x=0, the +N change at x=56 in a 10-tile
#     (80px) window: right-aligning slides the name into the value column.
#   * option menu -- labels at x=8 and values at x=130 in a 26-tile (208px)
#     window, with the value's erase rect covering x=130..200. Right-aligned,
#     the LABEL lands in that rect (so every value redraw rubs the label out)
#     while the value itself draws near x=78, outside it -- so the old value is
#     never erased and successive values pile up on each other.
#
# Every member of such a table stays left-anchored in plain visual order, which
# restores the vanilla geometry exactly -- same treatment as the battle menu.
_FIXED_COLUMN_TABLES = (
    "sLevelUpWindowStatNames",
    "sOptionMenuItemsNames", "sTextSpeedOptions", "sBattleSceneOptions",
    "sBattleStyleOptions", "sSoundOptions", "sButtonTypeOptions",
)
# Individually fixed-column strings that are not table members:
#   * gText_FrameType -- copied into a local buffer, then the frame number is
#     appended, then drawn in the option menu's value column.
#   * gText_PickSwitchCancel -- positioned as `0xE4 - GetStringWidth(...)`, so
#     it already right-aligns itself; {RTL} would apply that a second time and
#     throw it to the opposite edge.
_FIXED_COLUMN_SYMBOLS = ("gText_FrameType", "gText_PickSwitchCancel")


def buffer_source_symbols(repo: str | Path) -> set[str]:
    """Symbols whose bytes get spliced into a buffer and reprinted mid-line.

    See the regex comments above for the exact, destination-based rules. The
    search is deliberately narrow: it excludes genuine fragments (so a name
    drawn after them is not reversed) while leaving every standalone message
    on the RTL printer (so it reveals right-to-left). Purely structural, so
    new fragments are covered without editing a hand list.
    """
    repo = Path(repo)
    files = [p for sub in ("src", "data") if (repo / sub).is_dir()
             for p in (repo / sub).rglob("*.c")]
    texts = {p: p.read_text(encoding="utf-8", errors="replace") for p in files}

    symbols: set[str] = set(_FIXED_COLUMN_SYMBOLS)
    fragment_tables: set[str] = set()
    buffered_ids: set[str] = set()
    id_to_symbol: dict[str, str] = {}
    for txt in texts.values():
        symbols.update(_APPEND_RE.findall(txt))
        symbols.update(_COPY_TO_VAR_RE.findall(txt))
        fragment_tables.update(_APPEND_TABLE_RE.findall(txt))
        fragment_tables.update(_COPY_TABLE_RE.findall(txt))
        for rhs in _INLINE_COPY_RE.findall(txt):
            symbols.update(_SYMBOL_RE.findall(rhs))
        buffered_ids.update(_BUFFER_STORE_RE.findall(txt))

    for txt in texts.values():
        for tname, body in _PTR_TABLE_RE.findall(txt):
            rows = _TABLE_ROW_RE.findall(body)
            members = re.findall(r"\b([A-Za-z_]\w*)\b", body)
            # A small table indexed by an append/var-copy is a pure fragment
            # table (stat names, card colours): every member is a fragment.
            # A big one is a dispatch table that is only occasionally indexed
            # as a fragment (gBattleStringsTable), so its members are handled
            # one row at a time by the buffered-STRINGID rule below -- never
            # wholesale, which would strip {RTL} from its standalone messages.
            if tname in _FIXED_COLUMN_TABLES:
                symbols.update(members)
            elif tname in fragment_tables and len(members) <= _FRAGMENT_TABLE_MAX:
                symbols.update(members)
            id_to_symbol.update(rows)

    symbols.update(id_to_symbol[i] for i in buffered_ids if i in id_to_symbol)
    symbols.discard("")
    return symbols


def convert(
    text: str,
    table: cm.Charmap,
    alloc: Allocation,
    byte_chars: dict[int, str],
    *,
    rtl: bool = False,
) -> str:
    """Arabic with decomp markup -> charmap-character string for the sources.

    Shape, reorder, resolve each character to its byte, then express the byte
    as a charmap character. Macros and separators come out exactly as they
    went in, in their reordered positions.

    With ``rtl`` (requires the engine patch and an eligible string), the
    output is prefixed with {RTL} and each line is MIRRORED -- the RTL printer
    walks the bytes in order but draws right-to-left, so the byte stream must
    be the reverse of the visual order. The typewriter then reveals Arabic
    from the right, and lines right-align.
    """
    rtl = rtl and rtl_eligible(text)
    out: list[str] = ["{RTL}"] if rtl else []
    for line in tokenise(text):
        # Positional codes ({CLEAR_TO 56}...) are COLUMN separators: the pen
        # jumps to an absolute x. Reordering across one would swap which label
        # sits in which column slot -- the battle menu's FIGHT/BAG grid maps
        # cursor position to action by slot, so a swapped label picks the
        # wrong action. Each segment reorders independently; segment order
        # and the separators stay exactly where the author put them.
        segments: list[list] = [[]]
        separators: list[cm.Atom] = []
        for tok in line.tokens:
            if isinstance(tok, cm.Atom) and _is_positional(tok.name):
                separators.append(tok)
                segments.append([])
            else:
                segments[-1].append(tok)

        visual: list = []
        for i, seg in enumerate(segments):
            if i > 0:
                visual.append(bidi.Atom(separators[i - 1].name, b"", width=False))
            # Zero-width codes at a segment's edges stay at the edges. Left
            # to the generic binding they ride the first LOGICAL character,
            # which reordering puts mid-run -- so a leading colour code would
            # execute after half the word had already drawn in the old colour.
            lead: list = []
            trail: list = []
            while seg and isinstance(seg[0], cm.Atom) and not seg[0].width:
                lead.append(seg.pop(0))
            while seg and isinstance(seg[-1], cm.Atom) and not seg[-1].width:
                trail.append(seg.pop())
            trail.reverse()
            seg_vis = bidi.reorder(pipeline.shape_line(seg))
            if rtl:
                seg_vis = list(reversed(seg_vis))
            visual.extend(bidi.Atom(a.name, b"", width=False) for a in lead)
            visual.extend(seg_vis)
            visual.extend(bidi.Atom(a.name, b"", width=False) for a in trail)
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
    errors: list[str] = field(default_factory=list)
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


def patch_items_json(
    repo: "str | Path",
    names: dict[str, str],
    descriptions: dict[str, str],
    table: cm.Charmap,
    alloc: Allocation,
    byte_chars: dict[int, str],
) -> tuple[list[str], list[str]]:
    """Patch item names/descriptions in src/data/items.json.

    items.h looks like the place, but it is GENERATED from items.json by the
    build (and gitignored), so edits there are silently regenerated away and
    invisible to git resets -- which is exactly how a stale half-patched copy
    once masqueraded as a patching bug. The JSON is the source of truth:
    ``english`` feeds the ``.name`` field and ``description_english`` the
    gItemDescription_* symbol, per src/data/items.json.txt.

    Names are DATA (inserted via buffers, listed by the LTR printer): plain
    visual order, no RTL code. Descriptions are printed standalone and get
    the RTL treatment. The stale items.h is deleted so the build regenerates.

    Descriptions are keyed by their generated symbol name
    (``gItemDescription_<ITEM_ID>``), matching how they appear to the rest of
    the translation corpus.
    """
    import json as _json

    path = Path(repo) / "src" / "data" / "items.json"
    data = _json.loads(path.read_text(encoding="utf-8"))
    items = data["items"]

    by_name = {e.get("english"): e for e in items}
    named: list[str] = []
    for english, arabic in names.items():
        entry = by_name.get(english)
        if entry is None:
            continue
        entry["english"] = convert(arabic, table, alloc, byte_chars, rtl=False)
        named.append(english)

    by_symbol = {f"gItemDescription_{e['itemId']}": e
                 for e in items if e.get("itemId")}
    described: list[str] = []
    for symbol, arabic in descriptions.items():
        entry = by_symbol.get(symbol)
        if entry is None:
            continue
        entry["description_english"] = convert(arabic, table, alloc, byte_chars,
                                               rtl=True)
        described.append(symbol)

    path.write_text(_json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    stale = Path(repo) / "src" / "data" / "items.h"
    stale.unlink(missing_ok=True)
    return named, described


def patch_indexed_array(
    source: str,
    entries: dict[str, str],
    table: cm.Charmap,
    alloc: Allocation,
    byte_chars: dict[int, str],
    *,
    limit: int | None = None,
) -> tuple[str, list[str], list[str]]:
    """Patch ``[CONST] = _("...")`` rows in a data table.

    Species, move and type names live in fixed-width arrays
    (``gSpeciesNames[][POKEMON_NAME_LENGTH + 1]``), so ``limit`` is a HARD
    byte budget: an over-long name would silently truncate mid-glyph in game.
    Anything over budget is rejected with an error instead of patched.

    These are data strings -- inserted into sentences via buffers and listed
    by the LTR printer -- so they convert in plain visual order, no RTL code.
    """
    patched: list[str] = []
    errors: list[str] = []
    for const, arabic in entries.items():
        pat = re.compile(rf'(\[{re.escape(const)}\]\s*=\s*_\(")([^"]*)("\))')
        m = pat.search(source)
        if not m:
            errors.append(f"{const}: not found")
            continue
        converted = convert(arabic, table, alloc, byte_chars, rtl=False)
        if limit is not None and len(converted) > limit:
            errors.append(f"{const}: {arabic!r} needs {len(converted)} bytes, "
                          f"limit {limit}")
            continue
        source = source[:m.start(2)] + converted + source[m.end(2):]
        patched.append(const)
    return source, patched, errors


def patch_tree(
    repo: str | Path,
    translations: dict[str, str],
    table: cm.Charmap,
    alloc: Allocation,
    byte_chars: dict[int, str],
    *,
    search: tuple[str, ...] = ("src", "data"),
    rtl: "bool | Callable[[str], bool]" = False,
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
            candidates += [p for p in root.rglob("*.h")]
            candidates += [p for p in root.rglob("*.inc")]
            candidates += [p for p in root.rglob("*.s")]

    for path in candidates:
        if not remaining:
            break
        text = path.read_text(encoding="utf-8")
        hits = [s for s in remaining if s in text]
        if not hits:
            continue
        changed = False
        for symbol in hits:
            want_rtl = rtl(symbol) if callable(rtl) else rtl
            try:
                converted = convert(remaining[symbol], table, alloc, byte_chars,
                                    rtl=want_rtl)
            except cm.EncodeError as exc:
                report.errors.append(f"{symbol}: {exc}")
                del remaining[symbol]
                continue
            patched = (patch_inc_string(text, symbol, converted)
                       if path.suffix in (".inc", ".s")
                       else patch_c_string(text, symbol, converted))
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
