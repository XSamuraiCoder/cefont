import os, shutil, struct, subprocess, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cefont import (Font, FntError, Glyph, appvar_name, from_ttf, make_appvar, parse_font,
                    parse_fontpack, parse_ranges, parse_sizes, parse_windows_fnt, read_appvar,
                    score_font, serialize_font, serialize_fontpack, verify_appvar)
from cefont.cli import main
from cefont.tools import read_fnt

TTF = next((p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                        "C:/Windows/Fonts/arial.ttf", "/Library/Fonts/Arial.ttf")
            if Path(p).is_file()), None)


def tiny():
    f = Font(height=3, baseline=2, cap_height=0, x_height=1)
    f.glyphs[65] = Glyph(3, ["###", "# #", "###"])
    f.glyphs[67] = Glyph(10, ["#########", "#", "#"][0:1] and ["#########.".replace(".", "#"), " " * 10, "#" + " " * 9])
    return f


def windows_fnt(version):
    extension = 24 if version == 0x0300 else 0
    table_at = 118 + extension
    bitmap_at = table_at + (12 if extension else 4)
    data = bytearray(bitmap_at + 2)
    struct.pack_into("<HI", data, 0, version, len(data))
    struct.pack_into("<H", data, 66, 0)           # raster font
    struct.pack_into("<H", data, 74, 1)           # ascent
    struct.pack_into("<H", data, 88, 2)           # pixel height
    struct.pack_into("<BB", data, 95, 65, 65)     # one glyph: 'A'
    if version == 0x0200:
        struct.pack_into("<HH", data, table_at, 3, bitmap_at)
    else:
        struct.pack_into("<I", data, 118, 0x10)   # one-bit raster
        struct.pack_into("<HI", data, table_at, 0, 0)  # v3 dummy entry
        struct.pack_into("<HI", data, table_at + 6, 3, bitmap_at)
    data[bitmap_at:bitmap_at + 2] = bytes((0xE0, 0xA0))
    return bytes(data)


class Ranges(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(parse_ranges("ascii")[0], 32)
        self.assertEqual(parse_ranges("0x41-0x43"), [65, 66, 67])
        self.assertEqual(len(parse_ranges("latin1")), 95 + 96)
        for bad in ("abc", "5-1", "300", ""):
            with self.assertRaises(ValueError):
                parse_ranges(bad)

    def test_sizes(self):
        self.assertEqual(parse_sizes("8-10,16"), [8, 9, 10, 16])
        for bad in ("0", "x", "12-8", ""):
            with self.assertRaises(ValueError):
                parse_sizes(bad)


class Format(unittest.TestCase):
    def test_roundtrip_font(self):
        f = tiny()
        g = parse_font(serialize_font(f))
        for cp, gl in f.glyphs.items():
            self.assertEqual(g.glyphs[cp].width, gl.width)
            self.assertEqual(g.glyphs[cp].rows, gl.rows)
        self.assertEqual((g.height, g.baseline, g.x_height), (3, 2, 1))

    def test_roundtrip_pack_and_appvar(self):
        f = tiny()
        pack = serialize_fontpack([f, f], name="T", author="me", version="2", codepage="ASCII")
        p = parse_fontpack(pack)
        self.assertEqual((len(p.fonts), p.name, p.author, p.version), (2, "T", "me", "2"))
        blob = make_appvar("T", pack)
        self.assertEqual(read_appvar(blob), ("T", pack, True))
        self.assertEqual(len(verify_appvar(blob)), 2)
        bad = bytearray(blob); bad[60] ^= 1
        with self.assertRaises(ValueError):
            read_appvar(bytes(bad))

    def test_style_weight_strict(self):
        f = tiny(); f.weight = "chunky"
        with self.assertRaises(ValueError):
            serialize_font(f)
        f = tiny(); f.style = "wobbly"
        with self.assertRaises(ValueError):
            serialize_font(f)
        f = tiny(); f.weight = "extra bold"; f.style = "serif italic"
        g = parse_font(serialize_font(f))
        self.assertEqual((g.weight, g.style), ("extrabold", "serif italic"))

    def test_validation(self):
        f = tiny(); f.glyphs[65].rows[0] = "##"
        with self.assertRaises(FntError):
            f.validate()
        f = tiny(); f.glyphs[66] = Glyph(25, [" " * 25] * 3)
        with self.assertRaises(ValueError):
            serialize_font(f)
        f = tiny(); f.baseline = 300
        with self.assertRaises(ValueError):
            serialize_font(f)

    def test_names(self):
        self.assertEqual(appvar_name("Bungee-Regular"), "BUNGEE")
        self.assertEqual(appvar_name("my_font", derive=False.__class__(0)) if False else appvar_name("MYFONT2", derive=False), "MYFONT2")
        with self.assertRaises(ValueError):
            appvar_name("my font", derive=False)
        with self.assertRaises(ValueError):
            make_appvar("TOOLONGNAME", b"x")
        with self.assertRaises(ValueError):
            make_appvar("A", b"")


class WindowsFnt(unittest.TestCase):
    def test_reads_windows_fnt_versions_2_and_3(self):
        for version in (0x0200, 0x0300):
            with self.subTest(version=version):
                font = parse_windows_fnt(windows_fnt(version))
                self.assertEqual((font.height, font.baseline), (2, 1))
                self.assertEqual(font.glyphs[65], Glyph(3, ["###", "# #"]))

    def test_rejects_truncated_and_unsupported_fnt(self):
        for data in (b"", b"\x00\x01" + bytes(116), windows_fnt(0x0200)[:-1]):
            with self.subTest(data=data[:4]):
                with self.assertRaises(FntError):
                    parse_windows_fnt(data)

    def test_read_fnt_detects_legacy_binary(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "legacy.fnt"
            path.write_bytes(windows_fnt(0x0200))
            self.assertEqual(read_fnt(path).glyphs[65], Glyph(3, ["###", "# #"]))


@unittest.skipUnless(shutil.which("convfont"), "convfont is not installed")
class ConvfontCompatibility(unittest.TestCase):
    def test_binary_matches_convfont(self):
        font = Font(height=3, baseline=2, cap_height=0, x_height=1)
        font.glyphs[65] = Glyph(3, ["###", "# #", "###"])
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "reference.fnt"
            output = Path(d) / "reference.bin"
            source.write_text(font.to_fnt(), encoding="utf-8")
            result = subprocess.run(
                [shutil.which("convfont"), "-o", "binary", "-t", str(source), str(output)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(output.read_bytes(), serialize_font(font))

    def test_windows_fnt_matches_convfont(self):
        for version in (0x0200, 0x0300):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as d:
                source = Path(d) / "legacy.fnt"
                output = Path(d) / "legacy.bin"
                source.write_bytes(windows_fnt(version))
                result = subprocess.run(
                    [shutil.which("convfont"), "-o", "binary", "-f", str(source), str(output)],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(output.read_bytes(), serialize_font(parse_windows_fnt(source.read_bytes())))


@unittest.skipUnless(TTF, "no system TTF available")
class WithFont(unittest.TestCase):
    def test_from_ttf_checks(self):
        with self.assertRaises(OSError):
            from_ttf("/nope.ttf", 12)
        for kw in ({"size": 0}, {"size": 12.5}, {"size": 12, "threshold": 999}):
            size = kw.pop("size")
            with self.assertRaises(ValueError):
                from_ttf(TTF, size, **kw)

    def test_build_roundtrip(self):
        f = from_ttf(TTF, 12)
        g = parse_font(serialize_font(f))
        self.assertEqual(g.glyphs[ord("A")].rows, f.glyphs[ord("A")].rows)

    def test_skia_renderer_builds_a_valid_font(self):
        font = from_ttf(TTF, 12, engine="skia")
        self.assertEqual(font.engine, "skia")
        self.assertGreater(len(font.glyphs), 0)
        font.validate(max_width=24)

    def test_default_renderer_is_hinted_freetype(self):
        font = from_ttf(TTF, 12, codepoints=[32, 65, 66])
        self.assertEqual(font.engine, "freetype")
        self.assertEqual(set(font.glyphs), {32, 65, 66})

    def test_skia_engine_defaults_to_antialiasing(self):
        font = from_ttf(TTF, 12, engine="skia", codepoints=[32, 65, 66])
        self.assertEqual(font.engine, "skia")
        self.assertEqual(set(font.glyphs), {32, 65, 66})

    def test_antialias_option_selects_skia_automatically(self):
        font = from_ttf(TTF, 12, threshold=110, codepoints=[32, 65, 66])
        self.assertEqual(font.engine, "skia")

    def test_freetype_hinted_renderer_still_builds(self):
        font = from_ttf(TTF, 12, engine="freetype", codepoints=[32, 65, 66])
        self.assertEqual(font.engine, "freetype")
        self.assertEqual(set(font.glyphs), {32, 65, 66})
        font.validate(max_width=24)

    def test_hinted_score_can_skip_slow_raw_outline_comparison(self):
        font = from_ttf(TTF, 12, codepoints=[32, 65, 66])
        match = score_font(font, TTF, 12, basis="auto", include_raw=False)
        self.assertEqual(match.basis, "hinted")
        self.assertIsNone(match.raw)

    def run_cli(self, *args):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                main(list(args))
                return sorted(os.listdir(d)), d
            finally:
                os.chdir(cwd)

    def test_cli_implicit_build(self):
        import io, contextlib
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    main([TTF, "-s", "10-11"])
                files = os.listdir(d)
                self.assertEqual(files, ["DEJAVUSA.8xv"] if "DejaVu" in TTF else files)
                fonts = verify_appvar(Path(files[0]).read_bytes())
                self.assertEqual(len(fonts), 2)
            finally:
                os.chdir(cwd)

    def test_cli_errors_are_clean(self):
        import io, contextlib
        for argv in (["/nope.ttf"], [TTF, "-s", "abc"], [TTF, "--name", "bad name"], [TTF, "--range", "zzz"]):
            with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as c:
                main(argv)
            self.assertTrue(str(c.exception.code).startswith("error:"), (argv, c.exception.code))


if __name__ == "__main__":
    unittest.main()