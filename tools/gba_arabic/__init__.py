"""Arabic localisation toolchain for Gen 3 Pokemon (GBA).

The GBA text engine draws one fixed glyph per byte, strictly left to right,
with no shaper and no bidi support. Arabic needs all three. This package moves
that work to build time:

    logical Arabic  ->  shaping  ->  visual reordering  ->  byte encoding
                            |             |                     |
                    presentation      RTL with LTR        glyph slots
                       forms          runs preserved      allocated from
                                                          the free byte pool

Only ``fontgen`` needs Pillow; the text path is dependency-free.
"""

from . import bidi, charmap, fontgen, glyphs, pipeline, profiles, shaping

__all__ = ["bidi", "charmap", "fontgen", "glyphs", "pipeline", "profiles", "shaping"]
__version__ = "0.1.0"
