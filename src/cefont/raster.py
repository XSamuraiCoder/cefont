"""Rasterize TTF/OTF outlines into a bitmap Font."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .model import Font, Glyph

MAX_GLYPH_WIDTH = 24  # FontLibC / convfont limit


def parse_ranges(spec: str) -> list[int]:
    """'32-126,160-255' -> sorted code points. FontLibC fonts are 8-bit."""
    cps: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        lo = int(lo, 0)
        hi = int(hi, 0) if hi else lo
        if not 0 <= lo <= hi <= 255:
            raise ValueError(f"bad range {part!r}: code points must be 0-255")
        cps.update(range(lo, hi + 1))
    return sorted(cps)


def font_codepoints(path) -> set[int] | None:
    """Code points the font file covers, or None if fontTools isn't installed."""
    try:
        from fontTools.ttLib import TTFont
        return set(TTFont(str(path), fontNumber=0).getBestCmap())
    except Exception:
        return None


def _raster_char(font, ch, canvas_h, base_y, margin, mono, thr):
    adv = round(font.getlength(ch))
    w = adv + 2 * margin
    img = Image.new("L", (w, canvas_h), 0)
    draw = ImageDraw.Draw(img)
    draw.fontmode = "1" if mono else "L"
    draw.text((margin, base_y), ch, font=font, fill=255, anchor="ls")
    bits = [[img.getpixel((x, y)) >= thr for x in range(w)] for y in range(canvas_h)]
    ink = [x for x in range(w) if any(row[x] for row in bits)]
    lo = min(margin, ink[0]) if ink else margin
    hi = max(margin + adv, ink[-1] + 1) if ink else margin + adv
    if hi == lo:
        hi += 1  # a glyph needs at least one column
    return [row[lo:hi] for row in bits], hi - lo


def _first_ink_row(rows):
    for i, row in enumerate(rows):
        if any(row):
            return i
    return None


def _last_ink_row(rows):
    r = _first_ink_row(rows[::-1])
    return None if r is None else len(rows) - 1 - r


def from_ttf(path, size: int, *, codepoints=None, mono: bool = True,
             threshold: int = 128, tracking: int = 0, weight: str = "normal",
             style: str = "sans-serif", space_above: int = 0, space_below: int = 0,
             trim: bool = True, warn=None) -> Font:
    """Render a TTF/OTF at `size` pixels into a Font.

    mono=True uses hinted 1-bit rendering; mono=False renders antialiased and
    thresholds at `threshold`. `warn(msg)` is called for skipped characters.
    """
    path = Path(path)
    codepoints = list(codepoints) if codepoints is not None else list(range(32, 127))
    pil_font = ImageFont.truetype(str(path), size, layout_engine=ImageFont.Layout.BASIC)
    thr = 128 if mono else max(1, min(255, threshold))
    ascent, descent = pil_font.getmetrics()
    pad = max(2, size // 4)
    canvas_h = ascent + descent + 2 * pad
    base_y = ascent + pad
    margin = size

    have = font_codepoints(path)
    raw: dict[int, tuple[list, int]] = {}
    for cp in codepoints:
        if have is not None and cp not in have and cp != 32:
            if warn:
                warn(f"font has no glyph for U+{cp:04X}, skipping")
            continue
        rows, w = _raster_char(pil_font, chr(cp), canvas_h, base_y, margin, mono, thr)
        if tracking:
            rows = [r + [False] * tracking for r in rows]
            w += tracking
        raw[cp] = (rows, w)
    if not raw:
        raise ValueError("no glyphs produced")

    too_wide = sorted(cp for cp, (_, w) in raw.items() if w > MAX_GLYPH_WIDTH)
    if too_wide:
        shown = ", ".join(f"{chr(cp)!r} ({raw[cp][1]}px)" for cp in too_wide[:4])
        raise ValueError(
            f"at {size}px these glyphs are wider than FontLibC's {MAX_GLYPH_WIDTH}px limit: "
            f"{shown}{' ...' if len(too_wide) > 4 else ''}. Use a smaller size")

    tops = [t for t in (_first_ink_row(r) for r, _ in raw.values()) if t is not None]
    bottoms = [b for b in (_last_ink_row(r) for r, _ in raw.values()) if b is not None]
    if not trim or not tops:
        top, bottom = pad, pad + ascent + descent - 1
    else:
        top, bottom = min(tops), max(bottoms)
    height = bottom - top + 1
    if not 0 < height <= 255:
        raise ValueError(f"font height {height} is out of range")

    def to_rows(rows):
        return ["".join("#" if b else " " for b in r) for r in rows[top:bottom + 1]]

    def ink_top(ch):
        rows, _ = _raster_char(pil_font, ch, canvas_h, base_y, margin, mono, thr)
        r = _first_ink_row(rows[top:bottom + 1])
        return r if r is not None else 0

    font = Font(height=height, baseline=base_y - top, cap_height=ink_top("H"),
                x_height=ink_top("x"), weight=weight, style=style,
                space_above=space_above, space_below=space_below,
                name=f"{path.stem} {size}px")
    for cp, (rows, w) in raw.items():
        font.glyphs[cp] = Glyph(w, to_rows(rows))
    return font