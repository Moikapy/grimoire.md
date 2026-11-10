# Grimoire

Terminal reader for scripts. The line under the cursor stays raw and fixed on the middle row. Every other line of a markdown file is rendered, with `#`, `**`, and `_` markers hidden. Other text files stay plain, bright on the current line.

The name is the lore. Phylactery is already Lich's session files, and Ossuary is the desktop shell, so this book is the grimoire. The command and the config keys stay plain.

## Install

Runs on Linux, macOS, and Windows. Needs Python 3.10 or newer and nothing else (Windows also pulls in `windows-curses`, automatically).

Straight from GitHub, no registry needed. [uv](https://docs.astral.sh/uv/) installs Python programs the way `npm install -g` installs Node ones: each tool gets its own environment and its command lands on your PATH. It also downloads a suitable Python by itself, so the one your system ships does not matter. Get it with `brew install uv`, or `curl -LsSf https://astral.sh/uv/install.sh | sh`.

```sh
uv tool install git+https://github.com/Moikapy/grimoire.md
grimoire --help
uv tool upgrade grimoire-md   # later, to update
```

The upgrade pulls the newest commit. `grimoire --version` says what you have; the newest is the `version` line in [`package.json`](package.json).

`pipx install git+https://github.com/Moikapy/grimoire.md` does the same with the Python you already have.

With npm instead: `npm install -g github:Moikapy/grimoire.md`. That is a small Node shim that still runs on your Python. Set `GRIMOIRE_PYTHON` if the right one is not `python3`, and on Windows run `pip install windows-curses` yourself.

On Android, Termux counts as Linux. On a phone or tablet without a terminal, ssh to a machine that has it.

From a clone:

```sh
git clone https://github.com/Moikapy/grimoire.md grimoire
cd grimoire
ln -sfn "$(pwd)/grimoire.py" ~/.local/bin/grimoire
```

## Use

```sh
grimoire                  # ~/.grimoire/pages
grimoire -c               # the folder you are in
grimoire --local          # same as -c
grimoire path/to/file.md
grimoire setup            # shelf path and the name on screen
```

Keys are `~/.grimoire/keys.json`. `grimoire keys` writes it if it is missing. Action names stay fixed. You change the key list. ctrl+c always quits.

## Agents

Agents do not open the reader. They use `grimoire pages`, `grimoire read`, `grimoire search`, and `grimoire write`. `grimoire mcp` is the same calls over stdio for an MCP client.

[`skills/grimoire/SKILL.md`](skills/grimoire/SKILL.md) is a skill that teaches an agent to install Grimoire, give a project its own library, and read and write pages safely. To hand a project to an agent, paste this:

```text
Set up Grimoire for this project. Follow the skill at
https://github.com/Moikapy/grimoire.md/blob/master/skills/grimoire/SKILL.md
Keep the pages in docs/grimoire, start an index page, and tell me what you wrote.
```

To have the skill on hand in every session, copy it in. For Claude Code:

```sh
mkdir -p ~/.claude/skills/grimoire
curl -fsSL https://raw.githubusercontent.com/Moikapy/grimoire.md/master/skills/grimoire/SKILL.md \
  -o ~/.claude/skills/grimoire/SKILL.md
```

Use `.claude/skills/grimoire/` inside a repo to share it with everyone on that project.

To connect the MCP server to one project:

```sh
claude mcp add grimoire -e GRIMOIRE_LIBRARY="$PWD/docs/grimoire" -- grimoire mcp
```

## Settings


Config is `~/.grimoire/config.json`:

```json
{
  "name": "GRIMOIRE",
  "open": "library",
  "library": "~/.grimoire/pages"
}
```

`open` is `cwd`, `library`, or a path. esc leaves a file or a folder. ctrl+q quits. On the shelf, `n` makes a page and `N` makes a folder. `/` finds a file by name. Inside a page, `/` finds text. ? opens the keys and settings menu. The bottom bar shows only the hints that fit. A narrow pane keeps the first ones and `?`.

`.csv` and `.tsv` files open as a grid. The header row stays on top and the first column stays on the left while you scroll. Arrows move one cell at a time, and numbers are right-aligned. The footer shows the full value of the cell you are on. `v` flips to record view: one row at a time, with the header names down the left. In record view, up and down move between fields, and left and right move between records. Enter edits the cell you are on in the footer. Enter again keeps the change, and Esc cancels. Only that cell's text changes: quotes are added only when the value needs them (a comma, a tab, or a quote mark), and the rest of the line stays byte-for-byte. `i` edits the raw line, for a quoted value that spans several lines. Save keeps the file's own line endings (`\n` or `\r\n`) and whether it ends with one.

Colors are `~/.grimoire/theme.json`. `grimoire theme` writes it if it is missing. Names are `black`, `red`, `green`, `yellow`, `blue`, `magenta`, `cyan`, `white`, and `default`. A number from 0 to 255 also works when the terminal has that color. The background is always the terminal's. The font is the terminal's too. Restart Grimoire after an edit.

Plugins are plain Python files in `~/.grimoire/plugins`. Each one defines `register(api)`. A plugin runs with your full permissions the moment Grimoire starts, so only copy in files you have read. Core keys win. A handler returns False to quit the app. `grimoire plugins` lists what loaded. Copy `examples/linecount.py` there to try one: ctrl+w reports the line count.

## Mouse

The scroll wheel moves through a page three lines at a time, through a table by rows, and through the shelf and the keys menu. In the graph it zooms, and a click selects a node. Nothing needs the mouse.

## Links

In a markdown page, enter (or `o`) on a line with a link opens the page it names. `[[page]]`, `[[page|shown text]]`, and `[text](page.md)` all work. With several links on one line, it takes the one under the cursor, or the next one to its right. `[[page#heading]]` lands on that heading, and `[[#heading]]` jumps within the page.

Esc goes back to the page and the line you came from. Follow three links and Esc unwinds all three, then returns to the graph or the shelf you started from. A page with unsaved edits asks you to save before it lets you leave through a link. A link to a page that does not exist says so in the footer. Web addresses are not opened.

## Copy

In a page, `y` copies the line you are on. `V` starts a selection: move up or down, and `y` copies those lines (Esc drops the selection). `Y` copies the whole page. In a `.csv` or `.tsv` grid, `y` copies the cell. Copying never changes the file. The keys are `copy`, `select`, and `copy_page` in `keys.json`.

The text goes to the system clipboard through `wl-copy`, `xclip` or `xsel`, `pbcopy`, or `clip`, whichever the machine has. Inside tmux it also becomes a tmux buffer. Over ssh, or with none of those tools, Grimoire asks the terminal itself to take the text (OSC 52), so it lands on the machine you are sitting at; most current terminals allow that, and it tops out near 75 KB.

To select with the mouse the way the terminal normally does, hold shift while you drag.

## tmux, Herdr, ssh

Grimoire runs inside a multiplexer or over ssh with nothing to set up. Text you type can be any script, not only ASCII.

If `TERM` names a terminal the machine has no entry for (common inside tmux or over ssh from a newer terminal), Grimoire falls back to `xterm-256color` instead of failing.

Two tmux settings are yours to make:

```sh
set -g mouse on          # clicks and the scroll wheel reach Grimoire
set -sg escape-time 10   # older tmux waits half a second on esc
```

Without the mouse everything still works from the keys.

## Graph

`g` on the shelf opens the graph of the whole library. ctrl+g on a page opens it with that page selected (`g` on a page still jumps to the top, like everywhere else). Pages that link to nothing still sit on their own: every node repels the others, links act as springs, and a little gravity pulls the whole thing to the middle. The same library settles in the same shape every time.

The picture is drawn with braille dots, two per column and four per row, so lines stay lines. Each folder gets one color from the theme (`graph_1` to `graph_8`, in sorted folder order; top-level pages are their own group) and each link is a dim line. Color roles go in `~/.grimoire/theme.json` like the rest. The selected page and its neighbors get bold labels and bright edges; other labels appear when they fit. Arrow keys or `hjkl` move the selection to the nearest node in that direction. Enter opens the page, and Esc on the page returns to the graph, Esc in the graph returns to where you came from. The node's size grows with its link count.

`+` and `-` zoom, `0` re-fits the whole graph, and `w a s d` pans. Labels fade like in Obsidian: zoomed out you see only the selected page and its neighbors, zoomed in you see more. The mouse works too: click to select, click the same node again or double-click to open it, scroll wheel zooms around the pointer. Without a mouse everything still works from the keys.

`L` turns on the local graph: only the nodes within N link-steps of the selected page, in either direction. `[` and `]` change N from 1 to 5, and turning L off brings the whole graph back. The footer says `local, depth N`.

Links to pages that do not exist show as hollow dim nodes with the raw link text as the label when selected. `m` hides them, `m` again shows them.

`/` filters: type a few letters, and only pages with those letters in the slug or the title stay, with the edges between them. Esc clears the filter. ? lists the graph keys.

Physics is O(n^2) per step, so a few hundred nodes animate at a comfortable pace; above 400 nodes the graph settles before the first draw, capped at two seconds, then keeps settling as you watch (an oversized graph stops moving after about twelve seconds). Opening a very large library takes a moment the first time. Node dragging with the mouse is not supported.

## Images

A line that is only `![alt](path.png)` shows the alt text and the path. The cursor line stays the raw markdown. Nothing is downloaded. A web address stays text. Only a picture beside the page or below it gets a preview. An absolute path shows as its caption and still opens on enter. Enter opens image files only.

Two optional tools. Neither is required.

```sh
grimoire images
```

| tool | if it is installed | if it is not |
| --- | --- | --- |
| `chafa` | a small text preview in the reader | the alt text and the path |
| `imv` | enter or o opens the file | `xdg-open` on Linux, `open` on macOS, the default app on Windows |
| no opener | | the footer says `install imv to view images` |

```sh
# Arch
sudo pacman -S chafa imv

# macOS
brew install chafa
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

## Limits

A file longer than 8000 lines opens read-only, showing the first 8000. Saving it would drop the rest.

## License

MIT. See `LICENSE`.
