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
    glyphs: dict[int, Glyph] = field(default_factory=dict)

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
                font.style = val
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
