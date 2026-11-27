# Changelog

## 1.6.0
- PDF pages: Previous / Next under the preview, Page Up / Down, Home / End; also in full screen.
- Markdown shown formatted (headings, lists, tables) with a Plain text switch; links and pictures
  inside are never opened; files over 32 KB stay plain so the window never freezes.
- A Full screen button under the picture, shown when a picture or a video is on screen.
- A new search with nothing selected no longer leaves an old text file on screen.

## 1.5.0
- The preview is a media viewer: pictures zoom (wheel, drag, double-click), full screen with
  arrow keys through the results, video full screen keeps its controls, text files are shown
  as text, sound files play. Previews are drawn for the panel's size.
- Photos load about twice as fast and are turned the way the camera held them.

## 1.4.1
- The tests can no longer touch the real file list: it has its own override (`FINDFILES_DATA`),
  every test file loads the safety setup first, and a guard test checks it.
- MIT licence added.

## 1.4.0
- First release meant for any PC: no built-in folder lists, drives or machine names.
  A new PC starts with its home folders; everything else is added in Settings.
- `install.sh` / `uninstall.sh`: dependency check, menu entry, optional Desktop icon.
- Low-priority words are a setting (were fixed folder names).
- "Open folder" works with file managers other than Dolphin.
- Drive chips filter the results to one drive; with an empty search box they list its newest files.
- Result columns can be dragged, moved and hidden; extra columns Folder, Ext and Drive; a
  right-click menu on the column titles with more ways to sort.
- Buttons, chips and frames keep a visible outline in dark themes.

## 1.3
- Whole drives can be searched, with folders inside them under their own names.
- Skipped folder names (games, program folders, assets) are a setting.

## 1.2
- Quick start: only folders that changed are read. "Full index" on request.
- Video player in the preview. Saved pictures in WebP, about a quarter of the size.
