"""1.2: quick start (only changed folders are read), Full index, smaller pictures (WebP + one-off
shrink), disk use, the video player. Temp folders/config/cache only; never the real drives."""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safe_env  # noqa: E402,F401
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])
import findfiles  # noqa: E402
import indexer  # noqa: E402
import thumbs  # noqa: E402


def _touch(p, data=b"x"):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def _old(path, seconds=3600):
    """Make a folder look unchanged for a long time."""
    t = time.time() - seconds
    os.utime(path, (t, t))


def wait_scans(timeout=20):
    import threading
    for t in threading.enumerate():
        if t.name == "findfiles-index":
            t.join(timeout)


def names():
    db = indexer.connect_ro()
    try:
        return sorted(r[0] for r in db.execute("SELECT name FROM files"))
    finally:
        db.close()


class Temp(unittest.TestCase):
    def setUp(self):
        wait_scans()
        self.tmp = tempfile.mkdtemp()
        self.saved = (indexer.ROOTS, indexer.LOOSE, indexer.DB_DIR, indexer.DB_PATH, indexer.NEVER,
                      list(indexer.WATCH_DIRS), list(indexer.CHECK_DIRS))
        indexer.DB_DIR = os.path.join(self.tmp, "db")
        indexer.DB_PATH = os.path.join(indexer.DB_DIR, "index.db")
        self.root = os.path.join(self.tmp, "work")
        _touch(os.path.join(self.root, "a", "one.stl"))
        _touch(os.path.join(self.root, "b", "two.stl"))
        indexer.ROOTS, indexer.LOOSE, indexer.NEVER = [(self.root, "Work", "work")], [], []

    def tearDown(self):
        wait_scans()
        (indexer.ROOTS, indexer.LOOSE, indexer.DB_DIR, indexer.DB_PATH, indexer.NEVER, dirs, chk) = self.saved
        indexer.WATCH_DIRS[:] = dirs; indexer.CHECK_DIRS[:] = chk
        shutil.rmtree(self.tmp)

    def aged_build(self):
        """A full index that happened an hour ago, with untouched folders."""
        indexer.build()
        for d in indexer.CHECK_DIRS:
            _old(d)
        indexer.save_watch(time.time() - 1800)


class QuickStart(Temp):
    def test_needs_a_full_index_first(self):
        self.assertIsNone(indexer.quick())

    def test_nothing_changed_reads_no_folder(self):
        self.aged_build()
        with mock.patch.object(indexer, "_walk", side_effect=AssertionError("a folder was read")):
            n, missing, added, removed = indexer.quick()
        self.assertEqual((n, missing, added, removed), (2, [], 0, 0))
        self.assertIn(os.path.join(self.root, "a"), indexer.WATCH_DIRS)

    def test_new_deleted_and_new_folder_are_found(self):
        self.aged_build()
        _touch(os.path.join(self.root, "a", "three.stl"))          # new file in a known folder
        os.remove(os.path.join(self.root, "b", "two.stl"))         # deleted
        _touch(os.path.join(self.root, "c", "deep", "four.stl"))   # whole new folder tree
        n, _missing, added, removed = indexer.quick()
        self.assertEqual(names(), ["four.stl", "one.stl", "three.stl"])
        self.assertEqual((n, added, removed), (3, 2, 1))
        self.assertIn(os.path.join(self.root, "c", "deep"), indexer.WATCH_DIRS)  # watched from now on
        # and the next start has nothing left to do
        for d in indexer.WATCH_DIRS:
            _old(d, 7200)
        indexer.save_watch(time.time() - 60)
        with mock.patch.object(indexer, "_walk", side_effect=AssertionError("read again")):
            self.assertEqual(indexer.quick()[2:], (0, 0))

    def test_deleted_folder_drops_its_files(self):
        self.aged_build()
        shutil.rmtree(os.path.join(self.root, "b"))
        indexer.quick()
        self.assertEqual(names(), ["one.stl"])

    def test_changed_settings_need_a_full_index(self):
        self.aged_build()
        other = os.path.join(self.tmp, "other")
        _touch(os.path.join(other, "x.stl"))
        indexer.ROOTS = indexer.ROOTS + [(other, "Other", "work")]
        self.assertIsNone(indexer.quick())

    def test_unplugged_drive_needs_a_full_index(self):
        self.aged_build()
        with mock.patch.object(indexer, "_drive_missing", return_value=True):
            self.assertIsNone(indexer.quick())

    def test_damaged_folder_list_needs_a_full_index(self):
        self.aged_build()
        with open(indexer._watch_path(), "wb") as f:
            f.write(b"not gzip at all")
        self.assertIsNone(indexer.quick())

    def test_window_starts_quick_and_full_index_reads_everything(self):
        self.aged_build()
        with mock.patch.object(indexer, "build", side_effect=AssertionError("full index at start")):
            w = findfiles.Main()
            wait_scans(); APP.processEvents()
        self.assertIn("2 files ready", w.status.text())
        self.assertEqual(w.rescan_btn.text(), "Full index")
        self.assertIn("Last full index: today", w.rescan_btn.toolTip())
        _touch(os.path.join(self.root, "a", "late.stl"))
        w.rescan_btn.click()
        wait_scans(); APP.processEvents()
        self.assertIn("Full index done: 3 files", w.status.text())
        w.close()

    def test_window_without_a_list_does_the_full_index(self):
        w = findfiles.Main()
        wait_scans(); APP.processEvents()
        self.assertIn("Full index done: 2 files", w.status.text())
        self.assertTrue(os.path.exists(indexer._watch_path()))
        w.close()

    def test_old_full_index_is_mentioned(self):
        self.aged_build()
        indexer._FULL_TIME = time.time() - 9 * 86400
        indexer.save_watch()
        w = findfiles.Main()
        wait_scans(); APP.processEvents()
        self.assertIn("Last full index: 9 days ago", w.status.text())
        w.close()

    # ---- review findings 2026-11-26
    def test_folder_swapped_for_a_link_is_never_followed(self):
        """A known folder replaced by a link into a never-index place: not looked at, nothing listed."""
        secret = os.path.join(self.tmp, "never")
        _touch(os.path.join(secret, "sub", "secret.stl"))
        self.aged_build()
        shutil.rmtree(os.path.join(self.root, "b"))
        os.symlink(secret, os.path.join(self.root, "b"))
        indexer.NEVER = [secret]
        indexer.save_watch()  # (the never list is part of the settings signature)
        seen = []
        real = os.scandir
        with mock.patch.object(indexer.os, "scandir", side_effect=lambda p: (seen.append(str(p)), real(p))[1]):
            indexer.quick()
        self.assertNotIn("secret.stl", names())
        self.assertFalse([p for p in seen if p.startswith(secret) or p.endswith("/b")])

    def test_update_dirs_refuses_links_and_never_places(self):
        secret = os.path.join(self.tmp, "never")
        _touch(os.path.join(secret, "secret.stl"))
        indexer.build()
        os.symlink(secret, os.path.join(self.root, "lnk"))
        self.assertEqual(indexer.update_dirs([os.path.join(self.root, "lnk")])[3], False)
        self.assertNotIn("secret.stl", names())

    def test_folder_from_a_session_that_never_saved_is_picked_up(self):
        """The watcher listed a new folder, then the app died before saving the folder list."""
        self.aged_build()
        new = os.path.join(self.root, "N")
        _touch(os.path.join(new, "f1.stl"))
        indexer.update_dirs([self.root])                 # what watch mode did; nothing saved afterwards
        for d in (self.root, new):
            _old(d, 600)                                 # ... changed after the saved time, long ago now
        indexer.quick()                                  # next start
        self.assertIn(new, indexer.CHECK_DIRS)
        self.assertIn(new, indexer.WATCH_DIRS)
        _old(new, 7200); _old(self.root, 7200)
        indexer.save_watch(time.time() - 300)
        _touch(os.path.join(new, "f2.stl"))              # a later file in that folder
        indexer.quick()
        self.assertIn("f2.stl", names())

    def test_time_does_not_advance_when_a_drive_goes_during_the_check(self):
        self.aged_build()
        _touch(os.path.join(self.root, "a", "three.stl"))
        calls = []

        def flaky(root):  # present for the first checks, gone once folders are being re-read
            calls.append(root)
            return len(calls) > 1
        before = indexer._WATCH_TIME
        with mock.patch.object(indexer, "_drive_missing", side_effect=flaky):
            self.assertIsNone(indexer.quick())           # -> the caller does the full index
        import gzip, json
        with gzip.open(indexer._watch_path(), "rt") as f:
            self.assertEqual(json.load(f)["time"], before)

    def test_backup_locations_are_checked_at_start_too(self):
        copy = os.path.join(self.tmp, "backup")
        _touch(os.path.join(copy, "old", "kept.stl"))
        indexer.ROOTS = indexer.ROOTS + [(copy, "Backups (copy)", "copy")]
        self.aged_build()
        _old(os.path.join(copy, "old")); _old(copy)
        indexer.save_watch(time.time() - 1800)
        self.assertNotIn(os.path.join(copy, "old"), indexer.WATCH_DIRS)   # not watched live ...
        self.assertIn(os.path.join(copy, "old"), indexer.CHECK_DIRS)      # ... but checked at start
        _touch(os.path.join(copy, "old", "added.stl"))
        indexer.quick()
        self.assertIn("added.stl", names())

    def test_pending_pictures_restart_the_builder_once(self):
        self.aged_build()
        w = findfiles.Main()
        wait_scans(); APP.processEvents()
        with mock.patch.object(w, "start_builder") as sb:
            w._on_pictures(10, 5); w._on_pictures(11, 4)
            self.assertEqual(sb.call_count, 1)
            w._on_pictures(15, 0)
        w.close()

    def test_ago(self):
        self.assertEqual(findfiles.ago(0), "never")
        self.assertEqual(findfiles.ago(time.time() - 60), "today")
        self.assertEqual(findfiles.ago(time.time() - 86400 * 1.5), "yesterday")
        self.assertEqual(findfiles.ago(time.time() - 86400 * 3.2), "3 days ago")


class WholeDrive(Temp):
    """1.3: one entry for a whole drive, named folders inside it, skipped names, unticked = left out."""
    def setUp(self):
        super().setUp()
        self.keep = (indexer.EXCLUDE, indexer.SKIP_NAMES)
        d = self.drive = os.path.join(self.tmp, "drive")
        for rel in ("clips/c.mp4", "work/w.stl", "work/Assets/tex.png", "Games/big/game.exe",
                    "GOG/setup.exe", "Program Files/app/a.dll", "private/p.stl", "top.txt",
                    "work/deep/assets/model.obj", "backup/old.stl"):
            _touch(os.path.join(d, rel))
        s = {"roots": [findfiles.roots._r(d, "Drive › everything else"),
                       findfiles.roots._r(os.path.join(d, "work"), "Drive › work"),
                       findfiles.roots._r(os.path.join(d, "backup"), "Drive › backup (copy)", "copy"),
                       dict(findfiles.roots._r(os.path.join(d, "private"), "Drive › private"), enabled=False)],
             "never": [], "skip": findfiles.roots.DEFAULT_SKIP}
        indexer.apply_settings(findfiles.roots._clean(s))

    def tearDown(self):
        indexer.EXCLUDE, indexer.SKIP_NAMES = self.keep
        super().tearDown()

    def listed(self):
        db = indexer.connect_ro()
        try:
            return dict(db.execute("SELECT name, loc FROM files"))
        finally:
            db.close()

    def test_everything_but_games_assets_and_unticked(self):
        indexer.build()
        self.assertEqual(self.listed(), {"c.mp4": "Drive › everything else", "top.txt": "Drive › everything else",
                                         "w.stl": "Drive › work", "old.stl": "Drive › backup (copy)"})

    def test_quick_start_and_watch_keep_the_same_rules(self):
        indexer.build()
        for d in indexer.CHECK_DIRS:
            _old(d)
        indexer.save_watch(time.time() - 1800)
        d = self.drive
        for rel in ("new/n.stl", "new/assets/a.png", "games/g.exe", "private/p2.stl", "work/w2.stl", "clips/c2.mp4"):
            _touch(os.path.join(d, rel))
        self.assertIsNotNone(indexer.quick())
        got = self.listed()
        self.assertEqual({k: got.get(k) for k in ("n.stl", "w2.stl", "c2.mp4")},
                         {"n.stl": "Drive › everything else", "w2.stl": "Drive › work", "c2.mp4": "Drive › everything else"})
        for gone in ("a.png", "g.exe", "p2.stl", "game.exe", "tex.png"):
            self.assertNotIn(gone, got)
        _touch(os.path.join(d, "later", "Assets", "x.png")); _touch(os.path.join(d, "later", "l.stl"))
        indexer.update_dirs([d])                       # what watch mode does
        got = self.listed()
        self.assertIn("l.stl", got); self.assertNotIn("x.png", got)

    def test_changed_skip_list_needs_a_full_index(self):
        indexer.build()
        indexer.SKIP_NAMES = indexer.SKIP_NAMES | {"clips"}
        self.assertIsNone(indexer.quick())
        indexer.build()
        self.assertNotIn("c.mp4", self.listed())

    def test_settings_file_before_1_3_gets_the_default_skip_list(self):
        s = findfiles.roots._clean({"roots": [], "never": []})
        self.assertIn("games", s["skip"]); self.assertIn("assets", s["skip"])
        self.assertEqual(findfiles.roots._clean({"roots": [], "never": [], "skip": ["a/b", " x ", "x", 3]})["skip"], ["x"])

    def test_dialog_edits_the_skip_list(self):
        d = findfiles.SettingsDialog(None, {"roots": [], "never": [], "skip": ["games"]})
        with mock.patch.object(findfiles.QInputDialog, "getText", return_value=("games, Renders ,, bad/name", True)):
            d.edit_skip()
        d.save()
        self.assertEqual(d.result_settings["skip"], ["games", "Renders"])
        self.assertIn("Renders", d.skip_lbl.text())


class SmallPictures(unittest.TestCase):
    def setUp(self):
        self.db = thumbs.connect()
        with self.db:
            self.db.execute("DELETE FROM thumbs"); self.db.execute("DELETE FROM drawn")
        for n in os.listdir(thumbs.CACHE_DIR):
            os.remove(os.path.join(thumbs.CACHE_DIR, n))

    def tearDown(self):
        self.db.close()

    def img(self, colour="red"):
        im = QImage(256, 192, QImage.Format_ARGB32)
        im.fill(QColor(colour))
        return im

    def test_new_pictures_are_webp_and_readable(self):
        f = thumbs.store(self.db, "/p/a.stl", 1, 2.0, self.img(), "Size: 1 × 2 × 3 mm")
        self.assertTrue(f.endswith(".webp"))
        back = QImage(f)
        self.assertEqual((back.width(), back.height()), (256, 192))
        self.assertEqual(thumbs.lookup(self.db, "/p/a.stl", 1, 2.0)[0], f)

    def test_shrink_converts_old_png_and_keeps_the_row(self):
        k = thumbs.key("/p/b.stl", 5, 6.0)
        png = os.path.join(thumbs.CACHE_DIR, k + ".png")
        self.assertTrue(self.img("blue").save(png, "PNG"))
        with self.db:
            self.db.execute("INSERT INTO thumbs VALUES(?,?,?,?,?,?,?,?)", (k, "/p/b.stl", png, 1, 2, 3, "note", 0))
            self.db.execute("INSERT INTO drawn VALUES(?,?)", (k, thumbs.DRAW_VERSION))
        self.assertEqual(thumbs.old_format_left(self.db), 1)
        self.assertEqual(thumbs.shrink(self.db, log=lambda m: None, wait=lambda s: None), 1)
        f, dims, note = thumbs.lookup(self.db, "/p/b.stl", 5, 6.0)
        self.assertTrue(f.endswith(".webp") and os.path.exists(f))
        self.assertFalse(os.path.exists(png))
        self.assertEqual((dims, note), ((1, 2, 3), "note"))          # size and note untouched
        self.assertTrue(thumbs.current(self.db, "/p/b.stl", 5, 6.0))  # not drawn again
        self.assertEqual(QImage(f).pixelColor(10, 10).blue() > 200, True)
        self.assertEqual(thumbs.old_format_left(self.db), 0)
        self.assertEqual(thumbs.shrink(self.db, log=lambda m: None, wait=lambda s: None), 0)

    def test_shrink_never_touches_files_outside_the_cache(self):
        outside = tempfile.mkdtemp()
        png = os.path.join(outside, "mine.png")
        self.img().save(png, "PNG")
        with self.db:
            self.db.execute("INSERT INTO thumbs VALUES('kk','/p/c.stl',?,1,2,3,'',0)", (png,))
        thumbs.shrink(self.db, log=lambda m: None, wait=lambda s: None)
        self.assertTrue(os.path.exists(png))
        shutil.rmtree(outside)

    def test_shrink_forgets_a_picture_whose_file_is_gone(self):
        gone = os.path.join(thumbs.CACHE_DIR, "f" * 40 + ".png")
        with self.db:
            self.db.execute("INSERT INTO thumbs VALUES('gg','/p/d.stl',?,1,2,3,'',0)", (gone,))
        thumbs.shrink(self.db, log=lambda m: None, wait=lambda s: None)
        self.assertIsNone(self.db.execute("SELECT 1 FROM thumbs WHERE k='gg'").fetchone())

    def test_disk_use_counts_both_formats(self):
        thumbs.store(self.db, "/p/e.stl", 1, 2.0, self.img(), "")
        self.img().save(os.path.join(thumbs.CACHE_DIR, "x.png"), "PNG")
        size, n = thumbs.disk_use()
        self.assertEqual(n, 2)
        self.assertGreater(size, 0)

    def test_png_fallback_where_webp_is_missing(self):
        with mock.patch.object(thumbs, "_FMT", (".png", "PNG")):
            f = thumbs.store(self.db, "/p/f.stl", 1, 2.0, self.img(), "")
            self.assertTrue(f.endswith(".png"))
            self.assertEqual(thumbs.old_format_left(self.db), 0)  # nothing to shrink to


class Player(unittest.TestCase):
    def test_clock(self):
        import player
        self.assertEqual(player.clock(0), "0:00")
        self.assertEqual(player.clock(75_000), "1:15")
        self.assertEqual(player.clock(3_700_000), "1:01:40")
        self.assertEqual(player.clock(-5), "0:00")

    def test_controls_without_a_file(self):
        import player
        p = player.VideoPanel(40)
        self.assertEqual(p.vol.value(), 40)
        got = []
        p.volume_set.connect(got.append)
        p.on_key(player.Qt.Key_Up)
        self.assertEqual((p.vol.value(), got), (45, [45]))
        p.on_key(player.Qt.Key_M)
        self.assertTrue(p.audio.isMuted())
        p.vol.setValue(60)                 # turning the volume up un-mutes
        self.assertFalse(p.audio.isMuted())
        p.view.mousePressEvent(mock.Mock())   # a click on the picture does not pause (double-click = full screen)
        self.assertFalse(p.playing())
        p.jump(5000)                       # no file: nothing to jump in, no error
        p.stop()
        self.assertFalse(p.playing())
        self.assertTrue(p.player.source().isEmpty())
        p.deleteLater()


if __name__ == "__main__":
    unittest.main()


class SettingsRows(unittest.TestCase):
    def test_rows_fit_the_kind_box(self):
        from PySide6.QtWidgets import QApplication, QComboBox
        app = QApplication.instance() or QApplication([])
        import findfiles
        cur = findfiles.roots._clean({"roots": [findfiles.roots._r("/tmp", "Temp"),
                                                findfiles.roots._r("/home", "Home")]})
        d = findfiles.SettingsDialog(None, cur, "")
        try:
            self.assertGreater(d.table.rowCount(), 0)
            need = QComboBox().sizeHint().height()
            for i in range(d.table.rowCount()):
                self.assertGreaterEqual(d.table.rowHeight(i), need)
        finally:
            d.close()


class DriveChipFilter(unittest.TestCase):
    """A drive chip in the bottom bar shows only that drive's files; again = all drives."""

    def test_query_under_keeps_one_folder_only(self):
        import indexer, search
        rows = [("/mnt/x/a/star.stl", "X › a"), ("/mnt/xx/star.stl", "XX"), ("/mnt/s/star.stl", "Data › b")]
        db = __import__("sqlite3").connect(":memory:")
        db.execute("CREATE TABLE files(path,name,key,ext,loc,kind,size,mtime,seen)")
        db.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                       [(p, "star.stl", "star", "stl", loc, "work", 1, 1.0, 1.0) for p, loc in rows])
        with mock.patch.object(indexer, "connect_ro", lambda: _Keep(db)):
            self.assertEqual(len(search.query("star")), 3)
            got = [r[0] for r in search.query("star", under=("X › a",))]
            self.assertEqual(got, ["/mnt/x/a/star.stl"])          # not XX (another drive)
            self.assertEqual([r[0] for r in search.query_sized("star", under=("Data › b",))[0]],
                             ["/mnt/s/star.stl"])
            self.assertEqual(search.query("star", under=("",)), [])   # a chip whose drive left the list
            self.assertEqual(search.query(""), [])                    # no words, no chip: nothing
            self.assertEqual([r[0] for r in search.query("", under=("X › a",))], ["/mnt/x/a/star.stl"])

    def test_chip_click_toggles_and_right_click_opens_settings(self):
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import Qt
        app = QApplication.instance() or QApplication([])
        import findfiles
        w = findfiles.Main()
        try:
            seen = []
            w._run_search = lambda keep=False: seen.append(w._only)
            w.open_settings = lambda: seen.append("settings")
            w._on_drives([("X", True, 10, "/mnt/x\nX › a"), ("Data", True, 5, "/mnt/s\nData › b")])
            w._chip_clicked(None, "X", "/mnt/x")
            self.assertEqual(w._only, ("X", "/mnt/x"))
            self.assertEqual(w._only_locs(), ("X › a",))
            names = [w.chips.itemAt(i).widget().objectName() for i in range(w.chips.count())]
            self.assertEqual(sorted(names), ["chip", "chipon"])
            self.assertIn("Only", w.status.text())
            w._chip_clicked(None, "Data", "/mnt/s")           # another chip takes over
            self.assertEqual(w._only, ("Data", "/mnt/s"))
            w._chip_clicked(None, "Data", "/mnt/s")           # same chip again = all drives
            self.assertIsNone(w._only)

            class Right:
                def button(self):
                    return Qt.RightButton
            w._chip_clicked(Right(), "X", "/mnt/x")
            self.assertEqual(seen[-1], "settings")
            self.assertIsNone(w._only)
        finally:
            w.close()


class _Keep:
    """A database handle whose close() does nothing, so one in-memory list serves several queries."""
    def __init__(self, db):
        self._db = db

    def execute(self, *a):
        return self._db.execute(*a)

    def close(self):
        pass


class Columns(unittest.TestCase):
    """Result columns: drag widths, hide/show, extra columns, kept between runs."""

    def setUp(self):
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        import findfiles
        self.ff = findfiles
        self.rows = [(f"/x/dir{i % 3}/model {i}.stl", f"model {i}.stl", ["X › a", "Data › b"][i % 2],
                      100 * (i + 1), 1.7e9 + i) for i in range(20)]

    def _win(self):
        w = self.ff.Main(); w.resize(1300, 770); w.show(); self.app.processEvents()
        import threading
        for t in threading.enumerate():       # the start-up scan would refill (empty) the list under the test
            if t.name == "findfiles-index":
                t.join(10)
        self.app.processEvents()
        w._fill(self.rows); self.app.processEvents()
        return w

    def test_all_columns_can_be_dragged_and_extras_start_hidden(self):
        w = self._win()
        try:
            h = w.table.horizontalHeader()
            for c in range(len(self.ff.COLUMNS)):
                self.assertEqual(h.sectionResizeMode(c), self.ff.QHeaderView.Interactive)
            self.assertEqual([c for c in range(9) if w.table.isColumnHidden(c)], [6, 7, 8])
            self.assertEqual(w.table.item(4, 6).text(), "dir1")
            self.assertEqual(w.table.item(4, 7).text(), "stl")
            self.assertEqual(w.table.item(4, 8).text(), "X")
            self.assertFalse(w._cols_custom)
            w._toggle_column(7, True); w._toggle_column(7, False)
            self.assertFalse(w._cols_custom)                    # ticking a column is not a drag
            shown = sum(w.table.columnWidth(c) for c in range(6))
            self.assertEqual(shown, w.table.room())                            # Name fills what is left
            self.assertLessEqual(shown, w.table.viewport().width())            # and nothing scrolls sideways
            w.resize(1000, 600); self.app.processEvents()                      # a narrower window
            shown = sum(w.table.columnWidth(c) for c in range(6))
            self.assertLessEqual(shown - w.table.viewport().width(), 60)       # no wide sideways scroll
        finally:
            w._cols_timer.stop(); w.close()

    def test_resizing_through_every_width_settles(self):
        """Column fitting must never chase the scrollbars (a narrow window once hung the test run)."""
        w = self._win()
        try:
            fits = []
            w.table.resized.connect(lambda: fits.append(1))
            for n in (3, 18, 21, 24, 200):                     # around the point where the bar appears
                w._fill(self.rows * 10 if n == 200 else self.rows[:n]); self.app.processEvents()
                for width in range(1300, 500, -37):
                    w.resize(width, 640); self.app.processEvents()
            self.assertLess(len(fits), 5 * 22 * 3)              # one fit per width step, not a storm
        finally:
            w._cols_timer.stop(); w.close()

    def test_dragged_width_hidden_column_and_order_survive_a_restart(self):
        w = self._win()
        w.table.horizontalHeader().resizeSection(2, 333)       # as a drag does
        self.assertTrue(w._cols_custom)
        w._toggle_column(4, False); w._toggle_column(7, True)
        w.table.horizontalHeader().moveSection(3, 1)
        w._fill(self.rows); self.app.processEvents()
        self.assertEqual(w.table.columnWidth(2), 333)          # a refill leaves the user's widths alone
        w.close()
        w2 = self._win()
        try:
            self.assertEqual(w2.table.columnWidth(2), 333)
            self.assertTrue(w2.table.isColumnHidden(4)); self.assertFalse(w2.table.isColumnHidden(7))
            self.assertEqual(w2.table.horizontalHeader().visualIndex(3), 1)
            w2._reset_columns()
            self.assertFalse(w2._cols_custom)
            self.assertEqual([c for c in range(9) if w2.table.isColumnHidden(c)], [6, 7, 8])
            self.assertEqual(w2.table.horizontalHeader().visualIndex(3), 3)
        finally:
            w2._save_columns(); w2._cols_timer.stop(); w2.close()

    def test_sort_by_a_hidden_column_and_damaged_saved_entry(self):
        from PySide6.QtCore import Qt
        w = self._win()
        try:
            w._sort_exact(3, Qt.DescendingOrder)
            self.assertEqual(w.table.item(0, 0).text(), "model 19.stl")
            w._sort_exact(8, Qt.AscendingOrder)                 # Drive, while hidden
            self.assertEqual(w.table.item(0, 8).text(), "Data")
            self.ff.programs.set_state("columns", {"n": 9, "custom": True, "widths": ["x"], "hidden": "?", "order": 5})
            w._restore_columns()                                # must not raise
        finally:
            self.ff.programs.set_state("columns", None)
            w._cols_timer.stop(); w.close()


class Look(unittest.TestCase):
    """Buttons and chips keep a visible outline in a dark desktop theme."""

    def test_outline_colour_stands_out_from_a_dark_window(self):
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QPalette, QColor
        QApplication.instance() or QApplication([])
        import findfiles
        pal = QPalette(); R = QPalette.ColorRole
        pal.setColor(R.Window, QColor(40, 46, 51)); pal.setColor(R.WindowText, QColor(222, 222, 222))
        pal.setColor(R.Button, QColor(54, 61, 67))
        css = findfiles.style(pal)
        self.assertNotIn("@", css)                                   # every colour filled in
        self.assertNotIn("palette(mid)", css)                        # nearly the window colour when dark
        line = QColor(css.split("QPushButton { border: 1px solid ")[1][:7])
        self.assertGreater(line.lightness() - QColor(40, 46, 51).lightness(), 40)
        self.assertIn("background: #363d43", css)                    # buttons are filled, not see-through


class TestsStayOutOfRealData(unittest.TestCase):
    def test_file_list_and_settings_and_pictures_are_in_temp_folders(self):
        import indexer, roots, thumbs
        real = os.path.expanduser("~")
        for what, path in (("file list", indexer.DB_PATH), ("folder memory", indexer._watch_path()),
                           ("settings", roots.PATH), ("pictures", thumbs.DB_PATH)):
            self.assertFalse(os.path.abspath(path).startswith(os.path.join(real, ".")), f"{what}: {path}")
