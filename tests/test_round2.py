"""Tests for round 2 (2026-11-10): archives, exact copies, new previews, window behaviour.
Self-contained: temp files + temp index only.

Run:  QT_QPA_PLATFORM=offscreen python3 -I -m unittest discover -s tests -v
"""
import base64
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safe_env  # noqa: E402,F401  (temp picture cache, no builder)

from PySide6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

import archives  # noqa: E402
import copies  # noqa: E402
import indexer  # noqa: E402
import previews  # noqa: E402
import search  # noqa: E402
from test_findfiles import CUBE_TRIS, png_bytes, write_binary_stl  # noqa: E402


def wr(path, data, mode="w"):
    with open(path, mode) as f:
        f.write(data)


def stl_bytes(tris):
    out = b"\0" * 80 + struct.pack("<I", len(tris))
    for t in tris:
        out += struct.pack("<3f", 0, 0, 1) + b"".join(struct.pack("<3f", *v) for v in t) + b"\0\0"
    return out


class TempIndex(unittest.TestCase):
    """Base: a temp folder used as the only scanned root, and a temp index."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.saved = (indexer.ROOTS, indexer.LOOSE, indexer.DB_DIR, indexer.DB_PATH)
        indexer.ROOTS = [(self.work, "Work", "work")]
        indexer.LOOSE = []
        indexer.DB_DIR = os.path.join(self.tmp, "db")
        indexer.DB_PATH = os.path.join(indexer.DB_DIR, "index.db")

    def tearDown(self):
        indexer.ROOTS, indexer.LOOSE, indexer.DB_DIR, indexer.DB_PATH = self.saved
        shutil.rmtree(self.tmp)

    def names(self, q, copies_too=False):
        return [r[1] for r in search.query(q, None, copies_too)]


class Archives(TempIndex):
    def make_zip(self, name, members):
        p = os.path.join(self.work, name)
        with zipfile.ZipFile(p, "w") as z:
            for n, data in members.items():
                z.writestr(n, data)
        return p

    def test_split(self):
        self.assertEqual(archives.split("/a/b.ZIP::x/y.stl"), ("/a/b.ZIP", "x/y.stl"))
        self.assertIsNone(archives.split("/a/b.stl"))

    def test_work_members_indexed_fonts_not(self):
        self.make_zip("TransferNow-123.zip", {"mamuda corp.stl": stl_bytes(CUBE_TRIS),
                                              "fonts/arial.ttf": b"x", "__MACOSX/._mamuda corp.stl": b"x"})
        indexer.build()
        self.assertEqual(self.names("mamuda"), ["mamuda corp.stl"])
        self.assertEqual(self.names("arial"), [])

    def test_member_preview_and_cleanup(self):
        z = self.make_zip("a.zip", {"dir/cruce.stl": stl_bytes(CUBE_TRIS)})
        before = set(os.listdir(tempfile.gettempdir()))
        img, note = previews.render(z + "::dir/cruce.stl")
        self.assertIsNotNone(img)
        self.assertTrue(note.startswith("Inside archive"))
        self.assertIn("10.0 × 20.0 × 5.0", note)
        leftovers = [d for d in set(os.listdir(tempfile.gettempdir())) - before if d.startswith("findfiles-")]
        self.assertEqual(leftovers, [])

    def test_evil_member_name_stays_in_temp(self):
        z = self.make_zip("evil.zip", {"../../evil.stl": stl_bytes(CUBE_TRIS)})
        tmp, tmpdir = archives.extract_to_temp(z + "::../../evil.stl")
        try:
            self.assertEqual(os.path.dirname(tmp), tmpdir)
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.stl")))
        finally:
            shutil.rmtree(tmpdir)

    def test_listing_is_cached(self):
        self.make_zip("a.zip", {"x.stl": b"solid"})
        calls = []
        orig = archives.list_members
        archives.list_members = lambda p: calls.append(p) or orig(p)
        try:
            indexer.build()
            indexer.build()
        finally:
            archives.list_members = orig
        self.assertEqual(len(calls), 1)

    @unittest.skipUnless(shutil.which("7z"), "7z not installed")
    def test_7z_archive(self):
        src = os.path.join(self.tmp, "src")
        os.makedirs(src)
        write_binary_stl(os.path.join(src, "spate cruce.stl"), CUBE_TRIS)
        subprocess.run(["7z", "a", "-bd", os.path.join(self.work, "pack.7z"), os.path.join(src, "spate cruce.stl")],
                       capture_output=True, check=True)
        indexer.build()
        self.assertEqual(self.names("spate cruce"), ["spate cruce.stl"])
        path = search.query("spate cruce")[0][0]
        self.assertIsNotNone(previews.render(path)[0])


class Copies(TempIndex):
    def test_full_hash_not_size(self):
        a, b, c = (os.path.join(self.work, n) for n in ("a.stl", "b copy.stl", "c other.stl"))
        wr(a, b"A" * 1000, "wb")
        wr(b, b"A" * 1000, "wb")
        wr(c, b"A" * 999 + b"B", "wb")  # same size, different content
        wr(os.path.join(self.work, "d.stl"), b"A" * 10, "wb")
        indexer.build()
        res = copies.find(a, indexer.DB_PATH)
        self.assertEqual([os.path.basename(x[0]) for x in res["identical"]], ["b copy.stl"])
        self.assertEqual(res["different"], 1)

    def test_archive_member_refused(self):
        with self.assertRaises(ValueError):
            copies.find("/x/a.zip::b.stl", indexer.DB_PATH)


class NewPreviews(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def p(self, n):
        return os.path.join(self.tmp, n)

    def test_old_cdr_disp(self):
        w = h = 16
        pal = b"".join(struct.pack("<BBBB", i, i, i, 0) for i in range(256))
        pix = bytes((x * 13) % 256 for x in range(w * h))
        bih = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 8, 0, len(pix), 0, 0, 256, 0)
        chunk = struct.pack("<I", 8) + bih + pal + pix
        good = b"RIFF\0\0\0\0CDRDvrsn\x02\0\0\0\0\0DISP" + struct.pack("<I", len(chunk)) + chunk
        wr(self.p("a.cdr"), good, "wb")
        self.assertIsNotNone(previews.render(self.p("a.cdr"))[0])
        bad = b"RIFF\0\0\0\0CDRDDISP" + struct.pack("<I", len(chunk) + 50) + chunk + b"\0" * 50  # size mismatch
        wr(self.p("b.cdr"), bad, "wb")
        self.assertIsNone(previews.render(self.p("b.cdr"))[0])

    def test_anycubic(self):
        w, h = 32, 24
        pix = struct.pack(f"<{w * h}H", *[(x * 97) & 0xFFFF for x in range(w * h)])
        data = b"ANYCUBIC" + b"\0" * 140 + b"PREVIEW\0\0\0\0\0" + struct.pack("<IIII", len(pix) + 12, w, 120, h) + pix
        wr(self.p("a.pm3m"), data, "wb")
        img, note = previews.render(self.p("a.pm3m"))
        self.assertIsNotNone(img); self.assertIn("32×24", note)

    def test_cxdlp(self):
        small, big = b"\x21\x87" * (116 * 116), bytes((i * 7) % 256 for i in range(290 * 290 * 2))
        data = b"\0\0\0\x09CXSW3DV2" + b"\0" * 83 + small + b"\r\n" + big + b"\r\n" + big + b"\r\n"
        wr(self.p("a.cxdlp"), data, "wb")
        self.assertIsNotNone(previews.render(self.p("a.cxdlp"))[0])
        wr(self.p("b.cxdlp"), b"\0\0\0\x09CXSW3DV2" + b"\0" * 400000, "wb")  # markers missing
        self.assertIsNone(previews.render(self.p("b.cxdlp"))[0])

    def test_stm(self):
        pts = "".join(f"CSPoint\n201:{x}\n202:{y}\n203:0\nCSPointEnd\n" for x, y in ((0, 0), (13.5, 0), (13.5, 8)))
        wr(self.p("a.stm"), f"File Ver 3.0\nSECTION\nENTITIES\nPolyline\n{pts}PolylineEnd\nENDSEC\n")
        img, note = previews.render(self.p("a.stm"))
        self.assertIsNotNone(img); self.assertIn("13.5 × 8.0", note)
        wr(self.p("b.stm"), "File Ver 3.0\nSECTION\nENTITIES\nBezier\nBezierNode\nBezierNodeEnd\nENDSEC\n")
        self.assertIsNone(previews.render(self.p("b.stm"))[0])

    def test_gcode_toolpath_and_crlf_thumbnail(self):
        wr(self.p("a.gcode"), "G90\nG1 X0 Y0\nG1 X100 Y0 E1\nG1 X100 Y50 E2\nG0 X500 Y500\n")
        img, note = previews.render(self.p("a.gcode"))
        self.assertIsNotNone(img); self.assertIn("100.0 × 50.0 mm", note)  # travel G0 not counted
        b64 = base64.b64encode(png_bytes()).decode()
        wr(self.p("b.gcode"), f"; thumbnail begin 32x32 {len(b64)}\r\n; {b64}\r\n; thumbnail end\r\nG1 X0\r\n", "w")
        img, note = previews.render(self.p("b.gcode"))
        self.assertIn("Slicer thumbnail", note)  # regression: CRLF files lost their thumbnail

    def test_3mf_mesh_fallback(self):
        xml = ('<model><resources><object><mesh><vertices>'
               '<vertex x="0" y="0" z="0"/><vertex x="40" y="0" z="0"/><vertex x="40" y="40" z="70"/>'
               '</vertices></mesh></object></resources></model>')
        with zipfile.ZipFile(self.p("a.3mf"), "w") as z:
            z.writestr("3D/3dmodel.model", xml)
        img, note = previews.render(self.p("a.3mf"))
        self.assertIsNotNone(img); self.assertIn("40.0 × 40.0 × 70.0", note)

    def test_bspline_known_point(self):
        pts = previews._bspline([(0, 0), (1, 2), (2, 0)], [0, 0, 0, 1, 1, 1], 2, n=3)
        self.assertAlmostEqual(pts[1][0], 1.0); self.assertAlmostEqual(pts[1][1], 1.0)
        self.assertAlmostEqual(pts[0][0], 0.0); self.assertAlmostEqual(pts[-1][0], 2.0)
        self.assertIsNone(previews._bspline([(0, 0), (1, 1)], [0, 1], 3))  # invalid -> None

    def test_bulge_semicircle(self):
        pts = previews._bulge_arc((0, 0), (2, 0), 1.0, n=2)
        self.assertAlmostEqual(pts[0][0], 1.0); self.assertAlmostEqual(pts[0][1], -1.0)
        self.assertAlmostEqual(pts[-1][0], 2.0); self.assertAlmostEqual(pts[-1][1], 0.0, places=6)

    def test_dxf_lwpolyline_with_bulge(self):
        dxf = ("0\nSECTION\n2\nENTITIES\n0\nLWPOLYLINE\n8\n0\n90\n2\n70\n0\n"
               "10\n0\n20\n0\n42\n1\n10\n2\n20\n0\n0\nENDSEC\n0\nEOF\n")
        wr(self.p("a.dxf"), dxf)
        img, note = previews.render(self.p("a.dxf"))
        self.assertIsNotNone(img); self.assertIn("2.0 × 1.0", note)  # semicircle, not a flat line


class WindowRound2(TempIndex):
    def test_keep_selection_on_refresh_and_copies_view(self):
        import findfiles
        for n in ("cruce a.stl", "cruce b.stl"):
            write_binary_stl(os.path.join(self.work, n), CUBE_TRIS)  # identical files
        w = findfiles.Main()
        end = time.time() + 15  # wait for the file list (a fixed 2 s was flaky)
        while not w._index_msg and time.time() < end:
            APP.processEvents(); time.sleep(0.02)
        w.q.setText("cruce"); w.run_search()
        self.assertEqual(w.table.rowCount(), 2)
        w.table.selectRow(1)
        chosen = w.sel_path()
        w.on_indexed(2, ["Storage › Models"])  # background refresh finishing
        self.assertEqual(w.sel_path(), chosen)
        self.assertIn("Not plugged in", w.status.text())
        w.find_copies()
        for _ in range(200):
            APP.processEvents(); time.sleep(0.02)
            if not w._busy:
                break
        self.assertEqual(w._mode, "copies")
        self.assertEqual(w.table.rowCount(), 2)
        self.assertIn("1 exact copy", w.status.text())
        w.close()


if __name__ == "__main__":
    unittest.main()
