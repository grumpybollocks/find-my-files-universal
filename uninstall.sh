#!/bin/sh
# Find My Files - remove the menu entry and Desktop icon. Add --data to also remove the app's own
# file list, settings and saved pictures. Your files are never touched.
set -eu
rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/find-my-files.desktop"
D=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")
rm -f "$D/Find My Files.desktop"
echo "Menu entry and Desktop icon removed."
if [ "${1:-}" = "--data" ]; then
  rm -rf "$HOME/.local/share/findfiles" "$HOME/.cache/findfiles" "$HOME/.config/findfiles"
  echo "File list, saved pictures and settings removed."
else
  echo "Kept: ~/.config/findfiles (settings), ~/.local/share/findfiles (file list), ~/.cache/findfiles (pictures)."
  echo "Run with --data to remove those too."
fi
