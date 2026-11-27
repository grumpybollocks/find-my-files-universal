"""Picture cache: one small PNG + model size (mm) per previewable work file.

- Pictures come from previews.render() (the same code the window uses).
- Key = sha1(path, size, mtime): a changed file gets a new picture automatically.
- Cache: ~/.cache/findfiles/thumbs/*.webp (older: *.png) + thumbs.db. Never writes anywhere else.

Run the builder (gentle, single instance):  nice -n 19 ionice -c3 python3 -I thumbs.py
See docs/PLAN-model-library.md.
"""
import fcntl
import hashlib
import os
import re
import sqlite3
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indexer  # noqa: E402

_BASE = os.environ.get("FINDFILES_CACHE") or os.path.expanduser("~/.cache/findfiles")  # tests use a temp dir
CACHE_DIR = os.path.join(_BASE, "thumbs")
DB_PATH = os.path.join(_BASE, "thumbs.db")
LOCK_PATH = os.path.join(_BASE, "thumbs.lock")
LOG_PATH = os.path.join(_BASE, "thumbs.log")
SIZE = 256
QUALITY = 80                # WebP: about 5x smaller than PNG at this size, no visible difference
PIC_EXTS = (".webp", ".png")  # picture files this cache owns (.png = made before 1.2)
MIN_FREE_MB = 3000          # pause while the PC has less free RAM than this
GIVE_UP_S = 3600            # low RAM for this long: stop (the next app start tries again)
PAUSE_S = 0.2               # rest between files (keeps the PC responsive)
MODEL_EXTS = ("stl", "obj", "3mf", "glb", "3dm", "f3d", "lys", "lyt", "pwscene", "cxdlp", "pm3m", "pwmx")
OTHER_EXTS = ("cdr", "dxf", "svg", "ai", "gcode", "stm",
              "mp4", "mkv", "mov", "avi", "webm", "m4v", "wmv", "flv", "mpg", "mpeg", "m2ts", "3gp")  # videos: one frame
DRAW_VERSION = 3  # bump when previews.py draws differently: pictures are redrawn in the background
                  # (2 = 3/4 view, 3 = blank Lychee/G-code pictures dropped, 2026-11-11). Old pictures + sizes stay usable until then.
_DIMS = re.compile(r"Size: ([\d.]+) × ([\d.]+) × ([\d.]+)")


def key(path, size, mtime):
    return hashlib.sha1(f"{path}\0{size}\0{mtime}".encode("utf-8", "surrogateescape")).hexdigest()


def connect():
    os.makedirs(CACHE_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")  # the window reads while the builder writes
    db.execute("""CREATE TABLE IF NOT EXISTS thumbs(
        k TEXT PRIMARY KEY, path TEXT, file TEXT, w REAL, d REAL, h REAL, note TEXT, made REAL)""")
    db.execute("CREATE INDEX IF NOT EXISTS thumbs_path ON thumbs(path)")
    # separate table, so an older builder still running keeps working after an update
    db.execute("CREATE TABLE IF NOT EXISTS drawn(k TEXT PRIMARY KEY, v INTEGER)")
    return db


def current(db, path, size, mtime):
    """True if this file version has a picture made by the current drawing code."""
    r = db.execute("SELECT v FROM drawn WHERE k=?", (key(path, size, mtime),)).fetchone()
    return bool(r) and r[0] == DRAW_VERSION


def dims_from_note(note):
    m = _DIMS.search(note or "")
    return tuple(float(x) for x in m.groups()) if m else (None, None, None)


_FMT = None


def fmt():
    """(".webp", "WEBP") where Qt can write WebP, else (".png", "PNG")."""
    global _FMT
    if _FMT is None:
        from PySide6.QtGui import QImageWriter
        ok = b"webp" in [bytes(f).lower() for f in QImageWriter.supportedImageFormats()]
        _FMT = (".webp", "WEBP") if ok else (".png", "PNG")
    return _FMT


def _save(img, k):
    """Write one picture into the cache, all or nothing. Returns its path."""
    ext, name = fmt()
    f = os.path.join(CACHE_DIR, k + ext)
    tmp = f"{f}.{os.getpid()}.{threading.get_ident()}.tmp{ext}"  # window + builder may save the same one
    if not img.save(tmp, name, QUALITY if name == "WEBP" else -1):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise OSError(f"could not save picture {tmp}")
    os.replace(tmp, f)
    return f


def lookup(db, path, size, mtime):
    """(png_path or None, (w, d, h) or None, note) if cached for this exact file version, else None."""
    r = db.execute("SELECT file, w, d, h, note FROM thumbs WHERE k=?", (key(path, size, mtime),)).fetchone()
    if not r:
        return None
    f = r[0] if r[0] and os.path.exists(r[0]) else None
    dims = (r[1], r[2], r[3]) if r[1] is not None else None
    return f, dims, r[4]


def store(db, path, size, mtime, img, note):
    """Save one rendered preview (img may be None = 'no preview', remembered so we don't retry)."""
    k = key(path, size, mtime)
    f = None
    if img is not None:
        if max(img.width(), img.height()) > SIZE:
            from PySide6.QtCore import Qt
            img = img.scaled(SIZE, SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        f = _save(img, k)
    w, d, h = dims_from_note(note)
    old = db.execute("SELECT file FROM thumbs WHERE path=? AND k!=?", (path, k)).fetchall()
    with db:  # all or nothing: a failed write must not leave the database locked for the builder
        db.execute("DELETE FROM drawn WHERE k IN (SELECT k FROM thumbs WHERE path=? AND k!=?)", (path, k))
        db.execute("DELETE FROM thumbs WHERE path=? AND k!=?", (path, k))
        db.execute("INSERT OR REPLACE INTO drawn VALUES(?,?)", (k, DRAW_VERSION))
        db.execute("INSERT OR REPLACE INTO thumbs VALUES(?,?,?,?,?,?,?,?)",
                   (k, path, f, w, d, h, note, time.time()))
    for (of,) in old:  # the file changed: drop its older picture (after the commit)
        if of and of != f and os.path.exists(of):
            os.remove(of)
    return f


TRANSIENT = ("File not found", "Archive not found", "took over", "MemoryError")


def worth_saving(img, note):
    """A 'no preview' caused by something temporary (drive gone, timeout, low RAM) is not remembered,
    so the picture is tried again later."""
    return img is not None or not any(t in (note or "") for t in TRANSIENT)


def unchanged(path, size, mtime):
    """The file on disk is still the version the file list knows (else its picture would go under
    the wrong key)."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return st.st_size == size and abs(st.st_mtime - mtime) <= 0.001


def dims_for(db, rows):
    """{path: (w, d, h)} for rows (path, name, loc, size, mtime) whose current version has a known size."""
    want = {key(r[0], r[3], r[4]): r[0] for r in rows}
    out, ks = {}, list(want)
    for i in range(0, len(ks), 500):
        part = ks[i:i + 500]
        q = "SELECT k, w, d, h FROM thumbs WHERE w IS NOT NULL AND k IN (%s)" % ",".join("?" * len(part))
        for k, w, d, h in db.execute(q, part):
            out[want[k]] = (w, d, h)
    return out


def keys_by_largest_side(db, lo, hi):
    """{cache key: path} for cached models whose largest side is between lo and hi (mm)."""
    return dict(db.execute("SELECT k, path FROM thumbs WHERE w IS NOT NULL AND max(w, d, h) BETWEEN ? AND ?",
                           (lo, hi)).fetchall())


def todo(db, limit=None, outdated=True):
    """Work files without a picture for their current version (3D models first, small first),
    then (outdated=True) files whose picture was drawn by older drawing code."""
    idx = sqlite3.connect(f"file:{indexer.DB_PATH}?mode=ro", uri=True, timeout=10)
    exts = MODEL_EXTS + OTHER_EXTS
    q = ("SELECT path, size, mtime, ext FROM files WHERE kind='work' AND ext IN (%s) AND path NOT LIKE ?"
         % ",".join("?" * len(exts)))
    rows = idx.execute(q, (*exts, "%::%")).fetchall()
    idx.close()
    have = {r[0] for r in db.execute("SELECT k FROM thumbs")}
    fresh = {r[0] for r in db.execute("SELECT k FROM drawn WHERE v=?", (DRAW_VERSION,))}
    new, old = [], []
    for r in rows:
        k = key(r[0], r[1], r[2])
        if k not in have:
            new.append(r)
        elif outdated and k not in fresh:
            old.append(r)
    order = lambda r: (r[3] not in MODEL_EXTS, r[1])  # noqa: E731  models first, small files first
    out = sorted(new, key=order) + sorted(old, key=order)
    return out[:limit] if limit else out


def prune(db):
    """Forget pictures of files that are no longer in the file list (moved, renamed, deleted), and
    picture files nothing points to. Files on an unplugged drive stay in the file list, so their
    pictures are kept. Only touches the cache folder. Returns how many were removed."""
    try:
        idx = sqlite3.connect(f"file:{indexer.DB_PATH}?mode=ro", uri=True, timeout=10)
        listed = {r[0] for r in idx.execute("SELECT path FROM files")}
        idx.close()
    except sqlite3.Error:
        return 0
    if not listed:  # an empty/broken file list must never wipe the cache
        return 0
    dead = [(k, f) for k, p, f in db.execute("SELECT k, path, file FROM thumbs") if p not in listed]
    with db:
        db.executemany("DELETE FROM thumbs WHERE k=?", [(k,) for k, _ in dead])
        db.executemany("DELETE FROM drawn WHERE k=?", [(k,) for k, _ in dead])
    keep = {f for (f,) in db.execute("SELECT file FROM thumbs WHERE file IS NOT NULL")}
    removed = 0
    for _, f in dead:  # pictures of files that are gone: remove now
        if f and f not in keep and os.path.exists(f):
            os.remove(f)
            removed += 1
    for name in os.listdir(CACHE_DIR):
        f = os.path.join(CACHE_DIR, name)
        if name.endswith(PIC_EXTS) and f not in keep:  # (temp files end the same way and are old leftovers)
            try:
                if time.time() - os.path.getmtime(f) < 600:  # maybe being saved right now (file before row)
                    continue
                os.remove(f)
                removed += 1
            except OSError:
                pass
    return max(len(dead), removed)


def old_format_left(db):
    """How many saved pictures are still in the big PNG format."""
    if fmt()[0] == ".png":
        return 0
    return db.execute("SELECT COUNT(*) FROM thumbs WHERE file LIKE '%.png'").fetchone()[0]


def shrink(db, log=print, wait=time.sleep, pause=0.01):
    """One-off: rewrite pictures saved as PNG in the smaller format (same picture, same key).
    Only our own cache folder. Safe to stop at any point: each picture is new file -> row -> old file."""
    if fmt()[0] == ".png":
        return 0
    from PySide6.QtGui import QImage
    rows = db.execute("SELECT k, file FROM thumbs WHERE file LIKE '%.png'").fetchall()
    done = 0
    for k, old in rows:
        if os.path.dirname(old) != CACHE_DIR:
            continue  # never touch a file outside the cache
        try:
            img = QImage(old)
            if img.isNull():  # picture file missing / unreadable: forget it, it is drawn again
                with db:
                    db.execute("DELETE FROM thumbs WHERE k=?", (k,))
                    db.execute("DELETE FROM drawn WHERE k=?", (k,))
                continue
            new = _save(img, k)
            with db:
                cur = db.execute("UPDATE thumbs SET file=? WHERE k=? AND file=?", (new, k, old))
            if cur.rowcount and new != old:
                os.remove(old)
            done += 1
        except (OSError, sqlite3.Error) as e:
            log(f"could not shrink {old}: {e}")
        if done % 20 == 0:
            wait(pause)
        if done and done % 2000 == 0:
            log(f"shrunk {done}/{len(rows)}")
    if done:
        log(f"shrunk {done} pictures to {fmt()[0]}")
    return done


def disk_use():
    """(bytes, number of picture files) of the picture cache."""
    total = n = 0
    try:
        with os.scandir(CACHE_DIR) as it:
            for e in it:
                if e.name.endswith(PIC_EXTS):
                    total += e.stat().st_size
                    n += 1
    except OSError:
        pass
    return total, n


def free_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable"):
                return int(line.split()[1]) // 1024
    return 0


def _wait_for_ram(log, wait):
    """Block while free RAM is under MIN_FREE_MB. False = waited GIVE_UP_S, give up."""
    waited = 0
    while free_mb() < MIN_FREE_MB:
        if waited >= GIVE_UP_S:
            log(f"free RAM stayed under {MIN_FREE_MB} MB for {GIVE_UP_S // 60} min: stopping")
            return False
        if waited == 0:
            log(f"waiting: free RAM {free_mb()} MB")
        wait(30)
        waited += 30
    return True


def clear_all():
    """Settings > Rebuild + 'also redraw pictures': forget every saved picture and size.
    Returns (ok, message). Only when no picture builder runs (its lock is free); only our own cache."""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    with open(LOCK_PATH, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False, "The picture maker is busy right now; pictures were kept. Try again in a minute."
        try:
            db = connect()
            try:
                def forget():
                    with db:  # rows go first: a picture file without a row is never shown
                        db.execute("DELETE FROM thumbs")
                        db.execute("DELETE FROM drawn")
                forget()
                n = 0
                for name in os.listdir(CACHE_DIR):
                    if name.endswith(PIC_EXTS):
                        try:
                            os.remove(os.path.join(CACHE_DIR, name))
                            n += 1
                        except FileNotFoundError:
                            pass
                forget()  # the window may have saved one picture meanwhile: its file is gone, so its row goes too
            finally:
                db.close()
        except (OSError, sqlite3.Error) as e:
            return False, f"Pictures could not be cleared ({e}); nothing else changed."
        return True, f"{n:,} pictures cleared; they are drawn again in the background."


def build(limit=None, log=print, wait=time.sleep):
    """Make missing pictures one at a time. Returns number made. Single instance via a lock file."""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another builder is already running")
        return 0
    db = connect()
    gone = prune(db)
    if gone:
        log(f"removed {gone} pictures of files no longer in the file list")
    jobs = todo(db, limit)
    log(f"{len(jobs)} pictures to make")
    no_webp = os.path.join(_BASE, "no-webp")  # this PC's Qt can't write WebP: don't load Qt just to find out again
    old = None if os.path.exists(no_webp) else \
        db.execute("SELECT 1 FROM thumbs WHERE file LIKE '%.png' LIMIT 1").fetchone()
    if not (jobs or old) or not _wait_for_ram(log, wait):  # don't even load Qt when there's nothing to do
        db.close()
        return 0
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841 (QImage needs it)
    if fmt()[0] == ".png" and not os.path.exists(no_webp):
        open(no_webp, "w").close()
    if not jobs:
        shrink(db, log, wait)
        db.close()
        return 0
    import previews
    made = 0
    for path, size, mtime, _ in jobs:
        if not _wait_for_ram(log, wait):
            break
        try:  # one bad file (or an unplugged drive) must not stop the whole run
            if not unchanged(path, size, mtime):
                continue  # changed (or gone) since the file list was made: the next run gets it
            img, note = previews.render(path, SIZE)
            if not worth_saving(img, note):
                continue
            store(db, path, size, mtime, img, note)
        except (OSError, sqlite3.Error) as e:
            log(f"skipped {path}: {e}")
            continue
        made += 1
        wait(PAUSE_S)
        if made % 200 == 0:
            log(f"{made}/{len(jobs)}")
    shrink(db, log, wait)
    db.close()
    log(f"done: {made} made")
    return made


def stats():
    db = connect()
    n, pics = db.execute("SELECT count(*), count(file) FROM thumbs").fetchone()
    pending = len(todo(db, outdated=False))  # files with no picture/size at all
    db.close()
    return n, pics, pending


if __name__ == "__main__":
    build(log=lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
