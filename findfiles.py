#!/usr/bin/env python3
"""Find My Files - search every work drive at once (the window).

Type a few words (Romanian or English, with or without diacritics):
    cruce 4-1        ->  "cruce 4-1 baza2 (repaired).stl"
    cross celtic     ->  "corp cruce celtica ..."
Double-click a result to open its folder in Dolphin with the file selected.

Read-only: this app never moves, renames or deletes files.
See docs/DEVELOPER.md for how it's built.
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QFileSystemWatcher, QMimeData, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDrag, QFont, QIcon, QPainter, QPixmap, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QInputDialog, QFrame, 
    QApplication, QButtonGroup, QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListView,
    QListWidget, QListWidgetItem, QMainWindow, QPushButton, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView, QAbstractItemView, QMessageBox,
    QDialog, QDialogButtonBox, QMenu, QFileDialog, QCompleter,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import archives  # noqa: E402
import roots  # noqa: E402
import previews as _previews  # noqa: E402  (video file types)
from version import VERSION as APP_VERSION  # noqa: E402
import copies  # noqa: E402
import filetypes  # noqa: E402
import indexer  # noqa: E402
import previews  # noqa: E402
import programs  # noqa: E402
import search  # noqa: E402
import thumbs  # noqa: E402
import viewer  # noqa: E402

PREVIEW_SIZE = 420
PREVIEW_MAX = 900    # previews are drawn for the panel's size, up to this (bigger = slower for 3D models)
WATCH_DELAY_MS = 3000  # collect a burst of saves (a slicer writing 20 files) into one update
BUILDER_DELAY_MS = 30_000  # pictures for watch-mode arrivals: one builder run per half minute of changes
TILE = 160
COLUMNS = ["Name", "Type", "Where", "Size", "Date", "Model size", "Folder", "Ext", "Drive"]
EXTRA_COLS = (6, 7, 8)  # hidden until switched on from the column titles' right-click menu
SORT_WORDS = {3: ("smallest first", "largest first"), 4: ("oldest first", "newest first"),
              5: ("smallest first", "largest first")}
MAX_COL = 150   # widest the Type / Where columns get on their own (drag to widen): the name matters most
VERSION = Qt.UserRole + 1   # (size, mtime) of the file as listed = its picture cache key
FILTERS = filetypes.filter_options()
TYPE_SEP = " › "  # "Text & documents › PDF": one file ending inside a group (the Type menu's submenus)


def type_exts(name):
    """Endings for a Type choice: a group name, or 'Group › EXT' for one ending (None = no filter).
    An unknown name falls back to the default group."""
    if TYPE_SEP in name:
        group, ext = name.split(TYPE_SEP, 1)
        ext = ext.strip().lower()
        if group in FILTERS and FILTERS[group] and ext in FILTERS[group]:
            return [ext]
    return FILTERS.get(name, FILTERS["All my work"])


def valid_type(name):
    if not isinstance(name, str):
        return False
    if TYPE_SEP in name:
        group, ext = name.split(TYPE_SEP, 1)
        return group in FILTERS and bool(FILTERS[group]) and ext.strip().lower() in FILTERS[group]
    return name in FILTERS


def _amp(text):
    """Menus and buttons read '&' as a keyboard-shortcut mark ("Text & documents" showed "Text  documents")."""
    return text.replace("&", "&&")


def watch_room():
    """How many folders the live watch may use: half of what this PC allows (other programs, e.g. the
    file manager, need watches too; older setups allow only 8,192)."""
    try:
        with open("/proc/sys/fs/inotify/max_user_watches") as f:
            return int(f.read()) // 2
    except (OSError, ValueError):
        return 4096


class TypePicker(QPushButton):
    """The Type box: plain entries for "All my work" / "Everything", a submenu per group with
    "All <group>" plus each ending that has files and how many (counted when the menu opens).
    Keeps the few QComboBox calls the window and the tests use."""
    currentIndexChanged = Signal(int)

    def __init__(self, count_fn, parent=None):
        super().__init__(parent)
        self._count_fn = count_fn      # () -> {ext: files} for the current "Include backups" choice
        self._name = "All my work"
        self.setText(_amp(self._name))
        self.setMenu(QMenu(self))
        self.menu().aboutToShow.connect(self._fill)

    def currentText(self):
        return self._name

    def setCurrentText(self, name):
        if not valid_type(name) or name == self._name:
            return
        self._name = name
        self.setText(_amp(name))
        self.currentIndexChanged.emit(0)

    def _fill(self):
        m = self.menu(); m.clear()
        try:
            counts = self._count_fn() or {}
        except sqlite3.Error:
            counts = {}
        for name, exts in FILTERS.items():
            if not exts or name == "All my work":
                a = m.addAction(_amp(name)); a.setCheckable(True); a.setChecked(name == self._name)
                a.triggered.connect(lambda _=False, n=name: self.setCurrentText(n))
                continue
            sub = m.addMenu(_amp(f"{name}  ({sum(counts.get(e, 0) for e in exts):,})"))
            a = sub.addAction(_amp(f"All {name[0].lower() + name[1:] if name[1:2].islower() else name}"))
            a.setCheckable(True); a.setChecked(name == self._name)
            a.triggered.connect(lambda _=False, n=name: self.setCurrentText(n))
            have = sorted((e for e in exts if counts.get(e)), key=lambda e: (-counts[e], e))
            if have:
                sub.addSeparator()
            for e in have:
                full = f"{name}{TYPE_SEP}{e.upper()}"
                a = sub.addAction(f"{e.upper()}  ({counts[e]:,})")
                a.setCheckable(True); a.setChecked(full == self._name)
                a.triggered.connect(lambda _=False, n=full: self.setCurrentText(n))


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def mm_text(dims):
    """(60.0, 40.2, 12.0) -> '60 × 40 × 12 mm' (one decimal under 10 mm)."""
    return " × ".join(f"{v:.1f}" if v < 10 else f"{v:.0f}" for v in dims) + " mm"


class Bus(QObject):
    indexed = Signal(int, list)
    copies_done = Signal(str, object)
    copies_progress = Signal(int)
    preview = Signal(str, object, str)
    preview_full = Signal(str, object)        # a photo decoded large enough to zoom into
    text = Signal(str, object, str)           # path, text (None = not readable), note
    suggest = Signal(int, str, object, object)  # request number, search, suggestion (None = none), files / reason
    watched = Signal(int, int, list, bool)
    extracted = Signal(str, str, str, object)  # member path, temp file ('' = failed), reason, then-callback
    drives = Signal(list)                     # [(name, mounted, files, tooltip)] for the drive chips
    pictures = Signal(int, int)               # pictures made, pictures wanted
    statuses = Signal(list)                   # Settings: 'Now' column, checked off the UI thread
    scan = Signal(int, str)                   # files found so far, location being read (progress / time left)


ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons", "findfiles.svg")
RECENT_MAX = 15

# Cheeky look: rounded search box, soft buttons. Colours come from the system palette (light or dark).
STYLE = """
QLineEdit#search { border: 1px solid @line; border-radius: 15px; padding: 6px 14px; }
QLineEdit#search:focus { border: 2px solid palette(highlight); padding: 5px 13px; }
QPushButton { border: 1px solid @line; border-radius: 7px; padding: 5px 11px;
              background: @btn; color: palette(button-text); }
QPushButton:hover { background: @hover; border-color: palette(highlight); }
QPushButton:pressed { background: @press; }
QPushButton:focus { border-color: palette(highlight); }
QPushButton:disabled { color: @dim; background: transparent; }
QPushButton:checked { background: palette(highlight); color: palette(highlighted-text);
                      border-color: palette(highlight); }
QPushButton#primary { background: palette(highlight); color: palette(highlighted-text); font-weight: 600;
                      border-color: palette(highlight); }
QPushButton#primary:hover { border-color: palette(highlighted-text); }
QPushButton#segl { border-top-right-radius: 0; border-bottom-right-radius: 0; }
QPushButton#segr { border-top-left-radius: 0; border-bottom-left-radius: 0; border-left: none; }
QTableWidget#results, QListWidget#results { border: 1px solid @line; border-radius: 8px; }
QTableWidget#results::item { padding: 0 6px; }
QFrame#card { border: 1px solid @line; border-radius: 10px; background: palette(base); }
QLabel#info { padding: 2px 4px; }
QLabel#chip { border: 1px solid @line; border-radius: 9px; padding: 1px 8px; background: @btn; }
QLabel#chipoff { border: 1px dashed @line; border-radius: 9px; padding: 1px 8px; color: @dim; }
QLabel#chip:hover, QLabel#chipoff:hover { border-color: palette(highlight); }
QLabel#chipon { border: 1px solid palette(highlight); border-radius: 9px; padding: 1px 8px;
                background: palette(highlight); color: palette(highlighted-text); }
QLabel#dim { color: @dim; }
"""


def _mix(a, b, t):
    """Colour t of the way from b to a (0 = b, 1 = a), as #rrggbb."""
    return "#%02x%02x%02x" % tuple(round(x * t + y * (1 - t)) for x, y in
                                   ((a.red(), b.red()), (a.green(), b.green()), (a.blue(), b.blue())))


def style(pal):
    """The style sheet with outline/hover colours worked out from the desktop's own colours, so
    buttons and frames stay visible in a dark theme (there the theme's "mid" grey is nearly the
    window colour, and a styled button draws no frame of its own)."""
    text, win, btn = pal.color(pal.ColorRole.WindowText), pal.color(pal.ColorRole.Window), pal.color(pal.ColorRole.Button)
    out = STYLE
    for token, colour in (("@line", _mix(text, win, 0.30)), ("@hover", _mix(text, btn, 0.10)),
                          ("@press", _mix(text, btn, 0.20)), ("@dim", _mix(text, win, 0.55)),
                          ("@btn", btn.name())):
        out = out.replace(token, colour)
    return out


def eta_text(found, expected, elapsed, where=""):
    """'X › work stuff · 120,000 of about 300,000 files (40 %) · about 2 min left'.
    expected = files in the last list (0 = first scan: no estimate, never a guess)."""
    head = f"{where} · " if where else ""
    if expected <= 0 or found <= 0 or elapsed < 3:
        return f"{head}{found:,} files so far"
    frac = min(found / expected, 0.99)
    left = elapsed / frac - elapsed
    if left < 60:
        when = "less than a minute left"
    elif left < 3600:
        when = f"about {round(left / 60)} min left"
    else:
        when = f"about {left / 3600:.1f} h left"
    return f"{head}{found:,} of about {expected:,} files ({frac * 100:.0f} %) · {when} (estimate)"


def show_in_file_manager(path):
    """Open the file's folder with the file selected: Dolphin when it is there, else whatever file
    manager answers the desktop's standard "show items" call, else just the folder."""
    if shutil.which("dolphin"):
        subprocess.Popen(["dolphin", "--select", path], start_new_session=True)
        return "dolphin"
    if shutil.which("dbus-send"):
        try:
            done = subprocess.run(
                ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.FileManager1",
                 "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems",
                 "array:string:" + QUrl.fromLocalFile(path).toString(), "string:"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if done.returncode == 0:
                return "dbus"
        except (OSError, subprocess.TimeoutExpired):
            pass
    subprocess.Popen(["xdg-open", os.path.dirname(path)], start_new_session=True)
    return "xdg-open"


def drive_of(root):
    """Mount point a location lives on: /mnt/<d>, /run/media/<u>/<d>, or the home folder."""
    parts = root.split("/")
    if root.startswith("/run/media/") and len(parts) >= 5:
        return "/".join(parts[:5])
    if root.startswith("/mnt/") and len(parts) >= 3:
        return "/".join(parts[:3])
    return os.path.expanduser("~")


def disk_label(mount):
    """The drive's own name (its filesystem label, e.g. 'DATA') or ''. Read from
    /proc/self/mountinfo and /dev/disk/by-label only: the drive itself is never touched."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as f:
            src = next((ln.split(" - ")[1].split()[1] for ln in f
                        if len(ln.split()) > 4 and ln.split()[4].replace("\\040", " ") == mount and " - " in ln), "")
        if not src.startswith("/dev/"):
            return ""
        src = os.path.realpath(src)
        by = "/dev/disk/by-label"
        for name in os.listdir(by):
            if os.path.realpath(os.path.join(by, name)) == src:
                return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), name)  # 'my\\x20disk' -> 'my disk'
    except (OSError, IndexError, UnicodeError):
        pass
    return ""


def drive_name(label, mount):
    """'X › work stuff' -> 'X'; home -> 'Home'; otherwise the drive's own label, then its folder."""
    if mount == os.path.expanduser("~"):
        return "Home"
    if " › " in label:
        return label.split(" › ")[0].strip() or os.path.basename(mount)
    for tail in (" (root)",):  # 'X (root)' -> 'X'
        if label.endswith(tail) and label[:-len(tail)].strip():
            return label[:-len(tail)].strip()
    return disk_label(mount) or os.path.basename(mount) or label.strip()


def drive_summary(counts):
    """[(name, mounted, files, tooltip)] from {label: files} and the current folder list."""
    groups = {}
    for r, label, _kind in indexer.ROOTS + indexer._loose():
        m = drive_of(r)
        g = groups.setdefault(m, {"name": drive_name(label, m), "labels": [], "files": 0})
        if " › " in label:  # the user's own short name for the drive wins
            g["name"] = drive_name(label, m)
        g["labels"].append(label)
        g["files"] += counts.get(label, 0)
    out = []
    for m, g in groups.items():
        mounted = m == os.path.expanduser("~") or os.path.ismount(m)
        tip = f"{m}\n" + "\n".join(sorted(g["labels"]))
        out.append((g["name"], mounted, g["files"], tip))
    names = [o[0] for o in out]  # two drives with the same name: add the mount folder
    out = [(n if names.count(n) == 1 else f"{n} ({os.path.basename(t.split(chr(10))[0])})", m, f, t)
           for n, m, f, t in out]
    return sorted(out, key=lambda x: (x[0] != "Home", x[0].lower()))


FULL_TIP = ("Read every searched folder again and make any missing pictures (F5).\n"
            "Uses the drives and some CPU for a while, so it only runs when you press it.\n"
            "Opening the app only checks which folders changed, which is quick.\n"
            "Press this if a file is missing: the quick check can't see a file saved over itself,\n"
            "or files copied in by a tool that keeps the folder's old date.")


def ago(t):
    """'3 days ago' / 'today' for a time stamp (0 = never)."""
    if not t:
        return "never"
    days = int((time.time() - t) // 86400)
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


def drive_counts():
    """{location label: files} from the current file list ({} when there is none yet)."""
    try:
        db = indexer.connect_ro()
        try:
            return dict(db.execute("SELECT loc, COUNT(*) FROM files GROUP BY loc").fetchall())
        finally:
            db.close()
    except sqlite3.Error:
        return {}


def _clean_old_handoffs(max_age=86400):
    """Temp copies left by a crash (findfiles-* folders in the temp dir, older than a day)."""
    import glob
    import tempfile
    for d in glob.glob(os.path.join(tempfile.gettempdir(), "findfiles-*")):
        try:
            if os.path.isdir(d) and time.time() - os.path.getmtime(d) > max_age:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def handoff_mime(path):
    """What a drag carries: the file as a URL (programs, Dolphin, browsers understand it)."""
    m = QMimeData()
    m.setUrls([QUrl.fromLocalFile(path)])
    return m


class _DragOut:
    """Drag a result out of the window into a slicer, Blender, Dolphin... COPY only: the original
    is never moved (read-only rule)."""

    def startDrag(self, actions):
        win = self.window()
        path = win.handoff_path() if hasattr(win, "handoff_path") else None
        if not path:
            return
        drag = QDrag(self)
        drag.setMimeData(handoff_mime(path))
        drag.exec(Qt.CopyAction, Qt.CopyAction)


class ResultTable(_DragOut, QTableWidget):
    resized = Signal()

    _last_w = -1

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # only the table's own width counts: this event also fires when a scrollbar comes or goes,
        # and refitting columns on that could make the bars flip back and forth for ever
        if self.width() != self._last_w:
            self._last_w = self.width()
            self.resized.emit()

    def room(self):
        """Width the columns may use: the same whether or not a scrollbar is showing right now."""
        return max(0, self.width() - 2 * self.frameWidth() - self.verticalScrollBar().sizeHint().width())


class ResultGrid(_DragOut, QListWidget):
    pass


class ProgramPicker(QDialog):
    """Tick the programs to offer for one file type; order = the order in the list (drag to sort)."""

    def __init__(self, parent, ext, apps, offered):
        super().__init__(parent)
        self.setWindowTitle(f"Programs for .{ext} files")
        self.resize(460, 560)
        top = QLabel(f"Tick the programs to show under “Open with” for <b>.{ext}</b> files. "
                     "Drag to change the order.")
        top.setWordWrap(True)
        self.filter = QLineEdit(placeholderText="Filter…")
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        first = [a.id for a in offered]
        rest = sorted((a for a in apps if a.id not in first), key=lambda a: (a.hidden, a.label.lower()))
        by_id = {a.id: a for a in apps}
        for a in [by_id[i] for i in first if i in by_id] + rest:
            it = QListWidgetItem(a.label)
            it.setData(Qt.UserRole, a.id)
            it.setToolTip(f"{a.exec}\n{a.path}")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if a.id in first else Qt.Unchecked)
            self.list.addItem(it)
        self.filter.textChanged.connect(self._filter)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        for wdg in (top, self.filter, self.list, bb):
            lay.addWidget(wdg)

    def _filter(self, text):
        t = text.lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(t) and t not in it.text().lower())

    def picked(self):
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]


class SortItem(QTableWidgetItem):
    """Table cell that sorts by a hidden key (bytes, timestamp) instead of its text."""
    def __init__(self, text, key):
        super().__init__(text)
        self.key = key

    def __lt__(self, other):
        return self.key < getattr(other, "key", 0)


def space_used():
    """(file list bytes, picture bytes, pictures) this app keeps on the system disk."""
    lists = 0
    try:
        with os.scandir(indexer.DB_DIR) as it:
            lists = sum(e.stat().st_size for e in it if e.is_file())
    except OSError:
        pass
    pics, n = thumbs.disk_use()
    return lists, pics, n


class SettingsDialog(QDialog):
    """Which folders to search (tick / add / remove, Work or Copy), plus Rebuild from scratch.
    Never touches the user's files: it only edits the folder list (~/.config/findfiles/roots.json)."""

    COLS = ("Search", "Folder", "Name in the app", "Kind", "Subfolders", "Now")

    def __init__(self, parent, settings, problem=""):
        super().__init__(parent)
        self.setWindowTitle(f"Find My Files {APP_VERSION} – Settings")
        self.resize(1100, 680)
        self.never = list(settings["never"])
        self.skip = list(settings.get("skip", roots.DEFAULT_SKIP))
        self.low = list(settings.get("low", roots.DEFAULT_LOW))
        self.action, self.redraw, self.result_settings = "save", False, None
        self.history_cleared = False
        self._bus = Bus(self)
        self._bus.statuses.connect(self._on_statuses)
        top = QLabel("<b>Folders to search.</b> Add a whole drive or single folders. A folder inside another "
                     "keeps its own name; untick a folder to leave it out. <i>Copy</i> folders "
                     "(backups, hold folders) only show up with “Include backups &amp; trash”. "
                     "Your files are never changed — this only edits the app's folder list.")
        top.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.addWidget(top)
        if problem:
            warn = QLabel("⚠ " + problem); warn.setWordWrap(True); warn.setStyleSheet("color: #b35c00;")
            lay.addWidget(warn)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().hide()
        # rows tall enough for the Kind box in any desktop theme (Breeze boxes are taller than a text row)
        self.table.verticalHeader().setDefaultSectionSize(max(34, QComboBox().sizeHint().height() + 6))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (0, 3, 4, 5):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.setColumnWidth(2, 240)
        for r in settings["roots"]:
            self._add_row(r)
        self.table.setCurrentItem(None)  # no stray focus box on the first cell when the window opens
        self._check_status()
        lay.addWidget(self.table, 1)
        never = QLabel("Never searched (refused if added): " + (", ".join(self.never) or "nothing") +
                       "   ·   Steam / game-prefix folders and system folders are always refused.")
        never.setWordWrap(True); never.setStyleSheet("color: gray;")
        lay.addWidget(never)
        skip_row = QHBoxLayout()
        self.skip_lbl = QLabel(); self.skip_lbl.setWordWrap(True); self.skip_lbl.setStyleSheet("color: gray;")
        skip_btn = QPushButton("Edit skipped names…"); skip_btn.clicked.connect(self.edit_skip)
        skip_btn.setToolTip("Folder names that are never read, wherever they are (games, program folders, assets)")
        skip_row.addWidget(self.skip_lbl, 1); skip_row.addWidget(skip_btn)
        lay.addLayout(skip_row)
        low_row = QHBoxLayout()
        self.low_lbl = QLabel(); self.low_lbl.setWordWrap(True); self.low_lbl.setStyleSheet("color: gray;")
        low_btn = QPushButton("Edit low-priority words…"); low_btn.clicked.connect(self.edit_low)
        low_btn.setToolTip("A folder whose name in the app contains one of these words (software, music, archives)\n"
                           "ranks last in results, is not watched live, and archives inside it are not opened")
        low_row.addWidget(self.low_lbl, 1); low_row.addWidget(low_btn)
        lay.addLayout(low_row)
        self.note = QLabel(""); self.note.setWordWrap(True)
        lay.addWidget(self.note)
        self._show_skip()
        self.space = QLabel(self.space_text()); self.space.setStyleSheet("color: gray;")
        self.space.setToolTip("Pictures are small previews for the grid and the size search. "
                              "Rebuild from scratch can clear and redraw them.")
        lay.addWidget(self.space)
        row = QHBoxLayout()
        add = QPushButton("Add folder…"); add.clicked.connect(self.add_folder)
        rem = QPushButton("Remove"); rem.clicked.connect(self.remove_selected)
        rem.setToolTip("Take the selected folders off the list (the folders themselves are not touched)")
        reb = QPushButton("Rebuild from scratch…"); reb.clicked.connect(self.rebuild)
        reb.setToolTip("Read every folder and archive again and write a fresh file list")
        hist = QPushButton("Clear search history"); hist.clicked.connect(self.clear_history)
        hist.setToolTip("Forget the recent searches and the last search / view the window reopens with")
        row.addWidget(add); row.addWidget(rem); row.addStretch(1); row.addWidget(hist); row.addWidget(reb)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("Save && rescan")
        bb.accepted.connect(self.save); bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        ver = QLabel(f"Find My Files {APP_VERSION}  ·  settings file: {roots.PATH}")
        ver.setStyleSheet("color: gray;"); ver.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(ver)

    # -- rows
    def _add_row(self, r):
        i = self.table.rowCount()
        self.table.insertRow(i)
        on = QTableWidgetItem(); on.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        on.setCheckState(Qt.Checked if r["enabled"] else Qt.Unchecked)
        self.table.setItem(i, 0, on)
        path = QTableWidgetItem(r["path"]); path.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        path.setToolTip(r["path"])
        self.table.setItem(i, 1, path)
        self.table.setItem(i, 2, QTableWidgetItem(r["label"]))
        kind = QComboBox(); kind.addItems(["Work", "Copy"]); kind.setCurrentIndex(1 if r["kind"] == "copy" else 0)
        self.table.setCellWidget(i, 3, kind)
        sub = QTableWidgetItem(); sub.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        sub.setCheckState(Qt.Checked if r["recurse"] else Qt.Unchecked)
        sub.setToolTip("Unticked = only the files lying directly in this folder")
        self.table.setItem(i, 4, sub)
        now = QTableWidgetItem("checking…")  # filled from a thread: a sleeping HDD must not freeze the window
        now.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.table.setItem(i, 5, now)

    def _check_status(self):
        paths = [self.table.item(i, 1).text() for i in range(self.table.rowCount())]
        bus = self._bus

        def job():
            found = [(p, os.path.isdir(p)) for p in paths]
            try:
                bus.statuses.emit(found)
            except RuntimeError:
                pass  # dialog closed meanwhile
        threading.Thread(target=job, daemon=True).start()

    def _on_statuses(self, found):
        here = dict(found)
        for i in range(self.table.rowCount()):
            p = self.table.item(i, 1).text()
            if p in here:
                it = self.table.item(i, 5)
                it.setText("OK" if here[p] else "not there / unplugged")
                it.setToolTip("" if here[p] else "Its last known files stay searchable until the drive is back")

    def clear_history(self):
        programs.set_state("recent", [])
        programs.set_state("last_view", None)
        self.history_cleared = True
        QMessageBox.information(self, "Search history", "Recent searches and the last view are forgotten.")

    def rows(self):
        out = []
        for i in range(self.table.rowCount()):
            out.append({"path": self.table.item(i, 1).text(),
                        "label": self.table.item(i, 2).text().strip(),
                        "kind": "copy" if self.table.cellWidget(i, 3).currentIndex() == 1 else "work",
                        "recurse": self.table.item(i, 4).checkState() == Qt.Checked,
                        "enabled": self.table.item(i, 0).checkState() == Qt.Checked})
        return out

    def _show_skip(self):
        self.skip_lbl.setText("Folder names skipped everywhere: " + (", ".join(self.skip) or "none"))
        self.low_lbl.setText("Low-priority words (in a folder's name in the app): " + (", ".join(self.low) or "none"))

    def edit_low(self):
        text, ok = QInputDialog.getText(
            self, "Low-priority words",
            "A folder whose “Name in the app” contains one of these words ranks last in results,\n"
            "is not watched live, and archives inside it are not opened. Separate words with commas:",
            text=", ".join(self.low))
        if ok:
            self.low = list(dict.fromkeys(n.strip() for n in text.split(",") if n.strip()))
            self._show_skip()

    def edit_skip(self):
        text, ok = QInputDialog.getText(
            self, "Skipped folder names",
            "Folders with these names are never read, wherever they are (case does not matter).\n"
            "Separate names with commas:", text=", ".join(self.skip))
        if ok:
            self.skip = list(dict.fromkeys(n.strip() for n in text.split(",") if n.strip() and "/" not in n))
            self._show_skip()

    def space_text(self):
        lists, pics, n = space_used()
        return (f"Space used on the system disk: file list {human(lists)}  ·  pictures {human(pics)} ({n:,})")

    def add_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Add a folder to search", roots.HOME)
        if not path:
            return
        ok, why = roots.check(path, self.never, self.rows())
        if not ok:
            QMessageBox.warning(self, "Can't add this folder", why)
            return
        note = roots.overlaps(path, self.rows())
        if roots.is_whole_drive(path):
            note = ("That is a whole drive: everything on it is searched, except the skipped folder names "
                    "(games, program folders, assets…) shown below the list.  " + note)
        if note:
            self.note.setText(note)
        self._add_row({"path": os.path.normpath(path), "label": roots.label_for(path, self.rows()),
                       "kind": "work", "recurse": True, "enabled": True})
        self._check_status()
        self.table.scrollToBottom()

    def remove_selected(self):
        for i in sorted({ix.row() for ix in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(i)

    # -- finish
    def _valid(self):
        labels = [r["label"] for r in self.rows()]
        if any(not l for l in labels):
            QMessageBox.warning(self, "Settings", "Every folder needs a name.")
            return False
        dup = {l for l in labels if labels.count(l) > 1}
        if dup:
            QMessageBox.warning(self, "Settings", "Two folders have the same name: " + ", ".join(sorted(dup)))
            return False
        paths = [os.path.normpath(r["path"]) for r in self.rows()]
        if len(set(paths)) != len(paths):
            QMessageBox.warning(self, "Settings", "The same folder is in the list twice.")
            return False
        return True

    def save(self):
        if self._valid():
            self.result_settings = {"roots": self.rows(), "never": self.never, "skip": self.skip, "low": self.low}
            self.accept()

    def rebuild(self):
        if not self._valid():
            return
        box = QMessageBox(QMessageBox.Question, "Rebuild from scratch",
                          "Read every folder and archive again and write a fresh file list?\n\n"
                          "Your files are not touched. Search keeps working meanwhile. "
                          "Drives that are unplugged keep their last known files.",
                          QMessageBox.Yes | QMessageBox.Cancel, self)
        pics = QCheckBox("Also redraw all pictures (slow; they come back in the background)")
        box.setCheckBox(pics)
        if box.exec() != QMessageBox.Yes:
            return
        self.action, self.redraw = "rebuild", pics.isChecked()
        self.result_settings = {"roots": self.rows(), "never": self.never, "skip": self.skip, "low": self.low}
        self.accept()


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Find My Files {APP_VERSION}")
        self.setWindowIcon(QIcon(ICON_PATH))
        self._style = style(self.palette())
        self.setStyleSheet(self._style)
        self._again = None           # None = no rescan queued; False/True = one more (forced) rescan
        self.resize(1300, 740)
        self.bus = Bus()
        self.bus.indexed.connect(self.on_indexed)
        self.bus.preview.connect(self.on_preview)
        self.bus.preview_full.connect(self.on_preview_full)
        self.bus.text.connect(self.on_text)
        self.bus.suggest.connect(self.on_suggest)
        self.bus.copies_done.connect(self.on_copies)
        self.bus.copies_progress.connect(
            lambda mb: self.status.setText(f"Comparing whole files… {mb:,} MB read"))
        self._index_msg = ""      # last "file list" message, kept visible under search results
        self._mode = "search"     # "search" or "copies" (showing exact copies of one file)
        self._busy = False
        self.current_preview = None
        self._want = None                      # newest preview request
        self._wake = threading.Event()
        threading.Thread(target=self._preview_worker, daemon=True).start()
        self._sort = (-1, Qt.AscendingOrder)

        # --- top bar
        self.q = QLineEdit(placeholderText="Type a few words of the file name…   ·   60mm = models about that size")
        f = self.q.font(); f.setPointSize(f.pointSize() + 3); self.q.setFont(f)
        self.q.setObjectName("search"); self.q.setClearButtonEnabled(True)
        self.type = TypePicker(self._type_counts)
        self.type.setToolTip("Which kinds of files. Each group opens a list of its file endings with how many\n"
                             "you have; pick one to see only that kind. All my work = 3D, print, 2D/vector, laser, scans")
        self.copies = QCheckBox("Include backups && trash")
        self.timer = QTimer(singleShot=True, interval=220, timeout=self.run_search)
        self.q.textChanged.connect(lambda: self.timer.start())
        self.type.currentIndexChanged.connect(lambda *_: self.run_search())  # (signals pass an int)
        self.copies.stateChanged.connect(lambda *_: self.run_search())
        self.view_list = QPushButton("List", checkable=True, checked=True)
        self.view_grid = QPushButton("Grid", checkable=True)
        self.new_btn = QPushButton("New", checkable=True)
        self.new_btn.setToolTip(f"Files that arrived in the last {search.NEW_DAYS} days, newest first. "
                                "Type words to narrow it down.")
        self.new_btn.toggled.connect(self._toggle_new)
        self.view_grid.setToolTip("Pictures of the results (Ctrl+G switches)")
        grp = QButtonGroup(self); grp.addButton(self.view_list); grp.addButton(self.view_grid)
        self.view_list.clicked.connect(lambda: self.set_view("list"))
        self.view_grid.clicked.connect(lambda: self.set_view("grid"))
        top = QHBoxLayout()
        top.addWidget(self.q, 1); top.addWidget(self.type); top.addWidget(self.copies)
        self.view_list.setObjectName("segl"); self.view_grid.setObjectName("segr")
        seg = QHBoxLayout(); seg.setSpacing(1); seg.addWidget(self.view_list); seg.addWidget(self.view_grid)
        top.addWidget(self.new_btn); top.addLayout(seg)
        self.rescan_btn = QPushButton("Full index")
        self.rescan_btn.setToolTip(FULL_TIP)
        self.rescan_btn.clicked.connect(lambda: self.refresh_index())
        self.settings_btn = QPushButton("Settings")
        self.settings_btn.setToolTip("Choose which folders to search; rebuild the file list; clear history")
        self.settings_btn.clicked.connect(self.open_settings)
        top.addWidget(self.rescan_btn); top.addWidget(self.settings_btn)

        # --- results table
        self.table = ResultTable(0, len(COLUMNS)); self.table.setObjectName("results")
        self.table.setShowGrid(False); self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeaderItem(5).setToolTip("Width × depth × height (from the saved picture). "
                                                      "Type e.g. 60mm to find models of that size.")
        h = self.table.horizontalHeader()
        # Every column can be dragged wider/narrower and dragged to another place. Until one is dragged the
        # widths set themselves (Name takes what is left); right-click the titles to hide/show/sort/reset.
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setStretchLastSection(False); h.setSectionsMovable(True); h.setMinimumSectionSize(40)
        h.setToolTip("Drag an edge to change a column's width, drag a title to move it.\n"
                     "Right-click: hide or show columns, more ways to sort.")
        self._cols_custom, self._cols_busy = False, False
        for c in EXTRA_COLS:
            self.table.setColumnHidden(c, True)
        self._restore_columns()
        h.sectionResized.connect(self._column_dragged)
        h.sectionMoved.connect(lambda *_: self._columns_changed())
        h.setContextMenuPolicy(Qt.CustomContextMenu)
        h.customContextMenuRequested.connect(self._header_menu)
        self.table.resized.connect(self._fit_name)
        self._cols_timer = QTimer(self); self._cols_timer.setSingleShot(True); self._cols_timer.setInterval(800)
        self._cols_timer.timeout.connect(self._save_columns)
        self.table.setTextElideMode(Qt.ElideMiddle)
        h.sectionClicked.connect(self.sort_by)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.itemSelectionChanged.connect(self.on_select)
        self.table.cellDoubleClicked.connect(lambda *_: self.open_folder())
        self.table.setDragEnabled(True)
        self.table.setDragDropMode(QAbstractItemView.DragOnly)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(lambda pos: self._context_menu(self.table, pos))

        # --- grid (picture wall): same rows as the list, pictures from the cache
        self.grid = ResultGrid(); self.grid.setObjectName("results")
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setIconSize(QSize(TILE, TILE))
        self.grid.setGridSize(QSize(TILE + 24, TILE + 52))
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setWordWrap(True)
        self.grid.setTextElideMode(Qt.ElideMiddle)
        self.grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self.grid.itemSelectionChanged.connect(self.on_select)
        self.grid.itemDoubleClicked.connect(lambda *_: self.open_folder())
        self.grid.setDragEnabled(True)
        self.grid.setDragDropMode(QAbstractItemView.DragOnly)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(lambda pos: self._context_menu(self.grid, pos))
        self._blanks = {}       # file type -> plain tile with its name (until a picture exists)
        self._grid_todo = []     # items still waiting for their picture
        self._grid_db = None
        self._grid_timer = QTimer(interval=15, timeout=self._grid_load_some)
        self.hint = QLabel(); self.hint.setAlignment(Qt.AlignCenter); self.hint.setWordWrap(True)
        self.hint.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.hint.linkActivated.connect(self._take_suggestion)
        self._suggest_n = 0             # newest "did you mean" request; older answers are dropped
        self.hint.setStyleSheet("color: gray; font-size: 13pt; padding: 30px;")
        self.stack = QStackedWidget()
        for wdg in (self.table, self.grid, self.hint):
            self.stack.addWidget(wdg)
        self._view = "list"          # what the user chose; the hint page shows when there's nothing to list
        self._nfiles = 0
        self._last_search = None
        self._only = None         # (drive name, folder) while one drive chip is switched on
        # watch mode: new / changed / deleted files in the work folders show up by themselves
        self._index_lock = threading.Lock()   # full rescan and watch updates never write at the same time
        self._dirty, self._watch_busy = set(), False
        self.watcher = None
        if not os.environ.get("FINDFILES_NO_WATCH"):
            self.watcher = QFileSystemWatcher(self)
            self.watcher.directoryChanged.connect(self._on_dir_changed)
        self._watch_timer = QTimer(self, singleShot=True, interval=WATCH_DELAY_MS, timeout=self._apply_watch)
        # a drive plugged (back) in shows up in /run/media/<user>: rescan by itself a few seconds later
        self._media_dirs = []
        self._update_media_dirs()
        self._replug_timer = QTimer(self, singleShot=True, interval=5000, timeout=self.refresh_index)
        self._builder_timer = QTimer(self, singleShot=True, interval=BUILDER_DELAY_MS, timeout=self.start_builder)
        self._watch_save_timer = QTimer(self, singleShot=True, interval=30_000, timeout=self._save_watch)
        self.bus.watched.connect(self.on_watched)
        self.bus.extracted.connect(self._on_extracted)
        self._handoff = {}            # archive member -> temp copy handed to programs (deleted on close)
        self._extracting = set()
        _clean_old_handoffs()

        # --- preview panel
        self.pic = viewer.ImageView("Select a file to see a preview")
        self.pic.setToolTip("Wheel = zoom · drag = move · double-click = fit / 100 % · F = full screen")
        self.pic.zoom_changed.connect(self._zoom_note)
        self.pic.full_screen.connect(self.picture_full_screen)
        self.textview = viewer.TextView()
        self.mdview = viewer.MarkdownView()
        self._md_plain = False         # Markdown shown as plain text (the switch under the preview; kept while open)
        self._fullpic = None           # viewer.FullImage while a picture fills the screen
        self.info = QLabel(""); self.info.setWordWrap(True); self.info.setObjectName("info")
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.picstack = QStackedWidget(); self.picstack.addWidget(self.pic); self.picstack.addWidget(self.textview)
        self.picstack.addWidget(self.mdview)
        self.pic.page.connect(self.pdf_go)
        self._pdf = None               # (path, pages, page shown) while a PDF with more than one page is shown
        self.viewbar = QWidget()       # page buttons (PDF) and the text switch (Markdown), under the preview
        vb = QHBoxLayout(self.viewbar); vb.setContentsMargins(0, 0, 0, 0); vb.setSpacing(6)
        self.page_prev = QPushButton("Previous page"); self.page_prev.clicked.connect(lambda: self.pdf_go(-1))
        self.page_lbl = QLabel("")
        self.page_next = QPushButton("Next page"); self.page_next.clicked.connect(lambda: self.pdf_go(1))
        for b, tip in ((self.page_prev, "Page Up (click the picture first) · Home = first page"),
                       (self.page_next, "Page Down (click the picture first) · End = last page")):
            b.setToolTip(tip)
        vb.addWidget(self.page_prev); vb.addWidget(self.page_lbl); vb.addWidget(self.page_next); vb.addStretch(1)
        self.md_btn = QPushButton("Plain text"); self.md_btn.setCheckable(True)
        self.md_btn.setToolTip("Show this Markdown file as plain text (on) or formatted (off)")
        self.md_btn.toggled.connect(self._md_switch)
        vb.addWidget(self.md_btn)
        # under the picture, not in the button row below: that row's width decides how narrow this side can get
        self.full_btn = QPushButton("Full screen")
        self.full_btn.setToolTip("The picture or the playing video on the whole screen (F11, or F on the picture)")
        self.full_btn.clicked.connect(self.picture_full_screen); self.full_btn.setEnabled(False)
        vb.addWidget(self.full_btn)
        self.viewbar.hide()
        self._video = None             # player.VideoPanel, made the first time ▶ Play is pressed
        btns = QHBoxLayout()
        self.play_btn = QPushButton("Play video"); self.play_btn.setObjectName("primary")
        self._play_word = "video"      # "video" or "audio": what the Play button plays for the selected file
        self.play_btn.setToolTip("Play it here, with pause, jump, volume and full screen")
        self.play_btn.clicked.connect(self.toggle_video); self.play_btn.hide()
        btns.addWidget(self.play_btn)
        self.pic.picture_set.connect(lambda _on: self._show_viewbar())
        self.picstack.currentChanged.connect(lambda _i: self._show_viewbar())
        for label, fn, tip in (
                ("Open folder", self.open_folder, "Dolphin with the file selected (Enter)"),
                ("Open file", self.open_file, "Open with the default program (a file inside an archive opens the archive)"),
                ("Copy path", self.copy_path, "Copy the full path (Ctrl+Shift+C)"),
                ("Related files", self.show_related, "All versions and parts of this project "
                 "(corp / spate / baza / cheita…, any file type)"),
                ("Find exact copies", self.find_copies, "Compare whole files: which other files are byte-for-byte the same")):
            b = QPushButton(label); b.clicked.connect(fn); b.setToolTip(tip); btns.addWidget(b)
        card = QFrame(); card.setObjectName("card")
        cl = QVBoxLayout(card); cl.setContentsMargins(6, 6, 6, 6); cl.addWidget(self.picstack)
        right = QVBoxLayout(); right.setContentsMargins(6, 0, 0, 0)
        right.addWidget(card, 1); right.addWidget(self.viewbar); right.addWidget(self.info); right.addLayout(btns)
        rw = QWidget(); rw.setLayout(right)

        split = QSplitter(); split.addWidget(self.stack); split.addWidget(rw); split.setSizes([880, 420])
        self.split = split
        self.status = QLabel(""); self.status.setWordWrap(True)
        self.chips = QHBoxLayout(); self.chips.setSpacing(4)
        self.drives_lbl = QLabel("Drives:"); self.drives_lbl.setObjectName("dim")
        self.drives_lbl.setToolTip("Each box is a drive with folders in your search list, and how many of\n"
                                   "your files on it can be found. A dashed grey box = drive unplugged\n"
                                   "(its files still show in results, but can't be opened until it's back).\n"
                                   "Click a box to change which folders are searched.")
        self.pics_lbl = QLabel(""); self.pics_lbl.setToolTip("Small pictures of your files, for the grid view and the size search")
        self.scan_lbl = QLabel(""); self.scan_lbl.setStyleSheet("color: palette(highlight);")
        bottom = QHBoxLayout(); bottom.addWidget(self.status, 1); bottom.addWidget(self.scan_lbl)
        bottom.addWidget(self.drives_lbl); bottom.addLayout(self.chips)
        bottom.addSpacing(8); bottom.addWidget(self.pics_lbl)
        lay = QVBoxLayout(); lay.addLayout(top); lay.addWidget(split, 1); lay.addLayout(bottom)
        w = QWidget(); w.setLayout(lay); self.setCentralWidget(w)

        # keyboard: Ctrl+F / Ctrl+L = search box, Esc = clear, Enter in the list = open folder,
        # Down in the search box = jump into the list
        for keys in ("Ctrl+F", "Ctrl+L"):
            QShortcut(QKeySequence(keys), self, activated=lambda: (self.q.setFocus(), self.q.selectAll()))
        def local(keys, widget, fn):  # only while that widget has focus (default = whole window)
            QShortcut(QKeySequence(keys), widget, activated=fn).setContext(Qt.WidgetShortcut)

        local("Escape", self.q, self.q.clear)
        for keys in ("Return", "Enter"):
            local(keys, self.table, self.open_folder)
        QShortcut(QKeySequence("Ctrl+Shift+C"), self, activated=self.copy_path)
        QShortcut(QKeySequence("F5"), self, activated=self.refresh_index)  # new files without a restart
        QShortcut(QKeySequence("F11"), self, activated=self.picture_full_screen)  # picture / video on the whole screen
        QShortcut(QKeySequence("Ctrl+G"), self,
                  activated=lambda: self.set_view("list" if self.in_grid() else "grid"))
        for keys in ("Return", "Enter"):
            local(keys, self.grid, self.open_folder)
        local("Down", self.q, self._into_list)
        self.q.returnPressed.connect(self._into_list)

        self.pics_lbl.mousePressEvent = self._make_pictures
        self.bus.drives.connect(self._on_drives)
        self.bus.scan.connect(self._on_scan)
        self.bus.pictures.connect(self._on_pictures)
        self._clear_msg = ""
        self._load_recent()
        self._restore_view()
        self._pics_timer = QTimer(self, interval=60_000, timeout=self._count_pictures)
        self._pics_timer.start()
        self.refresh_index(quick=True)  # only folders that changed; "Full index" reads everything
        self.q.setFocus()

    # ---- remembered view + recent searches (programs.get_state / set_state, ~/.config/findfiles)
    def _load_recent(self):
        recent = programs.get_state("recent", []) or []
        comp = QCompleter([r for r in recent if isinstance(r, str)], self)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setFilterMode(Qt.MatchContains)
        self.q.setCompleter(comp)

    def _remember_search(self):
        q = self.q.text().strip()
        if not q:
            return
        old = [r for r in (programs.get_state("recent", []) or []) if isinstance(r, str)]
        if old[:1] == [q]:
            return  # already the newest: no disk write on every click
        programs.set_state("recent", ([q] + [r for r in old if r != q])[:RECENT_MAX])
        self._load_recent()

    def _restore_view(self):
        if os.environ.get("FINDFILES_NO_VIEW_MEMORY"):  # tests: every window starts plain
            return
        v = programs.get_state("last_view")
        if not isinstance(v, dict):
            return
        try:
            if valid_type(v.get("type")):
                self.type.setCurrentText(v["type"])
            self.copies.setChecked(bool(v.get("copies")))
            if isinstance(v.get("q"), str):
                self.q.setText(v["q"])
            if v.get("view") == "grid":
                self.set_view("grid")
            g = v.get("geometry")
            if isinstance(g, list) and len(g) == 4 and all(isinstance(x, int) for x in g):
                self.setGeometry(*g)
            sp = v.get("split")
            if isinstance(sp, list) and len(sp) == 2 and all(isinstance(x, int) and x > 50 for x in sp):
                self.split.setSizes(sp)
        except (TypeError, ValueError):
            pass  # a damaged state file only means the default view

    def _save_view(self):
        if os.environ.get("FINDFILES_NO_VIEW_MEMORY"):
            return
        if programs.get_state("last_view", "missing") is None and not self.q.text():
            return  # history was just cleared and nothing new was typed
        g = self.geometry()
        programs.set_state("last_view", {
            "q": self.q.text(), "type": self.type.currentText(), "copies": self.copies.isChecked(),
            "view": "grid" if self.in_grid() else "list",
            "geometry": [g.x(), g.y(), g.width(), g.height()], "split": self.split.sizes()})

    def _on_scan(self, found, where):
        if not getattr(self, "_indexing", False):
            return
        if not self._scan_expected:  # first start of this window: last list's size, read once
            try:
                db = indexer.connect_ro()
                try:
                    self._scan_expected = db.execute("SELECT COUNT(*) FROM files WHERE path NOT LIKE '%::%'").fetchone()[0]
                finally:
                    db.close()
            except sqlite3.Error:
                self._scan_expected = -1  # no earlier list: count only
        if not where:
            self.scan_lbl.setText(f"Scanning: folders done ({found:,} files), now inside archives…")
            return
        self.scan_lbl.setText("Scanning: " + eta_text(found, max(self._scan_expected, 0),
                                               time.time() - self._scan_start, where))
        self.scan_lbl.setToolTip(self._scan_what + " (search keeps working meanwhile)")

    # ---- drive chips + picture progress (status bar)
    def _on_drives(self, drives):
        while self.chips.count():
            w = self.chips.takeAt(0).widget()
            if w:
                w.hide()  # gone at once, even before the deferred delete runs
                w.deleteLater()
        for name, mounted, files, tip in drives:
            text = f"{name}: {files:,} files" if mounted else f"{name}: unplugged"
            mount, folders = tip.split("\n", 1) if "\n" in tip else (tip, "")
            on = bool(self._only) and self._only[1] == mount
            chip = QLabel(text); chip.setObjectName("chipon" if on else "chip" if mounted else "chipoff")
            state = (f"Drive “{name}” is plugged in. {files:,} of your files on it can be found." if mounted else
                     f"Drive “{name}” is not plugged in. Its last known {files:,} files still show in results,\n"
                     "but they can't be opened until it's plugged back in.")
            chip.setToolTip(f"{state}\n\nWhere: {mount}\nFolders searched on it:\n  "
                            + folders.replace("\n", "\n  ") + ("\n\nClick: show all drives again." if on else
                               f"\n\nClick: show only files on “{name}”.") + "\nRight-click: Settings.")
            chip.mousePressEvent = lambda e, n=name, m=mount: self._chip_clicked(e, n, m)
            self.chips.addWidget(chip)
        self._drives = list(drives)

    def _only_locs(self):
        """Location names of the switched-on drive chip (what its file count is made from)."""
        for _name, _mounted, _files, tip in getattr(self, "_drives", []):
            mount, _, folders = tip.partition("\n")
            if mount == self._only[1]:
                return tuple(sorted(x for x in folders.split("\n") if x)) or ("",)
        return ("",)  # the drive left the list meanwhile: nothing, not everything

    def _chip_clicked(self, event, name, mount):
        """Left click = only this drive's files (again = all drives); right click = Settings."""
        if event is not None and event.button() == Qt.RightButton:
            self.open_settings()
            return
        self._only = None if (self._only and self._only[1] == mount) else (name, mount)
        self._on_drives(getattr(self, "_drives", []))
        self._last_search = None
        self.run_search()

    def _save_watch(self):
        """Write the folder list (new folders seen while the window was open). Skipped during a scan,
        which writes it itself."""
        if not getattr(self, "_watch_dirty", False) or getattr(self, "_indexing", False):
            return
        try:
            indexer.save_watch()
            self._watch_dirty = False
        except OSError as err:
            print("folder list not saved:", err, file=sys.stderr)

    def _old_pictures(self):
        """True while saved pictures are still in the big old format (the builder shrinks them once)."""
        try:
            db = thumbs.connect()
            try:
                return bool(thumbs.old_format_left(db))
            finally:
                db.close()
        except (sqlite3.Error, OSError):
            return False

    def _make_pictures(self, _e=None):
        self.start_builder()
        self._pics_timer.start()
        self._count_pictures(force=True)
        self.status.setText("Making the missing pictures in the background (lowest priority).")

    def _count_pictures(self, force=False):
        if getattr(self, "_counting", False):
            return
        if not force and (not self.isVisible() or self.isMinimized()):
            return  # nobody is looking: no work while idle in the background
        self._counting = True
        bus = self.bus

        def job():
            made, left = -1, 0
            try:
                db = thumbs.connect()
                try:
                    made = db.execute("SELECT COUNT(*) FROM thumbs").fetchone()[0]
                    left = len(thumbs.todo(db, outdated=False))
                finally:
                    db.close()
            except Exception as e:  # any error: no number shown, and the counter keeps working
                print("picture count:", e, file=sys.stderr)
                made, left = -1, 0
            try:
                bus.pictures.emit(made, left)
            except RuntimeError:
                pass  # window closed meanwhile
        threading.Thread(target=job, daemon=True).start()

    def _on_pictures(self, made, left):
        self._counting = False
        if made < 0:
            self.pics_lbl.setText("")
        elif left:
            if not getattr(self, "_builder_kicked", False):
                self._builder_kicked = True  # pictures are waiting (e.g. the last run was cut short): go on
                self.start_builder()
            self.pics_lbl.setText(f"Pictures: {made:,} / {made + left:,} ready")
            self.pics_lbl.setToolTip(f"Small pictures of your files, for the grid view and the size search.\n"
                                     f"{made:,} are ready; {left:,} are still being drawn in the background\n"
                                     "(lowest priority, so the PC stays fast). Until then some files show no\n"
                                     "picture and the size search can miss them.\n\n"
                                     "Click to make the missing ones now.")
        else:
            self._pics_timer.stop()  # complete: no more counting until the next scan
            self.pics_lbl.setText(f"Pictures: all {made:,} ready")
            self.pics_lbl.setToolTip("Small pictures of your files, for the grid view and the size search.\n"
                                     "All are ready, so nothing is running in the background.")

    # ---- video in the preview panel
    def _stop_video(self):
        if self._video is not None:
            self._video.stop()
        self.picstack.setCurrentWidget(self.pic)
        self.play_btn.setText(f"Play {self._play_word}")

    def _save_volume(self, v):
        self._volume = v  # written once, when the window closes

    def toggle_video(self):
        p = self.sel_path()
        if not p or archives.split(p):
            return
        if self._video is not None and self.picstack.currentWidget() is self._video:
            self._stop_video()
            return
        if not os.path.isfile(p):
            self.status.setText("⚠ Not found: it was moved or deleted, or its drive is unplugged.")
            return
        if self._video is None:
            try:
                from player import VideoPanel  # QtMultimedia is loaded only now
            except ImportError as e:
                self.status.setText(f"⚠ Video player not available on this PC ({e}). Use Open file.")
                return
            vol = programs.get_state("volume", 70)
            self._video = VideoPanel(vol if isinstance(vol, int) else 70)
            self._video.failed.connect(lambda msg: self.status.setText(f"⚠ Video: {msg}"))
            self._video.volume_set.connect(self._save_volume)
            self.picstack.addWidget(self._video)
        self.picstack.setCurrentWidget(self._video)
        self._video.play(p)
        self.play_btn.setText(f"Stop {self._play_word}")
        self.status.setText("Space = play / pause · Left / Right = 5 s · Up / Down = volume · M = mute · "
                            "F or double-click = full screen")

    def _into_list(self):
        if self.in_grid():
            if self.grid.count():
                self.grid.setFocus()
                if self.grid.currentRow() < 0:
                    self.grid.setCurrentRow(0)
        elif self.table.rowCount():
            self.table.setFocus()
            if self.table.currentRow() < 0:
                self.table.selectRow(0)

    # ---- list / grid
    def in_grid(self):
        return self._view == "grid"

    def _show_results(self):
        """List / grid, or a short hint when there is nothing to show."""
        if self.table.rowCount():
            self.stack.setCurrentWidget(self.grid if self.in_grid() else self.table)
            return
        q = self.q.text().strip()
        if self.new_btn.isChecked():
            self.hint.setText(f"No new files in the last {search.NEW_DAYS} days" + (f" matching “{q}”." if q else "."))
        elif self._mode == "search" and self._only:
            what = f" matching “{q}”" if q else " of this type"
            self._hint(f"No files{what} on “{self._only[0]}”.\n\nClick the chip again to see all drives, "
                       "or pick “Everything” in the type box.")
            if q:
                self._ask_suggestion(q)
        elif self._mode != "search" or not q:
            n = (f"{self._nfiles:,} file" + ("s" if self._nfiles != 1 else "")) if self._nfiles else "all your files"
            self.hint.setText(f"Type a few words to search {n}.\n\n60mm = models about that size")
        else:
            self._hint(f"Nothing found for “{q}”.\n\nTry fewer words, “Everything” in the type box, "
                       "or tick Include backups & trash.")
            self._ask_suggestion(q)
        self.stack.setCurrentWidget(self.hint)

    # ---- "did you mean" (search.did_you_mean; worked out in a thread, the window never waits)
    def _hint(self, text, link=None):
        """Plain text in the empty-results area; `link` = (search, files) adds a clickable suggestion."""
        if link is None:
            self.hint.setTextFormat(Qt.PlainText)
            self.hint.setText(text)
            return
        import html
        new, found = link
        self.hint.setTextFormat(Qt.RichText)
        self.hint.setText(html.escape(text).replace("\n", "<br>")
                          + f"<br><br>Did you mean: <a href=\"{html.escape(new, quote=True)}\">{html.escape(new)}</a>"
                            f" ({found:,} file{'s' if found != 1 else ''})?")

    def _ask_suggestion(self, q):
        self._suggest_n += 1
        n, bus = self._suggest_n, self.bus
        args = (q, type_exts(self.type.currentText()), self.copies.isChecked(),
                self._only_locs() if self._only else None)

        def job():
            try:
                new, extra = search.did_you_mean(*args)
            except Exception as e:  # noqa: BLE001  a suggestion must never break the window
                print("did you mean:", e, file=sys.stderr)
                new, extra = None, ""
            try:
                bus.suggest.emit(n, q, new, extra)
            except RuntimeError:
                pass  # window closed meanwhile
        threading.Thread(target=job, daemon=True, name="findfiles-suggest").start()

    def on_suggest(self, n, q, new, extra):
        if n != self._suggest_n or q != self.q.text().strip() or self.table.rowCount() \
                or self.stack.currentWidget() is not self.hint:
            return  # typed on, or results arrived: this answer is old
        base = self.hint.text()
        if new:
            self._hint(base, (new, extra))
        elif extra == "together":
            self._hint(base.split("\n\n")[0] + "\n\nEach word finds files on its own, but no file has them all. "
                       "Try fewer words.")

    def _take_suggestion(self, new):
        self.q.setText(new)
        self.run_search()

    def set_view(self, which):
        keep = self.sel_path()
        (self.view_grid if which == "grid" else self.view_list).setChecked(True)
        if which == "grid":
            self._fill_grid(keep)
            self._view = "grid"
            self._show_results()
        else:
            self._grid_timer.stop()
            self._view = "list"
            self._show_results()
            if keep:
                self._select_in_table(keep)

    def _select_in_table(self, path):
        for i in range(self.table.rowCount()):
            if self.table.item(i, 0).data(Qt.UserRole) == path:
                self.table.selectRow(i)
                self.table.scrollToItem(self.table.item(i, 0))
                return True
        return False

    def _fill_grid(self, keep=None):
        """Grid = the list's rows in the list's order. Pictures load a few per tick (no freeze)."""
        self.grid.blockSignals(True)
        self.grid.clear()
        self._grid_todo = []
        for i in range(self.table.rowCount()):
            src = self.table.item(i, 0)
            dims = self.table.item(i, 5).text()
            ext = os.path.splitext(src.text())[1].lstrip(".").upper()
            it = QListWidgetItem(self._blank_tile(ext), src.text() + (f"\n{dims}" if dims else ""))
            it.setData(Qt.UserRole, src.data(Qt.UserRole)); it.setData(VERSION, src.data(VERSION))
            it.setToolTip(src.toolTip())
            self.grid.addItem(it)
            self._grid_todo.append(it)
            if keep and src.data(Qt.UserRole) == keep:
                self.grid.setCurrentItem(it)
        self.grid.blockSignals(False)
        if self.grid.currentItem():
            self.grid.scrollToItem(self.grid.currentItem())
        self._grid_timer.start()

    def _blank_tile(self, ext):
        if ext not in self._blanks:
            pm = QPixmap(TILE, TILE); pm.fill(QColor(50, 50, 50))
            p = QPainter(pm)
            f = QFont(); f.setPointSize(20); f.setBold(True); p.setFont(f)
            p.setPen(QColor(120, 120, 120))
            p.drawText(pm.rect(), Qt.AlignCenter, ext[:6] or "?")
            p.end()
            self._blanks[ext] = QIcon(pm)
        return self._blanks[ext]

    def _grid_load_some(self, n=40):
        if not self._grid_todo:
            self._grid_timer.stop()
            if self._grid_db:
                self._grid_db.close(); self._grid_db = None
            return
        try:
            if self._grid_db is None:
                self._grid_db = thumbs.connect()
            for it in self._grid_todo[:n]:
                ver = it.data(VERSION)
                if ver:
                    hit = thumbs.lookup(self._grid_db, it.data(Qt.UserRole), *ver)
                    if hit and hit[0]:
                        it.setIcon(QIcon(QPixmap(hit[0])))
        except sqlite3.Error:
            pass
        del self._grid_todo[:n]

    # ---- index (rebuilt in the background on every start)
    def _update_media_dirs(self):
        """Folders whose change means a drive was plugged in/out (rescan by itself)."""
        self._media_dirs = sorted({"/".join(r.split("/")[:4]) for r, *_ in indexer.ROOTS + indexer.LOOSE
                                   if r.startswith("/run/media/")})

    def refresh_index(self, force=False, clear_pics=False, quick=False):
        if getattr(self, "_indexing", False):
            if quick:
                return
            # one at a time; a rebuild / settings change asked for meanwhile runs right after this one
            if force or getattr(self, "_again", None) is not None:
                self._again = bool(force or self._again)
            else:
                self._again = False
            return
        self._indexing = True
        self._scan_what = ("Rebuilding the file list from scratch" if force else
                           "Checking for changes since last time" if quick else "Full index: reading every folder")
        self._quick = None  # (added, removed) when the quick start-up check was enough
        self._scan_start, self._scan_expected = time.time(), 0  # read from the last list on the first tick
        self.status.setText(self._scan_what + " in the background… (you can already search)")
        bus = self.bus

        def tick(found, where):
            try:
                bus.scan.emit(found, where)
            except RuntimeError:
                pass  # window closed

        def job():
            try:  # the drive boxes right away, from the last list (the scan refreshes them at the end)
                bus.drives.emit(drive_summary(drive_counts()))
            except RuntimeError:
                pass
            if clear_pics:  # off the UI thread: tens of thousands of files can be deleted
                try:
                    _ok, self._clear_msg = thumbs.clear_all()
                except Exception as e:  # never stop the rescan for this
                    self._clear_msg = f"Pictures could not be cleared ({e})."
            try:  # this thread only: lower priority, so a scan never slows the PC down
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
            except (OSError, AttributeError):
                pass
            try:
                with self._index_lock:
                    res = indexer.quick() if quick and not force and not clear_pics else None
                    if res is not None:
                        n, missing, self._quick = res[0], res[1], res[2:]
                    else:
                        n, missing = indexer.build(force=force, tick=tick)
            except Exception as e:  # keep the previous index
                print("index error:", e, file=sys.stderr)
                n, missing = -1, []
            try:
                self.bus.drives.emit(drive_summary(drive_counts()))
                self.bus.indexed.emit(n, missing)
            except RuntimeError:
                pass  # the window was closed while the list was being built
        self._index_thread = threading.Thread(target=job, daemon=True, name="findfiles-index")
        self._index_thread.start()

    def on_indexed(self, n, missing):
        self._indexing = False
        self._builder_kicked = False
        self.scan_lbl.setText("")
        self._pics_timer.start()  # new files may need pictures again
        if n >= 0:
            self._nfiles = n
            took = time.time() - getattr(self, "_scan_start", time.time())
            quick = self._quick
            if quick is not None:
                msg = f"{n:,} files ready."
                if quick[0] or quick[1]:
                    msg += f" Since last time: {quick[0]:,} new, {quick[1]:,} gone."
            else:
                msg = f"Full index done: {n:,} files" + (f" (took {took / 60:.0f} min)." if took >= 90 else ".")
            full = indexer.last_full()
            self.rescan_btn.setToolTip(FULL_TIP + f"\n\nLast full index: {ago(full)}.")
            if quick is not None and time.time() - full > 7 * 86400:
                msg += f"  Last full index: {ago(full)}."
            if missing:
                msg += "  ⚠ Not plugged in (showing their last known files): " + ", ".join(sorted(set(missing)))
        else:
            msg = "⚠ Could not update the file list (is a drive unplugged?). Showing the last saved list."
        if n >= 0 and self.watcher is not None:
            watching = self._sync_watch()
            msg += f"  Watching {watching:,} folders for new files." if watching else ""
        if indexer.SETTINGS_PROBLEM:
            msg = "⚠ " + indexer.SETTINGS_PROBLEM + "  " + msg
        if self._clear_msg:
            msg, self._clear_msg = self._clear_msg + "  " + msg, ""
        self._index_msg = msg
        self.status.setText(msg)
        again = getattr(self, "_again", None)
        if again is not None:  # settings changed / rebuild asked while this scan ran
            self._again = None
            QTimer.singleShot(0, lambda: self.refresh_index(force=again))
        if n >= 0:
            self._update_new_badge()
            if self._quick is None or self._quick[0] or self._old_pictures():
                self.start_builder()  # after a quick start: only when there is something to do
            self._count_pictures()
        if self._mode == "search":
            self.run_search(keep_selection=True)

    # ---- settings
    def open_settings(self):
        current, problem = roots.load()
        dlg = SettingsDialog(self, current, problem)
        accepted = dlg.exec() == QDialog.Accepted
        if dlg.history_cleared:  # cleared even if the dialog was then cancelled
            self._load_recent()
        if not accepted:
            return
        try:
            roots.save(dlg.result_settings)
        except OSError as e:
            QMessageBox.warning(self, "Settings", f"Could not save the settings:\n{e}")
            return
        old = {r["path"]: r["label"] for r in current["roots"]}
        for r in dlg.result_settings["roots"]:  # renamed: an unplugged drive's kept files follow the new name
            if old.get(r["path"]) not in (None, r["label"]):
                indexer.LABEL_RENAMES[old[r["path"]]] = r["label"]
        indexer.apply_settings()
        self._update_media_dirs()
        self.refresh_index(force=dlg.action == "rebuild", clear_pics=dlg.action == "rebuild" and dlg.redraw)

    # ---- "New" view
    def _toggle_new(self, on):
        if on:
            programs.set_state("new_seen_until", time.time())  # you've looked: the count starts again
        self.run_search()
        self._update_new_badge()

    def _update_new_badge(self):
        since = max(programs.get_state("new_seen_until", 0) or 0, time.time() - search.NEW_DAYS * 86400)
        n = 0 if self.new_btn.isChecked() else search.count_new(since, FILTERS["All my work"])
        self.new_btn.setText(f"New ({n:,})" if n else "New")

    # ---- watch mode
    def _sync_watch(self):
        """Watch exactly the folders the last scan listed (work locations; not Software/Zip/backups)."""
        want = set(indexer.WATCH_DIRS) | {d for d in self._media_dirs if os.path.isdir(d)}
        have = set(self.watcher.directories())
        if have - want:
            self.watcher.removePaths(list(have - want))
        room = watch_room() - len(have & want)
        # the folders nearest the top first; the rest are still checked at the next start
        todo = sorted(want - have, key=lambda d: (d.count("/"), d))[:max(0, room)]
        for i in range(0, len(todo), 500):
            self.watcher.addPaths(todo[i:i + 500])  # paths it can't watch are skipped by Qt
        return len(self.watcher.directories())

    def _on_dir_changed(self, path):
        if path in self._media_dirs:  # a drive was plugged in or out
            self._replug_timer.start()
            return
        self._dirty.add(path)
        self._watch_timer.start()  # restart: wait until the burst of saves is over

    def _apply_watch(self):
        if self._watch_busy or getattr(self, "_indexing", False):
            self._watch_timer.start()  # a rescan / update is running: try again shortly
            return
        dirs, self._dirty = self._dirty, set()
        if not dirs:
            return
        self._watch_busy = True

        def job():
            try:
                with self._index_lock:
                    res = indexer.update_dirs(dirs)
            except Exception as e:  # never break the window; try these folders again shortly
                print("watch update:", e, file=sys.stderr)
                self._retry = dirs
                res = (0, 0, [], False)
            self.bus.watched.emit(*res)
        threading.Thread(target=job, daemon=True).start()

    def on_watched(self, added, removed, new_dirs, written):
        self._watch_busy = False
        retry, self._retry = getattr(self, "_retry", None), None
        if retry:  # the update failed (e.g. database busy): keep those folders queued, a few times
            self._retries = getattr(self, "_retries", 0) + 1
            if self._retries <= 3:
                self._dirty |= retry
                self._watch_timer.start()
            else:
                self._retries = 0
                self.status.setText("⚠ Could not update the file list for new files. Press F5 to rescan.")
        else:
            self._retries = 0
        if new_dirs and self.watcher is not None:
            room = watch_room() - len(self.watcher.directories())  # same cap as _sync_watch
            fit = sorted(new_dirs, key=lambda d: (d.count("/"), d))[:max(0, room)]
            if fit:
                self.watcher.addPaths(fit)  # all of them are still checked at the next start
            indexer.remember(new_dirs)  # for the next quick start: saved shortly, and again on close
            self._watch_dirty = True
            self._watch_save_timer.start()
        if not written:
            return
        self._nfiles += added - removed
        self._update_new_badge()
        if self._mode == "search":
            self.run_search(keep_selection=True)
        parts = ([f"{added} new file{'s' if added != 1 else ''} listed"] if added else []) + \
                ([f"{removed} gone"] if removed else []) + ([] if added or removed else ["a file was updated"])
        self.status.setText("✓ " + ", ".join(parts) + f".   ·   {self._index_msg}")
        # pictures for the new files (low priority, only what's missing): one run after a burst of saves,
        # not a new process (index + cache check) for every file
        self._builder_timer.start()

    def start_builder(self):
        """Make missing pictures in a separate, low-priority process (keeps going if the window
        closes; only one runs at a time; waits while the PC is short on RAM)."""
        if os.environ.get("FINDFILES_NO_BUILDER"):
            return
        cmd = [sys.executable, "-I", os.path.abspath(thumbs.__file__)]
        if shutil.which("ionice"):
            cmd = ["ionice", "-c3"] + cmd
        if shutil.which("nice"):
            cmd = ["nice", "-n", "19"] + cmd
        try:
            os.makedirs(os.path.dirname(thumbs.LOG_PATH), exist_ok=True)
            if os.path.exists(thumbs.LOG_PATH) and os.path.getsize(thumbs.LOG_PATH) > 1_000_000:
                os.replace(thumbs.LOG_PATH, thumbs.LOG_PATH + ".old")  # keep the log small
            with open(thumbs.LOG_PATH, "a") as log:
                subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
        except OSError as e:
            print("picture builder:", e, file=sys.stderr)

    # ---- search
    def run_search(self, keep_selection=False):
        self._run_search(keep_selection)
        if self._only:  # one drive chip is on: say so, the result list alone doesn't show it
            self.status.setText(f"Only “{self._only[0]}” (click its chip again for all drives).  "
                                + self.status.text())

    def _run_search(self, keep_selection=False):
        only = self._only_locs() if self._only else None
        args = (self.q.text(), self.type.currentText(), self.copies.isChecked(), self.new_btn.isChecked(), only)
        if self._mode == "search" and args == self._last_search:
            keep_selection = True  # same search again (e.g. the typing timer after a quick click)
        self._last_search = args
        self._mode = "search"
        keep = self.sel_path() if keep_selection else None
        if self.new_btn.isChecked():  # "New" view: arrivals of the last days, newest first
            rows, mm = search.query(self.q.text(), type_exts(self.type.currentText()), self.copies.isChecked(),
                                    since=time.time() - search.NEW_DAYS * 86400, under=only), None
        else:
            rows, mm = search.query_sized(self.q.text(), type_exts(self.type.currentText()), self.copies.isChecked(),
                                          under=only)
        if not keep_selection:
            self._sort = (-1, Qt.AscendingOrder)  # a new search starts in rank order
        self._fill(rows, keep)
        if keep_selection and self._sort[0] >= 0:  # background refresh / F5: keep the user's sort
            self._apply_sort()
        if self.new_btn.isChecked():
            self.status.setText(f"{len(rows)} new in the last {search.NEW_DAYS} days (newest first)."
                                + (f"   ·   {self._index_msg}" if "⚠" in self._index_msg else ""))
            return
        if not self.q.text().strip():
            if only:  # a drive chip with an empty search box: that drive's newest files
                more = f"the newest {len(rows):,}" if len(rows) == search.LIMIT else f"{len(rows):,}"
                self.status.setText(f"{more} files, newest first. Type to search this drive only.")
            else:
                self.status.setText(self._index_msg)
            return
        if not rows:
            self.status.setText("Nothing found." + (f"   ·   {self._index_msg}" if "⚠" in self._index_msg else ""))
            return
        more = f" (showing best {search.LIMIT:,})" if len(rows) == search.LIMIT else ""
        size = ""
        if mm is not None:
            lo, hi = search.size_range(mm)
            size = f"  Size {lo:.0f}–{hi:.0f} mm (largest side)."
            try:
                pending = thumbs.stats()[2]
            except sqlite3.Error:
                pending = 0
            if pending:
                size += f" {pending:,} files don't have a size yet (being made in the background)."
        self.status.setText(f"{len(rows)} found{more}.{size}  Enter / double-click = open folder."
                            "  Click a column title to sort."
                            + (f"   ·   {self._index_msg}" if "⚠" in self._index_msg else ""))

    def _fill(self, rows, keep=None):
        """Put rows (path, name, loc, size, mtime) in the table; reselect `keep` if it's still there."""
        try:
            tdb = thumbs.connect()
            dims = thumbs.dims_for(tdb, rows)
            tdb.close()
        except sqlite3.Error:
            dims = {}
        self.table.blockSignals(True)
        self.table.clearSelection()
        # While filling, no column sizes itself to its contents and nothing repaints: otherwise every
        # cell written makes Qt measure up to 1,000 rows again (2,000 results froze the window ~50 s).
        self.table.setUpdatesEnabled(False)  # (no column sizes itself to its contents: all are Interactive)
        try:
            self.table.setRowCount(0)
            self.table.setRowCount(len(rows))
            self._fill_rows(rows, dims)
        finally:
            self.table.setUpdatesEnabled(True)
        self._fill_done(rows, keep)

    def _fill_rows(self, rows, dims):
        for i, (path, name, loc, size, mtime) in enumerate(rows):
            it = QTableWidgetItem(name); it.setData(Qt.UserRole, path); it.setToolTip(path)
            it.setData(VERSION, (size, mtime))
            self.table.setItem(i, 0, it)
            d = dims.get(path)
            self.table.setItem(i, 5, SortItem(mm_text(d), max(d)) if d else SortItem("", -1))
            ext = os.path.splitext(name)[1].lower().lstrip(".")
            inside = archives.split(path)
            desc = filetypes.describe(ext) + (" · in " + os.path.basename(inside[0]) if inside else "")
            for c, text in ((1, desc), (2, loc)):
                cell = QTableWidgetItem(text); cell.setToolTip(text)
                self.table.setItem(i, c, cell)
            self.table.setItem(i, 3, SortItem(human(size), size))
            self.table.setItem(i, 4, SortItem(time.strftime("%Y-%m-%d", time.localtime(mtime)), mtime))
            folder = os.path.dirname(inside[0] if inside else path)
            for c, text, tip in ((6, os.path.basename(folder) or folder, folder), (7, ext, ext),
                                 (8, loc.split(" › ")[0], loc)):
                cell = QTableWidgetItem(text); cell.setToolTip(tip)
                self.table.setItem(i, c, cell)

    def _fill_done(self, rows, keep):
        self._auto_columns()
        self.table.horizontalHeader().setSortIndicatorShown(False)
        reselected = False
        if keep:
            for i, r in enumerate(rows):
                if r[0] == keep:
                    self.table.selectRow(i)
                    self.table.scrollToItem(self.table.item(i, 0))
                    reselected = True
                    break
        self.table.blockSignals(False)
        if self.in_grid():
            self._fill_grid(keep)
        self._show_results()
        if not reselected:  # nothing highlighted: no hidden current row, no stale preview
            self.table.blockSignals(True)
            self.table.setCurrentCell(-1, -1)
            self.table.blockSignals(False)
            self.current_preview = None
            self._want = None
            self.pic.setPixmap(QPixmap()); self.pic.setText("Select a file to see a preview"); self.info.setText("")
            if self.picstack.currentWidget() in (self.textview, self.mdview):  # no old text left on show
                self.picstack.setCurrentWidget(self.pic)
            self._pdf, self._md_shown = None, None
            self._show_viewbar()

    # ---- columns: widths, order, hidden ones (kept between runs), right-click menu on the titles
    def _auto_columns(self):
        """Widths that set themselves, until the user drags one: contents (capped), Name takes the rest."""
        if self._cols_custom:
            return
        self._cols_busy = True
        try:
            for c in range(1, len(COLUMNS)):
                if not self.table.isColumnHidden(c):
                    self.table.resizeColumnToContents(c)
                    cap = MAX_COL if c in (1, 2, 6, 8) else 400
                    self.table.setColumnWidth(c, min(self.table.columnWidth(c), cap))
        finally:
            self._cols_busy = False
        self._fit_name()

    def _fit_name(self):
        if self._cols_custom or self.table.isColumnHidden(0):
            return
        shown = [c for c in range(1, len(COLUMNS)) if not self.table.isColumnHidden(c)]
        room = self.table.room()
        self._cols_busy = True
        try:
            # a narrow window: the wordy columns give way first (down to 70 px), then Name (down to 120)
            for c in (6, 8, 1, 2):
                over = sum(self.table.columnWidth(x) for x in shown) + 200 - room
                if over > 0 and c in shown:
                    self.table.setColumnWidth(c, max(70, self.table.columnWidth(c) - over))
            used = sum(self.table.columnWidth(c) for c in shown)
            self.table.setColumnWidth(0, max(120, room - used))
        finally:
            self._cols_busy = False

    def _column_dragged(self, *_):
        if self._cols_busy:
            return  # our own sizing, not the user's
        self._cols_custom = True
        self._columns_changed()

    def _columns_changed(self):
        self._cols_timer.start()

    def _save_columns(self):
        h = self.table.horizontalHeader()
        programs.set_state("columns", {
            "n": len(COLUMNS), "custom": self._cols_custom,
            "hidden": [c for c in range(len(COLUMNS)) if self.table.isColumnHidden(c)],
            "widths": [self.table.columnWidth(c) for c in range(len(COLUMNS))] if self._cols_custom else [],
            "order": [h.visualIndex(c) for c in range(len(COLUMNS))]})

    def _restore_columns(self):
        v = programs.get_state("columns")
        if not isinstance(v, dict) or v.get("n") != len(COLUMNS):
            return
        h = self.table.horizontalHeader()
        try:
            hidden = {int(c) for c in v.get("hidden", [])} - {0}
            for c in range(len(COLUMNS)):
                self.table.setColumnHidden(c, c in hidden)
            order = [int(x) for x in v.get("order", [])]
            if sorted(order) == list(range(len(COLUMNS))):
                for visual in range(len(COLUMNS)):
                    h.moveSection(h.visualIndex(order.index(visual)), visual)
            widths = [int(x) for x in v.get("widths", [])]
            if v.get("custom") and len(widths) == len(COLUMNS):
                for c, wd in enumerate(widths):
                    if wd >= 20:
                        self.table.setColumnWidth(c, min(wd, 4000))
                self._cols_custom = True
        except (TypeError, ValueError):
            pass  # a damaged entry: keep the defaults

    def _toggle_column(self, col, show):
        self._cols_busy = True   # hiding/showing reports a "resize": not a drag by the user
        try:
            self.table.setColumnHidden(col, not show)
        finally:
            self._cols_busy = False
        self._auto_columns()
        self._columns_changed()

    def _reset_columns(self):
        h = self.table.horizontalHeader()
        self._cols_busy = True
        try:
            for c in range(len(COLUMNS)):
                h.moveSection(h.visualIndex(c), c)
                self.table.setColumnHidden(c, c in EXTRA_COLS)
        finally:
            self._cols_busy = False
        self._cols_custom = False
        self._auto_columns()
        self._columns_changed()

    def _best_match_order(self):
        self._sort = (-1, Qt.AscendingOrder)
        self._last_search = None
        self.run_search(keep_selection=True)

    def _header_menu(self, pos):
        h = self.table.horizontalHeader()
        menu = QMenu(self)
        title = menu.addAction("Columns shown"); title.setEnabled(False)
        for c, name in enumerate(COLUMNS):
            a = menu.addAction(name); a.setCheckable(True); a.setChecked(not self.table.isColumnHidden(c))
            if c == 0:
                a.setEnabled(False)  # the name always stays
            a.toggled.connect(lambda on, c=c: self._toggle_column(c, on))
        menu.addSeparator()
        sort = menu.addMenu("Sort by")
        best = sort.addAction("Best match (the search's own order)"); best.triggered.connect(self._best_match_order)
        sort.addSeparator()
        for c, name in enumerate(COLUMNS):
            for order, word in ((Qt.AscendingOrder, SORT_WORDS.get(c, ("A to Z", "Z to A"))[0]),
                                (Qt.DescendingOrder, SORT_WORDS.get(c, ("A to Z", "Z to A"))[1])):
                a = sort.addAction(f"{name}: {word}")
                a.triggered.connect(lambda _=False, c=c, o=order: self._sort_exact(c, o))
        menu.addSeparator()
        menu.addAction("Fit widths to the contents again").triggered.connect(self._refit_columns)
        menu.addAction("Reset columns").triggered.connect(self._reset_columns)
        menu.exec(h.mapToGlobal(pos))

    def _refit_columns(self):
        self._cols_custom = False
        self._auto_columns()
        self._columns_changed()

    def _sort_exact(self, col, order):
        self._sort = (col, order)
        self._apply_sort()

    def sort_by(self, col):
        order = Qt.DescendingOrder if self._sort == (col, Qt.AscendingOrder) else Qt.AscendingOrder
        self._sort = (col, order)
        self._apply_sort()

    def _apply_sort(self):
        col, order = self._sort
        self.table.sortItems(col, order)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.horizontalHeader().setSortIndicator(col, order)
        it = self.table.currentItem()
        if it:
            self.table.scrollToItem(it)
        if self.in_grid():
            self._fill_grid(self.sel_path())

    # ---- preview
    def _sel_item(self):
        if self.in_grid():
            return self.grid.currentItem()
        r = self.table.currentRow()
        return self.table.item(r, 0) if r >= 0 else None

    def sel_path(self):
        it = self._sel_item()
        return it.data(Qt.UserRole) if it else None

    def on_select(self):
        it = self._sel_item()
        p = it.data(Qt.UserRole) if it else None
        if not p:
            return
        ver = it.data(VERSION)
        self.current_preview = p
        self._stop_video()
        self._pdf = None
        self._md_shown = None
        self._show_viewbar()
        ext = os.path.splitext(p)[1].lower().lstrip(".")
        self._play_word = "audio" if ext in _previews.AUDIO_EXTS else "video"
        self.play_btn.setText(f"Play {self._play_word}")
        self.play_btn.setVisible((ext in _previews.VIDEO_EXTS or ext in _previews.AUDIO_EXTS)
                                 and not archives.split(p))
        self._remember_search()
        self.info.setText(p)
        self.pic.setPixmap(QPixmap()); self.pic.setText("Drawing preview…")
        if ver and not archives.split(p):  # saved picture: show it now, the full-size one follows
            try:
                db = thumbs.connect()
                hit = thumbs.lookup(db, p, *ver)
                db.close()
            except sqlite3.Error:
                hit = None
            if hit and hit[0]:
                self.pic.setPixmap(QPixmap(hit[0]))
                self.info.setText(p + (f"\n{hit[2]}" if hit[2] else ""))
        # drawn for the panel's real size (sharper on a big panel), within sane limits
        side = max(PREVIEW_SIZE, min(PREVIEW_MAX, self.pic.width(), self.pic.height()))
        self._want = (p, ver, side, 0)
        self._wake.set()

    def _preview_worker(self):
        """One background thread draws previews, one at a time. Clicking quickly through
        big files skips the ones you've already passed (keeps RAM low)."""
        db = None
        while True:
            self._wake.wait()
            self._wake.clear()
            want = self._want
            if want is None:
                continue
            p, ver, side, page = want
            if page:  # another page of a PDF: drawn, never saved as the file's picture
                img, note = previews.pdf_page(p, page, side)
                self.bus.preview.emit(p, img, note)
                continue
            if previews.is_text(p):
                text, note = previews.text_preview(p)
                self.bus.text.emit(p, text, note)
                continue
            img, note = previews.render(p, side)
            self.bus.preview.emit(p, img, note)
            if ver and not archives.split(p) and thumbs.worth_saving(img, note) and thumbs.unchanged(p, *ver):
                try:  # remember it, so next time (and in the grid) it's instant
                    db = db or thumbs.connect()
                    if not thumbs.current(db, p, *ver):
                        thumbs.store(db, p, ver[0], ver[1], img, note)
                except (sqlite3.Error, OSError) as e:
                    print("picture cache:", e, file=sys.stderr)
            ext = os.path.splitext(p)[1].lower().lstrip(".")
            if (img is not None and ext in previews.IMAGE_EXTS and not archives.split(p)
                    and self._want is want):  # still the selected file: now the photo itself, for zooming
                big = previews.full_image(p)
                if big is not None and max(big.width(), big.height()) > max(img.width(), img.height()):
                    self.bus.preview_full.emit(p, big)

    def on_preview(self, p, img, note):
        if p != self.current_preview:  # user already clicked another file
            return
        m = (previews.PDF_PAGE_NOTE.match(note or "")
             if os.path.splitext(p)[1].lower() == ".pdf" and not archives.split(p) else None)
        if m:
            shown, total = int(m.group(1)) - 1, int(m.group(2))
            if self._pdf and self._pdf[0] == p and shown != self._pdf[2]:
                return  # an older page arrived after a newer one was asked for
            if total > 1:
                self._pdf = (p, total, shown)
            self._show_viewbar()
        if img is None:  # never keep a saved picture of a file that's gone or no longer previews
            self.pic.setPixmap(QPixmap()); self.pic.setText(note or "No preview")
            self.info.setText(p)
        else:
            self.pic.setPixmap(QPixmap.fromImage(img))
            self.info.setText(p + (f"\n{note}" if note else ""))
        if self._fullpic is not None:
            self._fullpic.setPixmap(self.pic.pixmap()); self._fullpic.setText(self.pic.text())

    def on_preview_full(self, p, img):
        """The photo itself arrived (the quick small version is already showing): zooming gets sharp."""
        if p != self.current_preview or img is None:
            return
        px = QPixmap.fromImage(img)
        self.pic.setPixmap(px, keep_view=True)
        if self._fullpic is not None:
            self._fullpic.setPixmap(px, keep_view=True)

    def on_text(self, p, text, note):
        if p != self.current_preview:
            return
        if text is None:
            self.pic.setPixmap(QPixmap()); self.pic.setText(note or "No preview")
            self.info.setText(p)
            return
        self.textview.setPlainText(text)
        md = os.path.splitext(p)[1].lower().lstrip(".") in previews.MARKDOWN_EXTS
        if md and len(text.encode("utf-8", "replace")) > previews.MARKDOWN_FORMAT_LIMIT:
            md = False  # too long to format without freezing the window
            note = (note + " - " if note else "") + "too long to format quickly, shown as plain text"
        if md:
            self.mdview.show_markdown(text)
            self.md_btn.blockSignals(True); self.md_btn.setChecked(self._md_plain); self.md_btn.blockSignals(False)
            self._md_shown = p
            self.picstack.setCurrentWidget(self.textview if self._md_plain else self.mdview)
        else:
            self.picstack.setCurrentWidget(self.textview)
        self._show_viewbar()
        self.info.setText(p + (f"\n{note}" if note else ""))

    def _md_switch(self, plain):
        self._md_plain = plain
        if getattr(self, "_md_shown", None) == self.current_preview and \
                self.picstack.currentWidget() in (self.textview, self.mdview):
            self.picstack.setCurrentWidget(self.textview if plain else self.mdview)

    def _zoom_note(self, zoom):
        now = self.status.text()
        if zoom:
            if not now.startswith("Zoom "):
                self._pre_zoom = now  # put back when the picture is fitted again or another file is picked
            self.status.setText(f"Zoom {zoom}  ·  wheel = zoom, drag = move, double-click or 0 = fit, F = full screen")
        elif now.startswith("Zoom "):
            self.status.setText(getattr(self, "_pre_zoom", ""))

    # ---- a picture on the whole screen
    def picture_full_screen(self):
        if self._fullpic is not None:
            self._fullpic.close()
            return
        if self._video is not None and self.picstack.currentWidget() is self._video:
            self._video.set_full(True)   # a playing video: its own full screen, with the controls
            return
        if self.picstack.currentWidget() is not self.pic or self.pic.pixmap().isNull():
            return  # text, or nothing to show
        f = viewer.FullImage()
        f.setPixmap(self.pic.pixmap())
        f.closed.connect(self._full_closed)
        f.step.connect(self.step_selection)
        f.page.connect(self._full_page)
        self._fullpic = f
        f.showFullScreen()

    def _full_closed(self):
        self._fullpic = None

    def _type_counts(self):
        """{ending: files} for the Type menu (work folders only, unless Include backups & trash is ticked)."""
        db = indexer.connect_ro()
        try:
            sql = "SELECT ext, COUNT(*) FROM files" + ("" if self.copies.isChecked() else " WHERE kind='work'")
            return dict(db.execute(sql + " GROUP BY ext").fetchall())
        finally:
            db.close()

    def _can_full_screen(self):
        cur = self.picstack.currentWidget()
        return ((cur is self.pic and not self.pic.pixmap().isNull())
                or (self._video is not None and cur is self._video))

    # ---- PDF pages
    def pdf_go(self, delta):
        """Turn the shown PDF by `delta` pages (clamped to first / last). False if no PDF pages are shown."""
        if not self._pdf or self._pdf[0] != self.current_preview:
            return False
        p, total, cur = self._pdf
        new = max(0, min(total - 1, cur + delta))
        if new != cur:
            self._pdf = (p, total, new)
            self._show_viewbar()
            it = self._sel_item()
            side = max(PREVIEW_SIZE, min(PREVIEW_MAX, self.pic.width(), self.pic.height()))
            self._want = (p, it.data(VERSION) if it else None, side, new)
            self._wake.set()
        return True

    def _full_page(self, delta):
        """Page keys on the full-screen picture: PDF pages, or else the next / previous result."""
        if not self.pdf_go(delta):
            self.step_selection(1 if delta > 0 else -1)

    def _show_viewbar(self):
        pages = bool(self._pdf) and self._pdf[0] == self.current_preview
        for w in (self.page_prev, self.page_lbl, self.page_next):
            w.setVisible(pages)
        if pages:
            _p, total, cur = self._pdf
            self.page_lbl.setText(f"Page {cur + 1} of {total}")
            self.page_prev.setEnabled(cur > 0)
            self.page_next.setEnabled(cur < total - 1)
        md = getattr(self, "_md_shown", None) == self.current_preview and self.current_preview is not None
        self.md_btn.setVisible(md)
        full = self._can_full_screen()
        self.full_btn.setVisible(full); self.full_btn.setEnabled(full)
        self.viewbar.setVisible(pages or md or full)

    def step_selection(self, by):
        """Select the next / previous result (used by the full-screen picture's arrow keys)."""
        if self.in_grid():
            n, cur = self.grid.count(), self.grid.currentRow()
            if n:
                self.grid.setCurrentRow(max(0, min(n - 1, cur + by)))
        else:
            n, cur = self.table.rowCount(), self.table.currentRow()
            if n:
                self.table.selectRow(max(0, min(n - 1, cur + by)))

    # ---- actions
    def _real(self):
        """Selected path on disk (for a file inside an archive: the archive). Tells the user if it's gone."""
        p = self.sel_path()
        if not p:
            return None
        sp = archives.split(p)
        real = sp[0] if sp else p
        if not os.path.exists(real):
            self.status.setText("⚠ Not found: it was moved or deleted, or its drive is unplugged. "
                                "Press F5 to refresh the list.")
            return None
        return real

    def open_folder(self):
        p = self._real()
        if p:
            show_in_file_manager(p)

    def open_file(self):
        """Opens the file (a file inside an archive: opens the archive)."""
        p = self._real()
        if p:
            subprocess.Popen(["xdg-open", p], start_new_session=True)

    def show_related(self):
        """Replace the results with every file of the same project (see search.family)."""
        p = self.sel_path()
        if not p:
            return
        name = os.path.basename(archives.split(p)[1] if archives.split(p) else p)
        key, rows = search.related(name, self.copies.isChecked())
        if not rows:
            self.status.setText(f"No related files found for “{name}”.")
            return
        self._mode = "related"
        self._fill(rows, p)
        self.status.setText(f"{len(rows)} file(s) of project “{key}” (versions, parts, slicer files…).   "
                            "Type in the search box to go back.")

    def find_copies(self):
        p = self.sel_path()
        if not p or self._busy:
            return
        if archives.split(p):
            self.status.setText("Files inside archives can't be compared yet.")
            return
        if not os.path.isfile(p):
            self.status.setText("⚠ Not found: it was moved or deleted, or its drive is unplugged.")
            return
        size, cands = copies.candidates(p, indexer.DB_PATH)
        if size < copies.MIN_SIZE:
            self.status.setText("This file is empty (0 bytes) - nothing to compare.")
            return
        if not cands:
            self.status.setText(f"No other file has exactly this size ({human(size)}) - so no exact copies anywhere in the list.")
            return
        total = size * (len(cands) + 1)
        if total > copies.ASK_ABOVE_BYTES:
            ok = QMessageBox.question(self, "Find exact copies",
                                      f"{len(cands)} files have the same size. Checking means reading {human(total)} "
                                      "from the drives (can take minutes). Go ahead?")
            if ok != QMessageBox.Yes:
                return
        self._busy = True
        self._copies_from = (p, self.q.text())  # to tell later whether the user moved on
        self.status.setText(f"Comparing whole files… ({len(cands)} files with the same size)")
        read = [0]

        def prog(n):
            read[0] += n
            if read[0] // (200 << 20) != (read[0] - n) // (200 << 20):
                self.bus.copies_progress.emit(read[0] >> 20)

        def job():
            try:
                res = copies.find(p, indexer.DB_PATH, prog)
            except Exception as e:
                res = {"error": str(e)}
            self.bus.copies_done.emit(p, res)
        threading.Thread(target=job, daemon=True).start()

    def on_copies(self, p, res):
        self._busy = False
        if getattr(self, "_copies_from", None) != (p, self.q.text()) or self.sel_path() != p:
            if "error" in res:
                self.status.setText(f"Copy check for “{os.path.basename(p)}” could not finish: {res['error']}")
                return
            n = len(res.get("identical", []))
            self.status.setText(f"Copy check for “{os.path.basename(p)}” finished: {n} exact cop"
                                f"{'y' if n == 1 else 'ies'}. (Select it again and press Find exact copies to see them.)")
            return  # the user moved on: don't replace what they're looking at
        if "error" in res:
            self.status.setText("⚠ Could not compare: " + res["error"])
            return
        same = res["identical"]
        name = os.path.basename(p)
        if not same:
            self.status.setText(f"No exact copies of “{name}”. {res['different']} file(s) have the same size "
                                "but different content (checked byte for byte).")
            return
        db = indexer.connect_ro()
        want = [p] + [s[0] for s in same]
        q = "SELECT path,name,loc,size,mtime FROM files WHERE path IN (%s)" % ",".join("?" * len(want))
        rows = db.execute(q, want).fetchall()
        db.close()
        rows.sort(key=lambda r: r[0] != p)  # the original first
        self._mode = "copies"
        self._fill(rows, p)
        self.status.setText(f"“{name}” + {len(same)} exact cop{'y' if len(same) == 1 else 'ies'} (whole files compared). "
                            f"{res['different']} same-size file(s) differ.   Type in the search box to go back.")

    # ---- hand-off to other programs (right-click menu, Open with, drag & drop)
    def handoff_path(self, then=None):
        """A real file other programs can open: the file itself, or (inside an archive) a temp copy of
        just that member, with its real name. Extraction runs in the background (a big 7z can take a
        minute): returns None now and calls then(path) when ready. Nothing on the user's drives changes."""
        p = self.sel_path()
        if not p:
            return None
        if not archives.split(p):
            return self._real()
        tmp = self._handoff.get(p)
        if tmp and os.path.isfile(tmp):
            return tmp
        if p not in self._extracting:
            self._extracting.add(p)
            name = os.path.basename(archives.split(p)[1])
            self.status.setText(f"Getting “{name}” out of the archive…")

            def job():
                try:
                    f, why = archives.extract_to_temp(p, keep_name=True)
                except Exception as e:  # noqa: BLE001
                    f, why = None, str(e)
                self.bus.extracted.emit(p, f or "", "" if f else str(why), then)
            threading.Thread(target=job, daemon=True).start()
        return None

    def _on_extracted(self, p, f, why, then):
        self._extracting.discard(p)
        if not f:
            self.status.setText(f"⚠ {why}")
            return
        self._handoff[p] = f
        if then is not None:
            then(f)
        else:
            self.status.setText(f"“{os.path.basename(f)}” is ready: drag it again.")

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == e.Type.PaletteChange:  # light/dark switched while open: outlines follow
            new = style(QApplication.palette())
            if new != getattr(self, "_style", new):
                self._style = new
                self.setStyleSheet(new)
        if e.type() == e.Type.WindowStateChange and not self.isMinimized() and self._pics_timer.isActive():
            QTimer.singleShot(500, self._count_pictures)  # back on screen: bring the picture count up to date

    def closeEvent(self, e):
        self._stop_video()
        if self._fullpic is not None:
            self._fullpic.close()
        if getattr(self, "_volume", None) is not None:
            programs.set_state("volume", int(self._volume))
        try:
            self._save_view()
        except OSError as err:
            print("view not saved:", err, file=sys.stderr)
        for f in self._handoff.values():  # temp copies of archive members (in RAM on this PC)
            shutil.rmtree(os.path.dirname(f), ignore_errors=True)
        self._handoff.clear()
        self._save_watch()  # folders made while the window was open are checked at the next start
        if self._cols_timer.isActive():
            self._cols_timer.stop(); self._save_columns()
        super().closeEvent(e)

    def _context_menu(self, view, pos):
        if view is self.table:
            it = self.table.itemAt(pos)
            if it is None:
                return
            self.table.selectRow(it.row())
        else:
            it = self.grid.itemAt(pos)
            if it is None:
                return
            self.grid.setCurrentItem(it)
        p = self.sel_path()
        if not p:
            return
        name = os.path.basename(archives.split(p)[1] if archives.split(p) else p)
        ext = programs.ext_of(name)
        menu = QMenu(self)
        menu.addAction("Open folder", self.open_folder)
        menu.addAction("Open file", self.open_file)
        sub = menu.addMenu("Open with")
        try:
            apps = programs.installed()
            offered = programs.for_file(name, apps)
        except Exception as e:  # a broken launcher must not break the menu
            print("open with:", e, file=sys.stderr)
            apps, offered = [], []
        for a in offered:
            sub.addAction(QAction(a.label, sub, triggered=lambda _=False, a=a: self.open_with(a)))
        if offered:
            sub.addSeparator()
        sub.addAction(f"Choose programs for .{ext or '(no extension)'} files…",
                      lambda: self.choose_programs(ext, apps, offered))
        menu.addSeparator()
        menu.addAction("Copy path", self.copy_path)
        menu.addAction("Related files", self.show_related)
        menu.addAction("Find exact copies", self.find_copies)
        self._last_menu = menu  # (tests look at it)
        if not os.environ.get("FINDFILES_NO_MENU_EXEC"):
            menu.exec(view.viewport().mapToGlobal(pos))

    def open_with(self, app):
        f = self.handoff_path(then=lambda f: self._launch(app, f))
        if f:
            self._launch(app, f)

    def _launch(self, app, f):
        try:
            programs.launch(app, f)
            self.status.setText(f"Opening “{os.path.basename(f)}” in {app.label}…")
        except (OSError, ValueError) as e:
            self.status.setText(f"⚠ Could not start {app.label}: {e}")

    def choose_programs(self, ext, apps, offered):
        dlg = ProgramPicker(self, ext, apps, offered)
        if dlg.exec() == QDialog.Accepted:
            programs.set_pick(ext, dlg.picked())
            self.status.setText(f"Saved: {len(dlg.picked())} program(s) for .{ext} files.")

    def copy_path(self):
        p = self.sel_path()
        if p:
            QGuiApplication.clipboard().setText(p)
            self.status.setText("Path copied.")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("Find My Files")
    w = Main()
    w.show()
    sys.exit(app.exec())
