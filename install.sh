#!/bin/sh
# Find My Files - check what is needed and add the app to the menu.
# Changes nothing outside your home folder and never asks for a password.
#   sh install.sh             check + menu entry
#   sh install.sh --desktop   also put an icon on the Desktop
#   sh install.sh --check     only check, write nothing
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
PY=$(command -v python3 || true)
[ -n "$PY" ] || { echo "python3 is not installed. Install it first."; exit 1; }

missing_py=""; missing_tools=""
"$PY" -c "import PySide6.QtWidgets, PySide6.QtSvg, PySide6.QtPdf" 2>/dev/null || missing_py="$missing_py pyside6"
"$PY" -c "import PySide6.QtMultimedia, PySide6.QtMultimediaWidgets" 2>/dev/null || missing_py="$missing_py qt-multimedia"
"$PY" -c "import numpy" 2>/dev/null || missing_py="$missing_py numpy"
for t in ffmpeg ffprobe 7z xdg-open; do command -v "$t" >/dev/null 2>&1 || missing_tools="$missing_tools $t"; done

ID=""; LIKE=""
[ -r /etc/os-release ] && . /etc/os-release && LIKE="${ID_LIKE:-}"
case " $ID $LIKE " in
  *" arch "*|*" manjaro "*) HOW="sudo pacman -S --needed pyside6 qt6-multimedia python-numpy ffmpeg 7zip xdg-utils" ;;
  *" debian "*|*" ubuntu "*) HOW="sudo apt install python3-pyside6.qtwidgets python3-pyside6.qtsvg python3-pyside6.qtpdf python3-pyside6.qtmultimedia python3-pyside6.qtmultimediawidgets python3-numpy ffmpeg 7zip xdg-utils   (not tested yet)" ;;
  *" fedora "*|*" rhel "*) HOW="sudo dnf install python3-pyside6 python3-numpy ffmpeg-free p7zip-plugins xdg-utils   (not tested yet)" ;;
  *" suse "*|*" opensuse "*) HOW="sudo zypper install python3-pyside6 python3-numpy ffmpeg 7zip xdg-utils   (not tested yet)" ;;
  *) HOW="python3 -m pip install --user -r \"$HERE/requirements.txt\"   plus ffmpeg and 7z from your package manager" ;;
esac

if [ -n "$missing_py$missing_tools" ]; then
  echo "Missing:$missing_py$missing_tools"
  echo "Install with:"
  echo "  $HOW"
  [ -n "$missing_py" ] && { echo "The app cannot start without the Python parts. Nothing was written."; exit 1; }
  echo "(Without ffmpeg/ffprobe there are no video pictures; without 7z no .7z/.rar contents.)"
else
  echo "Everything the app needs is installed."
fi
[ "${1:-}" = "--check" ] && exit 0

APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
ENTRY="$APPS/find-my-files.desktop"
cat > "$ENTRY" <<EOF
[Desktop Entry]
Type=Application
Name=Find My Files
Comment=Search every drive for a work file by name, with a preview
Exec=$PY "$HERE/findfiles.py"
Path=$HERE
Icon=$HERE/icons/findfiles.svg
Terminal=false
Categories=Utility;FileTools;
EOF
echo "Menu entry written: $ENTRY"
if [ "${1:-}" = "--desktop" ]; then
  D=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")
  if [ -d "$D" ]; then cp "$ENTRY" "$D/Find My Files.desktop"; chmod +x "$D/Find My Files.desktop"; echo "Desktop icon written: $D/Find My Files.desktop"; fi
fi
echo "Start it from the app menu, or:  $PY \"$HERE/findfiles.py\""
