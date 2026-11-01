"""Shared Skia outline rendering and font metrics."""
from __future__ import annotations

import math
from pathlib import Path


class Typeface:
    def __init__(self, path):
        try:
            import skia
        except ImportError as e:
            raise ValueError("cefont rendering needs skia-python: pip install skia-python") from e
        self.skia = skia
        self.typeface = skia.Typeface.MakeFromFile(str(Path(path)))
        if self.typeface is None:
            raise OSError(f"Skia could not load font file: {path}")

    def has(self, codepoint: int) -> bool:
        return self.typeface.unicharToGlyph(codepoint) != 0

    def measure(self, text: str, size: float) -> float:
        return self.skia.Font(self.typeface, size).measureText(text)

    def metrics(self, size: float) -> tuple[int, int]:
        metrics = self.skia.Font(self.typeface, size).getMetrics()
        return math.ceil(-metrics.fAscent), math.ceil(metrics.fDescent)

    def bounds(self, text: str, size: float):
        font = self.skia.Font(self.typeface, size)
        glyphs = font.textToGlyphs(text)
        if not glyphs:
            return self.skia.Rect.MakeEmpty()
        bounds = font.getBounds(glyphs)
        result = self.skia.Rect.MakeEmpty()
        for bound in bounds:
            result.join(bound)
        return result

    def coverage(self, text: str, size: float, width: int, height: int,
                 baseline: float, origin_x: float, *, scale: int = 1,
                 xscale: float = 1.0) -> list[list[float]]:
        """Render grayscale coverage and box-filter it to the target grid."""
        skia = self.skia
        surface = skia.Surface(max(1, width * scale), max(1, height * scale))
        if surface is None:
            raise ValueError("Skia could not allocate a glyph surface")
        canvas = surface.getCanvas()
        canvas.clear(skia.ColorTRANSPARENT)
        font = skia.Font(self.typeface, size * scale)
        font.setSubpixel(True)
        paint = skia.Paint(Color=skia.ColorWHITE, AntiAlias=True)
        canvas.save()
        canvas.translate(origin_x * scale, baseline * scale)
        canvas.scale(xscale, 1.0)
        canvas.drawString(text, 0, 0, font, paint)
        canvas.restore()

        pixels = surface.makeImageSnapshot().tobytes()
        output = [[0.0] * width for _ in range(height)]
        area = scale * scale
        for y in range(height):
            for x in range(width):
                total = 0
                for sy in range(y * scale, (y + 1) * scale):
                    start = (sy * width * scale + x * scale) * 4 + 3
                    total += sum(pixels[start + sx * 4] for sx in range(scale))
                output[y][x] = total / (255.0 * area)
        return output
