"""FontLibC packaging helpers -- pure Python, no CE toolchain needed."""
from __future__ import annotations

import re
import struct
from pathlib import Path

from .fontpack import serialize_font, serialize_fontpack, to_c_array
from .model import Font, FntError


def appvar_name(text: str) -> str:
    """'Bungee-Regular' -> 'BUNGEE'. Appvar names: letters/digits, start with
    a letter, max 8."""
    name = re.sub(r"[^A-Za-z0-9]", "", re.split(r"[-_ ]", text.strip())[0]).upper()[:8]
    if not name or not name[0].isalpha():
        raise ValueError(f"can't make an appvar name from {text!r}; pass --name")
    return name


def make_appvar(name: str, data: bytes, archived: bool = True,
                comment: bytes = b"Made with cefont") -> bytes:
    """Wrap raw bytes in a TI-84 Plus CE .8xv appvar file."""
    name = appvar_name(name)
    var = struct.pack("<H", len(data)) + data
    entry = (struct.pack("<HH", 13, len(var)) + b"\x15"
             + name.encode("ascii").ljust(8, b"\x00")
             + b"\x00" + (b"\x80" if archived else b"\x00")
             + struct.pack("<H", len(var)) + var)
    header = (b"**TI83F*\x1A\x0A\x00" + comment[:42].ljust(42, b"\x00")
              + struct.pack("<H", len(entry)))
    return header + entry + struct.pack("<H", sum(entry) & 0xFFFF)


def read_fnt(path: Path) -> Font:
    return Font.from_fnt(Path(path).read_text(encoding="utf-8-sig"))


def font_to_carray(fnt: Path, out: Path) -> None:
    """convfont -o carray equivalent: .fnt -> C array body (.inc)."""
    out.write_text(to_c_array(serialize_font(read_fnt(fnt))), encoding="utf-8")


def make_fontpack(fnts: list[Path], title: str, author: str = "",
                  version: str = "1", codepage: str = "ASCII") -> bytes:
    """convfont -o fontpack equivalent, straight to bytes."""
    fonts = [read_fnt(p) for p in fnts]
    return serialize_fontpack(fonts, name=title, author=author or None,
                              version=version, codepage=codepage)