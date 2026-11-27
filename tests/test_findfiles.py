"""Self-contained tests: they create their own sample files in a temp folder and a temp index.
They never read or write the real drives or the real index.

Run:  QT_QPA_PLATFORM=offscreen python3 -I -m unittest discover -s tests -v
"""
import base64
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import time
import unittest
import zipfile
import zlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safe_env  # noqa: E402,F401  (temp picture cache, no builder)

from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtGui import QImage, QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

import filetypes  # noqa: E402
import indexer  # noqa: E402
import previews  # noqa: E402
import search  # noqa: E402


def _wr(path, mode, data):
    with open(path, mode) as f:
        f.write(data)


def png_bytes(w=32, h=32, color=(200, 30, 30), pattern=True):
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor(*color))
    if pattern:
        for x in range(w // 2):
            img.setPixelColor(x, x % h, QColor(0, 0, 0))
    ba = QByteArray(); buf = QBuffer(ba); buf.open(QIODevice.WriteOnly); img.save(buf, "PNG")
    return bytes(ba)


def write_binary_stl(path, tris):
    with open(path, "wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", len(tris)))
        for t in tris:
            f.write(struct.pack("<3f", 0, 0, 1) + b"".join(struct.pack("<3f", *v) for v in t) + b"\0\0")


CUBE_TRIS = [((0, 0, 0), (10, 0, 0), (10, 20, 0)), ((0, 0, 0), (10, 20, 0), (0, 20, 0)),
             ((0, 0, 5), (10, 0, 5), (10, 20, 5)), ((0, 0, 5), (10, 20, 5), (0, 20, 5))]


class TextRules(unittest.TestCase):
    def test_norm_removes_both_diacritic_forms(self):
        self.assertEqual(indexer.norm("Cruciuliță Ştefan ȘTEFAN"), "cruciulita stefan stefan")

    def test_stem(self):
        for w in ("cruce", "cruci", "cruciulita"):
            self.assertEqual(search.stem(w), "cruc", w)
        self.assertEqual(search.stem("medalie"), "medal")
        self.assertEqual(search.stem("baza"), "baza")  # short words untouched

    def test_synonyms_both_ways(self):
        self.assertIn("cruc", search.variants("cross"))
        self.assertIn("cross", search.variants("cruce"))
        self.assertIn("cross", search.variants("cro"))  # typed start of a synonym

    def test_synonym_does_not_broaden(self):
        # regression 2026-11-10: "aurora" pulled in "aur" (gold) -> 500+ wrong hits
        self.assertNotIn("aur", search.variants("aurora"))
        self.assertNotIn("mic", search.variants("mica"))

    def test_like_escaping(self):
        self.assertEqual(search._like("a%b_c"), "%a\\%b\\_c%")

    def test_filetypes(self):
        opts = filetypes.filter_options()
        self.assertEqual(next(iter(opts)), "All my work")
        for e in ("stl", "obj", "dxf", "cdr", "ezd", "stm", "lys", "pm3m", "rscan"):
            self.assertIn(e, opts["All my work"])
        self.assertNotIn("lyx", opts["All my work"])
        self.assertEqual(filetypes.describe(""), "file")


class IndexAndSearch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = (indexer.ROOTS, indexer.LOOSE, indexer.DB_DIR, indexer.DB_PATH)
        work, copies = os.path.join(self.tmp, "work"), os.path.join(self.tmp, "backup")
        os.makedirs(os.path.join(work, "Crosses"))
        os.makedirs(os.path.join(work, "Software"))
        os.makedirs(copies)
        for p in ("Crosses/cruce 4-1 baza.stl", "Crosses/spate cruce celtica.stl", "Crosses/50%_off.dxf",
                  "Software/crossover tool.stl", "Crosses/aurora lamp.stl", "Crosses/aur medal.stl"):
            _wr(os.path.join(work, p), "w", "x")
        _wr(os.path.join(copies, "cruce 4-1 baza.stl"), "w", "x")
        # Software is a sub-folder of work here, so give it its own root that is scanned first
        indexer.ROOTS = [(os.path.join(work, "Crosses"), "Work", "work"),
                         (os.path.join(work, "Software"), "Storage › Software", "work"),
                         (copies, "Backups", "copy"), ("/run/media/__nope__/Drive/X", "Unplugged", "work")]
        indexer.LOOSE = []
        indexer.DB_DIR = self.tmp
        indexer.DB_PATH = os.path.join(self.tmp, "index.db")

    def tearDown(self):
        indexer.ROOTS, indexer.LOOSE, indexer.DB_DIR, indexer.DB_PATH = self.saved
        shutil.rmtree(self.tmp)

    def names(self, q, exts=None, copies=False):
        return [r[1] for r in search.query(q, exts, copies)]

    def test_build_and_find(self):
        n, missing = indexer.build()
        self.assertEqual(missing, ["Unplugged"])
        self.assertIn("cruce 4-1 baza.stl", self.names("cruci 4-1"))
        self.assertEqual(self.names("cross celtic"), ["spate cruce celtica.stl"])

    def test_macos_junk_skipped(self):
        _wr(os.path.join(self.tmp, "work", "Crosses", "._cruce 4-1 baza.stl"), "w", "x")
        _wr(os.path.join(self.tmp, "work", "Crosses", "Thumbs.db"), "w", "x")
        indexer.build()
        self.assertEqual(self.names("4-1"), ["cruce 4-1 baza.stl"])
        self.assertEqual(self.names("thumbs"), [])

    def test_copies_hidden_unless_asked(self):
        indexer.build()
        self.assertEqual(self.names("4-1").count("cruce 4-1 baza.stl"), 1)
        self.assertEqual(self.names("4-1", copies=True).count("cruce 4-1 baza.stl"), 2)

    def test_ranking_software_last(self):
        indexer.build()
        self.assertEqual(self.names("cross")[-1], "crossover tool.stl")

    def test_ext_filter_and_wildcards(self):
        indexer.build()
        self.assertEqual(self.names("50%", exts=["dxf"]), ["50%_off.dxf"])
        self.assertEqual(self.names("cruce", exts=["dxf"]), [])

    def test_aurora_not_gold(self):
        indexer.build()
        self.assertEqual(self.names("aurora"), ["aurora lamp.stl"])

    def test_no_write_when_unchanged(self):
        indexer.build()
        t1 = os.stat(indexer.DB_PATH).st_mtime_ns
        time.sleep(0.05)
        indexer.build()
        self.assertEqual(os.stat(indexer.DB_PATH).st_mtime_ns, t1)

    def test_unplugged_drive_keeps_old_entries(self):
        indexer.build()
        db = sqlite3.connect(indexer.DB_PATH)
        db.execute("INSERT INTO files(path,name,key,ext,size,mtime,loc,kind) VALUES('/run/media/__nope__/Drive/X/old.stl','old.stl','old.stl',"
                   "'stl',1,1,'Unplugged','work')")
        db.execute("UPDATE meta SET v='x' WHERE k='fingerprint'")
        db.commit(); db.close()
        indexer.build()
        self.assertIn("old.stl", self.names("old"))


class Previews(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def p(self, name):
        return os.path.join(self.tmp, name)

    def test_binary_stl(self):
        write_binary_stl(self.p("a.stl"), CUBE_TRIS)
        img, info = previews.render(self.p("a.stl"))
        self.assertIsNotNone(img); self.assertIn("10.0 × 20.0 × 5.0", info)

    def test_big_binary_stl_is_sampled(self):
        n = previews.MAX_TRIS * 3
        rec = struct.pack("<3f", 0, 0, 1) + struct.pack("<9f", 0, 0, 0, 1, 0, 0, 1, 1, 1) + b"\0\0"
        with open(self.p("big.stl"), "wb") as f:
            f.write(b"\0" * 80 + struct.pack("<I", n))
            f.write(rec * n)
        img, info = previews.render(self.p("big.stl"))
        self.assertIsNotNone(img); self.assertIn("1.0 × 1.0 × 1.0", info)

    def test_stl_with_invalid_numbers(self):
        # regression 2026-11-10: real damaged STLs hold NaN/huge values -> garbage preview
        nan = float("nan")
        write_binary_stl(self.p("n.stl"), CUBE_TRIS + [((nan, 0, 0), (1e30, 0, 0), (0, 0, 0))])
        img, info = previews.render(self.p("n.stl"))
        self.assertIsNotNone(img); self.assertIn("10.0 × 20.0 × 5.0", info)
        write_binary_stl(self.p("all.stl"), [((nan, nan, nan),) * 3])
        self.assertIsNone(previews.render(self.p("all.stl"))[0])

    def test_ascii_stl(self):
        with open(self.p("a.stl"), "w") as f:
            f.write("solid t\n")
            for t in CUBE_TRIS:
                f.write(" facet normal 0 0 1\n  outer loop\n")
                for v in t:
                    f.write("   vertex %f %f %f\n" % v)
                f.write("  endloop\n endfacet\n")
            f.write("endsolid t\n")
        img, info = previews.render(self.p("a.stl"))
        self.assertIsNotNone(img); self.assertIn("10.0 × 20.0", info)

    def test_obj(self):
        _wr(self.p("a.obj"), "w", "v 0 0 0\nv 5 0 0\nv 5 5 1\nf 1 2 3\n")
        self.assertIsNotNone(previews.render(self.p("a.obj"))[0])

    def test_zip_thumbnail_skips_blank(self):
        with zipfile.ZipFile(self.p("s.pwscene"), "w") as z:
            z.writestr("preview/image_0.png", png_bytes(pattern=False))  # flat colour -> skipped
            z.writestr("preview/image_1.png", png_bytes())
        self.assertIsNotNone(previews.render(self.p("s.pwscene"))[0])
        with zipfile.ZipFile(self.p("b.pwscene"), "w") as z:
            z.writestr("preview/image_0.png", png_bytes(pattern=False))
        self.assertIsNone(previews.render(self.p("b.pwscene"))[0])

    def test_cdr(self):
        with zipfile.ZipFile(self.p("a.cdr"), "w") as z:
            z.writestr("previews/thumbnail.png", png_bytes())
        self.assertIsNotNone(previews.render(self.p("a.cdr"))[0])
        _wr(self.p("old.cdr"), "wb", b"RIFF\0\0\0\0CDR9")
        self.assertIsNone(previews.render(self.p("old.cdr"))[0])

    def test_gcode_thumbnail(self):
        b64 = base64.b64encode(png_bytes()).decode()
        lines = "\n".join("; " + b64[i:i + 78] for i in range(0, len(b64), 78))
        _wr(self.p("a.gcode"), "w", f"; thumbnail begin 32x32 {len(b64)}\n{lines}\n; thumbnail end\nG1 X0\n")
        img, info = previews.render(self.p("a.gcode"))
        self.assertIsNotNone(img); self.assertIn("32×32", info)

    @staticmethod
    def _dib_header(w, h):
        return struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, 0, 0, 0, 0, 0)

    def test_3dm_valid_compressed_preview(self):
        w, h = 32, 20
        pix = bytes((x * 7) % 256 for x in range(((w * 3 + 3) & ~3) * h))
        data = b"3D Geometry File Format " + b"\0" * 50 + self._dib_header(w, h) + b"\0" * 21 + zlib.compress(pix)
        _wr(self.p("a.3dm"), "wb", data)
        self.assertIsNotNone(previews.render(self.p("a.3dm"))[0])

    def test_3dm_rejects_garbage(self):
        # regression 2026-11-10: a header followed by non-matching data produced noise images
        data = b"3D Geometry File Format " + self._dib_header(64, 64) + os.urandom(20000)
        _wr(self.p("b.3dm"), "wb", data)
        self.assertIsNone(previews.render(self.p("b.3dm"))[0])

    def test_lychee_png_at_start_and_end(self):
        _wr(self.p("a.lyt"), "wb", b"\x04\0\0\0" + b"\0" * 100 + png_bytes() + b"\0" * 1000)
        _wr(self.p("a.lys"), "wb", b"\x04\0\0\0" + os.urandom(5_000_000) + png_bytes() + b"\0" * 2000)
        self.assertIsNotNone(previews.render(self.p("a.lyt"))[0])
        self.assertIsNotNone(previews.render(self.p("a.lys"))[0])
        open(self.p("e.lys"), "wb").close()
        self.assertEqual(previews.render(self.p("e.lys")), (None, "Empty file"))

    def test_dxf(self):
        dxf = ("0\nSECTION\n2\nENTITIES\n"
               "0\nLINE\n8\n0\n10\n0\n20\n0\n11\n100\n21\n50\n"
               "0\nCIRCLE\n8\n0\n10\n50\n20\n25\n40\n10\n"
               "0\nTEXT\n8\n0\n10\n1\n20\n1\n1\nhello\n"
               "0\nENDSEC\n0\nEOF\n")
        _wr(self.p("a.dxf"), "w", dxf)
        img, info = previews.render(self.p("a.dxf"))
        self.assertIsNotNone(img)
        self.assertIn("100.0 × 50.0", info)
        self.assertIn("1 items not drawn", info)

    def test_svg(self):
        _wr(self.p("a.svg"), "w", 
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>')
        self.assertIsNotNone(previews.render(self.p("a.svg"))[0])

    def test_glb(self):
        pos = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 1)
        js = {"asset": {"version": "2.0"}, "buffers": [{"byteLength": len(pos)}],
              "bufferViews": [{"buffer": 0, "byteLength": len(pos)}],
              "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"}],
              "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}]}
        j = json.dumps(js).encode(); j += b" " * (-len(j) % 4)
        body = struct.pack("<I4s", len(j), b"JSON") + j + struct.pack("<I4s", len(pos), b"BIN\0") + pos
        _wr(self.p("a.glb"), "wb", b"glTF" + struct.pack("<II", 2, 12 + len(body)) + body)
        self.assertIsNotNone(previews.render(self.p("a.glb"))[0])

    def test_extension_from_name_not_folder(self):
        d = os.path.join(self.tmp, "folder.with.dots"); os.makedirs(d)
        _wr(os.path.join(d, "README"), "w", "x")
        self.assertEqual(previews.render(os.path.join(d, "README")), (None, "No preview (file has no extension)"))

    def test_missing_file(self):
        img, info = previews.render(self.p("gone.stl"))
        self.assertIsNone(img); self.assertIn("not found", info)

    def test_broken_file_never_raises(self):
        _wr(self.p("bad.3mf"), "wb", b"not a zip")
        img, info = previews.render(self.p("bad.3mf"))
        self.assertIsNone(img); self.assertTrue(info)


class Window(unittest.TestCase):
    def test_window_search_and_preview(self):
        import findfiles
        tmp = tempfile.mkdtemp()
        saved = (indexer.ROOTS, indexer.LOOSE, indexer.DB_DIR, indexer.DB_PATH)
        try:
            write_binary_stl(os.path.join(tmp, "cruce test.stl"), CUBE_TRIS)
            indexer.ROOTS = [(tmp, "Work", "work")]; indexer.LOOSE = []
            indexer.DB_DIR = tmp; indexer.DB_PATH = os.path.join(tmp, "i.db")
            w = findfiles.Main()
            end = time.time() + 15  # wait for the file list (a fixed 2 s was flaky)
            while not w._index_msg and time.time() < end:
                APP.processEvents(); time.sleep(0.02)
            self.assertEqual(w.type.currentText(), "All my work")
            w.q.setText("cruci"); w.run_search()
            self.assertEqual(w.table.rowCount(), 1)
            w.table.selectRow(0)
            for _ in range(150):
                APP.processEvents(); time.sleep(0.02)
                if not w.pic.pixmap().isNull():
                    break
            self.assertFalse(w.pic.pixmap().isNull())
            w.close()
        finally:
            indexer.ROOTS, indexer.LOOSE, indexer.DB_DIR, indexer.DB_PATH = saved
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
