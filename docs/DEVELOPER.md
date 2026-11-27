# Developer notes

Plain Python 3 + PySide6, no build step. Run `python3 findfiles.py`.

## Files

| File | Job |
| --- | --- |
| `findfiles.py` | the window: search box, results (list / grid), preview, bottom bar, Settings |
| `indexer.py` | reads the folders into a SQLite file list; quick start-up check; live watching |
| `search.py` | name matching (stems, accents, English and Romanian synonyms), ranking, size search, related files |
| `roots.py` | the settings file: folders, never-read drives, skipped names, low-priority words |
| `previews.py` | draws or extracts a preview for each file type |
| `thumbs.py` | saved pictures and model sizes; background builder at the lowest priority |
| `archives.py` | lists and reads files inside zip / rar / 7z |
| `copies.py` | exact-copy search |
| `filetypes.py` | file types, their names and the filter groups |
| `programs.py` | "Open with" programs per type, small saved window state |
| `player.py` | the video player panel (loaded only when a video is played) |

## Rules the code keeps

- **Never write to the searched folders.** Only `~/.config/findfiles`, `~/.local/share/findfiles`
  and `~/.cache/findfiles` are written.
- **Nothing machine-specific in the code.** Folders, drives and names come from the settings
  file. A test fails if a drive ID or a host-name check appears in the code.
- **A "never" drive is not touched at all**, not even to check whether it is mounted: mount
  state comes from `/proc/self/mountinfo`.
- **The window never waits for a drive.** Scans, previews and status checks run in threads.
- **No work while idle.** Timers stop when the window is hidden; a full scan only runs on request.

## Environment switches (used by the tests)

`FINDFILES_CONFIG`, `FINDFILES_CACHE`, `FINDFILES_DATA` (other folders for settings, pictures and the file list),
`FINDFILES_NO_BUILDER`, `FINDFILES_NO_WATCH`, `FINDFILES_NO_VIEW_MEMORY`, `FINDFILES_NO_MENU_EXEC`.

## Tests

```
python3 -m unittest discover -s tests
```

Every test file imports `tests/safe_env.py` first: settings and pictures go to temporary
folders, the folder list is empty, and a message box nobody expected fails the test.

## Adding things

- **A file type:** `filetypes.py` (name and group), then a reader in `previews.py`.
- **A language:** add word pairs to the synonym list at the top of `search.py`.
- **A distribution:** add its package names to `install.sh` and the README table once tested.
