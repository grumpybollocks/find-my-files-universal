"""Code audit 2026-11-11: one regression test per bug fixed. Temp files only.

Run:  QT_QPA_PLATFORM=offscreen python3 -I -m unittest discover -s tests -v
"""
import glob
import os
import sqlite3
import struct
import sys
import tempfile
import time
import unittest
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safe_env  # noqa: E402,F401

from PySide6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

import archives  # noqa: E402
import copies  # noqa: E402
import indexer  # noqa: E402
import previews  # noqa: E402
import search  # noqa: E402
import thumbs  # noqa: E402
from test_findfiles import CUBE_TRIS, write_binary_stl  # noqa: E402
from test_round2 import TempIndex, stl_bytes  # noqa: E402


def wr(path, data, mode="w"):
    with open(path, mode) as f:
        f.write(data)


class Previews(TempIndex):
    def p(self, name):
        return os.path.join(self.work, name)

    def test_corrupt_zip_member_gives_a_reason_and_leaves_no_temp(self):
        z = self.p("parts.zip")
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("corp.stl", stl_bytes(CUBE_TRIS * 50))
        data = bytearray(open(z, "rb").read())
        for k in range(60, 160):  # scramble the deflate stream (zlib.error escaped render() before)
            data[k] ^= 0x5A
        wr(z, bytes(data), "wb")
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "findfiles-*")))
        img, note = previews.render(z + archives.SEP + "corp.stl")
        self.assertIsNone(img)
        self.assertTrue(note)
        self.assertEqual(set(glob.glob(os.path.join(tempfile.gettempdir(), "findfiles-*"))), before)

    @unittest.skipIf(os.geteuid() == 0, "root can read anything")
    def test_unreadable_small_file_never_raises(self):
        f = self.p("locked.stl")
        wr(f, "solid x\nendsolid x\n")
        os.chmod(f, 0)
        try:
            img, note = previews.render(f)
        finally:
            os.chmod(f, 0o644)
        self.assertIsNone(img)
        self.assertTrue(note)

    def test_old_corel_preview_cut_off_is_refused(self):
        w = h = 300  # 24-bit 300x300 = 270 KB of pixels, but the file ends after 10 KB
        n = 4 + 40 + w * 3 * h
        hdr = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, 0, 0, 0, 0, 0)
        wr(self.p("old.cdr"), b"RIFF" + b"\0" * 8 + b"DISP" + struct.pack("<I", n) + b"\0" * 4 + hdr
           + b"\x80" * 10_000, "wb")
        img, note = previews.render(self.p("old.cdr"))
        self.assertIsNone(img)  # was: a half-black picture
        self.assertIn("cut-off", note)

    def test_obj_relative_faces_and_bad_vertex_keep_numbering(self):
        data = b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf -3 -2 -1\nv 5 5 5\nv 6 5 5\nv 5 6 5\nf -3 -2 -1\n"
        self.assertEqual(previews._obj_faces(data), [(0, 1, 2), (3, 4, 5)])
        pts, _ = previews._obj_vertices(b"v 0 0 0\nv 1 x 0\nv 0 1 0\n")
        self.assertEqual(len(pts), 3)  # bad line kept as NaN: later faces still point right

    def test_3mf_and_dxf_invalid_numbers(self):
        with zipfile.ZipFile(self.p("bad.3mf"), "w") as z:
            z.writestr("3D/3dmodel.model", '<model><mesh><vertices><vertex x="0" y="0" z="0"/>'
                       '<vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="5"/><vertex x="1e40" y="0" z="0"/>'
                       '</vertices><triangles><triangle v1="0" v2="1" v3="2"/><triangle v1="0" v2="1" v3="3"/>'
                       '</triangles></mesh></model>')
        img, note = previews.render(self.p("bad.3mf"))
        self.assertIsNotNone(img)
        self.assertIn("10.0 × 10.0 × 5.0", note)  # was "inf × 10 × 5"
        wr(self.p("nan.dxf"), "0\nSECTION\n2\nENTITIES\n0\nCIRCLE\n8\n0\n10\n0\n20\n0\n40\nnan\n0\nENDSEC\n0\nEOF\n")
        img, note = previews.render(self.p("nan.dxf"))
        self.assertIsNone(img)
        self.assertNotIn("nan ×", note)

    def test_gcode_g01_laser_power_and_g91_1(self):
        wr(self.p("laser.gcode"), "G90\nG91.1\nM3 S0\nG00 X0 Y0\nG01 X10 Y0 S800\nG01 X10 Y10\nG01 X0 Y10\n"
                                  "G01 X0 Y0\nM5\nG01 X50 Y50\n")
        img, note = previews.render(self.p("laser.gcode"))
        self.assertIsNotNone(img, note)
        self.assertIn("10.0 × 10.0", note)  # the M5 (laser off) move to 50,50 is not drawn

    def test_gcode_printer_temperatures_are_not_laser_power(self):
        """Review of the fix: M104 S215 made every travel move (G1 without E) a drawn line."""
        wr(self.p("fdm.gcode"), "M104 S215\nM140 S60\nM106 S255\nG1 X0 Y0\nG1 X20 Y0 E1\nG1 X20 Y20 E2\n"
                                "G1 X200 Y200 F9000\n")
        img, note = previews.render(self.p("fdm.gcode"))
        self.assertIn("20.0 × 20.0", note)

    def test_gcode_laser_m5_then_m3_keeps_power(self):
        wr(self.p("two.gcode"), "M3 S500\nG0 X0 Y0\nG1 X10 Y0\nG1 X10 Y10\nM5\nM500\nG0 X50 Y50\n"
                                "M3\nG1 X60 Y50\nG1 X60 Y60\n")
        img, note = previews.render(self.p("two.gcode"))
        self.assertIn("60.0 × 60.0", note)  # second cut drawn: S is modal across M5/M3

    def test_gcode_bad_thumbnail_falls_back_to_toolpath(self):
        wr(self.p("t.gcode"), "; thumbnail begin 300x300 8\n; !!!!notbase64\n; thumbnail end\n"
                              "G1 X0 Y0\nG1 X20 Y0 E1\nG1 X20 Y20 E2\n")
        img, note = previews.render(self.p("t.gcode"))
        self.assertIsNotNone(img, note)
        self.assertIn("Toolpath", note)

    def test_blank_lychee_preview_is_not_shown(self):
        from test_findfiles import png_bytes
        wr(self.p("blank.lys"), b"\x04\0\0\0" + os.urandom(5_000_000)
           + png_bytes(color=(255, 255, 255), pattern=False) + b"\0" * 2000, "wb")
        img, note = previews.render(self.p("blank.lys"))
        self.assertIsNone(img)
        self.assertIn("blank", note)

    def test_lychee_file_with_wrong_extension(self):
        """Real file 2026-11-12: a Lychee scene saved as 'fax plug2.stl'."""
        from test_findfiles import png_bytes
        wr(self.p("plug.stl"), b'\x04\0\0\0 \x02\0\0{"version":"3.1.0","mangoFiles":{"preview.png":1}}'
           + os.urandom(5_000_000) + png_bytes() + b"\0" * 2000, "wb")
        img, note = previews.render(self.p("plug.stl"))
        self.assertIsNotNone(img, note)
        self.assertIn("really a Lychee file", note)

    def test_cut_off_3mf_thumbnail_recovered_only_if_complete(self):
        """Real file 2026-11-12: a 31 KB .3mf missing its zip directory; thumbnail stored intact."""
        from test_findfiles import png_bytes
        png = png_bytes()
        with zipfile.ZipFile(self.p("full.3mf"), "w") as z:  # stored = PNG bytes appear as-is
            z.writestr("Metadata/thumbnail.png", png)
            z.writestr("3D/3dmodel.model", "<model>" + "x" * 5000)
        data = open(self.p("full.3mf"), "rb").read()
        wr(self.p("cut.3mf"), data[: data.find(b"<model>") + 100], "wb")  # no central directory
        img, note = previews.render(self.p("cut.3mf"))
        self.assertIsNotNone(img, note)
        self.assertIn("recovered", note)
        wr(self.p("cut2.3mf"), data[: data.find(png) + len(png) // 2], "wb")  # picture itself cut
        img, note = previews.render(self.p("cut2.3mf"))
        self.assertIsNone(img)  # a half picture is never shown

    def test_binary_stl_with_padding(self):
        write_binary_stl(self.p("pad.stl"), CUBE_TRIS)
        wr(self.p("pad.stl"), b"\0" * 7, "ab")
        img, note = previews.render(self.p("pad.stl"))
        self.assertIsNotNone(img, note)
        self.assertIn("10.0 × 20.0 × 5.0", note)


class IndexAndCopies(TempIndex):
    def test_kind_change_rebuilds_the_index(self):
        wr(os.path.join(self.work, "cruce.stl"), "x")
        indexer.build()
        indexer.ROOTS = [(self.work, "Work", "copy")]
        indexer.build()  # same files, only the kind changed: was skipped by the fingerprint
        kinds = {r[0] for r in indexer.connect_ro().execute("SELECT kind FROM files")}
        self.assertEqual(kinds, {"copy"})

    def test_failed_archive_listing_is_not_cached(self):
        z = os.path.join(self.work, "a.zip")
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("corp.stl", "x")
        saved = archives.list_members
        archives.list_members = lambda p: None  # e.g. 7z timed out
        try:
            indexer.build()
        finally:
            archives.list_members = saved
        self.assertEqual(self.names("corp"), [])
        indexer.build()  # next start: listed for real now
        self.assertEqual(self.names("corp"), ["corp.stl"])

    def test_search_never_creates_an_index(self):
        indexer.DB_PATH = os.path.join(self.tmp, "nothing", "index.db")
        self.assertEqual(search.query("cruce"), [])
        self.assertFalse(os.path.exists(indexer.DB_PATH))

    def test_copies_skip_empty_files_and_hard_links(self):
        a, b, c = (os.path.join(self.work, n) for n in ("a.stl", "b.stl", "empty.stl"))
        wr(a, "same bytes")
        os.link(a, b)  # the same file twice, not a copy
        wr(c, "")
        indexer.build()
        self.assertEqual(copies.candidates(c, indexer.DB_PATH)[1], [])
        self.assertEqual(copies.find(a, indexer.DB_PATH)["identical"], [])


class Growth(TempIndex):
    """Files keep being added, moved and deleted: the cache must follow, never grow forever."""

    def setUp(self):
        super().setUp()
        c = os.path.join(self.tmp, "cache")
        self.saved_t = (thumbs.CACHE_DIR, thumbs.DB_PATH, thumbs.LOCK_PATH)
        thumbs.CACHE_DIR, thumbs.DB_PATH, thumbs.LOCK_PATH = (os.path.join(c, "thumbs"), os.path.join(c, "t.db"),
                                                              os.path.join(c, "t.lock"))

    def tearDown(self):
        thumbs.CACHE_DIR, thumbs.DB_PATH, thumbs.LOCK_PATH = self.saved_t
        super().tearDown()

    def test_moved_or_deleted_files_lose_their_pictures(self):
        a = os.path.join(self.work, "cruce a.stl")
        b = os.path.join(self.work, "cruce b.stl")
        write_binary_stl(a, CUBE_TRIS); write_binary_stl(b, CUBE_TRIS)
        indexer.build()
        self.assertEqual(thumbs.build(log=lambda m: None, wait=lambda s: None), 2)
        stray = os.path.join(thumbs.CACHE_DIR, "0" * 40 + ".png")
        wr(stray, b"x", "wb")
        os.utime(stray, (1, time.time() - 3600))  # a stray file from an hour ago (new ones are left alone)
        os.remove(a)                      # deleted (or moved) by the user
        indexer.build()
        logs = []
        thumbs.build(log=logs.append, wait=lambda s: None)
        db = thumbs.connect()
        self.assertEqual([r[0] for r in db.execute("SELECT path FROM thumbs")], [b])
        db.close()
        self.assertEqual(len([n for n in os.listdir(thumbs.CACHE_DIR) if n.endswith(thumbs.PIC_EXTS)]), 1)
        self.assertTrue(any("removed" in m for m in logs))

    def test_empty_or_missing_file_list_never_wipes_the_cache(self):
        write_binary_stl(os.path.join(self.work, "cruce.stl"), CUBE_TRIS)
        indexer.build()
        thumbs.build(log=lambda m: None, wait=lambda s: None)
        indexer.DB_PATH = os.path.join(self.tmp, "missing.db")
        db = thumbs.connect()
        self.assertEqual(thumbs.prune(db), 0)
        self.assertEqual(db.execute("SELECT count(*) FROM thumbs").fetchone()[0], 1)
        db.close()

    def test_f5_refresh_finds_new_files_without_restart(self):
        import findfiles
        from test_library import wait_indexed
        w = findfiles.Main()
        wait_indexed(w)
        w.q.setText("cruce nou"); w.run_search()
        self.assertEqual(w.table.rowCount(), 0)
        write_binary_stl(os.path.join(self.work, "cruce nou.stl"), CUBE_TRIS)  # saved while the app is open
        w._index_msg = ""
        w.refresh_index(); w.refresh_index()                                   # double F5 = one refresh
        wait_indexed(w)
        self.assertEqual(w.table.rowCount(), 1)
        w.close()


class WindowFlows(TempIndex):
    """Second-pass review 2026-11-12: keyboard, hidden selection, sort, status, search words."""

    def window(self, names):
        import findfiles
        from test_library import wait_indexed
        for i, n in enumerate(names):
            write_binary_stl(os.path.join(self.work, n), CUBE_TRIS * (i + 1))  # different byte sizes
            os.utime(os.path.join(self.work, n), (1, 1_700_000_000 + i))
        w = findfiles.Main()
        wait_indexed(w)
        return w

    def test_keys_act_only_where_the_focus_is(self):
        import findfiles
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        w = self.window(["cruce a.stl", "cruce b.stl", "cruce c.stl"])
        opened = []
        saved = findfiles.subprocess.Popen
        findfiles.subprocess.Popen = lambda cmd, **kw: opened.append(cmd)
        try:
            w.show(); w.activateWindow()
            w.q.setText("cruce"); w.run_search()
            w.table.setFocus(); w.table.selectRow(0)
            QTest.keyClick(w.table, Qt.Key_Down); APP.processEvents()
            self.assertEqual(w.table.currentRow(), 1)              # Down moves in the list
            w.q.setFocus()
            QTest.keyClick(w.q, Qt.Key_Return); APP.processEvents()
            self.assertEqual(opened, [])                           # Enter in the box doesn't open Dolphin
            w.table.setFocus()
            QTest.keyClick(w.table, Qt.Key_Escape); APP.processEvents()
            self.assertEqual(w.q.text(), "cruce")                  # Esc outside the box doesn't clear it
        finally:
            findfiles.subprocess.Popen = saved
            w.close()

    def test_no_hidden_selection_after_new_results(self):
        w = self.window(["cruce a.stl", "cruce d.stl"])
        w.q.setText("cruce a"); w.run_search(); w.table.selectRow(0)
        w.q.setText("cruce d"); w.run_search()
        w.q.setText("cruce"); w.run_search()
        self.assertIsNone(w.sel_path())          # buttons can't act on a row you can't see
        self.assertIsNone(w.current_preview)
        w.close()

    def test_sort_survives_refresh_and_status_clears(self):
        w = self.window(["cruce a.stl", "cruce b.stl", "cruce c.stl"])
        w.q.setText("cruce"); w.run_search()
        w.sort_by(3)  # Size, small first
        first = w.table.item(0, 0).text()
        self.assertEqual(first, "cruce a.stl")
        w.on_indexed(3, [])                       # background refresh / F5
        self.assertEqual(w.table.item(0, 0).text(), first)
        w.set_view("grid")
        self.assertEqual(w.grid.item(0).text().split("\n")[0], first)  # grid follows the sorted list
        w.set_view("list")
        w.q.setText(""); w.run_search()
        self.assertNotIn("found", w.status.text())
        w.close()

    def test_same_search_again_keeps_the_click_and_empty_states_explain(self):
        w = self.window(["cruce a.stl", "cruce b.stl"])
        w.q.setText("cruce"); w.run_search(); w.table.selectRow(1)
        chosen = w.sel_path()
        w.run_search()                      # the typing timer firing after a quick click
        self.assertEqual(w.sel_path(), chosen)
        self.assertIs(w.stack.currentWidget(), w.table)
        w.q.setText("zzz nothing"); w.run_search()
        self.assertIs(w.stack.currentWidget(), w.hint)
        self.assertIn("Nothing found", w.hint.text())
        self.assertEqual(w.status.text().split(".")[0], "Nothing found")
        w.set_view("grid")                  # the hint stays; the choice is remembered
        self.assertTrue(w.in_grid()); self.assertIs(w.stack.currentWidget(), w.hint)
        w.q.setText("cruce"); w.run_search()
        self.assertIs(w.stack.currentWidget(), w.grid)
        w.q.setText(""); w.run_search()
        self.assertIn("Type a few words", w.hint.text())
        w.close()

    def test_size_word_in_the_name_still_finds_the_file(self):
        wr(os.path.join(self.work, "stea 34mm.dxf"), "0\nEOF\n")
        indexer.build()
        rows, mm = search.query_sized("stea 34mm")
        self.assertEqual(mm, 34.0)
        self.assertEqual([r[1] for r in rows], ["stea 34mm.dxf"])

    def test_short_plurals_and_oane(self):
        for n in ("baza cruce.stl", "medalion mic.stl", "manusa.stl"):
            wr(os.path.join(self.work, n), "x")
        indexer.build()
        self.assertEqual(self.names("baze"), ["baza cruce.stl"])
        self.assertEqual(self.names("medalioane"), ["medalion mic.stl"])
        self.assertEqual(self.names("mana"), [])  # no 3-letter stem "man" (would match manusa)


class WatchMode(TempIndex):
    """ROADMAP 0.1 (2026-11-12): new files show up by themselves; only changed rows are written."""

    def db_stamp(self):
        st = os.stat(indexer.DB_PATH)
        return st.st_mtime_ns, st.st_ino

    def test_partial_update_follows_every_kind_of_change(self):
        sub = os.path.join(self.work, "Crosses")
        os.makedirs(sub)
        write_binary_stl(os.path.join(sub, "cruce a.stl"), CUBE_TRIS)
        indexer.build()
        self.assertIn(sub, indexer.WATCH_DIRS)
        stamp = self.db_stamp()
        self.assertEqual(indexer.update_dirs([sub])[3], False)          # nothing changed: no write
        self.assertEqual(self.db_stamp(), stamp)
        write_binary_stl(os.path.join(sub, "cruce nou.stl"), CUBE_TRIS)  # new file
        newdir = os.path.join(self.work, "Medals 2026")
        os.makedirs(os.path.join(newdir, "parts"))
        wr(os.path.join(newdir, "parts", "medalie corp.stl"), "x")       # new folder tree
        added, removed, new_dirs, written = indexer.update_dirs([sub, self.work])
        self.assertEqual((added, removed, written), (2, 0, True))
        self.assertIn(os.path.join(newdir, "parts"), new_dirs)
        self.assertEqual(sorted(self.names("cruce")), ["cruce a.stl", "cruce nou.stl"])
        self.assertEqual(self.names("medalie"), ["medalie corp.stl"])
        os.rename(newdir, os.path.join(self.work, "Medals"))             # folder renamed
        os.remove(os.path.join(sub, "cruce a.stl"))                      # file deleted
        added, removed, _, _ = indexer.update_dirs([self.work, sub])
        self.assertEqual((added, removed), (1, 2))
        self.assertEqual(self.names("cruce"), ["cruce nou.stl"])
        paths = [r[0] for r in search.query("medalie")]
        self.assertTrue(paths[0].startswith(os.path.join(self.work, "Medals") + "/"))
        stamp = self.db_stamp()
        indexer.build()                                                  # next start: same fingerprint
        self.assertEqual(self.db_stamp(), stamp)                        # -> no 65 MB rewrite

    def test_drive_root_update_never_drops_nested_locations_or_unplugged_drives(self):
        """Drive root = loose-files location; Storage › Models etc. are separate locations inside it."""
        indexer.LOOSE = [(self.tmp, "Drive root")]
        wr(os.path.join(self.tmp, "loose cruce.stl"), "x")
        write_binary_stl(os.path.join(self.work, "cruce model.stl"), CUBE_TRIS)
        indexer.build()
        self.assertEqual(len(self.names("cruce")), 2)
        os.rename(self.work, self.work + "_away")                 # looks like a "deleted" subfolder
        try:
            indexer.update_dirs([self.tmp])
            self.assertIn("cruce model.stl", self.names("cruce"))  # other location's rows untouched
            saved = indexer._drive_missing
            indexer._drive_missing = lambda d: True                # drive being unplugged
            try:
                os.remove(os.path.join(self.tmp, "loose cruce.stl"))
                self.assertEqual(indexer.update_dirs([self.tmp])[3], False)
                self.assertEqual(len(self.names("cruce")), 2)     # last known files kept
            finally:
                indexer._drive_missing = saved
        finally:
            os.rename(self.work + "_away", self.work)

    def test_review_fixes_no_duplicates_unreadable_kept_music_not_watched(self):
        empty = os.path.join(self.work, "empty")
        os.makedirs(empty)
        indexer.build()
        wr(os.path.join(empty, "n.stl"), "x")             # saved into a folder that had no files
        indexer.update_dirs([self.work, empty])           # parent + folder in the same burst
        self.assertEqual([r[1] for r in search.query("n.stl")], ["n.stl"])  # once, not twice
        locked = os.path.join(self.work, "locked")
        os.makedirs(locked)
        wr(os.path.join(locked, "cruce.stl"), "x")
        indexer.update_dirs([self.work])
        self.assertEqual(self.names("cruce"), ["cruce.stl"])
        if os.geteuid() != 0:
            os.chmod(locked, 0)                            # like a USB drive pulled without unmounting
            try:
                indexer.update_dirs([locked, self.work])
                self.assertEqual(self.names("cruce"), ["cruce.stl"])  # last known files kept
            finally:
                os.chmod(locked, 0o755)
        self.assertFalse(indexer._watched("Stick (USB) › Old music", "work"))
        self.assertTrue(indexer._watched("Storage › Models", "work"))

    def test_efficiency_update_reads_only_its_folder_and_keeps_the_fingerprint(self):
        """2026-11-13: watch updates read just the changed folder's rows (not all 250k) and adjust the
        fingerprint row by row - it must still equal a full scan's, or every start rewrites 65 MB."""
        a, sib = os.path.join(self.work, "Cruci"), os.path.join(self.work, "Cruci-old")
        for d in (a, sib, a + "0"):  # '-' sorts before '/', '0' right after it: range edges
            os.makedirs(d)
            wr(os.path.join(d, "cruce.stl"), "x")
        indexer.build()
        os.remove(os.path.join(sib, "cruce.stl"))   # a change the update of `a` must not see
        os.remove(os.path.join(a + "0", "cruce.stl"))
        wr(os.path.join(a, "cruce nou.stl"), "x")
        added, removed, _, written = indexer.update_dirs([a])
        self.assertEqual((added, removed, written), (1, 0, True))
        self.assertEqual(len(self.names("cruce")), 4)  # the siblings' rows are untouched
        indexer.update_dirs([self.work, sib, a + "0"])
        self.assertEqual(len(self.names("cruce")), 2)
        stamp = self.db_stamp()
        indexer.build()
        self.assertEqual(self.db_stamp(), stamp)       # same fingerprint as a full scan: no rewrite
        db = sqlite3.connect(indexer.DB_PATH)            # a fingerprint from an older version
        db.execute("UPDATE meta SET v='0123abcd' WHERE k='fingerprint'"); db.commit(); db.close()
        wr(os.path.join(a, "cruce 3.stl"), "x")
        self.assertEqual(indexer.update_dirs([a])[3], True)
        stamp = self.db_stamp()
        indexer.build()
        self.assertEqual(self.db_stamp(), stamp)       # counted once from the whole list, then exact
        self.assertEqual(indexer.norm("Ștefan CRUCE"), "stefan cruce")
        self.assertEqual(indexer.norm("Plain ASCII.stl"), "plain ascii.stl")

    def test_window_lists_new_files_by_itself(self):
        import findfiles
        from test_library import wait_indexed
        sub = os.path.join(self.work, "Work")
        os.makedirs(sub)
        saved_env = os.environ.pop("FINDFILES_NO_WATCH")
        saved_delay = findfiles.WATCH_DELAY_MS
        findfiles.WATCH_DELAY_MS = 300
        try:
            w = findfiles.Main()
            wait_indexed(w)
            self.assertIn(sub, w.watcher.directories())
            updates = []
            w.bus.watched.connect(lambda *a: updates.append(a))
            w.q.setText("cruce"); w.run_search()
            self.assertEqual(w.table.rowCount(), 0)
            for i in range(30):                                          # a burst of saves
                write_binary_stl(os.path.join(sub, f"cruce {i}.stl"), CUBE_TRIS)
            end = time.time() + 10
            while w.table.rowCount() < 30 and time.time() < end:
                APP.processEvents(); time.sleep(0.05)
            self.assertEqual(w.table.rowCount(), 30)
            self.assertEqual(len(updates), 1)                            # one update for the burst
            self.assertIn("30 new files", w.status.text())
            self.assertTrue(w._builder_timer.isActive())  # pictures: one delayed run, not a process per update
            os.remove(os.path.join(sub, "cruce 0.stl"))
            end = time.time() + 10
            while w.table.rowCount() != 29 and time.time() < end:
                APP.processEvents(); time.sleep(0.05)
            self.assertEqual(w.table.rowCount(), 29)
            w.close()
        finally:
            os.environ["FINDFILES_NO_WATCH"] = saved_env
            findfiles.WATCH_DELAY_MS = saved_delay


class Handoff(TempIndex):
    """ROADMAP 1.1 + 1.2 (2026-11-12): Open with (user's own pick per file type) and drag & drop."""

    def apps_dir(self):
        d = os.path.join(self.tmp, "applications")
        os.makedirs(d, exist_ok=True)
        entries = {
            "orca.desktop": "Name=OrcaSlicer\nExec=/opt/orca/OrcaSlicer.AppImage %F\nMimeType=model/stl;model/3mf;\n",
            "lychee.desktop": "Name=Lychee\nExec=lychee --open=%f\nMimeType=model/stl;\n",
            "wine-extension-stl.desktop": "Name=Photon\nExec=env WINEPREFIX=/p wine start /unix %f\n"
                                          "MimeType=model/stl;\nNoDisplay=true\n",
            "wine-extension-pwmx.desktop": "Name=Photon\nExec=env WINEPREFIX=/p wine start /unix %f\n"
                                           "MimeType=application/x-wine-extension-pwmx;\nNoDisplay=true\n",
            "blender.desktop": "Name=Blender\nExec=blender %f\nMimeType=application/x-blender;\n",
            "hidden.desktop": "Name=Gone\nExec=gone %f\nHidden=true\nMimeType=model/stl;\n",
        }
        for n, body in entries.items():
            wr(os.path.join(d, n), "[Desktop Entry]\nType=Application\n" + body)
        return [d]

    def test_command_keeps_the_exact_path(self):
        import programs
        p = "/run/media/x/Storage/Models/Cruci/corp cruce celtică (repaired) x2.stl"
        app = programs.App("/a.desktop", "Orca", '"/opt/Orca Slicer/orca" --x %F', [], False)
        self.assertEqual(app.command(p), ["/opt/Orca Slicer/orca", "--x", p])
        self.assertEqual(programs.App("/b", "L", "lychee --open=%f", [], False).command(p), ["lychee", "--open=" + p])
        self.assertEqual(programs.App("/c", "N", "netfabb", [], False).command(p), ["netfabb", p])

    def test_unsafe_and_wine_handlers(self):
        import programs
        d = self.apps_dir()[0]
        wr(os.path.join(d, "opener.desktop"), "[Desktop Entry]\nType=Application\nName=Opener\n"
           "Exec=sh -c 'xdg-open \"%u\"'\nMimeType=model/stl;\n")
        wr(os.path.join(d, "wine-extension-f3d.desktop"), "[Desktop Entry]\nType=Application\nName=Fusion\n"
           "Exec=env WINEPREFIX=/f wine C:\\\\\\\\p.exe /ProgIDOpen \"f3d_file\" %f\nMimeType=application/fusion;\n")
        wr(os.path.join(d, "wine-extension-cam360.desktop"), "[Desktop Entry]\nType=Application\nName=Fusion\n"
           "Exec=env WINEPREFIX=/f wine C:\\\\\\\\p.exe /ProgIDOpen \"cam360_file\" %f\nMimeType=application/fusion;\n")
        apps = programs.installed([d])
        names = [a.name for a in apps]
        self.assertNotIn("Opener", names)                 # shell script reading the path: never offered
        self.assertEqual(names.count("Fusion"), 2)        # different ProgIDs = different handlers
        f3d = [a for a in apps if a.id == "wine-extension-f3d.desktop"][0]
        self.assertIn('f3d_file', f3d.command("/x/a.f3d"))
        self.assertIn("C:\\p.exe", f3d.command("/x/a.f3d"))  # 4 backslashes in the file (spec) = 1 real one
        self.assertEqual([a.id for a in programs.for_file("x.f3d", apps, {})], ["wine-extension-f3d.desktop"])
        programs.set_pick("stl", [])                      # nothing ticked = defaults again
        self.assertTrue(programs.for_file("x.stl", apps))
        self.assertNotIn("PeaZip", [a.name for a in programs.for_file("x.3mf", programs.installed())])

    def test_defaults_merge_wine_and_user_pick_wins(self):
        import programs
        apps = programs.installed(self.apps_dir())
        names = [a.name for a in apps]
        self.assertEqual(names.count("Photon"), 1)          # 2 wine launchers = 1 program
        self.assertNotIn("Gone", names)
        self.assertEqual([a.name for a in programs.for_file("x.stl", apps, {})], ["Lychee", "OrcaSlicer", "Photon"])
        self.assertEqual(programs.for_file("x.lys", apps, {}), [])
        programs.set_pick("stl", ["blender.desktop", "orca.desktop", "not-installed.desktop"])
        self.assertEqual([a.name for a in programs.for_file("x.stl", apps)], ["Blender", "OrcaSlicer"])
        self.assertEqual(programs.load()["stl"][0], "blender.desktop")  # survives a restart

    def test_menu_open_with_and_drag(self):
        import findfiles
        import programs
        from test_library import wait_indexed
        f = os.path.join(self.work, "cruce celtica (repaired).stl")
        write_binary_stl(f, CUBE_TRIS)
        z = os.path.join(self.work, "kit.zip")
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("baza cruce.stl", stl_bytes(CUBE_TRIS))
        dirs = self.apps_dir()
        saved = (programs.installed, programs.launch)
        launched = []
        programs.installed = lambda dirs_=None: saved[0](dirs)
        programs.launch = lambda app, path: launched.append((app.name, path))
        try:
            w = findfiles.Main()
            wait_indexed(w)
            w.q.setText("cruce celtica"); w.run_search()
            rect = w.table.visualItemRect(w.table.item(0, 0))
            w._context_menu(w.table, rect.center())
            menu = w._last_menu
            top = [a.text() for a in menu.actions() if a.text()]
            self.assertEqual(top, ["Open folder", "Open file", "Open with", "Copy path", "Related files",
                                   "Find exact copies"])
            sub = [a for a in menu.actions() if a.text() == "Open with"][0].menu()
            labels = [a.text() for a in sub.actions() if a.text()]
            self.assertEqual(labels[-1], "Choose programs for .stl files…")
            [a for a in sub.actions() if a.text().startswith("OrcaSlicer")][0].trigger()
            self.assertEqual(launched, [("OrcaSlicer", f)])
            mime = findfiles.handoff_mime(w.handoff_path())
            self.assertEqual([u.toLocalFile() for u in mime.urls()], [f])
            self.assertTrue(w.table.dragEnabled())
            w.q.setText("baza cruce"); w.run_search(); w.table.selectRow(0)
            self.assertIsNone(w.handoff_path())        # inside an archive: extracted in the background
            end = time.time() + 10
            while not w._handoff and time.time() < end:
                APP.processEvents(); time.sleep(0.02)
            tmp = w.handoff_path()                     # ready now (and reused, not extracted again)
            self.assertTrue(tmp and os.path.isfile(tmp) and not tmp.startswith(self.work))
            self.assertEqual(os.path.basename(tmp), "baza cruce.stl")  # real name, not member.stl
            self.assertIn("ready", w.status.text())
            w.close()
            self.assertFalse(os.path.exists(tmp))      # temp copies removed when the window closes
        finally:
            programs.installed, programs.launch = saved


class NewView(TempIndex):
    """ROADMAP 0.5 (2026-11-12): files that arrived in the last days, by when they FIRST appeared in the
    list (a model copied in from a USB stick keeps its old date but is still new)."""

    def seen(self, name):
        return indexer.connect_ro().execute("SELECT seen FROM files WHERE name=?", (name,)).fetchone()[0]

    def test_seen_kept_across_scans_updates_and_upgrade(self):
        import sqlite3
        old = os.path.join(self.work, "cruce veche.stl")
        wr(old, "x")
        os.utime(old, (1, 1_600_000_000))
        os.makedirs(indexer.DB_DIR, exist_ok=True)       # a list from the previous version: no 'seen' column
        db = sqlite3.connect(indexer.DB_PATH)
        db.execute("CREATE TABLE files(path TEXT, name TEXT, key TEXT, ext TEXT, size INTEGER, mtime REAL, "
                   "loc TEXT, kind TEXT)")
        db.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
        db.commit(); db.close()
        indexer.build()
        self.assertEqual(self.seen("cruce veche.stl"), 1_600_000_000)  # upgrade: not "new"
        copied = os.path.join(self.work, "cruce copiata.stl")
        wr(copied, "x")
        os.utime(copied, (1, 1_500_000_000))              # copied in, keeps an old date
        indexer.build()
        self.assertGreater(self.seen("cruce copiata.stl"), time.time() - 60)
        first = self.seen("cruce copiata.stl")
        wr(copied, "xy")                                   # saved over later
        indexer.build()
        self.assertEqual(self.seen("cruce copiata.stl"), first)  # still "arrived" at the same time
        wr(os.path.join(self.work, "cruce watch.stl"), "x")
        indexer.update_dirs([self.work])
        self.assertGreater(self.seen("cruce watch.stl"), time.time() - 60)
        self.assertEqual(self.seen("cruce copiata.stl"), first)
        rows = search.query("", since=time.time() - 7 * 86400)
        self.assertEqual(sorted(r[1] for r in rows), ["cruce copiata.stl", "cruce watch.stl"])
        self.assertEqual([r[1] for r in search.query("watch", since=time.time() - 7 * 86400)], ["cruce watch.stl"])

    def test_new_button_and_count(self):
        import findfiles
        from test_library import wait_indexed
        old = os.path.join(self.work, "cruce veche.stl")
        wr(old, "x"); os.utime(old, (1, 1_600_000_000))
        indexer.build()                                    # first list: old file is not new
        for n in ("medalie noua.stl", "cruce noua.stl"):
            wr(os.path.join(self.work, n), "x")
        w = findfiles.Main()
        wait_indexed(w)
        self.assertEqual(w.new_btn.text(), "New (2)")
        w.new_btn.setChecked(True)
        names = [w.table.item(i, 0).text() for i in range(w.table.rowCount())]
        self.assertEqual(sorted(names), ["cruce noua.stl", "medalie noua.stl"])
        self.assertEqual(w.new_btn.text(), "New")          # you've looked: count resets
        w.q.setText("cruce"); w.run_search()
        self.assertEqual(w.table.rowCount(), 1)
        self.assertIn("new in the last", w.status.text())
        w.new_btn.setChecked(False)
        self.assertEqual(w.table.rowCount(), 2)            # back to a normal search for "cruce"
        w.close()


class Cache(unittest.TestCase):
    def test_temporary_failures_are_not_remembered(self):
        self.assertFalse(thumbs.worth_saving(None, "File not found (moved, deleted, or its drive is unplugged)"))
        self.assertFalse(thumbs.worth_saving(None, "Preview failed: MemoryError: "))
        self.assertTrue(thumbs.worth_saving(None, "No thumbnail saved inside this file"))

    def test_unchanged_checks_the_disk(self):
        fd, f = tempfile.mkstemp()
        os.write(fd, b"abc"); os.close(fd)
        st = os.stat(f)
        try:
            self.assertTrue(thumbs.unchanged(f, 3, st.st_mtime))
            self.assertFalse(thumbs.unchanged(f, 4, st.st_mtime))
            os.utime(f, (1, time.time() + 50))
            self.assertFalse(thumbs.unchanged(f, 3, st.st_mtime))
        finally:
            os.remove(f)


if __name__ == "__main__":
    unittest.main()
