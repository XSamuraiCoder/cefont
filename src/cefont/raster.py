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


def _raster_char(font, big, scale, ch, canvas_h, base_y, margin, mono, thr,
                 overhang=1):
    """Rasterize one character to rows of booleans (True = ink).

    scale > 1 draws the glyph `scale` times larger (`big` is the font at that
    size), box-filters it down to the target size so every output pixel holds
    its exact fractional coverage (0-255), then thresholds that. This keeps thin
    strokes and curves far better than rendering and thresholding at 1x.
    """
    if scale > 1:
        adv = round(big.getlength(ch) / scale)
        w = adv + 2 * margin
        hi_img = Image.new("L", (w * scale, canvas_h * scale), 0)
        draw = ImageDraw.Draw(hi_img)
        draw.fontmode = "L"
        draw.text((margin * scale, base_y * scale), ch, font=big, fill=255, anchor="ls")
        img = hi_img.resize((w, canvas_h), Image.BOX)
    else:
        adv = round(font.getlength(ch))
        w = adv + 2 * margin
        img = Image.new("L", (w, canvas_h), 0)
        draw = ImageDraw.Draw(img)
        draw.fontmode = "1" if mono else "L"
        draw.text((margin, base_y), ch, font=font, fill=255, anchor="ls")
    px = img.load()
    bits = [[px[x, y] >= thr for x in range(w)] for y in range(canvas_h)]
    ink = [x for x in range(w) if any(row[x] for row in bits)]
    # FontLibC has no per-glyph offsets and glyphs are simply placed side by
    # side, so ink that hangs outside the advance width can't overlap the
    # neighbouring glyph like it does in the TTF; it just widens the glyph and
    # opens up the spacing. Script fonts (Lobster, ...) overhang a lot, so only
    # allow `overhang` px of ink beyond the advance on each side; the rest is
    # clipped. Use a large value (e.g. 99) for the old, unclamped behaviour.
    if ink:
        lo = max(margin - overhang, min(margin, ink[0]))
        hi = min(margin + adv + overhang, max(margin + adv, ink[-1] + 1))
    else:
        lo, hi = margin, margin + adv
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


DEFAULT_SUPERSAMPLE = 8
DEFAULT_THRESHOLD = 100  # ~39% pixel coverage: keeps hairlines without bloating stems


def from_ttf(path, size: int, *, codepoints=None, mono: bool = False,
             threshold: int = DEFAULT_THRESHOLD, tracking: int = 0,
             weight: str = "normal", style: str = "sans-serif",
             space_above: int = 0, space_below: int = 0, trim: bool = True,
             overhang: int = 1, supersample: int = DEFAULT_SUPERSAMPLE,
             warn=None) -> Font:
    """Render a TTF/OTF at `size` pixels into a Font.

    Default (mono=False) is the high-quality path: each glyph is drawn
    `supersample` times larger, box-filtered down to `size`, and a pixel is set
    when its coverage is >= `threshold` (0-255). supersample=1 skips this and
    thresholds a plain antialiased render. mono=True uses FreeType's hinted
    1-bit renderer instead (crisp on plain sans fonts, poor on script fonts).
    `overhang` is how many px of ink may stick out
    past a glyph's advance width on each side before it is clipped (keeps the
    font's intended spacing). `warn(msg)` is called for skipped characters.
    """
    path = Path(path)
    codepoints = list(codepoints) if codepoints is not None else list(range(32, 127))
    pil_font = ImageFont.truetype(str(path), size, layout_engine=ImageFont.Layout.BASIC)
    thr = 128 if mono else max(1, min(255, threshold))
    scale = 1 if mono else max(1, int(supersample))
    big = (ImageFont.truetype(str(path), size * scale,
                              layout_engine=ImageFont.Layout.BASIC)
           if scale > 1 else None)
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
        rows, w = _raster_char(pil_font, big, scale, chr(cp), canvas_h, base_y,
                               margin, mono, thr, overhang)
        if tracking:
            new_w = max(1, w + tracking)
            if tracking > 0:
                rows = [r + [False] * tracking for r in rows]
            else:  # negative tracking: trim columns so rows stay `w` wide
                rows = [r[:new_w] for r in rows]
            w = new_w
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
        rows, _ = _raster_char(pil_font, big, scale, ch, canvas_h, base_y, margin,
                               mono, thr, overhang)
        r = _first_ink_row(rows[top:bottom + 1])
        return r if r is not None else 0

    font = Font(height=height, baseline=base_y - top, cap_height=ink_top("H"),
                x_height=ink_top("x"), weight=weight, style=style,
                space_above=space_above, space_below=space_below,
                name=f"{path.stem} {size}px")
    for cp, (rows, w) in raw.items():
        font.glyphs[cp] = Glyph(w, to_rows(rows))
    return font