"""Rasterize TTF/OTF outlines into a bitmap Font."""
from __future__ import annotations

import math
from pathlib import Path

from . import ftengine
from .model import Font, Glyph
from .skiautils import Typeface

MAX_GLYPH_WIDTH = 24  # FontLibC / convfont limit


RANGE_ALIASES = {"ascii": "32-126", "latin1": "32-126,160-255", "all": "0-255"}


def parse_ranges(spec: str) -> list[int]:
    """'32-126,160-255' -> sorted code points. FontLibC fonts are 8-bit.

    Also accepts the names 'ascii' (32-126), 'latin1' (32-126,160-255) and
    'all' (0-255), and hex numbers like 0x20-0x7E.
    """
    spec = RANGE_ALIASES.get(spec.strip().lower(), spec)
    cps: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        lo, sep, hi = part.partition("-")
        try:
            lo_v = int(lo, 0)
            hi_v = int(hi, 0) if sep and hi.strip() else lo_v
        except ValueError:
            raise ValueError(f"bad range {part!r}: use numbers like 32-126 or 0x20-0x7E, "
                             "or one of ascii, latin1, all") from None
        if not 0 <= lo_v <= hi_v <= 255:
            raise ValueError(f"bad range {part!r}: code points must be 0-255 and low-high")
        cps.update(range(lo_v, hi_v + 1))
    if not cps:
        raise ValueError("the code point range is empty")
    return sorted(cps)


def parse_sizes(spec) -> list[int]:
    """'12', '8,12,16' or '8-12' (and mixes) -> sorted unique pixel sizes."""
    if isinstance(spec, int):
        spec = str(spec)
    sizes: set[int] = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        lo, sep, hi = part.partition("-")
        try:
            lo_v = int(lo)
            hi_v = int(hi) if sep else lo_v
        except ValueError:
            raise ValueError(f"bad size {part!r}: use e.g. 16, 12,16 or 8-12") from None
        if not 1 <= lo_v <= hi_v <= 255:
            raise ValueError(f"bad size {part!r}: sizes must be 1-255 and low-high")
        sizes.update(range(lo_v, hi_v + 1))
    if not sizes:
        raise ValueError("no sizes given")
    return sorted(sizes)


def font_codepoints(path) -> set[int] | None:
    """Code points the font file covers, or None if fontTools isn't installed."""
    try:
        from fontTools.ttLib import TTFont
        return set(TTFont(str(path), fontNumber=0).getBestCmap())
    except Exception:
        return None


DEFAULT_REFINE_SIGMA = 0.6  # blur radius (px) of the "eye model" used to score a bitmap
DEFAULT_REFINE_BOOST = 1.25  # >1 biases toward heavier ink so thin strokes stay connected


def _kernels(sigma):
    """1-D gaussian -> 2-D kernel K and K*K (the kernel of G^T G)."""
    r = max(1, int(2.5 * sigma + 0.999))
    g = [2.718281828 ** (-(i * i) / (2 * sigma * sigma)) for i in range(-r, r + 1)]
    t = sum(g)
    g = [v / t for v in g]
    K = [[a * b for b in g] for a in g]
    r2 = 2 * r
    n = 2 * r2 + 1
    K2 = [[0.0] * n for _ in range(n)]
    for i in range(-r2, r2 + 1):
        for j in range(-r2, r2 + 1):
            acc = 0.0
            for a in range(-r, r + 1):
                for b in range(-r, r + 1):
                    ya, xb = i - a, j - b
                    if -r <= ya <= r and -r <= xb <= r:
                        acc += K[a + r][b + r] * K[ya + r][xb + r]
            K2[i + r2][j + r2] = acc
    return r2, K2


HEAVY_STROKE = 1.6  # px; glyphs with strokes this thick skip refinement
_KCACHE: dict = {}
REFINE_BOOST = [DEFAULT_REFINE_BOOST]  # module-level knob, set by from_ttf


def _adaptive_boost(ref, boost):
    """Scale the weight bias by how thin the glyph's strokes are.

    Stroke width ~ 2*area/perimeter of the coverage map. Thin strokes (<= 1px)
    get the full `boost` so they stay connected; heavy strokes (>= 2px) get
    none, since boosting them just clogs counters and grows spurs.
    """
    area = sum(sum(r) for r in ref)
    h, w = len(ref), len(ref[0])
    per = 0.0
    for y in range(h):
        for x in range(w):
            if y + 1 < h:
                per += abs(ref[y][x] - ref[y + 1][x])
            if x + 1 < w:
                per += abs(ref[y][x] - ref[y][x + 1])
    if per <= 0:
        return 1.0, 0.0
    sw = 2 * area / per
    t = min(1.0, max(0.0, (2.0 - sw) / 1.0))  # 1 at <=1px strokes, 0 at >=2px
    return 1.0 + (boost - 1.0) * t, sw


def _refine(ref, bits, sigma, max_flips=400, boost=1.0):
    """Greedy pixel-flip refinement of a 1-bit glyph.

    `ref` is the exact fractional coverage (0..1) of each pixel, `bits` the
    thresholded starting bitmap. A plain threshold decides every pixel on its
    own, which snaps thin diagonals/curves off and fattens corners. Here the
    bitmap is instead scored the way the eye sees it: both images are blurred
    with a small gaussian and the squared difference is minimised, flipping the
    single best pixel each step until no flip helps. Strokes stay connected,
    stroke weight matches the outline, and no pixel is set that the outline
    doesn't justify.
    """
    h, w = len(bits), len(bits[0])
    key = round(sigma, 3)
    if key not in _KCACHE:
        _KCACHE[key] = _kernels(sigma)
    r2, K2 = _KCACHE[key]
    k0 = K2[r2][r2]
    boost, sw = _adaptive_boost(ref, boost)
    if sw >= HEAVY_STROKE:  # heavy glyph: a plain threshold is already clean
        return bits
    ref = [[min(1.0, v * boost) for v in row] for row in ref]
    D = [[(1.0 if bits[y][x] else 0.0) - ref[y][x] for x in range(w)] for y in range(h)]
    Q = [[0.0] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            acc = 0.0
            for dy in range(max(-r2, -y), min(r2, h - 1 - y) + 1):
                row, krow = D[y + dy], K2[dy + r2]
                for dx in range(max(-r2, -x), min(r2, w - 1 - x) + 1):
                    acc += row[x + dx] * krow[dx + r2]
            Q[y][x] = acc
    bits = [list(r) for r in bits]
    # only pixels near ink or near outline coverage can usefully change
    cand = [(y, x) for y in range(h) for x in range(w)
            if ref[y][x] > 0.02 or any(bits[yy][xx]
               for yy in range(max(0, y - 1), min(h, y + 2))
               for xx in range(max(0, x - 1), min(w, x + 2)))]
    for _ in range(max_flips):
        best, bp = -1e-9, None
        for y, x in cand:
            d = -1.0 if bits[y][x] else 1.0
            delta = 2 * d * Q[y][x] + k0
            if delta < best:
                best, bp = delta, (y, x, d)
        if bp is None:
            break
        y, x, d = bp
        bits[y][x] = not bits[y][x]
        for dy in range(max(-r2, -y), min(r2, h - 1 - y) + 1):
            qrow, krow = Q[y + dy], K2[dy + r2]
            for dx in range(max(-r2, -x), min(r2, w - 1 - x) + 1):
                qrow[x + dx] += d * krow[dx + r2]
    return bits


def _adv(x, mode):
    """Advance in whole px: 'round' (default), 'ceil' (never narrower than the
    TTF, keeps the designed gaps between letters) or 'floor'."""
    if mode == "ceil":
        return max(1, math.ceil(x - 1e-3))
    if mode == "floor":
        return max(1, math.floor(x + 1e-3))
    return round(x)


def _raster_char(face, size, scale, ch, canvas_h, base_y, margin, mono, thr,
                 overhang=1, refine=0.0, xscale=1.0, xoff=0.0, yoff=0.0,
                 advance="round"):
    """Rasterize one character to rows of booleans (True = ink).

    Returns (rows, width, lo, img): `lo` is the canvas column the rows start at
    and `img` the fractional-coverage canvas, for callers that compare against it.

    `xoff`/`yoff` slide the outline sideways / vertically by that many px
    (sub-pixel placement, used by the per-glyph tuner).

    scale > 1 draws the glyph `scale` times larger (`big` is the font at that
    size), box-filters it down to the target size so every output pixel holds
    its exact fractional coverage (0-255), then thresholds that. This keeps thin
    strokes and curves far better than rendering and thresholding at 1x.
    """
    if xscale != 1.0:
        margin = math.ceil(margin * xscale)
    adv = _adv(face.measure(ch, size) * xscale, advance)
    w = adv + 2 * margin
    coverage = face.coverage(ch, size, w, canvas_h, base_y + yoff, margin + xoff,
                             scale=scale, xscale=xscale)
    bits = [[c * 255 >= thr for c in row] for row in coverage]
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
    out = [row[lo:hi] for row in bits]
    if refine and not mono and ink:
        ref = [row[lo:hi] for row in coverage]
        out = _refine(ref, out, refine, boost=REFINE_BOOST[0])
    return out, hi - lo, lo, coverage


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


def from_ttf(path, size: int, *, codepoints=None, mono: bool | None = None,
             threshold: int | None = None, tracking: int = 0,
             weight: str = "normal", style: str = "sans-serif",
             space_above: int = 0, space_below: int = 0, trim: bool = True,
             overhang: int = 1, supersample: int = DEFAULT_SUPERSAMPLE,
             refine: float = 0.0,
             refine_boost: float = DEFAULT_REFINE_BOOST, xscale: float = 1.0,
             target_width: float | None = None, glyph_opts: dict | None = None,
             advance: str | None = None, engine: str = "auto", hinting: str = "auto",
             warn=None) -> Font:
    """Render a TTF/OTF at `size` pixels into a Font.

    The default uses FreeType's hinted monochrome renderer for fast, crisp
    glyphs. Set `mono=False` or `engine="skia"` to use Skia antialiasing:
    glyphs are drawn `supersample` times larger, box-filtered down, then
    thresholded. `mono=True` uses FreeType's hinted 1-bit renderer.
    `refine` > 0 (try DEFAULT_REFINE_SIGMA) turns on an optional pixel-refinement
    pass that smooths thin strokes; it is off by default because scored against
    the TTF outline (cefont.compare.score_vs_ttf) a plain threshold does better.
    `overhang` is how many px of ink may stick out
    past a glyph's advance width on each side before it is clipped (keeps the
    font's intended spacing). `warn(msg)` is called for skipped characters.
    `xscale` stretches every glyph (and its advance) horizontally by that
    factor, leaving the height alone: 1.25 makes a narrow face 25% wider.
    `target_width` instead picks xscale so the average glyph width (over the
    requested code points, space excluded) is that many px, e.g. 8. Both need
    the supersampled path; they are ignored (with a warning) in mono mode.
    `advance` rounds each glyph's advance to whole px: 'round' (default for
    Skia), 'ceil' (default for freetype; never narrower than the TTF), 'floor',
    or 'hinted' (freetype engine only).
    `glyph_opts` maps a code point to (threshold, xoff[, yoff]): a per-glyph
    threshold plus a sub-pixel shift of that glyph's outline.
    `engine` is 'auto' (FreeType monochrome by default; Skia for antialiasing),
    'skia' or 'freetype'. Skia renders grayscale outlines and supports
    supersampling and thresholding. 'freetype' renders hinted 1-bit glyphs,
    with `hinting` being 'auto', 'native', 'default' or 'none'; it requires
    freetype-py.
    """
    path = Path(path)
    if not path.is_file():
        raise OSError(f"font file not found: {path}")
    if isinstance(size, bool) or int(size) != size or not 1 <= size <= 255:
        raise ValueError(f"size must be a whole number of pixels, 1-255 (got {size!r})")
    size = int(size)
    if not 1 <= int(supersample) <= 32:
        raise ValueError("supersample must be 1-32")
    if threshold is not None and not 1 <= threshold <= 255:
        raise ValueError("threshold must be 1-255")
    if xscale <= 0:
        raise ValueError("xscale must be positive")
    REFINE_BOOST[0] = refine_boost
    if engine not in ("auto", "freetype", "skia"):
        raise ValueError("engine must be 'auto', 'freetype' or 'skia'")
    if advance not in (None, "round", "ceil", "floor", "hinted"):
        raise ValueError("advance must be 'round', 'ceil', 'floor' or 'hinted'")
    if advance == "hinted" and engine != "freetype":
        raise ValueError("advance='hinted' needs engine='freetype'")
    if mono is None:
        needs_skia = (engine == "skia" or
                      (engine == "auto" and
                       (threshold is not None or refine or xscale != 1.0 or target_width
                        or glyph_opts or supersample != DEFAULT_SUPERSAMPLE)))
        mono = not needs_skia
    path = Path(path)
    if engine == "auto":
        if mono:
            engine = "freetype"
        else:
            engine = "skia"
    if engine == "skia" and mono:
        raise ValueError("mono mode needs the freetype engine")
    codepoints = list(codepoints) if codepoints is not None else list(range(32, 127))
    if threshold is None:
        threshold = DEFAULT_THRESHOLD
    thr = 128 if mono else max(1, min(255, threshold))
    adv_mode = advance or ("ceil" if engine == "freetype" else "round")
    face = None
    ft = None
    skia = None
    if engine == "freetype":
        if xscale != 1.0 or target_width:
            if warn:
                warn("xscale/target_width are not supported by the freetype engine; ignored")
            xscale, target_width = 1.0, None
        ft = ftengine.Renderer(path, size, hinting, advance or "ceil", overhang)
        ascent, descent = ft.metrics()
    scale = 1 if mono else max(1, int(supersample))
    if mono and (target_width or xscale != 1.0):
        if warn:
            warn("xscale/target_width need the antialiased path; ignored in mono mode")
        target_width, xscale = None, 1.0
    if (target_width or xscale != 1.0) and scale == 1:
        scale = 4                       # stretching needs a high-res source
    if engine == "skia":
        from .skiaengine import Renderer as SkiaRenderer
        skia = SkiaRenderer(path, size, scale, thr, overhang, adv_mode)
        face = skia.face
        ascent, descent = face.metrics(size)
    if target_width:
        if target_width <= 0:
            raise ValueError("target_width must be positive")
        advs = [face.measure(chr(c), size) for c in codepoints if c != 32]
        advs = [a for a in advs if a > 0]
        if not advs:
            raise ValueError("can't measure widths for target_width")
        xscale = target_width * len(advs) / sum(advs)
    xscale = round(max(0.25, min(4.0, float(xscale))), 3)
    pad = max(2, size // 4)
    canvas_h = ascent + descent + 2 * pad
    base_y = ascent + pad
    margin = size

    gopts = glyph_opts or {}

    def render(cp):
        """One character -> (rows of bools over the whole canvas, width)."""
        if ft is not None:
            return ft.render(cp, canvas_h, base_y)
        o = gopts.get(cp)
        g_thr, g_x, g_y = ((o[0], o[1], o[2] if len(o) > 2 else 0.0) if o
                           else (thr, 0.0, 0.0))
        return skia.render(cp, canvas_h, base_y, margin, threshold=g_thr,
                           refine=refine, xscale=xscale, xoff=g_x, yoff=g_y)

    has_glyph = face.has if face is not None else ft.has
    have = {cp for cp in codepoints if has_glyph(cp) or cp == 32}
    raw: dict[int, tuple[list, int]] = {}
    for cp in codepoints:
        if have is not None and cp not in have and cp != 32:
            if warn:
                warn(f"font has no glyph for U+{cp:04X}, skipping")
            continue
        rows, w = render(cp)
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
        rows, _ = render(ord(ch))
        r = _first_ink_row(rows[top:bottom + 1])
        return r if r is not None else 0

    font = Font(height=height, baseline=base_y - top, cap_height=ink_top("H"),
                x_height=ink_top("x"), weight=weight, style=style,
                space_above=space_above, space_below=space_below,
                name=f"{path.stem} {size}px", xscale=xscale, engine=engine,
                hinting=hinting if engine == "freetype" else "")
    for cp, (rows, w) in raw.items():
        font.glyphs[cp] = Glyph(w, to_rows(rows))
    return font