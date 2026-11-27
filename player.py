"""Find My Files - the video player in the preview panel.

Made the first time a video is played (QtMultimedia is not loaded before that). Nothing runs while
no video plays: stop() also lets go of the file and the decoder.
Keys (click the picture first): Space = play / pause, Left / Right = 5 s back / forward,
Up / Down = volume, M = mute, F or double-click = full screen, Esc = leave full screen.
Full screen = the whole panel (picture AND controls) fills the screen; the controls and the pointer
hide after a few seconds without movement and come back when the mouse moves or a key is pressed.
Sound files play in the same panel, with the same controls.
"""
from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSlider, QToolButton, QVBoxLayout, QWidget

STEP_MS = 5000
HIDE_MS = 2500  # full screen: controls and pointer hide after this long without movement


def clock(ms):
    """75_000 -> '1:15'; 3_700_000 -> '1:01:40'."""
    s = max(0, int(ms)) // 1000
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Screen(QVideoWidget):
    """The picture: double-click = full screen; every key and mouse movement goes to the panel."""
    key = Signal(int)
    moved = Signal()
    full_toggle = Signal()

    def __init__(self):
        super().__init__()
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)

    def mouseDoubleClickEvent(self, e):
        self.full_toggle.emit()

    def mousePressEvent(self, e):
        self.setFocus()  # (no play / pause on a click: a double-click would pause on its way to full screen)
        self.moved.emit()

    def mouseMoveEvent(self, e):
        self.moved.emit()

    def keyPressEvent(self, e):
        self.key.emit(e.key())


class VideoPanel(QWidget):
    failed = Signal(str)      # a message for the status line
    volume_set = Signal(int)  # 0-100, when the user changed it (the window remembers it)

    def __init__(self, volume=70, parent=None):
        super().__init__(parent)
        self.player, self.audio = QMediaPlayer(self), QAudioOutput(self)
        self.view = Screen()
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.view)

        self.play_btn = QToolButton(); self.play_btn.setText("Pause"); self.play_btn.setToolTip("Play / pause (Space)")
        self.seek = QSlider(Qt.Horizontal); self.seek.setRange(0, 0); self.seek.setToolTip("Drag to jump (Left / Right = 5 s)")
        self.time = QLabel("0:00 / 0:00")
        self.mute_btn = QToolButton(); self.mute_btn.setText("Sound"); self.mute_btn.setCheckable(True)
        self.mute_btn.setToolTip("Sound on / off (M)")
        self.vol = QSlider(Qt.Horizontal); self.vol.setRange(0, 100); self.vol.setFixedWidth(70)
        self.vol.setToolTip("Volume (Up / Down)")
        self.full_btn = QToolButton(); self.full_btn.setText("Full screen")
        self.full_btn.setToolTip("Full screen (F or double-click; Esc to leave)")
        self.bar = QWidget()  # a widget of its own, so full screen can hide and show it
        bar = QHBoxLayout(self.bar); bar.setContentsMargins(6, 2, 6, 2); bar.setSpacing(6)
        for w in (self.play_btn, self.seek, self.time, self.mute_btn, self.vol, self.full_btn):
            bar.addWidget(w, 1 if w is self.seek else 0)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(4)
        lay.addWidget(self.view, 1); lay.addWidget(self.bar)
        self.setMouseTracking(True); self.bar.setMouseTracking(True)
        self._home = None  # the stack this panel lives in while it is not full screen
        self._hide = QTimer(self); self._hide.setSingleShot(True); self._hide.setInterval(HIDE_MS)
        self._hide.timeout.connect(self._hide_controls)

        self._dragging, self._path = False, ""
        self.vol.setValue(max(0, min(100, int(volume))))
        self.audio.setVolume(self.vol.value() / 100)
        self.play_btn.clicked.connect(self.toggle)
        self.mute_btn.toggled.connect(self._mute)
        self.vol.valueChanged.connect(self._volume)
        self.full_btn.clicked.connect(lambda: self.set_full(not self.is_full()))
        self.view.full_toggle.connect(lambda: self.set_full(not self.is_full()))
        self.view.moved.connect(self._show_controls)
        self.seek.sliderPressed.connect(lambda: setattr(self, "_dragging", True))
        self.seek.sliderReleased.connect(self._seek_done)
        self.seek.sliderMoved.connect(lambda v: self.time.setText(f"{clock(v)} / {clock(self.player.duration())}"))
        self.view.key.connect(self.on_key)
        self.player.durationChanged.connect(lambda d: self.seek.setRange(0, max(0, d)))
        self.player.positionChanged.connect(self._position)
        self.player.playbackStateChanged.connect(self._state)
        self.player.mediaStatusChanged.connect(self._status)
        self.player.errorOccurred.connect(lambda _e, msg: self.failed.emit(msg or "this video can't be played"))

    # ---- what the window calls
    def play(self, path):
        self._path = path
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()
        self.view.setFocus()

    def stop(self):
        """Stop and let go of the file (no decoder, no open file, no CPU while idle)."""
        self.set_full(False)
        self.player.stop()
        self.player.setSource(QUrl())

    def playing(self):
        return self.player.playbackState() != QMediaPlayer.PlaybackState.StoppedState

    # ---- controls
    def toggle(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            if self.player.source().isEmpty() and self._path:  # it had finished: open it again
                self.player.setSource(QUrl.fromLocalFile(self._path))
            self.player.play()

    def jump(self, ms):
        self.player.setPosition(max(0, min(self.player.duration(), self.player.position() + ms)))

    # ---- full screen: the whole panel, controls included
    def is_full(self):
        return self._home is not None

    def set_full(self, on):
        if on == self.is_full():
            return
        if on:
            self._home = self.parentWidget()
            self.setParent(None)                      # out of the window: a screen of its own
            self.setWindowFlag(Qt.Window, True)
            self.setWindowTitle("Find My Files - video")
            self.showFullScreen()
            self.full_btn.setText("Leave full screen")
            self._show_controls()
        else:
            home, self._home = self._home, None
            self._hide.stop()
            self.bar.show(); self.unsetCursor(); self.view.unsetCursor()
            self.full_btn.setText("Full screen")
            self.setWindowFlag(Qt.Window, False)
            if home is not None:
                if hasattr(home, "addWidget"):       # back into the preview panel's stack
                    home.addWidget(self)
                    home.setCurrentWidget(self)
                else:
                    self.setParent(home)
            self.showNormal()
        self.view.setFocus()

    def _show_controls(self):
        if not self.is_full():
            return
        self.bar.show(); self.unsetCursor(); self.view.unsetCursor()
        self._hide.start()

    def _hide_controls(self):
        if self.is_full() and not self._dragging and not self.bar.underMouse():
            self.bar.hide()
            self.setCursor(Qt.BlankCursor); self.view.setCursor(Qt.BlankCursor)

    def mouseMoveEvent(self, e):
        self._show_controls()

    def closeEvent(self, e):
        if self.is_full():      # closing the full-screen window = back into the app, not gone
            e.ignore()
            self.set_full(False)
        else:
            super().closeEvent(e)

    def on_key(self, key):
        self._show_controls()
        if key == Qt.Key_Escape:
            self.set_full(False)
        elif key == Qt.Key_F:
            self.set_full(not self.is_full())
        elif key == Qt.Key_Space:
            self.toggle()
        elif key == Qt.Key_Left:
            self.jump(-STEP_MS)
        elif key == Qt.Key_Right:
            self.jump(STEP_MS)
        elif key == Qt.Key_Up:
            self.vol.setValue(self.vol.value() + 5)
        elif key == Qt.Key_Down:
            self.vol.setValue(self.vol.value() - 5)
        elif key == Qt.Key_M:
            self.mute_btn.toggle()

    def keyPressEvent(self, e):
        self.on_key(e.key())

    def _mute(self, on):
        self.audio.setMuted(on)
        self.mute_btn.setText("Muted" if on else "Sound")

    def _volume(self, v):
        self.audio.setVolume(v / 100)
        if self.mute_btn.isChecked() and v:
            self.mute_btn.setChecked(False)
        self.volume_set.emit(v)

    def _seek_done(self):
        self._dragging = False
        self.player.setPosition(self.seek.value())

    def _position(self, pos):
        if not self._dragging:
            self.seek.setValue(pos)
            self.time.setText(f"{clock(pos)} / {clock(self.player.duration())}")

    def _state(self, state):
        self.play_btn.setText("Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "Play")

    def _status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:  # finished: let go of the file; Play opens it again
            self.player.stop()
            self.player.setSource(QUrl())
            self.seek.setValue(0)
