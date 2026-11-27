"""Find My Files - what the preview panel shows: a picture you can zoom, or a text file.

ImageView: the wheel zooms at the pointer, dragging moves a zoomed picture, a double-click switches
between "fit" and 100 %, keys + / - / 0 / 1 do the same. With no picture it shows a short message.
TextView: the start of a text file, read-only (Ctrl + wheel changes the letter size).
Nothing here reads files or runs in the background: the window hands over pictures and text.
"""
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QFontDatabase, QPainter, QPixmap
from PySide6.QtWidgets import QPlainTextEdit, QWidget

ZOOM_STEP = 1.25
ZOOM_MAX = 16.0


class ImageView(QWidget):
    zoom_changed = Signal(str)  # '' when fitted, else e.g. '150 %'
    full_screen = Signal()      # F pressed: the window decides what full screen means

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._px, self._text = QPixmap(), text
        self._scale = None            # None = fit the panel; else screen pixels per picture pixel
        self._centre = QPointF(0, 0)  # picture point shown in the middle of the panel (when zoomed)
        self._drag = None
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(200, 200)

    # ---- what a QLabel offered (the window and the tests use these)
    def setPixmap(self, px, keep_view=False):
        """keep_view: the same picture again, only sharper - stay where the user zoomed to."""
        old = self._px
        self._px = px if px is not None else QPixmap()
        if keep_view and self._scale is not None and not old.isNull() and not self._px.isNull():
            k = self._px.width() / max(1, old.width())
            self._scale /= k
            self._centre = QPointF(self._centre.x() * k, self._centre.y() * k)
        else:
            self._scale = None
        if not self._px.isNull():
            self._text = ""
        self._changed()

    def pixmap(self):
        return self._px

    def setText(self, text):
        self._text = text
        self.update()

    def text(self):
        return self._text

    # ---- zoom
    def fit_scale(self):
        """Scale that shows the whole picture; a small picture is not blown up."""
        if self._px.isNull():
            return 1.0
        return min(1.0, self.width() / self._px.width(), self.height() / self._px.height())

    def scale(self):
        return self.fit_scale() if self._scale is None else self._scale

    def zoomed(self):
        return self._scale is not None

    def zoom_to(self, scale, at=None):
        """Zoom to `scale`; the picture point under `at` (a panel position) stays under it."""
        if self._px.isNull():
            return
        fit = self.fit_scale()
        scale = max(fit, min(ZOOM_MAX, scale))
        at = QPointF(at) if at is not None else QPointF(self.width() / 2, self.height() / 2)
        spot = self._to_picture(at)
        if scale <= fit * 1.001:
            self._scale = None
        else:
            self._scale = scale
            mid = QPointF(self.width() / 2, self.height() / 2)
            self._centre = QPointF(spot.x() - (at.x() - mid.x()) / scale, spot.y() - (at.y() - mid.y()) / scale)
            self._clamp()
        self._changed()

    def zoom_by(self, factor, at=None):
        self.zoom_to(self.scale() * factor, at)

    def fit(self):
        self._scale = None
        self._changed()

    def _changed(self):
        self.zoom_changed.emit(f"{self._scale * 100:.0f} %" if self._scale is not None else "")
        self.setCursor(Qt.OpenHandCursor if self._scale is not None else Qt.ArrowCursor)
        self.update()

    def _clamp(self):
        """A zoomed picture can't be pushed out of the panel."""
        s = self._scale
        half_w, half_h = self.width() / 2 / s, self.height() / 2 / s
        w, h = self._px.width(), self._px.height()
        x = w / 2 if w <= 2 * half_w else max(half_w, min(w - half_w, self._centre.x()))
        y = h / 2 if h <= 2 * half_h else max(half_h, min(h - half_h, self._centre.y()))
        self._centre = QPointF(x, y)

    def _target(self):
        """Where the whole picture lands in the panel (may be larger than the panel)."""
        s = self.scale()
        w, h = self._px.width() * s, self._px.height() * s
        if self._scale is None:
            return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)
        return QRectF(self.width() / 2 - self._centre.x() * s, self.height() / 2 - self._centre.y() * s, w, h)

    def _to_picture(self, pos):
        r = self._target()
        s = self.scale()
        return QPointF((pos.x() - r.x()) / s, (pos.y() - r.y()) / s)

    # ---- events
    def paintEvent(self, _e):
        p = QPainter(self)
        if self._px.isNull():
            p.setPen(self.palette().color(self.foregroundRole()))
            p.drawText(self.rect().adjusted(12, 12, -12, -12), Qt.AlignCenter | Qt.TextWordWrap, self._text)
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform, self.scale() < 3)  # big zoom: show the real pixels
        p.drawPixmap(self._target(), self._px, QRectF(self._px.rect()))

    def resizeEvent(self, e):
        if self._scale is not None:
            if self._scale <= self.fit_scale():
                self._scale = None
            else:
                self._clamp()
        super().resizeEvent(e)

    def wheelEvent(self, e):
        if self._px.isNull() or not e.angleDelta().y():
            return
        self.zoom_by(ZOOM_STEP if e.angleDelta().y() > 0 else 1 / ZOOM_STEP, e.position())
        e.accept()

    def mousePressEvent(self, e):
        self.setFocus()
        if e.button() == Qt.LeftButton and self._scale is not None:
            self._drag = (e.position(), QPointF(self._centre))
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self._drag and self._scale is not None:
            start, centre = self._drag
            d = e.position() - start
            self._centre = QPointF(centre.x() - d.x() / self._scale, centre.y() - d.y() / self._scale)
            self._clamp()
            self.update()

    def mouseReleaseEvent(self, _e):
        self._drag = None
        self.setCursor(Qt.OpenHandCursor if self._scale is not None else Qt.ArrowCursor)

    def mouseDoubleClickEvent(self, e):
        if self._px.isNull():
            return
        if self._scale is None:
            self.zoom_to(1.0 if self.fit_scale() < 0.999 else 2.0, e.position())
        else:
            self.fit()

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom_by(ZOOM_STEP)
        elif k == Qt.Key_Minus:
            self.zoom_by(1 / ZOOM_STEP)
        elif k == Qt.Key_0:
            self.fit()
        elif k == Qt.Key_1:
            self.zoom_to(1.0)
        elif k == Qt.Key_F:
            self.full_screen.emit()
        else:
            super().keyPressEvent(e)


class TextView(QPlainTextEdit):
    """The start of a text file. Read-only; Ctrl + wheel changes the letter size."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.setFrameShape(QPlainTextEdit.NoFrame)

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.zoomIn(1) if e.angleDelta().y() > 0 else self.zoomOut(1)
            e.accept()
        else:
            super().wheelEvent(e)


class FullImage(ImageView):
    """A picture on the whole screen: Esc / F leaves, the arrow keys go to the next / previous result."""
    closed = Signal()
    step = Signal(int)

    def __init__(self):
        super().__init__("")
        self.setWindowFlag(Qt.Window, True)
        self.setWindowTitle("Find My Files - picture")
        self.setAutoFillBackground(True)
        pal = self.palette(); pal.setColor(self.backgroundRole(), Qt.black); pal.setColor(self.foregroundRole(), Qt.white)
        self.setPalette(pal)

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key_Escape, Qt.Key_F, Qt.Key_F11, Qt.Key_Q):
            self.close()
        elif k in (Qt.Key_Right, Qt.Key_Down, Qt.Key_PageDown, Qt.Key_Space):
            self.step.emit(1)
        elif k in (Qt.Key_Left, Qt.Key_Up, Qt.Key_PageUp, Qt.Key_Backspace):
            self.step.emit(-1)
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        self.closed.emit()
        super().closeEvent(e)
