"""1.1: reviewer fixes (never-drive guard, nested folders, renames, picture clearing), videos,
remembered view + recent searches, drive chips, picture progress. Temp folders/config/cache only."""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safe_env  # noqa: E402,F401
from PySide6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])
import filetypes  # noqa: E402
import findfiles  # noqa: E402
import indexer  # noqa: E402
import previews  # noqa: E402
import programs  # noqa: E402
import roots  # noqa: E402
import search  # noqa: E402
import thumbs  # noqa: E402


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


class Temp(unittest.TestCase):
    def setUp(self):
        wait_scans()
        self.tmp = tempfile.mkdtemp()
        self.saved = (indexer.ROOTS, indexer.LOOSE, indexer.DB_DIR, indexer.DB_PATH, indexer.NEVER,
                      dict(indexer.LABEL_RENAMES))
        indexer.DB_DIR = os.path.join(self.tmp, "db")
        indexer.DB_PATH = os.path.join(indexer.DB_DIR, "index.db")
        indexer.ROOTS, indexer.LOOSE, indexer.NEVER = [], [], []  # never the real drives

    def tearDown(self):
        wait_scans()
        (indexer.ROOTS, indexer.LOOSE, indexer.DB_DIR, indexer.DB_PATH, indexer.NEVER, renames) = self.saved
        indexer.LABEL_RENAMES.clear(); indexer.LABEL_RENAMES.update(renames)
        shutil.rmtree(self.tmp)


# ---------------------------------------------------------------- never-index guard (review MAJOR 1)
class NeverGuard(Temp):
    def test_symlink_into_never_refused_and_skipped(self):
        never = os.path.join(self.tmp, "windows")
        _touch(os.path.join(never, "secret.stl"))
        link = os.path.join(self.tmp, "win")
        os.symlink(never, link)
        ok, why = roots.check(link, [never])
        self.assertFalse(ok)
        self.assertIn("never", why)
        indexer.NEVER = [never]
        indexer.ROOTS = [(link, "Link", "work")]
        n, _ = indexer.build()
        self.assertEqual(n, 0)  # never read

    def test_apply_settings_drops_symlinked_root(self):
        never = os.path.join(self.tmp, "windows"); os.makedirs(never)
        link = os.path.join(self.tmp, "win"); os.symlink(never, link)
        _touch(os.path.join(never, "secret.stl"))
        indexer.apply_settings({"roots": [roots._r(link, "L"), roots._r(self.tmp, "T", recurse=False)],
                                "never": [never]})
        # the loader only compares text (no disk access on the window's thread) ...
        self.assertEqual(indexer.ROOTS, [(link, "L", "work")])
        self.assertEqual(indexer.LOOSE, [(self.tmp, "T", "work")])
        self.assertTrue(indexer.blocked(link))
        indexer.build()  # ... and the scan refuses to read through the link
        self.assertEqual([r[1] for r in search.query("secret")], [])

    def test_bind_mount_by_device(self):
        d = os.path.join(self.tmp, "d"); os.makedirs(d)
        with mock.patch.object(roots, "_mount_devs", return_value={os.stat(d).st_dev}):
            self.assertTrue(roots.on_never_device(d, ["/mnt/elsewhere"]))
        with mock.patch.object(roots, "_mount_devs", return_value=set()):
            self.assertFalse(roots.on_never_device(d, ["/mnt/elsewhere"]))

    def test_mount_devs_reads_mountinfo_only(self):
        with mock.patch("builtins.open", mock.mock_open(read_data=
                "36 1 259:7 / /mnt/W rw - ntfs3 /dev/nvme0n1p4 rw\n37 1 8:2 / /mnt/My\\040Disk rw - ext4 /dev/sda2 rw\n")):
            self.assertEqual(roots._mount_devs(["/mnt/W", "/mnt/My Disk"]), {os.makedev(259, 7), os.makedev(8, 2)})

    def test_program_parts_by_folder_name_only(self):
        ok_dir = os.path.join(self.tmp, "my.steamy"); os.makedirs(ok_dir)
        self.assertTrue(roots.check(ok_dir)[0])
        bad = os.path.join(self.tmp, "SteamLibrary"); os.makedirs(bad)
        self.assertFalse(roots.check(bad)[0])


# ---------------------------------------------------------------- nested folders (review MAJOR 2)
class Nested(Temp):
    def test_each_file_listed_once_by_its_most_specific_folder(self):
        top = os.path.join(self.tmp, "X")
        _touch(os.path.join(top, "a.stl"))
        _touch(os.path.join(top, "backup", "b.stl"))
        indexer.ROOTS = [(top, "X all", "work"), (os.path.join(top, "backup"), "Backup", "copy")]
        indexer.LOOSE = [(top, "X root", "work")]
        n, _ = indexer.build()
        self.assertEqual(n, 2)
        db = sqlite3.connect(indexer.DB_PATH)
        rows = dict(db.execute("SELECT name, loc FROM files").fetchall())
        db.close()
        self.assertEqual(rows, {"a.stl": "X all", "b.stl": "Backup"})
        self.assertEqual([r[1] for r in search.query("b")], [])  # a copy: hidden from normal search
        self.assertEqual([r[1] for r in search.query("b", include_copies=True)], ["b.stl"])

    def test_check_allows_overlap(self):
        top = os.path.join(self.tmp, "X"); child = os.path.join(top, "c"); os.makedirs(child)
        listed = [roots._r(top, "X")]
        self.assertEqual(roots.check(child, roots=listed), (True, ""))  # 1.3: allowed, keeps its own name
        self.assertIn("inside", roots.overlaps(child, listed))
        listed = [roots._r(child, "C")]
        self.assertEqual(roots.check(top, roots=listed), (True, ""))
        self.assertIn("contains 1 folder", roots.overlaps(top, listed))
        self.assertIn("already in the list", roots.check(child, roots=listed)[1])  # the same folder twice: no
        listed = [roots._r(top, "X root", recurse=False)]
        self.assertEqual(roots.check(child, roots=listed), (True, ""))  # loose-only root: no overlap

    def test_dialog_allows_subfolders_on_a_parent(self):
        top = os.path.join(self.tmp, "X"); child = os.path.join(top, "c"); os.makedirs(child)
        d = findfiles.SettingsDialog(None, {"roots": [roots._r(top, "X root", recurse=False), roots._r(child, "C")],
                                            "never": []})
        d.table.item(0, 4).setCheckState(findfiles.Qt.Checked)  # Subfolders on the root
        with mock.patch.object(findfiles.QMessageBox, "warning") as warn:
            d.save()
        warn.assert_not_called()  # 1.3: a drive with its subfolders plus a named folder inside it is fine
        self.assertEqual(len(d.result_settings["roots"]), 2)


# ---------------------------------------------------------------- renamed while unplugged (review MINOR)
class Renamed(Temp):
    def test_kept_files_follow_the_new_name(self):
        gone = "/run/media/__nope__/D/X"
        indexer.ROOTS = [(gone, "Old name", "work")]
        os.makedirs(indexer.DB_DIR)
        indexer.build()
        db = sqlite3.connect(indexer.DB_PATH)
        db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)", (gone + "/m.stl", "m.stl", "m.stl", "stl", 1, 1,
                                                                    "Old name", "work", 1))
        db.commit(); db.close()
        indexer.ROOTS = [(gone, "New name", "work")]
        indexer.LABEL_RENAMES["Old name"] = "New name"
        n, missing = indexer.build()
        self.assertEqual(missing, ["New name"])
        self.assertEqual([(r[1], r[2]) for r in search.query("m")], [("m.stl", "New name")])
        self.assertEqual(indexer.LABEL_RENAMES, {})


# ---------------------------------------------------------------- clear pictures (review MINOR)
class ClearErrors(unittest.TestCase):
    def test_db_error_reported_not_raised(self):
        with mock.patch.object(thumbs, "connect", side_effect=sqlite3.OperationalError("database is locked")):
            ok, msg = thumbs.clear_all()
        self.assertFalse(ok)
        self.assertIn("locked", msg)


# ---------------------------------------------------------------- videos
@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
class Videos(Temp):
    def make(self, name="clip.mp4"):
        p = os.path.join(self.tmp, name)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
                        "-pix_fmt", "yuv420p", p], check=True, timeout=60)
        return p

    def test_real_frame_and_facts(self):
        img, note = previews.render(self.make(), 200)
        self.assertIsNotNone(img)
        self.assertLessEqual(max(img.width(), img.height()), 200)
        self.assertIn("320×240", note)
        self.assertIn("0:02", note)

    def test_not_a_video(self):
        p = os.path.join(self.tmp, "fake.mkv")
        _touch(p, b"not a video at all" * 300)
        img, note = previews.render(p, 200)
        self.assertIsNone(img)
        self.assertTrue(note)

    def test_video_in_archive_not_unpacked(self):
        img, note = previews.render(os.path.join(self.tmp, "a.zip") + "::clip.mp4", 200)
        self.assertIsNone(img)
        self.assertIn("archive", note)

    def test_videos_filter_and_typescript_not_video(self):
        self.assertIn("mkv", filetypes.filter_options()["Videos"])
        self.assertNotIn("ts", previews.VIDEO_EXTS)

    def test_play_button_only_for_videos(self):
        work = os.path.join(self.tmp, "work")
        clip = self.make("clip.mp4"); os.makedirs(work); shutil.move(clip, os.path.join(work, "clip.mp4"))
        _touch(os.path.join(work, "part.stl"))
        indexer.ROOTS = [(work, "Work", "work")]
        indexer.build()
        w = findfiles.Main()
        w.type.setCurrentText("Everything")
        w.q.setText("clip"); w.run_search()
        w.table.selectRow(0); w.on_select()
        self.assertTrue(w.play_btn.isVisibleTo(w))
        w.q.setText("part"); w.run_search()
        w.table.selectRow(0); w.on_select()
        self.assertFalse(w.play_btn.isVisibleTo(w))
        w.close()


# ---------------------------------------------------------------- remembered view, recent searches, chips
class ViewAndHistory(Temp):
    def setUp(self):
        super().setUp()
        self._env = mock.patch.dict(os.environ, {"FINDFILES_NO_VIEW_MEMORY": ""})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.work = os.path.join(self.tmp, "work")
        _touch(os.path.join(self.work, "piesa cantar.stl"))
        indexer.ROOTS = [(self.work, "Work", "work")]
        indexer.build()
        programs.set_state("recent", [])
        programs.set_state("last_view", None)

    def test_view_saved_and_restored(self):
        w = findfiles.Main()
        w.q.setText("piesa"); w.type.setCurrentText("3D models"); w.copies.setChecked(True)
        w.split.setSizes([700, 500])
        w.close()
        w2 = findfiles.Main()
        self.assertEqual(w2.q.text(), "piesa")
        self.assertEqual(w2.type.currentText(), "3D models")
        self.assertTrue(w2.copies.isChecked())
        w2.close()

    def test_damaged_state_means_default_view(self):
        programs.set_state("last_view", {"q": 5, "type": "nope", "geometry": "x", "split": [1, 2, 3]})
        w = findfiles.Main()
        self.assertEqual(w.q.text(), "")
        w.close()

    def test_recent_searches_dedup_and_cap(self):
        w = findfiles.Main()
        for i in range(20):
            w.q.setText(f"word{i}"); w._remember_search()
        w.q.setText("word19"); w._remember_search()
        recent = programs.get_state("recent")
        self.assertEqual(len(recent), findfiles.RECENT_MAX)
        self.assertEqual(recent[0], "word19")
        self.assertEqual(len(set(recent)), len(recent))
        w.close()

    def test_clear_history(self):
        programs.set_state("recent", ["a"]); programs.set_state("last_view", {"q": "a"})
        d = findfiles.SettingsDialog(None, {"roots": [], "never": []})
        with mock.patch.object(findfiles.QMessageBox, "information"):
            d.clear_history()
        self.assertTrue(d.history_cleared)
        self.assertEqual(programs.get_state("recent"), [])
        self.assertIsNone(programs.get_state("last_view"))

    def test_drive_summary(self):
        home = os.path.expanduser("~")
        indexer.ROOTS = [(f"{home}/Desktop", "Desktop", "work"), ("/mnt/__nope__/work", "X › work", "work"),
                         ("/mnt/__nope__/old", "X › old (copy)", "copy")]
        indexer.LOOSE = [("/mnt/__nope__", "X (root)", "work")]
        s = findfiles.drive_summary({"Desktop": 5, "X › work": 7, "X (root)": 1})
        self.assertEqual(s[0][:3], ("Home", True, 5))
        self.assertEqual(s[1][:3], ("X", False, 8))  # not mounted -> shown as unplugged

    def test_picture_progress_text(self):
        w = findfiles.Main()
        w._on_pictures(10, 5)
        self.assertIn("10 / 15 ready", w.pics_lbl.text())
        w._on_pictures(15, 0)
        self.assertIn("all 15 ready", w.pics_lbl.text())
        w._on_pictures(-1, 0)
        self.assertEqual(w.pics_lbl.text(), "")
        w.close()

    def test_idle_window_does_not_count(self):
        w = findfiles.Main()  # never shown = like minimised / hidden
        w._counting = False
        with mock.patch.object(findfiles.threading, "Thread") as th:
            w._count_pictures()
        th.assert_not_called()
        self.assertGreaterEqual(w._pics_timer.interval(), 60_000)
        w.close()

    def test_chip_text_plain_words(self):
        w = findfiles.Main()
        w._on_drives([("X", True, 1234, "/mnt/x\nX › a"), ("Data", False, 5, "/mnt/s\nData › b")])
        texts = [w.chips.itemAt(i).widget().text() for i in range(w.chips.count())]
        self.assertEqual(texts, ["X: 1,234 files", "Data: unplugged"])
        self.assertIn("plugged in", w.chips.itemAt(0).widget().toolTip())
        w.close()

    def test_refilling_a_full_list_is_fast(self):
        """2,000 results replacing 2,000 results froze the window for ~50 s (2026-11-26)."""
        import time as _t
        w = findfiles.Main(); w.resize(1300, 770); w.show(); APP.processEvents()
        rows = [(f"/x/d{i}/long model name number {i} final.stl", f"long model name number {i} final.stl",
                 "X › work", 1000 + i, 1.7e9 + i) for i in range(2000)]
        w._fill(rows); APP.processEvents()
        t = _t.time()
        w._fill(rows, keep=rows[5][0]); APP.processEvents()
        self.assertLess(_t.time() - t, 5)
        self.assertEqual(w.table.rowCount(), 2000)
        self.assertEqual(w.table.currentRow(), 5)  # the kept file is selected again
        self.assertEqual(w.table.horizontalHeader().sectionResizeMode(3), findfiles.QHeaderView.Interactive)
        w.close()

    def test_disk_label_never_raises(self):
        self.assertEqual(findfiles.disk_label("/definitely/not/a/mount"), "")

    def test_disk_label_reads_escaped_names(self):
        info = "36 1 8:2 / /mnt/__d__ rw - ntfs3 /dev/__sdz2 rw\n"
        with mock.patch("builtins.open", mock.mock_open(read_data=info)), \
                mock.patch.object(findfiles.os, "listdir", return_value=["other", "my\\x20disk"]), \
                mock.patch.object(findfiles.os.path, "realpath",
                                  side_effect=lambda p: "/dev/__sdz2" if p.endswith(("disk", "__sdz2")) else p):
            self.assertEqual(findfiles.disk_label("/mnt/__d__"), "my disk")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- progress + time left while scanning
class ScanProgress(Temp):
    def test_ticks_while_walking(self):
        work = os.path.join(self.tmp, "work")
        for i in range(25):
            _touch(os.path.join(work, f"f{i}.stl"))
        indexer.ROOTS = [(work, "Work", "work")]
        seen = []
        with mock.patch.object(indexer, "TICK_EVERY", 10):
            indexer.build(tick=lambda n, where: seen.append((n, where)))
        self.assertEqual(seen, [(10, "Work"), (20, "Work"), (25, "")])

    def test_eta_text(self):
        self.assertEqual(findfiles.eta_text(500, 0, 10, "X"), "X · 500 files so far")  # first scan: no guess
        t = findfiles.eta_text(100_000, 400_000, 60, "X › work")
        self.assertIn("100,000 of about 400,000 files (25 %)", t)
        self.assertIn("about 3 min left", t)
        self.assertIn("estimate", t)
        self.assertIn("less than a minute", findfiles.eta_text(90, 100, 30))
        self.assertIn("(99 %)", findfiles.eta_text(150, 100, 30))  # more files than last time: never 100 %+

    def test_status_shows_progress(self):
        work = os.path.join(self.tmp, "work"); _touch(os.path.join(work, "a.stl"))
        indexer.ROOTS = [(work, "Work", "work")]
        indexer.build()
        w = findfiles.Main()
        w._indexing, w._scan_what, w._scan_expected = True, "Updating the file list", 0
        w._scan_start = findfiles.time.time() - 10
        w.status.setText("3 found")
        w._on_scan(1, "Work")
        self.assertIn("1 of about 1 files", w.scan_lbl.text())
        self.assertEqual(w.status.text(), "3 found")  # the search line is not hidden by progress
        w._on_scan(1, "")
        self.assertIn("archives", w.scan_lbl.text())
        w.on_indexed(1, [])
        self.assertEqual(w.scan_lbl.text(), "")
        w.close()


# ---------------------------------------------------------------- audit 2026-11-26 v1.1 fixes
class AuditFixes(Temp):
    def test_rename_made_during_a_scan_survives(self):
        gone = "/run/media/__nope__/D/X"
        indexer.ROOTS = [(gone, "B", "work")]
        indexer.LABEL_RENAMES["A"] = "B"
        real_walk = indexer._walk

        def walk_and_rename(*a, **k):  # Settings saves another rename while this scan runs
            indexer.LABEL_RENAMES["C"] = "D"
            return real_walk(*a, **k)
        os.makedirs(indexer.DB_DIR)
        with mock.patch.object(indexer, "_drive_missing", side_effect=lambda r: False), \
                mock.patch.object(indexer, "_walk", walk_and_rename):
            indexer.build()
        self.assertEqual(indexer.LABEL_RENAMES, {"C": "D"})  # only the applied one is cleared

    def test_watch_update_keeps_nested_location_separate(self):
        top = os.path.join(self.tmp, "X"); inner = os.path.join(top, "inner")
        _touch(os.path.join(inner, "deep.stl")); os.makedirs(top, exist_ok=True)
        indexer.ROOTS = [(top, "Top", "work"), (inner, "Inner", "copy")]
        indexer.build()
        newdir = os.path.join(top, "new"); _touch(os.path.join(newdir, "n.stl"))
        indexer.update_dirs([top])
        db = sqlite3.connect(indexer.DB_PATH)
        rows = db.execute("SELECT name, loc FROM files ORDER BY name").fetchall()
        db.close()
        self.assertEqual(rows, [("deep.stl", "Inner"), ("n.stl", "Top")])

    def test_never_bind_mount_inside_a_folder_skipped(self):
        top = os.path.join(self.tmp, "X"); win = os.path.join(top, "win")
        _touch(os.path.join(top, "a.stl")); _touch(os.path.join(win, "secret.stl"))
        indexer.ROOTS = [(top, "Top", "work")]
        with mock.patch.object(roots, "never_mounts", return_value={win}):
            indexer.build()
        self.assertEqual([r[1] for r in search.query("secret")], [])
        self.assertEqual([r[1] for r in search.query("a")], ["a.stl"])

    def test_never_mounts_from_mountinfo(self):
        info = ("36 1 259:7 / /mnt/W rw - ntfs3 /dev/nvme0n1p4 rw\n"
                "37 1 259:7 /Users /home/u/winbind rw - ntfs3 /dev/nvme0n1p4 rw\n"
                "38 1 8:2 / /mnt/Other rw - ext4 /dev/sda2 rw\n")
        with mock.patch("builtins.open", mock.mock_open(read_data=info)):
            self.assertEqual(roots.never_mounts(["/mnt/W"]), {"/mnt/W", "/home/u/winbind"})

    def test_apply_settings_never_touches_disks(self):
        with mock.patch("os.stat", side_effect=AssertionError("disk access on the UI thread")), \
                mock.patch("os.path.realpath", side_effect=AssertionError("disk access on the UI thread")):
            indexer.apply_settings({"roots": [roots._r("/mnt/A/x", "A"), roots._r("/mnt/W/y", "W")],
                                    "never": ["/mnt/W"]})
        self.assertEqual(indexer.ROOTS, [("/mnt/A/x", "A", "work")])

    def test_chip_names_unique(self):
        indexer.ROOTS = [("/mnt/__k__/Comfy/output", "ComfyUI output (ssd)", "work"),
                         ("/mnt/__a__/Comfy/output", "ComfyUI output (nvme)", "work"),
                         ("/mnt/__x__/w", "X › w", "work"), ("/mnt/__y__/v", "Y › v", "work")]
        indexer.LOOSE = [("/mnt/__x__", "X (root)", "work")]
        names = [n for n, *_ in findfiles.drive_summary({})]
        self.assertIn("__k__", names)  # a folder name is never shown as a drive name
        self.assertIn("__a__", names)
        self.assertNotIn("ComfyUI output (ssd)", names)
        self.assertEqual(names.count("X"), 1)
        self.assertEqual(len(set(names)), len(names))

    def test_picture_counter_stops_when_complete_and_survives_errors(self):
        w = findfiles.Main()
        w._on_pictures(5, 0)
        self.assertFalse(w._pics_timer.isActive())
        w.on_indexed(1, [])
        self.assertTrue(w._pics_timer.isActive())
        import types

        def run_now(target, daemon=True, **_k):  # the count job runs here, synchronously
            return types.SimpleNamespace(start=target)
        w._counting = False  # (on_indexed above may have started a real count in the background)
        with mock.patch.object(thumbs, "connect", side_effect=ValueError("odd")), \
                mock.patch.object(findfiles.threading, "Thread", run_now):
            w._count_pictures(force=True)
        APP.processEvents()
        self.assertFalse(w._counting)  # an unexpected error does not leave the counter stuck
        self.assertEqual(w.pics_lbl.text(), "")
        w.close()
