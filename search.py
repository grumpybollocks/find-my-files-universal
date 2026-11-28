"""Search logic: Romanian/English-tolerant name matching on top of the SQLite index."""
import os
import re
import sqlite3

import indexer

LIMIT = 2000  # results shown at most (best first)

# English <-> Romanian word stems; typing any one also finds the others.
SYNONYMS = [
    ["cross", "cruc"], ["church", "biseric"], ["cathedral", "catedral"],
    ["medal", "medali", "medalion"], ["coin", "moned", "banut"], ["knife", "cutit"],
    ["sword", "sabi"], ["crown", "coroan"], ["bell", "clopot"], ["key", "cheit"],
    ["back", "spate"], ["base", "baza"], ["body", "corp"], ["dove", "pigeon", "porumbel"],
    ["ring", "inel"], ["shield", "scut"], ["face", "fata", "fatza"],
    ["hand", "mana"], ["flower", "floare", "flori"], ["heart", "inim"],
    ["angel", "inger"], ["saint", "sfant"], ["clover", "trefla", "trifoi"],
    ["box", "caset", "cutie"], ["plate", "farfuri", "placut"], ["lips", "buze"],
    ["skull", "craniu"], ["mold", "matrit"], ["stamp", "stampil", "sigiliu"],
    ["handle", "maner"], ["chain", "lant"], ["star", "stea"], ["lamp", "lampa"],
    ["vase", "vaza"], ["cup", "pahar"], ["mug", "cana"], ["holder", "suport"], ["silver", "argint"],
    ["gold", "aur"], ["small", "mic"], ["big", "mare"], ["text", "scris"], ["letters", "litere"],
    ["candle", "lumanar"], ["icon", "icoan"], ["tomb", "mormant"], ["bag", "geant"],
    ["egg", "oua"], ["snowman", "olaf"], ["bust", "bust"], ["magnet", "magnet"],
]
_SUFFIXES = ("ulita", "ulite", "urile", "elor", "ilor", "ului", "uri", "ile", "ul", "ii", "ea", "e", "i", "a", "u")


def stem(w: str) -> str:
    """cruce/cruci/cruciulita -> cruc ; medalie/medalii -> medal ; medalioane -> medalion"""
    for suf in ("oanele", "oane"):  # medalioane -> medalion, butoane -> buton
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)] + "on"
    for _ in range(2):
        if len(w) <= 4:
            break
        for suf in _SUFFIXES:
            if w.endswith(suf) and len(w) - len(suf) >= 4:
                w = w[: -len(suf)]
                break
        else:
            break
    return w


def variants(tok: str):
    st = stem(tok)
    out = {tok, st}
    if len(tok) == 4 and tok[-1] in "aei" and tok[:3].isalpha():
        # baze/baza/bazi: short words keep 4 letters (a 3-letter stem like "man" would match "romana")
        out.update(tok[:3] + v for v in "aei")
    for group in SYNONYMS:
        for g in (x.strip() for x in group):
            # exact stem, or the user typed the start of a synonym ("cro" -> cross).
            # Never the other way round: "aurora" must not pull in "aur" (gold).
            if st == g or tok == g or (len(st) >= 3 and g.startswith(st)):
                out.update(x.strip() for x in group)
                break
    return out


def _like(v: str) -> str:
    """Escape LIKE wildcards so a typed % or _ is matched literally."""
    return "%" + v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def tokens(text: str):
    return [t for t in re.split(r"[\s_]+", indexer.norm(text).strip()) if t]


NEW_DAYS = 7  # the "New" view: files that appeared in the list in the last week


def count_new(since, exts=None):
    """How many work files appeared in the list after `since` (the badge on the New button)."""
    where, args = ["seen > ?", "kind='work'"], [since]
    if exts:
        where.append("ext IN (%s)" % ",".join("?" * len(exts)))
        args += list(exts)
    try:
        db = indexer.connect_ro()
        n = db.execute("SELECT count(*) FROM files WHERE " + " AND ".join(where), args).fetchone()[0]
        db.close()
        return n
    except sqlite3.Error:
        return 0


def query(text, exts=None, include_copies=False, limit=None, since=None, under=None):
    """Return rows (path, name, loc, size, mtime), best first.

    Ranking: work folders with the typed words > synonym-only matches >
    software/music/archive folders > backups/trash; newest first inside each group.
    since = only files that first appeared in the list after this time ("New" view; words optional),
    newest arrivals first.
    under = only files from these locations (a list of location names: one drive's entries, picked
    with its chip in the bottom bar - the same names the chip's file count is made from).
    """
    limit = limit or LIMIT
    toks = tokens(text)
    if not toks and since is None and not under:
        return []
    where, args = [], []
    if since is not None:
        where.append("seen > ?")
        args.append(since)
    for t in toks:
        vs = variants(t)
        where.append("(" + " OR ".join("key LIKE ? ESCAPE '\\'" for _ in vs) + ")")
        args += [_like(v) for v in vs]
    if exts:
        where.append("ext IN (%s)" % ",".join("?" * len(exts)))
        args += list(exts)
    if not include_copies:
        where.append("kind='work'")
    if under:
        where.append("loc IN (%s)" % ",".join("?" * len(under)))
        args += list(under)
    if since is not None or not toks:  # no words: the New view, or one drive's newest files (its chip)
        by = "seen" if since is not None else "mtime"
        sql = ("SELECT path,name,loc,size,mtime FROM files WHERE " + " AND ".join(where)
               + f" ORDER BY {by} DESC LIMIT ?")
        try:
            db = indexer.connect_ro()
            rows = db.execute(sql, args + [limit]).fetchall()
            db.close()
        except sqlite3.Error:
            rows = []
        return rows
    low = [w for w in indexer.WATCH_SKIP if w]  # low-priority locations rank after everything else
    sql = (
        "SELECT path,name,loc,size,mtime FROM files WHERE " + " AND ".join(where) +
        " ORDER BY CASE WHEN kind='copy' THEN 3"
        + (" WHEN " + " OR ".join("loc LIKE ? ESCAPE '\\'" for _ in low) + " THEN 2" if low else "") +
        " WHEN " + " AND ".join("key LIKE ? ESCAPE '\\'" for _ in toks) + " THEN 0 ELSE 1 END, mtime DESC LIMIT ?"
    )
    args += [_like(w) for w in low]
    args += [_like(stem(t)) for t in toks] + [limit]
    try:
        db = indexer.connect_ro()  # never create/modify the index here
        rows = db.execute(sql, args).fetchall()
        db.close()
    except sqlite3.Error:
        rows = []
    return rows


# ---------------------------------------------------------------- related files (versions + parts)
# Part words seen in real project names (corp/spate/baza/cheita/clopotel/capac...) + English ones.
PART_WORDS = {"corp", "spate", "baza", "cheita", "clopotel", "capac", "fata", "fatza", "top", "bottom",
              "back", "front", "base", "body", "full", "jos", "sus", "mijloc", "ornament", "inel"}
_PARENS = re.compile(r"\([^)]*\)?")
_TRAIL_PART = re.compile(r"_\d+$")


def family(name: str):
    """(model_key, project_key) for a file name.

    model_key   = name without extension, brackets "(repaired)", "x2", standalone numbers
    project_key = model_key without part words (corp, spate, baza, cheita, ...)
    'corp cruce celtica (repaired) x2.stl' -> ('corp cruce celtica', 'cruce celtica')
    'cruce 4-1 baza2 (repaired).stl'       -> ('cruce 4-1 baza2', 'cruce 4-1')
    """
    base = indexer.norm(os.path.splitext(name)[0])
    base = _PARENS.sub(" ", base)
    base = _TRAIL_PART.sub("", base.strip())
    raw = [t for t in re.split(r"[\s_.,]+", base) if t and not re.fullmatch(r"x\d+", t)]
    toks = [t for t in raw if not t.isdigit()]
    model = " ".join(toks)

    def strip_parts(ts):
        return [t for t in ts if re.sub(r"\d+$", "", t) not in PART_WORDS]

    proj = strip_parts(toks)
    if len(proj) < 2:
        # one generic word left ("Cruce 3 63 top" -> "cruce"): the numbers may be the version,
        # and we can't tell - keep them rather than merge different models
        proj = strip_parts(raw)
    return model, " ".join(proj) or model


def related(name, include_copies=False, limit=None):
    """All indexed files of the same project (same project_key), any file type.
    Returns (project_key, rows)."""
    limit = limit or LIMIT
    _, key = family(name)
    words = sorted(key.split(), key=len, reverse=True)[:3]
    if not words:
        return key, []
    where = " AND ".join("key LIKE ? ESCAPE '\\'" for _ in words)
    args = [_like(w) for w in words]
    if not include_copies:
        where += " AND kind='work'"
    try:
        db = indexer.connect_ro()  # never create/modify the index here
        rows = db.execute(f"SELECT path,name,loc,size,mtime FROM files WHERE {where} "
                          "ORDER BY mtime DESC LIMIT 20000", args).fetchall()
        db.close()
    except sqlite3.Error:
        rows = []
    return key, [r for r in rows if family(r[1])[1] == key][:limit]


# ---------------------------------------------------------------- search by model size ("60mm")
_SIZE_WORD = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*mm\b", re.I)
SIZE_TOLERANCE = 0.15


def parse_size(text):
    """'cruce 60mm' -> ('cruce', 60.0); '60 mm' -> ('', 60.0); no size word -> (text, None)."""
    m = _SIZE_WORD.search(text or "")
    if not m:
        return text, None
    mm = float(m.group(1).replace(",", "."))
    if mm <= 0:
        return text, None
    return " ".join((text[:m.start()] + " " + text[m.end():]).split()), mm


def size_range(mm):
    return mm * (1 - SIZE_TOLERANCE), mm * (1 + SIZE_TOLERANCE)


def query_sized(text, exts=None, include_copies=False, limit=None, under=None):
    """Like query(), plus a size word: keeps models whose LARGEST side is within ±15 % of it.
    Only models with a cached size (thumbs.py) can match. Returns (rows, mm or None)."""
    limit = limit or LIMIT
    import thumbs
    words, mm = parse_size(text)
    if mm is None:
        return query(text, exts, include_copies, limit, under=under), None
    lo, hi = size_range(mm)
    try:
        tdb = thumbs.connect()
        keys = thumbs.keys_by_largest_side(tdb, lo, hi)
        tdb.close()
    except sqlite3.Error:
        keys = {}
    if words.strip():
        rows = query(words, exts, include_copies, 20000, under=under)
    else:  # only a size typed: every model with that size
        paths = sorted(set(keys.values()))
        rows = []
        try:
            db = indexer.connect_ro()  # never create/modify the index here
            for i in range(0, len(paths), 500):
                part = paths[i:i + 500]
                q = "SELECT path,name,loc,size,mtime FROM files WHERE path IN (%s)" % ",".join("?" * len(part))
                if exts:
                    q += " AND ext IN (%s)" % ",".join("?" * len(exts))
                if not include_copies:
                    q += " AND kind='work'"
                rows += db.execute(q, part + list(exts or ())).fetchall()
            db.close()
        except sqlite3.Error:
            rows = []
        if under:
            rows = [r for r in rows if r[2] in set(under)]
        rows.sort(key=lambda r: -r[4])
    rows = [r for r in rows if thumbs.key(r[0], r[3], r[4]) in keys]  # current version only
    # "stea 34mm" may simply be the file's NAME (DXF/CDR have no model size): name matches first
    named = query(text, exts, include_copies, limit, under=under)
    seen = {r[0] for r in named}
    return (named + [r for r in rows if r[0] not in seen])[:limit], mm


# ---------------------------------------------------------------- "did you mean"
# Rules from how others do it (see docs/PLAN-1.7.md): correct only a word that finds no file (Norvig;
# Lucene "suggest when not in index"); no suggestions for words under 4 letters (Lucene's default);
# candidates 1 typo away (4-letter words) or 2 (longer), a swapped pair of letters counting as one typo;
# a candidate must be in 2+ files when the list is big (Lucene's minimum document frequency); best =
# fewest typos, then the word most files use (Norvig). A suggestion is offered only if it finds files.
SUGGEST_MIN_LEN = 4
_WORD = re.compile(r"[^\W_]+")
_VOCAB = {"stamp": None, "count": {}, "by_len": {}, "files": 0}
_VOCAB_LOCK = __import__("threading").Lock()


def _typos(a, b, limit):
    """Optimal string alignment distance (insert, delete, change, swap two neighbours), or limit + 1."""
    la, lb = len(a), len(b)
    if abs(la - lb) > limit:
        return limit + 1
    prev2, prev = None, list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        best = i
        for j in range(1, lb + 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            best = min(best, v)
        if best > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[lb]


def vocabulary():
    """{word: files using it} from every file name in the list, read the way the search reads text.
    Built once per version of the list (about 0.4 s for 250,000 names); call it off the window's thread."""
    try:
        st = os.stat(indexer.DB_PATH)
        stamp = (indexer.DB_PATH, st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    with _VOCAB_LOCK:
        if _VOCAB["stamp"] == stamp:
            return _VOCAB["count"]
        count, files = {}, 0
        try:
            db = indexer.connect_ro()
            try:
                for (key, ext) in db.execute("SELECT key, ext FROM files"):  # key = the name, read as the search reads it
                    files += 1
                    if ext and key.endswith("." + ext):
                        key = key[:-len(ext) - 1]  # the file ending is not a word of the name (".tar" -> "tar")
                    for w in set(_WORD.findall(key)):
                        if len(w) >= 2 and not w.isdigit():
                            count[w] = count.get(w, 0) + 1
            finally:
                db.close()
        except sqlite3.Error:
            return {}
        by_len = {}
        for w in count:
            by_len.setdefault(len(w), []).append(w)
        _VOCAB.update(stamp=stamp, count=count, by_len=by_len, files=files)
        return count


def _closest(word):
    """Best replacement for one word, or None."""
    vocabulary()
    count, by_len = _VOCAB["count"], _VOCAB["by_len"]
    limit = 1 if len(word) <= 4 else 2
    floor = 2 if _VOCAB["files"] >= 1000 else 1
    best = None
    for n in range(len(word) - limit, len(word) + limit + 1):
        for w in by_len.get(n, ()):
            if count[w] < floor or w == word:
                continue
            d = _typos(word, w, limit)
            if d <= limit:
                key = (d, -count[w], w)
                if best is None or key < best:
                    best = key
    return best[2] if best else None


def did_you_mean(text, exts=None, include_copies=False, under=None):
    """(suggested search, files it finds) or (None, reason). reason 'together' = every word finds
    files on its own but not all together (nothing to correct)."""
    words = tokens(text)
    if not words:
        return None, ""
    out, changed = [], False
    for w in words:
        if len(w) < SUGGEST_MIN_LEN or not w.isalpha() or query(w, exts, include_copies, 1, under=under):
            out.append(w)
            continue
        fix = _closest(w)
        if fix is None:
            return None, ""
        out.append(fix)
        changed = True
    if not changed:
        return None, "together" if len(words) > 1 else ""
    new = " ".join(out)
    found = len(query(new, exts, include_copies, under=under))
    return (new, found) if found else (None, "")
