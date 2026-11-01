"""Skia antialiased rasterizer."""
from __future__ import annotations

from .raster import _raster_char
from .skiautils import Typeface


class Renderer:
    """Draw individual glyphs with Skia and box-filter to the target grid."""

    def __init__(self, path, size, scale, threshold, overhang, advance):
        self.face = Typeface(path)
        self.size = size
        self.scale = scale
        self.threshold = threshold
        self.overhang = overhang
        self.advance = advance

    def has(self, cp: int) -> bool:
        return self.face.has(cp)

    def render(self, cp, canvas_h, base_y, margin, *, threshold=None, refine=0.0,
               xscale=1.0, xoff=0.0, yoff=0.0):
        rows, width, _, _ = _raster_char(
            self.face, self.size, self.scale, chr(cp), canvas_h, base_y, margin,
            False, self.threshold if threshold is None else threshold,
            self.overhang, refine, xscale, xoff, yoff, self.advance)
        return rows, width
