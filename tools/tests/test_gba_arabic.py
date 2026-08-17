"""Tests for the Arabic GBA toolchain.

Shaping and reordering are the parts where a subtle error produces text that
looks plausible but reads as gibberish in game, so they carry the most cases.
Expected forms below were derived from Unicode's Presentation Forms-B block and
Arabic joining rules, not from the implementation.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.gba_arabic import bidi, charmap as cm, glyphs as gl, pipeline, scriptio, shaping
from tools.gba_arabic import profiles as profiles_mod


# -- shaping ---------------------------------------------------------------

def test_forms_table_matches_unicode_block():
    # 1 hamza + 12 right-joining x 2 + 23 dual-joining x 4 = 117 slots,
    # so the table must end exactly where the lam-alef ligatures begin.
    total = sum(len(v) for v in shaping.FORMS.values())
    assert total == 117
    assert shaping.FORMS[0x0628]["initial"] == 0xFE91
    assert shaping.FORMS[0x064A]["medial"] == 0xFEF4
    assert shaping.FORMS[0x0621]["isolated"] == 0xFE80


@pytest.mark.parametrize("word, expected", [
    # بوكيمون: beh initial, waw final, kaf initial, yeh medial, meem medial,
    # waw final, noon isolated -- waw is right-joining so it breaks the run.
    ("بوكيمون", [0xFE91, 0xFEEE, 0xFEDB, 0xFEF4, 0xFEE4, 0xFEEE, 0xFEE5]),
    # A single letter is always isolated.
    ("ب", [0xFE8F]),
    # Alef never joins to what follows it.
    ("دار", [0xFEA9, 0xFE8D, 0xFEAD]),
    ("العربية", [0xFE8D, 0xFEDF, 0xFECC, 0xFEAE, 0xFE91, 0xFEF4, 0xFE94]),
])
def test_shape_forms(word, expected):
    assert [ord(c) for c in shaping.shape(word)] == expected


@pytest.mark.parametrize("word, expected", [
    ("لا", [0xFEFB]),               # isolated ligature
    ("سلام", [0xFEB3, 0xFEFC, 0xFEE1]),   # final ligature, lam joined to seen
    ("لآ", [0xFEF5]),
    ("لأ", [0xFEF7]),
    ("لإ", [0xFEF9]),
])
def test_lam_alef_ligatures(word, expected):
    # Leaving lam+alef as two glyphs is the classic sign of a broken hack.
    assert [ord(c) for c in shaping.shape(word)] == expected


def test_meem_after_ligature_is_isolated():
    # The ligature ends in an alef, which cannot join forward, so the next
    # letter must not take a final/medial form.
    assert ord(shaping.shape("سلام")[-1]) == shaping.FORMS[0x0645]["isolated"]


def test_marks_dropped_by_default_but_transparent_to_joining():
    with_marks = "بَيْت"
    assert shaping.shape(with_marks) == shaping.shape("بيت")
    kept = shaping.shape(with_marks, drop_marks=False)
    assert any(shaping.is_mark(c) for c in kept)
    # The fatha between beh and yeh must not break the join.
    assert ord(kept[0]) == shaping.FORMS[0x0628]["initial"]


def test_non_arabic_passes_through():
    assert shaping.shape("Lv 100") == "Lv 100"
    assert shaping.shape("Lv 100 ب").startswith("Lv 100 ")


def test_context_types_join_across_a_gap():
    # Standing alone, beh is isolated; told a dual-joining letter follows,
    # it must take its initial form.
    assert ord(shaping.shape("ب")[0]) == shaping.FORMS[0x0628]["isolated"]
    assert ord(shaping.shape("ب", next_type=shaping.D)[0]) == shaping.FORMS[0x0628]["initial"]
    assert ord(shaping.shape("ب", prev_type=shaping.D)[0]) == shaping.FORMS[0x0628]["final"]


# -- reordering ------------------------------------------------------------

def test_arabic_is_reversed():
    assert bidi.reorder_string("ابج") == "جبا"


def test_latin_run_keeps_its_direction():
    out = bidi.shape_and_reorder("احصل على Lv 100 الآن")
    assert "Lv 100" in out          # not "001 vL"


def test_digits_are_not_reversed():
    out = bidi.shape_and_reorder("الحد 151 نقطة")
    assert "151" in out


def test_trailing_neutral_takes_base_direction():
    out = bidi.reorder_string("اب!")
    assert out == "!با"


def test_ltr_base_leaves_latin_alone():
    assert bidi.reorder_string("abc", base=bidi.LTR) == "abc"


def test_atoms_are_not_split_or_reversed():
    table = cm.Charmap()
    lines = table.tokenise("مرحبا {PLAYER} اليوم")
    visual = bidi.reorder(pipeline.shape_line(lines[0].tokens))
    atoms = [t for t in visual if isinstance(t, bidi.Atom)]
    assert len(atoms) == 1
    assert atoms[0].data == bytes([cm.PLACEHOLDER, 0x01])


def test_zero_width_atom_does_not_break_a_join():
    table = cm.Charmap()
    # A colour code dropped inside a word must leave the cursive join intact.
    plain = pipeline.shape_line(table.tokenise("بيت")[0].tokens)
    split = pipeline.shape_line(table.tokenise("بي{FC 01}ت")[0].tokens)
    letters = [t.ch for t in split if isinstance(t, bidi.Char)]
    assert letters == [t.ch for t in plain if isinstance(t, bidi.Char)]


def test_placeholder_does_break_a_join():
    table = cm.Charmap()
    toks = pipeline.shape_line(table.tokenise("ب{PLAYER}ت")[0].tokens)
    chars = [t for t in toks if isinstance(t, bidi.Char)]
    assert ord(chars[0].ch) == shaping.FORMS[0x0628]["isolated"]


# -- charmap ---------------------------------------------------------------

def test_default_charmap_core_values():
    table = cm.Charmap()
    assert table.to_byte["A"] == 0xBB
    assert table.to_byte["z"] == 0xEE
    assert table.to_byte["0"] == 0xA1
    assert table.to_byte[" "] == 0x00


def test_tokenise_separators_and_atoms():
    table = cm.Charmap()
    lines = table.tokenise("سطر\\nثان\\pثالث")
    assert [ln.separator for ln in lines] == [cm.NEWLINE, cm.PARAGRAPH, None]
    assert len(lines) == 3


def test_raw_hex_atom():
    table = cm.Charmap()
    lines = table.tokenise("{FC 01 02}x")
    atom = lines[0].tokens[0]
    assert isinstance(atom, cm.Atom) and atom.data == bytes([0xFC, 0x01, 0x02])


def test_unknown_token_is_an_error():
    with pytest.raises(cm.EncodeError):
        cm.Charmap().tokenise("{NOPE}")


def test_literal_brace():
    table = cm.Charmap()
    assert "".join(t for t in table.tokenise("{{x")[0].tokens if isinstance(t, str)) == "{x"


def test_load_tbl(tmp_path):
    p = tmp_path / "t.tbl"
    p.write_text("# comment\n01=ا\n02=ب\nBB=A\n", encoding="utf-8")
    table = cm.load_tbl(str(p))
    assert table.to_byte["ا"] == 0x01 and table.verified


def test_load_decomp_charmap(tmp_path):
    p = tmp_path / "charmap.txt"
    p.write_text("'A' = BB\n'B' = BC\nSPACE = 00\n'0' = A1\nFOO = FC 01\n", encoding="utf-8")
    table = cm.load_pokeemerald_charmap(str(p))
    assert table.to_byte["A"] == 0xBB
    assert table.to_byte["0"] == 0xA1
    assert 0xFC not in table.to_char        # multi-byte macros are skipped


# -- allocation ------------------------------------------------------------

def test_allocation_is_frequency_ordered_and_deterministic():
    script = ["بببب ت"]          # beh far more frequent than teh
    a = gl.allocate(script)
    b = gl.allocate(script)
    assert a.glyph_to_byte == b.glyph_to_byte
    ordered = sorted(a.glyph_to_byte.items(), key=lambda kv: kv[1])
    assert a.frequency[ordered[0][0]] >= a.frequency[ordered[-1][0]]


def test_existing_glyphs_are_reused_not_allocated():
    alloc = gl.allocate(["مرحبا 100"])
    # Digits and the space already exist in the ROM font.
    assert alloc.reused[ord("1")] == 0xA2
    assert ord("1") not in alloc.glyph_to_byte


def test_markup_is_not_counted_as_text():
    alloc = gl.allocate(["مرحبا {PLAYER}\\nثان"])
    assert not any(chr(g) in "{}PLAYERn\\" for g in alloc.glyph_to_byte)


def test_overflow_is_reported_not_raised():
    alloc = gl.allocate(["أبجد هوز حطي كلمن سعفص قرشت ثخذ ضظغ"],
                        free_ranges=((0x01, 0x03),))
    assert not alloc.ok
    assert alloc.slots_used == 3
    assert "OVERFLOW" in gl.report(alloc)


def test_no_keep_latin_frees_slots():
    script = ["أبجد هوز حطي كلمن سعفص قرشت ثخذ ضظغ"]
    tight = ((0x01, 0x10),)
    assert gl.allocate(script, free_ranges=tight).slots_total == 16
    assert gl.allocate(script, free_ranges=tight, keep_latin=False).slots_total > 16


def test_equivalence_shares_bytes():
    always = lambda a, b: True
    alloc = gl.allocate(["بت"], equivalent=always)
    assert alloc.slots_used == 1


def test_allocation_json_roundtrip(tmp_path):
    from tools.gba_arabic.cli import _load_alloc
    alloc = gl.allocate(["مرحبا بالعالم"])
    p = tmp_path / "m.json"
    p.write_text(alloc.to_json(), encoding="utf-8")
    back = _load_alloc(str(p))
    assert back.glyph_to_byte == alloc.glyph_to_byte
    assert back.reused == alloc.reused


# -- encoding --------------------------------------------------------------

def test_encode_terminates_and_reverses():
    table = cm.Charmap()
    alloc = gl.allocate(["ابج"], table=table)
    data = pipeline.encode("ابج", table, alloc)
    assert data[-1] == cm.EOS
    body = data[:-1]
    assert len(body) == 3
    # Visually first byte is the last letter, jeem.
    assert body[0] == alloc.byte_for(ord(shaping.shape("ابج")[2]))


def test_encode_keeps_separators_in_place():
    table = cm.Charmap()
    alloc = gl.allocate(["اب\\nجد"], table=table)
    data = pipeline.encode("اب\\nجد", table, alloc)
    assert data.count(cm.NEWLINE) == 1
    assert data.index(cm.NEWLINE) == 2


def test_encode_embeds_placeholder_bytes():
    table = cm.Charmap()
    alloc = gl.allocate(["مرحبا {PLAYER}"], table=table)
    data = pipeline.encode("مرحبا {PLAYER}", table, alloc)
    assert bytes([cm.PLACEHOLDER, 0x01]) in data


def test_encode_latin_run_stays_readable():
    table = cm.Charmap()
    text = "الحد Lv 50"
    alloc = gl.allocate([text], table=table)
    data = pipeline.encode(text, table, alloc)
    assert bytes([table.to_byte["L"], table.to_byte["v"]]) in data
    assert bytes([table.to_byte["5"], table.to_byte["0"]]) in data


def test_encode_without_a_glyph_slot_errors():
    with pytest.raises(cm.EncodeError):
        pipeline.encode("مرحبا", cm.Charmap(), None)


def test_decode_roundtrips_latin_and_markup():
    table = cm.Charmap()
    data = pipeline.encode("Lv 50\\n{PLAYER}", table, None)
    out = table.decode(data)
    assert out == "Lv 50\\n{PLAYER}"


def test_measure_skips_control_sequences():
    widths = {0xBB: 6, 0x00: 3}
    data = bytes([0xBB, cm.PLACEHOLDER, 0x01, cm.NEWLINE, 0xBB, cm.EOS])
    assert pipeline.measure(data, widths, default=0) == 12


def test_align_right_pads_the_line():
    table = cm.Charmap()
    alloc = gl.allocate(["اب"], table=table)
    widths = {b: 6 for b in alloc.glyph_to_byte.values()} | {cm.SPACE: 3}
    data = pipeline.encode("اب", table, alloc, align_right=60, widths=widths)
    assert data.startswith(bytes([cm.SPACE]) * 16)


# -- script IO -------------------------------------------------------------

def test_load_block_format(tmp_path):
    p = tmp_path / "s.pks"
    p.write_text("[MSG_A]\nسطر أول\nسطر ثان\n\n[MSG_B]\nنص\n", encoding="utf-8")
    entries = scriptio.load(str(p))
    assert list(entries) == ["MSG_A", "MSG_B"]
    assert entries["MSG_A"].count("\n") == 1


def test_load_json_dict_and_list(tmp_path):
    d = tmp_path / "a.json"
    d.write_text(json.dumps({"K": "نص"}, ensure_ascii=False), encoding="utf-8")
    assert scriptio.load(str(d)) == {"K": "نص"}
    l = tmp_path / "b.json"
    l.write_text(json.dumps(["one", "two"]), encoding="utf-8")
    assert list(scriptio.load(str(l)).values()) == ["one", "two"]


def test_load_txt_skips_comments(tmp_path):
    p = tmp_path / "s.txt"
    p.write_text("# note\n\nنص\n", encoding="utf-8")
    assert list(scriptio.load(str(p)).values()) == ["نص"]


def test_load_many_disambiguates_clashing_keys(tmp_path):
    a, b = tmp_path / "a.pks", tmp_path / "b.pks"
    a.write_text("[K]\nواحد\n", encoding="utf-8")
    b.write_text("[K]\nاثنان\n", encoding="utf-8")
    merged = scriptio.load_many([str(a), str(b)])
    assert len(merged) == 2


# -- CLI -------------------------------------------------------------------

def _run(*args, cwd=None):
    return subprocess.run(
        [sys.executable, "-m", "tools.gba_arabic", *args],
        capture_output=True, text=True,
        cwd=str(cwd or Path(__file__).resolve().parents[2]),
    )


def test_cli_shape():
    r = _run("shape", "بوكيمون")
    assert r.returncode == 0
    assert "initial" in r.stdout and "visual" in r.stdout


def test_cli_doctor():
    r = _run("doctor")
    assert r.returncode == 0 and "profiles" in r.stdout


def test_cli_scan_then_alloc_then_encode(tmp_path):
    script = tmp_path / "s.pks"
    script.write_text("[MSG_HELLO]\nمرحبا {PLAYER}!\\nهل أنت مستعد؟\n", encoding="utf-8")

    assert _run("scan", str(script)).returncode == 0

    m = tmp_path / "map.json"
    r = _run("alloc", str(script), "-o", str(m))
    assert r.returncode == 0 and m.exists()

    out = tmp_path / "out.json"
    r = _run("encode", str(script), "-m", str(m), "-o", str(out))
    assert r.returncode == 0
    blob = json.loads(out.read_text())
    assert blob["MSG_HELLO"].endswith("FF")


def test_cli_encode_without_map_fails_cleanly(tmp_path):
    script = tmp_path / "s.txt"
    script.write_text("مرحبا\n", encoding="utf-8")
    r = _run("encode", str(script))
    assert r.returncode == 1 and "glyph slot" in r.stderr


def test_cli_verify_reports_a_conflict(tmp_path):
    p = tmp_path / "charmap.txt"
    p.write_text("'A' = 42\n", encoding="utf-8")
    r = _run("verify", str(p))
    # 'A' is 0xBB in the built-in table; a real conflict must be called one,
    # not lumped in with glyphs the built-in table merely does not name.
    assert r.returncode == 0
    assert "CONFLICT" in r.stdout and "1 conflict" in r.stdout


def test_cli_verify_reports_no_conflict_when_only_coverage_differs(tmp_path):
    p = tmp_path / "charmap.txt"
    p.write_text("'A' = BB\n'é' = 1B\n", encoding="utf-8")
    r = _run("verify", str(p))
    assert r.returncode == 0 and "0 conflict(s)" in r.stdout


# -- font generation -------------------------------------------------------

from tools.gba_arabic import fontgen     # noqa: E402

pil = pytest.mark.skipif(not fontgen.HAVE_PIL, reason="Pillow not installed")

ALEF, MEEM_MEDIAL = 0xFE8D, 0xFEE4


@pil
def test_blank_cell_is_detected_as_blank():
    # Measured on palette indices, not colours: index 0 is a visible magenta,
    # so a luminance-based check would call every empty cell full of ink.
    assert fontgen.is_blank(fontgen.blank())
    assert fontgen.ink_bbox(fontgen.blank()) is None


@pil
def test_glyph_advance_is_narrower_than_the_cell(font_path):
    spec = fontgen.FontSpec()
    glyphs = fontgen.render([ALEF, MEEM_MEDIAL], font_path, spec)
    # Alef is a bare vertical stroke; meem is a wider bowl. Neither may claim
    # the whole cell, or letters render spaced out with the joins broken.
    alef_w = fontgen.glyph_width(glyphs[ALEF], spec, ALEF)
    meem_w = fontgen.glyph_width(glyphs[MEEM_MEDIAL], spec, MEEM_MEDIAL)
    assert 0 < alef_w < meem_w < spec.cell_w


def test_form_of_covers_forms_and_ligatures():
    assert shaping.form_of(0xFE91) == shaping.INITIAL     # beh
    assert shaping.form_of(0xFEE4) == shaping.MEDIAL      # meem
    assert shaping.form_of(0xFE8E) == shaping.FINAL       # alef
    assert shaping.form_of(0xFE8D) == shaping.ISOLATED    # alef
    assert shaping.form_of(0xFEFB) == shaping.ISOLATED    # lam-alef
    assert shaping.form_of(0xFEFC) == shaping.FINAL       # lam-alef
    assert shaping.form_of(ord("A")) is None


def test_visual_bearings_follow_the_joining_sides():
    # Drawn coordinates: forward-join side is visual LEFT (text is
    # pre-reversed), backward-join side is visual RIGHT.
    assert fontgen.visual_bearings(0xFEE4) == (0, 0)      # medial: butts both
    assert fontgen.visual_bearings(0xFE8E) == (1, 0)      # final: right joins
    assert fontgen.visual_bearings(0xFE91) == (0, 1)      # initial: left joins
    assert fontgen.visual_bearings(0xFE8D) == (1, 1)      # isolated
    assert fontgen.visual_bearings(0x061F) == (1, 1)      # Arabic punctuation


@pil
def test_isolated_alef_is_not_swallowed(font_path):
    # Alef's ink is a ~1px stroke. With zero bearings its advance was 1px and
    # it vanished into the neighbouring letters -- e.g. in "دار", where drawn
    # order is reh, alef, dal and nothing joins. Bearings on both sides give
    # it breathing room and a >=3px advance.
    spec = fontgen.FontSpec()
    glyphs = fontgen.render([0xFE8D], font_path, spec)
    assert fontgen.ink_bbox(glyphs[0xFE8D])[0] == 1       # inset from the left
    assert fontgen.glyph_width(glyphs[0xFE8D], spec, 0xFE8D) >= 3


@pil
def test_joining_edges_stay_flush(font_path):
    # A medial form must keep its ink at both cell edges of its advance, or
    # every cursive join in the word gains a visible 1px break.
    spec = fontgen.FontSpec()
    glyphs = fontgen.render([MEEM_MEDIAL], font_path, spec)
    bbox = fontgen.ink_bbox(glyphs[MEEM_MEDIAL])
    assert bbox[0] == 0
    assert fontgen.glyph_width(glyphs[MEEM_MEDIAL], spec, MEEM_MEDIAL) == bbox[2]


@pil
def test_final_form_joins_right_but_not_left(font_path):
    # Final alef joins the preceding letter, drawn to its right: ink must end
    # exactly at the advance (right edge flush) but start one column in.
    spec = fontgen.FontSpec()
    glyphs = fontgen.render([0xFE8E], font_path, spec)
    bbox = fontgen.ink_bbox(glyphs[0xFE8E])
    assert bbox[0] == 1
    assert fontgen.glyph_width(glyphs[0xFE8E], spec, 0xFE8E) == bbox[2]


@pil
def test_sheet_roundtrips_through_png(tmp_path, font_path):
    spec = fontgen.FontSpec()
    order = [ALEF, MEEM_MEDIAL, 0xFE91]
    glyphs = fontgen.render(order, font_path, spec)
    sheet = fontgen.build_sheet(glyphs, order, spec)
    p = tmp_path / "sheet.png"
    sheet.save(p)
    back = fontgen.load_sheet(str(p), order, spec)
    assert [back[c].tobytes() for c in order] == [glyphs[c].tobytes() for c in order]


@pil
def test_4bpp_packing_size_and_nibble_order():
    spec = fontgen.FontSpec()
    img = fontgen.blank(spec)
    img.load()[0, 0] = 1        # leftmost pixel -> low nibble
    img.load()[1, 0] = 2        # next pixel     -> high nibble
    data = fontgen.to_4bpp(img)
    assert len(data) == (spec.cell_w // 8) * (spec.cell_h // 8) * 32
    assert data[0] == 0x21


@pil
def test_bitmap_equivalence_matches_identical_renders(font_path):
    glyphs = fontgen.render([ALEF, MEEM_MEDIAL], font_path, fontgen.FontSpec())
    glyphs[0xFFFF] = glyphs[ALEF].copy()
    same = fontgen.bitmap_equivalence(glyphs)
    assert same(ALEF, 0xFFFF)
    assert not same(ALEF, MEEM_MEDIAL)


@pil
def test_width_table_is_keyed_by_byte(font_path):
    alloc = gl.allocate(["مرحبا"])
    glyphs = fontgen.render(list(alloc.glyph_to_byte), font_path, fontgen.FontSpec())
    widths = fontgen.width_table(glyphs, alloc.glyph_to_byte, fontgen.FontSpec())
    assert set(widths) == set(alloc.glyph_to_byte.values())
    assert all(0 < w <= 16 for w in widths.values())


@pil
def test_ink_bbox_ignores_the_background_box():
    # Index 3 is the white box the real sheets draw behind every glyph,
    # including the space. Counting it as ink makes every cell look occupied.
    spec = fontgen.FontSpec()
    img = fontgen.blank(spec)
    px = img.load()
    for y in range(fontgen.BOX_HEIGHT):
        for x in range(6):
            px[x, y] = fontgen.BOX
    assert fontgen.is_blank(img)
    px[2, 3] = fontgen.INK
    assert not fontgen.is_blank(img)


@pil
def test_to_sheet_cell_reproduces_the_decomp_convention():
    spec = fontgen.FontSpec()
    src = fontgen.blank(spec)
    src.load()[1, 2] = fontgen.INK
    out = fontgen.to_sheet_cell(src, width=6, spec=spec)
    px = out.load()
    assert px[1, 2] == fontgen.INK                       # ink survives
    assert px[0, 0] == fontgen.BOX                       # inside advance box
    assert px[5, fontgen.BOX_HEIGHT - 1] == fontgen.BOX  # last box pixel
    assert px[6, 0] == fontgen.BG                        # right of the advance
    assert px[0, fontgen.BOX_HEIGHT] == fontgen.BG       # below the box


# -- control code lengths --------------------------------------------------

def test_ext_ctrl_code_lengths_match_the_engine():
    # From GetExtCtrlCodeLength in pokefirered's src/string_util.c.
    assert cm.ext_ctrl_code_length(0x01) == 2   # COLOR: code + 1 arg
    assert cm.ext_ctrl_code_length(0x04) == 4   # COLOR_HIGHLIGHT_SHADOW: 3 args
    assert cm.ext_ctrl_code_length(0x09) == 1   # PAUSE_UNTIL_PRESS: no args
    assert cm.ext_ctrl_code_length(0x0B) == 3   # PLAY_BGM: 2 args
    assert cm.ext_ctrl_code_length(0xEE) == 1   # unknown: consume code only


def test_decode_consumes_full_control_sequences():
    table = cm.Charmap()
    data = bytes([cm.SPECIAL, 0x04, 0x01, 0x02, 0x03]) + bytes([table.to_byte["A"], cm.EOS])
    # A naive 2-byte assumption would emit the three colour arguments as text.
    assert table.decode(data) == "{FC 04 01 02 03}A"


def test_measure_skips_variable_length_control_codes():
    widths = {0xBB: 6}
    data = bytes([cm.SPECIAL, 0x04, 0x01, 0x02, 0x03, 0xBB, cm.EOS])
    assert pipeline.measure(data, widths, default=0) == 6


# -- decomp charmap precedence --------------------------------------------

def test_decomp_charmap_prefers_the_international_mapping(tmp_path):
    # A Gen 3 charmap.txt defines the same byte twice: international first,
    # Japanese later. For an English base ROM the first one is the real one.
    p = tmp_path / "charmap.txt"
    p.write_text("'e' = 1B\n'A' = BB\n'x' = 1B\n", encoding="utf-8")
    assert cm.load_decomp_charmap(str(p)).to_char[0x1B] == "e"
    assert cm.load_decomp_charmap(str(p), prefer="last").to_char[0x1B] == "x"


# -- decomp integration ----------------------------------------------------

from tools.gba_arabic import decomp     # noqa: E402


def _fake_sheet(tmp_path, cell_w=16, cell_h=16, cols=16, rows=16, name="latin_normal.png"):
    """A synthetic sheet following the real convention: ink for letters and
    digits, a bare background box for the space."""
    from PIL import Image
    table = cm.Charmap()
    img = Image.new("P", (cols * cell_w, rows * cell_h), fontgen.BG)
    img.putpalette([144, 200, 255, 56, 56, 56, 216, 216, 216, 255, 255, 255] + [0] * 756)
    px = img.load()
    cells = cols * rows
    for ch in "AZaz09":
        b = table.to_byte[ch]
        if b >= cells:
            continue                    # sheet too small for this probe
        cx, cy = (b % cols) * cell_w, (b // cols) * cell_h
        px[cx + 1, cy + 1] = fontgen.INK
    b = table.to_byte[" "]
    if b < cells:
        cx, cy = (b % cols) * cell_w, (b // cols) * cell_h
        for y in range(min(fontgen.BOX_HEIGHT, cell_h)):
            px[cx, cy + y] = fontgen.BOX
    d = tmp_path / "graphics" / "fonts"
    d.mkdir(parents=True, exist_ok=True)
    img.save(d / name)
    return d / name


@pil
def test_detect_spec_finds_16x16(tmp_path):
    from PIL import Image
    p = _fake_sheet(tmp_path)
    spec = decomp.detect_spec(Image.open(p).convert("P"), cm.Charmap())
    assert (spec.cell_w, spec.cell_h) == (16, 16)


@pil
def test_detect_spec_finds_8x16(tmp_path):
    from PIL import Image
    p = _fake_sheet(tmp_path, cell_w=8, cols=32, rows=16, name="latin_small.png")
    spec = decomp.detect_spec(Image.open(p).convert("P"), cm.Charmap())
    assert (spec.cell_w, spec.cell_h) == (8, 16)


@pil
def test_detect_spec_rejects_an_unrecognisable_sheet(tmp_path):
    from PIL import Image
    img = Image.new("P", (100, 37), 0)
    with pytest.raises(ValueError):
        decomp.detect_spec(img, cm.Charmap())


@pil
def test_install_touches_only_allocated_cells(tmp_path, font_path):
    p = _fake_sheet(tmp_path)
    original = decomp.load_sheet(p)
    sheet = decomp.load_sheet(p)
    alloc = gl.allocate(["مرحبا"], free_ranges=((0x01, 0x0F),))
    order = list(alloc.glyph_to_byte)
    glyphs = fontgen.render(order, font_path, sheet.spec)

    written, skipped = decomp.install(sheet, glyphs, alloc.glyph_to_byte)
    assert written == len(order) and not skipped

    changed = {
        i for i in range(original.cells)
        if sheet.cell(i).tobytes() != original.cell(i).tobytes()
    }
    assert changed == set(alloc.glyph_to_byte.values())


@pil
def test_install_reports_bytes_outside_the_sheet(tmp_path, font_path):
    p = _fake_sheet(tmp_path, rows=1)          # only 16 cells
    sheet = decomp.load_sheet(p, fontgen.FontSpec())
    glyphs = fontgen.render([0xFE8D], font_path, sheet.spec)
    written, skipped = decomp.install(sheet, glyphs, {0xFE8D: 0xC0})
    assert written == 0 and skipped == [0xC0]


@pil
def test_verify_sheet_indexing_flags_a_shifted_layout(tmp_path):
    p = _fake_sheet(tmp_path)
    sheet = decomp.load_sheet(p, fontgen.FontSpec(cell_w=8, columns=32))
    assert decomp.verify_sheet_indexing(sheet, cm.Charmap())


@pil
def test_blank_cells_excludes_glyphs(tmp_path):
    p = _fake_sheet(tmp_path)
    sheet = decomp.load_sheet(p)
    blanks = set(decomp.blank_cells(sheet))
    table = cm.Charmap()
    assert table.to_byte["A"] not in blanks
    assert table.to_byte[" "] in blanks        # a bare box counts as blank


def test_read_and_patch_width_table(tmp_path):
    src = tmp_path / "text.c"
    src.write_text(
        "static const u8 sFontNormalLatinGlyphWidths[] =\n{\n"
        "     6,  6,  6,  6,\n     6,  6,  6,  6,\n};\n"
        "static const u8 other[] = { 1, 2 };\n",
        encoding="utf-8",
    )
    assert decomp.read_width_table(src, "sFontNormalLatinGlyphWidths") == [6] * 8
    out = decomp.patch_width_table(src, "sFontNormalLatinGlyphWidths", {0: 3, 7: 9})
    src.write_text(out, encoding="utf-8")
    assert decomp.read_width_table(src, "sFontNormalLatinGlyphWidths") == [3, 6, 6, 6, 6, 6, 6, 9]
    assert decomp.read_width_table(src, "other") == [1, 2]      # untouched


def test_patch_width_table_rejects_a_missing_array(tmp_path):
    src = tmp_path / "text.c"
    src.write_text("int x;\n", encoding="utf-8")
    with pytest.raises(KeyError):
        decomp.patch_width_table(src, "sFontNormalLatinGlyphWidths", {0: 1})


def test_firered_profile_excludes_the_glyphs_that_are_in_use():
    prof = profiles_mod.get("firered")
    free = {b for lo, hi in prof.free_ranges for b in range(lo, hi + 1)}
    assert 0x1B not in free, "0x1B is 'é', used in 1234 ROM strings"
    assert 0x2D not in free, "0x2D is '&', used in real strings"
    assert 0x01 in free and 0x77 in free
    assert len(free) == 117
    assert prof.placeholders["PLAYER"] == 0x01
    assert "VERSION" not in prof.placeholders    # FireRed has no VERSION


@pil
def test_preview_width_equals_sum_of_advances(font_path):
    spec = fontgen.FontSpec()
    table = cm.Charmap()
    alloc = gl.allocate(["مرحبا"], table=table)
    order = list(alloc.glyph_to_byte)
    glyphs = fontgen.render(order, font_path, spec)
    data = pipeline.encode("مرحبا", table, alloc, terminate=False)
    img = fontgen.preview(data, glyphs, alloc.glyph_to_byte, spec, scale=1)
    expected = sum(fontgen.glyph_width(glyphs[ord(c)], spec, ord(c))
                   for c in shaping.shape("مرحبا"))
    assert img.width == expected
