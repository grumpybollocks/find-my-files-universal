"""1.5: the preview panel as a media viewer - zoomable pictures, text files, sound files, full screen
with controls. Temp folders only; never the real drives or the real file list."""
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
from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QImage, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QStackedWidget  # noqa: E402

APP = QApplication.instance() or QApplication([])
import findfiles  # noqa: E402
import previews  # noqa: E402
import viewer  # noqa: E402


def pump(sec=0.2):
    end = time.time() + sec
    while time.time() < end:
        APP.processEvents(); time.sleep(0.01)


class Zoom(unittest.TestCase):
    def setUp(self):
        self.v = viewer.ImageView("nothing yet"); self.v.resize(400, 300)
        self.px = QPixmap(2000, 1000); self.px.fill(Qt.red)

    def test_starts_fitted_and_never_blows_up_a_small_picture(self):
        self.assertEqual(self.v.text(), "nothing yet")
        self.v.setPixmap(self.px)
        self.assertFalse(self.v.zoomed())
        self.assertAlmostEqual(self.v.scale(), 0.2)
        small = QPixmap(100, 50); small.fill(Qt.blue)
        self.v.setPixmap(small)
        self.assertEqual(self.v.scale(), 1.0)

    def test_wheel_zoom_keeps_the_point_under_the_pointer(self):
        self.v.setPixmap(self.px)
        at = QPointF(300, 100)
        before = self.v._to_picture(at)
        self.v.zoom_by(viewer.ZOOM_STEP ** 4, at)
        self.assertTrue(self.v.zoomed())
        after = self.v._to_picture(at)
        self.assertAlmostEqual(before.x(), after.x(), delta=1.0)
        self.assertAlmostEqual(before.y(), after.y(), delta=1.0)

    def test_zoom_limits_and_back_to_fit(self):
        self.v.setPixmap(self.px)
        self.v.zoom_to(1000)
        self.assertEqual(self.v.scale(), viewer.ZOOM_MAX)
        self.v.zoom_to(0.0001)                         # below "fit" = fit
        self.assertFalse(self.v.zoomed())
        self.v.zoom_to(1.0); self.assertAlmostEqual(self.v.scale(), 1.0)
        self.v.fit(); self.assertFalse(self.v.zoomed())

    def test_a_zoomed_picture_cannot_be_dragged_out_of_the_panel(self):
        self.v.setPixmap(self.px); self.v.zoom_to(1.0)
        self.v._centre = QPointF(-5000, 99999); self.v._clamp()
        r = self.v._target()
        self.assertLessEqual(r.left(), 0.5); self.assertGreaterEqual(r.right(), self.v.width() - 0.5)
        self.assertLessEqual(r.top(), 0.5); self.assertGreaterEqual(r.bottom(), self.v.height() - 0.5)

    def test_sharper_version_of_the_same_picture_keeps_the_view(self):
        small = QPixmap(500, 250); small.fill(Qt.red)
        self.v.setPixmap(small); self.v.zoom_to(2.0, QPointF(100, 100))
        spot = self.v._to_picture(QPointF(200, 150))
        self.v.setPixmap(self.px, keep_view=True)            # 4x the pixels
        self.assertAlmostEqual(self.v.scale(), 0.5)
        new = self.v._to_picture(QPointF(200, 150))
        self.assertAlmostEqual(new.x(), spot.x() * 4, delta=2); self.assertAlmostEqual(new.y(), spot.y() * 4, delta=2)
        self.v.setPixmap(small)                               # another file: fitted again
        self.assertFalse(self.v.zoomed())

    def test_paint_and_keys_do_not_raise(self):
        self.v.setPixmap(self.px); self.v.show(); pump(0.05)
        self.v.grab()
        self.v.zoom_to(4.0); self.v.grab()
        self.v.setPixmap(QPixmap()); self.v.setText("gone"); self.v.grab()
        self.v.close()


class Text(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ff-text-")
        self.addCleanup(shutil.rmtree, self.d, True)

    def p(self, name, data):
        path = os.path.join(self.d, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_text_by_ending_and_by_content(self):
        self.assertTrue(previews.is_text(self.p("notes.txt", b"hello")))
        self.assertTrue(previews.is_text(self.p("README", b"plain words\nmore\n")))
        self.assertFalse(previews.is_text(self.p("blob", b"\x00\x01\x02\x03" * 100)))
        self.assertFalse(previews.is_text(self.p("model.stl", b"solid x\n")))    # has its own preview
        self.assertFalse(previews.is_text(self.p("song.mp3", b"ID3")))
        self.assertFalse(previews.is_text(os.path.join(self.d, "missing")))

    def test_reads_utf8_other_encodings_and_only_the_start(self):
        t, note = previews.text_preview(self.p("a.txt", "țară\nline 2\n".encode()))
        self.assertEqual(t, "țară\nline 2\n"); self.assertIn("2 lines", note)
        t, _ = previews.text_preview(self.p("b.txt", "café".encode("latin-1")))
        self.assertIn("caf", t)
        t, _ = previews.text_preview(self.p("c.txt", "﻿bom".encode("utf-16")))
        self.assertIn("bom", t)
        big = self.p("big.log", b"x" * 500_000)
        t, note = previews.text_preview(big)
        self.assertEqual(len(t), previews.TEXT_LIMIT); self.assertIn("showing the first", note)
        t, note = previews.text_preview(os.path.join(self.d, "nope.txt"))
        self.assertIsNone(t); self.assertIn("not found", note)

    def test_photo_is_decoded_small_and_reports_its_real_size(self):
        img = QImage(3000, 2000, QImage.Format_RGB32); img.fill(0x336699)
        path = os.path.join(self.d, "photo.jpg"); self.assertTrue(img.save(path))
        small, note = previews.preview_image(path, 420)
        self.assertEqual((small.width(), small.height()), (420, 280)); self.assertEqual(note, "3000×2000 px")
        full = previews.full_image(path)
        self.assertEqual((full.width(), full.height()), (3000, 2000))
        with mock.patch.object(previews, "FULL_PIXELS", 1_500_000):
            capped = previews.full_image(path)
        self.assertLessEqual(capped.width() * capped.height(), 1_600_000)
        self.assertIsNone(previews.full_image(os.path.join(self.d, "missing.jpg")))


class Window(unittest.TestCase):
    """The panel in the real window: text, sharp photo for zooming, sound files, full screen."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ff-win-")
        self.addCleanup(shutil.rmtree, self.d, True)
        img = QImage(2400, 1600, QImage.Format_RGB32); img.fill(0x884422)
        self.photo = os.path.join(self.d, "photo.jpg"); img.save(self.photo)
        self.txt = os.path.join(self.d, "notes.txt")
        with open(self.txt, "w", encoding="utf-8") as f:
            f.write("first line\nsecond line\n")
        self.song = os.path.join(self.d, "song.mp3")
        with open(self.song, "wb") as f:
            f.write(b"ID3" + b"\0" * 64)
        self.w = findfiles.Main(); self.w.resize(1300, 770); self.w.show(); pump(0.1)
        for t in threading.enumerate():
            if t.name == "findfiles-index":
                t.join(10)
        pump(0.1)
        now = time.time()
        self.w._fill([(p, os.path.basename(p), "Temp", os.path.getsize(p), now) for p in (self.photo, self.txt, self.song)])
        self.addCleanup(self._close)

    def _close(self):
        self.w._cols_timer.stop(); self.w.close(); pump(0.05)

    def _select(self, row, until):
        self.w.table.selectRow(row)
        end = time.time() + 8
        while time.time() < end and not until():
            pump(0.05)
        self.assertTrue(until())

    def test_text_file_shows_in_the_text_view_and_a_picture_brings_the_picture_back(self):
        self._select(1, lambda: self.w.picstack.currentWidget() is self.w.textview)
        self.assertIn("second line", self.w.textview.toPlainText())
        self.assertIn("Text,", self.w.info.text())
        self._select(0, lambda: not self.w.pic.pixmap().isNull())
        self.assertIs(self.w.picstack.currentWidget(), self.w.pic)

    def test_photo_gets_its_full_size_for_zooming(self):
        self._select(0, lambda: self.w.pic.pixmap().width() == 2400)
        self.assertFalse(self.w.pic.zoomed())
        self.w.pic.zoom_to(1.0)
        self.assertIn("Zoom 100 %", self.w.status.text())
        self.w.pic.fit()
        self.assertNotIn("Zoom", self.w.status.text())           # the earlier status line is back

    def test_sound_file_offers_play_audio(self):
        self.w.table.selectRow(2); pump(0.2)
        self.assertTrue(self.w.play_btn.isVisibleTo(self.w)); self.assertEqual(self.w.play_btn.text(), "Play audio")
        self.w.table.selectRow(1); pump(0.2)
        self.assertFalse(self.w.play_btn.isVisibleTo(self.w))

    def test_picture_full_screen_and_arrow_keys_step_through_results(self):
        self._select(0, lambda: not self.w.pic.pixmap().isNull())
        with mock.patch.object(viewer.FullImage, "showFullScreen", viewer.FullImage.show):
            self.w.picture_full_screen()
        f = self.w._fullpic
        self.assertIsNotNone(f); self.assertFalse(f.pixmap().isNull())
        f.step.emit(1); pump(0.1)
        self.assertEqual(self.w.table.currentRow(), 1)
        f.step.emit(-5); pump(0.1)
        self.assertEqual(self.w.table.currentRow(), 0)        # stops at the first result
        f.close(); pump(0.05)
        self.assertIsNone(self.w._fullpic)


class FullScreenVideo(unittest.TestCase):
    def test_whole_panel_goes_full_screen_and_comes_back_into_its_stack(self):
        import player
        stack = QStackedWidget(); other = viewer.ImageView("x"); stack.addWidget(other)
        p = player.VideoPanel(40); stack.addWidget(p); stack.setCurrentWidget(p); stack.show(); pump(0.05)
        with mock.patch.object(player.VideoPanel, "showFullScreen", player.VideoPanel.show):
            p.set_full(True)
        try:
            self.assertTrue(p.is_full()); self.assertTrue(p.isWindow())
            self.assertEqual(stack.indexOf(p), -1)
            self.assertTrue(p.bar.isVisibleTo(p))               # the controls are there in full screen
            p._hide_controls()
            self.assertFalse(p.bar.isVisibleTo(p))              # ...and hide when nothing moves
            p.view.moved.emit()
            self.assertTrue(p.bar.isVisibleTo(p))               # ...and come back with the mouse
            p._hide_controls(); p.on_key(Qt.Key_Space)
            self.assertTrue(p.bar.isVisibleTo(p))               # ...or a key
            p.on_key(Qt.Key_Escape)
            self.assertFalse(p.is_full()); self.assertFalse(p.isWindow())
            self.assertIs(stack.currentWidget(), p); self.assertTrue(p.bar.isVisibleTo(p))
            with mock.patch.object(player.VideoPanel, "showFullScreen", player.VideoPanel.show):
                p.on_key(Qt.Key_F)
            self.assertTrue(p.is_full())
            p.stop()                                            # stopping always leaves full screen
            self.assertFalse(p.is_full()); self.assertIs(stack.currentWidget(), p)
        finally:
            p.stop(); stack.close()


if __name__ == "__main__":
    unittest.main()
