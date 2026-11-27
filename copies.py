"""Find exact copies of one file - by FULL-FILE hash, never by name or size alone.

Only files with exactly the same size are read (a different size can't be the same file),
so a check normally reads just a handful of files. Read-only.
"""
import hashlib
import os
import sqlite3

import archives

ASK_ABOVE_BYTES = 20_000_000_000  # the window asks before reading more than this


MIN_SIZE = 1  # an empty file "matches" every other empty file - meaningless


def candidates(path, db_path):
    """Other indexed files with exactly the same size (real files only, not archive members).
    Index lookup only (fast, safe on the GUI thread); find() checks the files really exist."""
    size = os.path.getsize(path)
    if size < MIN_SIZE:
        return size, []
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    rows = db.execute("SELECT path, loc, kind FROM files WHERE size=? AND path!=?", (size, path)).fetchall()
    db.close()
    return size, [r for r in rows if not archives.split(r[0])]


def file_hash(path, progress=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 << 20), b""):
            h.update(block)
            if progress:
                progress(len(block))
    return h.hexdigest()


def find(path, db_path, progress=None):
    """Returns dict: identical=[(path, loc, kind)], different=int (same size, other content), size=int."""
    if archives.split(path):
        raise ValueError("Files inside archives can't be compared yet - extract it first")
    size, cands = candidates(path, db_path)
    if not cands:
        return {"identical": [], "different": 0, "size": size}
    ref = file_hash(path, progress)
    same, diff = [], 0
    for p, loc, kind in cands:
        try:
            if not os.path.isfile(p) or os.path.samefile(p, path):  # gone, or a hard link to the same file
                continue
            if file_hash(p, progress) == ref:
                same.append((p, loc, kind))
            else:
                diff += 1
        except OSError:
            diff += 1
    return {"identical": same, "different": diff, "size": size}
