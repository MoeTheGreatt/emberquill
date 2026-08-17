# Arabic localisation toolchain for Gen 3 Pokémon (GBA)

Turns logical Arabic into the byte stream a Game Boy Advance Pokémon game can
actually draw.

## Why this needs a toolchain at all

The Gen 3 text engine does three things that make Arabic hard:

1. **One byte selects one fixed glyph.** There is no font shaper in the ROM, so
   nothing decides at runtime that a letter is word-initial or word-medial.
2. **Bytes are drawn strictly left to right.** There is no bidi support.
3. **There are only 256 byte values**, ~20 of them reserved for control codes.

Arabic needs all three of those things to be different. It is cursive, so every
letter has up to four contextual forms; it runs right to left, but embedded
Latin and numbers still run left to right; and fully shaped Arabic wants roughly
130 distinct glyphs, which does not comfortably fit alongside the Latin set.

The fix is to move all of that work to build time:

```
logical Arabic  →  shaping  →  visual reordering  →  encoding  →  bytes
                      │             │                   │
              presentation      RTL, with LTR      glyph slots taken
                  forms         runs preserved     from the free pool
```

Order matters and is easy to get wrong. Shaping must precede reordering (a
letter's form depends on its *logical* neighbours, not its drawn position), and
reordering must precede encoding (the charmap maps drawable glyphs, and only
shaped text has those).

## Install

```bash
pip install pillow          # only needed for the font commands
python -m tools.gba_arabic doctor
```

The text pipeline — shaping, reordering, allocation, encoding — has no
dependencies. Pillow is only used to build font sheets.

## Workflow

```bash
# 1. Inspect one string. Fastest way to sanity-check shaping and ordering.
python -m tools.gba_arabic shape "مرحبا بك في عالم بوكيمون"

# 2. See what your script costs in glyph slots.
python -m tools.gba_arabic scan script.pks

# 3. Assign byte values to glyphs.
python -m tools.gba_arabic alloc script.pks -o glyphmap.json

# 4. Build the font sheet, raw tiles and width table.
python -m tools.gba_arabic font -m glyphmap.json -f NotoNaskhArabic.ttf \
    -o font_arabic.png --tiles font_arabic.4bpp --widths widths.json

# 5. Hand-edit font_arabic.png in a pixel editor, then re-import it.
python -m tools.gba_arabic font -m glyphmap.json --edit font_arabic.png \
    -o font_arabic.png --tiles font_arabic.4bpp --widths widths.json

# 6. Encode the script.
python -m tools.gba_arabic encode script.pks -m glyphmap.json -o text.json

# 7. Check a line renders correctly without booting an emulator.
python -m tools.gba_arabic preview "مرحبا!" -m glyphmap.json \
    -f NotoNaskhArabic.ttf -o preview.png
```

`preview` is the highest-value step in that list. If the PNG reads correctly
right to left, the byte stream is correct.

## Script format

`.pks` — readable, keys preserved, multi-line entries need no escaping:

```
[MSG_INTRO]
مرحبا! أنا البروفيسور بيرش.\pهذا العالم مليء بمخلوقات\nتُسمى بوكيمون!
```

`.json` (`{"KEY": "text"}`) and `.txt` (one entry per line) also work.

Markup, in every format:

| Markup | Meaning |
|---|---|
| `\n` | next line, same box |
| `\l` | scroll up one line and continue |
| `\p` | wait for input, clear the box, continue |
| `{PLAYER}`, `{STR_VAR_1}` | runtime placeholder, from the profile |
| `{FC 01 02}` | raw bytes, for control codes with arguments |
| `{{` | a literal `{` |

## Glyph budget

This is the binding constraint on the project. Run `scan` early.

Fully shaped Arabic is ~130 glyphs: 22 dual-joining letters × 4 forms, 12
right-joining × 2, hamza forms, teh marbuta, alef maksura, the four mandatory
lam-alef ligatures, and Arabic punctuation. The default free pool
(`0x01–0x77`, the accented-Latin and symbol region) holds ~119 usable slots
with the Latin alphabet kept.

Levers, cheapest first:

- The allocator only spends a slot on a form your script **actually uses**, so a
  partial translation costs far less than 130.
- `--dedupe` (needs `--font`) shares one byte between forms that rasterise
  identically. At a 16px cell several isolated and final forms genuinely do.
  It compares rendered pixels rather than assuming.
- `--no-keep-latin` reclaims ~62 more slots. Only safe once no Latin text
  survives — Pokémon names and version strings often stay Latin.
- `--free-ranges` widens the pool, once you have **verified** the bytes are
  spare in your target.

Overflow is reported, never silently truncated.

## Verify before you build

The built-in charmap covers the international (English) Gen 3 table, and the
per-game defaults in `profiles.py` — free byte ranges, placeholder IDs, font
cell size — are **defaults, not verified constants**. Hacks move glyphs,
repoint text and change font sheets. FireRed's placeholder IDs differ from
Emerald's.

So, before trusting a build:

```bash
python -m tools.gba_arabic verify path/to/charmap.txt   # decomp charmap
python -m tools.gba_arabic verify path/to/table.tbl     # or a .tbl
```

`verify` diffs the real table against the built-in one and lists bytes with no
glyph assigned — your candidates for `--free-ranges`. Pass `--charmap` to every
command to use the real table instead of the built-in one.

## Which target to pick

Strongly prefer a **decomp** (`pokeemerald`, `pokefirered`, `pokeruby`) over
patching a ROM binary. In a decomp you edit string sources and rebuild, which
means no repointing, no fixed-length text regions, and the charmap and font
sheets are files you can read rather than offsets you have to guess. The font
sheet is a PNG the repo's own graphics converter turns into tiles — which is
why `font` emits PNG as its primary output.

Patching a raw ROM works, but Gen 3 text lives in fixed regions: an Arabic
string that encodes longer than the English one it replaces needs the pointer
table updated too. That part is not implemented here.

## Right alignment

The engine draws from the left edge of the box, so a reversed Arabic line sits
flush left, which reads as broken. Two options:

- `pipeline.encode(..., align_right=<box width px>, widths=<byte→px>)` pads
  each line with leading spaces. Works on any target, costs a few bytes per
  line.
- On a decomp, change the alignment in the engine instead. Cleaner.

## Known limits

- **Diacritics are dropped** by default (`drop_marks`). A one-byte-per-glyph
  engine cannot stack a mark over a base letter. Undiacritised text is normal
  for Arabic game translations.
- **Bidi is single-level.** One base direction per line, no explicit embedding
  controls. Fine for dialogue; not a general bidi implementation.
- **Auto-rendered fonts need hand work.** Arabic at ~10px loses the curve and
  dot detail that distinguishes letters. Expect to pixel the frequent forms by
  hand; `--edit` round-trips your work. A flat, geometric face downsamples
  better than Naskh — DejaVu Sans beat Noto Naskh Arabic at a 16px cell in
  side-by-side tests.
- **No ROM repointing.** Decomp path only for length-increasing text.
- **`preview` blanks reused glyphs.** Latin letters, digits and punctuation the
  allocator marked as reused live in the ROM's own font, not in your sheet, so
  they render as gaps.

## Tests

```bash
python -m pytest tools/tests/ -q
```

Font tests need a face with Presentation Forms-B coverage; they skip if none is
found. Point them at one with `GBA_ARABIC_TEST_FONT=/path/to/font.ttf`.
