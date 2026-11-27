"""Model library (2026-11-11): picture cache, builder, size search, grid view.
Self-contained: temp files, temp index, temp picture cache (see safe_env.py).

Run:  QT_QPA_PLATFORM=offscreen python3 -I -m unittest discover -s tests -v
"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safe_env  # noqa: E402,F401

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

import indexer  # noqa: E402
import search  # noqa: E402
import thumbs  # noqa: E402
from test_findfiles import write_binary_stl  # noqa: E402
from test_round2 import TempIndex  # noqa: E402


def wait_indexed(w, timeout=15):
    """Wait for the window's background file-list build (a fixed sleep was flaky in the full run)."""
    end = time.time() + timeout
    while not w._index_msg and time.time() < end:
        APP.processEvents(); time.sleep(0.02)
    assert w._index_msg, "file list never finished"


def box(w, d, h):
    """Two triangles spanning a w × d × h box (enough for the size)."""
    return [((0, 0, 0), (w, 0, 0), (w, d, 0)), ((0, 0, h), (w, d, h), (0, d, h))]


class Library(TempIndex):
    def setUp(self):
        super().setUp()
        cache = os.path.join(self.tmp, "cache")
        self.saved_t = (thumbs.CACHE_DIR, thumbs.DB_PATH, thumbs.LOCK_PATH, thumbs.LOG_PATH)
        thumbs.CACHE_DIR = os.path.join(cache, "thumbs")
        thumbs.DB_PATH = os.path.join(cache, "thumbs.db")
        thumbs.LOCK_PATH = os.path.join(cache, "thumbs.lock")
        thumbs.LOG_PATH = os.path.join(cache, "thumbs.log")
        self.cache = cache

    def tearDown(self):
        thumbs.CACHE_DIR, thumbs.DB_PATH, thumbs.LOCK_PATH, thumbs.LOG_PATH = self.saved_t
        super().tearDown()

    def stl(self, name, w, d, h):
        p = os.path.join(self.work, name)
        write_binary_stl(p, box(w, d, h))
        return p

    def build(self, **kw):
        logs = []
        kw.setdefault("wait", lambda s: None)
        return thumbs.build(log=logs.append, **kw), logs

    # ---- cache
    def test_key_changes_with_file_version(self):
        k = thumbs.key("/a/b.stl", 100, 5.0)
        self.assertNotEqual(k, thumbs.key("/a/b.stl", 101, 5.0))
        self.assertNotEqual(k, thumbs.key("/a/b.stl", 100, 6.0))
        self.assertEqual(k, thumbs.key("/a/b.stl", 100, 5.0))

    def test_store_lookup_scales_and_drops_old_version(self):
        db = thumbs.connect()
        img = QImage(420, 420, QImage.Format_RGB888); img.fill(Qt.red)
        f1 = thumbs.store(db, "/x/a.stl", 1, 1.0, img, "Size: 60.0 × 40.0 × 12.0 (model units, usually mm)")
        self.assertEqual(QImage(f1).width(), thumbs.SIZE)
        f, dims, _ = thumbs.lookup(db, "/x/a.stl", 1, 1.0)
        self.assertEqual((f, dims), (f1, (60.0, 40.0, 12.0)))
        thumbs.store(db, "/x/a.stl", 2, 2.0, img, "")  # the file changed
        self.assertFalse(os.path.exists(f1))
        self.assertIsNone(thumbs.lookup(db, "/x/a.stl", 1, 1.0))
        thumbs.store(db, "/x/b.dxf", 3, 3.0, None, "No preview")  # "no picture" is remembered too
        self.assertEqual(thumbs.lookup(db, "/x/b.dxf", 3, 3.0)[:2], (None, None))
        db.close()

    # ---- builder
    def test_builder_makes_only_new_or_changed_and_writes_only_its_cache(self):
        a = self.stl("cruce a.stl", 60, 40, 12)
        self.stl("cruce b.stl", 10, 20, 5)
        indexer.build()
        before = set(os.listdir(self.work))
        made, _ = self.build()
        self.assertEqual(made, 2)
        self.assertEqual(self.build()[0], 0)  # nothing changed: nothing to do
        self.assertEqual(set(os.listdir(self.work)), before)  # user files untouched
        st = os.stat(a)
        os.utime(a, (st.st_atime, st.st_mtime + 10))
        indexer.build()
        self.assertEqual(self.build()[0], 1)
        for f in os.listdir(self.cache):
            self.assertTrue(f.startswith("thumbs"), f)

    def test_builder_waits_while_ram_is_low_and_gives_up(self):
        self.stl("cruce a.stl", 60, 40, 12)
        indexer.build()
        ram = iter([100, 100, 9999] + [9999] * 10)
        saved = thumbs.free_mb
        thumbs.free_mb = lambda: next(ram)
        waits = []
        try:
            made, logs = self.build(wait=waits.append)
        finally:
            thumbs.free_mb = saved
        self.assertEqual(made, 1)
        self.assertIn(30, waits)
        self.assertTrue(any("waiting" in m for m in logs))
        thumbs.free_mb = lambda: 100
        try:
            os.utime(os.path.join(self.work, "cruce a.stl"), (1, 1000))
            indexer.build()
            made, logs = self.build()
        finally:
            thumbs.free_mb = saved
        self.assertEqual(made, 0)
        self.assertTrue(any("stopping" in m for m in logs))

    def test_builder_skips_file_changed_after_listing(self):
        a = self.stl("cruce a.stl", 60, 40, 12)
        indexer.build()
        os.utime(a, (1, 1000))
        self.assertEqual(self.build()[0], 0)

    def test_builder_survives_a_failing_file(self):
        """Review 2026-11-11: a store() error (e.g. the window saving the same picture) killed the builder."""
        self.stl("cruce a.stl", 60, 40, 12)
        self.stl("cruce b.stl", 10, 20, 5)
        indexer.build()
        saved, calls = thumbs.store, []

        def flaky(db, path, *a):
            calls.append(path)
            if len(calls) == 1:
                raise FileNotFoundError("tmp picture renamed by another writer")
            return saved(db, path, *a)
        thumbs.store = flaky
        try:
            made, logs = self.build()
        finally:
            thumbs.store = saved
        self.assertEqual(made, 1)
        self.assertTrue(any("skipped" in m for m in logs))
        self.assertEqual(self.build()[0], 1)  # the failed one is made next time

    def test_temp_picture_name_is_per_writer(self):
        import threading
        db = thumbs.connect()
        img = QImage(8, 8, QImage.Format_RGB888); img.fill(Qt.red)
        errors = []

        def save():
            try:
                d = thumbs.connect()
                thumbs.store(d, "/x/same.stl", 1, 1.0, img, "")
                d.close()
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=save) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual([f for f in os.listdir(thumbs.CACHE_DIR) if "tmp" in f], [])
        self.assertIsNotNone(thumbs.lookup(db, "/x/same.stl", 1, 1.0)[0])
        db.close()

    def test_window_drops_saved_picture_when_file_no_longer_previews(self):
        import findfiles
        from PySide6.QtGui import QPixmap
        w = findfiles.Main()
        w.current_preview = "/gone.stl"
        px = QPixmap(10, 10); px.fill(Qt.red)
        w.pic.setPixmap(px)                       # the saved picture shown at click time
        w.on_preview("/gone.stl", None, "File not found (moved, deleted, or its drive is unplugged)")
        self.assertTrue(w.pic.pixmap().isNull())
        self.assertIn("not found", w.pic.text())
        w.close()

    def test_new_drawing_version_redraws_but_keeps_old_pictures_meanwhile(self):
        self.stl("cruce a.stl", 60, 40, 12)
        indexer.build()
        self.assertEqual(self.build()[0], 1)
        db = thumbs.connect()
        db.execute("DELETE FROM drawn")  # = pictures made by an older version (no 'drawn' row)
        db.commit()
        self.assertEqual(thumbs.stats()[2], 0)                 # still counts as having a size
        rows = [(r[0], r[1], r[2], r[3], r[4]) for r in
                indexer.connect_ro().execute("SELECT path,name,loc,size,mtime FROM files")]
        self.assertTrue(thumbs.dims_for(db, rows))             # size still shown
        r = rows[0]
        self.assertFalse(thumbs.current(db, r[0], r[3], r[4]))
        self.stl("cruce new.stl", 10, 10, 10)
        indexer.build()
        jobs = thumbs.todo(db)
        self.assertEqual([os.path.basename(j[0]) for j in jobs], ["cruce new.stl", "cruce a.stl"])  # new first
        db.close()
        self.assertEqual(self.build()[0], 2)
        db = thumbs.connect()
        self.assertTrue(thumbs.current(db, r[0], r[3], r[4]))
        db.close()

    def test_single_instance(self):
        import fcntl
        os.makedirs(self.cache, exist_ok=True)
        with open(thumbs.LOCK_PATH, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            made, logs = self.build()
        self.assertEqual(made, 0)
        self.assertIn("already running", logs[0])

    # ---- size search
    def test_parse_size(self):
        self.assertEqual(search.parse_size("60mm"), ("", 60.0))
        self.assertEqual(search.parse_size("cruce 60 mm"), ("cruce", 60.0))
        self.assertEqual(search.parse_size("cruce 60,5MM spate"), ("cruce spate", 60.5))
        for t in ("cruce 4-1", "mm", "cruce", "x60mmm", "0mm"):
            self.assertIsNone(search.parse_size(t)[1], t)

    def test_size_filter_15_percent_of_largest_side(self):
        self.stl("cruce mic.stl", 50, 30, 5)      # 50 < 51
        self.stl("cruce bun.stl", 66, 10, 3)      # 66 in 51-69
        self.stl("cruce mare.stl", 70, 70, 70)    # 70 > 69
        self.stl("medalie.stl", 10, 60, 4)        # 60, other name
        self.stl("cruce noua.stl", 60, 60, 60)    # no picture yet
        indexer.build()
        db = thumbs.connect()
        for r in indexer.connect_ro().execute("SELECT path,size,mtime FROM files"):
            if "noua" not in r[0]:
                img, note = __import__("previews").render(r[0], thumbs.SIZE)
                thumbs.store(db, r[0], r[1], r[2], img, note)
        db.close()
        rows, mm = search.query_sized("cruce 60mm")
        self.assertEqual(mm, 60.0)
        self.assertEqual([r[1] for r in rows], ["cruce bun.stl"])
        rows, _ = search.query_sized("60mm")
        self.assertEqual(sorted(r[1] for r in rows), ["cruce bun.stl", "medalie.stl"])
        rows, mm = search.query_sized("cruce")
        self.assertIsNone(mm); self.assertEqual(len(rows), 4)

    # ---- window
    def test_window_grid_same_rows_size_column_and_cache(self):
        import findfiles
        for n, s in (("cruce a.stl", 60), ("cruce b.stl", 20), ("cruce c.stl", 40)):
            self.stl(n, s, s / 2, 5)
        w = findfiles.Main()
        wait_indexed(w)
        w.q.setText("cruce"); w.run_search()
        self.assertEqual(w.table.rowCount(), 3)
        self.assertEqual(w.table.item(0, 5).text(), "")      # no picture yet
        w.table.selectRow(0)
        for _ in range(150):                                  # preview drawn + saved
            APP.processEvents(); time.sleep(0.02)
            if not w.pic.pixmap().isNull() and thumbs.stats()[1] >= 1:
                break
        self.assertGreaterEqual(thumbs.stats()[1], 1)
        first = w.sel_path()
        w.run_search(keep_selection=True)
        self.assertNotEqual(w.table.item(0, 5).text(), "")   # size from the cache
        w.sort_by(3)
        w.set_view("grid")
        self.assertTrue(w.in_grid())
        self.assertEqual([w.grid.item(i).data(Qt.UserRole) for i in range(w.grid.count())],
                         [w.table.item(i, 0).data(Qt.UserRole) for i in range(w.table.rowCount())])
        self.assertEqual(w.sel_path(), first)                 # selection carried over
        w.grid.setCurrentRow(2)
        chosen = w.sel_path()
        w.set_view("list")
        self.assertEqual(w.sel_path(), chosen)
        w.close()


class Bugcheck(TempIndex):
    """Found 2026-11-11 on real files / the user's screenshot."""

    @staticmethod
    def filled(img):
        """How solid the drawn shape is: inside each row, between its first and last drawn pixel,
        the share that is drawn (1.0 = solid surface; scattered dots score near 0)."""
        import numpy as np
        g = img.convertToFormat(QImage.Format_Grayscale8)
        a = np.frombuffer(g.constBits(), dtype=np.uint8, count=g.bytesPerLine() * g.height())
        a = a.reshape(g.height(), g.bytesPerLine())[:, :g.width()] > 40
        drawn = inside = 0
        for row in a:
            xs = np.flatnonzero(row)
            if len(xs) > 1:
                inside += xs[-1] - xs[0] + 1
                drawn += len(xs)
        return drawn / inside if inside > 2000 else 0.0  # a few dots = not solid

    def test_big_flat_triangles_draw_solid_not_dots(self):
        import zipfile
        import previews
        sq = [((0, 0, 0), (100, 0, 0), (100, 100, 0)), ((0, 0, 0), (100, 100, 0), (0, 100, 0))]
        stl = os.path.join(self.work, "plate.stl"); write_binary_stl(stl, sq)
        obj = os.path.join(self.work, "plate.obj")
        with open(obj, "w") as f:
            f.write("v 0 0 0\nv 100 0 0\nv 100 100 100\nv 0 100 100\nf 1/1 2/2 3/3 4/4\n")  # tilted: stays top view
        tmf = os.path.join(self.work, "plate.3mf")
        with zipfile.ZipFile(tmf, "w") as z:
            z.writestr("3D/3dmodel.model", '<model><resources><object id="1"><mesh><vertices>'
                       '<vertex x="0" y="0" z="0"/><vertex x="100" y="0" z="0"/><vertex x="100" y="100" z="0"/>'
                       '<vertex x="0" y="100" z="0"/></vertices><triangles><triangle v1="0" v2="1" v3="2"/>'
                       '<triangle v1="0" v2="2" v3="3"/></triangles></mesh></object></resources></model>')
        for p in (stl, obj, tmf):
            img, note = previews.render(p, 200)
            self.assertIsNotNone(img, p)
            self.assertGreater(self.filled(img), 0.9, p)   # was ~0 (2-4 dots)
            self.assertIn("100.0 × 100.0", note, p)        # size still from the real vertices

    def test_big_stl_drawn_in_chunks_matches_whole(self):
        import numpy as np
        import previews
        tris = []  # 40 x 40 grid of a 100 x 100 plate, tilted, = 3200 triangles
        for i in range(40):
            for j in range(40):
                x0, y0, x1, y1 = i * 2.5, j * 2.5, (i + 1) * 2.5, (j + 1) * 2.5
                tris += [((x0, y0, y0), (x1, y0, y0), (x1, y1, y1)), ((x0, y0, y0), (x1, y1, y1), (x0, y1, y1))]
        p = os.path.join(self.work, "relief.stl"); write_binary_stl(p, tris)
        whole, note_w = previews.render(p, 200)
        saved = (previews.MAX_TRIS, previews.STREAM_CHUNK)
        previews.MAX_TRIS, previews.STREAM_CHUNK = 1000, 700  # force the chunked path
        try:
            chunked, note_c = previews.render(p, 200)
        finally:
            previews.MAX_TRIS, previews.STREAM_CHUNK = saved
        self.assertEqual(note_c, note_w)  # exact size, not sampled
        self.assertNotIn("approximate", note_c)
        def pixels(im):  # keep the converted image alive while reading it (a temporary was freed -> flaky)
            g = im.convertToFormat(QImage.Format_Grayscale8)
            return np.frombuffer(g.constBits(), dtype=np.uint8, count=g.height() * g.bytesPerLine()).astype(int)
        a, b = pixels(whole), pixels(chunked)
        self.assertLess(float(np.mean(np.abs(a - b) > 30)), 0.02, (note_w, note_c, whole.size(), chunked.size()))
        self.assertGreater(self.filled(chunked), 0.9)

    def test_obj_line_continuation(self):
        import previews
        p = os.path.join(self.work, "flower.obj")  # real file 'flower floare 3d.zip::1.obj' did this
        with open(p, "w") as f:
            f.write("v 0 0 0\nv 50 0 \\\n 25\nv 50 50 50\nv 0 50 25\nf 1 2 3 4\n")
        img, note = previews.render(p, 200)
        self.assertIsNotNone(img, note)
        self.assertIn("50.0 × 50.0 × 50.0", note)

    def test_big_obj_is_sampled_not_refused(self):
        import previews
        p = os.path.join(self.work, "big.obj")
        with open(p, "w") as f:
            for i in range(20000):
                f.write(f"v {i % 100} {i // 100 % 100} {i // 10000}\n")
        saved = previews.OBJ_FULL_MAX
        previews.OBJ_FULL_MAX = 1000
        try:
            img, note = previews.render(p, 200)
        finally:
            previews.OBJ_FULL_MAX = saved
        self.assertIsNotNone(img, note)
        self.assertIn("sampled", note)

    def test_glb_triangles_draw_solid(self):
        import json
        import struct
        import previews
        pos = struct.pack("<12f", 0, 0, 0, 100, 0, 0, 100, 100, 100, 0, 100, 100)  # tilted square, Y-up
        idx = struct.pack("<6H", 0, 1, 2, 0, 2, 3) + b"\0\0"
        js = {"asset": {"version": "2.0"}, "buffers": [{"byteLength": len(pos) + len(idx)}],
              "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(pos)},
                              {"buffer": 0, "byteOffset": len(pos), "byteLength": 12}],
              "accessors": [{"bufferView": 0, "componentType": 5126, "count": 4, "type": "VEC3"},
                            {"bufferView": 1, "componentType": 5123, "count": 6, "type": "SCALAR"}],
              "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}]}
        j = json.dumps(js).encode(); j += b" " * (-len(j) % 4)
        b = pos + idx
        data = (b"glTF" + struct.pack("<II", 2, 12 + 8 + len(j) + 8 + len(b)) + struct.pack("<I", len(j))
                + b"JSON" + j + struct.pack("<I", len(b)) + b"BIN\0" + b)
        p = os.path.join(self.work, "plate.glb")
        with open(p, "wb") as f:
            f.write(data)
        img, note = previews.render(p, 200)
        self.assertIsNotNone(img, note)
        self.assertGreater(self.filled(img), 0.9)

    def test_y_up_models_are_turned_not_mirrored(self):
        import numpy as np
        import previews
        m = previews.y_up_to_z_up(np.eye(3, dtype=np.float32))
        self.assertAlmostEqual(float(np.linalg.det(m)), 1.0)  # -1 would mirror (text reads backwards)
        self.assertEqual(m[1].tolist(), [0, 0, 1])           # model's up (Y) becomes Z

    def test_long_archive_name_does_not_push_columns_off_screen(self):
        import zipfile
        import findfiles
        from test_round2 import stl_bytes
        from test_findfiles import CUBE_TRIS
        long = "uploads_files_2793946_FBI+Seal+Logo+3D+model_OBJ_with_a_very_long_name_indeed.zip"
        with zipfile.ZipFile(os.path.join(self.work, long), "w") as z:
            z.writestr("model seal.stl", stl_bytes(CUBE_TRIS))
        w = findfiles.Main()
        wait_indexed(w)
        w.q.setText("model seal"); w.run_search()
        self.assertEqual(w.table.rowCount(), 1)
        self.assertLessEqual(w.table.columnWidth(1), findfiles.MAX_COL)
        self.assertIn(long, w.table.item(0, 1).toolTip())  # full text still there
        w.close()


if __name__ == "__main__":
    unittest.main()
