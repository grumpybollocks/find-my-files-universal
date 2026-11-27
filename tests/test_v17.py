"""1.7: pictures fill the preview, the Type menu with submenus, "did you mean". Temp folders only."""
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safe_env  # noqa: E402,F401
sys.path.insert(0, HERE)
from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])
import viewer  # noqa: E402


class Stretch(unittest.TestCase):
    def setUp(self):
        self.v = viewer.ImageView(); self.v.resize(480, 560)
        self.small = QPixmap(64, 64); self.small.fill(Qt.blue)
        self.big = QPixmap(4000, 2000); self.big.fill(Qt.red)

    def test_a_small_picture_fills_the_panel(self):
        self.v.setPixmap(self.small)
        self.assertFalse(self.v.zoomed())
        self.assertAlmostEqual(self.v.scale(), 7.5)                     # 480 / 64
        r = self.v._target()
        self.assertAlmostEqual(r.width(), 480); self.assertAlmostEqual(r.left(), 0)

    def test_zoom_out_stops_at_real_size_and_back_in_to_fit(self):
        self.v.setPixmap(self.small)
        for _ in range(30):
            self.v.zoom_by(1 / viewer.ZOOM_STEP)
        self.assertAlmostEqual(self.v.scale(), 1.0)                     # real size, no smaller
        r = self.v._target()                                            # centred
        self.assertAlmostEqual(r.center().x(), 240, delta=1); self.assertAlmostEqual(r.center().y(), 280, delta=1)
        self.v.fit(); self.assertAlmostEqual(self.v.scale(), 7.5)
        self.v.zoom_to(1.0); self.assertTrue(self.v.zoomed())           # key 1 = real size
        self.v.zoom_to(7.5); self.assertFalse(self.v.zoomed())          # back at fit = fitted
        self.v.zoom_to(100); self.assertEqual(self.v.scale(), viewer.ZOOM_MAX)

    def test_a_large_photo_is_unchanged_and_each_new_picture_starts_fitted(self):
        self.v.setPixmap(self.big)
        self.assertAlmostEqual(self.v.scale(), 0.12)
        self.v.zoom_by(1 / viewer.ZOOM_STEP)
        self.assertAlmostEqual(self.v.scale(), 0.12)                    # can't go below fit
        self.v.zoom_to(2.0)
        self.v.setPixmap(self.small)
        self.assertFalse(self.v.zoomed())

    def test_double_click_switches_fit_and_real_size(self):
        self.v.setPixmap(self.small)
        e = mock.Mock(); e.position.return_value = QPointF(240, 280)
        self.v.mouseDoubleClickEvent(e); self.assertAlmostEqual(self.v.scale(), 1.0)
        self.v.mouseDoubleClickEvent(e); self.assertFalse(self.v.zoomed())

    def test_resize_keeps_a_fitted_picture_fitted(self):
        self.v.setPixmap(self.small); self.v.show()
        self.v.resize(320, 320); APP.processEvents()
        self.assertAlmostEqual(self.v.scale(), 5.0)
        self.v.close()



class TypeMenu(unittest.TestCase):
    COUNTS = {"pdf": 1248, "txt": 29844, "md": 485, "stl": 900, "docx": 0}

    def test_names_and_endings(self):
        import findfiles as ff
        self.assertEqual(ff.type_exts("Text & documents › PDF"), ["pdf"])
        self.assertEqual(ff.type_exts("3D models"), ff.FILTERS["3D models"])
        self.assertIsNone(ff.type_exts("Everything"))
        self.assertEqual(ff.type_exts("Nonsense › XYZ"), ff.FILTERS["All my work"])
        self.assertTrue(ff.valid_type("Text & documents › pdf"))
        for bad in ("Text & documents › STL", "Everything › PDF", "Nope", None, 5):
            self.assertFalse(ff.valid_type(bad), bad)

    def test_menu_has_a_submenu_per_group_with_counts_sorted(self):
        import findfiles as ff
        p = ff.TypePicker(lambda: dict(self.COUNTS))
        p._fill()
        acts = p.menu().actions()
        shown = lambda a: a.text().replace("&&", "&")                    # what the user reads
        self.assertEqual([shown(a) for a in acts if not a.menu()], ["All my work", "Everything"])
        subs = {shown(a).split("  (")[0]: a.menu() for a in acts if a.menu()}
        self.assertIn("Text && documents", [a.text().split("  (")[0] for a in acts])   # '&' kept, not a shortcut mark
        self.assertEqual(len(subs), len(ff.FILTERS) - 2)
        text = [shown(a) for a in subs["Text & documents"].actions() if not a.isSeparator()]
        self.assertEqual(text, ["All text & documents", "TXT  (29,844)", "PDF  (1,248)", "MD  (485)"])   # no DOCX (0)
        got = []
        p.currentIndexChanged.connect(got.append)
        [a for a in subs["Text & documents"].actions() if a.text().startswith("PDF")][0].trigger()
        self.assertEqual(p.currentText(), "Text & documents › PDF"); self.assertEqual(p.text(), "Text && documents › PDF")
        self.assertEqual(got, [0])
        p._fill()                                                       # reopened: the choice is ticked
        sub = {shown(a).split("  (")[0]: a.menu() for a in p.menu().actions() if a.menu()}["Text & documents"]
        self.assertTrue([a for a in sub.actions() if a.text().startswith("PDF")][0].isChecked())
        p.setCurrentText("bad name"); self.assertEqual(p.currentText(), "Text & documents › PDF")

    def test_window_passes_one_ending_with_drive_and_backups_together(self):
        import findfiles as ff
        w = ff.Main()
        try:
            for t in threading.enumerate():
                if t.name == "findfiles-index":
                    t.join(10)
            seen = []
            with mock.patch.object(ff.search, "query_sized", lambda *a, **k: (seen.append((a, k)) or ([], None))):
                w._drives = [("X", True, 5, "/mnt/x\nX › a")]
                w._only = ("X", "/mnt/x")
                w.copies.setChecked(True)
                w.q.setText("bracket")
                w.type.setCurrentText("Text & documents › PDF")
                w.run_search()
            a, k = seen[-1]
            self.assertEqual((a[0], a[1], a[2], k["under"]), ("bracket", ["pdf"], True, ("X › a",)))
            counts = w._type_counts()                                   # empty temp list: no error
            self.assertIsInstance(counts, dict)
        finally:
            w._cols_timer.stop(); w.close()

    def test_saved_choice_comes_back(self):
        import findfiles as ff
        with mock.patch.dict(os.environ, {"FINDFILES_NO_VIEW_MEMORY": ""}):
            w = ff.Main()
            try:
                w.q.setText("x"); w.type.setCurrentText("Text & documents › MD"); w._save_view()
            finally:
                w._cols_timer.stop(); w.close()
            w2 = ff.Main()
            try:
                self.assertEqual(w2.type.currentText(), "Text & documents › MD")
            finally:
                w2._cols_timer.stop(); w2.close()



class DidYouMean(unittest.TestCase):
    NAMES = ["bracket holder.stl", "bracket small.stl", "celtic knot.stl", "celtic cross.stl", "cruce 4-1.stl",
             "cruce simpla.stl", "biserica mare.stl", "biserica mica.3mf", "tar notes.txt", "manual bracket.pdf"]

    def setUp(self):
        import indexer, search
        self.indexer, self.search = indexer, search
        self.tmp = tempfile.mkdtemp(prefix="ff-dym-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        work = os.path.join(self.tmp, "work"); os.makedirs(work)
        for n in self.NAMES:
            with open(os.path.join(work, n), "wb") as f:
                f.write(b"x")
        self.saved = (indexer.ROOTS, indexer.LOOSE, indexer.DB_DIR, indexer.DB_PATH)
        self.addCleanup(self._restore)
        indexer.ROOTS = [(work, "Work", "work")]; indexer.LOOSE = []
        indexer.DB_DIR = os.path.join(self.tmp, "db"); indexer.DB_PATH = os.path.join(indexer.DB_DIR, "index.db")
        indexer.build()

    def _restore(self):
        self.indexer.ROOTS, self.indexer.LOOSE, self.indexer.DB_DIR, self.indexer.DB_PATH = self.saved

    def dym(self, q, **kw):
        return self.search.did_you_mean(q, **kw)

    def test_typos_english_romanian_and_swapped_letters(self):
        self.assertEqual(self.dym("brakcet"), ("bracket", 3))
        self.assertEqual(self.dym("cruec"), ("cruce", 3))   # swapped letters = one typo; 3 = cruce x2 + "cross" (en-ro pair)
        self.assertEqual(self.dym("biserca"), ("biserica", 2))                 # Romanian, no accents typed
        self.assertEqual(self.dym("bișerca"), ("biserica", 2))                 # ...and with one
        self.assertEqual(self.dym("BRACKTE holder"), ("bracket holder", 1))    # one bad word among good ones

    def test_no_suggestion_when_it_would_not_help(self):
        self.assertEqual(self.dym("bracket"), (None, ""))                      # finds files already
        self.assertEqual(self.dym("brk"), (None, ""))                          # under 4 letters
        self.assertEqual(self.dym("xyzzyq"), (None, ""))                       # nothing close
        self.assertEqual(self.dym("brakcet celtc"), (None, ""))                # both fixed, still no file has both
        self.assertEqual(self.dym("bracket celtic"), (None, "together"))       # nothing to fix: say why
        self.assertEqual(self.dym(""), (None, ""))

    def test_numbers_are_left_alone_and_filters_are_kept(self):
        self.assertEqual(self.dym("4-1 cruec"), ("4-1 cruce", 1))
        self.assertEqual(self.dym("brakcet", exts=["pdf"]), ("bracket", 1))    # counted inside the filter
        self.assertEqual(self.dym("celtc", exts=["pdf"]), (None, ""))          # "celtic" has no PDF: not offered

    def test_the_file_ending_is_not_a_word(self):
        v = self.search.vocabulary()
        self.assertNotIn("stl", v); self.assertEqual(v.get("tar"), 1)          # "tar notes.txt": a word of the name
        self.assertEqual(self.search._typos("cruec", "cruce", 2), 1)
        self.assertEqual(self.search._typos("abc", "xyz", 1), 2)               # stops early above the limit

    def test_window_shows_and_takes_the_suggestion(self):
        import findfiles as ff
        w = ff.Main(); w.resize(1200, 700); w.show()
        try:
            for t in threading.enumerate():
                if t.name == "findfiles-index":
                    t.join(10)
            APP.processEvents()
            w.type.setCurrentText("Everything")
            w.q.setText("brakcet"); w.run_search()
            end = time.time() + 8
            while time.time() < end and "Did you mean" not in w.hint.text():
                APP.processEvents(); time.sleep(0.02)
            self.assertIn("Did you mean", w.hint.text()); self.assertIn("bracket", w.hint.text())
            self.assertIn("3 files", w.hint.text())
            w.hint.linkActivated.emit("bracket")
            self.assertEqual(w.q.text(), "bracket"); self.assertEqual(w.table.rowCount(), 3)
            w.q.setText("bracket celtic"); w.run_search()
            end = time.time() + 8
            while time.time() < end and "on its own" not in w.hint.text():
                APP.processEvents(); time.sleep(0.02)
            self.assertIn("Each word finds files on its own", w.hint.text())
            w.on_suggest(w._suggest_n - 1, "bracket celtic", "old answer", 9)   # an old answer is dropped
            self.assertNotIn("old answer", w.hint.text())
        finally:
            w._cols_timer.stop(); w.close()


if __name__ == "__main__":
    unittest.main()
