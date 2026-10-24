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

from .model import Font, Glyph

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


def _weight_byte(weight) -> int:
    if isinstance(weight, int):
        return weight & 0xFF
    key = str(weight).strip().lower().replace(" ", "").replace("-", "")
    # normalize "extra light" -> "extralight"
    return WEIGHTS.get(key, WEIGHTS.get(str(weight).strip().lower(), 0x80))


def _style_byte(style) -> int:
    if isinstance(style, int):
        return style & 0xFF
    value = 0
    for part in str(style).replace(",", " ").split():
        value |= STYLES.get(part.strip().lower(), 0)
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
    if not font.glyphs:
        raise ValueError("font has no glyphs")
    if not 0 < font.height <= 127:
        raise ValueError(f"font height {font.height} out of range (1-127)")
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
        if len(g.rows) != font.height:
            raise ValueError(f"U+{cp:02X}: {len(g.rows)} rows, font height is {font.height}")
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
        font.space_above,
        font.space_below,
        _weight_byte(font.weight),
        _style_byte(font.style),
        font.cap_height,
        font.x_height,
        font.baseline,           # baseline height
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