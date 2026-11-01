"""Serialize FontLibC fonts, font packs, and C arrays -- no CE toolchain.

Byte-for-byte compatible with drdnar/convfont (v1.2):
  - `serialize_font`   == convfont -o binary / the font blob inside a pack
  - `serialize_fontpack` == convfont -o fontpack
  - `to_c_array`       == convfont -o carray

Format reference: github.com/drdnar/convfont (convfont.c, serialize_font.c)
and the FontLibC docs (ce-programming.github.io/toolchain/libraries/fontlibc.html).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .model import Font, FntError, Glyph

MAX_APPVAR_SIZE = 0xFFE8
METADATA_STRUCT_SIZE = 21  # convfont's (sic) "MEATADATA_STRUCT_SIZE"

# convfont's name -> byte tables
WEIGHTS = {
    "thin": 0x20, "extralight": 0x30, "light": 0x40, "semilight": 0x60,
    "normal": 0x80, "medium": 0x90, "semibold": 0xA0, "bold": 0xC0,
    "extrabold": 0xE0, "black": 0xF0,
}
STYLES = {
    "sans-serif": 0, "sansserif": 0, "serif": 1, "upright": 0,
    "oblique": 2, "italic": 4, "monospaced": 8, "fixed": 8,
    "proportional": 0,
}


def byte_columns(width: int) -> int:
    """Bytes needed per glyph row (convfont: (((width) - 1) >> 3) + 1)."""
    return ((width - 1) >> 3) + 1


def _norm(text) -> str:
    return str(text).strip().lower().replace(" ", "").replace("-", "").replace("_", "")


def _weight_byte(weight) -> int:
    """Weight name ('bold', 'extra light'...), a 0-255 int, or a numeric string."""
    if isinstance(weight, int):
        if not 0 <= weight <= 255:
            raise ValueError(f"weight {weight} outside 0-255")
        return weight
    key = _norm(weight)
    if key in WEIGHTS:
        return WEIGHTS[key]
    if key.isdigit() and int(key) <= 255:
        return int(key)
    raise ValueError(f"unknown weight {weight!r} (use one of {', '.join(WEIGHTS)} or 0-255)")


def _style_byte(style) -> int:
    """Style names combine ('serif italic', 'sans-serif,monospaced'); an int is taken as is."""
    if isinstance(style, int):
        if not 0 <= style <= 255:
            raise ValueError(f"style {style} outside 0-255")
        return style
    value = 0
    for part in str(style).replace(",", " ").split():
        key = part.strip().lower()
        if key not in STYLES:
            raise ValueError(f"unknown style {part!r} (use {', '.join(STYLES)})")
        value |= STYLES[key]
    return value


def _byte(name: str, value) -> int:
    if not isinstance(value, int) or not 0 <= value <= 255:
        raise ValueError(f"{name} must be an integer 0-255, got {value!r}")
    return value


def _glyph_bytes(glyph: Glyph, height: int) -> tuple[bytes, int]:
    """One glyph's bitmap: height rows x byte_columns(width) bytes,
    rows written with the column bytes in reverse order (convfont's layout,
    which lets FontLibC read a row as one aligned 24-bit read)."""
    cols = byte_columns(glyph.width)
    out = bytearray()
    for y in range(height):
        row = glyph.rows[y] if y < len(glyph.rows) else ""
        for c in range(cols - 1, -1, -1):  # convfont writes columns reversed
            b = 0
            for bit in range(8):
                x = c * 8 + bit
                if x < len(row) and row[x] == "#":
                    b |= 0x80 >> bit
            out.append(b)
    return bytes(out), cols


def serialize_font(font: Font, italic_space_adjust: int = 0) -> bytes:
    """Font -> FontLibC binary font (what convfont -o binary emits)."""
    font.validate()                      # FntError (a ValueError) on any inconsistency
    _byte("italic_space_adjust", italic_space_adjust)
    first, last = min(font.glyphs), max(font.glyphs)
    total = last - first + 1
    if total > 256:
        raise ValueError("FontLibC fonts hold at most 256 glyphs")

    glyphs: list[Glyph] = []
    for cp in range(first, last + 1):
        g = font.glyphs.get(cp)
        if g is None:  # fill holes with a blank 1px glyph
            g = Glyph(1, [" "] * font.height)
        if not 1 <= g.width <= 24:
            raise ValueError(f"U+{cp:02X}: width {g.width} outside 1-24")
        glyphs.append(g)

    # 18-byte header (offsets are 24-bit little-endian "ezword"s)
    header = bytes([
        0,                       # font format version, must be zero
        font.height,
        total & 0xFF,            # 256 glyphs wraps to 0, as in convfont
        first,                   # first glyph code point
        *struct.pack("<I", 18)[:3],          # widths table offset
        *struct.pack("<I", 18 + total)[:3],  # bitmaps-offsets table offset
        italic_space_adjust,
        _byte("space_above", font.space_above),
        _byte("space_below", font.space_below),
        _weight_byte(font.weight),
        _style_byte(font.style),
        _byte("cap_height", font.cap_height),
        _byte("x_height", font.x_height),
        _byte("baseline", font.baseline),           # baseline height
    ])

    widths = bytes(g.width for g in glyphs)
    bitmaps = [_glyph_bytes(g, font.height) for g in glyphs]

    # offsets table: fontlib reads each row as up to 3 bytes aligned at the
    # END of the glyph, so convfont stores  offset = start - 2 + (cols - 1)
    offsets = bytearray()
    off = 18 + total * 3  # first bitmap starts right after the 3 tables
    for _g, (bm, cols) in zip(glyphs, bitmaps):
        offsets += struct.pack("<H", off - 2 + cols - 1)
        off += len(bm)
        if off >= MAX_APPVAR_SIZE:
            raise ValueError("output font too big to fit!")

    data = header + widths + bytes(offsets) + b"".join(bm for bm, _ in bitmaps)
    return data


def serialize_fontpack(fonts: list[Font], name: str | None = None,
                       author: str | None = None,
                       pseudocopyright: str | None = None,
                       description: str | None = None,
                       version: str | None = None,
                       codepage: str | None = None) -> bytes:
    """Fonts -> FONTPACK binary (what convfont -o fontpack emits)."""
    if not 1 <= len(fonts) <= 255:
        raise ValueError("a font pack needs 1-255 fonts")
    packed = [serialize_font(f) for f in fonts]

    metadata = [name, author, pseudocopyright, description, version, codepage]
    for label, text in zip(("name", "author", "pseudocopyright", "description",
                            "version", "codepage"), metadata):
        if text is not None and "\x00" in text:
            raise ValueError(f"font pack {label} contains a NUL character")
    no_metadata = all(s is None for s in metadata)

    location = 12 + len(fonts) * 3          # header: 8 magic + 3 + 1 byte
    md_offset = 0
    str_offsets: list[int] = []
    if not no_metadata:
        md_offset = location
        location += METADATA_STRUCT_SIZE  # 21: size word + six 24-bit offsets
        for s in metadata:
            if s is not None:
                str_offsets.append(location)
                location += len(s.encode()) + 1
            else:
                str_offsets.append(0)

    font_offsets = []
    for p in packed:
        font_offsets.append(location)
        location += len(p)
    if location >= MAX_APPVAR_SIZE:
        raise ValueError("font pack exceeds the 64K appvar size limit")

    out = bytearray()
    ez = lambda v: out.extend(struct.pack("<I", v)[:3])  # 24-bit LE "ezword"

    out += b"FONTPACK"                     # 8 bytes, no NUL
    ez(md_offset)                          # metadata offset (0 = none)
    out.append(len(fonts))
    for o in font_offsets:
        ez(o)
    if not no_metadata:
        ez(METADATA_STRUCT_SIZE)
        for o in str_offsets:
            ez(o)
        for s in metadata:                 # NUL-terminated strings
            if s is not None:
                out += s.encode() + b"\x00"
    for p in packed:
        out += p
    return bytes(out)


def to_c_array(data: bytes) -> str:
    """Bytes -> convfont-style C array body: 16 hex bytes per line."""
    lines = [", ".join(f"0x{b:02X}" for b in data[i:i + 16])
             for i in range(0, len(data), 16)]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Reading: the inverse of the serializers, so output can be verified
# ---------------------------------------------------------------------------
@dataclass
class FontPack:
    fonts: list = field(default_factory=list)
    name: str | None = None
    author: str | None = None
    pseudocopyright: str | None = None
    description: str | None = None
    version: str | None = None
    codepage: str | None = None


def _ez(data: bytes, pos: int) -> int:
    if pos + 3 > len(data):
        raise ValueError("truncated font data")
    return data[pos] | data[pos + 1] << 8 | data[pos + 2] << 16


def _weight_name(b: int) -> str:
    for k, v in WEIGHTS.items():
        if v == b:
            return k
    return str(b)


def _style_name(b: int) -> str:
    names = [n for n, v in (("serif", 1), ("oblique", 2), ("italic", 4), ("monospaced", 8))
             if b & v]
    return " ".join(names) if names else "sans-serif"


def parse_font(data: bytes) -> Font:
    """FontLibC binary font -> Font (inverse of serialize_font).

    Code points the original left out come back as blank 1px glyphs, exactly
    as they are stored. Raises ValueError on malformed data.
    """
    if len(data) < 18:
        raise ValueError("font data shorter than the 18-byte header")
    if data[0] != 0:
        raise ValueError(f"unsupported font format version {data[0]}")
    height, total, first = data[1], data[2] or 256, data[3]
    widths_at, offsets_at = _ez(data, 4), _ez(data, 7)
    if first + total > 256:
        raise ValueError("glyph range runs past code point 255")
    if widths_at + total > len(data) or offsets_at + 2 * total > len(data):
        raise ValueError("font tables run past the end of the data")
    font = Font(height=height, baseline=data[17], cap_height=data[15], x_height=data[16],
                weight=_weight_name(data[13]), style=_style_name(data[14]),
                space_above=data[11], space_below=data[12])
    for i in range(total):
        w = data[widths_at + i]
        if not 1 <= w <= 24:
            raise ValueError(f"glyph {first + i}: width {w} outside 1-24")
        cols = byte_columns(w)
        stored = data[offsets_at + 2 * i] | data[offsets_at + 2 * i + 1] << 8
        start = stored - cols + 3                      # undo serialize_font's offset trick
        if start < 0 or start + height * cols > len(data):
            raise ValueError(f"glyph {first + i}: bitmap runs past the end of the data")
        rows = []
        for y in range(height):
            chunk = data[start + y * cols:start + (y + 1) * cols]
            bits = []
            for c in range(cols):                      # stored reversed: byte 0 is the last column
                b = chunk[cols - 1 - c]
                bits.extend("#" if b & (0x80 >> k) else " " for k in range(8))
            rows.append("".join(bits[:w]))
        font.glyphs[first + i] = Glyph(w, rows)
    return font


def parse_fontpack(data: bytes) -> FontPack:
    """FONTPACK binary -> FontPack (inverse of serialize_fontpack)."""
    if data[:8] != b"FONTPACK":
        raise ValueError("not a FONTPACK (missing the 'FONTPACK' signature)")
    if len(data) < 12:
        raise ValueError("truncated font pack header")
    md_at, count = _ez(data, 8), data[11]
    if not count:
        raise ValueError("font pack lists no fonts")
    offs = [_ez(data, 12 + 3 * i) for i in range(count)]
    if offs != sorted(offs) or offs[-1] >= len(data):
        raise ValueError("font pack offsets are out of order or past the end")
    pack = FontPack()
    for i, o in enumerate(offs):
        end = offs[i + 1] if i + 1 < count else len(data)
        pack.fonts.append(parse_font(data[o:end]))
    if md_at:
        for attr, idx in zip(("name", "author", "pseudocopyright", "description",
                              "version", "codepage"), range(6)):
            o = _ez(data, md_at + 3 + 3 * idx)
            if o:
                stop = data.index(b"\x00", o)
                setattr(pack, attr, data[o:stop].decode("utf-8", "replace"))
    return pack