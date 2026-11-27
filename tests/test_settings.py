"""1.0: folders in a settings file, Settings window, Rebuild from scratch, this PC's own example words.
Temp folders / temp settings / temp cache only (tests/safe_env.py); never the real drives or index."""
import os
import sys
import tempfile
import shutil
import sqlite3
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safe_env  # noqa: E402,F401
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

APP = QApplication.instance() or QApplication([])
import findfiles  # noqa: E402
import indexer  # noqa: E402
import roots  # noqa: E402
import search  # noqa: E402
import thumbs  # noqa: E402
from version import VERSION  # noqa: E402


def _touch(p, data=b"x"):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def wait_scans(timeout=20):
    """Let every window's background scan finish (it would otherwise run into the next test's temp folders)."""
    import threading
    for t in threading.enumerate():
        if t.name == "findfiles-index":
            t.join(timeout)


class TempIndex(unittest.TestCase):
    """A temp work folder + temp index (same pattern as test_findfiles.IndexAndSearch)."""

    def setUp(self):
        wait_scans()
        self.tmp = tempfile.mkdtemp()
        self.saved = (indexer.ROOTS, indexer.LOOSE, indexer.DB_DIR, indexer.DB_PATH, indexer.NEVER)
        self.work = os.path.join(self.tmp, "work")
        indexer.ROOTS, indexer.LOOSE = [(self.work, "Work", "work")], []
        indexer.DB_DIR = os.path.join(self.tmp, "db")
        indexer.DB_PATH = os.path.join(indexer.DB_DIR, "index.db")

    def tearDown(self):
        wait_scans()
        indexer.ROOTS, indexer.LOOSE, indexer.DB_DIR, indexer.DB_PATH, indexer.NEVER = self.saved
        shutil.rmtree(self.tmp)


# ---------------------------------------------------------------- roots.py
class SettingsFile(unittest.TestCase):
    def setUp(self):
        self.saved_path = roots.PATH
        self.dir = tempfile.mkdtemp()
        roots.PATH = os.path.join(self.dir, "roots.json")
        roots._BASE = self.dir

    def tearDown(self):
        roots.PATH = self.saved_path
        shutil.rmtree(self.dir)

    def test_no_file_gives_defaults(self):
        s, problem = roots.load()
        self.assertEqual(problem, "")
        self.assertTrue(s["roots"])

    def test_damaged_file_not_overwritten(self):
        with open(roots.PATH, "w") as f:
            f.write("{not json")
        s, problem = roots.load()
        self.assertIn("could not be read", problem)
        self.assertTrue(s["roots"])
        with open(roots.PATH) as f:
            self.assertEqual(f.read(), "{not json")  # left for the user, replaced only by Save

    def test_wrong_shape_counts_as_damaged(self):
        with open(roots.PATH, "w") as f:
            f.write('{"roots": "nope"}')
        self.assertTrue(roots.load()[1])

    def test_save_load_roundtrip_and_cleaning(self):
        roots.save({"roots": [
            {"path": "/a", "label": "A", "kind": "copy", "recurse": False, "enabled": False},
            {"path": "/b", "label": "A"},                       # duplicate label -> dropped
            {"path": "/c", "label": "C", "kind": "weird"},      # bad kind -> work
            {"label": "no path"}, "junk"], "never": ["/n", 3]})
        s, problem = roots.load()
        self.assertEqual(problem, "")
        self.assertEqual([r["label"] for r in s["roots"]], ["A", "C"])
        self.assertEqual(s["roots"][0], {"path": "/a", "label": "A", "kind": "copy", "recurse": False, "enabled": False})
        self.assertEqual(s["roots"][1]["kind"], "work")
        self.assertEqual(s["never"], ["/n"])
        self.assertFalse(os.path.exists(roots.PATH + ".tmp"))

    def test_active_split(self):
        s = {"roots": [roots._r("/a", "A"), roots._r("/b", "B", recurse=False),
                       {**roots._r("/c", "C"), "enabled": False}], "never": []}
        self.assertEqual(roots.active(s), ([("/a", "A", "work")], [("/b", "B", "work")]))


class Checks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_refused(self):
        never = ["/mnt/WINDRIVE"]
        for p in ("", "relative/path", "/", "/mnt", "/home", roots.HOME, "/run/media", "/run/media/u",
                  "/usr/share", "/proc", "/etc", "/mnt/WINDRIVE", "/mnt/WINDRIVE/Users",
                  "/mnt/x/SteamLibrary/steamapps", "/mnt/x/steam/steamapps/common",
                  os.path.join(self.tmp, "does-not-exist")):
            ok, why = roots.check(p, never)
            self.assertFalse(ok, p)
            self.assertTrue(why, p)

    def test_accepted_and_duplicate(self):
        self.assertEqual(roots.check(self.tmp), (True, ""))
        ok, why = roots.check(self.tmp + "/", roots=[{"path": self.tmp}])
        self.assertFalse(ok)
        self.assertIn("already", why)

    def test_never_prefix_is_not_a_substring_match(self):
        other = os.path.join(self.tmp, "ab")
        os.makedirs(other)
        self.assertTrue(roots.check(other, never=[os.path.join(self.tmp, "a")])[0])

    def test_label_unique(self):
        taken = [{"label": "work stuff (X)"}]
        self.assertEqual(roots.label_for("/mnt/X/work stuff", taken), "work stuff (X) 2")
        self.assertEqual(roots.label_for("/mnt/X/movies"), "movies (X)")


class Defaults(unittest.TestCase):
    def test_a_new_pc_starts_with_its_home_folders_only(self):
        with mock.patch.object(roots, "HOME", "/home/someone"), \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/nonexistent"}):
            d = roots.defaults()
        self.assertEqual([r["label"] for r in d["roots"]], ["Desktop", "Documents", "Downloads", "Pictures"])
        self.assertTrue(all(r["path"].startswith("/home/someone/") for r in d["roots"]))
        self.assertEqual(d["never"], [])

    def test_home_folders_in_another_language(self):
        with tempfile.TemporaryDirectory() as t:
            home = os.path.join(t, "home"); os.makedirs(os.path.join(home, "Documente"))
            os.makedirs(os.path.join(home, "Birou"))
            with open(os.path.join(t, "user-dirs.dirs"), "w", encoding="utf-8") as f:
                f.write('# comment\nXDG_DESKTOP_DIR="$HOME/Birou"\nXDG_DOCUMENTS_DIR="$HOME/Documente"\n'
                        'XDG_TEMPLATES_DIR="$HOME/"\nXDG_MUSIC_DIR="$HOME/Muzica"\n')
            with mock.patch.object(roots, "HOME", home), mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": t}):
                d = roots.defaults()
        self.assertEqual([r["label"] for r in d["roots"]], ["Birou", "Documente"])   # only the ones that exist

    def test_no_machine_names_or_drive_ids_in_the_code(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import re
        bad = re.compile(r"/mnt/[0-9A-F]{12,}|gethostname")
        for name in sorted(os.listdir(here)):
            if name.endswith((".py", ".sh", ".in")):
                with open(os.path.join(here, name), encoding="utf-8") as f:
                    self.assertIsNone(bad.search(f.read()), name)

    def test_low_priority_words_are_a_setting(self):
        c = roots._clean({"roots": [], "low": ["Old stuff", "", 5, "Old stuff"]})
        self.assertEqual(c["low"], ["Old stuff"])
        self.assertEqual(roots._clean({"roots": []})["low"], roots.DEFAULT_LOW)   # an older settings file
        import indexer
        import archives
        before = indexer.WATCH_SKIP
        try:
            indexer.apply_settings({"roots": [], "never": [], "skip": [], "low": ["Old stuff"]})
            self.assertFalse(indexer._watched("Disk › old stuff", "work"))
            self.assertTrue(indexer._watched("Disk › Music", "work"))                  # no longer low
            self.assertEqual(archives.SKIP_LOCS, ("Old stuff",))
        finally:
            indexer.apply_settings({"roots": [], "never": [], "skip": [], "low": list(before)})


# ---------------------------------------------------------------- indexer
class ApplySettings(unittest.TestCase):
    def setUp(self):
        self.saved = (indexer.ROOTS, indexer.LOOSE, indexer.NEVER)

    def tearDown(self):
        indexer.ROOTS, indexer.LOOSE, indexer.NEVER = self.saved

    def test_never_wins_over_a_hand_edited_list(self):
        indexer.apply_settings({"roots": [roots._r("/mnt/W/Users", "W"), roots._r("/mnt/WX", "WX"),
                                          roots._r("/mnt/W", "W root", recurse=False)],
                                "never": ["/mnt/W"]})
        self.assertEqual(indexer.ROOTS, [("/mnt/WX", "WX", "work")])  # /mnt/WX is not under /mnt/W
        self.assertEqual(indexer.LOOSE, [])


class ForceRebuild(TempIndex):
    def test_force_writes_and_forgets_archive_cache(self):
        _touch(os.path.join(self.work, "a.stl"))
        indexer.build()
        before = os.stat(indexer.DB_PATH).st_ino
        indexer.build()
        self.assertEqual(os.stat(indexer.DB_PATH).st_ino, before)  # unchanged -> no write
        _touch(os.path.join(indexer.DB_DIR, "archives.db"), b"stale")
        n, _ = indexer.build(force=True)
        self.assertEqual(n, 1)
        self.assertNotEqual(os.stat(indexer.DB_PATH).st_ino, before)  # written again
        with open(os.path.join(indexer.DB_DIR, "archives.db"), "rb") as f:
            self.assertNotEqual(f.read(), b"stale")  # recreated, not the stale cache
        self.assertTrue(os.path.exists(os.path.join(self.work, "a.stl")))  # the user's file untouched

    def test_force_keeps_unplugged_drive_files(self):
        _touch(os.path.join(self.work, "a.stl"))
        indexer.ROOTS = [(self.work, "Work", "work"), ("/run/media/__nope__/D/X", "Gone", "work")]
        indexer.build()
        db = sqlite3.connect(indexer.DB_PATH)
        db.execute("INSERT INTO files VALUES('/run/media/__nope__/D/X/old.stl','old.stl','old','stl',1,1,'Gone','work',1)")
        db.commit(); db.close()
        indexer.build(force=True)
        self.assertIn("old.stl", [r[1] for r in search.query("old")])


# ---------------------------------------------------------------- thumbs.clear_all
class ClearPictures(unittest.TestCase):
    def test_busy_builder_keeps_pictures(self):
        import fcntl
        thumbs.connect().close()
        keep = os.path.join(thumbs.CACHE_DIR, "keep.png")
        _touch(keep)
        with open(thumbs.LOCK_PATH, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            ok, msg = thumbs.clear_all()
        self.assertFalse(ok)
        self.assertIn("busy", msg)
        self.assertTrue(os.path.exists(keep))
        os.remove(keep)

    def test_clears_rows_and_only_png(self):
        db = thumbs.connect()
        db.execute("INSERT OR REPLACE INTO thumbs VALUES('k','/p','f.png',1,1,1,'',0)")
        db.execute("INSERT OR REPLACE INTO drawn VALUES('k',1)")
        db.commit()
        _touch(os.path.join(thumbs.CACHE_DIR, "f.png"))
        _touch(os.path.join(thumbs.CACHE_DIR, "notes.txt"))
        ok, msg = thumbs.clear_all()
        self.assertTrue(ok, msg)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM thumbs").fetchone()[0], 0)  # open reader sees it
        self.assertEqual(db.execute("SELECT COUNT(*) FROM drawn").fetchone()[0], 0)
        db.close()
        self.assertFalse(os.path.exists(os.path.join(thumbs.CACHE_DIR, "f.png")))
        self.assertTrue(os.path.exists(os.path.join(thumbs.CACHE_DIR, "notes.txt")))
        os.remove(os.path.join(thumbs.CACHE_DIR, "notes.txt"))


# ---------------------------------------------------------------- example words
class Examples(TempIndex):
    def test_from_this_pcs_files(self):
        for i, n in enumerate(("piesa cantar 2.stl", "El Maddah Book (repaired).stl", "a1b2.stl",
                               "notes about church.txt", "Copy of model.obj")):
            p = os.path.join(self.work, n)
            _touch(p)
            os.utime(p, (1000 + i, 1000 + i))
        indexer.build()
        ex = search.examples()
        self.assertEqual(ex, ["maddah book", "piesa cantar"])  # newest first, junk/hex/non-3D skipped
        for e in ex:
            self.assertTrue(search.query(e))

    def test_empty_index(self):
        indexer.build()
        self.assertEqual(search.examples(), [])

    def test_no_index_at_all(self):
        self.assertEqual(search.examples(), [])


# ---------------------------------------------------------------- window
class Window(TempIndex):
    def test_title_version_and_no_tria_words(self):
        indexer.build()
        w = findfiles.Main()
        self.assertIn(VERSION, w.windowTitle())
        w._examples = []
        w._mode, w._nfiles = "search", 0
        w.q.setText("")
        w._show_results()
        self.assertNotIn("cruce", w.hint.text())
        self.assertNotIn("cruce", w.q.placeholderText())
        w._examples = ["piesa cantar"]
        w._show_results()
        self.assertIn("piesa cantar", w.hint.text())
        w.close()

    def test_settings_dialog_roundtrip_and_refusal(self):
        s = {"roots": [roots._r(self.tmp, "Temp"), {**roots._r("/nope/gone", "Gone", "copy"), "enabled": False}],
             "never": ["/mnt/W"]}
        d = findfiles.SettingsDialog(None, s)
        self.assertEqual(d.rows(), s["roots"])
        import time
        end = time.time() + 5
        while d.table.item(1, 5).text() == "checking…" and time.time() < end:
            APP.processEvents(); time.sleep(0.02)
        self.assertEqual(d.table.item(1, 5).text(), "not there / unplugged")
        self.assertEqual(d.table.item(0, 5).text(), "OK")
        with mock.patch.object(findfiles.QFileDialog, "getExistingDirectory", return_value="/usr/share"), \
                mock.patch.object(findfiles.QMessageBox, "warning") as warn:
            d.add_folder()
        warn.assert_called_once()
        self.assertEqual(len(d.rows()), 2)
        inside = os.path.join(self.tmp, "inside")  # inside "Temp", which is searched with subfolders
        os.makedirs(inside)
        with mock.patch.object(findfiles.QFileDialog, "getExistingDirectory", return_value=inside), \
                mock.patch.object(findfiles.QMessageBox, "warning") as warn:
            d.add_folder()
        warn.assert_not_called()  # 1.3: a folder inside another is fine, with a note
        self.assertIn("inside", d.note.text())
        d.table.removeRow(d.table.rowCount() - 1)
        new = tempfile.mkdtemp()  # a separate folder: accepted
        self.addCleanup(shutil.rmtree, new, True)
        with mock.patch.object(findfiles.QFileDialog, "getExistingDirectory", return_value=new):
            d.add_folder()
        self.assertEqual(d.rows()[-1]["path"], new)
        d.table.selectRow(0)
        d.remove_selected()
        self.assertEqual([r["label"] for r in d.rows()], ["Gone", d.rows()[-1]["label"]])
        d.table.item(0, 2).setText(d.rows()[-1]["label"])  # same name twice -> refused
        with mock.patch.object(findfiles.QMessageBox, "warning") as warn:
            d.save()
        warn.assert_called_once()
        self.assertIsNone(d.result_settings)

    def test_save_applies_and_rescans(self):
        _touch(os.path.join(self.work, "a.stl"))
        indexer.build()
        w = findfiles.Main()
        saved_path, saved_base = roots.PATH, roots._BASE
        cfg = tempfile.mkdtemp()
        roots.PATH, roots._BASE = os.path.join(cfg, "roots.json"), cfg
        try:
            other = os.path.join(self.tmp, "other")
            _touch(os.path.join(other, "b.stl"))
            new = {"roots": [roots._r(other, "Other")], "never": []}

            class FakeDlg:
                def __init__(self, *a):
                    self.result_settings, self.action, self.redraw = new, "rebuild", False
                    self.history_cleared = False

                def exec(self):
                    return QDialog.Accepted
            with mock.patch.object(findfiles, "SettingsDialog", FakeDlg), \
                    mock.patch.object(w, "refresh_index") as rescan:
                w.open_settings()
            rescan.assert_called_once_with(force=True, clear_pics=False)
            self.assertEqual(indexer.ROOTS, [(other, "Other", "work")])
            self.assertEqual(roots.load()[0]["roots"][0]["path"], other)
        finally:
            roots.PATH, roots._BASE = saved_path, saved_base
            shutil.rmtree(cfg)
            w.close()

    def test_queued_rescan_while_busy(self):
        indexer.build()
        w = findfiles.Main()
        w._indexing = True
        w.refresh_index()
        self.assertIs(w._again, False)
        w.refresh_index(force=True)
        self.assertIs(w._again, True)
        w.refresh_index()
        self.assertIs(w._again, True)  # a queued rebuild is never downgraded
        w.close()


if __name__ == "__main__":
    unittest.main()
