"""'Open with' programs: installed apps (from their .desktop launchers) + the user's own pick per file type.

- Default for a file type = the installed apps whose launcher says they open it (its MIME type or a
  parent type), like Dolphin's "Open with". Wine launchers that repeat the same program are merged.
- The user's pick (which programs, in which order) is saved per extension in
  ~/.config/findfiles/programs.json. Nothing else is written.
- Launching = the launcher's Exec line with the file path put in. Never through a shell.
"""
import json
import os
import re
import shlex
import subprocess

_BASE = os.environ.get("FINDFILES_CONFIG") or os.path.expanduser("~/.config/findfiles")  # tests use a temp dir
SETTINGS = os.path.join(_BASE, "programs.json")
STATE = os.path.join(_BASE, "state.json")  # small app state (e.g. when you last looked at "New")


class App:
    def __init__(self, path, name, exec_, mimes, hidden):
        self.path, self.name, self.exec, self.mimes, self.hidden = path, name, exec_, mimes, hidden
        self.id = os.path.basename(path)

    def __repr__(self):
        return f"App({self.id})"

    @property
    def label(self):
        """Name + where it comes from, so two copies of one program can be told apart."""
        e = self.exec.lower()
        how = ("flatpak" if "flatpak run" in e else "AppImage" if ".appimage" in e or "appimage" in self.id.lower()
               else "Wine" if "wine" in e or "portproton" in e else "")
        base = f"{self.name} ({how})" if how and how.lower() not in self.name.lower() else self.name
        return f"{base} – {self.extra}" if getattr(self, "extra", None) else base

    def command(self, file):
        """argv for opening `file`: %f %F %u %U -> the path; other field codes dropped.
        None = this launcher can't be used safely (a shell script that would read the file name as
        commands: a crafted name inside a downloaded zip must never run anything)."""
        try:
            parts = shlex.split(_unescape(self.exec))
        except ValueError:
            return None
        if _shell_script(parts):
            return None
        argv, placed = [], False
        for p in parts:
            if p in ("%f", "%F", "%u", "%U"):
                argv.append(file); placed = True
            elif p in ("%i", "%c", "%k", "%d", "%D", "%n", "%N", "%v", "%m"):
                continue
            else:
                for code in ("%f", "%F", "%u", "%U"):  # e.g. --file=%f
                    if code in p:
                        if any(c.isspace() for c in p):
                            return None  # a field code inside a quoted script line: unsafe
                        p = p.replace(code, file); placed = True
                argv.append(p.replace("%%", "%"))
        if not placed:
            argv.append(file)
        return argv


_SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "fish"}


def _shell_script(parts):
    """`sh -c '...'` (also after `env X=y`): the path would be read by a shell."""
    i = 0
    while i < len(parts) and (parts[i] == "env" or ("=" in parts[i] and not parts[i].startswith("-"))):
        i += 1
    return i < len(parts) and os.path.basename(parts[i]) in _SHELLS and "-c" in parts[i + 1:]


def _unescape(exec_):
    """Desktop-entry string escapes before Exec quoting: \\s -> space, \\\\ -> \\ ..."""
    table = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    return re.sub(r"\\(.)", lambda m: table.get(m.group(1), "\\" + m.group(1)), exec_)


def app_dirs():
    """Folders with .desktop launchers (user first, so a user launcher wins over a system one)."""
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    dirs = [data_home] + (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    dirs += [os.path.expanduser("~/.local/share/flatpak/exports/share"), "/var/lib/flatpak/exports/share"]
    out = []
    for d in dirs:
        a = os.path.join(d, "applications")
        if os.path.isdir(a) and a not in out:
            out.append(a)
    return out


def _parse(path):
    vals, section = {}, None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("["):
                    section = line
                elif section == "[Desktop Entry]" and "=" in line:
                    k, v = line.split("=", 1)
                    vals.setdefault(k.strip(), v.strip())
    except OSError:
        return None
    if vals.get("Type", "Application") != "Application" or not vals.get("Exec") or vals.get("Hidden") == "true":
        return None
    mimes = [m for m in vals.get("MimeType", "").split(";") if m]
    return App(path, vals.get("Name", os.path.basename(path)), vals["Exec"], mimes, vals.get("NoDisplay") == "true")


def installed(dirs=None):
    """All launchers, de-duplicated (same id: first folder wins; same name + same program: merged)."""
    seen_ids, seen_prog, out = set(), {}, []
    for d in dirs or app_dirs():
        for root, _, files in os.walk(d):
            for n in sorted(files):
                if not n.endswith(".desktop") or n in seen_ids:
                    continue
                seen_ids.add(n)
                app = _parse(os.path.join(root, n))
                if app is None:
                    continue
                if app.command("/x") is None:
                    continue  # unsafe launcher (shell script reading the path): never offered
                # same name + same command line (field codes aside) = one program (wine repeats its launcher
                # per file type); a different ProgID / argument = a different handler, kept apart
                prog = (app.name, re.sub(r"%[a-zA-Z]", "", app.exec).strip())
                if prog in seen_prog:
                    seen_prog[prog].mimes += app.mimes
                    continue
                seen_prog[prog] = app
                out.append(app)
    return out


# The system's MIME database gets some 3D types wrong (.obj = "COFF object file") or doesn't know them.
EXTRA_MIME = {
    "obj": ["model/obj", "application/prs.wavefront-obj"],
    "stl": ["model/stl", "application/sla"],
    "3mf": ["model/3mf", "application/vnd.ms-3mfdocument"],
    "step": ["model/step", "application/vnd.step"], "stp": ["model/step", "application/vnd.step"],
    "gcode": ["text/x.gcode", "text/x-gcode"],
    "ply": ["application/vnd.ply", "model/ply"],
}


def mime_types(path):
    """MIME types of a file name and their parents (model/stl, application/octet-stream...)."""
    from PySide6.QtCore import QMimeDatabase
    t = QMimeDatabase().mimeTypeForFile(path, QMimeDatabase.MatchExtension)
    return EXTRA_MIME.get(ext_of(path), []) + [t.name()] + list(t.aliases()) + list(t.allAncestors())


def ext_of(path):
    return os.path.splitext(path)[1].lower().lstrip(".")


def load():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, SETTINGS)


def get_state(key, default=None):
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f).get(key, default)
    except (OSError, ValueError, AttributeError):
        return default


def set_state(key, value):
    try:
        with open(STATE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[key] = value
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE)


def for_file(path, apps=None, settings=None):
    """Programs to offer for this file: the user's pick for its extension (in their order, only the
    ones still installed), else the installed apps that declare its type (generic MIME types like
    octet-stream / text/plain don't count)."""
    apps = apps if apps is not None else installed()
    settings = settings if settings is not None else load()
    by_id = {a.id: a for a in apps}
    pick = [by_id[i] for i in settings.get(ext_of(path)) or [] if i in by_id]
    if pick:
        return pick  # (an empty or fully uninstalled pick falls back to the defaults)
    want = [m for m in mime_types(path) if m not in GENERIC_MIME]
    out = [a for a in apps if any(m in a.mimes for m in want)]
    return _distinct_labels(_one_per_program(out, ext_of(path)))


def _one_per_program(apps, ext):
    """Wine writes one launcher per file type for the same program (wine-extension-f3d, -f2d, -cam360...):
    when several of one program match, keep the one made for THIS extension."""
    by_name = {}
    for a in apps:
        by_name.setdefault(a.name, []).append(a)
    out = []
    for a in apps:
        group = by_name[a.name]
        exact = [g for g in group if g.id.lower().endswith(f"-{ext}.desktop")]
        if len(group) > 1 and exact and a not in exact:
            continue
        out.append(a)
    return out


GENERIC_MIME = ("application/octet-stream", "text/plain", "application/zip")  # 3mf "is a" zip: no PeaZip


def _distinct_labels(apps):
    """Two launchers with the same label (two Lychee AppImages): add what tells them apart."""
    seen = {}
    for a in apps:
        seen.setdefault(a.label, []).append(a)
    for same in seen.values():
        if len(same) > 1:
            for a in same:
                a.extra = os.path.splitext(a.id)[0]
    return apps


def set_pick(ext, ids):
    data = load()
    if ids:
        data[ext] = list(ids)
    else:
        data.pop(ext, None)  # nothing ticked = back to the defaults
    save(data)


def launch(app, file):
    argv = app.command(file)
    if not argv:
        raise ValueError(f"Can't read the launcher of {app.name}")
    return subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
