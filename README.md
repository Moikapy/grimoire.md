# Grimoire

Terminal reader for scripts. The line under the cursor stays raw and fixed on the middle row. Every other line of a markdown file is rendered, with `#`, `**`, and `_` markers hidden. Other text files stay plain, bright on the current line.

The name is the lore. Phylactery is already Lich's session files, and Ossuary is the desktop shell, so this book is the grimoire. The command and the config keys stay plain.

## Install

Needs Python 3. curses is in the standard library.

```sh
git clone <url> grimoire
cd grimoire
ln -sfn "$(pwd)/grimoire.py" ~/.local/bin/grimoire
grimoire --help
```

No install step copies config. Each person gets their own `~/.grimoire/config.json` the first time they run `grimoire setup`. Plugins stay in `~/.grimoire/plugins` and are not part of the clone.

## Use

```sh
grimoire                  # ~/.grimoire/pages
grimoire -c               # the folder you are in
grimoire --local          # same as -c
grimoire path/to/file.md
grimoire setup            # shelf path and the name on screen
```

`folio` still runs the same program.

Keys are `~/.grimoire/keys.json`. `grimoire keys` writes it if it is missing. Action names stay fixed. You change the key list. ctrl+c always quits.

Agents do not open the reader. They use `grimoire pages`, `grimoire read`, `grimoire search`, and `grimoire write`. `grimoire mcp` is the same calls over stdio for an MCP client.

Config is `~/.grimoire/config.json`:

```json
{
  "name": "GRIMOIRE",
  "open": "library",
  "library": "~/.grimoire/pages"
}
```

`open` is `cwd`, `library`, or a path. esc leaves a file or a folder. q quits. On the shelf, `n` makes a page and `N` makes a folder. `/` finds a file by name. Inside a page, `/` finds text. ? opens the keys and settings menu. The bottom bar shows only the hints that fit. A narrow pane keeps the first ones and `?`.

`.csv` and `.tsv` files open as a grid. The header row stays on top and the first column stays on the left while you scroll. Arrows move one cell at a time, and numbers are right-aligned. The footer shows the full value of the cell you are on. `v` flips to record view: one row at a time, with the header names down the left. In record view, up and down move between fields, and left and right move between records. `i` edits the raw line with the cursor at the start of the cell. Esc goes back to the grid.

Colors are `~/.grimoire/theme.json`. `grimoire theme` writes it if it is missing. Names are `black`, `red`, `green`, `yellow`, `blue`, `magenta`, `cyan`, `white`, and `default`. A number from 0 to 255 also works when the terminal has that color. The background is always the terminal's. The font is the terminal's too. Restart Grimoire after an edit.

Plugins are plain Python files in `~/.grimoire/plugins`. Each one defines `register(api)`. Core keys win. A handler returns False to quit the app. `grimoire plugins` lists what loaded. Copy `examples/linecount.py` there to try one: ctrl+w reports the line count.

## Images

A line that is only `![alt](path.png)` shows the alt text and the path. The cursor line stays the raw markdown. Nothing is downloaded. A web address stays text.

Two optional tools. Neither is required.

```sh
grimoire images
```

| tool | if it is installed | if it is not |
| --- | --- | --- |
| `chafa` | a small text preview in the reader | the alt text and the path |
| `imv` | enter or o opens the file | `xdg-open` is the backup |
| neither opener | | the footer says `install imv to view images` |

```sh
# Arch
sudo pacman -S chafa imv
```

`chafa` is only used as symbols, so the preview is text and works in Ghostty, foot, Alacritty, Herdr, or ssh. It does not send Kitty or sixel sequences. The real picture is the viewer.

```python
def register(api):
    api.suffix(".todo")          # show this extension in the list
    api.markdown(".mdx")         # render it as markdown
    api.render(".todo", paint)   # paint(line) -> spans, or None to fall through
    api.key("ctrl+w", count)     # normal mode. also mode="list" or "insert"
    api.on("open", remember)     # also "save" and "leave"
```


