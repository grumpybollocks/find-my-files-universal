"""Imported first by every test file: the picture cache goes to a temp folder (never ~/.cache)
and the background picture builder is never started."""
import atexit
import os
import shutil
import tempfile

if "FINDFILES_CACHE" not in os.environ:
    _tmp = tempfile.mkdtemp(prefix="findfiles-test-cache-")
    os.environ["FINDFILES_CACHE"] = _tmp
    atexit.register(shutil.rmtree, _tmp, True)
os.environ["FINDFILES_NO_BUILDER"] = "1"
os.environ["FINDFILES_NO_VIEW_MEMORY"] = "1"  # view-memory tests switch it off for themselves
os.environ["FINDFILES_NO_WATCH"] = "1"  # watch tests switch it on for their own window
os.environ["FINDFILES_NO_MENU_EXEC"] = "1"  # right-click menus are built but not shown
if "FINDFILES_CONFIG" not in os.environ:
    _cfg = tempfile.mkdtemp(prefix="findfiles-test-config-")
    os.environ["FINDFILES_CONFIG"] = _cfg
    atexit.register(shutil.rmtree, _cfg, True)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# No real drives in tests: an empty folder list unless a test sets indexer.ROOTS itself
# (without this file the per-PC defaults would point at the real drives).
_roots_file = os.path.join(os.environ["FINDFILES_CONFIG"], "roots.json")
if not os.path.exists(_roots_file):
    with open(_roots_file, "w", encoding="utf-8") as _f:
        _f.write('{"version": 1, "roots": [], "never": ["/mnt/WINDRIVE"]}')


# A message box nobody patched would wait for a click forever (offscreen): fail the test instead.
from PySide6.QtWidgets import QMessageBox  # noqa: E402


def _unexpected_box(*args, **kw):
    raise AssertionError(f"unexpected message box in a test: {args[1:3]}")


for _name in ("warning", "question", "information", "critical"):
    setattr(QMessageBox, _name, staticmethod(_unexpected_box))
