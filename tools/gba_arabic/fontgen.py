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
    size: int = 11          # point size to rasterise at (autofit re-derives this)
    baseline: int = 11      # pixels from cell top to the text baseline
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


# Gen 3 sheet convention, read off pokefirered's latin_normal.png:
#   0 = transparent, 1 = glyph ink, 2 = shadow, 3 = the background box drawn
#   behind the glyph, spanning (advance width) x (BOX_HEIGHT) pixels.
BG, INK, SHADOW, BOX = 0, 1, 2, 3
INK_INDICES = frozenset({INK, SHADOW})
INK_ONLY = frozenset({INK})
BOX_HEIGHT = 14         # gGlyphInfo.height in src/text.c


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
    # The strip is deliberately much taller than the cell, with the pen
    # baseline far from either edge: rasterising straight into a cell-sized
    # image hard-clips deep descenders -- the tails of ع ى و were being cut
    # off at the strip boundary before they ever reached the cell.
    strip_h = spec.cell_h * 3
    y0 = spec.cell_h * 2
    for cp in codepoints:
        # Draw onto an oversized strip first, then slide the ink flush against
        # the cell's left edge. Glyphs are drawn left-to-right by the engine
        # over pre-reversed text, exactly like Latin ones, and it advances by
        # the width table -- so ink must start at x=0 plus the form's bearing.
        # Drawing straight into the cell would clip forms whose outline starts
        # left of the pen position, which several Arabic finals do.
        strip = Image.new("L", (spec.cell_w * 3, strip_h), 0)
        ImageDraw.Draw(strip).text(
            (spec.cell_w, y0), chr(cp), fill=255, font=font, anchor="ls"
        )
        # Threshold before measuring, not after: a column of antialiasing too
        # faint to survive quantisation would otherwise set the crop origin and
        # then vanish, leaving the glyph inset from the cell edge by a pixel.
        quantised = _quantise(strip, spec)
        cell = blank(spec)
        bbox = ink_bbox(quantised)
        if bbox is not None:
            # Inset the ink by the glyph's left bearing: joining sides stay
            # flush so cursive strokes meet, non-joining sides keep a spacing
            # column so narrow letters (alef!) are not swallowed by neighbours.
            lb = visual_bearings(cp)[0]
            right = min(bbox[2], bbox[0] + spec.cell_w - lb)
            # Vertical: strip row (y0 + d) lands on cell row (baseline + d),
            # so the baseline position in the cell is spec.baseline exactly.
            top = y0 - spec.baseline
            cell.paste(quantised.crop((bbox[0], top, right, top + spec.cell_h)),
                       (lb, 0))
            add_shadow(cell)
        out[cp] = cell
    return out


def measure_extents(
    codepoints: Sequence[int],
    font_path: str,
    spec: FontSpec = FontSpec(),
) -> tuple[int, int, int | None, int | None]:
    """(ascent, descent, tallest glyph, deepest glyph) after quantisation.

    Ascent counts rows above the baseline, descent rows at and below it.
    Measured on the thresholded bitmap, because that is what actually lands in
    the cell -- outline metrics overstate what survives quantisation.
    """
    _require_pil()
    font = ImageFont.truetype(font_path, spec.size)
    strip_h = spec.cell_h * 3
    y0 = spec.cell_h * 2
    ascent = descent = 0
    tallest = deepest = None
    for cp in codepoints:
        strip = Image.new("L", (spec.cell_w * 3, strip_h), 0)
        ImageDraw.Draw(strip).text(
            (spec.cell_w, y0), chr(cp), fill=255, font=font, anchor="ls"
        )
        bbox = ink_bbox(_quantise(strip, spec))
        if bbox is None:
            continue
        a, d = y0 - bbox[1], bbox[3] - y0
        if a > ascent:
            ascent, tallest = a, cp
        if d > descent:
            descent, deepest = d, cp
    return ascent, descent, tallest, deepest


def autofit(
    codepoints: Sequence[int],
    font_path: str,
    spec: FontSpec = FontSpec(),
    box_h: int = BOX_HEIGHT,
    min_size: int = 8,
) -> tuple[FontSpec, dict]:
    """Pick the largest size whose glyphs fit the engine's glyph box.

    The engine draws only ``box_h`` rows of each cell (gGlyphInfo.height = 14
    in FireRed), so ink outside rows 0..box_h-1 silently vanishes in game.
    This walks sizes downward until ascent + descent fits, then bottom-aligns:
    ``baseline = box_h - descent`` puts the deepest tail on the box's last row,
    which is where Latin descenders sit too, so mixed Arabic/Latin lines share
    a baseline.

    Not every face fits at a comfortable size -- DejaVu Sans fits FireRed's
    box at 11pt, Noto Naskh Arabic only at 9pt -- and if nothing fits by
    ``min_size`` the least-clipping size is returned with ``fit: False`` so
    the caller can warn.
    """
    from dataclasses import replace

    best: tuple[int, int, int, int | None, int | None] | None = None
    # Scan from well above the profile's size: the spec is a starting point,
    # not a cap, and a compact face may fit the box at a larger size.
    for size in range(max(spec.size, 16), min_size - 1, -1):
        trial = replace(spec, size=size)
        asc, desc, tallest, deepest = measure_extents(codepoints, font_path, trial)
        if asc + desc <= box_h:
            fitted = replace(trial, baseline=box_h - desc)
            return fitted, {
                "fit": True, "size": size, "baseline": fitted.baseline,
                "ascent": asc, "descent": desc,
                "tallest": tallest, "deepest": deepest, "box": box_h,
            }
        if best is None or asc + desc < best[1] + best[2]:
            best = (size, asc, desc, tallest, deepest)

    size, asc, desc, tallest, deepest = best      # type: ignore[misc]
    fallback = replace(spec, size=size, baseline=max(0, box_h - desc))
    return fallback, {
        "fit": False, "size": size, "baseline": fallback.baseline,
        "ascent": asc, "descent": desc,
        "tallest": tallest, "deepest": deepest, "box": box_h,
        "clipped_rows": asc + desc - box_h,
    }


def _quantise(img: "Image.Image", spec: FontSpec) -> "Image.Image":
    """Grayscale coverage -> binary ink (palette index 1).

    Deliberately binary, NOT three-level. Mapping medium-coverage pixels to the
    shadow index looked reasonable on paper but was wrong in game: the engine's
    palette renders index 2 as a light grey, so every antialiasing fringe
    became a grey halo and small marks -- the dots that distinguish ب ت ث ن ي
    -- came out faint or vanished ("the shadow eats the letters"). The original
    font never does this: its glyphs are solid ink plus a clean offset drop
    shadow, which ``add_shadow`` reproduces.
    """
    px = img.load()
    dst = Image.new("P", img.size, 0)
    dpx = dst.load()
    for y in range(img.height):
        for x in range(img.width):
            dpx[x, y] = INK if px[x, y] >= spec.edge_at else BG
    dst.putpalette(_palette())
    return dst


def add_shadow(cell: "Image.Image") -> "Image.Image":
    """Add the (+1, +1) drop shadow the original Gen 3 fonts carry.

    Shadow pixels are only placed where there is no ink, and advance widths are
    measured on ink alone -- so on a joining edge the shadow column falls
    beyond the advance and is clipped by the engine's blit, keeping cursive
    seams closed, while on a non-joining edge it lands inside the spacing
    column where it is visible, exactly like the Latin glyphs' shadows.
    """
    px = cell.load()
    w, h = cell.size
    for y in range(h - 1, -1, -1):
        for x in range(w - 1, -1, -1):
            if px[x, y] == INK:
                sx, sy = x + 1, y + 1
                if sx < w and sy < h and px[sx, sy] == BG:
                    px[sx, sy] = SHADOW
    return cell


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


def ink_bbox(img: "Image.Image",
             ink: frozenset[int] = INK_INDICES) -> tuple[int, int, int, int] | None:
    """Bounding box of the glyph's ink.

    Measured on palette *indices*, deliberately: converting to "L" first would
    resolve indices to colours, and a non-black background would then measure
    as full-cell ink.

    Only ink and shadow count. Index 3 -- the white box the real sheets draw
    behind every glyph, including the space -- is background, not ink, so
    including it would make every cell look occupied.
    """
    _require_pil()
    idx = img.convert("P")
    mask = bytes(0xFF if v in ink else 0 for v in idx.tobytes())
    return Image.frombytes("L", idx.size, mask).getbbox()


def is_blank(img: "Image.Image", ink: frozenset[int] = INK_INDICES) -> bool:
    return ink_bbox(img, ink) is None


def to_sheet_cell(
    glyph: "Image.Image",
    width: int,
    spec: FontSpec = FontSpec(),
    box_height: int = BOX_HEIGHT,
) -> "Image.Image":
    """Rewrite a rendered glyph into the decomp sheet's index convention.

    Rendering produces 0/1/2 with 0 as background. The sheets instead expect
    the glyph's advance box to be filled with index 3 and only the area outside
    it left as index 0, which is what the engine's palette expects. Writing
    index 0 where index 3 belongs loses the glyph's background and can show
    through as transparent in-game.
    """
    _require_pil()
    out = blank(spec)
    src, dst = glyph.convert("P").load(), out.load()
    w = min(width, spec.cell_w)
    h = min(box_height, spec.cell_h)
    for y in range(spec.cell_h):
        for x in range(spec.cell_w):
            v = src[x, y]
            if v in INK_INDICES:
                dst[x, y] = v
            elif x < w and y < h:
                dst[x, y] = BOX
            else:
                dst[x, y] = BG
    return out


def visual_bearings(cp: int) -> tuple[int, int]:
    """(left, right) spacing columns for a glyph, in drawn coordinates.

    A single per-glyph advance cannot distinguish a joining edge from a
    non-joining one, so the spacing is decided by the glyph's *form*. Text is
    drawn pre-reversed, so a letter's forward-join side (toward the next
    logical letter) is its visual LEFT, and its backward-join side its visual
    RIGHT:

      medial    joins both sides            -> no gaps: strokes must butt
      final     joins backward (right)      -> gap on the left only
      initial   joins forward (left)        -> gap on the right only
      isolated  joins nothing               -> gap on both sides

    Without this, a narrow non-joining letter -- alef is a 1px vertical stroke
    at this size -- sits flush against its neighbours and is swallowed by them.
    """
    from . import shaping

    form = shaping.form_of(cp)
    if form == shaping.MEDIAL:
        return (0, 0)
    if form == shaping.FINAL:
        return (1, 0)
    if form == shaping.INITIAL:
        return (0, 1)
    return (1, 1)       # isolated, and any non-form glyph (Arabic punctuation)


def glyph_width(img: "Image.Image", spec: FontSpec = FontSpec(),
                cp: int | None = None) -> int:
    """Advance width for a glyph: left bearing + ink + right bearing.

    ``render`` positions the ink ``visual_bearings(cp)[0]`` columns in from the
    left edge, so the ink bbox's right extent already includes the left gap;
    only the right bearing is added here. Without ``cp`` the ``pad_right``
    fallback applies -- fine for Latin, wrong for Arabic, so pass it whenever
    the glyph is a shaped form.

    Measured on INK only, excluding the drop shadow: the shadow belongs inside
    the spacing column on non-joining edges and must be clipped on joining
    ones, so it never widens the advance.
    """
    bbox = ink_bbox(img, INK_ONLY)
    if bbox is None:
        bbox = ink_bbox(img)                # shadow-only cell, or truly blank
        if bbox is None:
            return max(2, spec.cell_w // 4)
    right = visual_bearings(cp)[1] if cp is not None else spec.pad_right
    return min(spec.cell_w, bbox[2] + right)


def width_table(
    glyphs: dict[int, "Image.Image"],
    byte_of: dict[int, int],
    spec: FontSpec = FontSpec(),
) -> dict[int, int]:
    """byte value -> pixel advance, for line measurement and alignment."""
    return {
        byte_of[cp]: glyph_width(img, spec, cp)
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
    cells: list[tuple["Image.Image | None", int | None]] = []
    i = 0
    while i < len(text_bytes):
        b = text_bytes[i]
        if b == cm.EOS:
            break
        if b == cm.SPECIAL and i + 1 < len(text_bytes):
            i += 1 + cm.ext_ctrl_code_length(text_bytes[i + 1])
            continue
        if b == cm.PLACEHOLDER:
            i += 2
            continue
        if b in cm.CONTROL_BYTES:
            i += 1
            continue
        cp = cp_of.get(b)
        cells.append((glyphs.get(cp) if cp is not None else None, cp))
        i += 1

    widths = [glyph_width(c, spec, cp) if c is not None else spec.cell_w // 3
              for c, cp in cells]
    cells = [c for c, _ in cells]
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
