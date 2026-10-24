"""cefont - TTF/OTF -> FontLibC fonts for the TI-84 Plus CE.

Pure Python: no CE toolchain, no compiler. Convert and package fonts
straight to .8xv appvars with only Pillow installed.
"""
from .fontpack import (byte_columns, serialize_font, serialize_fontpack,
                      to_c_array, WEIGHTS, STYLES)
from .model import Font, Glyph
from .raster import from_ttf, parse_ranges
from .tools import appvar_name, make_appvar

__all__ = [
    "Font", "Glyph", "from_ttf", "parse_ranges",
    # fontpack (no-CEdev serialization)
    "serialize_font", "serialize_fontpack", "to_c_array",
    "byte_columns", "WEIGHTS", "STYLES",
    # appvar packaging
    "appvar_name", "make_appvar",
]
__version__ = "1.1.0"

# Full no-toolchain pipeline in the library:
#   from cefont import from_ttf, serialize_fontpack, make_appvar
#   font = from_ttf("Bungee-Regular.ttf", 16, weight="bold")
#   pack = serialize_fontpack([font], name="Bungee", version="1")
#   open("BUNGEE.8xv", "wb").write(make_appvar("BUNGEE", pack))