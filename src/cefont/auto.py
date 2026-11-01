"""Auto-tune rasterizer settings by comparing the bitmap with the TTF.

For one font and size, candidate settings (threshold, refinement, horizontal
stretch) are built and judged against the outline on everything measurable:

  overlap   pixel overlap with the outline's exact coverage (score_vs_ttf,
            which already charges for wrong advance widths)
  weight    total ink vs the outline's area (stem thickness / boldness)
  height    cap height and x-height in px vs the outline's
  width     mean |glyph width - true advance| in px
  shape     a small pull toward xscale 1.0 so the face isn't distorted for
            a tiny gain

`quality` = overlap minus penalties for the other four (weights below, in
score points per unit). The search is staged (threshold x refine, then
xscale, then a fine threshold pass) so it stays quick, and it judges a
representative subset of glyphs; the winning settings are then built for the
full code point list.

Then, glyph by glyph (per_glyph=True), each character may get its own
threshold and a sub-pixel placement of its outline (sideways and, since 1.9,
vertically), chosen to best match that character's own coverage map, ink
weight and width. This helps the glyphs a single global threshold fits worst
(dots, hairlines, thin strokes that straddle two pixel columns). The
per-glyph search is staged (a coarse sweep, then a fine one) and the whole
pass is only kept when it also improves the real score_vs_ttf on the tuning
sample.

The global search runs a coarse threshold x refine grid, a coarse horizontal
stretch, then two rounds of fine threshold and fine xscale passes
(coordinate ascent), so a winner like threshold 138 / xscale 1.08 gets
polished to the nearest bit of coverage instead of stopping at a grid point.
"""
from __future__ import annotations

import math
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import ftengine
from .compare import compare_fonts, score_vs_ttf
from .raster import (DEFAULT_REFINE_BOOST, DEFAULT_REFINE_SIGMA, DEFAULT_SUPERSAMPLE,
                     DEFAULT_THRESHOLD, REFINE_BOOST, _raster_char, from_ttf)
from .skiautils import Typeface

HI = 8
W_WEIGHT = 60.0    # points per 1.0 of relative ink-weight error (10% off = 6 pts)
W_HEIGHT = 8.0     # points per px of cap-height / x-height error
W_WIDTH = 6.0      # points per px of mean width error
W_SHAPE = 30.0     # points per 1.0 of |xscale - 1|
SAMPLE = [ord(c) for c in
          "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"]
THRESHOLDS = (70, 85, 100, 115, 130)
FINE_THRESHOLDS = (-8, -5, -3, -2, -1, 1, 2, 3, 5, 8)   # fine pass around the winner
XSCALES = (1.0, 1.02, 1.04, 1.06, 1.08, 1.10, 1.12, 1.14, 1.16)
FINE_XSCALES = (-0.04, -0.02, -0.01, 0.01, 0.02, 0.04)


@dataclass
class Trial:
    kwargs: dict
    quality: float
    overlap: float
    weight_ratio: float
    cap_err: float
    x_err: float
    width_err: float

    def label(self) -> str:
        k = self.kwargs
        if k.get("engine") == "freetype":
            return (f"--engine freetype --hinting {k.get('hinting', 'auto')} "
                    f"--advance {k.get('advance', 'ceil')}")
        s = f"--threshold {k['threshold']}"
        if k.get("refine"):
            s += f" --refine --refine-boost {k['refine_boost']:g}"
        if k.get("xscale", 1.0) != 1.0:
            s += f" --xscale {k['xscale']:g}"
        return s


@dataclass
class AutoResult:
    font: object
    kwargs: dict
    best: Trial
    trials: list = field(default_factory=list)
    glyph_opts: dict = field(default_factory=dict)   # cp -> (threshold, xoff, yoff)
    glyph_before: float = 0.0     # pooled per-glyph overlap %, global settings only
    glyph_after: float = 0.0      # ... after glyph-by-glyph tuning

    def report(self) -> str:
        b = self.best
        lines = [f"auto-tuned ({len(self.trials)} settings tried): {b.label()}",
                 f"  quality {b.quality:.1f}  overlap {b.overlap:.1f}%  ink weight "
                 f"{100 * b.weight_ratio:.0f}% of outline  cap-height off {b.cap_err:.1f}px  "
                 f"x-height off {b.x_err:.1f}px  width off {b.width_err:.2f}px avg"]
        if self.glyph_opts:
            lines.append(f"  per-glyph: {len(self.glyph_opts)} of {len(self.font.glyphs)} glyphs "
                         f"re-tuned, strict overlap {self.glyph_before:.1f}% -> {self.glyph_after:.1f}%")
        return "\n".join(lines)


class _Ideal:
    """What the outline says each measure should be at this size."""
    reference = None            # optional known-good bitmap Font to match instead

    def __init__(self, ttf, size, cps):
        self.face = Typeface(ttf)
        self.adv, self.area = {}, {}
        for cp in cps:
            ch = chr(cp)
            self.adv[cp] = self.face.measure(ch, size * HI) / HI
            bounds = self.face.bounds(ch, size * HI)
            if not bounds.isEmpty():
                left, top = bounds.left(), bounds.top()
                width = max(1, math.ceil(bounds.right() - left) + 2)
                height = max(1, math.ceil(bounds.bottom() - top) + 2)
                coverage = self.face.coverage(ch, size * HI, width, height,
                                              1 - top, 1 - left)
                self.area[cp] = sum(map(sum, coverage)) / (HI * HI)
            else:
                self.area[cp] = 0.0
        self.cap = -self.face.bounds("H", size * HI).top() / HI
        self.xh = -self.face.bounds("x", size * HI).top() / HI


def _evaluate(font, ttf, size, ideal, cps, width_slack=0.0) -> Trial:
    xs = font.xscale
    match = score_vs_ttf(font, ttf, size, cps)
    ink = ref = 0.0
    wsum = n = 0
    for cp in cps:
        g = font.glyphs.get(cp)
        if g is None:
            continue
        ink += sum(r.count("#") for r in g.rows)
        ref += ideal.area[cp] * xs
        if ideal.adv[cp] > 0:
            wsum += max(0.0, abs(g.width - ideal.adv[cp] * xs) - width_slack)
            n += 1
    ratio = ink / ref if ref else 1.0
    cap_err = abs((font.baseline - font.cap_height) - ideal.cap)
    x_err = abs((font.baseline - font.x_height) - ideal.xh)
    werr = wsum / n if n else 0.0
    q = (match.score - W_WEIGHT * abs(ratio - 1) - W_HEIGHT * (cap_err + x_err)
         - W_WIDTH * werr - W_SHAPE * abs(xs - 1))
    if ideal.reference is not None:       # a known-good bitmap font beats the heuristics
        ref = compare_fonts(ideal.reference, font)
        return Trial({}, ref.score, ref.ink, ratio, cap_err, x_err, werr)
    return Trial({}, q, match.score, ratio, cap_err, x_err, werr)


GLYPH_THR = (-30, -15, 0, 15, 30)           # round-1 threshold offsets per glyph
GLYPH_XOFF = (-0.5, -0.25, 0.0, 0.25, 0.5)  # round-1 sideways outline shifts (px)
GLYPH_THR_FINE = (-8, -4, -2, 2, 4, 8)      # round-2 offsets around the round-1 pick
GLYPH_XOFF_FINE = (-0.25, -0.125, 0.125, 0.25)
GLYPH_YOFF_FINE = (-0.25, 0.0, 0.25)        # sub-pixel vertical placement (round 2)
GLYPH_THR_REFINE = (-15, 0, 15)             # smaller grid when refinement is on (slow)
GLYPH_XOFF_REFINE = (-0.25, 0.0, 0.25)
W_SHIFT = 1.0      # points per px of outline shift, sideways or vertical
W_WEIGHT_G = 0.0   # per-glyph ink-weight penalty; 0 won on every font tried (60 made scores worse)
W_WIDTH_G = 2.0    # per-glyph width penalty, gentler than the global W_WIDTH so a
                   # placement that fits the pixels better can win; the sample check
                   # in autotune() reverts the whole pass if it hurts the real score


def _make_glyph_tuner(ttf, size, kw, xs):
    """Build the per-glyph search for one font/size/settings; returns tune(cp).

    tune(cp) -> None for a blank glyph, else (cp, picked_opts_or_None,
    (base_inter, base_union), (best_inter, best_union)). Module-level so worker
    processes can build their own copy.
    """
    scale = max(1, int(kw.get("supersample", DEFAULT_SUPERSAMPLE)))
    if scale == 1:
        scale = 4 if xs != 1.0 else 1
    REFINE_BOOST[0] = kw.get("refine_boost", DEFAULT_REFINE_BOOST)
    face = Typeface(ttf)
    ascent, descent = face.metrics(size)
    pad = max(2, size // 4)
    canvas_h = ascent + descent + 2 * pad
    base_y = ascent + pad
    margin = size
    base_thr = kw["threshold"]
    refine = kw.get("refine", 0.0)
    overhang = kw.get("overhang", 1)
    advmode = kw.get("advance") or "round"
    if refine:      # refinement is slow: one small sweep, no fine pass
        grids = [(GLYPH_THR_REFINE, GLYPH_XOFF_REFINE, (0.0,))]
    else:
        grids = [(GLYPH_THR, GLYPH_XOFF, (0.0,)),
                 (GLYPH_THR_FINE, GLYPH_XOFF_FINE, GLYPH_YOFF_FINE)]
    grids = grids[:max(1, kw.get("_rounds", 2))]

    def objective(rows, w, lo, cov, adv_true, xoff, yoff):
        H, W = len(cov), len(cov[0])
        inter = union = ink = csum = 0.0
        for y in range(H):
            brow = rows[y]
            for x in range(W):
                c = cov[y][x]
                bx = x - lo
                b = 1.0 if 0 <= bx < w and brow[bx] else 0.0
                if b or c:
                    if b > c:
                        inter += c; union += b
                    else:
                        inter += b; union += c
                ink += b
                csum += c
        if not union:
            return None
        ov = inter / union
        ratio = ink / csum if csum else 1.0
        score = (100.0 * ov - W_WEIGHT_G * min(1.0, abs(ratio - 1))
                 - W_WIDTH_G * abs(w - adv_true)
                 - W_SHIFT * (abs(xoff) + abs(yoff)))
        return score, inter, union

    def tune(cp):
        ch = chr(cp)
        adv_true = face.measure(ch, size) * xs
        rows, w, lo, cov = _raster_char(face, size, scale, ch, canvas_h, base_y, margin,
                                        False, base_thr, overhang, refine, xs, 0.0, 0.0,
                                        advmode)
        base = objective(rows, w, lo, cov, adv_true, 0.0, 0.0)
        if base is None:                      # blank glyph
            return None
        cur = [base_thr, 0.0, 0.0]
        best = base
        for thr_ds, x_ds, y_ds in grids:
            for dt in thr_ds:
                thr = max(1, min(255, cur[0] + dt))
                for dx in x_ds:
                    xo = round(min(0.75, max(-0.75, cur[1] + dx)), 4)
                    for dy in y_ds:
                        yo = round(min(0.5, max(-0.5, cur[2] + dy)), 4)
                        if dt == 0 and dx == 0.0 and dy == 0.0:
                            continue
                        if (thr, xo, yo) == (cur[0], cur[1], cur[2]):
                            continue
                        r2, w2, lo2, _ = _raster_char(face, size, scale, ch, canvas_h,
                                                      base_y, margin, False, thr, overhang,
                                                      refine, xs, xo, yo, advmode)
                        o = objective(r2, w2, lo2, cov, adv_true, xo, yo)
                        if o and o[0] > best[0] + 0.05:
                            best, cur = o, [thr, xo, yo]
        picked = tuple(cur) if tuple(cur) != (base_thr, 0.0, 0.0) else None
        return cp, picked, (base[1], base[2]), (best[1], best[2])

    return tune


_WORKER: dict = {}


def _worker_init(args):
    _WORKER["tune"] = _make_glyph_tuner(*args)


def _worker_run(cp):
    return _WORKER["tune"](cp)


def _tune_glyphs(ttf, size, kw, font, cps, progress=None, rounds=2, jobs=1):
    """Pick (threshold, xoff, yoff) per glyph; returns (opts, overlap_before, overlap_after).

    The search is staged per glyph: a coarse threshold/shift sweep around the
    global settings, then (when refinement is off, because it is slow) a fine
    pass around the coarse winner that also tries a sub-pixel vertical
    placement. Every candidate is scored against that glyph's own unshifted
    coverage map, so a shift only wins if it really fits the pixels better.
    With jobs > 1 the glyphs are tuned in that many worker processes.
    """
    xs = font.xscale
    scale = max(1, int(kw.get("supersample", DEFAULT_SUPERSAMPLE)))
    if (scale == 1 and xs == 1.0) or kw.get("mono") or kw.get("engine") == "freetype":
        return {}, 0.0, 0.0
    kw = dict(kw, _rounds=rounds)
    todo = [cp for cp in cps if cp in font.glyphs and cp != 32]
    results = []
    if jobs and jobs > 1 and len(todo) > 8:
        try:
            with ProcessPoolExecutor(max_workers=jobs, initializer=_worker_init,
                                     initargs=((str(ttf), size, kw, xs),)) as ex:
                for n, r in enumerate(ex.map(_worker_run, todo, chunksize=4)):
                    if progress and n % 16 == 0:
                        progress(f"glyph {n + 1}/{len(todo)} ({jobs} processes)")
                    results.append(r)
        except Exception as e:                # no fork/spawn available: do it serially
            if progress:
                progress(f"parallel tuning unavailable ({e.__class__.__name__}); running serially")
            results = []
    if not results:
        tune = _make_glyph_tuner(ttf, size, kw, xs)
        for n, cp in enumerate(todo):
            if progress and n % 16 == 0:
                progress(f"glyph {n + 1}/{len(todo)}")
            results.append(tune(cp))
    opts: dict = {}
    b_i = b_u = a_i = a_u = 0.0
    for r in results:
        if r is None:
            continue
        cp, picked, (bi, bu), (ai, au) = r
        b_i += bi; b_u += bu; a_i += ai; a_u += au
        if picked:
            opts[cp] = picked
    return (opts, 100.0 * b_i / b_u if b_u else 0.0, 100.0 * a_i / a_u if a_u else 0.0)


def autotune(ttf, size: int, *, codepoints=None, base: dict | None = None,
             tune_threshold: bool = True, tune_xscale: bool = True,
             max_xscale: float = 1.16, per_glyph: bool = True,
             glyph_rounds: int = 2, jobs: int = 1, engines=("skia", "freetype"),
             reference=None, progress=None) -> AutoResult:
    """Pick threshold / refine / xscale for `ttf` at `size` px.

    `base` holds other from_ttf keywords (tracking, weight, overhang ...) that
    are kept as given. With tune_threshold=False an explicit `threshold` in
    `base` is kept as given and everything else is tuned around it. With
    tune_xscale=False any xscale/target_width in `base` is kept as it is.
    `per_glyph` adds the glyph-by-glyph pass; `glyph_rounds` is how many staged
    sweeps it does (2 = coarse, then fine with sub-pixel vertical placement).
    `engines` lists the rasterizers it may choose from: 'skia' (threshold on a
    supersampled render) and 'freetype' (hinted 1-bit glyphs through FreeType,
    needs freetype-py; skipped when a threshold or stretch was fixed by hand).
    `jobs` > 1 tunes glyphs in that many processes.
    `reference` is a known-good bitmap Font of the same face (e.g. a convfont
    .fnt): settings are then chosen to match it glyph for glyph (pixels and
    widths) instead of the outline heuristics, which is how to steer the choice
    toward a hinted, hand-built look. Glyph origins must line up with the
    reference's (compare_fonts aligns by origin and baseline).
    Raises ValueError if nothing can be built.
    """
    ttf = Path(ttf)
    base = dict(base or {})
    base.setdefault("engine", "skia")
    base.setdefault("mono", False)
    fixed_thr = None
    if tune_threshold:
        for k in ("threshold", "refine", "refine_boost"):
            base.pop(k, None)
    else:
        fixed_thr = max(1, min(255, int(base.get("threshold") or DEFAULT_THRESHOLD)))
        base.pop("refine", None)
        base.pop("refine_boost", None)
    if tune_xscale:
        base.pop("xscale", None)
        base.pop("target_width", None)
    full = list(codepoints) if codepoints is not None else list(range(32, 127))
    sample = [c for c in SAMPLE if c in set(full)] or [c for c in full if c != 32]
    if reference is not None:
        shared = [c for c in sample if c in reference.glyphs] or \
                 [c for c in full if c in reference.glyphs and c != 32]
        if not shared:
            raise ValueError("the reference font shares no characters with the requested range")
        sample, per_glyph = shared, False
    ideal = _Ideal(ttf, size, sample)
    ideal.reference = reference
    trials: list[Trial] = []
    cache: dict = {}

    def run(thr, refine, boost, xs):
        key = (thr, refine, boost, xs)
        if key in cache:
            return cache[key]
        kw = dict(base, threshold=thr, refine=refine, refine_boost=boost)
        if tune_xscale:
            kw["xscale"] = xs
        if progress:
            progress(f"{'threshold ' + str(thr)}"
                     f"{' refine boost ' + format(boost, 'g') if refine else ''}"
                     f"{' xscale ' + str(xs) if tune_xscale else ''}")
        try:
            f = from_ttf(ttf, size, codepoints=sample, **kw)
            t = _evaluate(f, ttf, size, ideal, sample)
        except ValueError:                      # e.g. glyph wider than 24px
            t = None
        if t is not None:
            t.kwargs = kw
            trials.append(t)
        cache[key] = t
        return t

    def best_of(ts):
        ts = [t for t in ts if t]
        return max(ts, key=lambda t: t.quality) if ts else None

    def run_ft(hinting, adv):
        key = ("ft", hinting, adv)
        if key in cache:
            return cache[key]
        kw = dict(base, engine="freetype", hinting=hinting, advance=adv)
        for k in ("threshold", "refine", "refine_boost", "xscale", "target_width"):
            kw.pop(k, None)
        if progress:
            progress(f"freetype hinting {hinting} advance {adv}")
        try:
            f = from_ttf(ttf, size, codepoints=sample, **kw)
            t = _evaluate(f, ttf, size, ideal, sample, width_slack=1.0)
        except ValueError:
            t = None
        if t is not None:
            t.kwargs = kw
            trials.append(t)
        cache[key] = t
        return t

    xs0 = 1.0
    refines = ((0.0, DEFAULT_REFINE_BOOST), (DEFAULT_REFINE_SIGMA, 1.0),
               (DEFAULT_REFINE_SIGMA, DEFAULT_REFINE_BOOST))
    coarse = THRESHOLDS if tune_threshold else (fixed_thr,)
    # 1) threshold x refine at the natural width
    cur = best_of([run(t, r, b, xs0) for t in coarse for r, b in refines])
    # 1b) FreeType's hinted 1-bit renders (a hand-built bitmap font look); only
    # when nothing was fixed by hand, since they have no threshold or stretch
    ft_ok = ("freetype" in engines and tune_threshold and tune_xscale
             and not base.get("mono") and ftengine.available())
    if ft_ok:
        ft_best = best_of([run_ft(h, a) for h in ("auto", "native", "default")
                           for a in ("ceil", "round")])
        if ft_best and (cur is None or ft_best.quality > cur.quality):
            cur = ft_best
    if cur is None:
        raise ValueError("no setting produced a font (glyphs too wide?)")
    if cur.kwargs.get("engine") == "freetype":
        kw = dict(cur.kwargs)
        final = from_ttf(ttf, size, codepoints=full, **kw)
        return AutoResult(final, kw, cur, sorted(trials, key=lambda t: -t.quality))
    # 2) coarse horizontal stretch, keeping the best threshold/refine
    if tune_xscale:
        k = cur.kwargs
        for xs in XSCALES:
            if 1.0 < xs <= max_xscale:
                t = run(k["threshold"], k["refine"], k["refine_boost"], xs)
                if t and t.quality > cur.quality:
                    cur = t
    # 3) two rounds of fine passes: threshold, then xscale, around the winner
    for _ in range(2):
        k = cur.kwargs
        if tune_threshold:
            for d in FINE_THRESHOLDS:
                thr = max(1, min(255, k["threshold"] + d))
                t = run(thr, k["refine"], k["refine_boost"], k.get("xscale", 1.0))
                if t and t.quality > cur.quality:
                    cur, k = t, t.kwargs
        if tune_xscale:
            for d in FINE_XSCALES:
                xs = round(min(max_xscale, max(1.0, k.get("xscale", 1.0) + d)), 3)
                t = run(k["threshold"], k["refine"], k["refine_boost"], xs)
                if t and t.quality > cur.quality:
                    cur, k = t, t.kwargs
    kw = dict(cur.kwargs)
    final = from_ttf(ttf, size, codepoints=full, **kw)
    opts, before, after = {}, 0.0, 0.0
    if per_glyph:
        opts, before, after = _tune_glyphs(ttf, size, kw, final, full, progress,
                                           rounds=glyph_rounds, jobs=jobs)
        if opts:
            # keep the per-glyph picks only if they help the real score too:
            # build both on the tuning sample and compare against the outline
            have = set(sample)
            s_opts = {c: o for c, o in opts.items() if c in have}
            s_plain = score_vs_ttf(from_ttf(ttf, size, codepoints=sample, **kw),
                                   ttf, size, sample).score
            s_tuned = score_vs_ttf(from_ttf(ttf, size, codepoints=sample,
                                            glyph_opts=s_opts, **kw),
                                   ttf, size, sample).score
            if progress:
                progress(f"per-glyph check: {s_plain:.1f}% -> {s_tuned:.1f}% on the sample")
            if s_tuned < s_plain:               # made it worse: revert
                opts, before, after = {}, 0.0, 0.0
        if opts:
            final = from_ttf(ttf, size, codepoints=full, glyph_opts=opts, **kw)
    return AutoResult(final, kw, cur, sorted(trials, key=lambda t: -t.quality),
                      opts, before, after)