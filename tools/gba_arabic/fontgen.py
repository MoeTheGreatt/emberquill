"""Build GBA font glyphs for the allocated Arabic forms.

Two outputs, for the two ways people patch these games:

  * a PNG glyph sheet -- what you want for a decomp build, where the repo's
    graphics converter turns PNG into tiles for you, and what you want for
    hand-editing in a pixel editor either way;
  * raw 4bpp tile bytes plus a width table -- for patching a ROM directly.

A caveat worth taking seriously: automatic rasterisation at Gen 3's glyph
height is a starting point, not a finished font. Arabic carries much of its
letter-to-letter distinction in curves and dot placement, and that does not
survive downsampling to ~10 pixels intact. Expect to hand-pixel at least the
frequent forms; the allocator orders them by frequency so you can work down the
list, and ``load_sheet`` reads your edits back so nothing is lost on a rebuild.

On face choice: a flatter, more geometric face downsamples markedly better here
than a Naskh one. Comparing DejaVu Sans against Noto Naskh Arabic at a 16px
cell, the sans came out the more legible of the two -- Naskh's stroke contrast
and fine curve detail are the first things lost at this size.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except ImportError:  # pragma: no cover - exercised by absence, not presence
    HAVE_PIL = False


@dataclass(frozen=True)
class FontSpec:
    """Glyph cell geometry.

    Gen 3 font sheets are grids of fixed cells with a separate width table, so
    the drawn glyph can be narrower than the cell. Confirm these against your
    target's font sheet before building: hacks change them.
    """

    cell_w: int = 16
    cell_h: int = 16
    columns: int = 16
    size: int = 15          # point size to rasterise at
    baseline: int = 13      # pixels from cell top to the text baseline
    levels: int = 3         # palette entries used: 0 transparent, 1 fill, 2 edge
    fill_at: int = 160      # coverage >= this becomes index 1
    edge_at: int = 64       # coverage >= this becomes index 2
    pad_right: int = 0      # blank columns added to the advance; 0 keeps joins closed


def _require_pil() -> None:
    if not HAVE_PIL:
        raise RuntimeError(
            "Pillow is required for font work: pip install pillow "
            "(the shaping, reordering and encoding stages do not need it)"
        )


def render(
    codepoints: Sequence[int],
    font_path: str,
    spec: FontSpec = FontSpec(),
) -> dict[int, "Image.Image"]:
    """Rasterise each glyph into its own cell-sized indexed image.

    Presentation Forms-B codepoints are drawn directly, so no text shaper is
    involved here -- the form was already decided in ``shaping``. Most Arabic
    fonts cover the block; if a glyph comes out blank, that font does not, and
    you will need to hand-pixel it or pick another face.
    """
    _require_pil()
    font = ImageFont.truetype(font_path, spec.size)
    out: dict[int, Image.Image] = {}
    for cp in codepoints:
        # Draw onto an oversized strip first, then slide the ink flush against
        # the cell's left edge. Glyphs are drawn left-to-right by the engine
        # over pre-reversed text, exactly like Latin ones, and it advances by
        # the width table -- so ink must start at x=0 with no side bearing.
        # Drawing straight into the cell would clip forms whose outline starts
        # left of the pen position, which several Arabic finals do.
        strip = Image.new("L", (spec.cell_w * 3, spec.cell_h), 0)
        ImageDraw.Draw(strip).text(
            (spec.cell_w, spec.baseline), chr(cp), fill=255, font=font, anchor="ls"
        )
        # Threshold before measuring, not after: a column of antialiasing too
        # faint to survive quantisation would otherwise set the crop origin and
        # then vanish, leaving the glyph inset from the cell edge by a pixel.
        quantised = _quantise(strip, spec)
        cell = blank(spec)
        bbox = ink_bbox(quantised)
        if bbox is not None:
            right = min(bbox[2], bbox[0] + spec.cell_w)
            cell.paste(quantised.crop((bbox[0], 0, right, spec.cell_h)), (0, 0))
        out[cp] = cell
    return out


def _quantise(img: "Image.Image", spec: FontSpec) -> "Image.Image":
    """Grayscale coverage -> palette indices 0/1/2."""
    px = img.load()
    dst = Image.new("P", img.size, 0)
    dpx = dst.load()
    for y in range(img.height):
        for x in range(img.width):
            v = px[x, y]
            dpx[x, y] = 1 if v >= spec.fill_at else (2 if v >= spec.edge_at else 0)
    dst.putpalette(_palette())
    return dst


def _palette() -> list[int]:
    # Index 0 is the transparent/background colour the graphics converter keys
    # on; 1 is the glyph body, 2 its darker edge. Magenta makes a stray
    # background pixel obvious when you are editing the sheet by hand.
    entries = [255, 0, 255, 248, 248, 248, 96, 96, 112] + [0, 0, 0] * 13
    return entries[:48] + [0] * (768 - 48)


def blank(spec: FontSpec = FontSpec()) -> "Image.Image":
    _require_pil()
    img = Image.new("P", (spec.cell_w, spec.cell_h), 0)
    img.putpalette(_palette())
    return img


def build_sheet(
    glyphs: dict[int, "Image.Image"],
    order: Sequence[int],
    spec: FontSpec = FontSpec(),
) -> "Image.Image":
    """Compose a glyph sheet, ``order`` giving cell order left-to-right.

    Pass the byte-sorted allocation as ``order`` so cell position matches byte
    value -- that is what lets the ROM index the sheet directly.
    """
    _require_pil()
    rows = max(1, -(-len(order) // spec.columns))
    sheet = Image.new("P", (spec.columns * spec.cell_w, rows * spec.cell_h), 0)
    sheet.putpalette(_palette())
    for i, cp in enumerate(order):
        cell = glyphs.get(cp)
        if cell is None:
            continue
        x = (i % spec.columns) * spec.cell_w
        y = (i // spec.columns) * spec.cell_h
        sheet.paste(cell, (x, y))
    return sheet


def load_sheet(
    path: str,
    order: Sequence[int],
    spec: FontSpec = FontSpec(),
) -> dict[int, "Image.Image"]:
    """Read a hand-edited sheet back into per-glyph cells."""
    _require_pil()
    sheet = Image.open(path).convert("P")
    out: dict[int, Image.Image] = {}
    for i, cp in enumerate(order):
        x = (i % spec.columns) * spec.cell_w
        y = (i // spec.columns) * spec.cell_h
        if x + spec.cell_w > sheet.width or y + spec.cell_h > sheet.height:
            break
        out[cp] = sheet.crop((x, y, x + spec.cell_w, y + spec.cell_h))
    return out


def to_4bpp(img: "Image.Image") -> bytes:
    """Pack an image into GBA 4bpp tiles.

    8x8 tiles in row-major order; within a tile, two pixels per byte with the
    left pixel in the low nibble.
    """
    _require_pil()
    px = img.convert("P").load()
    out = bytearray()
    for ty in range(0, img.height, 8):
        for tx in range(0, img.width, 8):
            for y in range(8):
                for x in range(0, 8, 2):
                    lo = px[tx + x, ty + y] & 0xF
                    hi = px[tx + x + 1, ty + y] & 0xF
                    out.append(lo | (hi << 4))
    return bytes(out)


def ink_bbox(img: "Image.Image") -> tuple[int, int, int, int] | None:
    """Bounding box of the non-background pixels.

    Measured on palette *indices*, deliberately. Converting a paletted glyph to
    "L" first would resolve index 0 to its palette colour -- which is a visible
    magenta here -- and every cell would then measure as full-width ink.
    """
    _require_pil()
    idx = img.convert("P")
    return Image.frombytes("L", idx.size, idx.tobytes()).getbbox()


def is_blank(img: "Image.Image") -> bool:
    return ink_bbox(img) is None


def glyph_width(img: "Image.Image", spec: FontSpec = FontSpec()) -> int:
    """Advance width for a glyph: its inked extent plus right padding.

    Keep ``pad_right`` at 0 for Arabic unless you have a reason not to: a
    connecting script wants adjacent glyphs to butt together so the cursive
    stroke of one meets the next, and every padding column is a visible break
    in the join.
    """
    bbox = ink_bbox(img)
    if bbox is None:
        return max(2, spec.cell_w // 4)     # a space-like blank
    return min(spec.cell_w, (bbox[2] - bbox[0]) + spec.pad_right)


def width_table(
    glyphs: dict[int, "Image.Image"],
    byte_of: dict[int, int],
    spec: FontSpec = FontSpec(),
) -> dict[int, int]:
    """byte value -> pixel advance, for line measurement and alignment."""
    return {
        byte_of[cp]: glyph_width(img, spec)
        for cp, img in glyphs.items()
        if cp in byte_of
    }


def bitmap_equivalence(
    glyphs: dict[int, "Image.Image"]
) -> Callable[[int, int], bool]:
    """Equality test for the allocator, based on rendered pixels.

    At Gen 3 glyph sizes some isolated and final forms rasterise identically.
    Where that happens they can share one byte, which buys back slots for free
    -- but only decide it from pixels, never from an assumption about the
    script.
    """
    keys: dict[int, bytes] = {}
    for cp, img in glyphs.items():
        keys[cp] = img.convert("P").tobytes()

    def same(a: int, b: int) -> bool:
        ka, kb = keys.get(a), keys.get(b)
        return ka is not None and ka == kb

    return same


def preview(text_bytes: bytes, glyphs: dict[int, "Image.Image"],
            byte_of: dict[int, int], spec: FontSpec = FontSpec(),
            scale: int = 2) -> "Image.Image":
    """Render an encoded line exactly as the engine would draw it.

    The cheapest way to catch a reordering or shaping mistake without booting
    an emulator: if this image reads correctly right-to-left, the byte stream
    is right.

    Bytes that map to the ROM's own glyphs rather than this sheet -- Latin
    letters, digits, punctuation the allocator marked as reused -- come out as
    blank space, because their artwork lives in the ROM and not here.
    """
    _require_pil()
    from . import charmap as cm

    cp_of = {b: cp for cp, b in byte_of.items()}
    cells: list[Image.Image | None] = []
    i = 0
    while i < len(text_bytes):
        b = text_bytes[i]
        if b == cm.EOS:
            break
        if b in (cm.SPECIAL, cm.PLACEHOLDER):
            i += 2
            continue
        if b in cm.CONTROL_BYTES:
            i += 1
            continue
        cells.append(glyphs.get(cp_of.get(b, -1)))
        i += 1

    widths = [glyph_width(c, spec) if c is not None else spec.cell_w // 3 for c in cells]
    out = Image.new("P", (max(1, sum(widths)), spec.cell_h), 0)
    out.putpalette(_palette())
    x = 0
    for cell, w in zip(cells, widths):
        if cell is not None:
            out.paste(cell.crop((0, 0, w, spec.cell_h)), (x, 0))
        x += w
    if scale > 1:
        out = out.resize((out.width * scale, out.height * scale), Image.NEAREST)
    return out


def iter_cells(spec: FontSpec, count: int) -> Iterable[tuple[int, int]]:
    for i in range(count):
        yield (i % spec.columns) * spec.cell_w, (i // spec.columns) * spec.cell_h
