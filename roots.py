"""Which folders Find My Files searches - one settings file per PC.

~/.config/findfiles/roots.json  (FINDFILES_CONFIG overrides the folder; tests use a temp dir)
{
  "version": 1,
  "roots": [{"path": ..., "label": ..., "kind": "work"|"copy", "recurse": true, "enabled": true}, ...],
  "never": ["/mnt/..."],         # never indexed, refused when added
  "skip": ["games", "assets", ...], # folder NAMES never read, anywhere (case does not matter)
  "low": ["Software", "Music", ...] # words in a location's name that mark it low priority
}
A folder may sit inside another one in the list (a whole drive plus some of its folders): the inner
one keeps its own name and kind. An unticked folder is left out, also when a ticked one contains it.
No file yet -> this PC's defaults (below). A damaged file is never overwritten silently: the
defaults are used and load() says why, so the window can tell the user.
"""
import json
import os

HOME = os.path.expanduser("~")
_BASE = os.environ.get("FINDFILES_CONFIG") or os.path.join(HOME, ".config/findfiles")
PATH = os.path.join(_BASE, "roots.json")
KINDS = ("work", "copy")
# Folder names that are never read, wherever they are: games, program folders, game / engine assets.
DEFAULT_SKIP = ["games", "GOG", "GOG Games", "GOG Galaxy", "Epic Games", "Riot Games", "Origin Games", "XboxGames",
                "Ubisoft", "Steam", "Program Files", "Program Files (x86)", "ProgramData", "Windows", "WindowsApps",
                "assets", "found.000", "found.001"]


# Words in a location's NAME IN THE APP that mark it as low priority (software, music, archives):
# its files rank last, it is not watched live, and archives inside it are not opened.
DEFAULT_LOW = ["Software", "Music", "Zip", "Archive"]


def _r(path, label, kind="work", recurse=True):
    return {"path": path, "label": label, "kind": kind, "recurse": recurse, "enabled": True}


def _user_dirs():
    """The desktop's own folders (they have other names in other languages), from
    ~/.config/user-dirs.dirs; the English names when that file is missing."""
    found = {}
    try:
        with open(os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"),
                               "user-dirs.dirs"), encoding="utf-8") as f:
            for line in f:
                key, _, val = line.strip().partition("=")
                if key.startswith("XDG_") and key.endswith("_DIR"):
                    val = val.strip('"').replace("$HOME", HOME)
                    if val.rstrip("/") != HOME and val.startswith("/"):
                        found[key[4:-4]] = val.rstrip("/")
    except OSError:
        pass
    return [found.get(k, os.path.join(HOME, d)) for k, d in
            (("DESKTOP", "Desktop"), ("DOCUMENTS", "Documents"), ("DOWNLOAD", "Downloads"),
             ("PICTURES", "Pictures"), ("VIDEOS", "Videos"))]


def _home_roots():
    dirs = _user_dirs()
    there = [d for d in dirs if os.path.isdir(d)] or dirs[:4]
    return [_r(d, os.path.basename(d)) for d in there]


def defaults():
    """The starting list on a PC with no settings file yet: the home folders that exist.
    Drives are added in Settings (Add folder...)."""
    return {"roots": _home_roots(), "never": []}


# ---------------------------------------------------------------- checks
_SYSTEM = ("/proc", "/sys", "/dev", "/run/user", "/usr", "/etc", "/boot", "/var", "/bin", "/lib", "/sbin")
_TOO_WIDE = {"/", "/mnt", "/media", "/run", "/run/media", "/home", HOME, os.path.dirname(HOME)}
_PROGRAM_PARTS = {"steamapps", "SteamLibrary", ".SteamLibrary", "compatdata", ".steam", "shadercache"}


def _mount_devs(paths):
    """st_dev numbers of these mount points, read from /proc/self/mountinfo - the drives themselves
    are never touched (reading the Windows NTFS drive makes the kernel rewrite it)."""
    want = {os.path.normpath(p) for p in paths}
    devs = set()
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as f:
            for line in f:
                part = line.split()
                if len(part) > 4:
                    mnt = part[4].replace("\\040", " ")
                    if mnt in want:
                        major, minor = part[2].split(":")
                        devs.add(os.makedev(int(major), int(minor)))
    except (OSError, ValueError):
        pass
    return devs


def never_mounts(never):
    """Every mount point on a never-index drive's device (bind mounts too), from /proc/self/mountinfo only."""
    devs = _mount_devs(never)
    out = set()
    if not devs:
        return out
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as f:
            for line in f:
                part = line.split()
                if len(part) > 4:
                    major, minor = part[2].split(":")
                    if os.makedev(int(major), int(minor)) in devs:
                        out.add(part[4].replace("\\040", " "))
    except (OSError, ValueError):
        pass
    return out


def on_never_device(path, never):
    """True if path really lives on a never-index drive (through a symlink or bind mount too)."""
    if not never:
        return False
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    if any(real == os.path.normpath(n) or real.startswith(os.path.normpath(n) + "/") for n in never):
        return True
    devs = _mount_devs(never)
    if not devs:
        return False
    try:
        return os.stat(path).st_dev in devs
    except OSError:
        return False


def _inside(a, b):
    return a == b or a.startswith(b.rstrip("/") + "/")


def _norm(p):
    return os.path.normpath(os.path.expanduser(p.strip())) if p and p.strip() else ""


def check(path, never=(), roots=()):
    """(ok, reason). Refuses what would read program files, the whole system or a 'never' drive."""
    p = _norm(path)
    if not p or not os.path.isabs(p):
        return False, "Choose a full folder path."
    if p in _TOO_WIDE or (p.startswith("/run/media/") and p.count("/") <= 3):
        return False, "That is a whole system area, far too wide. Pick the folder that holds your files."
    if any(p == s or p.startswith(s + "/") for s in _SYSTEM):
        return False, "That is a system folder, not your files."
    for n in never:
        n = _norm(n)
        if n and (p == n or p.startswith(n + "/")):
            return False, f"{n} is on the never-index list (for example the Windows drive)."
    if _PROGRAM_PARTS & set(p.split("/")) or "/.local/share/Steam" in p + "/":
        return False, "That is a Steam / game-prefix folder: program files, not your work."
    if not os.path.isdir(p):
        return False, "That folder doesn't exist (or its drive isn't plugged in)."
    if on_never_device(p, never):
        return False, "That folder leads into a never-index drive (through a link or mount)."
    for r in roots:
        q = _norm(r["path"])
        if q == p:
            return False, "That folder is already in the list."
    problem = nesting(p, roots)
    if problem:
        return False, problem
    return True, ""


def nesting(path, roots, recurse=True):
    """Always '' since 1.3: a folder inside another is allowed (the inner one keeps its own name and
    kind, the outer one lists the rest). Kept for callers; `overlaps()` describes the relation."""
    return ""


def overlaps(path, roots):
    """A short note for the user when this folder is inside / around listed ones, else ''."""
    p = _norm(path)
    inside = [r["label"] for r in roots if r.get("recurse", True) and _norm(r["path"]) != p and _inside(p, _norm(r["path"]))]
    around = [r["label"] for r in roots if _norm(r["path"]) != p and _inside(_norm(r["path"]), p)]
    if inside:
        return f"It is inside “{inside[0]}”: its files will be shown under its own name instead."
    if around:
        return (f"It contains {len(around)} folder(s) already in the list: they keep their own names, "
                "this one lists everything else.")
    return ""


def is_whole_drive(path):
    p = _norm(path)
    return p not in ("", "/") and os.path.ismount(p)


def label_for(path, roots=()):
    """'/mnt/X/Projects' -> 'Projects (X)'; made unique in the list."""
    p = _norm(path).rstrip("/")
    name, parent = os.path.basename(p) or p, os.path.basename(os.path.dirname(p))
    base = f"{name} ({parent})" if parent else name
    taken = {r["label"] for r in roots}
    label, i = base, 2
    while label in taken:
        label, i = f"{base} {i}", i + 1
    return label


def _clean(data):
    """Keep only well-formed entries; labels unique (they name each location in the index)."""
    roots, seen = [], set()
    for r in data.get("roots", []):
        if not isinstance(r, dict) or not isinstance(r.get("path"), str) or not isinstance(r.get("label"), str):
            continue
        if not r["label"] or r["label"] in seen:
            continue
        seen.add(r["label"])
        roots.append({"path": r["path"], "label": r["label"],
                      "kind": r.get("kind") if r.get("kind") in KINDS else "work",
                      "recurse": bool(r.get("recurse", True)), "enabled": bool(r.get("enabled", True))})
    never = [n for n in data.get("never", []) if isinstance(n, str) and n]
    skip = data.get("skip")
    if not isinstance(skip, list):
        skip = DEFAULT_SKIP  # a settings file from before 1.3
    skip = [n.strip() for n in skip if isinstance(n, str) and n.strip() and "/" not in n]
    low = data.get("low")
    if not isinstance(low, list):
        low = DEFAULT_LOW
    low = [n.strip() for n in low if isinstance(n, str) and n.strip()]
    return {"roots": roots, "never": never, "skip": list(dict.fromkeys(skip)), "low": list(dict.fromkeys(low))}


# ---------------------------------------------------------------- load / save
def load():
    """(settings, problem). problem = '' or why the defaults are used instead of a damaged file."""
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("roots"), list):
            raise ValueError("no folder list in it")
        return _clean(data), ""
    except FileNotFoundError:
        return _clean(defaults()), ""
    except (OSError, ValueError) as e:
        return _clean(defaults()), f"Settings file {PATH} could not be read ({e}); using the defaults. " \
                                   "Saving in Settings replaces it."


def save(settings):
    data = {"version": 1, **_clean(settings)}
    os.makedirs(_BASE, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)  # all or nothing


def excluded(settings):
    """Paths of the unticked folders: left out even when a ticked folder contains them."""
    return [_norm(r["path"]) for r in settings["roots"] if not r["enabled"] and r["path"]]


def active(settings):
    """-> (ROOTS, LOOSE) in the shapes indexer uses; enabled entries only."""
    on = [r for r in settings["roots"] if r["enabled"]]
    return ([(r["path"], r["label"], r["kind"]) for r in on if r["recurse"]],
            [(r["path"], r["label"], r["kind"]) for r in on if not r["recurse"]])
