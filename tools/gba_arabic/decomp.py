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

# latin_normal drives TWO width arrays: FONT_NORMAL_COPY_1 has its own table
# (used by the quest log and several battle panels) that reads the same
# glyph sheet -- leaving it unpatched drew Arabic with the old accented-Latin
# widths, scattering glyphs across those screens.
WIDTH_ARRAYS = {
    "latin_normal": ("sFontNormalLatinGlyphWidths",
                     "sFontNormalCopy1LatinGlyphWidths"),
    "latin_small": ("sFontSmallLatinGlyphWidths",),
    "latin_male": ("sFontMaleLatinGlyphWidths",),
    "latin_female": ("sFontFemaleLatinGlyphWidths",),
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
        # not bleed into the Arabic one. The codepoint matters: the advance
        # (and so the background box) includes form-aware side bearings.
        prepared = to_sheet_cell(cell, glyph_width(cell, sheet.spec, codepoint),
                                 sheet.spec)
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


# -- right-to-left text printer --------------------------------------------

# A previously-unused extended control code. 0x00-0x18 are taken by the
# engine; GetStringWidth ignores unknown codes after consuming the code byte,
# and GetExtCtrlCodeLength is extended below so string utilities skip it too.
RTL_CTRL_CODE = 0x19


class RtlPatchError(RuntimeError):
    pass


def _edit(path: Path, old: str, new: str, *, count: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    found = text.count(old)
    if found != count:
        raise RtlPatchError(
            f"{path.name}: expected {count} match(es) for anchor, found {found} "
            f"-- decomp code differs from what this patch expects:\n  {old[:80]!r}"
        )
    path.write_text(text.replace(old, new), encoding="utf-8")


def enable_rtl_printer(repo: str | Path) -> list[str]:
    """Teach the engine to draw right-to-left, one line at a time.

    The typewriter effect prints byte-by-byte at increasing x, which reveals a
    (pre-reversed) Arabic line end-first. The fix: a new control code, FC 19,
    that flips the printer for the rest of the string -- each glyph's x is
    DECREMENTED by its width before drawing, starting from the window's right
    edge. Strings printed this way must be stored MIRRORED (reversed visual
    order, i.e. logical order for pure Arabic), which ``decompsrc.convert``
    emits when the string qualifies; everything without the code -- all the
    untranslated English -- renders exactly as before. Right alignment falls
    out for free.

    Idempotent: raises RtlPatchError if anchors are missing, silently skips
    files already patched.
    """
    repo = Path(repo)
    done: list[str] = []
    text_c = repo / "src" / "text.c"
    text_h = repo / "include" / "text.h"
    chars_h = repo / "include" / "characters.h"
    strutil = repo / "src" / "string_util.c"
    charmap = repo / "charmap.txt"

    if "EXT_CTRL_CODE_RTL" in text_c.read_text(encoding="utf-8"):
        return ["already patched"]

    # The flag lives in three bits the engine defines but never reads.
    _edit(text_h, "u8 font_type_5:3;", "u8 rtl:1;\n    u8 font_type_5:2;")
    done.append("text.h: rtl flag bit")

    _edit(chars_h,
          "#define EXT_CTRL_CODE_RESUME_MUSIC",
          "#define EXT_CTRL_CODE_RTL                    0x19\n"
          "#define EXT_CTRL_CODE_RESUME_MUSIC")
    done.append("characters.h: EXT_CTRL_CODE_RTL")

    src = text_c.read_text(encoding="utf-8")

    # Helper: the x where an RTL line begins (the window's right edge, with
    # the same margin the left edge gets).
    anchor = "u16 RenderText(struct TextPrinter *textPrinter)"
    if anchor not in src:
        raise RtlPatchError("text.c: RenderText signature not found")
    helper = (
        "static s32 RtlLineStartX(struct TextPrinter *textPrinter)\n"
        "{\n"
        "    return gWindows[textPrinter->printerTemplate.windowId].window.width * 8\n"
        "         - textPrinter->printerTemplate.x;\n"
        "}\n\n"
    )
    _edit(text_c, anchor, helper + anchor)
    done.append("text.c: RtlLineStartX helper")

    # The control code itself: set the flag, jump to the right edge. The
    # anchor includes SHIFT_RIGHT's body line, which only RenderText's copy
    # of the switch has -- GetStringWidth's fallthrough versions do not.
    shift_right_in_render = (
        "            case EXT_CTRL_CODE_SHIFT_RIGHT:\n"
        "                textPrinter->printerTemplate.currentX = "
        "textPrinter->printerTemplate.x + *textPrinter->printerTemplate.currentChar;"
    )
    _edit(text_c, shift_right_in_render,
          "            case EXT_CTRL_CODE_RTL:\n"
          "                subStruct->rtl = TRUE;\n"
          "                textPrinter->printerTemplate.currentX = RtlLineStartX(textPrinter);\n"
          "                return RENDER_REPEAT;\n" + shift_right_in_render)
    done.append("text.c: control code handler")

    # Every line start -- newline, box clear, scroll -- begins at the right
    # edge when the flag is up. Each site is anchored by its preceding line.
    reset = "textPrinter->printerTemplate.currentX = textPrinter->printerTemplate.x;"
    rtl_reset = ("textPrinter->printerTemplate.currentX = textPrinter->subUnion.sub.rtl\n"
                 "                ? RtlLineStartX(textPrinter) : textPrinter->printerTemplate.x;")
    for lead in (
        "        case CHAR_NEWLINE:\n            ",
        "FillWindowPixelBuffer(textPrinter->printerTemplate.windowId, "
        "PIXEL_FILL(textPrinter->printerTemplate.bgColor));\n            ",
        "textPrinter->scrollDistance = gFonts[textPrinter->printerTemplate.fontId]"
        ".maxLetterHeight + textPrinter->printerTemplate.lineSpacing;\n            ",
    ):
        _edit(text_c, lead + reset, lead + rtl_reset)
    done.append("text.c: line starts at right edge (x3)")

    # The draw step: step left by the glyph's width, then draw.
    _edit(text_c,
          "        CopyGlyphToWindow(textPrinter);\n\n"
          "        if (textPrinter->minLetterSpacing)",
          "        if (subStruct->rtl && !textPrinter->japanese)\n"
          "        {\n"
          "            textPrinter->printerTemplate.currentX -= gGlyphInfo.width;\n"
          "            // currentX is u8: a line wider than the window would\n"
          "            // wrap negative and every remaining glyph would vanish.\n"
          "            // Clamp so overflow degrades to left-flush, not blank.\n"
          "            if (textPrinter->printerTemplate.currentX > 240)\n"
          "                textPrinter->printerTemplate.currentX = 0;\n"
          "            CopyGlyphToWindow(textPrinter);\n"
          "            return RENDER_PRINT;\n"
          "        }\n\n"
          "        CopyGlyphToWindow(textPrinter);\n\n"
          "        if (textPrinter->minLetterSpacing)")
    done.append("text.c: RTL draw step")

    # The waiting cursor. Both the arrow draw and its clear paint a 10x12
    # rect at currentX -- which in RTL is the LEFT edge of the last glyph, so
    # the fill erased the sentence's final word before stamping the arrow on
    # top of it. In RTL the arrow belongs 10px to the left of the pen instead.
    # The blit is edited FIRST: its 0x80/0x10 prefix is unique, and rewriting
    # its currentX line is what disambiguates the fill's identical 4-line tail.
    blit_x = ("                0x80,\n"
              "                0x10,\n"
              "                textPrinter->printerTemplate.currentX,")
    _edit(text_c, blit_x,
          "                0x80,\n"
          "                0x10,\n"
          "                textPrinter->subUnion.sub.rtl\n"
          "                    ? textPrinter->printerTemplate.currentX - 10\n"
          "                    : textPrinter->printerTemplate.currentX,")
    done.append("text.c: down-arrow blit position")

    arrow_x = ("                textPrinter->printerTemplate.currentX,\n"
               "                textPrinter->printerTemplate.currentY,\n"
               "                10,\n"
               "                12);")
    rtl_arrow_x = ("                textPrinter->subUnion.sub.rtl\n"
                   "                    ? textPrinter->printerTemplate.currentX - 10\n"
                   "                    : textPrinter->printerTemplate.currentX,\n"
                   "                textPrinter->printerTemplate.currentY,\n"
                   "                10,\n"
                   "                12);")
    _edit(text_c, arrow_x, rtl_arrow_x)          # fill in TextPrinterDrawDownArrow
    done.append("text.c: down-arrow fill position")

    clear_x = ("        textPrinter->printerTemplate.currentX,\n"
               "        textPrinter->printerTemplate.currentY,\n"
               "        10,\n"
               "        12);")
    _edit(text_c, clear_x,
          "        textPrinter->subUnion.sub.rtl\n"
          "            ? textPrinter->printerTemplate.currentX - 10\n"
          "            : textPrinter->printerTemplate.currentX,\n"
          "        textPrinter->printerTemplate.currentY,\n"
          "        10,\n"
          "        12);")
    done.append("text.c: down-arrow clear position")

    # String utilities must know the code is one byte long.
    st = strutil.read_text(encoding="utf-8")
    m = re.search(r"(GetExtCtrlCodeLength.*?lengths\[\]\s*=\s*\{)(.*?)(\};)", st, re.S)
    if not m:
        raise RtlPatchError("string_util.c: lengths table not found")
    entries = re.findall(r"\d+", m.group(2))
    if len(entries) != RTL_CTRL_CODE:
        raise RtlPatchError(
            f"string_util.c: lengths table has {len(entries)} entries, "
            f"expected {RTL_CTRL_CODE}")
    st = st[:m.end(2)] + "    1,\n    " + st[m.end(2):]
    strutil.write_text(st, encoding="utf-8")
    done.append("string_util.c: code length = 1")

    cm_text = charmap.read_text(encoding="utf-8")
    if "RTL = FC 19" not in cm_text:
        charmap.write_text(cm_text + "\nRTL = FC 19\n", encoding="utf-8")
    done.append("charmap.txt: RTL = FC 19")

    return done


_BOX_EXP_FN = '''
// Every boxed Pokemon shares the battle's EXP.
//
// A boxed Pokemon stores no level and no stats: its level is DERIVED from EXP
// on every read (GetLevelFromBoxMonExp), and BoxMonToMon recomputes stats and
// full HP through CalculateMonStats when it is withdrawn. So adding EXP is the
// whole job -- it comes out of the box at the right level with right stats.
//
// The skips are all load-bearing:
//   * Eggs -- an egg's "friendship" field IS its hatch counter, and
//     CreatedHatchedMon copies an egg's moves into the hatched Pokemon, so an
//     egg that gained levels would hatch with a scrambled timer and moves it
//     must not have.
//   * Empty slots -- writing EXP would leave a half-initialised entry.
//   * Bad Eggs -- SetBoxMonData recomputes the checksum, which would launder
//     a corrupt entry into one the game treats as real.
void GiveExpToBoxedPokemon(u32 amount)
{
    u32 box, slot;

    if (amount == 0)
        return;

    for (box = 0; box < TOTAL_BOXES_COUNT; box++)
    {
        for (slot = 0; slot < IN_BOX_COUNT; slot++)
        {
            struct BoxPokemon *boxMon = &gPokemonStoragePtr->boxes[box][slot];
            u16 species = GetBoxMonData(boxMon, MON_DATA_SPECIES, NULL);
            u32 exp, maxExp;
            u8 oldLevel, newLevel;
            s32 i;

            if (species == SPECIES_NONE)
                continue;
            if (GetBoxMonData(boxMon, MON_DATA_IS_EGG, NULL))
                continue;
            if (GetBoxMonData(boxMon, MON_DATA_SANITY_IS_BAD_EGG, NULL))
                continue;

            oldLevel = GetLevelFromBoxMonExp(boxMon);
            if (oldLevel >= MAX_LEVEL)
                continue;

            // Clamping is mandatory: unclamped EXP pins the level calculation
            // at 100 forever and makes the gained-EXP numbers meaningless.
            maxExp = gExperienceTables[gSpeciesInfo[species].growthRate][MAX_LEVEL];
            exp = GetBoxMonData(boxMon, MON_DATA_EXP, NULL) + amount;
            if (exp > maxExp)
                exp = maxExp;
            SetBoxMonData(boxMon, MON_DATA_EXP, &exp);

            // Teach the level-up moves it passed, but only into FREE slots:
            // GiveMoveToBoxMon fills an empty slot, reports a duplicate, and
            // reports a full moveset WITHOUT overwriting -- so a moveset the
            // player chose is never touched. Its delete-the-first-move sibling
            // is deliberately not used here.
            newLevel = GetLevelFromBoxMonExp(boxMon);
            for (i = 0; gLevelUpLearnsets[species][i] != LEVEL_UP_END; i++)
            {
                u8 moveLevel = (gLevelUpLearnsets[species][i] & LEVEL_UP_MOVE_LV) >> 9;

                if (moveLevel > oldLevel && moveLevel <= newLevel)
                    GiveMoveToBoxMon(boxMon,
                                     gLevelUpLearnsets[species][i] & LEVEL_UP_MOVE_ID);
            }
        }
    }
}

'''


def enable_shared_exp(repo: str | Path) -> list[str]:
    """Share every battle's EXP with the whole party and every boxed Pokemon.

    The engine already awards EXP to a non-participating party member if it
    holds an Exp. Share, looping all six slots in ``Cmd_getexp``. This widens
    that gate to every party member and gives the undivided amount, so the
    existing state machine still drives level-ups, stat recalculation, move
    learning, evolution and the level-up box -- nothing new to reimplement.

    Boxed Pokemon have no such state machine, so the battle's total is banked
    and applied once at battle end (not per faint: 420 slots of decrypt and
    checksum inside a battle frame would hitch).

    Eggs are excluded everywhere. ``Cmd_getexp`` has no egg guard of its own --
    vanilla is safe only because an egg can hold no Exp. Share and is never
    sent in, which is exactly the gate being widened here.

    Idempotent: raises RtlPatchError if anchors are missing, skips if applied.
    """
    repo = Path(repo)
    done: list[str] = []
    pokemon_c = repo / "src" / "pokemon.c"
    pokemon_h = repo / "include" / "pokemon.h"
    battle_main = repo / "src" / "battle_main.c"
    battle_cmds = repo / "src" / "battle_script_commands.c"

    if "GiveExpToBoxedPokemon" in pokemon_c.read_text(encoding="utf-8"):
        return ["already patched"]

    # The box walker itself, plus the bank it draws on. Both live in pokemon.c,
    # which already has the storage system, the experience tables, the level-up
    # learnsets and the (static) GiveMoveToBoxMon.
    _edit(pokemon_c, "u16 GiveMoveToMon(struct Pokemon *mon, u16 move)",
          "u32 gSharedExpFromBattle;\n" + _BOX_EXP_FN
          + "u16 GiveMoveToMon(struct Pokemon *mon, u16 move)")
    done.append("pokemon.c: GiveExpToBoxedPokemon + EXP bank")

    _edit(pokemon_h, "u16 GiveMoveToMon(struct Pokemon *mon, u16 move);",
          "u16 GiveMoveToMon(struct Pokemon *mon, u16 move);\n"
          "void GiveExpToBoxedPokemon(u32 amount);\n"
          "extern u32 gSharedExpFromBattle;")
    done.append("pokemon.h: declarations")

    # Bank the undivided amount per faint, and hand every party member the
    # full value instead of a share of it.
    _edit(battle_cmds,
          "            gBattleScripting.getexpState++;\n"
          "            gBattleStruct->expGetterMonId = 0;\n"
          "            gBattleStruct->sentInPokes = sentIn;",
          "            // Shared EXP: the undivided amount for everyone, and\n"
          "            // the same total banked for the boxes.\n"
          "            *exp = calculatedExp;\n"
          "            gExpShareExp = 0;\n"
          "            gSharedExpFromBattle += calculatedExp;\n"
          "            gBattleScripting.getexpState++;\n"
          "            gBattleStruct->expGetterMonId = 0;\n"
          "            gBattleStruct->sentInPokes = sentIn;")
    done.append("battle_script_commands.c: bank the total, undivided share")

    # Widen the participation gate to every party member -- except eggs, which
    # this gate is the only thing currently keeping EXP away from.
    _edit(battle_cmds,
          "            if (holdEffect != HOLD_EFFECT_EXP_SHARE && !(gBattleStruct->sentInPokes & 1))",
          "            if (GetMonData(&gPlayerParty[gBattleStruct->expGetterMonId], MON_DATA_IS_EGG))")
    done.append("battle_script_commands.c: all party members, never eggs")

    _edit(battle_cmds,
          "                    if (gBattleStruct->sentInPokes & 1)\n"
          "                        gBattleMoveDamage = *exp;\n"
          "                    else\n"
          "                        gBattleMoveDamage = 0;",
          "                    gBattleMoveDamage = *exp;")
    done.append("battle_script_commands.c: award regardless of participation")

    # Reset the bank per battle, and spend it once the battle is won.
    _edit(battle_main, "    gLeveledUpInBattle = 0;",
          "    gLeveledUpInBattle = 0;\n    gSharedExpFromBattle = 0;")
    done.append("battle_main.c: reset the bank")

    _edit(battle_main,
          "        ResetSpriteData();\n"
          "        if (gLeveledUpInBattle == 0 || gBattleOutcome != B_OUTCOME_WON)",
          "        ResetSpriteData();\n"
          "        if (gBattleOutcome == B_OUTCOME_WON)\n"
          "        {\n"
          "            GiveExpToBoxedPokemon(gSharedExpFromBattle);\n"
          "            gSharedExpFromBattle = 0;\n"
          "        }\n"
          "        if (gLeveledUpInBattle == 0 || gBattleOutcome != B_OUTCOME_WON)")
    done.append("battle_main.c: spend the bank on the boxes at battle end")

    return done


def enable_scrolling_options(repo: str | Path, visible_rows: int = 7) -> list[str]:
    """Make the options list scroll, so it can hold more rows than it shows.

    The list window is 96px tall and rows sit 13px apart, so row 7 ends at 94px
    -- the seven vanilla rows fill it exactly and an eighth would draw outside.
    Every position in the menu is derived from the item's index, so the fix is
    to derive them from its *screen row* (index - scrollOffset) instead, in all
    three places that draw: the labels, the values, and the hardware highlight
    band. The cursor then drags a scroll offset behind it.

    Row wrapping moves from MENUITEM_CANCEL to MENUITEM_COUNT - 1 so that rows
    appended after CANCEL are reachable.

    Idempotent: raises RtlPatchError if anchors are missing, skips if applied.
    """
    repo = Path(repo)
    done: list[str] = []
    src = repo / "src" / "option_menu.c"
    text = src.read_text(encoding="utf-8")

    if "OPTIONS_VISIBLE_ROWS" in text:
        return ["already patched"]

    # A scroll offset, and the row budget the window can actually show.
    _edit(src, "    /*0x0E*/ u16 cursorPos;",
          "    /*0x0E*/ u16 cursorPos;\n"
          "             u16 scrollOffset;")
    _edit(src, "// Menu items\nenum\n{",
          f"// The list window is 96px tall with rows 13px apart, so this many\n"
          f"// rows fit; anything beyond scrolls.\n"
          f"#define OPTIONS_VISIBLE_ROWS {visible_rows}\n\n"
          "// Menu items\nenum\n{")
    _edit(src, "static void UpdateSettingSelectionDisplay(u16 selection);",
          "static void UpdateSettingSelectionDisplay(u16 selection);\n"
          "static bool8 OptionsScrollToCursor(void);")
    done.append("option_menu.c: scroll offset + row budget")

    # Labels: draw the visible window of the list, not the whole list.
    _edit(src,
          "    FillWindowPixelBuffer(1, PIXEL_FILL(1));\n"
          "    for (i = 0; i < MENUITEM_COUNT; i++)\n"
          "    {\n"
          "        AddTextPrinterParameterized(WIN_OPTIONS, FONT_NORMAL, sOptionMenuItemsNames[i], 8, (u8)((i * (GetFontAttribute(FONT_NORMAL, FONTATTR_MAX_LETTER_HEIGHT))) + 2) - i, TEXT_SKIP_DRAW, NULL);    \n"
          "    }",
          "    FillWindowPixelBuffer(1, PIXEL_FILL(1));\n"
          "    for (i = 0; i < OPTIONS_VISIBLE_ROWS; i++)\n"
          "    {\n"
          "        u8 item = sOptionMenuPtr->scrollOffset + i;\n"
          "\n"
          "        if (item >= MENUITEM_COUNT)\n"
          "            break;\n"
          "        AddTextPrinterParameterized(WIN_OPTIONS, FONT_NORMAL, sOptionMenuItemsNames[item], 8, (u8)((i * (GetFontAttribute(FONT_NORMAL, FONTATTR_MAX_LETTER_HEIGHT))) + 2) - i, TEXT_SKIP_DRAW, NULL);\n"
          "    }")
    done.append("option_menu.c: labels follow the scroll offset")

    # Values: same, and skip entirely when the row is off screen -- otherwise
    # the erase rect would blank a row that belongs to a different item.
    _edit(src,
          "    memcpy(dst, sOptionMenuTextColor, 3);\n"
          "    x = 0x82;\n"
          "    y = ((GetFontAttribute(FONT_NORMAL, FONTATTR_MAX_LETTER_HEIGHT) - 1) * selection) + 2;",
          "    u8 row;\n"
          "\n"
          "    if (selection < sOptionMenuPtr->scrollOffset\n"
          "     || selection >= sOptionMenuPtr->scrollOffset + OPTIONS_VISIBLE_ROWS)\n"
          "        return;\n"
          "    row = selection - sOptionMenuPtr->scrollOffset;\n"
          "    memcpy(dst, sOptionMenuTextColor, 3);\n"
          "    x = 0x82;\n"
          "    y = ((GetFontAttribute(FONT_NORMAL, FONTATTR_MAX_LETTER_HEIGHT) - 1) * row) + 2;")
    done.append("option_menu.c: values follow the scroll offset")

    # The highlight band is a hardware window, positioned the same way.
    _edit(src, "    y = selection * (maxLetterHeight - 1) + 0x3A;",
          "    y = (selection - sOptionMenuPtr->scrollOffset) * (maxLetterHeight - 1) + 0x3A;")
    done.append("option_menu.c: highlight follows the scroll offset")

    # Cursor movement wraps over the whole list and drags the offset with it.
    # A scroll needs every row repainted, so it reports a distinct code.
    _edit(src,
          "        if (sOptionMenuPtr->cursorPos == MENUITEM_TEXTSPEED)\n"
          "            sOptionMenuPtr->cursorPos = MENUITEM_CANCEL;\n"
          "        else\n"
          "            sOptionMenuPtr->cursorPos = sOptionMenuPtr->cursorPos - 1;\n"
          "        return 3;        ",
          "        if (sOptionMenuPtr->cursorPos == 0)\n"
          "            sOptionMenuPtr->cursorPos = MENUITEM_COUNT - 1;\n"
          "        else\n"
          "            sOptionMenuPtr->cursorPos = sOptionMenuPtr->cursorPos - 1;\n"
          "        return OptionsScrollToCursor() ? 5 : 3;")
    _edit(src,
          "        if (sOptionMenuPtr->cursorPos == MENUITEM_CANCEL)\n"
          "            sOptionMenuPtr->cursorPos = MENUITEM_TEXTSPEED;\n"
          "        else\n"
          "            sOptionMenuPtr->cursorPos = sOptionMenuPtr->cursorPos + 1;\n"
          "        return 3;",
          "        if (sOptionMenuPtr->cursorPos == MENUITEM_COUNT - 1)\n"
          "            sOptionMenuPtr->cursorPos = 0;\n"
          "        else\n"
          "            sOptionMenuPtr->cursorPos = sOptionMenuPtr->cursorPos + 1;\n"
          "        return OptionsScrollToCursor() ? 5 : 3;")
    done.append("option_menu.c: cursor drags the scroll offset")

    _edit(src, "static u8 OptionMenu_ProcessInput(void)\n{ ",
          "// Keeps the cursor inside the visible window. TRUE when the list\n"
          "// actually scrolled, which means every row must be repainted rather\n"
          "// than just the highlight moved.\n"
          "static bool8 OptionsScrollToCursor(void)\n"
          "{\n"
          "    u16 first = sOptionMenuPtr->scrollOffset;\n"
          "\n"
          "    if (sOptionMenuPtr->cursorPos < first)\n"
          "        sOptionMenuPtr->scrollOffset = sOptionMenuPtr->cursorPos;\n"
          "    else if (sOptionMenuPtr->cursorPos >= first + OPTIONS_VISIBLE_ROWS)\n"
          "        sOptionMenuPtr->scrollOffset =\n"
          "            sOptionMenuPtr->cursorPos - (OPTIONS_VISIBLE_ROWS - 1);\n"
          "    return sOptionMenuPtr->scrollOffset != first;\n"
          "}\n\n"
          "static u8 OptionMenu_ProcessInput(void)\n{ ")
    done.append("option_menu.c: OptionsScrollToCursor")

    # Repaint the whole list when it scrolls.
    _edit(src,
          "        case 4:\n"
          "            BufferOptionMenuString(sOptionMenuPtr->cursorPos);\n"
          "            break;\n"
          "        }",
          "        case 4:\n"
          "            BufferOptionMenuString(sOptionMenuPtr->cursorPos);\n"
          "            break;\n"
          "        case 5:\n"
          "            {\n"
          "                u8 i;\n"
          "\n"
          "                LoadOptionMenuItemNames();\n"
          "                for (i = 0; i < MENUITEM_COUNT; i++)\n"
          "                    BufferOptionMenuString(i);\n"
          "                UpdateSettingSelectionDisplay(sOptionMenuPtr->cursorPos);\n"
          "            }\n"
          "            break;\n"
          "        }")
    done.append("option_menu.c: repaint on scroll")

    return done


def set_default_text_speed(repo: str | Path, speed: str = "FAST") -> str | None:
    """Make new games start on the given text speed (default the fastest).

    Only touches the new-game default in SetDefaultOptions; a player can still
    change it in the options menu, and existing saves keep their setting.
    Returns a status line, or None if the source was already on that speed.
    """
    repo = Path(repo)
    new_game = repo / "src" / "new_game.c"
    want = f"OPTIONS_TEXT_SPEED_{speed}"
    src = new_game.read_text(encoding="utf-8")
    m = re.search(r"(optionsTextSpeed\s*=\s*)OPTIONS_TEXT_SPEED_(\w+)(\s*;)", src)
    if not m:
        raise RtlPatchError("new_game.c: default optionsTextSpeed not found")
    if m.group(2) == speed:
        return None
    src = src[:m.start()] + m.group(1) + want + m.group(3) + src[m.end():]
    new_game.write_text(src, encoding="utf-8")
    return f"new_game.c: default text speed -> {speed}"


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
