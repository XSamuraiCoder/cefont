"""FreeType backend: hinted 1-bit rendering, like the bitmap fonts convfont expects.

convfont itself never rasterizes outlines -- it only repacks bitmaps that already
exist (Windows .FNT raster fonts or its text format) and keeps their widths as
given. Those hand-made / hinted bitmaps are where the crisp 7-9px glyphs come
from. This module talks to FreeType directly (freetype-py) to rasterize any TTF/OTF:

  hinting   "auto"    FreeType's autohinter (FORCE_AUTOHINT): snaps stems and
                      heights to whole pixels; usually the crispest result
            "native"  the font's own bytecode hints only (NO_AUTOHINT)
            "default" FreeType's default hinting choice
            "none"    no hinting, plain 1-bit scan conversion
  advance   how the unhinted ("linear") advance is rounded to whole px:
            "ceil" never narrower than the font intends, "round", "floor";
            "hinted" uses FreeType's own hinted advance

Optional dependency:  pip install freetype-py
"""
from __future__ import annotations

import math

HINTINGS = ("auto", "native", "default", "none")
ADVANCES = ("ceil", "round", "floor", "hinted")


def available() -> bool:
    try:
        import freetype  # noqa: F401
        return True
    except Exception:
        return False


def _flags(ft, hinting):
    if hinting not in HINTINGS:
        raise ValueError(f"hinting must be one of {', '.join(HINTINGS)}")
    f = ft.FT_LOAD_TARGET_MONO | ft.FT_LOAD_RENDER
    return f | {"auto": ft.FT_LOAD_FORCE_AUTOHINT, "native": ft.FT_LOAD_NO_AUTOHINT,
                "default": 0, "none": ft.FT_LOAD_NO_HINTING}[hinting]


def round_advance(linear: float, hinted: float, mode: str) -> int:
    if mode == "hinted":
        return max(1, int(round(hinted)))
    if mode == "ceil":
        return max(1, math.ceil(linear - 1e-3))
    if mode == "floor":
        return max(1, math.floor(linear + 1e-3))
    return max(1, int(round(linear)))


class Renderer:
    """Renders characters at one pixel size onto a canvas of `canvas_h` rows
    whose baseline is row `base_y` (the same frame cefont.raster uses)."""

    def __init__(self, path, size, hinting="auto", advance="ceil", overhang=1):
        try:
            import freetype
        except ImportError as e:
            raise ValueError("the freetype engine needs freetype-py: pip install freetype-py") from e
        if advance not in ADVANCES:
            raise ValueError(f"advance must be one of {', '.join(ADVANCES)}")
        self._ft = freetype
        self.face = freetype.Face(str(path))
        self.face.set_pixel_sizes(0, size)
        self.flags = _flags(freetype, hinting)
        self.advance, self.overhang = advance, overhang

    def metrics(self):
        metrics = self.face.size
        return (max(0, math.ceil(metrics.ascender / 64)),
                max(0, math.ceil(-metrics.descender / 64)))

    def has(self, cp: int) -> bool:
        return self.face.get_char_index(cp) != 0

    def render(self, cp, canvas_h, base_y):
        """-> (rows of bools, width). Ink may overhang the advance by `overhang` px."""
        self.face.load_char(chr(cp), self.flags)
        g = self.face.glyph
        bm = g.bitmap
        adv = round_advance(g.linearHoriAdvance / 65536.0, g.advance.x / 64.0, self.advance)
        left, top, bw, bh = g.bitmap_left, g.bitmap_top, bm.width, bm.rows
        lo = -min(self.overhang, max(0, -left))
        hi = adv + min(self.overhang, max(0, left + bw - adv))
        w = max(1, hi - lo)
        rows = [[False] * w for _ in range(canvas_h)]
        buf = bm.buffer
        for r in range(bh):
            y = base_y - top + r
            if not 0 <= y < canvas_h:
                continue
            base = r * bm.pitch
            for c in range(bw):
                if (buf[base + (c >> 3)] >> (7 - (c & 7))) & 1:
                    x = left - lo + c
                    if 0 <= x < w:
                        rows[y][x] = True
        return rows, w
