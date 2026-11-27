# Changelog

## 1.7.1
- **Any folder depth.** A chain of more than about 1,000 nested folders used to stop the whole scan;
  every depth is read now, also by the quick check at start.
- **Leaves room for other programs.** The live watch uses at most half of the folder watches the
  system allows (some setups allow only 8,192), so the file manager and other programs keep
  noticing changes. Folders beyond that are still checked at the next start.
- **A scan saves only into the file list it started on** (fixed a rare test failure).
- Checked on real drives: the new folder walk lists exactly the same files as before, a little
  faster. Unreadable folders are skipped, folder links are never followed (no loops), names with
  accents or emoji are found, a file at the system's path-length limit is found, 100,000 files in
  one folder scan in under a second.
- Known: file names that are not valid UTF-8 (some old Windows drives) are skipped, and the app
  does not say so yet.

## 1.7.0
- **Type menu with submenus.** Each group (Text & documents, 3D models, Images...) lists its file
  endings with how many files you have of each, e.g. Text & documents > PDF. Endings you have no
  files of are left out. Your choice is remembered.
- **"Did you mean?"** When a search finds nothing, a clickable suggestion learnt from your own file
  names. Two swapped letters count as one typo, a word that already finds files is never
  "corrected", and only a search that really finds files is suggested. Works in the background.
- **Pictures fill the preview.** Small pictures are enlarged to the panel; zoom out with the wheel
  down to real size. Double-click = fit / real size.

## 1.6.0
- **PDF pages**: Previous / Next under the preview, Page Up / Page Down, Home / End; also in full
  screen.
- **Markdown shown formatted** (headings, lists, tables) with a **Plain text** switch. Links and
  pictures inside are never opened. Files over 32 KB stay plain so the window never freezes
  (measured: a 200 KB table takes 5 s to format).
- **Full screen** button under the picture, shown for a picture or a video.

## 1.5.0: the preview is a media viewer
- **Pictures zoom**: wheel at the pointer, drag to move, double-click = fit / 100 %, keys
  `+` `-` `0` `1`. Photos are decoded up to 24 megapixels and turned the way the camera held them.
- **Full screen** with **F** or **F11**; the arrow keys step through the results.
- **Video full screen keeps its controls**; they hide after 2.5 s without movement.
- **Text files shown as text** (the first 200 KB), **sound files play** in the same player.
- Sharper previews, drawn for the panel's real size. New type filters: Audio, Text & documents.

## 1.4.1
- Tests can never write into the real file list (their own temporary data folder, plus a guard test).

## 1.4.0
- **Drive chips** in the bottom bar: click = only that drive, click again = all, right-click =
  Settings.
- **Columns**: drag to resize or move; right-click the titles to hide or show (extra: Folder, Ext,
  Drive), sort by any column either way, or reset.
- Buttons and chips keep a visible outline in dark themes.
- **Low-priority words** are a setting. `install.sh` / `uninstall.sh`.

## 1.3
- **Whole drives**: add a drive as one entry; a folder inside it keeps its own name and kind, and an
  unticked folder is left out even when a ticked drive contains it.
- **Skipped folder names** (games, program folders, assets...) are a setting. Changing them triggers
  one full index.

## 1.2
- **Quick start**: only folders that changed since last time are read (0.3 s instead of 6 to 11 s on
  a list of about 300,000 files). **Full index** (F5) on request, at lowered priority.
- **Video player** in the preview: play / pause, seek, volume, mute, full screen.
- Saved pictures in WebP, about 5x smaller than PNG; Settings shows the space used.
- Fixed: the window froze for about 50 s when one long results list replaced another.

## 1.0 / 1.1
- **Settings**: tick which folders to search, add / remove, Work or Copy, with or without
  subfolders, live "plugged in?" status. System folders, Steam / Proton folders, overlapping folders
  and the never-search list are refused (also through links or bind mounts). Rebuild from scratch,
  clear search history.
- **Bottom bar**: scan progress with time left, one chip per drive (unplugged drives keep their
  files searchable), picture progress.
- **Idle = no work**: new files are noticed by the system (inotify), not by polling.
- **Videos**: a real frame with size, length and codec, and Play in the preview.
- **Remembers** your last search, filter, view, window and panel size; recent searches drop down.

## Before 1.0
- Search every drive at once; Romanian and English words, accents optional, plurals.
- Real previews: STL, OBJ, 3MF, GLB drawn from their geometry; slicer, CAD, vector, G-code and
  laser files show the picture saved inside them or their toolpath.
- Model size in mm and `60mm` size search, List / Grid, Related files, exact copies (whole-file
  hash), search inside zip / rar / 7z, drag a result into another program, Open with.
- Two full code audits by independent reviewers; every fix has a test.
