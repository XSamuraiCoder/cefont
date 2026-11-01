"""Compare a candidate bitmap Font against a reference and accept it at a match %.

Glyphs are aligned by origin (left edge) and baseline, so fonts with different
heights or padding still line up. The score is pixel overlap (intersection over
union of ink pixels) pooled over every glyph both fonts have, so big glyphs
count for more than a full stop. A glyph whose advance width differs from the
reference also counts its extra/missing columns (times the font height) as
non-overlapping, so a font with the right pixels but the wrong spacing doesn't
score 100. The pure pixel overlap is reported separately as `ink`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .model import Font
from .skiautils import Typeface


@dataclass
class Match:
    score: float                      # pooled overlap incl. width differences, 0-100
    ink: float = 0.0                  # pooled overlap of ink pixels only, 0-100
    width_match: float = 0.0          # % of shared glyphs with identical width
    compared: int = 0                 # glyphs present in both fonts
    only_ref: list = field(default_factory=list)   # code points the candidate lacks
    per_glyph: dict = field(default_factory=dict)  # cp -> overlap 0-100
    ceiling: float = 0.0              # best overlap ANY 1-bit bitmap could reach
                                      # against the same outlines at this size,
                                      # 0-100 (0 = not computed)
    basis: str = "outline"            # 'outline' (raw) or 'hinted' (FreeType's hinted shape)
    raw: object = None                # the raw-outline Match, when basis is 'hinted'

    def accepted(self, threshold: float) -> bool:
        return self.score >= threshold

    def worst(self, n: int = 5):
        return sorted(self.per_glyph.items(), key=lambda kv: kv[1])[:n]


def _ink(font: Font, cp: int) -> set:
    g = font.glyphs[cp]
    return {(x, y - font.baseline)
            for y, row in enumerate(g.rows) for x, c in enumerate(row) if c == "#"}


def compare_fonts(ref: Font, cand: Font) -> Match:
    shared = sorted(set(ref.glyphs) & set(cand.glyphs))
    inter_tot = union_tot = same_w = width_pen = 0
    per: dict[int, float] = {}
    for cp in shared:
        a, b = _ink(ref, cp), _ink(cand, cp)
        inter, union = len(a & b), len(a | b)
        dw = abs(ref.glyphs[cp].width - cand.glyphs[cp].width)
        if dw == 0:
            same_w += 1
        width_pen += dw * ref.height
        if union:                                  # skip glyphs blank in both
            per[cp] = 100.0 * inter / union
            inter_tot += inter
            union_tot += union
    ink = 100.0 * inter_tot / union_tot if union_tot else 0.0
    score = 100.0 * inter_tot / (union_tot + width_pen) if union_tot else 0.0
    wm = 100.0 * same_w / len(shared) if shared else 0.0
    return Match(score, ink, wm, len(shared),
                 sorted(set(ref.glyphs) - set(cand.glyphs)), per)


def side_by_side(ref: Font, cand: Font, ch: str, gap: int = 3) -> list[str]:
    """Reference | candidate for one character, baseline-aligned ('#' ink, 'X' = ink in only one)."""
    cp = ord(ch)
    if cp not in ref.glyphs or cp not in cand.glyphs:
        return [f"{ch!r}: not in both fonts"]
    a, b = _ink(ref, cp), _ink(cand, cp)
    pts = a | b
    if not pts:
        return [f"{ch!r}: blank"]
    y0, y1 = min(y for _, y in pts), max(y for _, y in pts)
    w = max(ref.glyphs[cp].width, cand.glyphs[cp].width)
    rows = []
    for y in range(y0, y1 + 1):
        left = "".join("#" if (x, y) in a else "." for x in range(w))
        right = "".join("#" if (x, y) in b else "." for x in range(w))
        diff = "".join("X" if ((x, y) in a) != ((x, y) in b) else " " for x in range(w))
        rows.append(f"{left}{' ' * gap}{right}{' ' * gap}{diff}".rstrip())
    return [f"{ch!r}  ref{' ' * (w - 3 + gap)}cand{' ' * (w - 4 + gap)}diff"] + rows


# ---------------------------------------------------------------------------
# Scoring against the original TTF/OTF
# ---------------------------------------------------------------------------
HI = 8          # outline is drawn this many times larger, then box-filtered
SHIFT_X = 2     # a bitmap may sit up to this many px left/right of the origin
SHIFT_Y = 1     # ...and this many px up/down (covers wrong baseline metadata)
WIDTH_SLACK = 0.5  # px of rounding a width may differ from the true advance


def _coverage(face, size, ch, canvas_w, canvas_h, base_y, origin_x, xscale=1.0):
    """Exact fractional coverage (0-1) of the outline per pixel, as rows.
    `xscale` stretches the outline horizontally (for fonts made with xscale)."""
    return face.coverage(ch, size, canvas_w, canvas_h, base_y, origin_x,
                         scale=HI, xscale=xscale)


def _pixel_ceiling(cov) -> float:
    """Best overlap with this coverage map that ANY 1-bit bitmap could reach.

    Per pixel the bitmap has two choices: ink, contributing (c, 1) to
    (intersection, union), or no ink, contributing (0, c). For a fixed number
    of inked pixels the best intersection is the sum of the largest coverages,
    so scanning k = 1..n over the values sorted downwards maximises
    P / (k + S - P) over every possible bitmap. This is the physical limit of
    a 1-bit raster at this size -- no threshold or refinement can beat it.
    """
    vals = sorted((c for row in cov for c in row if c > 0), reverse=True)
    total = sum(vals)
    best = prefix = 0.0
    for k, c in enumerate(vals, 1):
        prefix += c
        r = prefix / (k + total - prefix)
        if r > best:
            best = r
    return best


def _score_glyphs(font: Font, size: int, codepoints, cov_adv, width_slack, basis) -> Match:
    """Pooled overlap of `font` against per-glyph coverage maps.

    `cov_adv(ch, canvas_w, canvas_h, base_y, origin_x)` returns (coverage rows,
    true advance in px) for one character on a canvas whose baseline is row
    `base_y` and whose origin is column `origin_x`.
    """
    cps = sorted(set(codepoints if codepoints is not None else font.glyphs) & set(font.glyphs))
    pad = max(2, size)
    inter_tot = union_tot = width_pen = near = ceil_tot = 0.0
    per: dict[int, float] = {}
    for cp in cps:
        g = font.glyphs[cp]
        ch = chr(cp)
        cw = g.width + 2 * pad + 2 * SHIFT_X + 2
        ch_h = font.height + 2 * pad
        cov, adv = cov_adv(ch, cw, ch_h, pad + font.baseline, pad + SHIFT_X + 1)
        ink = [[1.0 if c == "#" else 0.0 for c in row] for row in g.rows]
        best = None
        for dy in range(-SHIFT_Y, SHIFT_Y + 1):
            for dx in range(-SHIFT_X, SHIFT_X + 1):
                inter = union = 0.0
                for y in range(ch_h):
                    by = y - pad - dy
                    brow = ink[by] if 0 <= by < font.height else None
                    crow = cov[y]
                    for x in range(cw):
                        bx = x - pad - SHIFT_X - 1 - dx
                        b = brow[bx] if brow is not None and 0 <= bx < g.width else 0.0
                        c = crow[x]
                        if b or c:
                            if b > c:
                                inter += c; union += b
                            else:
                                inter += b; union += c
                if best is None or (union and inter / union > best[0]):
                    best = (inter / union if union else 0.0, inter, union)
        if not best[2]:
            continue                                # blank in both (space)
        per[cp] = 100.0 * best[0]
        inter_tot += best[1]
        union_tot += best[2]
        ceil_tot += _pixel_ceiling(cov) * best[2]   # ceiling, weighted like the score
        dw = max(0.0, abs(g.width - adv) - width_slack)
        width_pen += dw * font.height
        if dw == 0:
            near += 1
    n = len(per)
    ink = 100.0 * inter_tot / union_tot if union_tot else 0.0
    score = 100.0 * inter_tot / (union_tot + width_pen) if union_tot else 0.0
    ceiling = 100.0 * ceil_tot / union_tot if union_tot else 0.0
    return Match(score, ink, 100.0 * near / n if n else 0.0, n, [], per,
                 ceiling=ceiling, basis=basis)


def score_vs_ttf(font: Font, ttf, size: int, codepoints=None) -> Match:
    """Score a bitmap font against the outlines of the TTF/OTF it came from.

    Per glyph the score is the overlap between the bitmap and the outline's
    exact pixel coverage (sum of min / sum of max, so a half-covered pixel is
    half right whichever way it is set). The bitmap is allowed a small shift
    so a font with different padding isn't punished for it. Glyphs whose width
    is more than rounding away from the true advance lose the extra columns,
    so squeezed or stretched spacing costs points. Pooled over all glyphs.
    The result's `ceiling` is the best score any 1-bit bitmap could reach
    against these outlines at this size: the score's physical limit, which
    separates "needs more tuning" from "needs more pixels".
    """
    face = Typeface(ttf)
    xs = getattr(font, "xscale", 1.0)

    def cov_adv(ch, cw, ch_h, base_y, origin_x):
        return (_coverage(face, size, ch, cw, ch_h, base_y, origin_x, xs),
                face.measure(ch, size) * xs)

    return _score_glyphs(font, size, codepoints, cov_adv, WIDTH_SLACK, "outline")


def score_vs_hinted(font: Font, ttf, size: int, codepoints=None, hinting=None) -> Match:
    """Score a bitmap font against the HINTED outline FreeType draws at this size.

    Hinting (FreeType's autohinter, or the font's own bytecode) deliberately
    snaps stems and heights to whole pixels, so the shape a hinted renderer
    shows at 8-16px is not the raw outline. This scores against that shape
    (FreeType's 8-bit gray render of the hinted outline), which is the fair
    yardstick for a hinted 1-bit font: it separates "the 1-bit conversion lost
    detail" from "hinting reshaped the glyph". It is NOT a measure of how close
    the glyph is to the designer's raw outline; use score_vs_ttf for that.
    `hinting` is 'auto', 'native', 'default' or 'none' (default: what the font
    was built with, else 'auto'). Needs freetype-py. Width slack is a full
    pixel because advances may be rounded up or down.
    """
    try:
        import freetype as ft
    except ImportError as e:
        raise ValueError("scoring against the hinted outline needs freetype-py") from e
    hinting = hinting or getattr(font, "hinting", "") or "auto"
    if hinting not in ("auto", "native", "default", "none"):
        raise ValueError(f"unknown hinting {hinting!r}")
    flags = ft.FT_LOAD_TARGET_MONO | {
        "auto": ft.FT_LOAD_FORCE_AUTOHINT, "native": ft.FT_LOAD_NO_AUTOHINT,
        "default": 0, "none": ft.FT_LOAD_NO_HINTING}[hinting]
    face = ft.Face(str(ttf))
    face.set_pixel_sizes(0, size)

    def cov_adv(ch, cw, ch_h, base_y, origin_x):
        face.load_char(ch, flags)
        face.glyph.render(ft.FT_RENDER_MODE_NORMAL)
        g = face.glyph
        bm = g.bitmap
        rows = [[0.0] * cw for _ in range(ch_h)]
        buf = bm.buffer
        for r in range(bm.rows):
            y = base_y - g.bitmap_top + r
            if not 0 <= y < ch_h:
                continue
            for c in range(bm.width):
                x = origin_x + g.bitmap_left + c
                if 0 <= x < cw:
                    rows[y][x] = buf[r * bm.pitch + c] / 255.0
        return rows, g.linearHoriAdvance / 65536.0

    return _score_glyphs(font, size, codepoints, cov_adv, 1.0, "hinted")


def score_font(font: Font, ttf, size: int, codepoints=None, basis: str = "auto",
               include_raw: bool = True) -> Match:
    """Score with the right yardstick for how the font was made.

    basis 'outline' = raw outline, 'hinted' = the hinted outline, 'auto'
    (default) = hinted for fonts made by the freetype engine, otherwise
    outline. When `include_raw` is true, the result's `raw` holds the
    raw-outline Match for reference when the hinted basis is used.
    """
    if basis not in ("auto", "outline", "hinted"):
        raise ValueError("basis must be 'auto', 'outline' or 'hinted'")
    hinted = basis == "hinted" or (basis == "auto" and getattr(font, "engine", "") == "freetype")
    if not hinted:
        return score_vs_ttf(font, ttf, size, codepoints)
    try:
        m = score_vs_hinted(font, ttf, size, codepoints)
    except ValueError:
        if basis == "hinted":
            raise
        return score_vs_ttf(font, ttf, size, codepoints)
    if include_raw:
        m.raw = score_vs_ttf(font, ttf, size, codepoints)
    return m


def side_by_side_ttf(font: Font, ttf, size: int, ch: str) -> list[str]:
    """Outline (shaded by coverage) next to the bitmap, baseline-aligned."""
    cp = ord(ch)
    if cp not in font.glyphs:
        return [f"{ch!r}: not in the font"]
    face = Typeface(ttf)
    g = font.glyphs[cp]
    pad = max(2, size)
    cw = g.width + 2 * pad
    cov = _coverage(face, size, ch, cw, font.height + 2 * pad, pad + font.baseline, pad,
                    getattr(font, "xscale", 1.0))
    rows = []
    shade = " .:-=+*#%@"
    for y in range(font.height + 2 * pad):
        by = y - pad
        left = "".join(shade[min(9, int(c * 9.99))] for c in cov[y][pad:pad + g.width])
        right = g.rows[by] if 0 <= by < font.height else " " * g.width
        right = "".join("#" if c == "#" else "." for c in right)
        if left.strip() or "#" in right:
            rows.append(f"{left}   {right}")
    return [f"{ch!r}  ttf   bitmap"] + rows