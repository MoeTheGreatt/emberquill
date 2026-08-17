"""Shared fixtures.

The font tests need a face that covers Arabic Presentation Forms-B, which is
not something a CI image is guaranteed to have. Rather than vendor a font
binary into the repo, locate one and skip if there is none.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Presentation Forms-B glyphs that must render non-blank for a font to be usable.
_PROBE = (0xFE8D, 0xFEE4, 0xFEFB)

_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
    "/usr/share/fonts/opentype/amiri/Amiri-Regular.ttf",
    # DejaVu Sans does carry full Presentation Forms-B coverage, and its flatter
    # letterforms actually survive downsampling to Gen 3 glyph sizes better than
    # a Naskh face does.
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/GeezaPro.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


def _covers_arabic(path: str) -> bool:
    """True only if the font really has these glyphs.

    "Not blank" is not enough: a font missing a codepoint draws .notdef, a
    hollow box, and fc-match always hands back *some* fallback face. Alef, a
    medial meem and the lam-alef ligature look nothing like each other, so if
    all three rasterise identically they are all .notdef.
    """
    from tools.gba_arabic import fontgen

    if not fontgen.HAVE_PIL:
        return False
    try:
        glyphs = fontgen.render(list(_PROBE), path, fontgen.FontSpec())
    except Exception:
        return False
    if any(fontgen.is_blank(img) for img in glyphs.values()):
        return False
    shapes = {img.tobytes() for img in glyphs.values()}
    return len(shapes) == len(_PROBE)


def _discover() -> str | None:
    env = os.environ.get("GBA_ARABIC_TEST_FONT")
    if env and Path(env).exists() and _covers_arabic(env):
        return env

    for cand in _CANDIDATES:
        if Path(cand).exists() and _covers_arabic(cand):
            return cand

    if shutil.which("fc-match"):
        try:
            out = subprocess.run(
                ["fc-match", "-f", "%{file}", ":lang=ar"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        if out and Path(out).exists() and _covers_arabic(out):
            return out
    return None


@pytest.fixture(scope="session")
def font_path() -> str:
    found = _discover()
    if not found:
        pytest.skip(
            "no Arabic-capable font found; install one (e.g. fonts-noto-naskh-arabic) "
            "or set GBA_ARABIC_TEST_FONT=/path/to/font.ttf"
        )
    return found
