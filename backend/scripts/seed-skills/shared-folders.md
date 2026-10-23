# Shared folders

Use this when the owner wants you to work on files on their own computer or phone (a folder they shared in Settings → Shared folders), or asks about a file "on my laptop", "in my Downloads", or similar. The files stay on their device: you read only the pieces you need and nothing is uploaded or stored.

## The command

`folders` is `python3 "$SCRIPTS_DIR/folders.py"`. Paths start with the shared folder's name.

```bash
F="python3 $SCRIPTS_DIR/folders.py"
$F list                                  # shared folders: device, rights, state, id
$F ls "Downloads"                        # contents; big folders page with --cursor
$F info "Downloads/report.pdf"
$F read "Downloads/notes.txt"            # first 256 KB; --offset N / --length N
$F read "Downloads/app.log" --tail 20000 # the end of a big file
$F show "Downloads/scan.jpg"             # temporary copy to view; photos are shrunk on the device first
$F write "Downloads/summary.md" --text "…"        # or --from-file PATH, or stdin
$F move "Downloads/IMG_1.jpg" "Photos/2026-10-01.jpg"
$F mkdir "Downloads/Sorted"
$F bin "Downloads/old.zip"               # moves it to the "Möbius bin" folder
```

`write`, `move`, `mkdir` and `bin` work only in folders shared as **Read & edit** (Chrome or Edge). Safari, Firefox and Brave share folders read-only, for one visit.

## Rules

- Read the least you need: `ls`, `info`, then a slice or `--tail`. Every byte you read goes to the AI model.
- For photos, `show` one at a time and look at each copy right away: only the 3 newest copies are kept. The device shrinks photos to 2,576 px first, so big phone photos work; HEIC may not open in Chrome.
- Never delete. `bin` is the only way to remove something; the owner empties the bin. Do not try to work around this with `write` or `move`.
- Moving whole folders is not possible in browsers. Move the files inside it one by one, after telling the owner how many.
- Before changing more than a few files, tell the owner what you will do and get a yes.
- Keep `write` content exact. Use `--overwrite` only when the owner asked to replace that file.
- A refusal is an answer, not a bug. Relay its fix in plain words:
  - `offline`: Möbius is not open in that browser. Ask the owner to open it there.
  - `needs_permission`: the owner taps **Reconnect** in Settings → Shared folders.
  - `needs_pick`: the owner taps **Pick again** (Safari, Firefox and Brave forget folders between visits).
  - `paused`: the owner paused sharing; they can tap **Resume**.
  - `read_only`: the folder is Read only; ask before suggesting they share it again with Read & edit.
- If no folder is shared, tell the owner they can add one in Settings → Shared folders. Do not ask them to upload instead unless they prefer that.
