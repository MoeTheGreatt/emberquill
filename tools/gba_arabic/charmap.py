"""Gen 3 Pokemon text encoding.

These games do not use ASCII. Each byte indexes a glyph in the ROM's font
sheet, with a handful of values reserved as control codes. This module holds
the byte<->character table, the script-source tokeniser, and the encoder.

The built-in table covers the international (English) charmap. Treat it as a
starting point, not gospel: ROM hacks routinely move glyphs around. Load the
real table from your target with ``load_tbl`` or ``load_pokeemerald_charmap``
and diff it with ``gba-arabic verify`` before you trust a build.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Reserved control bytes. These are stable across the Gen 3 family.
EOS = 0xFF          # end of string
NEWLINE = 0xFE      # \n  - next line, same box
SCROLL = 0xFA       # \l  - scroll up one line and continue
PARAGRAPH = 0xFB    # \p  - wait for input, clear box, continue
PLACEHOLDER = 0xFD  # \v  - followed by one ID byte, expands at runtime
SPECIAL = 0xFC      # control sequence, followed by a code byte and its args
SPACE = 0x00

CONTROL_BYTES = frozenset({EOS, NEWLINE, SCROLL, PARAGRAPH, PLACEHOLDER, SPECIAL})

# Byte -> character for the international charmap.
_DEFAULT: dict[int, str] = {SPACE: " "}
for _i, _c in enumerate("0123456789"):
    _DEFAULT[0xA1 + _i] = _c
_DEFAULT.update({
    0xAB: "!", 0xAC: "?", 0xAD: ".", 0xAE: "-", 0xAF: "·",
    0xB0: "…", 0xB1: "“", 0xB2: "”", 0xB3: "‘", 0xB4: "’",
    0xB5: "♂", 0xB6: "♀", 0xB7: "¥", 0xB8: ",", 0xB9: "×",
    0xBA: "/", 0xF0: ":",
})
for _i in range(26):
    _DEFAULT[0xBB + _i] = chr(ord("A") + _i)
    _DEFAULT[0xD5 + _i] = chr(ord("a") + _i)
del _i, _c

# Byte values that are safe to hand over to Arabic glyphs in a fully-Arabic
# translation. 0x01-0x77 holds accented Latin and symbol glyphs that Arabic
# text never uses. VERIFY THESE PER ROM: a hack may already have claimed them,
# and some engines special-case individual values.
DEFAULT_FREE_SLOTS: tuple[tuple[int, int], ...] = ((0x01, 0x77),)

# Runtime placeholders: name -> ID byte following 0xFD. Emerald/Ruby/Sapphire
# numbering; FireRed differs, so override this from the profile.
DEFAULT_PLACEHOLDERS: dict[str, int] = {
    "PLAYER": 0x01, "STR_VAR_1": 0x02, "STR_VAR_2": 0x03, "STR_VAR_3": 0x04,
    "KUN": 0x05, "RIVAL": 0x06, "VERSION": 0x07,
}

_TOKEN_RE = re.compile(r"\{([^}]*)\}|\\([nlp])")
_HEX_RE = re.compile(r"^(?:[0-9A-Fa-f]{2})(?:\s+[0-9A-Fa-f]{2})*$")


class EncodeError(ValueError):
    """Raised when a character or token cannot be encoded."""


@dataclass
class Atom:
    """Opaque byte run: a control code or a runtime placeholder."""

    name: str
    data: bytes
    width: bool = False     # True if it draws something (placeholders do)


@dataclass
class Line:
    """One rendered line, plus the separator that ended it."""

    tokens: list[str | Atom] = field(default_factory=list)
    separator: int | None = None    # NEWLINE / SCROLL / PARAGRAPH, or None at end


@dataclass
class Charmap:
    to_char: dict[int, str] = field(default_factory=lambda: dict(_DEFAULT))
    placeholders: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_PLACEHOLDERS))
    verified: bool = False

    def __post_init__(self) -> None:
        self._rebuild()

    def _rebuild(self) -> None:
        self.to_byte: dict[str, int] = {}
        # Lowest byte wins when a character appears twice, for determinism.
        for b in sorted(self.to_char):
            self.to_byte.setdefault(self.to_char[b], b)

    def assign(self, byte: int, char: str) -> None:
        self.to_char[byte] = char
        self._rebuild()

    def free_slots(self, ranges: tuple[tuple[int, int], ...] = DEFAULT_FREE_SLOTS,
                   *, keep: frozenset[int] = frozenset()) -> list[int]:
        """Byte values available for reassignment, low to high."""
        out = []
        for lo, hi in ranges:
            for b in range(lo, hi + 1):
                if b not in CONTROL_BYTES and b not in keep:
                    out.append(b)
        return out

    # -- parsing -----------------------------------------------------------

    def tokenise(self, text: str) -> list[Line]:
        """Split script source into lines of characters and opaque atoms.

        Recognised markup:
          ``\\n`` ``\\l`` ``\\p``   line separators
          ``{PLAYER}``            runtime placeholder from the profile
          ``{FC 01 02}``          raw bytes, for control codes with arguments
          ``{{``                  a literal brace
        """
        lines = [Line()]
        pos = 0
        text = text.replace("{{", "\x00LBRACE\x00")
        while pos < len(text):
            m = _TOKEN_RE.search(text, pos)
            if not m:
                self._push_text(lines[-1], text[pos:])
                break
            self._push_text(lines[-1], text[pos:m.start()])
            pos = m.end()

            if m.group(2):                              # \n \l \p
                lines[-1].separator = {"n": NEWLINE, "l": SCROLL, "p": PARAGRAPH}[m.group(2)]
                lines.append(Line())
                continue

            body = m.group(1).strip()
            lines[-1].tokens.append(self._atom(body))
        return lines

    def _push_text(self, line: Line, chunk: str) -> None:
        line.tokens.extend(chunk.replace("\x00LBRACE\x00", "{"))

    def _atom(self, body: str) -> Atom:
        upper = body.upper()
        if upper in self.placeholders:
            return Atom(upper, bytes([PLACEHOLDER, self.placeholders[upper]]), width=True)
        if _HEX_RE.match(body):
            return Atom(body.upper(), bytes(int(b, 16) for b in body.split()))
        raise EncodeError(
            f"unknown token {{{body}}} - use a placeholder name from the profile "
            f"or raw hex like {{FC 01 02}}"
        )

    # -- encoding ----------------------------------------------------------

    def encode_char(self, ch: str) -> int:
        try:
            return self.to_byte[ch]
        except KeyError:
            raise EncodeError(
                f"no byte for {ch!r} (U+{ord(ch):04X}); allocate a glyph slot for it"
            ) from None

    def decode(self, data: bytes) -> str:
        out: list[str] = []
        i = 0
        while i < len(data):
            b = data[i]
            if b == EOS:
                break
            if b == PLACEHOLDER and i + 1 < len(data):
                nid = data[i + 1]
                name = next((k for k, v in self.placeholders.items() if v == nid), f"{nid:02X}")
                out.append(f"{{{name}}}")
                i += 2
                continue
            if b == SPECIAL and i + 1 < len(data):
                out.append(f"{{FC {data[i + 1]:02X}}}")
                i += 2
                continue
            out.append({NEWLINE: "\\n", SCROLL: "\\l", PARAGRAPH: "\\p"}.get(
                b, self.to_char.get(b, f"[{b:02X}]")))
            i += 1
        return "".join(out)


def load_tbl(path: str) -> Charmap:
    """Load a standard ROM-hacking ``.tbl`` file (``XX=char`` per line)."""
    table: dict[int, str] = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.rstrip("\n")
            if not raw or raw.startswith(("#", ";")) or "=" not in raw:
                continue
            key, _, val = raw.partition("=")
            key = key.strip()
            try:
                byte = int(key, 16)
            except ValueError:
                continue
            if 0 <= byte <= 0xFF and val:
                table[byte] = val
    return Charmap(to_char=table, verified=True)


def load_pokeemerald_charmap(path: str) -> Charmap:
    """Load a decomp ``charmap.txt`` (``'A' = BB`` / ``SYMBOL = XX`` lines).

    Reading the table straight out of the decomp is the only way to be certain
    it matches what you are building.
    """
    table: dict[int, str] = {}
    pat = re.compile(r"^\s*(?:'(?P<q>.)'|(?P<n>[A-Za-z0-9_]+))\s*=\s*(?P<bytes>[0-9A-Fa-f ]+)")
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.split("@")[0]
            m = pat.match(raw)
            if not m:
                continue
            parts = m.group("bytes").split()
            if len(parts) != 1:
                continue        # multi-byte macro, not a plain glyph
            byte = int(parts[0], 16)
            char = m.group("q")
            if char:
                table[byte] = char
    return Charmap(to_char=table, verified=True)
