"""FontLibC packaging helpers -- pure Python, no CE toolchain needed."""
from __future__ import annotations

import re
import struct
from pathlib import Path

from .fontpack import (MAX_APPVAR_SIZE, parse_fontpack, serialize_font,
                       serialize_fontpack, to_c_array)
from .model import Font, FntError
from .windowsfnt import parse_windows_fnt

_APPVAR_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,7}")


def appvar_name(text: str, derive: bool = True) -> str:
    """Make a valid appvar name (1-8 letters/digits, starting with a letter).

    derive=True (default) is for file names: 'Bungee-Regular' -> 'BUNGEE'
    (the part before the first '-', '_' or space, symbols dropped, cut to 8).
    derive=False is for a name the user typed: it must already be valid
    ('my font' is an error, not silently 'MY').
    """
    text = text.strip()
    if not derive:
        if not _APPVAR_RE.fullmatch(text):
            raise ValueError(f"{text!r} is not a valid appvar name: use 1-8 letters/digits, "
                             "starting with a letter")
        return text.upper()
    name = re.sub(r"[^A-Za-z0-9]", "", re.split(r"[-_ ]", text)[0]).upper()[:8]
    if not name or not name[0].isalpha():
        raise ValueError(f"can't make an appvar name from {text!r}; pass --name")
    return name


def make_appvar(name: str, data: bytes, archived: bool = True,
                comment: bytes | str = b"Made with cefont") -> bytes:
    """Wrap raw bytes in a TI-84 Plus CE .8xv appvar file.

    `name` must be a valid appvar name (see appvar_name(..., derive=False)).
    """
    name = appvar_name(name, derive=False)
    if not data:
        raise ValueError("appvar data is empty")
    if len(data) > MAX_APPVAR_SIZE:
        raise ValueError(f"appvar data is {len(data)} bytes; the limit is {MAX_APPVAR_SIZE}")
    if isinstance(comment, str):
        comment = comment.encode("ascii", "replace")
    var = struct.pack("<H", len(data)) + data
    entry = (struct.pack("<HH", 13, len(var)) + b"\x15"
             + name.encode("ascii").ljust(8, b"\x00")
             + b"\x00" + (b"\x80" if archived else b"\x00")
             + struct.pack("<H", len(var)) + var)
    header = (b"**TI83F*\x1A\x0A\x00" + comment[:42].ljust(42, b"\x00")
              + struct.pack("<H", len(entry)))
    return header + entry + struct.pack("<H", sum(entry) & 0xFFFF)


def read_appvar(blob: bytes) -> tuple[str, bytes, bool]:
    """.8xv bytes -> (name, payload, archived). Checks signature, sizes and checksum."""
    if blob[:8] != b"**TI83F*":
        raise ValueError("not a TI .8xv file (missing '**TI83F*' signature)")
    if len(blob) < 55 + 17 + 2 + 2:
        raise ValueError("file is too short to be an appvar")
    (entry_len,) = struct.unpack_from("<H", blob, 53)
    entry = blob[55:55 + entry_len]
    if len(entry) != entry_len or len(blob) < 55 + entry_len + 2:
        raise ValueError("file is truncated")
    (checksum,) = struct.unpack_from("<H", blob, 55 + entry_len)
    if checksum != sum(entry) & 0xFFFF:
        raise ValueError("checksum mismatch: the file is corrupt")
    if entry[4] != 0x15:
        raise ValueError(f"not an appvar (variable type 0x{entry[4]:02X})")
    name = entry[5:13].rstrip(b"\x00").decode("ascii", "replace")
    (n,) = struct.unpack_from("<H", entry, 15)
    var = entry[17:17 + n]
    if len(var) != n or n < 2 or struct.unpack_from("<H", var)[0] != n - 2:
        raise ValueError("appvar length fields disagree")
    return name, var[2:], entry[14] == 0x80


def verify_appvar(blob: bytes) -> list[Font]:
    """Read a font appvar back and return its fonts (raises ValueError if it is bad).

    A cheap end-to-end self-check: parse the file, its pack and every glyph.
    """
    _, payload, _ = read_appvar(blob)
    return parse_fontpack(payload).fonts


def read_fnt(path: Path) -> Font:
    path = Path(path)
    try:
        data = path.read_bytes()
        if data[:2] in (b"\x00\x02", b"\x00\x03"):
            return parse_windows_fnt(data)
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return Font.from_fnt(data.decode("utf-16"))
        return Font.from_fnt(data.decode("utf-8-sig"))
    except UnicodeDecodeError as e:
        raise FntError(f"{path.name}: unsupported text font encoding: {e}") from None
    except FntError as e:
        raise FntError(f"{path.name}: {e}") from None


def font_to_carray(fnt: Path, out: Path) -> None:
    """convfont -o carray equivalent: .fnt -> C array body (.inc)."""
    out.write_text(to_c_array(serialize_font(read_fnt(fnt))), encoding="utf-8")


def make_fontpack(fnts: list[Path], title: str, author: str = "",
                  version: str = "1", codepage: str = "ASCII") -> bytes:
    """convfont -o fontpack equivalent, straight to bytes."""
    fonts = [read_fnt(p) for p in fnts]
    return serialize_fontpack(fonts, name=title, author=author or None,
                              version=version, codepage=codepage)