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
grimoire                  # folder you are in, unless setup says otherwise
grimoire .                # this folder, always
grimoire library          # the shelf in config, default ~/scripts
grimoire path/to/file.md
grimoire setup            # ask where a bare launch opens
```

`folio` still runs the same program.

Config is `~/.grimoire/config.json`:

```json
{
  "name": "GRIMOIRE",
  "open": "cwd",
  "library": "~/scripts"
}
```

`open` is `cwd`, `library`, or a path. esc leaves a file or a folder. q quits. ? is help.

Plugins are plain Python files in `~/.grimoire/plugins`. Each one defines `register(api)`. Core keys win. A handler returns False to quit the app. `grimoire plugins` lists what loaded. Copy `examples/linecount.py` there to try one: ctrl+w reports the line count.

```python
def register(api):
    api.suffix(".todo")          # show this extension in the list
    api.markdown(".mdx")         # render it as markdown
    api.render(".todo", paint)   # paint(line) -> spans, or None to fall through
    api.key("ctrl+w", count)     # normal mode. also mode="list" or "insert"
    api.on("open", remember)     # also "save" and "leave"
```


