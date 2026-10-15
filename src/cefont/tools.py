"""Thin wrappers around the CE toolchain's convfont."""
from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
from pathlib import Path


class ToolError(RuntimeError):
    pass


def find_convfont() -> str:
    exe = shutil.which("convfont")
    if not exe and os.environ.get("CEDEV"):
        exe = shutil.which("convfont", path=str(Path(os.environ["CEDEV"]) / "bin"))
    if not exe:
        raise ToolError("convfont not found on PATH or in $CEDEV/bin "
                        "(it ships with the CE C toolchain)")
    return exe


def convfont_carray(fnt: Path, out: Path) -> None:
    _run([find_convfont(), "-o", "carray", "-t", str(fnt), str(out)])


def convfont_pack(fnts, out: Path, name: str, author: str = "", version: str = "1",
                  pages: str = "ASCII") -> None:
    cmd = [find_convfont(), "-o", "fontpack", "-N", name, "-P", pages, "-V", version]
    if author:
        cmd += ["-A", author]
    for f in fnts:
        cmd += ["-t", str(f)]
    _run(cmd + [str(out)])


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ToolError(f"{' '.join(cmd)} failed:\n{r.stdout}{r.stderr}")


def appvar_name(text: str) -> str:
    """'Bungee-Regular' -> 'BUNGEE'. Appvar names: letters/digits, start with a letter, max 8."""
    name = re.sub(r"[^A-Za-z0-9]", "", re.split(r"[-_ ]", text.strip())[0]).upper()[:8]
    if not name or not name[0].isalpha():
        raise ToolError(f"can't make an appvar name from {text!r}; pass --name")
    return name


def make_appvar(name: str, data: bytes, archived: bool = True,
                comment: bytes = b"Made with cefont") -> bytes:
    """Wrap raw bytes in a TI-84 Plus CE .8xv appvar file."""
    name = appvar_name(name)
    var = struct.pack("<H", len(data)) + data
    entry = (struct.pack("<HH", 13, len(var)) + b"\x15"
             + name.encode("ascii").ljust(8, b"\0")
             + b"\x00" + (b"\x80" if archived else b"\x00")
             + struct.pack("<H", len(var)) + var)
    header = (b"**TI83F*\x1A\x0A\x00" + comment[:42].ljust(42, b"\0")
              + struct.pack("<H", len(entry)))
    return header + entry + struct.pack("<H", sum(entry) & 0xFFFF)