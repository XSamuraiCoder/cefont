"""Quality benchmark: score cefont settings across several fonts and sizes.

Replaces the loose bench.py / bench2.py scripts. Each *variant* is a set of
`from_ttf` keyword arguments; every variant is rendered for every font at every
size and scored against the original outlines, then the table and per-variant
means are reported so settings (threshold, refine, supersample...) can be
compared on more than one font.

Metrics
  match  score_vs_ttf: pooled pixel overlap incl. width errors, 0-100,
         higher is better. Uses Skia for outline rasterization.
  blur   gaussian-blurred absolute error against a 16x reference, relative to
         the reference ink, lower is better. Needs numpy and scipy.

A variant may be {"auto": true} to use the auto-tuner (extra from_ttf
keywords in it are kept as given).

Library use:
    from cefont.bench import run_bench
    res = run_bench(["DejaVuSans.ttf"], [8, 12],
                    {"default": {}, "thin": {"threshold": 130}})
    print(res.table())

CLI:  cefont bench FONT... -s 8,12 --variants '{"a": {}, "b": {"threshold": 85}}'
"""
from __future__ import annotations

import ast
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from .auto import autotune
from .compare import score_vs_ttf
from .raster import from_ttf

DEFAULT_SIZES = (8, 10, 12, 16)
ASCII = list(range(33, 127))          # printable, no space
SWEEP_THRESHOLDS = (70, 85, 100, 115, 130)
BLUR_SIGMA = 0.6
BLUR_HI = 16                          # reference is drawn this many times larger

# Used by `cefont bench` when no font is given; only those that exist are used.
CANDIDATE_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/cour.ttf",
    "C:/Windows/Fonts/georgia.ttf",
    "C:/Windows/Fonts/verdana.ttf",
)


def default_fonts() -> list[str]:
    found = [p for p in CANDIDATE_FONTS if Path(p).is_file()]
    if not found:
        raise ValueError("no system fonts found for the benchmark; pass font files explicitly")
    return found


def sweep_variants() -> dict[str, dict]:
    """Threshold x refine grid (same grid as `cefont compare --sweep`)."""
    out: dict[str, dict] = {}
    for thr in SWEEP_THRESHOLDS:
        out[f"t{thr}"] = {"threshold": thr}
        out[f"t{thr}+r"] = {"threshold": thr, "refine": 0.6, "refine_boost": 1.0}
        out[f"t{thr}+rb"] = {"threshold": thr, "refine": 0.6}
    return out


def parse_variants(spec) -> dict[str, dict]:
    """None -> {'default': {}}; a dict passes through; a string is JSON or a
    Python-literal dict ({"name": {kwarg: value}}). Never eval()s."""
    if spec is None:
        return {"default": {}}
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError:
            try:
                spec = ast.literal_eval(spec)
            except (ValueError, SyntaxError):
                raise ValueError("variants must be a dict like "
                                 "{\"name\": {\"threshold\": 85}}") from None
    if (not isinstance(spec, dict) or not spec
            or not all(isinstance(v, dict) for v in spec.values())):
        raise ValueError("variants must be a non-empty dict of name -> {from_ttf kwargs}")
    return {str(k): dict(v) for k, v in spec.items()}


# ---------------------------------------------------------------------------
# Metrics. Each takes (font, ttf_path, size) and returns one number.
# ---------------------------------------------------------------------------
def match_metric(font, ttf, size, **_) -> float:
    return score_vs_ttf(font, ttf, size).score


def blur_metric(font, ttf, size, sigma: float = BLUR_SIGMA, **_) -> float:
    """Relative gaussian-blurred error vs a 16x reference (lower is better)."""
    try:
        import numpy as np
        from scipy.ndimage import gaussian_filter
    except ImportError as e:
        raise ValueError("the 'blur' metric needs numpy and scipy installed") from e
    from .skiautils import Typeface
    face = Typeface(ttf)

    xs = getattr(font, "xscale", 1.0)

    def ref_cov(ch, dx, w):
        coverage = face.coverage(ch, size, w + 8, font.height, font.baseline,
                                 4 + dx, scale=BLUR_HI, xscale=xs)
        return np.asarray(coverage, float)

    err = tot = 0.0
    for cp, g in font.glyphs.items():
        if not 33 <= cp < 127:
            continue
        bm = np.zeros((font.height, g.width + 8))
        for y, row in enumerate(g.rows):
            for x, c in enumerate(row):
                if c == "#":
                    bm[y, x + 4] = 1.0
        fb = gaussian_filter(bm, sigma)
        best = None
        for dx in range(-4, 5):            # the glyph's left edge vs the origin
            fr = gaussian_filter(ref_cov(chr(cp), dx, g.width), sigma)
            e = float(np.abs(fb - fr).sum())
            if best is None or e < best[0]:
                best = (e, float(fr.sum()))
        err += best[0]
        tot += best[1]
    if not tot:
        raise ValueError("no printable glyphs to score")
    return err / tot


METRICS = {
    "match": (match_metric, True, "match % vs TTF (higher is better)"),
    "blur": (blur_metric, False, "blurred error vs TTF (lower is better)"),
}


# ---------------------------------------------------------------------------
@dataclass
class BenchResult:
    metric: str
    variants: list[str]
    rows: list[tuple[str, int, dict]] = field(default_factory=list)  # (font, px, {variant: score})

    @property
    def higher_is_better(self) -> bool:
        return METRICS[self.metric][1]

    def means(self) -> dict[str, float]:
        out = {}
        for v in self.variants:
            vals = [r[2][v] for r in self.rows if not math.isnan(r[2][v])]
            out[v] = sum(vals) / len(vals) if vals else float("nan")
        return out

    def best(self) -> str:
        m = {k: v for k, v in self.means().items() if not math.isnan(v)}
        if not m:
            raise ValueError("no variant produced a score")
        return (max if self.higher_is_better else min)(m, key=m.get)

    def table(self) -> str:
        prec = 1 if self.metric == "match" else 3
        width = max(10, *(len(v) + 1 for v in self.variants))
        name_w = max([14] + [len(r[0]) + 1 for r in self.rows])
        lines = [f"metric: {METRICS[self.metric][2]}",
                 f"{'font':{name_w}}{'px':>3} " + " ".join(f"{v:>{width}}" for v in self.variants)]
        for name, px, scores in self.rows:
            lines.append(f"{name:{name_w}}{px:>3} "
                         + " ".join(f"{scores[v]:{width}.{prec}f}" for v in self.variants))
        m = self.means()
        lines.append(f"{'MEAN':{name_w + 3}} " + " ".join(f"{m[v]:{width}.{prec}f}" for v in self.variants))
        lines.append(f"best: {self.best()}")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps({
            "metric": self.metric,
            "higher_is_better": self.higher_is_better,
            "rows": [{"font": f, "size": s, "scores": sc} for f, s, sc in self.rows],
            "means": self.means(),
            "best": self.best(),
        }, indent=2)


def run_bench(fonts, sizes=DEFAULT_SIZES, variants=None, metric: str = "match",
              codepoints=ASCII, sigma: float = BLUR_SIGMA, progress=None) -> BenchResult:
    """Render every font x size x variant and score it against the TTF.

    A combination that can't be built (e.g. glyphs wider than FontLibC's 24px
    limit) scores NaN and is left out of the means. `progress(label)` is
    called before each font/size.
    """
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r} (choose from {', '.join(METRICS)})")
    fn = METRICS[metric][0]
    variants = parse_variants(variants)
    res = BenchResult(metric, list(variants))
    for font_path in fonts:
        path = Path(font_path)
        if not path.is_file():
            raise OSError(f"font not found: {path}")
        for size in sizes:
            if progress:
                progress(f"{path.stem} {size}px")
            scores = {}
            for name, kw in variants.items():
                try:
                    kw = dict(kw)
                    if kw.pop("auto", False):          # {"auto": true} = let cefont pick
                        f = autotune(path, size, codepoints=codepoints, base=kw).font
                    else:
                        f = from_ttf(path, size, codepoints=codepoints, **kw)
                    scores[name] = fn(f, path, size, sigma=sigma)
                except ValueError as e:
                    if "needs numpy" in str(e):
                        raise
                    scores[name] = float("nan")
            res.rows.append((path.stem, size, scores))
    return res
