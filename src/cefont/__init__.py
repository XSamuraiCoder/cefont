"""cefont - TTF/OTF -> FontLibC fonts for the TI-84 Plus CE.

Pure Python: no CE toolchain, no compiler. Convert and package fonts
straight to .8xv appvars with Skia. Optional extras: numpy+scipy (blur metric).
"""
from .fontpack import (FontPack, byte_columns, parse_font, parse_fontpack,
                      serialize_font, serialize_fontpack, to_c_array, WEIGHTS, STYLES)
from .auto import AutoResult, autotune
from .bench import BenchResult, run_bench
from .compare import Match, compare_fonts, score_font, score_vs_hinted, score_vs_ttf
from .model import Font, FntError, Glyph
from .raster import from_ttf, parse_ranges, parse_sizes
from .tools import appvar_name, make_appvar, read_appvar, verify_appvar
from .windowsfnt import parse_windows_fnt

__all__ = [
    "Font", "Glyph", "FntError", "from_ttf", "parse_ranges", "parse_sizes",
    # reference comparison
    "Match", "compare_fonts", "score_vs_ttf", "score_vs_hinted", "score_font",
    # auto-tuning against the TTF
    "autotune", "AutoResult",
    # quality benchmark across fonts/sizes/settings
    "run_bench", "BenchResult",
    # fontpack (no-CEdev serialization)
    "serialize_font", "serialize_fontpack", "to_c_array",
    # reading back (verify output, inspect existing packs)
    "parse_font", "parse_fontpack", "FontPack", "read_appvar", "verify_appvar",
    "byte_columns", "WEIGHTS", "STYLES",
    "parse_windows_fnt",
    # appvar packaging
    "appvar_name", "make_appvar",
]
__version__ = "1.2.0"

# Full no-toolchain pipeline in the library:
#   from cefont import from_ttf, serialize_fontpack, make_appvar
#   font = from_ttf("Bungee-Regular.ttf", 16, weight="bold")
#   pack = serialize_fontpack([font], name="Bungee", version="1")
#   blob = make_appvar("BUNGEE", pack)
#   verify_appvar(blob)               # parses it back; raises ValueError if bad
#   open("BUNGEE.8xv", "wb").write(blob)4