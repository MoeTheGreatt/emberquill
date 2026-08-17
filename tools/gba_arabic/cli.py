"""Command line for the Arabic GBA toolchain.

    gba-arabic shape   "مرحبا"                 inspect shaping + reordering
    gba-arabic scan    script.pks              glyph budget for a script
    gba-arabic alloc   script.pks -o map.json  assign byte values to glyphs
    gba-arabic encode  script.pks -m map.json  emit the byte stream
    gba-arabic font    -m map.json -f f.ttf    build the font sheet
    gba-arabic preview "مرحبا" -m map.json     render a line as the engine will
    gba-arabic verify  charmap.txt             diff a real charmap vs built-in
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import bidi, charmap as cm, glyphs as gl, pipeline, profiles, scriptio, shaping


def _table(args: argparse.Namespace) -> cm.Charmap:
    if getattr(args, "charmap", None):
        path = Path(args.charmap)
        table = (cm.load_pokeemerald_charmap(str(path))
                 if path.suffix.lower() == ".txt"
                 else cm.load_tbl(str(path)))
    else:
        table = cm.Charmap()
    prof = profiles.get(args.profile)
    table.placeholders = dict(prof.placeholders)
    return table


def _ranges(args: argparse.Namespace) -> tuple[tuple[int, int], ...]:
    if getattr(args, "free_ranges", None):
        return profiles.parse_ranges(args.free_ranges)
    return profiles.get(args.profile).free_ranges


def _fitted_render(order, font_path: str, spec, label: str = ""):
    """Autofit the size to the engine's glyph box, then rasterise.

    Returns (glyphs, fitted spec). Everything downstream -- widths, sheets,
    previews -- must use the fitted spec, or baselines drift.
    """
    from . import fontgen

    fitted, info = fontgen.autofit(order, font_path, spec)
    prefix = f"  {label}: " if label else ""
    line = (f"{prefix}autofit {info['size']}pt, baseline {info['baseline']} "
            f"(ascent {info['ascent']} + descent {info['descent']} in a "
            f"{info['box']}-row box)")
    if not info["fit"]:
        line += (f" -- NO SIZE FITS this face: {info['clipped_rows']} row(s) "
                 f"will clip (tallest {chr(info['tallest'])}, deepest "
                 f"{chr(info['deepest'])}). Use a flatter or smaller face: DejaVu "
                 f"Sans fits a 14-row box at 11pt, Noto Naskh only at 9pt.")
    print(line, file=sys.stderr)
    return fontgen.render(order, font_path, fitted), fitted


def _load_alloc(path: str) -> gl.Allocation:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    alloc = gl.Allocation(slots_total=data.get("slots_total", 0))
    for row in data["glyphs"]:
        alloc.glyph_to_byte[int(row["codepoint"][2:], 16)] = int(row["byte"], 16)
        alloc.frequency[int(row["codepoint"][2:], 16)] = row.get("count", 0)
    for key, val in data.get("reused", {}).items():
        alloc.reused[int(key[2:], 16)] = int(val, 16)
    return alloc


# -- commands --------------------------------------------------------------

def cmd_shape(args: argparse.Namespace) -> int:
    text = args.text
    shaped = shaping.shape(text)
    visual = bidi.reorder_string(shaped, bidi.LTR if args.ltr else bidi.RTL)

    print(f"input   : {text}")
    print(f"shaped  : {shaped}")
    print(f"visual  : {visual}")
    print(f"bytes   : {' '.join(f'{ord(c):04X}' for c in visual)}")
    print()
    print("logical order, form chosen per letter:")
    for src, glyph, form in shaping.describe(text):
        if shaping.is_arabic(glyph):
            print(f"  {src}  ->  {glyph}  U+{ord(glyph):04X}  {form}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    entries = scriptio.load_many(args.script)
    alloc = gl.allocate(
        entries.values(),
        table=_table(args),
        free_ranges=_ranges(args),
        keep_latin=not args.no_keep_latin,
    )
    print(f"entries: {len(entries)}")
    print()
    print(gl.report(alloc))
    return 0 if alloc.ok else 1


def cmd_alloc(args: argparse.Namespace) -> int:
    entries = scriptio.load_many(args.script)
    table = _table(args)
    equivalent = None

    if args.dedupe:
        from . import fontgen
        if not args.font:
            print("--dedupe needs --font to compare rendered pixels", file=sys.stderr)
            return 2
        wanted = sorted(gl.collect(entries.values()))
        rendered, _ = _fitted_render(wanted, args.font, profiles.get(args.profile).font)
        equivalent = fontgen.bitmap_equivalence(rendered)

    alloc = gl.allocate(
        entries.values(),
        table=table,
        free_ranges=_ranges(args),
        keep_latin=not args.no_keep_latin,
        equivalent=equivalent,
    )
    Path(args.out).write_text(alloc.to_json(), encoding="utf-8")
    print(gl.report(alloc))
    print()
    print(f"wrote {args.out}")
    return 0 if alloc.ok else 1


def cmd_encode(args: argparse.Namespace) -> int:
    entries = scriptio.load_many(args.script)
    table = _table(args)
    alloc = _load_alloc(args.map) if args.map else None

    blobs: dict[str, bytes] = {}
    failures = 0
    for key, text in entries.items():
        try:
            blobs[key] = pipeline.encode(text, table, alloc)
        except cm.EncodeError as exc:
            print(f"{key}: {exc}", file=sys.stderr)
            failures += 1

    if args.out:
        out = Path(args.out)
        if out.suffix.lower() == ".json":
            out.write_text(json.dumps(
                {k: v.hex(" ").upper() for k, v in blobs.items()}, indent=2
            ), encoding="utf-8")
        else:
            out.write_bytes(b"".join(blobs.values()))
        print(f"wrote {len(blobs)} entr{'y' if len(blobs) == 1 else 'ies'} to {out}")
    else:
        for key, data in blobs.items():
            print(f"{key}: {data.hex(' ').upper()}")

    if failures:
        print(f"\n{failures} entr{'y' if failures == 1 else 'ies'} failed to encode",
              file=sys.stderr)
    return 1 if failures else 0


def cmd_font(args: argparse.Namespace) -> int:
    from . import fontgen

    alloc = _load_alloc(args.map)
    spec = profiles.get(args.profile).font
    order = [g for g, _ in sorted(alloc.glyph_to_byte.items(), key=lambda kv: kv[1])]

    if args.edit:
        rendered = fontgen.load_sheet(args.edit, order, spec)
        print(f"read {len(rendered)} glyph(s) from {args.edit}")
    else:
        if not args.font:
            print("need --font TTF, or --edit SHEET.png to import hand-pixelled glyphs",
                  file=sys.stderr)
            return 2
        rendered, spec = _fitted_render(order, args.font, spec)
        blanks = [cp for cp, img in rendered.items() if fontgen.is_blank(img)]
        if blanks:
            print(f"warning: {len(blanks)} glyph(s) rendered blank -- this font lacks "
                  f"Presentation Forms-B coverage for: "
                  f"{' '.join(f'U+{c:04X}' for c in blanks[:12])}"
                  f"{' ...' if len(blanks) > 12 else ''}", file=sys.stderr)

    out = Path(args.out)
    sheet = fontgen.build_sheet(rendered, order, spec)
    sheet.save(out)
    print(f"wrote sheet {out}  ({sheet.width}x{sheet.height}, "
          f"{len(order)} cells, byte order)")

    if args.tiles:
        Path(args.tiles).write_bytes(fontgen.to_4bpp(sheet))
        print(f"wrote 4bpp tiles {args.tiles}")

    if args.widths:
        widths = fontgen.width_table(rendered, alloc.glyph_to_byte, spec)
        Path(args.widths).write_text(json.dumps(
            {f"0x{b:02X}": w for b, w in sorted(widths.items())}, indent=2
        ), encoding="utf-8")
        print(f"wrote width table {args.widths}")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    from . import fontgen

    alloc = _load_alloc(args.map)
    spec = profiles.get(args.profile).font
    order = [g for g, _ in sorted(alloc.glyph_to_byte.items(), key=lambda kv: kv[1])]
    if args.edit:
        rendered = fontgen.load_sheet(args.edit, order, spec)
    else:
        rendered, spec = _fitted_render(order, args.font, spec)

    data = pipeline.encode(args.text, _table(args), alloc, terminate=False)
    img = fontgen.preview(data, rendered, alloc.glyph_to_byte, spec, scale=args.scale)
    img.save(args.out)
    print(f"encoded : {data.hex(' ').upper()}")
    print(f"wrote {args.out} -- read it right-to-left; if it reads correctly, "
          f"the byte stream is correct")
    return 0


def cmd_decomp(args: argparse.Namespace) -> int:
    from . import decomp, fontgen

    alloc = _load_alloc(args.map)
    prof = profiles.get(args.profile)
    spec = prof.font
    order = [g for g, _ in sorted(alloc.glyph_to_byte.items(), key=lambda kv: kv[1])]

    sheets = decomp.find_sheets(args.repo)
    if not sheets:
        print(f"no Latin font sheets under {args.repo}/graphics/fonts", file=sys.stderr)
        return 1

    table = _table(args)
    targets = [n for n in args.sheets.split(",") if n.strip()] if args.sheets else list(sheets)

    for name in targets:
        path = sheets.get(name)
        if path is None:
            print(f"  {name}: not found, skipped", file=sys.stderr)
            continue
        sheet = decomp.load_sheet(path, spec)

        # Geometry differs per sheet, so detect it rather than trust the profile.
        try:
            sheet.spec = decomp.detect_spec(sheet.image, table, spec)
        except ValueError as exc:
            print(f"  {name}: {exc}", file=sys.stderr)
            continue
        if (sheet.spec.cell_w, sheet.spec.cell_h) != (spec.cell_w, spec.cell_h):
            print(f"  {name}: {sheet.spec.cell_w}x{sheet.spec.cell_h} cells "
                  f"(profile says {spec.cell_w}x{spec.cell_h}) - using detected")

        problems = decomp.verify_sheet_indexing(sheet, table)
        if problems:
            print(f"  {name}: layout check failed -- {'; '.join(problems)}",
                  file=sys.stderr)
            if not args.force:
                print("  refusing to write; pass --force to override", file=sys.stderr)
                continue

        # Glyphs must be rasterised at this sheet's cell size, then autofit to
        # the engine's 14-row glyph box so no tail is cut off in game.
        if args.edit:
            rendered = fontgen.load_sheet(args.edit, order, sheet.spec)
        else:
            rendered, sheet_spec = _fitted_render(order, args.font, sheet.spec,
                                                  label=name)
            sheet.spec = sheet_spec

        written, skipped = decomp.install(sheet, rendered, alloc.glyph_to_byte)
        if args.dry_run:
            print(f"  {name}: would write {written} glyph(s)"
                  + (f", {len(skipped)} outside the sheet" if skipped else ""))
            continue
        decomp.save(sheet)
        print(f"  {name}: wrote {written} glyph(s) into {path}"
              + (f", {len(skipped)} outside the sheet" if skipped else ""))

        if args.widths:
            array = decomp.WIDTH_ARRAYS.get(name)
            src = Path(args.repo) / "src" / "text.c"
            if array and src.exists():
                widths = fontgen.width_table(rendered, alloc.glyph_to_byte, spec)
                patched = decomp.patch_width_table(src, array, widths)
                src.write_text(patched, encoding="utf-8")
                print(f"  {name}: updated {array} in {src}")
    return 0


def cmd_translate(args: argparse.Namespace) -> int:
    """Full decomp translation pass: allocate, install fonts, patch sources."""
    from . import decomp, decompsrc, fontgen

    repo = Path(args.repo)
    charmap_path = Path(args.charmap) if args.charmap else repo / "charmap.txt"
    if not charmap_path.exists():
        print(f"{charmap_path} not found", file=sys.stderr)
        return 1

    table = cm.load_decomp_charmap(str(charmap_path))
    table.placeholders = dict(profiles.get(args.profile).placeholders)
    byte_chars = decompsrc.load_byte_chars(charmap_path)

    translations = {k: v for k, v in json.loads(
        Path(args.translations).read_text(encoding="utf-8")).items()
        if not k.startswith("_")}
    print(f"{len(translations)} translated string(s) loaded")

    # One allocation over the WHOLE corpus, so every string shares the map.
    corpus = [decompsrc.plain_runs(t) for t in translations.values()]
    alloc = gl.allocate(corpus, table=table, free_ranges=_ranges(args))
    print(f"glyphs: {alloc.slots_used} new + {len(alloc.reused)} reused, "
          f"{alloc.slots_total - alloc.slots_used} slot(s) spare")
    if not alloc.ok:
        print(gl.report(alloc), file=sys.stderr)
        return 1
    if args.map:
        Path(args.map).write_text(alloc.to_json(), encoding="utf-8")

    # Install the Arabic glyphs into every Latin font sheet + width tables.
    if args.font:
        order = [g for g, _ in sorted(alloc.glyph_to_byte.items(), key=lambda kv: kv[1])]
        sheets = decomp.find_sheets(repo)
        for name, path in sheets.items():
            sheet = decomp.load_sheet(path, profiles.get(args.profile).font)
            try:
                sheet.spec = decomp.detect_spec(sheet.image, table,
                                                profiles.get(args.profile).font)
            except ValueError as exc:
                print(f"  {name}: {exc} -- skipped", file=sys.stderr)
                continue
            rendered, sheet.spec = _fitted_render(order, args.font, sheet.spec,
                                                  label=name)
            written, _ = decomp.install(sheet, rendered, alloc.glyph_to_byte)
            decomp.save(sheet)
            print(f"  {name}: {written} glyph(s) installed")
            array = decomp.WIDTH_ARRAYS.get(name)
            src = repo / "src" / "text.c"
            if array and src.exists():
                widths = fontgen.width_table(rendered, alloc.glyph_to_byte, sheet.spec)
                src.write_text(decomp.patch_width_table(src, array, widths),
                               encoding="utf-8")
                print(f"  {name}: {array} widths patched")

    # Optionally teach the engine to draw right-to-left, so the typewriter
    # reveals Arabic from the right and lines right-align.
    rtl_pred: object = False
    if args.rtl:
        import re as _re
        done = decomp.enable_rtl_printer(repo)
        print("RTL printer: " + "; ".join(done))
        exclude = _re.compile(args.rtl_exclude) if args.rtl_exclude else None
        # Data-like strings (name presets) must never carry the control code:
        # their bytes are copied into buffers and reprinted elsewhere.
        rtl_pred = (lambda sym: not (exclude and exclude.search(sym)))

    # Patch the string sources.
    report = decompsrc.patch_tree(repo, translations, table, alloc, byte_chars,
                                  rtl=rtl_pred)
    print(f"\npatched {len(report.patched)} string(s) across "
          f"{len(report.files)} file(s)")
    for f in sorted(report.files):
        print(f"  {f}")
    if report.missing:
        print(f"\nNOT FOUND ({len(report.missing)}) -- symbol names to fix:",
              file=sys.stderr)
        for s in report.missing:
            print(f"  {s}", file=sys.stderr)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    path = Path(args.charmap)
    real = (cm.load_pokeemerald_charmap(str(path)) if path.suffix.lower() == ".txt"
            else cm.load_tbl(str(path)))
    builtin = cm.Charmap()

    # Two kinds of real danger, and one harmless difference, so they are
    # reported separately.
    #
    #  * a byte that means a different character in each table
    #  * a character that lives at a different byte in each table -- just as
    #    dangerous, because encoding it would emit the wrong byte, and invisible
    #    to a byte-by-byte comparison
    #
    # Anything else is only a coverage difference.
    reassigned, relocated, only_real, only_builtin, agree = [], [], [], [], 0
    for byte in sorted(set(real.to_char) | set(builtin.to_char)):
        a, b = builtin.to_char.get(byte), real.to_char.get(byte)
        if a == b:
            agree += 1
        elif a is None:
            only_real.append((byte, b))
        elif b is None:
            only_builtin.append((byte, a))
        else:
            reassigned.append((byte, a, b))

    for char, mine in sorted(builtin.to_byte.items()):
        theirs = real.to_byte.get(char)
        if theirs is not None and theirs != mine:
            relocated.append((char, mine, theirs))

    conflicts = len(reassigned) + len(relocated)

    if reassigned:
        print(f"CONFLICTS - byte reassigned ({len(reassigned)}):")
        for byte, a, b in reassigned:
            print(f"  0x{byte:02X}  built-in {a!r:>6}  actual {b!r}")
    if relocated:
        print(f"CONFLICTS - character moved ({len(relocated)}) - encoding these "
              f"would emit the wrong byte:")
        for char, mine, theirs in relocated:
            print(f"  {char!r:>6}  built-in 0x{mine:02X}  actual 0x{theirs:02X}")
    if only_builtin:
        print(f"\nIn the built-in table but not {path.name} ({len(only_builtin)}) "
              f"- suspect:")
        for byte, a in only_builtin:
            print(f"  0x{byte:02X}  {a!r}")
    if only_real:
        print(f"\nOnly in {path.name} ({len(only_real)}) - glyphs the built-in "
              f"table does not name, free to reuse if your script has no need "
              f"of them:")
        print("  " + " ".join(f"{byte:02X}={c}" for byte, c in only_real[:48])
              + (" ..." if len(only_real) > 48 else ""))

    print()
    print(f"{agree} entr{'y' if agree == 1 else 'ies'} agree, "
          f"{conflicts} conflict(s)")
    if not conflicts:
        print("No conflicts: the built-in table is safe for this target, though "
              f"passing --charmap {path.name} is still better.")
    else:
        print(f"Pass --charmap {path} to every command and treat the actual "
              f"table as authoritative.")

    used = set(real.to_char) | cm.CONTROL_BYTES
    free = [b for b in range(0x100) if b not in used]
    print(f"\nbytes with no glyph in {path.name}: {len(free)}")
    if free:
        print("  " + " ".join(f"{b:02X}" for b in free[:64])
              + (" ..." if len(free) > 64 else ""))
        print("  ^ candidates for --free-ranges, after checking the engine does "
              "not special-case them")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"python      : {sys.version.split()[0]}")
    try:
        from PIL import Image, features
        print(f"pillow      : {Image.__version__}  (raqm: {features.check('raqm')})")
    except ImportError:
        print("pillow      : MISSING - font commands unavailable "
              "(text pipeline still works)")

    print()
    print("profiles:")
    for key, prof in sorted(profiles.PROFILES.items()):
        ranges = ", ".join(f"0x{a:02X}-0x{b:02X}" for a, b in prof.free_ranges)
        pool = sum(b - a + 1 for a, b in prof.free_ranges)
        print(f"  {key:9} {prof.game_code}  decomp={prof.decomp}")
        print(f"            free {ranges} ({pool} slots, unverified)")
        print(f"            font cell {prof.font.cell_w}x{prof.font.cell_h}")

    print()
    print("Fully shaped Arabic needs ~130 glyph slots. Run `scan` on your")
    print("script for the real number -- only forms you actually use cost one.")
    return 0


# -- wiring ----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="gba-arabic",
        description="Arabic localisation toolchain for Gen 3 Pokemon (GBA).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--profile", default="emerald",
                    choices=sorted(profiles.PROFILES),
                    help="target game (default: emerald)")
    ap.add_argument("--charmap", help="real charmap: decomp charmap.txt or a .tbl")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("shape", help="inspect shaping and reordering of one string")
    p.add_argument("text")
    p.add_argument("--ltr", action="store_true", help="assume a base-LTR line")
    p.set_defaults(fn=cmd_shape)

    for name, fn, need_out in (("scan", cmd_scan, False), ("alloc", cmd_alloc, True)):
        p = sub.add_parser(name, help=f"{name} glyph slots for a script")
        p.add_argument("script", nargs="+")
        p.add_argument("--free-ranges", help="override, e.g. 0x01-0x77,0x80-0x9F")
        p.add_argument("--no-keep-latin", action="store_true",
                       help="reclaim Latin letter slots (~62 more)")
        if need_out:
            p.add_argument("-o", "--out", default="glyphmap.json")
            p.add_argument("--dedupe", action="store_true",
                           help="share bytes between forms that render identically")
            p.add_argument("-f", "--font", help="TTF, required by --dedupe")
        p.set_defaults(fn=fn)

    p = sub.add_parser("encode", help="encode a script to bytes")
    p.add_argument("script", nargs="+")
    p.add_argument("-m", "--map", help="glyphmap.json from `alloc`")
    p.add_argument("-o", "--out", help=".bin for raw bytes, .json for per-entry hex")
    p.set_defaults(fn=cmd_encode)

    p = sub.add_parser("font", help="build or re-import the font sheet")
    p.add_argument("-m", "--map", required=True)
    p.add_argument("-f", "--font", help="TTF to rasterise")
    p.add_argument("--edit", help="import a hand-edited sheet instead of rasterising")
    p.add_argument("-o", "--out", default="font_arabic.png")
    p.add_argument("--tiles", help="also write raw 4bpp tiles here")
    p.add_argument("--widths", help="also write the width table here")
    p.set_defaults(fn=cmd_font)

    p = sub.add_parser("preview", help="render an encoded line as the engine will draw it")
    p.add_argument("text")
    p.add_argument("-m", "--map", required=True)
    p.add_argument("-f", "--font")
    p.add_argument("--edit", help="use a hand-edited sheet")
    p.add_argument("-o", "--out", default="preview.png")
    p.add_argument("--scale", type=int, default=3)
    p.set_defaults(fn=cmd_preview)

    p = sub.add_parser("decomp", help="install Arabic glyphs into a decomp's font sheets")
    p.add_argument("repo", help="path to a pokefirered/pokeemerald checkout")
    p.add_argument("-m", "--map", required=True)
    p.add_argument("-f", "--font", help="TTF to rasterise")
    p.add_argument("--edit", help="use a hand-edited sheet instead")
    p.add_argument("--sheets", help="comma-separated subset, e.g. latin_normal")
    p.add_argument("--widths", action="store_true", help="also patch src/text.c widths")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="write even if the sheet layout check fails")
    p.set_defaults(fn=cmd_decomp)

    p = sub.add_parser("translate",
                       help="full pass: allocate, install fonts, patch decomp strings")
    p.add_argument("repo", help="path to a pokefirered/pokeemerald checkout")
    p.add_argument("-t", "--translations", required=True,
                   help="JSON of {decomp symbol: Arabic text}")
    p.add_argument("-f", "--font", help="TTF to rasterise (omit to skip fonts)")
    p.add_argument("-m", "--map", help="also write the glyph map here")
    p.add_argument("--free-ranges", help="override the profile's free byte ranges")
    p.add_argument("--rtl", action="store_true",
                   help="patch the engine for right-to-left printing and emit "
                        "mirrored strings (typewriter reveals Arabic correctly)")
    p.add_argument("--rtl-exclude", default=r"^gNameChoice_|^gText_MainMenuTime$",
                   help="regex of symbols that must NOT use the RTL printer "
                        "(data-like strings whose bytes get copied into buffers)")
    p.set_defaults(fn=cmd_translate)

    p = sub.add_parser("verify", help="diff a real charmap against the built-in table")
    p.add_argument("charmap")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("doctor", help="show environment and profile defaults")
    p.set_defaults(fn=cmd_doctor)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (cm.EncodeError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"error: {exc.filename}: not found", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
