"""1.7.1: a scan saves only into the list it started on; any folder depth; awkward setups; the live watch
leaves room for other programs. Temp folders only."""
import os
import sys
import time
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safe_env  # noqa: E402,F401
sys.path.insert(0, HERE)
from PySide6.QtCore import QFileSystemWatcher  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])
import findfiles  # noqa: E402
import indexer  # noqa: E402
import search  # noqa: E402
from test_round2 import TempIndex  # noqa: E402


def touch(p):
    open(p, "w").close()


def chain(top, name, depth):
    """`depth` nested folders (os.makedirs itself fails past ~1,000 levels)."""
    d = top
    for _ in range(depth):
        d = os.path.join(d, name)
        os.mkdir(d)
    return d


def unchain(top, name, depth):
    """Remove a chain made by chain(), deepest first. shutil.rmtree keeps a file open per level and
    fails past the open-files limit (1,024 on some setups)."""
    d = os.path.join(top, *([name] * depth))
    for f in os.listdir(d):
        os.remove(os.path.join(d, f))
    for _ in range(depth):
        os.rmdir(d)
        d = os.path.dirname(d)


class ScanTarget(TempIndex):
    def test_a_scan_never_writes_into_a_list_that_replaced_its_own(self):
        touch(os.path.join(self.work, "cruce.stl"))
        other = os.path.join(self.tmp, "other", "index.db")

        def moved(_label):  # the next test's list takes over while this scan runs
            indexer.DB_DIR, indexer.DB_PATH = os.path.dirname(other), other
        indexer.build(progress=moved)
        self.assertFalse(os.path.exists(os.path.dirname(other)))  # nothing at all made in the new place

    def test_moved_after_the_folders_were_read_changes_nothing_shared(self):
        touch(os.path.join(self.work, "cruce.stl"))
        other = os.path.join(self.tmp, "other", "index.db")
        before = list(indexer.WATCH_DIRS)

        def tick(_n, where):
            if where == "":  # folders done, archives / saving next
                indexer.DB_DIR, indexer.DB_PATH = os.path.dirname(other), other
        indexer.build(tick=tick)
        self.assertFalse(os.path.exists(os.path.dirname(other)))
        self.assertEqual(indexer.WATCH_DIRS, before)

    def test_a_normal_scan_still_saves(self):
        touch(os.path.join(self.work, "cruce.stl"))
        self.assertEqual(indexer.build()[0], 1)
        self.assertTrue(os.path.exists(indexer.DB_PATH))


class Setups(TempIndex):
    def names(self, q):
        return [r[1] for r in search.query(q, None, False)]

    def test_very_deep_folders_are_listed_and_found_at_quick_start(self):
        touch(os.path.join(chain(self.work, "a", 1100), "deep file.txt"))
        touch(os.path.join(self.work, "top.txt"))
        self.assertEqual(indexer.build()[0], 2)
        self.assertEqual(self.names("deep"), ["deep file.txt"])
        time.sleep(1.1)
        touch(os.path.join(chain(self.work, "b", 1100), "later.txt"))
        res = indexer.quick()
        self.assertIsNotNone(res)
        self.assertEqual(res[2], 1)
        self.assertEqual(self.names("later"), ["later.txt"])

    def tearDown(self):
        for name in ("a", "b"):
            if os.path.isdir(os.path.join(self.work, name)):
                unchain(self.work, name, 1100)
        super().tearDown()

    def test_locked_folder_link_loop_odd_names_missing_location(self):
        locked = os.path.join(self.work, "locked")
        os.mkdir(locked); touch(os.path.join(locked, "secret.txt")); os.chmod(locked, 0)
        os.symlink(self.work, os.path.join(self.work, "loop"))
        touch(os.path.join(self.work, "țară ștefan 🎉.png"))
        with open(os.fsencode(self.work) + b"/bad\xff name.txt", "w"):
            pass
        indexer.ROOTS = indexer.ROOTS + [(os.path.join(self.tmp, "not there"), "Gone", "work")]
        try:
            n, missing = indexer.build()
        finally:
            os.chmod(locked, 0o755)  # tearDown must be able to delete it
        self.assertEqual(n, 1)                       # only the good name; nothing through the loop
        self.assertEqual(missing, [])
        self.assertEqual(self.names("tara stefan"), ["țară ștefan 🎉.png"])
        self.assertEqual(len(indexer.SKIPPED_NAMES), 1)


class WatchRoom(unittest.TestCase):
    def fake(self, dirs):
        w = types.SimpleNamespace(watcher=QFileSystemWatcher(), _media_dirs=set())
        with mock.patch.object(indexer, "WATCH_DIRS", dirs):
            n = findfiles.Main._sync_watch(w)
        return w, n

    def test_never_more_than_the_room_and_top_folders_first(self):
        import tempfile
        top = tempfile.mkdtemp(); self.addCleanup(__import__("shutil").rmtree, top)
        deep = chain(top, "x", 3)
        dirs = [deep, os.path.dirname(deep), top]
        with mock.patch.object(findfiles, "watch_room", return_value=2):
            w, n = self.fake(dirs)
        self.assertEqual(n, 2)
        self.assertEqual(sorted(w.watcher.directories()), sorted([top, os.path.dirname(deep)]))

    def test_new_folders_while_open_respect_the_room_too(self):
        import shutil
        import tempfile
        top = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, top)
        a, b, c = (os.path.join(top, n) for n in "abc")
        for d in (a, b, c):
            os.mkdir(d)
        w = types.SimpleNamespace(watcher=QFileSystemWatcher(), _retry=None, _watch_save_timer=mock.Mock())
        w.watcher.addPath(a)
        with mock.patch.object(findfiles, "watch_room", return_value=2), \
                mock.patch.object(indexer, "remember") as rem:
            findfiles.Main.on_watched(w, 0, 0, [c, b], False)
        self.assertEqual(sorted(w.watcher.directories()), [a, b])
        rem.assert_called_once_with([c, b])               # all still checked at the next start

    def test_room_is_half_the_system_limit(self):
        with open("/proc/sys/fs/inotify/max_user_watches") as f:
            self.assertEqual(findfiles.watch_room(), int(f.read()) // 2)


if __name__ == "__main__":
    unittest.main()
