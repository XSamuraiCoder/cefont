import shutil
from pathlib import Path

import pytest

from cefont import Font, Glyph, from_ttf, parse_ranges
from cefont.model import FntError, parse_number

CANDIDATES = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
]
TTF = next((p for p in CANDIDATES if Path(p).exists()), None)


def tiny_font():
    f = Font(height=3, baseline=2, cap_height=0, x_height=1)
    f.glyphs[ord("I")] = Glyph(3, [" # ", " # ", " # "])
    f.glyphs[ord(" ")] = Glyph(2, ["  ", "  ", "  "])
    return f


def test_parse_ranges():
    assert parse_ranges("65-67,70") == [65, 66, 67, 70]
    with pytest.raises(ValueError):
        parse_ranges("200-300")


def test_parse_number():
    assert parse_number("0x41") == 65
    assert parse_number("$41") == 65
    assert parse_number("'A'") == 65
    assert parse_number("010") == 10  # not octal


def test_fnt_roundtrip():
    f = tiny_font()
    g = Font.from_fnt(f.to_fnt("test"))
    assert g.height == 3 and g.baseline == 2
    assert g.glyphs[ord("I")].rows == [" # ", " # ", " # "]
    assert g.glyphs[ord(" ")].width == 2


def test_render_and_width():
    f = tiny_font()
    assert f.render("I I") == [" #    # "] * 3
    assert f.text_width("I I") == 8
    assert f.render("?") == ["", "", ""]  # unknown chars are skipped


def test_bad_header():
    with pytest.raises(FntError):
        Font.from_fnt("nope\nHeight: 3\n")


@pytest.mark.skipif(TTF is None, reason="no system TTF found")
def test_from_ttf_and_roundtrip():
    f = from_ttf(TTF, 12)
    assert len(f.glyphs) == 95
    assert all(len(g.rows) == f.height for g in f.glyphs.values())
    assert all(len(r) == g.width for g in f.glyphs.values() for r in g.rows)
    g = Font.from_fnt(f.to_fnt())
    assert g.render("Hello") == f.render("Hello")
    assert (g.height, g.baseline) == (f.height, f.baseline)


@pytest.mark.skipif(TTF is None or shutil.which("convfont") is None,
                    reason="needs a TTF and convfont on PATH")
def test_convfont_accepts_output(tmp_path):
    from cefont import tools
    fnt = tmp_path / "t.fnt"
    fnt.write_text(from_ttf(TTF, 12).to_fnt(), encoding="utf-8")
    tools.convfont_carray(fnt, tmp_path / "t.inc")
    assert (tmp_path / "t.inc").stat().st_size > 1000
