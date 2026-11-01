"""Bitmap font model plus reader/writer for convfont's text format.

A glyph's rows are strings of '#' (set pixel) and ' ' (blank pixel), each as
long as the glyph's width. Every glyph in a font has exactly `height` rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field

BLANK_CHARS = set(" 0\u2014\u2013\u2012\u2212")  # what convfont treats as blank


class FntError(ValueError):
    pass


@dataclass
class Glyph:
    width: int
    rows: list[str]

    def validate(self, height: int, label: str = "glyph") -> None:
        """Raise FntError unless this glyph is `width` x `height` of '#'/' ' only."""
        if not isinstance(self.width, int) or self.width < 1:
            raise FntError(f"{label}: width must be a positive integer, got {self.width!r}")
        if len(self.rows) != height:
            raise FntError(f"{label}: {len(self.rows)} rows, font height is {height}")
        for i, row in enumerate(self.rows):
            if len(row) != self.width:
                raise FntError(f"{label}: row {i} is {len(row)} wide, glyph width is {self.width}")
            bad = set(row) - {"#", " "}
            if bad:
                raise FntError(f"{label}: row {i} has characters other than '#' and ' ': "
                               f"{''.join(sorted(bad))!r}")


@dataclass
class Font:
    height: int
    baseline: int
    cap_height: int = 0
    x_height: int = 0
    weight: str = "normal"
    style: str = "sans-serif"
    space_above: int = 0
    space_below: int = 0
    name: str = ""
    xscale: float = 1.0   # horizontal stretch used when rasterized (scoring needs it)
    engine: str = ""      # which rasterizer made it ('skia' or 'freetype'), informational
    hinting: str = ""     # freetype hinting mode used, if any
    glyphs: dict[int, Glyph] = field(default_factory=dict)

    # ---- checking
    def validate(self, max_width: int | None = None) -> "Font":
        """Check the font is internally consistent; returns self so it chains.

        Raises FntError (a ValueError) naming the first problem: no glyphs,
        bad height/baseline, code points outside 0-255, rows of the wrong
        length or with characters other than '#' and ' ', or a glyph wider
        than `max_width` (FontLibC allows 24).
        """
        if not self.glyphs:
            raise FntError("font has no glyphs")
        if not 0 < self.height <= 127:
            raise FntError(f"font height {self.height} out of range (1-127)")
        for name in ("baseline", "cap_height", "x_height", "space_above", "space_below"):
            v = getattr(self, name)
            if not isinstance(v, int) or not 0 <= v <= 255:
                raise FntError(f"{name} must be an integer 0-255, got {v!r}")
        for cp, g in sorted(self.glyphs.items()):
            if not 0 <= cp <= 255:
                raise FntError(f"code point {cp} outside 0-255 (FontLibC fonts are 8-bit)")
            g.validate(self.height, f"U+{cp:02X}")
            if max_width is not None and g.width > max_width:
                raise FntError(f"U+{cp:02X}: width {g.width} is over the {max_width}px limit")
        return self

    # ---- measuring / rendering
    def text_width(self, text: str) -> int:
        return sum(self.glyphs[ord(c)].width for c in text if ord(c) in self.glyphs)

    def render(self, text: str, missing: str | None = None) -> list[str]:
        """Render text to `height` rows of '#'/' '. Characters the font lacks are
        skipped, or drawn as `missing` if given and that character exists."""
        rows = [""] * self.height
        for ch in text:
            g = self.glyphs.get(ord(ch))
            if g is None and missing is not None:
                g = self.glyphs.get(ord(missing))
            if g is None:
                continue
            for i, r in enumerate(g.rows):
                rows[i] += r
        return rows

    # ---- writing
    def to_fnt(self, comment: str = "") -> str:
        out = ["convfont"]
        if comment:
            out.append(f": {comment}")
        out += [f"Height: {self.height}",
                f"Weight: {self.weight}",
                f"Style: {self.style}",
                f"Cap height: {self.cap_height}",
                f"x-height: {self.x_height}",
                f"Baseline: {self.baseline}"]
        if self.space_above:
            out.append(f"Space above: {self.space_above}")
        if self.space_below:
            out.append(f"Space below: {self.space_below}")
        out.append("Font data:")
        for cp, g in sorted(self.glyphs.items()):
            out += [f"Code point: {cp}", f"Width: {g.width}", "Data:"]
            out += [r.replace(" ", "0") for r in g.rows]
            out.append("")
        return "\n".join(out) + "\n"

    # ---- reading
    @classmethod
    def from_fnt(cls, text: str) -> "Font":
        lines = text.lstrip("\ufeff").splitlines()
        if not lines or lines[0].strip() != "convfont":
            raise FntError("not a convfont text font (first line must be 'convfont')")
        pos = 1

        def next_meta():
            """-> (tag, value) or None at EOF; skips blank lines and comments."""
            nonlocal pos
            while pos < len(lines):
                line = lines[pos].strip()
                pos += 1
                if not line or line.startswith(":"):
                    continue
                tag, sep, value = line.partition(":")
                if not sep:
                    raise FntError(f"line {pos}: expected 'Tag: value', got {line!r}")
                return tag.strip().lower(), value.strip()
            return None

        font = cls(height=0, baseline=0)
        seen_style = False
        opts = {"double width": False, "default width": 0, "fixed-width": 0}
        while True:
            item = next_meta()
            if item is None:
                raise FntError("missing 'Font data:' tag")
            tag, val = item
            if tag == "font data":
                break
            if tag == "height":
                font.height = parse_number(val)
            elif tag == "weight":
                font.weight = val
            elif tag == "style":
                # convfont allows several Style lines (e.g. Sans-serif + Upright
                # + Proportional); they combine, so accumulate instead of overwrite
                font.style = val if font.style == "sans-serif" and not seen_style else f"{font.style} {val}"
                seen_style = True
            elif tag == "code page":
                pass
            elif tag == "cap height":
                font.cap_height = parse_number(val)
            elif tag == "x-height":
                font.x_height = parse_number(val)
            elif tag == "baseline":
                font.baseline = parse_number(val)
            elif tag == "space above":
                font.space_above = parse_number(val)
            elif tag == "space below":
                font.space_below = parse_number(val)
            elif tag == "italic adjust":
                pass
            elif tag == "double width":
                opts["double width"] = val.lower() == "true"
            elif tag in ("default width", "fixed-width", "fixed width"):
                opts["fixed-width" if tag != "default width" else tag] = parse_number(val)
        if font.height <= 0:
            raise FntError("font needs a positive 'Height'")

        cp = -1
        gl_double, gl_width, gl_cp = opts["double width"], None, None
        while True:
            item = next_meta()
            if item is None:
                break
            tag, val = item
            if tag == "code point":
                gl_cp = parse_number(val)
            elif tag == "width":
                gl_width = parse_number(val)
            elif tag == "double width":
                gl_double = val.lower() == "true"
            elif tag in ("name", "unicode"):
                pass  # informational glyph tags written by convfont's own files
            elif tag == "data":
                raw = lines[pos:pos + font.height]
                if len(raw) < font.height:
                    raise FntError("glyph bitmap runs past end of file")
                pos += font.height
                cp = gl_cp if gl_cp is not None else cp + 1
                font.glyphs[cp] = _make_glyph(raw, gl_width, gl_double, opts)
                gl_double, gl_width, gl_cp = opts["double width"], None, None
            else:
                raise FntError(f"unknown glyph tag {tag!r}")
        return font


def _make_glyph(raw, width, double, opts):
    rows = []
    for line in raw:
        bits = ["#" if c not in BLANK_CHARS else " " for c in line]
        if double:
            bits = bits[::2]
        rows.append("".join(bits))
    w = width if width is not None else max((len(r) for r in rows), default=0)
    if width is None:
        w = max(w, opts["default width"])
    if opts["fixed-width"]:
        if w > opts["fixed-width"]:
            raise FntError("glyph wider than 'Fixed width'")
        w = opts["fixed-width"]
    return Glyph(w, [r[:w].ljust(w) for r in rows])


def parse_number(s: str) -> int:
    s = s.strip()
    if len(s) >= 3 and s[0] == "'" and s[-1] == "'":
        return ord(s[1:-1].encode().decode("unicode_escape"))
    if s.startswith("$"):
        return int(s[1:], 16)
    if s.lower().startswith("0x"):
        return int(s, 16)
    return int(s, 10)  # decimal, even with leading zeros