"""Integration with a Gen 3 decomp (pokefirered / pokeemerald / pokeruby).

Working in a decomp instead of patching a ROM binary removes most of the hard
parts of this project: no pointer repointing, no fixed-length text regions, and
the font sheet and glyph widths are ordinary files rather than offsets you have
to locate. This module edits those files.

The font sheet is a PNG grid of glyph cells indexed directly by character byte
-- cell 0xBB is 'A', cell 0x1B is 'é' -- so installing an Arabic glyph is
literally pasting it into the cell for its allocated byte. Widths live in a
sibling C array, indexed the same way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .fontgen import FontSpec, blank, ink_bbox, _require_pil

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:  # pragma: no cover
    HAVE_PIL = False

# Latin font sheets in a Gen 3 decomp, in the order most translations care
# about. Every one of these needs the same Arabic glyphs installed, or Arabic
# text will silently fall back to Latin artwork in whichever window uses the
# font you skipped.
LATIN_SHEETS = ("latin_normal", "latin_small", "latin_male", "latin_female")

WIDTH_ARRAYS = {
    "latin_normal": "sFontNormalLatinGlyphWidths",
    "latin_small": "sFontSmallLatinGlyphWidths",
    "latin_male": "sFontMaleLatinGlyphWidths",
    "latin_female": "sFontFemaleLatinGlyphWidths",
}


@dataclass
class Sheet:
    """A decomp font sheet and its geometry."""

    path: Path
    image: "Image.Image"
    spec: FontSpec

    @property
    def cells(self) -> int:
        return (self.image.width // self.spec.cell_w) * (
            self.image.height // self.spec.cell_h
        )

    def cell_box(self, index: int) -> tuple[int, int, int, int]:
        cols = self.image.width // self.spec.cell_w
        x = (index % cols) * self.spec.cell_w
        y = (index // cols) * self.spec.cell_h
        return (x, y, x + self.spec.cell_w, y + self.spec.cell_h)

    def cell(self, index: int) -> "Image.Image":
        return self.image.crop(self.cell_box(index))

    def is_blank(self, index: int) -> bool:
        return ink_bbox(self.cell(index)) is None


# Cell geometry is a property of the sheet, not the game. In pokefirered
# latin_normal.png is 256x512 with 16x16 cells, while latin_small.png is
# 256x256 with 8x16 ones -- both work out to 512 cells, matching the 512-entry
# width arrays in src/text.c. Rather than hardcode that, candidates are tried
# and validated against the charmap.
CANDIDATE_SPECS = ((16, 16), (8, 16), (8, 8), (16, 8))


def detect_spec(image: "Image.Image", table, base: FontSpec = FontSpec()) -> FontSpec:
    """Infer cell geometry by testing which one the charmap agrees with.

    A correct geometry puts artwork in the cells for 'A', 'Z', 'a', 'z', '0'
    and '9', and leaves the space cell empty. A wrong one almost always breaks
    one of those, which makes this a cheap and reliable check -- and far safer
    than assuming, since writing a glyph at the wrong stride corrupts the sheet.
    """
    _require_pil()
    probes = [c for c in "AZaz09" if table.to_byte.get(c) is not None]
    space = table.to_byte.get(" ")

    for cw, ch in CANDIDATE_SPECS:
        if image.width % cw or image.height % ch:
            continue
        cols = image.width // cw
        cells = cols * (image.height // ch)
        spec = FontSpec(cell_w=cw, cell_h=ch, columns=cols,
                        size=base.size, baseline=base.baseline,
                        fill_at=base.fill_at, edge_at=base.edge_at,
                        pad_right=base.pad_right)
        trial = Sheet(path=Path("<probe>"), image=image, spec=spec)

        ok = all(
            table.to_byte[c] < cells and not trial.is_blank(table.to_byte[c])
            for c in probes
        )
        if ok and space is not None and space < cells:
            ok = trial.is_blank(space)
        if ok:
            return spec

    raise ValueError(
        f"could not determine cell geometry for a {image.width}x{image.height} "
        f"sheet; tried {CANDIDATE_SPECS}. Pass the geometry explicitly."
    )


def load_sheet(path: str | Path, spec: FontSpec = FontSpec()) -> Sheet:
    _require_pil()
    p = Path(path)
    img = Image.open(p)
    if img.mode != "P":
        raise ValueError(
            f"{p.name} is mode {img.mode}; decomp font sheets are paletted (P). "
            f"Converting would lose the index values the tile converter needs."
        )
    return Sheet(path=p, image=img.copy(), spec=spec)


def find_sheets(repo: str | Path) -> dict[str, Path]:
    """Locate the Latin font sheets in a decomp checkout."""
    root = Path(repo) / "graphics" / "fonts"
    if not root.is_dir():
        raise FileNotFoundError(f"{root} not found - is {repo} a Gen 3 decomp?")
    return {
        name: root / f"{name}.png"
        for name in LATIN_SHEETS
        if (root / f"{name}.png").exists()
    }


def blank_cells(sheet: Sheet, lo: int = 0x00, hi: int = 0xFF) -> list[int]:
    """Cells in ``[lo, hi]`` with no artwork at all.

    A blank cell is unambiguously free: there is no glyph to lose. Cells that
    *do* hold artwork may still be reusable -- the accented Latin in 0x01-0x77
    is, for an Arabic build -- but that is a judgement about the script, not
    about the sheet, so it is not decided here.
    """
    return [i for i in range(lo, min(hi, sheet.cells - 1) + 1) if sheet.is_blank(i)]


def install(
    sheet: Sheet,
    glyphs: dict[int, "Image.Image"],
    glyph_to_byte: dict[int, int],
) -> tuple[int, list[int]]:
    """Paste rendered glyphs into the cells for their allocated bytes.

    Returns (cells written, bytes skipped because they fall outside the sheet).
    """
    _require_pil()
    from .fontgen import glyph_width, to_sheet_cell

    written, skipped = 0, []
    for codepoint, byte in sorted(glyph_to_byte.items(), key=lambda kv: kv[1]):
        cell = glyphs.get(codepoint)
        if cell is None:
            continue
        if byte >= sheet.cells:
            skipped.append(byte)
            continue
        box = sheet.cell_box(byte)
        # Convert into the sheet's own index convention, then clear the cell
        # first so leftover pixels from the accented Latin glyph underneath do
        # not bleed into the Arabic one.
        prepared = to_sheet_cell(cell, glyph_width(cell, sheet.spec), sheet.spec)
        sheet.image.paste(blank(sheet.spec), box[:2])
        sheet.image.paste(prepared, box[:2])
        written += 1
    return written, skipped


def save(sheet: Sheet, path: str | Path | None = None) -> Path:
    out = Path(path) if path else sheet.path
    # Preserve the palette and indexed mode; the decomp's converter reads
    # palette indices, not colours.
    sheet.image.save(out)
    return out


# -- width table -----------------------------------------------------------

def read_width_table(source: str | Path, array: str) -> list[int]:
    """Parse a glyph width array out of a decomp C source file."""
    text = Path(source).read_text(encoding="utf-8")
    m = re.search(
        rf"{re.escape(array)}\s*\[\s*\]\s*=\s*\{{(.*?)\}}\s*;", text, re.S
    )
    if not m:
        raise KeyError(f"{array} not found in {source}")
    return [int(v) for v in re.findall(r"\d+", m.group(1))]


def patch_width_table(
    source: str | Path,
    array: str,
    widths: dict[int, int],
    *,
    per_line: int = 14,
) -> str:
    """Return ``source``'s text with ``array`` updated from ``widths``.

    Only the indices present in ``widths`` change; the rest of the table, and
    the rest of the file, are left exactly as they were.
    """
    path = Path(source)
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"({re.escape(array)}\s*\[\s*\]\s*=\s*\{{)(.*?)(\}}\s*;)", re.S
    )
    m = pattern.search(text)
    if not m:
        raise KeyError(f"{array} not found in {path}")

    values = [int(v) for v in re.findall(r"\d+", m.group(2))]
    for index, width in widths.items():
        if 0 <= index < len(values):
            values[index] = width

    lines = []
    for start in range(0, len(values), per_line):
        chunk = values[start:start + per_line]
        lines.append("    " + " ".join(f"{v:2d}," for v in chunk))
    body = "\n" + "\n".join(lines) + "\n"
    return text[:m.start(2)] + body + text[m.end(2):]


def verify_sheet_indexing(sheet: Sheet, table) -> list[str]:
    """Sanity-check that cell index really equals character byte.

    Cheap insurance against a sheet whose layout differs from the assumption
    the whole pipeline rests on. Characters that should have artwork are
    checked for artwork, and the space cell for the absence of it.
    """
    problems = []
    for ch in "AZaz09":
        byte = table.to_byte.get(ch)
        if byte is None:
            problems.append(f"{ch!r} missing from the charmap")
        elif byte < sheet.cells and sheet.is_blank(byte):
            problems.append(f"cell 0x{byte:02X} for {ch!r} is blank")
    space = table.to_byte.get(" ")
    if space is not None and space < sheet.cells and not sheet.is_blank(space):
        problems.append(f"cell 0x{space:02X} for space has artwork")
    return problems
