"""1.6: PDF pages in the preview, Markdown shown formatted, a Full screen button.
Temp folders only; never the real drives or the real file list."""
import os
import shutil
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
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QFont, QImage, QKeyEvent, QPageSize, QPainter, QPdfWriter  # noqa: E402
from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])
import findfiles  # noqa: E402
import previews  # noqa: E402
import viewer  # noqa: E402


def pump(sec=0.2):
    end = time.time() + sec
    while time.time() < end:
        APP.processEvents(); time.sleep(0.01)


def make_pdf(path, pages):
    w = QPdfWriter(path); w.setPageSize(QPageSize(QPageSize.A4))
    p = QPainter(w); p.setFont(QFont("Sans", 40))
    for i in range(pages):
        p.drawText(400 + 300 * i, 1500 + 600 * i, f"Page {i + 1}")
        p.fillRect(300 * i, 3000, 1500, 1500, Qt.black)       # a big mark in a different place on each page
        if i < pages - 1:
            w.newPage()
    p.end()


def key(k):
    return QKeyEvent(QEvent.KeyPress, k, Qt.NoModifier)


class PdfPages(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ff-pdf-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.pdf = os.path.join(self.d, "manual.pdf"); make_pdf(self.pdf, 5)

    def test_every_page_renders_differently_and_quickly(self):
        imgs = []
        for n in range(5):
            t = time.time()
            img, note = previews.pdf_page(self.pdf, n, 600)
            self.assertLess(time.time() - t, 0.1)
            self.assertIsNotNone(img); self.assertEqual(note, f"page {n + 1} of 5")
            imgs.append(img)
        for a in range(5):
            for b in range(a + 1, 5):
                self.assertNotEqual(imgs[a], imgs[b], f"pages {a + 1} and {b + 1} look the same")

    def test_bad_pages_and_files_are_refused_not_raised(self):
        img, note = previews.pdf_page(self.pdf, 5, 600)
        self.assertIsNone(img); self.assertIn("No page 6", note)
        img, note = previews.pdf_page(self.pdf, -1, 600)
        self.assertIsNone(img)
        img, note = previews.pdf_page(os.path.join(self.d, "gone.pdf"), 1, 600)
        self.assertIsNone(img); self.assertIn("not found", note)
        junk = os.path.join(self.d, "junk.pdf")
        with open(junk, "wb") as f:
            f.write(b"%PDF-1.4 not really")
        img, note = previews.pdf_page(junk, 1, 600)
        self.assertIsNone(img)

    def test_first_page_note_is_what_the_window_reads(self):
        img, note = previews.render(self.pdf, 600)
        m = previews.PDF_PAGE_NOTE.match(note)
        self.assertTrue(m); self.assertEqual((m.group(1), m.group(2)), ("1", "5"))

    def test_page_keys(self):
        v = viewer.ImageView(); got = []
        v.page.connect(got.append)
        for k in (Qt.Key_PageDown, Qt.Key_PageUp, Qt.Key_Home, Qt.Key_End):
            v.keyPressEvent(key(k))
        self.assertEqual(got, [1, -1, -viewer.PAGE_JUMP, viewer.PAGE_JUMP])
        f = viewer.FullImage(); pages, steps = [], []
        f.page.connect(pages.append); f.step.connect(steps.append)
        for k in (Qt.Key_PageDown, Qt.Key_Right, Qt.Key_PageUp, Qt.Key_Left):
            f.keyPressEvent(key(k))
        self.assertEqual((pages, steps), ([1, -1], [1, -1]))   # pages and results are separate keys now
        f.close()


class Window(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ff-win16-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.pdf5 = os.path.join(self.d, "manual.pdf"); make_pdf(self.pdf5, 5)
        self.pdf1 = os.path.join(self.d, "flyer.pdf"); make_pdf(self.pdf1, 1)
        img = QImage(800, 600, QImage.Format_RGB32); img.fill(0x446688)
        self.photo = os.path.join(self.d, "photo.png"); img.save(self.photo)
        self.md = os.path.join(self.d, "notes.md")
        with open(self.md, "w", encoding="utf-8") as f:
            f.write("# Print settings\n\n- **layer** 0.05 mm\n- exposure 2 s\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
                    "\n![pic](photo.png) [link](https://example.invalid/)\n")
        self.txt = os.path.join(self.d, "plain.txt")
        with open(self.txt, "w", encoding="utf-8") as f:
            f.write("# not a heading here\n")
        self.files = [self.pdf5, self.pdf1, self.photo, self.md, self.txt]
        self.w = findfiles.Main(); self.w.resize(1300, 770); self.w.show(); pump(0.1)
        for t in threading.enumerate():
            if t.name == "findfiles-index":
                t.join(10)
        pump(0.1)
        now = time.time()
        self.w._fill([(p, os.path.basename(p), "Temp", os.path.getsize(p), now) for p in self.files])
        self.addCleanup(self._close)

    def _close(self):
        if self.w._fullpic is not None:
            self.w._fullpic.close()
        self.w._cols_timer.stop(); self.w.close(); pump(0.05)

    def wait(self, until, sec=8):
        end = time.time() + sec
        while time.time() < end and not until():
            pump(0.03)
        self.assertTrue(until())

    def select(self, path):
        self.w.table.selectRow(self.files.index(path))

    def test_pages_turn_and_a_new_selection_starts_at_page_one(self):
        w = self.w
        self.select(self.pdf5)
        self.wait(lambda: w.viewbar.isVisibleTo(w) and w.page_lbl.text() == "Page 1 of 5")
        self.assertFalse(w.page_prev.isEnabled()); self.assertTrue(w.page_next.isEnabled())
        first = w.pic.pixmap().toImage()
        w.page_next.click()
        self.wait(lambda: "page 2 of 5" in w.info.text())
        self.assertEqual(w.page_lbl.text(), "Page 2 of 5")
        self.assertNotEqual(w.pic.pixmap().toImage(), first)
        w.pdf_go(viewer.PAGE_JUMP)                                # End
        self.wait(lambda: "page 5 of 5" in w.info.text())
        self.assertFalse(w.page_next.isEnabled())
        self.assertTrue(w.pdf_go(1))                              # past the end: stays on 5
        self.assertEqual(w._pdf[2], 4)
        self.select(self.photo)
        self.wait(lambda: not w.pic.pixmap().isNull() and "800×600" in w.info.text())
        self.assertFalse(w.page_prev.isVisibleTo(w))              # not a PDF: no page buttons
        self.assertFalse(w.pdf_go(1))
        self.select(self.pdf5)
        self.wait(lambda: w.page_lbl.text() == "Page 1 of 5" and "page 1 of 5" in w.info.text())

    def test_one_page_pdf_has_no_page_buttons(self):
        self.select(self.pdf1)
        self.wait(lambda: "page 1 of 1" in self.w.info.text())
        self.assertFalse(self.w.page_prev.isVisibleTo(self.w)); self.assertFalse(self.w.page_lbl.isVisibleTo(self.w))

    def test_a_late_page_does_not_replace_a_newer_one(self):
        w = self.w
        self.select(self.pdf5)
        self.wait(lambda: w.page_lbl.text() == "Page 1 of 5")
        w._pdf = (self.pdf5, 5, 3)                                # page 4 was asked for
        img, note = previews.pdf_page(self.pdf5, 1, 400)          # page 2 arrives late
        before = w.info.text()
        w.on_preview(self.pdf5, img, note)
        self.assertEqual(w.info.text(), before)

    def test_page_keys_in_full_screen(self):
        w = self.w
        self.select(self.pdf5)
        self.wait(lambda: w.page_lbl.text() == "Page 1 of 5" and not w.pic.pixmap().isNull())
        with mock.patch.object(viewer.FullImage, "showFullScreen", viewer.FullImage.show):
            w.picture_full_screen()
        w._fullpic.keyPressEvent(key(Qt.Key_PageDown))
        self.wait(lambda: "page 2 of 5" in w.info.text())
        self.assertEqual(w.table.currentRow(), self.files.index(self.pdf5))   # still the same file
        w._fullpic.keyPressEvent(key(Qt.Key_Right))                          # arrows = next result
        pump(0.1)
        self.assertEqual(w.table.currentRow(), self.files.index(self.pdf1))
        w._fullpic.keyPressEvent(key(Qt.Key_PageDown))                       # 1-page PDF: next result
        pump(0.1)
        self.assertEqual(w.table.currentRow(), self.files.index(self.photo))


class Markdown(Window):
    def test_markdown_is_formatted_and_can_be_switched_to_plain(self):
        w = self.w
        self.select(self.md)
        self.wait(lambda: w.picstack.currentWidget() is w.mdview)
        shown = w.mdview.toPlainText()
        self.assertIn("Print settings", shown); self.assertNotIn("# Print", shown)     # a heading, not "#"
        self.assertNotIn("**", shown); self.assertNotIn("|---|", shown)                # bold and table drawn
        self.assertTrue(w.md_btn.isVisibleTo(w)); self.assertFalse(w.md_btn.isChecked())
        w.md_btn.click()
        self.assertIs(w.picstack.currentWidget(), w.textview)
        self.assertIn("# Print settings", w.textview.toPlainText())                     # the raw text
        w.md_btn.click()
        self.assertIs(w.picstack.currentWidget(), w.mdview)

    def test_plain_choice_is_kept_for_the_next_markdown_file_but_not_used_elsewhere(self):
        w = self.w
        self.select(self.md)
        self.wait(lambda: w.picstack.currentWidget() is w.mdview)
        w.md_btn.click()                                                          # plain
        self.select(self.txt)
        self.wait(lambda: "not a heading" in w.textview.toPlainText())
        self.assertIs(w.picstack.currentWidget(), w.textview)
        self.assertFalse(w.md_btn.isVisibleTo(w)); self.assertFalse(w.viewbar.isVisibleTo(w))   # text: no bar at all
        self.select(self.md)
        self.wait(lambda: "# Print settings" in w.textview.toPlainText())
        self.assertIs(w.picstack.currentWidget(), w.textview)                    # still plain
        self.assertTrue(w.md_btn.isChecked())

    def test_links_and_pictures_inside_are_never_opened(self):
        v = viewer.MarkdownView()
        self.assertFalse(v.openLinks()); self.assertFalse(v.openExternalLinks())
        self.assertIsNone(v.loadResource(2, findfiles.QUrl.fromLocalFile(self.photo)))
        v.show_markdown(open(self.md, encoding="utf-8").read())
        self.assertIn("link", v.toPlainText())

    def test_long_markdown_is_shown_plain_and_never_freezes_the_window(self):
        w = self.w
        big = os.path.join(self.d, "big.md")
        with open(big, "w", encoding="utf-8") as f:
            f.write("# Big\n\n" + "- line of text\n" * 40000)            # about 600 KB of list
        self.files.append(big)
        w._fill([(p, os.path.basename(p), "Temp", os.path.getsize(p), time.time()) for p in self.files])
        t = time.time()
        self.select(big)
        self.wait(lambda: "shown as plain text" in w.info.text())
        self.assertLess(time.time() - t, 2.0)
        self.assertIs(w.picstack.currentWidget(), w.textview)
        self.assertFalse(w.md_btn.isVisibleTo(w))

    def test_worst_case_at_the_format_limit_is_quick(self):
        unit = "| a | b | c |\n"                                     # tables were the slowest measured
        body = "| a | b | c |\n|---|---|---|\n" + unit * (previews.MARKDOWN_FORMAT_LIMIT // len(unit) - 3)
        self.assertLessEqual(len(body.encode()), previews.MARKDOWN_FORMAT_LIMIT)
        v = viewer.MarkdownView(); v.resize(480, 600)
        t = time.time(); v.show_markdown(body); APP.processEvents()
        self.assertLess(time.time() - t, 0.5)

    def test_new_search_with_nothing_selected_clears_text_and_bars(self):
        w = self.w
        self.select(self.md)
        self.wait(lambda: w.picstack.currentWidget() is w.mdview)
        w._fill([(self.photo, "photo.png", "Temp", 1, time.time())])           # nothing reselected
        self.assertIs(w.picstack.currentWidget(), w.pic)
        self.assertFalse(w.viewbar.isVisibleTo(w))


class FullScreenButton(Window):
    def test_enabled_only_for_a_picture_or_a_playing_video(self):
        w = self.w
        self.assertFalse(w.full_btn.isVisibleTo(w))                           # nothing selected
        self.select(self.txt)
        self.wait(lambda: w.picstack.currentWidget() is w.textview)
        self.assertFalse(w.full_btn.isVisibleTo(w))                           # text: nothing to fill the screen
        w.picture_full_screen()
        self.assertIsNone(w._fullpic)                                         # F11 on text does nothing
        px = findfiles.QPixmap(50, 50); px.fill(Qt.red)
        w.pic.setPixmap(px)                                                   # even if a picture is left behind
        self.assertIs(w.picstack.currentWidget(), w.textview)
        self.assertFalse(w.full_btn.isVisibleTo(w))
        w.picture_full_screen()
        self.assertIsNone(w._fullpic)
        self.select(self.photo)
        self.wait(lambda: not w.pic.pixmap().isNull())
        self.assertTrue(w.full_btn.isVisibleTo(w) and w.full_btn.isEnabled())
        self.assertTrue(w.viewbar.isVisibleTo(w)); self.assertFalse(w.page_prev.isVisibleTo(w))
        with mock.patch.object(viewer.FullImage, "showFullScreen", viewer.FullImage.show):
            w.full_btn.click()
        self.assertIsNotNone(w._fullpic)
        w.full_btn.click()                                                    # again = leave
        pump(0.05)
        self.assertIsNone(w._fullpic)

    def test_with_a_playing_video_it_uses_the_players_own_full_screen(self):
        w = self.w
        w.picstack.addWidget(viewer.ImageView("stand-in"))                   # the player's place in the stack
        stand_in = w.picstack.widget(w.picstack.count() - 1)
        w._video = stand_in; stand_in.set_full = mock.Mock()
        w.picstack.setCurrentWidget(stand_in)
        self.assertTrue(w.full_btn.isVisibleTo(w) and w.full_btn.isEnabled())
        w.full_btn.click()
        stand_in.set_full.assert_called_once_with(True)
        self.assertIsNone(w._fullpic)
        w._video = None


class Width(unittest.TestCase):
    def test_the_preview_side_is_no_wider_than_in_1_5(self):
        w = findfiles.Main(); w.resize(1000, 600); w.show(); pump(0.05)
        try:
            for name in ("full_btn", "page_prev", "page_next", "md_btn"):
                getattr(w, name).show()                          # the widest the bar under the picture gets
            w.viewbar.show(); pump(0.05)
            self.assertLessEqual(w.split.widget(1).minimumSizeHint().width(), 493)   # 1.5 measured 493 px
        finally:
            w._cols_timer.stop(); w.close()


if __name__ == "__main__":
    unittest.main()
