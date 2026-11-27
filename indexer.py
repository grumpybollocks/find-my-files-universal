"""Find My Files - index builder.

Walks the work drives and stores every file name in a small SQLite database
(~/.local/share/findfiles/index.db). Read-only on the drives: it never moves,
renames or deletes anything.

Run directly to rebuild:  python3 indexer.py
"""
import hashlib
import os
import sqlite3
import sys
import threading
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sibling modules (archives) even with python -I

HOME = os.path.expanduser("~")
# FINDFILES_DATA: another folder for the file list (the tests use a temp dir, never the real list)
DB_DIR = os.environ.get("FINDFILES_DATA") or os.path.join(HOME, ".local/share/findfiles")
DB_PATH = os.path.join(DB_DIR, "index.db")

import roots as _roots  # noqa: E402  (which folders: ~/.config/findfiles/roots.json, per PC)

# (root folder, label shown in the app, kind) - kind: "work" = real files, "copy" = backups / hold (hidden unless asked)
# LOOSE = loose files sitting directly in these folders (no recursion).
# Filled from the settings at start and after Save in Settings (apply_settings); tests set them directly.
ROOTS, LOOSE, NEVER = [], [], []
EXCLUDE = []     # unticked folders: left out even inside a ticked one
SKIP_NAMES = set()  # folder names never read anywhere (casefolded): built-in SKIP_DIRS + the settings' list
LABEL_RENAMES = {}  # old label -> new (Settings): an unplugged drive's kept files follow the new name
SETTINGS_PROBLEM = ""


def _under(path, prefixes):
    return any(path == n or path.startswith(n.rstrip("/") + "/") for n in prefixes)


def blocked(root):
    """True if root is (or leads, via a symlink or bind mount, into) a never-index drive."""
    return _under(os.path.normpath(root), NEVER) or _roots.on_never_device(root, NEVER)


def apply_settings(settings=None):
    """Use these settings (default: load the file). Never-index folders are dropped even if listed."""
    global ROOTS, LOOSE, NEVER, SETTINGS_PROBLEM, EXCLUDE, SKIP_NAMES, WATCH_SKIP
    if settings is None:
        settings, SETTINGS_PROBLEM = _roots.load()
    NEVER = list(settings["never"])
    EXCLUDE = _roots.excluded(settings)
    SKIP_NAMES = {n.casefold() for n in settings.get("skip", _roots.DEFAULT_SKIP)}
    WATCH_SKIP = tuple(settings.get("low", _roots.DEFAULT_LOW))
    import archives
    archives.SKIP_LOCS = WATCH_SKIP  # archives in low-priority locations are not opened
    r, loose = _roots.active(settings)
    # text only here (runs on the window's thread: a sleeping HDD must not freeze it);
    # build() checks devices / links again before reading anything
    ROOTS = [x for x in r if not _under(os.path.normpath(x[0]), NEVER)]
    LOOSE = [x for x in loose if not _under(os.path.normpath(x[0]), NEVER)]


def skipped(name):
    """True for a folder name that is never read (program / game / asset folders, bins, caches)."""
    low = name.casefold()
    return (name in SKIP_DIRS or low in SKIP_NAMES or name.endswith("_autosave")
            or low.startswith("compatdata"))  # Proton prefixes and their dated backups: program files


def _own():
    """Folders a containing location must not read: other locations (they list their own files),
    unticked folders and never-index mounts."""
    return ({os.path.normpath(r) for r, _l, _k in ROOTS} | {os.path.normpath(x) for x in EXCLUDE}
            | _roots.never_mounts(NEVER))


def _loose():
    """LOOSE entries as (path, label, kind); older 2-item entries are work folders."""
    return [(x[0], x[1], x[2] if len(x) > 2 else "work") for x in LOOSE]


apply_settings()

SKIP_DIRS = {
    "$RECYCLE.BIN", "System Volume Information", ".Trash-1000", "node_modules",
    ".git", "__pycache__", ".venv", "venv", "site-packages", ".cache", "lost+found",
    "old dumps (not 3D work)",  # source code / configs parked on purpose
    "System-Fixes",             # scripts
    "AppPrefixes",              # Storage/Backups/AppPrefixes = Wine/app snapshots (program files, not work)
    # games / Proton prefixes when a whole drive is searched (program files; compatdata may be an automount)
    "steamapps", "SteamLibrary", ".SteamLibrary", "compatdata", "shadercache",
}

# Romanian diacritics (both cedilla and comma forms) -> plain letters
_RO = str.maketrans("ăâîșşțţĂÂÎȘŞȚŢ", "aaissttAAISSTT")


JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
INDEX_ARCHIVES = True  # also list the files inside zip/rar/7z archives (see archives.py)
SKIPPED_NAMES = []  # raw paths skipped because their name isn't valid UTF-8 (filled by build)
TICK_EVERY = 2000  # build(tick=...) is called with (files found so far, location) this often


def norm(s: str) -> str:
    s = s.translate(_RO).lower()
    if s.isascii():  # nearly every name: nothing to strip (saves ~1 s per full scan of 250k names)
        return s
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def _walk(root, label, kind, rows, recurse=True, dirs=None, skip=frozenset(), tick=None):
    """skip = other scanned locations inside this one: their files belong to them (no double listing).
    Folders are read from a list, not by calling itself: any depth works (a chain of 1,000+ folders used
    to stop the whole scan)."""
    todo = [root]
    while todo:
        folder = todo.pop()
        if dirs is not None:
            dirs.append(folder)
        try:
            it = os.scandir(folder)
        except OSError:
            continue
        subs = []
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if recurse and not skipped(e.name) and e.path not in skip:
                            subs.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        if e.name.startswith("._") or e.name in JUNK_FILES:
                            continue  # macOS metadata copies ("._name.jpg" = AppleDouble), not real files
                        st = e.stat(follow_symlinks=False)
                        name = e.name
                        ext = os.path.splitext(name)[1].lower().lstrip(".")
                        try:
                            e.path.encode("utf-8")  # NTFS names that aren't valid UTF-8 can't go in SQLite
                        except UnicodeEncodeError:
                            SKIPPED_NAMES.append(e.path.encode("utf-8", "surrogateescape"))
                            continue
                        rows.append((e.path, name, norm(name), ext, st.st_size, st.st_mtime, label, kind))
                        if tick is not None and len(rows) % TICK_EVERY == 0:
                            tick(len(rows), label)
                except OSError:
                    continue
        todo.extend(reversed(subs))  # first sub-folder read next (same order as before)


# Watch mode: folders of these locations are NOT watched (thousands of folders that hardly change).
# Backups / hold folders (kind "copy") are never watched either.
WATCH_SKIP = tuple(_roots.DEFAULT_LOW)  # low-priority locations (settings "low"): not watched live
WATCH_DIRS = []  # filled by build(): folders the window watches for new files
CHECK_DIRS = []  # filled by build(): every folder read (all locations); quick() checks these at start


def _watched(label, kind):
    return kind == "work" and not any(s.lower() in label.lower() for s in WATCH_SKIP)


_FP_MOD = 1 << 128


def _row_hash(r):
    """128-bit hash of (path, key, size, mtime, loc, kind) of one row (path,name,key,ext,size,mtime,loc,kind,...).
    A changed label/kind/norm() also counts as a change."""
    b = f"{r[0]}\0{r[2]}\0{r[4]}\0{r[5]}\0{r[6]}\0{r[7]}".encode("utf-8", "surrogateescape")
    return int.from_bytes(hashlib.blake2b(b, digest_size=16).digest(), "big")


def _fp_text(total):
    return f"v2 schema {SCHEMA} {total:032x}"


def _fp_total(text):
    """The sum stored by _fp_text(), or None for an older / other-schema fingerprint."""
    parts = (text or "").split()
    if len(parts) == 4 and parts[0] == "v2" and parts[2] == str(SCHEMA):
        try:
            return int(parts[3], 16)
        except ValueError:
            return None
    return None


def _fingerprint(rows):
    """Sum of the row hashes: the same in any order, so a watch-mode update can subtract the rows it
    deletes and add the ones it writes, without re-reading 250k rows. Same for full builds and updates."""
    return _fp_text(sum(map(_row_hash, rows)) % _FP_MOD)


SCHEMA = 3  # 3 = 'seen' column (when a file first appeared). Bump when the meaning of a column changes (forces one rebuild)


def connect_ro():
    """Read-only connection to the index."""
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)


def _drive_missing(root):
    """True if root lives on a /run/media or /mnt drive that is not mounted right now."""
    if root.startswith("/run/media/"):
        drive = "/".join(root.split("/")[:5])  # /run/media/<user>/<drive>
    elif root.startswith("/mnt/"):
        drive = "/".join(root.split("/")[:3])  # /mnt/<drive>
    else:
        return False
    return not os.path.ismount(drive)


def build(progress=None, force=False, tick=None):
    """Rescan everything. Returns (number_of_files, [labels of unplugged locations]).

    - Locations on an unplugged drive keep their previous entries (so they can still be found).
    - Nothing is written when no file changed (the DB sits on the Btrfs/Timeshift system disk).
    - force=True (Settings > Rebuild): archives are listed again and the list is always written.
    - tick(files_so_far, location) is called every TICK_EVERY files (the window's progress / time left).
    - A changed index is written to a temp file and swapped in atomically.
    """
    import glob
    for stale in glob.glob(DB_PATH + ".tmp*"):  # left by a crashed/killed earlier build
        try:
            if time.time() - os.path.getmtime(stale) > 600:
                os.remove(stale)
        except OSError:
            pass
    global _FULL_TIME
    t0 = time.time()
    target = DB_PATH  # the list this scan belongs to (fixed now, not when it ends)
    n, missing = _build(progress, force, tick, target, DB_DIR)
    if DB_PATH != target:  # the list moved during the scan (only in tests): leave the new one alone
        return n, missing
    _FULL_TIME = t0
    try:
        save_watch(t0, missing)  # lets the next start check folders for changes instead of reading every file
    except OSError as e:
        print("folder list not saved:", e, file=sys.stderr)
    return n, missing


def _build(progress, force, tick, target, db_dir):
    renames = dict(LABEL_RENAMES)  # snapshot: Settings may add one while this scan runs
    SKIPPED_NAMES.clear()
    rows, missing, dirs, every = [], [], [], []
    nested = _own()
    for root, label, kind, recurse in [(r, l, k, True) for r, l, k in ROOTS] + \
            [(r, l, k, False) for r, l, k in _loose()]:
        if blocked(root):  # settings edited by hand / a symlink into a never-index drive: never read it
            continue
        if _drive_missing(root):
            missing.append(label)
            continue
        if progress:
            progress(label)
        before, here = len(rows), []
        _walk(root, label, kind, rows, recurse=recurse, dirs=here,
              skip=frozenset(n for n in nested if n != os.path.normpath(root)), tick=tick)
        if _drive_missing(root):  # unplugged DURING the scan: the list is partial, keep the old one
            del rows[before:]
            missing.append(label)
            continue
        every += here                      # every folder read: checked for changes at the next start
        if _watched(label, kind):
            dirs += here                   # ... and these are also watched live
    if DB_PATH != target:  # the list moved during the scan (only in tests): touch nothing of the new one
        return len(rows), missing
    seen_paths = set()  # backstop: a file is listed once (first = most specific location)
    rows = [r for r in rows if not (r[0] in seen_paths or seen_paths.add(r[0]))]
    if tick:
        tick(len(rows), "")  # folders done; archives next
    if INDEX_ARCHIVES:
        import archives
        if force:  # forget every cached archive listing (only our own cache file)
            for f in ("archives.db", "archives.db-wal", "archives.db-shm", "archives.db-journal"):
                try:
                    os.remove(os.path.join(db_dir, f))
                except FileNotFoundError:
                    pass
        rows += archives.member_rows(rows, db_dir, norm, missing)

    old_fp = None
    if os.path.exists(target):
        try:
            old = sqlite3.connect(target, timeout=30)
            if missing:
                back = {old_l: new_l for old_l, new_l in renames.items() if new_l in missing}
                want = list(missing) + list(back)
                q = ("SELECT path,name,key,ext,size,mtime,loc,kind FROM files WHERE loc IN (%s)"
                     % ",".join("?" * len(want)))
                rows += [r[:6] + (back.get(r[6], r[6]),) + r[7:] for r in old.execute(q, want).fetchall()]
            r = old.execute("SELECT v FROM meta WHERE k='fingerprint'").fetchone()
            old_fp = r[0] if r else None
            old.close()
        except sqlite3.Error:
            old_fp = None

    if DB_PATH != target:
        return len(rows), missing
    WATCH_DIRS[:] = dirs
    CHECK_DIRS[:] = every
    for old_l, new_l in renames.items():  # carried over (or the drive was back); a rename made DURING
        if LABEL_RENAMES.get(old_l) == new_l:  # this scan stays for the next one
            del LABEL_RENAMES[old_l]
    fp = _fingerprint(rows)
    if fp == old_fp and not force:
        return len(rows), missing  # nothing changed -> no disk write
    rows.sort()  # stored in path order (only sorted when it is written)

    seen = _old_seen(target)
    now = time.time()
    # seen = when the file first appeared in the list ("New" view). Kept from the old list; the first time
    # (no old 'seen' column) every file gets its own date, so not everything shows up as new at once.
    rows = [r + (seen.get(r[0], now) if seen is not None else r[5],) for r in rows]
    os.makedirs(db_dir, exist_ok=True)
    tmp = f"{target}.tmp{os.getpid()}.{threading.get_ident()}"  # unique even for two builds in one process
    db = sqlite3.connect(tmp)
    db.execute("""CREATE TABLE files(
        path TEXT, name TEXT, key TEXT, ext TEXT,
        size INTEGER, mtime REAL, loc TEXT, kind TEXT, seen REAL)""")
    db.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    db.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)", rows)  # paths are unique by construction
    db.executemany("INSERT INTO meta VALUES(?,?)", [("built", str(time.time())), ("fingerprint", fp)])
    db.commit()
    db.execute("VACUUM")  # only on the temp file, only when something changed (~100 -> ~60 MB)
    db.close()
    if DB_PATH != target:  # the list moved while this scan ran: never write into the new one
        os.remove(tmp)
        return len(rows), missing
    os.replace(tmp, target)  # atomic: open readers keep the old file until they close it
    return len(rows), missing


# ---- quick start: check the known folders for changes instead of reading every file again
_WATCH_TIME = 0.0   # changes up to this moment are in the file list
_WATCH_MISSING = []  # locations that were unplugged at the last full index
_FULL_TIME = 0.0    # when every file was last read


def _watch_path():
    return os.path.join(DB_DIR, "folders.json.gz")


def _sig():
    return hashlib.sha1(repr((sorted(ROOTS), sorted(_loose()), sorted(NEVER), sorted(EXCLUDE), sorted(SKIP_NAMES), sorted(WATCH_SKIP))
                             ).encode("utf-8", "replace")).hexdigest()


def last_full():
    """When every file was last read (0 = unknown)."""
    return _FULL_TIME


def remember(new_dirs, label_kind=None):
    """Folders that appeared while the window was open: checked (and watched) from now on."""
    known = set(CHECK_DIRS)
    CHECK_DIRS.extend(d for d in new_dirs if d not in known)
    known = set(WATCH_DIRS)
    WATCH_DIRS.extend(d for d in new_dirs if d not in known)


def save_watch(when=None, missing=None):
    """Remember the folders and up to when the file list is current (small gzip file)."""
    import gzip
    import json
    global _WATCH_TIME, _WATCH_MISSING
    if when is not None:
        _WATCH_TIME = when
    if missing is not None:
        _WATCH_MISSING = sorted(set(missing))
    data = {"v": 2, "sig": _sig(), "time": _WATCH_TIME, "full": _FULL_TIME, "missing": _WATCH_MISSING,
            "dirs": sorted(set(WATCH_DIRS)), "check": sorted(set(CHECK_DIRS))}
    os.makedirs(DB_DIR, exist_ok=True)
    path = _watch_path()
    tmp = f"{path}.tmp{os.getpid()}.{threading.get_ident()}"
    with gzip.open(tmp, "wt", encoding="utf-8", errors="surrogateescape") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _unknown_subdirs(d, known, own, out):
    """Folders under d that the saved list doesn't know (same rules as the full walk): added to out.
    Any depth (read from a list, like _walk)."""
    todo = [d]
    while todo:
        try:
            with os.scandir(todo.pop()) as it:
                subs = [e.path for e in it if e.is_dir(follow_symlinks=False) and not skipped(e.name)
                        and os.path.normpath(e.path) not in own]
        except OSError:
            continue
        new = [sub for sub in subs if sub not in known]
        known.update(new)
        out.extend(new)
        todo.extend(reversed(new))


def quick():
    """Start-up without a full index: look only at the folders the last index read and re-read the
    ones that changed since (a folder's date changes when a file is added, removed or renamed in it).
    Returns (files, unplugged_labels, added, removed), or None when a full index is needed: no list
    yet, the folder settings changed, or a drive was plugged in / out since (or during the check).
    NOT seen by this check (Full index reads them):
    - a file saved over itself in place (the folder's date does not change);
    - files copied into an existing folder by a tool that puts the folder's old date back
      (rsync -a, cp -a onto an existing folder, some archive extractors).
    Folders reached through a link, or leading into a never-index drive, are never looked at."""
    import gzip
    import json
    import stat as _stat
    global _WATCH_TIME, _WATCH_MISSING, _FULL_TIME
    if not os.path.exists(DB_PATH):
        return None
    try:
        with gzip.open(_watch_path(), "rt", encoding="utf-8", errors="surrogateescape") as f:
            data = json.load(f)
        if data.get("v") != 2 or data.get("sig") != _sig():
            return None
        watch = [d for d in data["dirs"] if isinstance(d, str)]
        check = sorted(d for d in data["check"] if isinstance(d, str))  # sorted: a folder before what's in it
        since = float(data["time"])
        was_missing, full = list(data.get("missing", [])), float(data.get("full", 0))
    except (OSError, ValueError, KeyError, TypeError, EOFError):
        return None

    def unplugged():
        return sorted({label for root, label, _k in ROOTS + _loose() if not blocked(root) and _drive_missing(root)})
    missing = unplugged()
    if missing != sorted(was_missing):
        return None  # a drive came or went: its files / folders need the full pass
    t0 = time.time()
    # never through a link and never into a never-index drive: such a folder and all below it are skipped
    bad = [os.path.normpath(r).rstrip("/") + "/" for r, _l, _k in ROOTS + _loose() if blocked(r)]
    changed, last_bad = [], None
    for d in check:
        if last_bad and d.startswith(last_bad):
            continue
        if any((d + "/").startswith(b) for b in bad):
            continue
        try:
            st = os.lstat(d)
        except OSError:
            continue  # folder gone: its parent's date changed too, and re-reading the parent drops it
        if not _stat.S_ISDIR(st.st_mode):
            last_bad = d.rstrip("/") + "/"  # now a link or a file: nothing under it is looked at
            continue
        if st.st_mtime > since - 2:
            changed.append(d)
    known = set(check)
    own = _own()
    extra = []  # folders the saved list doesn't know (made in a session that ended without saving)
    for d in changed:
        where = _root_for(d)
        if where and where[2]:
            _unknown_subdirs(d, known, own, extra)
    WATCH_DIRS[:] = watch
    CHECK_DIRS[:] = check
    _WATCH_TIME, _WATCH_MISSING, _FULL_TIME = since, missing, full
    added = removed = 0
    if changed:
        added, removed, new_dirs, _written = update_dirs(changed + extra)
        if unplugged() != missing:
            return None  # a drive went away during the check: let the full pass sort it out
        fresh = set(extra) | set(new_dirs)
        CHECK_DIRS.extend(sorted(fresh))
        seen_watch = set(watch)
        for d in sorted(fresh):
            where = _root_for(d)
            if where and _watched(where[0], where[1]) and d not in seen_watch:
                WATCH_DIRS.append(d)
        unread = []
        for d in changed:  # a folder that is there but could not be read: keep the old time, try again next start
            try:
                os.scandir(d).close()
            except FileNotFoundError:
                pass
            except OSError:
                unread.append(d)
        try:
            save_watch(since if unread else t0)
        except OSError as e:
            print("folder list not saved:", e, file=sys.stderr)
    db = connect_ro()
    try:
        n = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    finally:
        db.close()
    return n, missing, added, removed


def _old_seen(db_path=None):
    """{path: seen} from the current list, or None if it has no 'seen' column yet (or no list)."""
    try:
        old = sqlite3.connect(f"file:{db_path or DB_PATH}?mode=ro", uri=True, timeout=30)
        try:
            return dict(old.execute("SELECT path, seen FROM files"))
        finally:
            old.close()
    except sqlite3.Error:
        return None


def _root_for(d):
    """(label, kind, recurse) of the scanned location that contains folder d, or None."""
    best = None
    for r, label, kind in ROOTS:
        if (d == r or d.startswith(r.rstrip("/") + "/")) and (best is None or len(r) > len(best[0])):
            best = (r, label, kind, True)
    for r, label, kind in _loose():
        if d == r and best is None:
            best = (r, label, kind, False)
    return best[1:] if best else None


def update_dirs(changed):
    """Watch mode: re-read only these folders and change just their rows, in place, in one transaction
    (no 65 MB rewrite + VACUUM on the snapshotted disk for every new file). New sub-folders are read
    whole; vanished ones drop their rows. Returns (added, removed, new_folders_to_watch, written)."""
    if not os.path.exists(DB_PATH):
        return 0, 0, [], False
    db = sqlite3.connect(DB_PATH, timeout=30)
    added = removed = 0
    new_dirs = []
    try:
        known, seen = {}, {}
        drop, insert = set(), []
        for d in sorted(set(changed)):
            where = _root_for(d)
            if where is None or os.path.islink(d) or blocked(d):
                continue  # a folder swapped for a link / leading into a never-index drive: never read
            if not os.path.isdir(d) or _drive_missing(d):
                continue  # gone folder (its parent's change handles it) / drive being unplugged: keep rows
            try:
                os.scandir(d).close()
            except OSError:
                continue  # unreadable (e.g. a USB drive pulled without unmounting): keep its last known rows
            label, kind, recurse = where
            prefix = d.rstrip("/") + "/"
            # only this folder's rows of this location (a drive root's loose files must never touch
            # Storage › Models etc.). path >= 'd/' AND < 'd0' = everything under d/ ('0' follows '/'),
            # read by SQLite, not 250k rows pulled into Python for every saved file.
            try:
                rows_here = db.execute("SELECT path, size, mtime, seen FROM files WHERE loc=? AND path>=? AND path<?",
                                       (label, prefix, prefix[:-1] + "0")).fetchall()
            except sqlite3.OperationalError:  # list from an older version: the next full scan updates it
                return 0, 0, new_dirs, False
            old = []
            for x, s, m, sn in rows_here:
                known[x], seen[x] = (s, m), sn
                old.append(x)
            have_sub = set()
            for x in old:
                rest = x[len(prefix):].split("::", 1)[0]
                if "/" not in rest:
                    drop.add(x)                      # a file (or archive member) directly in d: re-read
                else:
                    sub = rest.split("/", 1)[0]
                    have_sub.add(sub)
                    if not os.path.isdir(prefix + sub):
                        drop.add(x)                  # its folder was deleted / renamed away
            rows = []
            _walk(d, label, kind, rows, recurse=False)
            if recurse:
                try:
                    subs = [e for e in os.scandir(d) if e.is_dir(follow_symlinks=False)]
                except OSError:
                    subs = []
                own = _own()
                for e in subs:
                    if skipped(e.name) or os.path.normpath(e.path) in own:
                        continue  # a nested location (listed under its own name) / a never-index mount
                    if e.name not in have_sub:      # new (or moved-in) folder: read all of it
                        _walk(e.path, label, kind, rows, dirs=new_dirs if _watched(label, kind) else None,
                              skip=frozenset(own))
            if INDEX_ARCHIVES:
                import archives
                rows += archives.member_rows(rows, DB_DIR, norm, forget=False)
            insert += rows
        # one row per path: a folder can be read twice in one update (as "new" by its parent and by itself)
        insert = list({r[0]: r for r in insert}.values())
        new_paths = {r[0] for r in insert}
        added = len(new_paths - drop)
        removed = len(drop - new_paths)
        changed_rows = any(known.get(r[0]) != (r[4], r[5]) for r in insert) if insert else False
        if not added and not removed and not changed_rows:
            return 0, 0, new_dirs, False  # nothing new (e.g. a temp file came and went): no disk write
        with db:
            # one set-based DELETE: the table has no index on path, so a DELETE per row scans all of it
            # (7 ms x thousands of rows for a renamed folder)
            db.execute("CREATE TEMP TABLE IF NOT EXISTS gone(path TEXT PRIMARY KEY)")
            db.execute("DELETE FROM gone")
            db.executemany("INSERT OR IGNORE INTO gone VALUES(?)", [(x,) for x in drop | new_paths])
            r = db.execute("SELECT v FROM meta WHERE k='fingerprint'").fetchone()
            total = _fp_total(r[0] if r else None)
            if total is None:  # fingerprint of an older version: count it once from the whole list
                total = sum(map(_row_hash, db.execute("SELECT * FROM files"))) % _FP_MOD
            total -= sum(map(_row_hash, db.execute("SELECT * FROM files WHERE path IN (SELECT path FROM gone)")))
            db.execute("DELETE FROM files WHERE path IN (SELECT path FROM gone)")
            now = time.time()
            db.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                           [r + (seen.get(r[0], now),) for r in insert])
            total += sum(map(_row_hash, insert))
            db.execute("INSERT OR REPLACE INTO meta VALUES('fingerprint', ?)", (_fp_text(total % _FP_MOD),))
            db.execute("INSERT OR REPLACE INTO meta VALUES('built', ?)", (str(time.time()),))
        return added, removed, new_dirs, True
    finally:
        db.close()


if __name__ == "__main__":
    t = time.time()
    n, missing = build(lambda l: print("  scanning", l, file=sys.stderr))
    print(f"indexed {n} files in {time.time() - t:.1f}s -> {DB_PATH}")
    if missing:
        print("not mounted (kept previous entries):", ", ".join(missing))
