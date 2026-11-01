"""Reader for legacy Windows 2.x/3.x bitmap fonts accepted by convfont."""
from __future__ import annotations

import struct

from .model import Font, FntError, Glyph

_HEADER_SIZE = 118


def parse_windows_fnt(data: bytes) -> Font:
    """Parse a raster Windows FNT v2/v3 file into a FontLibC bitmap font."""
    if len(data) < _HEADER_SIZE:
        raise FntError("Windows FNT header is truncated")

    version, declared_size = struct.unpack_from("<HI", data)
    if version not in (0x0200, 0x0300):
        raise FntError(f"unsupported Windows FNT version 0x{version:04X}")
    if declared_size < _HEADER_SIZE or declared_size > len(data):
        raise FntError("Windows FNT declared size is invalid or truncated")

    view = memoryview(data)[:declared_size]
    font_type, = struct.unpack_from("<H", view, 66)
    if font_type & 1:
        raise FntError("Windows FNT is not a raster font")

    ascent, = struct.unpack_from("<H", view, 74)
    height, = struct.unpack_from("<H", view, 88)
    first, last = struct.unpack_from("<BB", view, 95)
    if last < first:
        raise FntError("Windows FNT character range is reversed")
    if not 1 <= height <= 127:
        raise FntError(f"Windows FNT height {height} is outside FontLibC's 1-127 range")

    table_at = _HEADER_SIZE
    if version == 0x0300:
        if table_at + 24 > declared_size:
            raise FntError("Windows FNT v3 header is truncated")
        flags, = struct.unpack_from("<I", view, table_at)
        if flags & 0x0C:
            raise FntError("Windows FNT ABC-width glyphs are not supported")
        if flags & 0xF0 != 0x10:
            raise FntError("Windows FNT must use one-bit raster glyphs")
        table_at += 24

    count = last - first + 1
    entry_size = 4 if version == 0x0200 else 6
    if version == 0x0300:
        table_at += 6  # v3 dummy entry before the character table
    table_end = table_at + count * entry_size
    if table_end > declared_size:
        raise FntError("Windows FNT character table is truncated")

    entries = []
    for index in range(count):
        entry_at = table_at + index * entry_size
        if version == 0x0200:
            width, bitmap_at = struct.unpack_from("<HH", view, entry_at)
        else:
            width, bitmap_at = struct.unpack_from("<HI", view, entry_at)
        if not 1 <= width <= 24:
            raise FntError(f"Windows FNT U+{first + index:02X} width {width} is outside 1-24")
        columns = (width + 7) // 8
        bitmap_end = bitmap_at + columns * height
        if bitmap_at < table_end or bitmap_end > declared_size:
            raise FntError(f"Windows FNT U+{first + index:02X} bitmap is out of bounds")
        entries.append((width, bitmap_at, columns))

    font = Font(height=height, baseline=min(ascent, 255), weight="0")
    for index, (width, bitmap_at, columns) in enumerate(entries):
        rows = []
        for y in range(height):
            row = []
            for x in range(width):
                byte_at = bitmap_at + (x // 8) * height + y
                row.append("#" if view[byte_at] & (0x80 >> (x % 8)) else " ")
            rows.append("".join(row))
        font.glyphs[first + index] = Glyph(width, rows)

    return font
