# FireRed Arabic translation

The working translation of Pokémon FireRed's core strings, plus everything
needed to apply it to a `pokefirered` checkout.

## Contents

| File | What it is |
|---|---|
| `firered_ar.json` | The translation source: `{decomp symbol: logical Arabic}` with the decomp's own `{MACRO}` placeholders and `\n \l \p` breaks. **This is the file to edit.** |
| `firered_ar_strings.patch` | Generated `git diff` of the string sources + width tables against pristine `pokefirered` — apply with `git apply` if you don't want to run the tool. |
| `fonts/latin_*.png` | The four font sheets with Arabic installed at the allocated cells (generated; DejaVu Sans 11pt, autofit to the 14-row glyph box). |

## Reproducing from source

```bash
git clone https://github.com/pret/pokefirered && cd pokefirered
python -m tools.gba_arabic --profile firered translate . \
    -t tools/gba_arabic/translations/firered_ar.json \
    -f /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf
make -j$(nproc) modern
```

`translate` performs the whole pass in one deterministic run: allocates glyph
slots over the corpus, installs the font into all four Latin sheets, patches
the width arrays in `src/text.c`, and splices converted strings into the C and
assembly sources. Always run it against a **pristine** tree (`git checkout -- .`
first) so the allocation, fonts and strings stay consistent with each other.

## Coverage

~165 strings: the complete new-game intro (Oak's speech, controls guide,
naming, gender/rival dialogue), Arabic default name presets, main menu, start
menu, save flow, PC access, item pickup, and the core battle text (encounters,
send-outs, damage effectiveness, status conditions, experience/level/moves,
catching, fleeing, prize money).

Everything else — NPC dialogue, item/move/species names, Pokédex entries —
remains English. Add entries to `firered_ar.json` (key = decomp symbol) and
re-run; the tool reports any symbol it cannot find.

## How this was verified

The patched tree builds cleanly with `make modern` (arm-none-eabi-gcc), and
the resulting `pokefirered_modern.gba` was verified at the byte level:

1. Each macro-free translated string was independently re-encoded
   (shape → reorder → bytes) and located verbatim inside the built ROM.
2. The compiled font (`latin_normal.fwlatfont`, 32 KB, 2 bpp glyph-major) was
   found byte-identical in the ROM, as was the patched 512-entry width table.
3. Dialogue lines were then rendered **using only bytes extracted from the
   built ROM** — its font glyphs, its width table, its string data — and read
   as correct, connected, right-to-left Arabic.

What this does not cover: actually booting the game. The modern build is the
decomp project's supported configuration and is expected to be playable, but
byte-level verification is not a play-test — check the intro in an emulator.

## Translation conventions

- Modern Standard Arabic, concise, no diacritics (the engine cannot stack
  marks; the pipeline drops them anyway).
- POKéMON → بوكيمون throughout.
- Battle name prefixes: `Foe`/`Wild` are *prepended* by the engine, but since
  the prefix sits visually left of the (Latin) name, right-to-left reading
  order comes out correct: «البري PIDGEY» reads as «بيدجي البري».
- Species, move, item and trainer names stay Latin for now — they live in
  fixed-length data tables and are a separate, mechanical batch of work.
- {PLAYER} is grammatically masculine in battle text, matching the original's
  third-person register. Gender-neutral phrasing is used where it reads
  naturally.

## Known limits

- The typewriter effect reveals each line left-to-right, i.e. an Arabic line
  appears end-first as it types. Setting text speed to FAST minimises it; the
  real fix is an engine-side right-to-left printer, which is future work.
- Text is left-aligned in boxes (the engine draws from the left edge).
- Auto-rendered glyphs at 11pt are legible but not beautiful; hand-pixel the
  frequent forms via the sheet PNGs when polish matters.
