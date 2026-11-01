# cefont

Turn TTF/OTF fonts into [FontLibC](https://ce-programming.github.io/toolchain/libraries/fontlibc.html)
fonts for the TI-84 Plus CE.

```bash
pip install cefont            # Python 3.11+; includes FreeType and Skia
```

## Command line

One step, TTF straight to a calculator appvar (archived `.8xv`):

```bash
cefont build Bungee-Regular.ttf                    # -> BUNGEE.8xv, size 16
```

Send `BUNGEE.8xv` to the calculator, then in C: `fontlib_GetFontByIndex("BUNGEE", 0)`.
Needs `convfont` (CE C toolchain) on PATH or in `$CEDEV/bin`.

```bash
cefont build Font.ttf -s 12,16 --name MYFONT       # two sizes in one pack
cefont build a.ttf b.ttf                           # several fonts in one pack
cefont build Font.ttf --keep-fnt --carray font.inc # also keep .fnt / write a C array
```

Smaller steps:

```bash
cefont convert Bungee-Regular.ttf -s 16 --weight bold -o bungee16.fnt --preview "FILES"
cefont preview bungee16.fnt -t "Settings"
```

`build` and `convert` use FreeType's hinted 1-bit renderer by default for fast,
crisp glyphs. `--mode aa` selects Skia antialiasing instead. Both trim the
height to the ink and set baseline / cap height / x-height.
FontLibC glyphs are 8-bit, so `--range` only accepts code points 0-255.

Skia antialiasing supports supersampling, threshold tuning, glyph refinement,
and horizontal stretching:

```bash
cefont build Bungee-Regular.ttf --mode aa -s 12
```

`--weight` only sets FontLibC metadata; it does not embolden the TTF's pixels.
Choose an actual bold TTF file for bold glyph shapes.

The Python package also reads legacy Windows raster `.FNT` files accepted by
CEdev `convfont`, in addition to convfont text `.fnt` files, and writes compatible
font binaries, font packs, and calculator appvars without invoking the CE toolchain.

## Library

```python
from cefont import from_ttf, Font

font = from_ttf("Bungee-Regular.ttf", 16, weight="bold")
print("\n".join(font.render("Hello")))     # list of '#'/' ' rows, font.height tall
open("bungee16.fnt", "w").write(font.to_fnt())
font2 = Font.from_fnt(open("bungee16.fnt").read())   # read it back
font.text_width("Hello"), font.baseline, font.glyphs[ord("A")].rows
```

The `Font` / `Glyph` model is what a font tester can build on.

## Tests

If you have the directory locally and want to run tests:

```bash
pip install -e .[dev] && pytest
```
