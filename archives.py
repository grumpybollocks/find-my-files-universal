"""Files inside .zip / .rar / .7z archives (many models arrive as TransferNow / WeTransfer zips).

- list_members(): names + sizes of the files inside an archive (zip: Python zipfile; rar/7z: `7z l`).
  Listings are cached in archives.db by (path, size, mtime), so an unchanged archive is never re-read.
- An archive member is addressed as  "<archive path>::<member name>"  everywhere in the app.
- extract_to_temp(): copies ONE member into a temp file (for previews only) and returns its path.
  Never extracts by the member's own name, so "../" names can't write anywhere.

Read-only on the archives themselves.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import zipfile

ARCHIVE_EXTS = {"zip", "rar", "7z"}
SEP = "::"
# archives in these locations are software/backups, not work -> not opened
SKIP_LOCS = ("Software", "Music", "Zip", "Archive")  # set from the settings ("low") by indexer.apply_settings
MAX_PREVIEW_MEMBER = 300_000_000  # bytes extracted at most for one preview
_SPLIT = re.compile(r"^(.*?\.(?:zip|rar|7z))::(.+)$", re.I)


EXTRACT_TIMEOUT = 60  # seconds for 7z to extract one member for a preview


def split(path):
    """'/x/a.zip::dir/m.stl' -> ('/x/a.zip', 'dir/m.stl'); a normal path -> None."""
    m = _SPLIT.match(path)
    return (m.group(1), m.group(2)) if m else None


def _list_zip(path):
    out = []
    with zipfile.ZipFile(path) as z:
        for i in z.infolist():
            if i.is_dir():
                continue
            try:
                mt = time.mktime(i.date_time + (0, 0, -1))
            except (OverflowError, ValueError):
                mt = 0
            out.append((i.filename, i.file_size, mt))
    return out


def _list_7z(path):
    r = subprocess.run(["7z", "l", "-slt", "-ba", "-p", "--", path], capture_output=True, text=True,
                       errors="replace", timeout=60, stdin=subprocess.DEVNULL)
    out, cur = [], {}
    for line in (r.stdout + "\n").splitlines():
        if not line.strip():
            if cur.get("Path") and cur.get("Folder") != "+" and not cur.get("Attributes", "").startswith("D"):
                mt = 0
                try:
                    mt = time.mktime(time.strptime(cur.get("Modified", "")[:19], "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    pass
                out.append((cur["Path"], int(cur.get("Size") or 0), mt))
            cur = {}
        elif " = " in line:
            k, v = line.split(" = ", 1)
            cur[k.strip()] = v
    return out


def list_members(path):
    """[(member, size, mtime)], or None when the listing failed (timeout, read error):
    a failure must not be cached as 'empty', or the archive's files would never be indexed."""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    try:
        if ext == "zip" and zipfile.is_zipfile(path):
            return _list_zip(path)
        if shutil.which("7z"):
            return _list_7z(path)
    except (zipfile.BadZipFile, OSError, subprocess.TimeoutExpired, RuntimeError, ValueError):
        return None
    return []


class Cache:
    """archives.db: archive path -> (size, mtime, json members). Written only when something new was listed."""

    def __init__(self, db_dir):
        self.path = os.path.join(db_dir, "archives.db")
        os.makedirs(db_dir, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.execute("CREATE TABLE IF NOT EXISTS a(path TEXT PRIMARY KEY, size INTEGER, mtime REAL, members TEXT)")
        self.dirty = False

    def members(self, path, size, mtime):
        r = self.db.execute("SELECT size, mtime, members FROM a WHERE path=?", (path,)).fetchone()
        if r and r[0] == size and r[1] == mtime:
            return json.loads(r[2])
        mem = list_members(path)
        if mem is None:  # failed: try again next time, keep any older listing
            return json.loads(r[2]) if r else []
        self.db.execute("INSERT OR REPLACE INTO a VALUES(?,?,?,?)", (path, size, mtime, json.dumps(mem)))
        self.dirty = True
        return mem

    def close(self, keep_paths=None):
        if keep_paths is not None:  # forget archives that no longer exist on a scanned drive
            known = {p for (p,) in self.db.execute("SELECT path FROM a")}
            gone = known - keep_paths
            if gone:
                self.db.executemany("DELETE FROM a WHERE path=?", [(p,) for p in gone])
                self.dirty = True
        if self.dirty:
            self.db.commit()
        self.db.close()


def member_rows(rows, db_dir, norm, missing_labels=(), forget=True):
    """For every archive row in a work location, return index rows for the WORK files inside it
    (3D / print / 2D / laser / scan types from filetypes.py)."""
    import filetypes
    work = set(filetypes.WORK_EXTS)
    cache = Cache(db_dir)
    out, seen = [], set()
    for path, name, _key, ext, size, mtime, loc, kind in rows:
        if ext not in ARCHIVE_EXTS or any(s in loc for s in SKIP_LOCS):
            continue
        seen.add(path)
        for member, msize, mmtime in cache.members(path, size, mtime):
            base = member.replace("\\", "/").rsplit("/", 1)[-1]
            if not base or base.startswith("._") or "__MACOSX/" in member:
                continue
            mext = os.path.splitext(base)[1].lower().lstrip(".")
            if mext not in work:  # only work files inside archives (not fonts, icons, firmware...)
                continue
            out.append((path + SEP + member, base, norm(base), mext, msize, mmtime or mtime, loc, kind))
    # don't forget cached listings of archives on unplugged drives (or during a watch-mode update,
    # which only sees a few folders)
    cache.close(None if (missing_labels or not forget) else seen)
    return out


def extract_to_temp(path, keep_name=False):
    """Extract one member to a temp file (keeps its extension). Returns (temp_path, temp_dir) or (None, reason)."""
    sp = split(path)
    if not sp:
        return None, "Not an archive member"
    arc, member = sp
    if not os.path.isfile(arc):
        return None, "Archive not found (moved, deleted, or its drive is unplugged)"
    ext = os.path.splitext(member)[1]
    tmpdir = tempfile.mkdtemp(prefix="findfiles-")
    name = os.path.basename(member.replace("\\", "/")) if keep_name else ""
    name = "".join(c for c in name if c not in "\0/") or "member" + ext  # real name when handed to a program
    dst = os.path.join(tmpdir, name)
    try:
        if arc.lower().endswith(".zip") and zipfile.is_zipfile(arc):
            with zipfile.ZipFile(arc) as z:
                info = z.getinfo(member)
                if info.file_size > MAX_PREVIEW_MEMBER:
                    shutil.rmtree(tmpdir)
                    return None, "File inside the archive is too big to preview"
                with z.open(info) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
        else:
            with open(dst, "wb") as out:
                p = subprocess.Popen(["7z", "e", "-so", "-p", "--", arc, member], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
                # watchdog: a big solid archive can decode for minutes before the first byte arrives
                timer = threading.Timer(EXTRACT_TIMEOUT, p.kill)
                timer.start()
                written, too_big = 0, False
                try:
                    while True:
                        chunk = p.stdout.read(1 << 20)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_PREVIEW_MEMBER:
                            too_big = True
                            p.kill()
                            break
                        out.write(chunk)
                finally:
                    timer.cancel()
                    p.stdout.close()
                    p.wait()
                if too_big:
                    shutil.rmtree(tmpdir)
                    return None, "File inside the archive is too big to preview"
                if p.returncode and p.returncode < 0:  # killed by the watchdog
                    shutil.rmtree(tmpdir)
                    return None, f"Reading this file from the archive took over {EXTRACT_TIMEOUT} s - skipped"
                if p.returncode and p.returncode >= 2:  # 7z fatal error (CRC, truncated): don't show half a file
                    shutil.rmtree(tmpdir)
                    return None, "Could not read this file from the archive (damaged or password?)"
            if os.path.getsize(dst) == 0:
                shutil.rmtree(tmpdir)
                return None, "Could not read this file from the archive (damaged or password?)"
    except Exception as e:  # damaged member (zlib.error, EOFError...) must not kill the preview thread
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None, f"Could not read this file from the archive ({type(e).__name__})"
    return dst, tmpdir
