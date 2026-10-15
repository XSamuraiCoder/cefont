# cefont

Turn TTF/OTF fonts into [FontLibC](https://ce-programming.github.io/toolchain/libraries/fontlibc.html)
fonts for the TI-84 Plus CE.

```bash
pip install cefont            # needs Python 3.9 + Pillow (fontTools optional)
```

## Command line

One step, TTF straight to a calculator appvar (archived `.8xv`):

```bash
cefont build Bungee-Regular.ttf --weight bold      # -> BUNGEE.8xv, size 16
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

`build` and `convert` render hinted 1-bit glyphs (`--mode aa` renders antialiased and
thresholds it), trim the height to the ink, and set baseline / cap height / x-height.
FontLibC glyphs are 8-bit, so `--range` only accepts code points 0-255.

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
