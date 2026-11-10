#!/usr/bin/env python3
"""Grimoire — read scripts in the terminal.

The line under the cursor stays raw and fixed on the middle row.
Every other line is rendered. Heading marks, bold, and italic markers
are hidden. That is the behavior measured in the NOTE screen recording.

This is a reader, not that app. Format keys the recording never applied
are not invented here. Only .md files render. Other scripts stay plain
text, bright on the current line.

The display name is lore. The command and the config keys stay plain.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import locale
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

__version__ = "0.2.0"

try:
    import curses
except ImportError:  # Windows has no curses in the standard library
    print("grimoire: this Python has no curses. On Windows run: pip install windows-curses", file=sys.stderr)
    raise SystemExit(1)

try:
    import termios
except ImportError:  # Windows
    termios = None

try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:  # a locale the system does not have
    pass
os.environ.setdefault("ESCDELAY", "25")

TEXT_EXT = {
    ".md", ".markdown", ".txt", ".sh", ".bash", ".zsh", ".fish",
    ".py", ".lua", ".js", ".ts", ".mjs", ".cjs", ".rb", ".go",
    ".rs", ".toml", ".yaml", ".yml", ".json", ".css", ".html",
    ".conf", ".ini", ".service", ".desktop", ".csv", ".tsv",
}
TABLE_EXT = {".csv": ",", ".tsv": "\t"}
TABLE_CELL_MAX = 28
MD_SUFFIXES = (".markdown", ".md")
MARKDOWN_EXT = set(MD_SUFFIXES)
REMOTE = ("http://", "https://", "mailto:")
SKIP_DIRS = {"__pycache__", "node_modules", "dist", "build", ".venv", "venv"}
MAX_LINES = 8000
READ_ONLY = f"read-only: only the first {MAX_LINES} lines are loaded"
CARD_W = 60

# Styles the painter understands.
TITLE = "title"
HEADING = "heading"
BOLD = "bold"
ITALIC = "italic"
CODE = "code"
LINK = "link"
FOLDER = "folder"
SELECTED = "selected"
DIM = "dim"
FOCUS = "focus"
CURSOR = "cursor"
FOOTER = "footer"
TEXT = "text"


@dataclass(frozen=True)
class Entry:
    name: str
    detail: str
    kind: str  # folder | file
    path: Path


def config_path() -> Path:
    override = os.environ.get("GRIMOIRE_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".grimoire" / "config.json"


def default_config() -> dict[str, str]:
    return {"name": "GRIMOIRE", "open": "library", "library": "~/.grimoire/pages"}


def load_config() -> dict[str, str]:
    cfg = default_config()
    path = config_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"grimoire: ignoring {path} ({exc})", file=sys.stderr)
            data = {}
        if isinstance(data, dict):
            for key in ("name", "open", "library"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    cfg[key] = value.strip()
    if os.environ.get("GRIMOIRE_OPEN"):
        cfg["open"] = os.environ["GRIMOIRE_OPEN"]
    if os.environ.get("GRIMOIRE_LIBRARY"):
        cfg["library"] = os.environ["GRIMOIRE_LIBRARY"]
    elif os.environ.get("FOLIO_DIR"):
        cfg["library"] = os.environ["FOLIO_DIR"]
    return cfg


NAMED_COLORS = {
    "black": 0,
    "red": 1,
    "green": 2,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7,
}
DEFAULT_THEME = {
    "text": "default",
    "title": "blue",
    "heading": "blue",
    "bold": "blue",
    "italic": "green",
    "code": "cyan",
    "link": "cyan",
    "folder": "magenta",
    "footer": "default",
    "graph_edge": "default",
    "graph_edge_hi": "cyan",
    "graph_1": "yellow",
    "graph_2": "cyan",
    "graph_3": "green",
    "graph_4": "magenta",
    "graph_5": "red",
    "graph_6": "blue",
    "graph_7": "white",
    "graph_8": "245",
}
THEME_PAIRS: dict[str, int] = {}


def theme_path() -> Path:
    override = os.environ.get("GRIMOIRE_THEME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".grimoire" / "theme.json"


def color_index(name: str) -> int | None:
    key = name.strip().lower()
    if key in ("", "default"):
        return None
    if key in NAMED_COLORS:
        return NAMED_COLORS[key]
    if key.isdigit():
        return int(key)
    raise ValueError(key)


def load_theme() -> dict[str, str]:
    theme = dict(DEFAULT_THEME)
    path = theme_path()
    if not path.is_file():
        return theme
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"grimoire: ignoring {path} ({exc})", file=sys.stderr)
        return theme
    if not isinstance(data, dict):
        print(f"grimoire: ignoring {path}", file=sys.stderr)
        return theme
    for role in DEFAULT_THEME:
        value = data.get(role)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            color_index(value)
        except ValueError:
            print(f"grimoire: theme {role}: unknown color {value}", file=sys.stderr)
            continue
        theme[role] = value.strip().lower()
    return theme


def write_default_theme(path: Path | None = None) -> Path:
    dest = path or theme_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(DEFAULT_THEME, indent=2) + "\n", encoding="utf-8")
    return dest


def atomic_write(path: Path, data: bytes) -> None:
    """A crash mid-write leaves the old file, not half of the new one.

    Writes through a symlink to its target and keeps the old file's mode and owner.
    A hard-linked file, or a folder we cannot make a temp file in, is written in place.
    """
    path = Path(os.path.realpath(path))
    try:
        old = path.stat()
    except FileNotFoundError:
        old = None
    if old is not None and old.st_nlink > 1:
        path.write_bytes(data)  # a new inode would cut the other links
        return
    try:
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    except PermissionError:
        path.write_bytes(data)
        return
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if old is None:
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp, 0o666 & ~umask)
        else:
            os.chmod(tmp, old.st_mode & 0o7777)
            if hasattr(os, "getuid") and (old.st_uid, old.st_gid) != (os.getuid(), os.getgid()):
                try:
                    os.chown(tmp, old.st_uid, old.st_gid)
                except PermissionError:
                    pass
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        folder = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(folder)
    except OSError:
        pass
    finally:
        os.close(folder)


def save_config(cfg: dict[str, str]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, (json.dumps(cfg, indent=2) + "\n").encode("utf-8"))
    return path


def library_path(cfg: dict[str, str]) -> Path:
    path = Path(cfg["library"]).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def scripts_dir() -> Path:
    """Library folder from config. Kept for older callers."""
    return library_path(load_config())


def plugins_dir() -> Path:
    override = os.environ.get("GRIMOIRE_PLUGINS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".grimoire" / "plugins"


def parse_key(name: str) -> int:
    raw = name.strip()
    if len(raw) == 1:
        return ord(raw)
    key = raw.lower()
    if key in ("esc", "escape"):
        return 27
    if key in ("enter", "return"):
        return 10
    if key in ("backspace", "bs"):
        return 127
    if key == "tab":
        return 9
    if key in ("pgup", "pageup"):
        return curses.KEY_PPAGE
    if key in ("pgdn", "pagedown", "pgdown"):
        return curses.KEY_NPAGE
    if key in ("delete", "del"):
        return curses.KEY_DC
    if key == "home":
        return curses.KEY_HOME
    if key == "end":
        return curses.KEY_END
    if key == "space":
        return ord(" ")
    if key.startswith("ctrl+") and len(key) == 6 and key[-1].isalpha():
        return ord(key[-1]) & 0x1F
    if key.startswith("f") and key[1:].isdigit():
        number = int(key[1:])
        if 1 <= number <= 12:
            return curses.KEY_F1 + number - 1
    arrows = {
        "up": curses.KEY_UP,
        "down": curses.KEY_DOWN,
        "left": curses.KEY_LEFT,
        "right": curses.KEY_RIGHT,
    }
    if key in arrows:
        return arrows[key]
    raise ValueError(f"unknown key {name}")


def typed(key: int | str) -> str:
    """The character a key types, or "" for a control or function key."""
    if isinstance(key, int):
        return chr(key) if 32 <= key < 127 else ""
    return key if key.isprintable() else ""


def read_key(stdscr) -> int | str:
    """One key. ASCII and function keys are ints, as the key map expects.

    Anything else typed (é, ✓, 語) comes back as the character itself. -1 is a timeout.
    """
    try:
        key = stdscr.get_wch()
    except curses.error:
        return -1
    if isinstance(key, str):
        return ord(key) if len(key) == 1 and ord(key) < 128 else key
    return key


def key_codes(name: str) -> set[int]:
    """One name can be several bytes. Enter and backspace are not one code."""
    key = name.strip().lower()
    if key in ("enter", "return"):
        return {10, 13, curses.KEY_ENTER}
    if key in ("backspace", "bs"):
        return {curses.KEY_BACKSPACE, 127, 8}
    return {parse_key(name)}


# Action names are fixed. The file only changes which keys fire them.
# ctrl+c always quits, even if this file never mentions it.
DEFAULT_KEYS: dict[str, dict[str, list[str]]] = {
    "list": {
        "quit": ["ctrl+q"],
        "back": ["esc", "left", "h"],
        "up": ["up", "k"],
        "down": ["down", "j"],
        "open": ["enter", "right", "l"],
        "new_page": ["n"],
        "new_folder": ["N"],
        "find": ["/"],
        "help": ["?", "f1"],
        "graph": ["g"],
    },
    "normal": {
        "leave": ["esc"],
        "quit": ["ctrl+q"],
        "insert": ["i"],
        "append": ["a"],
        "up": ["up", "k"],
        "down": ["down", "j"],
        "left": ["left", "h"],
        "right": ["right", "l"],
        "line_start": ["home", "0"],
        "line_end": ["end", "$"],
        "top": ["g"],
        "bottom": ["G"],
        "page_up": ["pgup"],
        "page_down": ["pgdn"],
        "save": ["f2", "ctrl+s"],
        "reload": ["f5"],
        "help": ["f1"],
        "find": ["/"],
        "find_next": ["ctrl+n"],
        "find_prev": ["ctrl+p"],
        "view": ["enter", "o"],
        "record": ["v"],
        "undo": ["ctrl+z"],
        "redo": ["ctrl+y"],
        "graph": ["ctrl+g"],
    },
    "graph": {
        "quit": ["ctrl+q"],
        "leave": ["esc"],
        "open": ["enter"],
        "select_up": ["up", "k"],
        "select_down": ["down", "j"],
        "select_left": ["left", "h"],
        "select_right": ["right", "l"],
        "zoom_in": ["+", "="],
        "zoom_out": ["-"],
        "fit": ["0"],
        "pan_left": ["a"],
        "pan_right": ["d"],
        "pan_up": ["w"],
        "pan_down": ["s"],
        "local_depth": ["L"],
        "depth_up": ["]"],
        "depth_down": ["["],
        "missing": ["m"],
        "filter": ["/"],
        "help": ["?", "f1"],
    },
    "gfilter": {
        "cancel": ["esc"],
        "accept": ["enter"],
        "erase": ["backspace"],
    },
    "insert": {
        "normal": ["esc"],
        "up": ["up"],
        "down": ["down"],
        "left": ["left"],
        "right": ["right"],
        "line_start": ["home"],
        "line_end": ["end"],
        "page_up": ["pgup"],
        "page_down": ["pgdn"],
        "save": ["f2", "ctrl+s"],
        "reload": ["f5"],
        "help": ["f1"],
        "undo": ["ctrl+z"],
        "redo": ["ctrl+y"],
        "backspace": ["backspace"],
        "delete": ["delete"],
        "break": ["enter"],
        "indent": ["tab"],
    },
    "confirm": {
        "yes": ["s", "y", "S", "Y"],
        "no": ["d", "n", "D", "N"],
        "stay": ["esc"],
    },
    "search": {
        "cancel": ["esc"],
        "jump": ["enter"],
        "erase": ["backspace"],
    },
    "name": {
        "cancel": ["esc"],
        "accept": ["enter"],
        "erase": ["backspace"],
    },
    "filter": {
        "cancel": ["esc"],
        "open": ["enter"],
        "erase": ["backspace"],
        "up": ["up"],
        "down": ["down"],
    },
    "help": {
        "close": ["esc", "q", "?", "f1"],
        "up": ["up", "k"],
        "down": ["down", "j"],
    },
}


class KeyMap:
    def __init__(self, labels: dict[str, dict[str, list[str]]], codes: dict[str, dict[str, set[int]]]) -> None:
        self.labels = labels
        self.codes = codes

    def pressed(self, mode: str, action: str, key: int | str) -> bool:
        if isinstance(key, str):
            if len(key) != 1:
                return False
            key = ord(key)  # a key list may name a letter like é
        return key in self.codes.get(mode, {}).get(action, ())

    def show(self, mode: str, action: str) -> str:
        names = self.labels.get(mode, {}).get(action) or ["?"]
        return " ".join(names[:3])


def keys_path() -> Path:
    override = os.environ.get("GRIMOIRE_KEYS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".grimoire" / "keys.json"


def compile_keys(data: dict) -> KeyMap:
    labels = {
        mode: {action: list(names) for action, names in actions.items()}
        for mode, actions in DEFAULT_KEYS.items()
    }
    if isinstance(data, dict):
        for mode, actions in data.items():
            if mode not in DEFAULT_KEYS or not isinstance(actions, dict):
                print(f"grimoire: ignoring keys section {mode}", file=sys.stderr)
                continue
            for action, names in actions.items():
                if action not in DEFAULT_KEYS[mode]:
                    print(f"grimoire: ignoring {mode}.{action}", file=sys.stderr)
                    continue
                if isinstance(names, str):
                    names = [names]
                if not isinstance(names, list) or not all(isinstance(item, str) and item.strip() for item in names):
                    print(f"grimoire: ignoring {mode}.{action}", file=sys.stderr)
                    continue
                labels[mode][action] = [item.strip() for item in names]
    codes: dict[str, dict[str, set[int]]] = {}
    for mode, actions in labels.items():
        codes[mode] = {}
        for action, names in actions.items():
            bag: set[int] = set()
            for name in names:
                try:
                    bag |= key_codes(name)
                except ValueError as exc:
                    print(f"grimoire: {mode}.{action}: {exc}", file=sys.stderr)
            if not bag:
                labels[mode][action] = list(DEFAULT_KEYS[mode][action])
                for name in labels[mode][action]:
                    bag |= key_codes(name)
            codes[mode][action] = bag
    return KeyMap(labels, codes)


def load_keys() -> KeyMap:
    path = keys_path()
    data: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"grimoire: ignoring {path} ({exc})", file=sys.stderr)
            loaded = {}
        if isinstance(loaded, dict):
            data = loaded
    return compile_keys(data)


def write_default_keys(path: Path | None = None) -> Path:
    dest = path or keys_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(DEFAULT_KEYS, indent=2) + "\n", encoding="utf-8")
    return dest


class PluginAPI:
    """What a plugin file receives. Core keys win. A handler returns False to quit."""

    def __init__(self, host: "PluginHost", name: str) -> None:
        self.host = host
        self.name = name

    def suffix(self, ext: str) -> None:
        ext = ext if ext.startswith(".") else "." + ext
        TEXT_EXT.add(ext.lower())

    def markdown(self, ext: str) -> None:
        ext = ext if ext.startswith(".") else "." + ext
        ext = ext.lower()
        TEXT_EXT.add(ext)
        MARKDOWN_EXT.add(ext)

    def render(self, ext: str, fn) -> None:
        ext = ext if ext.startswith(".") else "." + ext
        self.host.renderers[ext.lower()] = fn

    def key(self, key: str, fn, *, mode: str = "normal") -> None:
        if mode not in ("normal", "insert", "list"):
            raise ValueError(f"unknown mode {mode}")
        self.host.keys.setdefault(mode, []).append((parse_key(key), fn, self.name))

    def on(self, event: str, fn) -> None:
        if event not in ("open", "save", "leave"):
            raise ValueError(f"unknown event {event}")
        self.host.hooks.setdefault(event, []).append((fn, self.name))


class PluginHost:
    def __init__(self) -> None:
        self.loaded: list[str] = []
        self.errors: list[str] = []
        self.renderers: dict = {}
        self.keys: dict[str, list] = {}
        self.hooks: dict[str, list] = {}

    def renderer_for(self, path: Path):
        return self.renderers.get(path.suffix.lower())

    def dispatch(self, app: "App", mode: str, key: int) -> bool | None:
        for code, fn, name in self.keys.get(mode, []):
            if code != key:
                continue
            try:
                result = fn(app)
            except Exception as exc:
                app.message = f"{name}: {exc}"
                return True
            return False if result is False else True
        return None

    def emit(self, event: str, app: "App") -> None:
        for fn, name in self.hooks.get(event, []):
            try:
                fn(app)
            except Exception as exc:
                app.message = f"{name}: {exc}"


def load_plugins(directory: Path | None = None) -> PluginHost:
    host = PluginHost()
    folder = directory if directory is not None else plugins_dir()
    if not folder.is_dir():
        return host
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        name = path.stem
        try:
            spec = importlib.util.spec_from_file_location(f"grimoire_plugin_{name}", path)
            if spec is None or spec.loader is None:
                raise ImportError("no loader")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            register = getattr(module, "register", None)
            if not callable(register):
                raise TypeError("register(api) is missing")
            register(PluginAPI(host, name))
        except Exception as exc:
            host.errors.append(f"{path.name}: {exc}")
            print(f"grimoire: plugin {path.name} skipped ({exc})", file=sys.stderr)
            continue
        host.loaded.append(name)
    return host


def is_binary(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return True
    return b"\0" in chunk


def is_text_file(path: Path) -> bool:
    if not path.is_file():
        return False
    suffix = path.suffix.lower()
    if suffix in TEXT_EXT:
        return True
    if suffix == "" and path.stat().st_size < 1_000_000:
        return not is_binary(path)
    return False


def read_lines(path: Path) -> tuple[list[str], str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"(unreadable: {exc})"], ""
    lines = text.splitlines()
    note = ""
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        note = f"truncated at {MAX_LINES} lines"
    return lines or [""], note


def line_style(path: Path) -> tuple[str, bool]:
    """The file's own line ending, and whether it ends with one. Saving keeps both."""
    try:
        data = path.read_bytes()
    except OSError:
        return "\n", True
    if not data:
        return "\n", True
    newline = "\r\n" if b"\r\n" in data else "\n"
    return newline, data.endswith(b"\n")


def split_row(raw: str, delimiter: str) -> list[str]:
    try:
        return next(csv.reader([raw], delimiter=delimiter))
    except (csv.Error, StopIteration):
        return [raw]


def cell_offset(raw: str, delimiter: str, index: int) -> int:
    """Character where cell `index` starts in the raw line. Quoted delimiters do not count."""
    if index <= 0:
        return 0
    quoted = False
    seen = 0
    for i, ch in enumerate(raw):
        if ch == '"':
            quoted = not quoted
        elif ch == delimiter and not quoted:
            seen += 1
            if seen == index:
                return i + 1
    return len(raw)


def cell_span(raw: str, delimiter: str, index: int) -> tuple[int, int] | None:
    """Start and end of cell `index` in the raw line, quotes included. None if the row is shorter."""
    start = 0
    seen = 0
    quoted = False
    for i, ch in enumerate(raw):
        if ch == '"':
            quoted = not quoted
        elif ch == delimiter and not quoted:
            if seen == index:
                return start, i
            seen += 1
            start = i + 1
    if seen == index:
        return start, len(raw)
    return None


def quote_cell(value: str, delimiter: str) -> str:
    """Quote only when the value needs it: a delimiter, a quote, or a leading/trailing space."""
    if delimiter in value or '"' in value or value != value.strip():
        return '"' + value.replace('"', '""') + '"'
    return value


def replace_cell(raw: str, delimiter: str, index: int, value: str) -> str:
    """Swap one cell. Every other byte of the line stays as it was."""
    if raw.count('"') % 2:
        raise ValueError("multi-line cell, use i to edit raw")
    span = cell_span(raw, delimiter, index)
    if span is None:
        count = 1
        while cell_span(raw, delimiter, count) is not None:
            count += 1
        return raw + delimiter * (index - count + 1) + quote_cell(value, delimiter)
    start, end = span
    return raw[:start] + quote_cell(value, delimiter) + raw[end:]


def is_number(text: str) -> bool:
    try:
        float(text.replace(",", ""))
    except ValueError:
        return False
    return True


@dataclass
class Table:
    rows: list[list[str]]
    widths: list[int]
    numeric: list[bool]

    @property
    def columns(self) -> int:
        return len(self.widths)

    def cell(self, row: int, col: int) -> str:
        if 0 <= row < len(self.rows) and 0 <= col < len(self.rows[row]):
            return self.rows[row][col]
        return ""

    def header(self, col: int) -> str:
        return self.cell(0, col) or f"column {col + 1}"


def parse_table(lines: list[str], delimiter: str) -> Table:
    rows = [[cell.strip() for cell in split_row(raw, delimiter)] if raw.strip() else [] for raw in lines]
    count = max((len(r) for r in rows), default=0)
    widths = [1] * count
    numeric = [True] * count
    filled = [False] * count
    for index, cells in enumerate(rows):
        for col, text in enumerate(cells):
            widths[col] = max(widths[col], min(len(text), TABLE_CELL_MAX))
            if index > 0 and text:
                filled[col] = True
                if not is_number(text):
                    numeric[col] = False
    numeric = [n and f for n, f in zip(numeric, filled)]
    return Table(rows, widths, numeric)


def fit_cell(text: str, width: int, right: bool) -> str:
    if len(text) > width:
        text = text[: max(0, width - 1)] + "…"
    return text.rjust(width) if right else text.ljust(width)


def table_window(table: Table, col: int, width: int, sep: int = 3) -> list[int]:
    """Columns that fit. Column 0 is frozen; the rest scroll so `col` stays visible."""
    if table.columns == 0:
        return []
    frozen = table.widths[0]
    room = width - frozen - 2  # keep space for the › that says more is to the right
    if col <= 0:
        start = 1
    else:
        start = col
        used = table.widths[col] + sep
        while start > 1 and used + table.widths[start - 1] + sep <= room:
            start -= 1
            used += table.widths[start] + sep
    shown = [0]
    used = 0
    for c in range(start, table.columns):
        need = table.widths[c] + sep
        if used + need > room and len(shown) > 1:
            break
        shown.append(c)
        used += need
    return shown


def table_layout(
    table: Table,
    row: int,
    col: int,
    height: int,
    width: int,
) -> list[tuple[int, int, list[tuple[str, str]]]]:
    """Grid view. The header stays on top, the first column stays on the left."""
    if height < 1 or not table.rows:
        return []
    shown = table_window(table, col, width)

    def line(index: int, style: str) -> list[tuple[str, str]]:
        spans: list[tuple[str, str]] = []
        for n, c in enumerate(shown):
            if n:
                mark = " ‹ " if n == 1 and c > 1 else " │ "
                spans.append((mark, DIM))
            text = fit_cell(table.cell(index, c), table.widths[c], table.numeric[c] and index > 0)
            spans.append((text, SELECTED if index == row and c == col else style))
        if shown and shown[-1] < table.columns - 1:
            spans.append((" ›", DIM))
        return spans

    out = [(0, 0, line(0, HEADING))]
    body = height - 1
    total = len(table.rows) - 1
    if body < 1 or total < 1:
        return out
    cursor = max(1, row)
    top = max(1, min(cursor - body // 2, total - body + 1))
    for y, index in enumerate(range(top, min(top + body, total + 1)), start=1):
        out.append((y, 0, line(index, TEXT)))
    return out


def record_layout(
    table: Table,
    row: int,
    col: int,
    height: int,
    width: int,
) -> list[tuple[int, int, list[tuple[str, str]]]]:
    """One row turned on its side: header on the left, value on the right."""
    if height < 1:
        return []
    record = max(1, row) if len(table.rows) > 1 else 0
    label_w = min(max((len(table.header(c)) for c in range(table.columns)), default=1), TABLE_CELL_MAX)
    out = [(0, 0, [(f"record {record} of {max(0, len(table.rows) - 1)}", DIM)])]
    value_w = max(1, width - label_w - 2)
    lines: list[tuple[int, list[tuple[str, str]]]] = []
    for c in range(table.columns):
        value = table.cell(record, c)
        chunks = [value[i : i + value_w] for i in range(0, len(value), value_w)] or [""]
        label = fit_cell(table.header(c), label_w, False)
        for k, chunk in enumerate(chunks):
            left = label if k == 0 else " " * label_w
            style = SELECTED if c == col else TEXT
            lines.append((c, [(left, HEADING if k == 0 else DIM), ("  ", DIM), (chunk, style)]))
    body = height - 1
    focus = next((i for i, (c, _) in enumerate(lines) if c == col), 0)
    top = max(0, min(focus - body // 2, len(lines) - body))
    for y, (_, spans) in enumerate(lines[top : top + body], start=1):
        out.append((y, 0, spans))
    return out


def pick_renderer(path: Path, lines: list[str], plugins=None):
    """Plugin renderers. Tables are drawn by table_layout, not line by line."""
    return plugins.renderer_for(path) if plugins else None


def table_delimiter(path: Path | None, plugins=None) -> str | None:
    if path is None or (plugins and plugins.renderer_for(path)):
        return None
    return TABLE_EXT.get(path.suffix.lower())


def page_slug(path: Path, library: Path) -> str:
    rel = path.resolve().relative_to(library.resolve()).as_posix()
    return strip_suffix(rel)


def list_pages(library: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    if not library.is_dir():
        return found
    for path in sorted(library.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in MARKDOWN_EXT:
            continue
        parts = path.relative_to(library).parts
        if any(part.startswith(".") for part in parts):
            continue
        try:
            slug = page_slug(path, library)
        except ValueError:
            continue  # a symlink that leaves the library is not a page
        found[slug.lower()] = path
    return found


def links_in(text: str) -> list[str]:
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    found: list[str] = []
    for match in re.finditer(r"\[\[([^\]|#]+)", text):
        found.append(match.group(1).strip())
    text = re.sub(r"(?<!\\)!\[[^\]]*\]\([^)]*\)", "", text)  # images are not pages; [![b](b.png)](page.md) keeps its link
    for match in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", text):
        target = match.group(1).strip().split("#", 1)[0]
        if target.startswith(REMOTE):
            continue
        target = strip_suffix(target).removeprefix("./").strip()
        if target:
            found.append(target)
    return found


_WIKI_LINK = re.compile(r"\[\[([^\]|#]*)(?:#([^\]|]*))?(?:\|[^\]]*)?\]\]")
_MD_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")


def links_on_line(raw: str) -> list[tuple[int, int, str, str]]:
    """Page links on one line, left to right: (start, end, page, heading).

    Code spans are skipped, and so are images and web addresses.
    """
    masked = re.sub(r"`[^`]*`", lambda m: " " * len(m.group(0)), raw)
    found: list[tuple[int, int, str, str]] = []
    for match in _WIKI_LINK.finditer(masked):
        page = match.group(1).strip()
        heading = (match.group(2) or "").strip()
        if page or heading:
            found.append((match.start(), match.end(), page, heading))
    for match in _MD_LINK.finditer(masked):
        dest = match.group(1).strip()
        if dest.startswith(REMOTE) or Path(dest).suffix.lower() in IMAGE_EXT:
            continue
        page, _, heading = dest.partition("#")
        page = strip_suffix(page.strip()).removeprefix("./").strip()
        if page or heading:
            found.append((match.start(), match.end(), page, heading.strip()))
    return sorted(found)


def link_at(raw: str, col: int) -> tuple[str, str] | None:
    """The link under the cursor, or the next one to its right, or the last one on the line."""
    links = links_on_line(raw)
    if not links:
        return None
    for start, end, page, heading in links:
        if col < end:
            return page, heading
    return links[-1][2], links[-1][3]


def link_target(note: Path, page: str, roots: list[Path]) -> Path | None:
    """The file a link names: a page of the library first, then a file beside the note."""
    key = page.lower().replace("\\", "/").strip("/")
    for root in roots:
        hit = list_pages(root).get(key)
        if hit is not None:
            return hit
    for suffix in ("", *MD_SUFFIXES):
        beside = note.parent / (page + suffix)
        if is_text_file(beside):
            return beside.resolve()
    return None


def heading_row(lines: list[str], heading: str) -> int | None:
    want = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
    for index, line in enumerate(lines):
        if line.startswith("#") and re.sub(r"[^a-z0-9]+", "-", line.lstrip("#").lower()).strip("-") == want:
            return index
    return None


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def page_title(path: Path, slug: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("#"):
                return line.lstrip("#").strip() or slug
    except OSError:
        pass
    return slug


def build_graph(library: Path) -> dict:
    pages = list_pages(library)
    nodes: dict[str, dict] = {}
    for key, path in pages.items():
        slug = page_slug(path, library)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        resolved: list[str] = []
        missing: list[str] = []
        for link in links_in(text):
            hit = pages.get(link.lower().replace("\\", "/"))
            if hit is None:
                missing.append(link)
            else:
                resolved.append(page_slug(hit, library))
        nodes[slug] = {
            "path": str(path),
            "title": page_title(path, slug),
            "links": _unique(resolved),
            "missing": _unique(missing),
            "backlinks": [],
        }
    for slug, node in nodes.items():
        for dest in node["links"]:
            if dest in nodes:
                nodes[dest]["backlinks"].append(slug)
    for node in nodes.values():
        node["backlinks"] = _unique(node["backlinks"])
    return {"library": str(library.resolve()), "pages": nodes}


def graph_path(library: Path) -> Path:
    return library / ".graph.json"


def write_graph(library: Path) -> Path:
    path = graph_path(library)
    atomic_write(path, (json.dumps(build_graph(library), indent=2) + "\n").encode("utf-8"))
    return path


def refresh_graph_for(path: Path) -> None:
    library = library_path(load_config())
    try:
        path.resolve().relative_to(library.resolve())
    except ValueError:
        return
    write_graph(library)


def backlinks_for(path: Path) -> list[str]:
    library = library_path(load_config())
    try:
        path.resolve().relative_to(library.resolve())
    except ValueError:
        return []
    slug = page_slug(path, library)
    return build_graph(library)["pages"].get(slug, {}).get("backlinks", [])


def file_detail(path: Path) -> str:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return "unreadable"
    lines, _ = read_lines(path)
    if path.suffix.lower() in {".md", ".markdown"}:
        words = len("\n".join(lines).split())
        unit = "word" if words == 1 else "words"
        return f"{mtime} · {words} {unit}"
    n = len(lines) if lines != [""] else 0
    unit = "line" if n == 1 else "lines"
    return f"{mtime} · {n} {unit}"


def folder_detail(path: Path) -> str:
    n = 0
    try:
        children = list(path.iterdir())
    except OSError:
        return "unreadable"
    for child in children:
        if child.name.startswith(".") or child.name in SKIP_DIRS:
            continue
        if child.is_dir() or is_text_file(child):
            n += 1
    if n == 0:
        return "empty"
    if n == 1:
        return "1 item"
    return f"{n} items"


def scan(path: Path) -> list[Entry]:
    folders: list[Entry] = []
    files: list[Entry] = []
    try:
        children = list(path.iterdir())
    except OSError:
        return []
    for child in children:
        if child.name.startswith(".") or child.name in SKIP_DIRS:
            continue
        if child.is_dir():
            folders.append(Entry(child.name, folder_detail(child), "folder", child))
        elif is_text_file(child):
            files.append(Entry(child.name, file_detail(child), "file", child))
    folders.sort(key=lambda e: e.name.lower())
    files.sort(key=lambda e: e.path.stat().st_mtime, reverse=True)
    return folders + files


def file_matches(folder: Path, needle: str) -> list[Entry]:
    """Files and folders under this shelf whose names contain the text."""
    needle = needle.lower()
    if not needle:
        return scan(folder)
    found: list[Entry] = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [name for name in dirnames if not name.startswith(".") and name not in SKIP_DIRS]
        base = Path(dirpath)
        for name in dirnames:
            if needle not in name.lower():
                continue
            path = base / name
            found.append(Entry(path.relative_to(folder).as_posix(), folder_detail(path), "folder", path))
        for name in filenames:
            path = base / name
            if name.startswith(".") or needle not in name.lower() or not is_text_file(path):
                continue
            found.append(Entry(path.relative_to(folder).as_posix(), file_detail(path), "file", path))
    found.sort(key=lambda entry: entry.name.lower())
    return found


def shelf_stamp(path: Path) -> tuple[str, ...]:
    try:
        children = list(path.iterdir())
    except OSError:
        return ()
    names = [child.name for child in children if not child.name.startswith(".") and child.name not in SKIP_DIRS]
    return tuple(sorted(names))


def strip_suffix(text: str, suffixes: tuple[str, ...] = MD_SUFFIXES) -> str:
    lowered = text.lower()
    for suffix in suffixes:
        if lowered.endswith(suffix):
            return text[: -len(suffix)]
    return text


def clean_name(raw: str, *, page: bool) -> str:
    text = raw.strip()
    if page:
        slug = re.sub(r"[^a-z0-9]+", "-", strip_suffix(text).lower()).strip("-")
        if not slug:
            raise ValueError("name needs a letter or number")
        return slug + ".md"
    if not text or text in {".", ".."} or text.startswith(".") or "/" in text or "\\" in text:
        raise ValueError("folder name refused")
    if len(Path(text).parts) != 1:
        raise ValueError("folder name refused")
    return text


def new_child(folder: Path, name: str) -> Path:
    path = folder / name
    if path.exists():
        raise FileExistsError(name)
    return path


def create_page(folder: Path, raw: str) -> Path:
    path = new_child(folder, clean_name(raw, page=True))
    title = strip_suffix(raw.strip()) or path.stem
    path.write_text(f"# {title}\n", encoding="utf-8", newline="\n")
    return path


def create_folder(folder: Path, raw: str) -> Path:
    path = new_child(folder, clean_name(raw, page=False))
    path.mkdir()
    return path


def is_fence(line: str) -> bool:
    return line.lstrip().startswith("```") or line.lstrip().startswith("~~~")


def fence_flags(lines: list[str]) -> list[bool]:
    """True when a line is inside a fence, or is a fence marker itself."""
    inside = False
    flags: list[bool] = []
    for line in lines:
        if is_fence(line):
            flags.append(True)
            inside = not inside
        else:
            flags.append(inside)
    return flags


def parse_inline(text: str) -> list[tuple[str, str]]:
    """Split one line into (text, style). Markers that close are dropped.

    Observed in the recording: **bold** draws blue with the asterisks gone,
    and _italic_ draws green with the underscores gone. Inline code was not
    shown. It is kept, markers dropped, so a script command is readable.
    """
    out: list[tuple[str, str]] = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("**", i):
            j = text.find("**", i + 2)
            if j != -1:
                out.append((text[i + 2:j], BOLD))
                i = j + 2
                continue
        if text.startswith("[[", i):
            end = text.find("]]", i + 2)
            if end != -1:
                inner = text[i + 2:end]
                shown = inner.split("|", 1)[-1].split("#", 1)[0].strip() or inner
                out.append((shown, LINK))
                i = end + 2
                continue
        if text[i] in "_*":
            marker = text[i]
            j = text.find(marker, i + 1)
            if j > i + 1 and text[j - 1] != " ":
                out.append((text[i + 1:j], ITALIC))
                i = j + 1
                continue
        if text[i] == "`":
            j = text.find("`", i + 1)
            if j != -1:
                out.append((text[i + 1:j], CODE))
                i = j + 1
                continue
        nxt = i + 1
        while nxt < n and text[nxt] not in "_*`[":
            nxt += 1
        out.append((text[i:nxt], TEXT))
        i = nxt
    return [(t, s) for t, s in out if t]


def render_unfocused(
    raw: str,
    *,
    markdown: bool,
    in_fence: bool,
    renderer=None,
) -> list[tuple[str, str]]:
    if renderer is not None and not in_fence:
        custom = renderer(raw)
        if custom is not None:
            return custom
    if not markdown or in_fence:
        style = CODE if in_fence else TEXT
        return [(raw, style)] if raw else []
    if raw.startswith(">"):
        body = raw[1:].lstrip()
        return [("│ ", DIM), *parse_inline(body)]
    if raw.startswith("- [ ] ") or raw.startswith("- [x] ") or raw.startswith("- [X] "):
        mark = raw[2:5]
        return [(f"{mark} ", DIM), *parse_inline(raw[6:])]
    if raw.startswith("- ") or raw.startswith("* "):
        return [("- ", DIM), *parse_inline(raw[2:])]
    hashes = 0
    while hashes < len(raw) and raw[hashes] == "#":
        hashes += 1
    if 1 <= hashes <= 6 and (hashes == len(raw) or raw[hashes] in " \t"):
        title = raw[hashes:].strip()
        return [(title, HEADING)] if title else []
    return parse_inline(raw)


def fit(spans: list[tuple[str, str]], width: int) -> list[tuple[str, str]]:
    if width <= 0:
        return []
    out: list[tuple[str, str]] = []
    used = 0
    for text, style in spans:
        room = width - used
        if room <= 0:
            break
        if len(text) <= room:
            out.append((text, style))
            used += len(text)
            continue
        cut = text[: room - 1] + "…" if room > 1 else "…"
        out.append((cut, style))
        break
    return out


def slice_focus(raw: str, col: int, width: int) -> tuple[str, int]:
    """Window of the raw line that keeps the cursor cell on screen."""
    if width < 1:
        return "", 0
    col = max(0, min(col, len(raw)))
    start = 0
    if col >= width:
        start = col - width + 1
    visible = raw[start:start + width]
    cursor = col - start
    if cursor >= width:
        start += 1
        visible = raw[start:start + width]
        cursor = col - start
    return visible, cursor


def focus_spans(visible: str, cursor: int) -> list[tuple[str, str]]:
    cursor = max(0, min(cursor, len(visible)))
    spans: list[tuple[str, str]] = []
    if cursor:
        spans.append((visible[:cursor], FOCUS))
    if cursor < len(visible):
        spans.append((visible[cursor], CURSOR))
        if cursor + 1 < len(visible):
            spans.append((visible[cursor + 1:], FOCUS))
    else:
        spans.append((" ", CURSOR))
    return spans


def focus_anchor(index: int, count: int, height: int, stride: int = 1) -> int:
    """Screen row of the focused item.

    A short document, and the top of a long one, pin to the top.
    Past the midpoint the row stays put and the text scrolls.
    Near the end the last item sits on the last row, so the view
    never opens on a blank gap.
    """
    if height < 1 or count < 1:
        return 0
    index = max(0, min(index, count - 1))
    span = (count - 1) * stride + 1
    if span <= height:
        return index * stride
    center = height // 2
    anchor = min(index * stride, center)
    last = anchor + (count - 1 - index) * stride
    if last >= height:
        return anchor
    pulled = height - 1 - (count - 1 - index) * stride
    return max(0, min(index * stride, pulled))


def wrap_ranges(text: str, width: int) -> list[tuple[int, int]]:
    """Source spans that cover text. A break prefers the last space in the window."""
    if width < 1:
        return [(0, 0)]
    if not text:
        return [(0, 0)]
    ranges: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        end = min(i + width, n)
        if end < n:
            cut = text.rfind(" ", i, end)
            if cut > i:
                end = cut + 1
        ranges.append((i, end))
        i = end
    return ranges


def slice_spans(spans: list[tuple[str, str]], start: int, end: int) -> list[tuple[str, str]]:
    if start >= end:
        return []
    out: list[tuple[str, str]] = []
    pos = 0
    for text, style in spans:
        nxt = pos + len(text)
        if nxt <= start:
            pos = nxt
            continue
        if pos >= end:
            break
        chunk = text[max(0, start - pos): min(len(text), end - pos)]
        if chunk:
            out.append((chunk, style))
        pos = nxt
    return out


def wrap_spans(spans: list[tuple[str, str]], width: int) -> list[list[tuple[str, str]]]:
    text = "".join(part for part, _ in spans)
    rows: list[list[tuple[str, str]]] = []
    for start, end in wrap_ranges(text, width):
        piece = slice_spans(spans, start, end)
        if piece and piece[-1][0].endswith(" "):
            last, style = piece[-1]
            last = last[:-1]
            if last:
                piece[-1] = (last, style)
            else:
                piece.pop()
        rows.append(piece)
    return rows or [[]]


def focus_piece(raw: str, start: int, end: int, col: int) -> list[tuple[str, str]]:
    piece = raw[start:end]
    owns = start <= col < end or (col >= len(raw) and end == len(raw))
    if owns:
        return focus_spans(piece, min(max(0, col - start), len(piece)))
    if piece.endswith(" "):
        piece = piece[:-1]
    return [(piece, FOCUS)] if piece else []


IMAGE_LINE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_PREVIEW_CACHE: dict[tuple, list[str]] = {}


def image_mark(raw: str) -> tuple[str, str] | None:
    match = IMAGE_LINE.match(raw.strip())
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip()


def image_file(note: Path, dest: str) -> Path | None:
    """Local image beside the note. None means a remote address, not a file."""
    dest = dest.strip()
    if not dest or dest.startswith(REMOTE):
        return None
    rel = Path(dest)
    if rel.is_absolute():
        return rel.expanduser().resolve()
    if ".." in rel.parts:
        raise ValueError(dest)
    path = (note.parent / rel).resolve()
    path.relative_to(note.parent.resolve())
    return path


def image_caption(alt: str, dest: str, *, missing: bool = False) -> str:
    label = f"{alt}  {dest}" if alt else dest
    return label + ("  (missing)" if missing else "")


STARTFILE = "startfile"  # Windows opens a file with whatever is registered for it


def image_tools(which=shutil.which, platform: str | None = None) -> dict[str, str | None]:
    platform = platform or sys.platform
    if platform == "win32":
        opener = STARTFILE
    elif platform == "darwin":
        opener = which("imv") or which("open")
    else:
        opener = which("imv") or which("xdg-open")
    return {"preview": which("chafa"), "open": opener}


def image_status(tools: dict[str, str | None] | None = None) -> str:
    found = tools or image_tools()
    preview = "chafa" if found["preview"] else "not installed (chafa)"
    opener = Path(found["open"]).name if found["open"] else "not installed (imv, xdg-open, or open)"
    return f"preview  {preview}\nopen     {opener}"


def chafa_preview(path: Path, width: int, rows: int = 8) -> list[str]:
    if width < 8 or not path.is_file() or path.suffix.lower() not in IMAGE_EXT:
        return []
    tool = shutil.which("chafa")
    if not tool:
        return []
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return []
    key = (str(path), stamp, width, rows)
    cached = _PREVIEW_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            [tool, "--format", "symbols", "-c", "none", f"--size={width}x{rows}", "--animate", "off", str(path)],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    lines = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        clean = _ANSI.sub("", line).rstrip()
        if clean:
            lines.append(clean[:width])
    _PREVIEW_CACHE[key] = lines
    return lines


def open_image(note: Path, raw: str, tools: dict[str, str | None] | None = None, spawn=None) -> str:
    mark = image_mark(raw)
    if mark is None:
        return ""
    _alt, dest = mark
    try:
        target = image_file(note, dest)
    except ValueError:
        return "image path refused"
    if target is None:
        return "not a local image"
    if target.suffix.lower() not in IMAGE_EXT:
        return "not an image"  # the opener would run whatever handles the file
    if not target.is_file():
        return "image missing"
    found = tools or image_tools()
    opener = found.get("open")
    if not opener:
        return "install imv to view images"
    if opener == STARTFILE and spawn is None:
        os.startfile(str(target))
        return f"opened {target.name}"
    launch = spawn or subprocess.Popen
    launch([opener, str(target)], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"opened {target.name}"


def doc_layout(
    lines: list[str],
    row: int,
    col: int,
    height: int,
    width: int,
    *,
    markdown: bool,
    fences: list[bool] | None = None,
    margin: int = 2,
    renderer=None,
    note: Path | None = None,
    preview=None,
) -> list[tuple[int, int, list[tuple[str, str]]]]:
    """Screen rows. Wrapped lines take more than one row. The cursor row starts at the top, then stays centered."""
    if height < 1 or not lines:
        return []
    row = max(0, min(row, len(lines) - 1))
    flags = fences if fences is not None else fence_flags(lines)
    inner = max(1, width - margin)
    visuals: list[list[tuple[str, str]]] = []
    cursor_at = 0
    for index, raw in enumerate(lines):
        if index == row:
            pieces = wrap_ranges(raw, inner)
            cursor_at = len(visuals)
            for start, end in pieces:
                if start <= col < end or (col >= len(raw) and end == len(raw)):
                    cursor_at = len(visuals)
                visuals.append(focus_piece(raw, start, end, col))
            continue
        mark = image_mark(raw) if markdown and not (flags[index] if index < len(flags) else False) else None
        if mark is not None:
            alt, dest = mark
            missing = False
            rows: list[str] = []
            if note is not None:
                try:
                    target = image_file(note, dest)
                except ValueError:
                    target = None
                    missing = True
                if target is None:
                    missing = not dest.startswith(REMOTE)
                elif not target.is_file():
                    missing = True
                elif not Path(dest).is_absolute():
                    # A page only previews pictures beside it. An absolute path
                    # shows as a caption and opens on a keypress.
                    paint = preview if preview is not None else chafa_preview
                    rows = paint(target, inner) or []
            for line in rows:
                visuals.append([(line, DIM)])
            visuals.append([(image_caption(alt, dest, missing=missing), DIM)])
            continue
        rendered = render_unfocused(
            raw,
            markdown=markdown,
            in_fence=flags[index] if index < len(flags) else False,
            renderer=renderer,
        )
        visuals.extend(wrap_spans(rendered, inner))
    if not visuals:
        return []
    cursor_at = max(0, min(cursor_at, len(visuals) - 1))
    focus_y = focus_anchor(cursor_at, len(visuals), height)
    first = cursor_at - focus_y
    painted: list[tuple[int, int, list[tuple[str, str]]]] = []
    for screen_y in range(height):
        visual = first + screen_y
        if visual < 0 or visual >= len(visuals) or not visuals[visual]:
            continue
        painted.append((screen_y, margin, visuals[visual]))
    return painted


def list_layout(
    entries: list[Entry],
    selected: int,
    height: int,
    width: int,
) -> list[tuple[int, int, list[tuple[str, str]]]]:
    """Selected name starts at the top. It centers only once the list is long."""
    if height < 1 or width < 4 or not entries:
        return []
    selected = max(0, min(selected, len(entries) - 1))
    card = min(CARD_W, width - 4)
    left = max(0, (width - card) // 2)
    stride = 3
    anchor = focus_anchor(selected, len(entries), height, stride)
    painted: list[tuple[int, int, list[tuple[str, str]]]] = []
    for i, entry in enumerate(entries):
        name_y = anchor + (i - selected) * stride
        style = SELECTED if i == selected else (FOLDER if entry.kind == "folder" else DIM)
        name = entry.name + "/" if entry.kind == "folder" else entry.name
        if 0 <= name_y < height:
            painted.append((name_y, left, fit([(name, style)], card)))
        if 0 <= name_y + 1 < height:
            painted.append((name_y + 1, left, fit([(entry.detail, DIM)], card)))
        if 0 <= name_y + 2 < height and i != len(entries) - 1:
            painted.append((name_y + 2, left, [("─" * card, DIM)]))
    return painted


def find_line(lines: list[str], query: str, start: int, direction: int = 1) -> int | None:
    if not query or not lines:
        return None
    needle = query.lower()
    count = len(lines)
    for step in range(1, count + 1):
        index = (start + direction * step) % count
        if needle in lines[index].lower():
            return index
    return None


def help_lines(name: str, keys: KeyMap | None = None) -> tuple[str, ...]:
    bound = keys or load_keys()
    return (
        name,
        "the book that will not stay shut",
        "",
        "List",
        f"  {bound.show('list', 'up')}  {bound.show('list', 'down')}   move",
        f"  {bound.show('list', 'open')}   open",
        f"  {bound.show('list', 'new_page')}   new page    {bound.show('list', 'new_folder')}   new folder",
        f"  {bound.show('list', 'back')}   back, or quit at the top",
        f"  {bound.show('list', 'quit')}   quit",
        f"  {bound.show('list', 'find')}   find a file",
        f"  {bound.show('list', 'help')}   this help",
        "",
        "Reader, normal",
        f"  {bound.show('normal', 'left')} {bound.show('normal', 'down')} {bound.show('normal', 'up')} {bound.show('normal', 'right')}   move",
        f"  {bound.show('normal', 'insert')}   insert at the cursor",
        f"  {bound.show('normal', 'view')}   follow the link on this line, or view its image",
        f"  {bound.show('normal', 'append')}   append, then type",
        f"  {bound.show('normal', 'record')}   .csv/.tsv: grid or one record at a time",
        f"  {bound.show('normal', 'view')}   .csv/.tsv: edit the cell; {bound.show('normal', 'insert')} edits the raw line",
        f"  {bound.show('normal', 'leave')} {bound.show('normal', 'quit')}   back, asks if unsaved",
        "",
        "Reader, insert",
        "  type            edit the raw line",
        f"  {bound.show('insert', 'break')}   new line",
        f"  {bound.show('insert', 'backspace')}   delete",
        f"  {bound.show('insert', 'normal')}   back to normal, not out",
        "",
        "Either mode",
        f"  {bound.show('normal', 'save')}   save",
        f"  {bound.show('normal', 'undo')}   undo    {bound.show('normal', 'redo')} redo",
        f"  {bound.show('normal', 'find')}   find text, then {bound.show('normal', 'find_next')} and {bound.show('normal', 'find_prev')}",
        f"  {bound.show('normal', 'reload')}   reload from disk",
        "  ctrl+c          quit, always",
        f"  {bound.show('normal', 'help')}   help",
        "",
        "Graph",
        f"  {bound.show('list', 'graph')} (shelf) or {bound.show('normal', 'graph')} (page)   the graph",
        f"  {bound.show('graph', 'select_left')} {bound.show('graph', 'select_right')} {bound.show('graph', 'select_up')} {bound.show('graph', 'select_down')}   move the selection",
        f"  {bound.show('graph', 'open')}   open the selected page",
        f"  {bound.show('graph', 'leave')}   back; from the page it returns here",
        f"  {bound.show('graph', 'zoom_in')} {bound.show('graph', 'zoom_out')} zoom   {bound.show('graph', 'fit')} re-fit",
        f"  {bound.show('graph', 'pan_left')} {bound.show('graph', 'pan_down')} {bound.show('graph', 'pan_up')} {bound.show('graph', 'pan_right')}   pan",
        f"  {bound.show('graph', 'local_depth')}   local graph   {bound.show('graph', 'depth_down')} {bound.show('graph', 'depth_up')} depth 1..5",
        f"  {bound.show('graph', 'missing')}   show or hide missing pages",
        f"  {bound.show('graph', 'filter')}   keep pages, {bound.show('graph', 'leave')} clears it",
        "  mouse   click to select, again to open, wheel zooms",
        f"  ctrl+c  quit    {bound.show('graph', 'quit')} quits",
        "",
        "Keys live in ~/.grimoire/keys.json. grimoire keys writes the file.",
        "The line you are on is the source.",
        "Other lines of a .md file hide #  **  and _ markers.",
        "",
        "grimoire            the pages in ~/.grimoire/pages",
        "grimoire -c         the folder you are in",
        "plugins            ~/.grimoire/plugins",
        "",
        "Settings",
        f"  library  {load_config().get('library', '')}",
        f"  keys     {keys_path()}",
        f"  theme    {theme_path()}",
        *[f"  {line}" for line in image_status().splitlines()],
        "  edit the file, then restart",
        "",
        "esc closes this menu",
    )


class App:
    def __init__(self, root: Path, open_file: Path | None = None, name: str = "GRIMOIRE") -> None:
        self.name = name
        self.keys = load_keys()
        self.root = root.resolve()
        self.cwd = self.root
        self.entries: list[Entry] = []
        self.selected = 0
        self.mode = "list"
        self.help_from = "list"
        self.help_at = 0
        self.lines = [""]
        self.path: Path | None = None
        self.markdown = False
        self.fences: list[bool] = [False]
        self.row = 0
        self.col = 0
        self.pref_col = 0
        self.query = ""
        self.note = ""
        self.truncated = False
        self.message = ""
        self.dirty = False
        self.inserting = False
        self.plugins: PluginHost | None = None
        self.renderer = None
        self.delimiter: str | None = None
        self.tcol = 0
        self.record = False
        self.cell_text = ""
        self._table: tuple[tuple[str, ...], Table] | None = None
        self.backlinks: list[str] = []
        self.undo: list[tuple[list[str], int, int]] = []
        self.redo: list[tuple[list[str], int, int]] = []
        self._coalesce: str | None = None
        self.naming = "page"
        self.shelf_stamp: tuple[str, ...] = ()
        self.gdata: dict | None = None
        self.gstate: dict | None = None
        self.gsel = ""
        self.gview: dict | None = None
        self.glocal: int | None = None
        self.gmissing = True
        self.gfilter = ""
        self.gquery = ""
        self.graph_from = "list"
        self.graph_return = False
        self.trail: list[tuple[Path, int, int]] = []  # pages a followed link was opened from
        self.glast: tuple[int, int] = (80, 24)
        self.gshown: dict | None = None
        if open_file is not None:
            self.open_path(open_file)
        else:
            self.refresh()

    def listing(self) -> list[Entry]:
        if self.mode == "filter" and self.query:
            return file_matches(self.cwd, self.query)
        return self.entries

    def take_query(self, key: int | str, mode: str) -> bool:
        if self.keys.pressed(mode, "erase", key):
            self.query = self.query[:-1]
            return True
        if typed(key):
            self.query += typed(key)
            return True
        return False

    def refresh(self, keep: Path | None = None) -> None:
        if keep is None and self.entries and 0 <= self.selected < len(self.entries):
            keep = self.entries[self.selected].path
        self.entries = scan(self.cwd)
        self.shelf_stamp = shelf_stamp(self.cwd)
        self.selected = 0
        if keep is None:
            return
        for index, entry in enumerate(self.entries):
            if entry.path == keep:
                self.selected = index
                return

    def poll_shelf(self) -> bool:
        if self.mode != "list":
            return False
        stamp = shelf_stamp(self.cwd)
        if stamp == self.shelf_stamp:
            return False
        self.refresh()
        return True

    def open_path(self, path: Path) -> None:
        self.lines, self.note = read_lines(path)
        self.truncated = bool(self.note)
        self.newline, self.final_newline = line_style(path)
        self.fences = fence_flags(self.lines)
        self.path = path
        self.markdown = path.suffix.lower() in MARKDOWN_EXT
        self.renderer = pick_renderer(path, self.lines, self.plugins)
        self.delimiter = table_delimiter(path, self.plugins)
        self.tcol = 0
        self.record = False
        self._table = None
        self.row = 1 if self.delimiter and len(self.lines) > 1 else 0
        self.col = 0
        self.pref_col = 0
        self.mode = "read"
        self.inserting = False
        self.dirty = False
        self.undo.clear()
        self.redo.clear()
        self._coalesce = None
        self.message = self.note
        self.backlinks = backlinks_for(path)
        if self.plugins:
            self.plugins.emit("open", self)

    def clamp(self) -> None:
        if not self.lines:
            self.lines = [""]
            self.fences = [False]
        self.row = max(0, min(self.row, len(self.lines) - 1))
        self.col = max(0, min(self.col, len(self.lines[self.row])))

    def table(self) -> Table | None:
        if not self.delimiter:
            return None
        key = tuple(self.lines)
        if self._table is None or self._table[0] != key:
            self._table = (key, parse_table(self.lines, self.delimiter))
        return self._table[1]

    def table_view(self) -> bool:
        return self.delimiter is not None and not self.inserting and self.mode != "search"

    def begin_cell_edit(self) -> None:
        table = self.table()
        if table is None:
            return
        if self.truncated:
            self.message = READ_ONLY
            return
        if self.lines[self.row].count('"') % 2:
            self.message = "multi-line cell, use i to edit raw"
            return
        self.cell_text = table.cell(self.row, self.tcol)
        self.mode = "cell"
        self.message = ""

    def commit_cell(self) -> None:
        self.mode = "read"
        table = self.table()
        if table is None or self.cell_text == table.cell(self.row, self.tcol):
            return
        try:
            new = replace_cell(self.lines[self.row], self.delimiter or ",", self.tcol, self.cell_text)
        except ValueError as exc:
            self.message = str(exc)
            return
        self._coalesce = None
        self.checkpoint("cell")
        self._coalesce = None
        self.lines[self.row] = new
        self.col = cell_offset(new, self.delimiter or ",", self.tcol)
        self.touch()

    def handle_cell(self, key: int | str) -> bool:
        if self.keys.pressed("search", "cancel", key):
            self.mode = "read"
            return True
        if self.keys.pressed("search", "jump", key):
            self.commit_cell()
            return True
        if self.keys.pressed("search", "erase", key):
            self.cell_text = self.cell_text[:-1]
        elif typed(key):
            self.cell_text += typed(key)
        return True

    def move_cell(self, drow: int, dcol: int) -> None:
        table = self.table()
        if table is None:
            return
        if self.record:
            drow, dcol = dcol, drow
        self.tcol = max(0, min(table.columns - 1, self.tcol + dcol))
        self.row = max(0, min(len(self.lines) - 1, self.row + drow))
        self.col = cell_offset(self.lines[self.row], self.delimiter or ",", self.tcol)

    def move_row(self, delta: int) -> None:
        self.row = max(0, min(len(self.lines) - 1, self.row + delta))
        self.col = min(self.pref_col, len(self.lines[self.row]))

    def move_col(self, delta: int) -> None:
        self.col = max(0, min(len(self.lines[self.row]), self.col + delta))
        self.pref_col = self.col
        self._coalesce = None

    def _snapshot(self) -> tuple[list[str], int, int]:
        return (self.lines.copy(), self.row, self.col)

    def checkpoint(self, kind: str) -> None:
        if self._coalesce == kind and kind in ("insert", "erase"):
            return
        self.undo.append(self._snapshot())
        if len(self.undo) > 80:
            self.undo.pop(0)
        self.redo.clear()
        self._coalesce = kind

    def touch(self) -> None:
        self.dirty = True
        self.fences = fence_flags(self.lines)
        self.message = ""

    def insert_text(self, text: str) -> None:
        if not text:
            return
        self.checkpoint("insert")
        line = self.lines[self.row]
        self.lines[self.row] = line[: self.col] + text + line[self.col :]
        self.col += len(text)
        self.pref_col = self.col
        self.touch()

    def break_line(self) -> None:
        self.checkpoint("break")
        line = self.lines[self.row]
        self.lines[self.row] = line[: self.col]
        self.lines.insert(self.row + 1, line[self.col :])
        self.row += 1
        self.col = 0
        self.pref_col = 0
        self._coalesce = "break"
        self.touch()

    def erase_back(self) -> None:
        if self.col == 0 and self.row == 0:
            return
        self.checkpoint("erase")
        if self.col > 0:
            line = self.lines[self.row]
            self.lines[self.row] = line[: self.col - 1] + line[self.col :]
            self.col -= 1
            self.pref_col = self.col
        else:
            prev = self.lines[self.row - 1]
            self.col = len(prev)
            self.pref_col = self.col
            self.lines[self.row - 1] = prev + self.lines[self.row]
            del self.lines[self.row]
            self.row -= 1
        self.touch()

    def erase_forward(self) -> None:
        line = self.lines[self.row]
        if self.col < len(line):
            self.col += 1
            self.erase_back()
            return
        if self.row < len(self.lines) - 1:
            self.row += 1
            self.col = 0
            self.erase_back()

    def undo_edit(self) -> None:
        if not self.undo:
            self.message = "nothing to undo"
            return
        self.redo.append(self._snapshot())
        self.lines, self.row, self.col = self.undo.pop()
        self.pref_col = self.col
        self._coalesce = None
        self.touch()
        self.message = "undone"

    def redo_edit(self) -> None:
        if not self.redo:
            self.message = "nothing to redo"
            return
        self.undo.append(self._snapshot())
        self.lines, self.row, self.col = self.redo.pop()
        self.pref_col = self.col
        self._coalesce = None
        self.touch()
        self.message = "redone"

    def save(self) -> bool:
        if self.path is None:
            self.message = "no file"
            return False
        if self.truncated:
            self.message = READ_ONLY  # saving would drop every line past the cut
            return False
        newline = getattr(self, "newline", "\n")
        text = newline.join(self.lines)
        if text and getattr(self, "final_newline", True) and not text.endswith(newline):
            text += newline
        try:
            atomic_write(self.path, text.encode("utf-8"))
        except OSError as exc:
            self.message = f"save failed: {exc}"
            return False
        self.dirty = False
        self.message = "saved"
        refresh_graph_for(self.path)
        self.backlinks = backlinks_for(self.path)
        if self.plugins:
            self.plugins.emit("save", self)
        return True

    def reload(self) -> None:
        if self.path is None:
            return
        if self.dirty:
            self.mode = "confirm"
            self.message = "reload"
            return
        row = self.row
        self.open_path(self.path)
        self.row = min(row, len(self.lines) - 1)
        self.message = "reloaded"

    def follow_link(self) -> bool:
        """Open the page the link on this line names. step_back returns here."""
        if self.path is None:
            return False
        link = link_at(self.lines[self.row], self.col)
        if link is None:
            return False
        page, heading = link
        target = self.path
        if page:
            roots = [self.root]
            try:
                roots.insert(0, library_path(load_config()))
            except OSError:
                pass
            found = link_target(self.path, page, roots)
            if found is None:
                self.message = f"no page {page}"
                return True
            target = found
        if target != self.path:
            if self.dirty:
                self.message = "save first, then follow the link"
                return True
            self.trail.append((self.path, self.row, self.col))
            self.open_path(target)
        if heading:
            row = heading_row(self.lines, heading)
            if row is None:
                self.message = f"no heading {heading}"
            else:
                self.row, self.col, self.pref_col = row, 0, 0
        return True

    def step_back(self) -> bool:
        """One step out of a page: the page a link came from, then the graph, then the shelf."""
        while self.trail:
            path, row, col = self.trail.pop()
            if not path.is_file():
                continue
            self.dirty = False
            self.open_path(path)
            self.row = min(row, len(self.lines) - 1)
            self.col = min(col, len(self.lines[self.row]))
            self.pref_col = self.col
            return True
        if self.graph_return:
            return self.back_to_graph()
        return self.leave_reader()

    def leave_reader(self) -> bool:
        if self.plugins:
            self.plugins.emit("leave", self)
        self.trail.clear()
        self.mode = "list"
        self.dirty = False
        self.message = ""
        self.refresh(keep=self.path)
        return True

    def jump_match(self, direction: int) -> None:
        found = find_line(self.lines, self.query, self.row, direction)
        if found is None:
            self.message = f"no match for /{self.query}"
            return
        self.row = found
        at = self.lines[found].lower().find(self.query.lower())
        self.col = max(0, at)
        self.pref_col = self.col
        self.message = f"/{self.query}"

    def go_up(self) -> bool:
        """Leave a folder. False means the caller should quit."""
        if self.cwd == self.root:
            return False
        parent = self.cwd.parent
        name = self.cwd.name
        self.cwd = parent
        self.refresh()
        for i, entry in enumerate(self.entries):
            if entry.name == name:
                self.selected = i
                break
        return True

    def open_selected(self) -> None:
        shown = self.listing()
        if not shown:
            return
        entry = shown[min(self.selected, len(shown) - 1)]
        self.query = ""
        if entry.kind == "folder":
            self.mode = "list"
            self.cwd = entry.path
            self.selected = 0
            self.refresh()
            return
        self.trail.clear()
        self.open_path(entry.path)

    def title(self) -> str:
        if self.cwd == self.root:
            return self.name
        try:
            rel = self.cwd.relative_to(self.root)
        except ValueError:
            rel = self.cwd
        return self.name + " › " + str(rel)

    def handle(self, key: int | str) -> bool:
        """Return False to quit. ctrl+c always quits, even if keys.json omits it."""
        if key == curses.KEY_RESIZE:
            return True
        if key == 3:
            return False
        if self.mode == "help":
            if self.keys.pressed("help", "close", key):
                self.mode = self.help_from
            elif self.keys.pressed("help", "up", key):
                self.help_at = max(0, self.help_at - 1)
            elif self.keys.pressed("help", "down", key):
                self.help_at += 1
            return True
        if self.mode == "search":
            return self.handle_search(key)
        if self.mode == "cell":
            return self.handle_cell(key)
        if self.mode == "confirm":
            return self.handle_confirm(key)
        if self.mode == "name":
            return self.handle_name(key)
        if self.mode == "filter":
            return self.handle_filter(key)
        if self.mode == "graph":
            return self.handle_graph(key)
        if self.mode == "gfilter":
            return self.handle_gfilter(key)
        if self.mode == "list":
            return self.handle_list(key)
        return self.handle_read(key)

    def handle_list(self, key: int | str) -> bool:
        if self.keys.pressed("list", "quit", key):
            return False
        if self.keys.pressed("list", "back", key):
            return self.go_up()
        if self.keys.pressed("list", "graph", key):
            return self.open_graph()
        if self.keys.pressed("list", "up", key):
            self.selected = max(0, self.selected - 1)
        elif self.keys.pressed("list", "down", key):
            self.selected = min(max(0, len(self.entries) - 1), self.selected + 1)
        elif self.keys.pressed("list", "open", key):
            self.open_selected()
        elif self.keys.pressed("list", "help", key):
            self.help_from = "list"
            self.help_at = 0
            self.mode = "help"
        elif self.keys.pressed("list", "new_page", key):
            self.begin_name("page")
        elif self.keys.pressed("list", "new_folder", key):
            self.begin_name("folder")
        elif self.keys.pressed("list", "find", key):
            self.query = ""
            self.selected = 0
            self.message = ""
            self.mode = "filter"
        elif self.plugins:
            hit = self.plugins.dispatch(self, "list", key)
            if hit is not None:
                return hit
        return True

    def begin_insert(self, *, append: bool) -> None:
        if self.truncated:
            self.message = READ_ONLY
            return
        if self.delimiter and not append:
            self.col = cell_offset(self.lines[self.row], self.delimiter, self.tcol)
            self.pref_col = self.col
        if append:
            self.col = len(self.lines[self.row])
            self.pref_col = self.col
        self.inserting = True
        self._coalesce = None
        self.message = ""

    def handle_read(self, key: int | str) -> bool:
        page = 20
        mode = "insert" if self.inserting else "normal"
        if self.keys.pressed(mode, "save", key):
            self.save()
            return True
        if self.keys.pressed(mode, "reload", key):
            self.reload()
            return True
        if self.keys.pressed(mode, "help", key):
            self.help_from = "read"
            self.help_at = 0
            self.mode = "help"
            return True
        if self.keys.pressed(mode, "undo", key):
            self.undo_edit()
            return True
        if self.keys.pressed(mode, "redo", key):
            self.redo_edit()
            return True
        if self.delimiter and not self.inserting:
            moves = {"up": (-1, 0), "down": (1, 0), "left": (0, -1), "right": (0, 1),
                     "page_up": (-page, 0), "page_down": (page, 0)}
            for action, (dr, dc) in moves.items():
                if self.keys.pressed("normal", action, key):
                    self.move_cell(dr, dc)
                    return True
            table = self.table()
            if self.keys.pressed("normal", "line_start", key):
                self.move_cell(0, -10_000) if not self.record else self.move_cell(-10_000, 0)
                return True
            if self.keys.pressed("normal", "line_end", key):
                self.move_cell(0, 10_000) if not self.record else self.move_cell(10_000, 0)
                return True
            if self.keys.pressed("normal", "view", key) and table is not None:
                self.begin_cell_edit()
                return True
            if self.keys.pressed("normal", "record", key) and table is not None:
                self.record = not self.record
                if self.record and self.row == 0 and len(self.lines) > 1:
                    self.row = 1
                return True
        if self.keys.pressed(mode, "up", key):
            self.move_row(-1)
            return True
        if self.keys.pressed(mode, "down", key):
            self.move_row(1)
            return True
        if self.keys.pressed(mode, "left", key):
            self.move_col(-1)
            return True
        if self.keys.pressed(mode, "right", key):
            self.move_col(1)
            return True
        if self.keys.pressed(mode, "line_start", key):
            self.col = 0
            self.pref_col = 0
            self._coalesce = None
            return True
        if self.keys.pressed(mode, "line_end", key):
            self.col = len(self.lines[self.row])
            self.pref_col = self.col
            self._coalesce = None
            return True
        if self.keys.pressed(mode, "page_up", key):
            self.move_row(-page)
            return True
        if self.keys.pressed(mode, "page_down", key):
            self.move_row(page)
            return True
        if not self.inserting:
            return self.handle_normal(key)
        if self.keys.pressed("insert", "normal", key):
            self.inserting = False
            self._coalesce = None
            return True
        if self.keys.pressed("insert", "backspace", key):
            self.erase_back()
            return True
        if self.keys.pressed("insert", "delete", key):
            self.erase_forward()
            return True
        if self.keys.pressed("insert", "break", key):
            self.break_line()
            return True
        if self.keys.pressed("insert", "indent", key):
            self.insert_text("  ")
            return True
        if self.plugins:
            hit = self.plugins.dispatch(self, "insert", key)
            if hit is not None:
                return hit
        if typed(key):
            self.insert_text(typed(key))
        return True

    def handle_normal(self, key: int | str) -> bool:
        leaving = self.keys.pressed("normal", "leave", key)
        quitting = self.keys.pressed("normal", "quit", key)
        if leaving or quitting:
            if self.dirty:
                self.mode = "confirm"
                self.message = "leave"
                return True
            return self.step_back() if leaving else False
        if self.keys.pressed("normal", "graph", key):
            return self.open_graph()
        if self.keys.pressed("normal", "find", key):
            self.query = ""
            self.mode = "search"
            return True
        if self.keys.pressed("normal", "find_next", key) and self.query:
            self.jump_match(1)
            return True
        if self.keys.pressed("normal", "find_prev", key) and self.query:
            self.jump_match(-1)
            return True
        if self.keys.pressed("normal", "insert", key):
            self.begin_insert(append=False)
            return True
        if self.keys.pressed("normal", "append", key):
            self.begin_insert(append=True)
            return True
        if self.keys.pressed("normal", "view", key):
            if self.path is not None:
                note = open_image(self.path, self.lines[self.row])
                if note:
                    self.message = note
                elif self.markdown:
                    self.follow_link()
            return True
        if self.keys.pressed("normal", "bottom", key):
            self.row = len(self.lines) - 1
            self.col = 0
            self.pref_col = 0
            return True
        if self.keys.pressed("normal", "top", key):
            self.row = 0
            self.col = 0
            self.pref_col = 0
            return True
        if self.plugins:
            hit = self.plugins.dispatch(self, "normal", key)
            if hit is not None:
                return hit
        return True

    def back_to_graph(self) -> bool:
        """Leave a page for the graph it was opened from."""
        self.graph_return = False
        self.mode = "graph"
        self.message = ""
        return True

    def handle_confirm(self, key: int | str) -> bool:
        if self.keys.pressed("confirm", "yes", key):
            if self.message == "reload":
                row = self.row
                self.open_path(self.path) if self.path else None
                self.row = min(row, len(self.lines) - 1)
                self.mode = "read"
                self.message = "reloaded"
                return True
            if self.save():
                self.mode = "read"
                return self.step_back()
            self.mode = "read"
            return True
        if self.keys.pressed("confirm", "no", key):
            if self.message == "reload":
                self.mode = "read"
                self.message = ""
                return True
            self.mode = "read"
            return self.step_back()
        if self.keys.pressed("confirm", "stay", key):
            self.mode = "read"
            self.message = ""
        return True

    def handle_search(self, key: int | str) -> bool:
        if self.keys.pressed("search", "cancel", key):
            self.mode = "read"
            return True
        if self.keys.pressed("search", "jump", key):
            self.mode = "read"
            if self.query:
                self.jump_match(1)
            return True
        if self.keys.pressed("search", "erase", key) or typed(key):
            self.take_query(key, "search")
            return True
        return True

    def begin_name(self, kind: str) -> None:
        self.naming = kind
        self.query = ""
        self.message = ""
        self.mode = "name"

    def finish_name(self) -> None:
        kind = self.naming
        raw = self.query
        self.mode = "list"
        self.query = ""
        try:
            path = create_page(self.cwd, raw) if kind == "page" else create_folder(self.cwd, raw)
        except ValueError as exc:
            self.message = str(exc)
            return
        except FileExistsError as exc:
            self.message = f"already there: {exc}"
            return
        self.refresh(keep=path)
        if kind == "page":
            self.open_path(path)

    def handle_name(self, key: int | str) -> bool:
        if self.keys.pressed("name", "cancel", key):
            self.mode = "list"
            self.query = ""
            return True
        if self.keys.pressed("name", "accept", key):
            self.finish_name()
            return True
        if self.take_query(key, "name"):
            return True
        return True

    def handle_filter(self, key: int | str) -> bool:
        if self.keys.pressed("filter", "cancel", key):
            self.mode = "list"
            self.query = ""
            self.selected = 0
            return True
        if self.keys.pressed("filter", "open", key):
            self.open_selected()
            return True
        shown = self.listing()
        if self.keys.pressed("filter", "up", key):
            self.selected = max(0, self.selected - 1)
            return True
        if self.keys.pressed("filter", "down", key):
            self.selected = min(max(0, len(shown) - 1), self.selected + 1)
            return True
        if self.take_query(key, "filter"):
            self.selected = 0
        return True

    # Graph view ----------------------------------------------------------------

    def open_graph(self) -> bool:
        cfg = load_config()
        library = library_path(cfg)
        data = build_graph(library)
        if not data["pages"]:
            self.message = "no pages to graph"
            return True
        slug = ""
        if self.path is not None:
            try:
                slug = page_slug(self.path, library).lower()
            except ValueError:
                slug = ""
        self.gdata = data
        self.gsel = graph_home(data, slug)
        self.glocal = None
        self.gmissing = True
        self.gfilter = ""
        self.gquery = ""
        self.graph_from = self.mode if self.mode in ("list", "read") else "list"
        self.graph_return = False
        self.gview = None
        self.mode = "graph"
        self.graph_refresh()
        if self.gstate is not None and len(self.gstate["shown"]) > GRAPH_NODE_MAX:
            settle_layout(self.gstate, seconds=2.0)
            self.message = f"{len(self.gstate['shown'])} nodes, settled before draw"
        return True

    def graph_refresh(self) -> None:
        if self.gdata is None:
            return
        nodes, edges = graph_subset(self.gdata, self.gsel, self.glocal, self.gmissing, self.gfilter)
        if self.gsel not in nodes:
            keep_pages = {key: value for key, value in self.gdata["pages"].items() if key in nodes}
            self.gsel = graph_home({"pages": keep_pages}, "") if keep_pages else ""
        old = (self.gstate or {}).get("pos", {})
        pos = {slug: old[slug] for slug in nodes if slug in old}
        pos.update({slug: xy for slug, xy in seed_positions(sorted(set(nodes) - set(pos))).items()})
        self.gstate = {
            "pos": pos,
            "vel": {slug: (0.0, 0.0) for slug in nodes},
            "edges": edges,
            "links": link_map(edges),
            "settled": False,
            "moved": float("inf"),
            "started": time.perf_counter(),
        }
        self.gshown = {"nodes": nodes, "edges": edges, "colors": graph_colors(nodes)}
        self.gstate["shown"] = nodes
        self.gstate["colors"] = graph_colors(nodes)
        self.gview = None

    def close_graph(self) -> bool:
        if self.graph_from == "read" and self.path is not None:
            self.mode = "read"
        else:
            self.mode = "list"
            self.refresh(keep=self.path)
        self.message = ""
        return True

    def step_graph(self) -> None:
        """Batch physics steps to use most of the 33 ms frame, then redraw."""
        st = self.gstate
        if st is None or st.get("settled"):
            return
        budget = time.perf_counter() + 0.02
        count = max(1, len(st["pos"]))
        for _ in range(30):
            step_layout(st)
            if st["moved"] / count < 0.002:
                st["settled"] = True
                if self.gview is not None and self.gview.get("auto"):
                    self.gview = None
                break
            if time.perf_counter() >= budget:
                break
        if time.perf_counter() - st["started"] > GRAPH_ANIMATE_SECONDS:
            # a very large graph could keep drifting for minutes; freeze it
            st["settled"] = True
            if self.gview is not None and self.gview.get("auto"):
                self.gview = None

    def graph_open_selected(self) -> bool:
        nodes = self.gshown["nodes"]
        node = nodes.get(self.gsel)
        if node is None or node["missing"]:
            self.message = "no page behind that node"
            return True
        self.graph_return = True
        self.trail.clear()
        self.message = ""
        self.open_path(Path(node["path"]))
        return True

    def handle_graph(self, key: int | str) -> bool:
        if self.keys.pressed("graph", "quit", key):
            return False
        if self.keys.pressed("graph", "leave", key):
            if self.gfilter:
                self.gfilter = ""
                self.graph_refresh()
                return True
            return self.close_graph()
        if self.keys.pressed("graph", "open", key):
            return self.graph_open_selected()
        if self.keys.pressed("graph", "help", key):
            self.help_from = "graph"
            self.help_at = 0
            self.mode = "help"
            return True
        if self.keys.pressed("graph", "filter", key):
            self.gquery = ""
            self.mode = "gfilter"
            return True
        moves = {"select_up": (0, -1), "select_down": (0, 1), "select_left": (-1, 0), "select_right": (1, 0)}
        for action, vector in moves.items():
            if self.keys.pressed("graph", action, key):
                self.graph_move(vector)
                return True
        pans = {"pan_left": (-1, 0), "pan_right": (1, 0), "pan_up": (0, -1), "pan_down": (0, 1)}
        for action, (sx, sy) in pans.items():
            if self.keys.pressed("graph", action, key):
                cols, rows = self.glast
                if self.gview is not None:
                    self.gview = pan_view(self.gview, 4 * sx, 4 * sy, cols, rows - 1)
                return True
        if self.keys.pressed("graph", "zoom_in", key):
            if self.gview is not None:
                self.gview = zoom_view(self.gview, 1.25)
            return True
        if self.keys.pressed("graph", "zoom_out", key):
            if self.gview is not None:
                self.gview = zoom_view(self.gview, 0.8)
            return True
        if self.keys.pressed("graph", "fit", key):
            self.gview = None
            return True
        if self.keys.pressed("graph", "local_depth", key):
            if self.glocal is None:
                self.glocal = 1
            else:
                self.glocal = min(5, self.glocal + 1) if self.glocal < 5 else None
            self.graph_refresh()
            return True
        if self.keys.pressed("graph", "depth_up", key):
            self.glocal = min(5, (self.glocal or 0) + 1)
            self.graph_refresh()
            return True
        if self.keys.pressed("graph", "depth_down", key):
            depth = (self.glocal or 1) - 1
            self.glocal = depth if depth >= 1 else None
            self.graph_refresh()
            return True
        if self.keys.pressed("graph", "missing", key):
            self.gmissing = not self.gmissing
            self.graph_refresh()
            return True
        return True

    def graph_move(self, vector: tuple[int, int]) -> None:
        st = self.gstate
        if st is None or self.gsel not in st["pos"]:
            return
        cols, rows = self.glast
        if self.gview is None:
            self.gview = fit_view(st["pos"], cols, rows - 1)
        screen = graph_screen(st, self.gview, cols, rows - 1)
        if self.gsel not in screen:
            return
        self.gsel = cone_pick(screen, self.gsel, vector)

    def handle_gfilter(self, key: int | str) -> bool:
        if self.keys.pressed("gfilter", "cancel", key):
            self.gquery = ""
            self.mode = "graph"
            return True
        if self.keys.pressed("gfilter", "accept", key):
            self.gfilter = self.gquery
            self.graph_refresh()
            if self.gstate is not None and not self.gstate["shown"]:
                self.message = f"no pages match /{self.gfilter}"
            self.gquery = ""
            self.mode = "graph"
            return True
        if self.keys.pressed("gfilter", "erase", key):
            self.gquery = self.gquery[:-1]
            return True
        if typed(key):
            self.gquery += typed(key)
        return True

    def graph_mouse(self) -> bool:
        try:
            _, x, y, _, bstate = curses.getmouse()
        except curses.error:
            return True
        cols, rows = self.glast
        view = self.gview
        st = self.gstate
        if st is None or view is None:
            return True
        dot = ((x + 0.5) * 2, (y + 0.5) * 4)
        if bstate & curses.BUTTON4_PRESSED:
            self.gview = zoom_view(view, 1.25, dot[0], dot[1], cols, rows - 1)
            return True
        if bstate & curses.BUTTON5_PRESSED:
            self.gview = zoom_view(view, 0.8, dot[0], dot[1], cols, rows - 1)
            return True
        if bstate & (curses.BUTTON1_DOUBLE_CLICKED | curses.BUTTON1_TRIPLE_CLICKED):
            return self.graph_open_selected()
        if bstate & curses.BUTTON1_PRESSED:
            screen = graph_screen(st, view, cols, rows - 1)
            if not screen:
                return True
            best = min(screen, key=lambda slug: math.hypot(screen[slug][0] - dot[0], screen[slug][1] - dot[1]))
            if math.hypot(screen[best][0] - dot[0], screen[best][1] - dot[1]) > 24:
                return True
            if best == self.gsel:
                return self.graph_open_selected()
            self.gsel = best
        return True


# Graph view. Positions, projection and raster plans are plain data, so the
# tests run without a tty. Only draw_graph touches the screen.

MISSING_MARK = "\x00"
GRAPH_NODE_MAX = 400
GRAPH_STEP_SECONDS = 0.033
GRAPH_ANIMATE_SECONDS = 12.0  # keep spinning at most this long before freezing
GRAPH_COLOR_ROLES = tuple(f"graph_{n}" for n in range(1, 9))


def graph_folder(slug: str) -> str:
    return slug.rsplit("/", 1)[0] if "/" in slug else ""


def graph_nodes(data: dict) -> dict[str, dict]:
    """Every real page plus one hollow node per missing link target."""
    nodes: dict[str, dict] = {}
    for slug, page in data["pages"].items():
        nodes[slug] = {
            "slug": slug,
            "title": page["title"],
            "folder": graph_folder(slug),
            "path": page["path"],
            "missing": False,
            "degree": 0,
        }
    for slug, page in data["pages"].items():
        for raw in page["missing"]:
            ghost = MISSING_MARK + raw
            if ghost not in nodes:
                nodes[ghost] = {
                    "slug": ghost,
                    "title": raw,
                    "folder": graph_folder(raw),
                    "path": "",
                    "missing": True,
                    "degree": 0,
                }
    return nodes


def graph_links(data: dict, nodes: dict) -> list[tuple[str, str]]:
    """Undirected edges, deduped, real and missing."""
    edges: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(a: str, b: str) -> None:
        key = (a, b) if a <= b else (b, a)
        if key in seen:
            return
        seen.add(key)
        edges.append(key)

    for slug, page in data["pages"].items():
        for dest in page["links"]:
            if dest in nodes:
                add(slug, dest)
        for raw in page["missing"]:
            add(slug, MISSING_MARK + raw)
    for a, b in edges:
        nodes[a]["degree"] += 1
        nodes[b]["degree"] += 1
    return edges


GRAPH_REPEL = 0.03
GRAPH_SPRING = 0.06
GRAPH_REST = 0.5
GRAPH_GRAVITY = 0.02
GRAPH_DAMP = 0.85
GRAPH_SPEED = 0.25


def seed_positions(slugs: list[str]) -> dict[str, tuple[float, float]]:
    """Initial positions hashed from the slug. Same library, same layout, always."""
    pos: dict[str, tuple[float, float]] = {}
    for slug in sorted(slugs):
        raw = hashlib.sha256(slug.encode("utf-8")).digest()
        angle = int.from_bytes(raw[:8], "big") / 2**64 * math.tau
        radius = 0.4 + int.from_bytes(raw[8:12], "big") / 2**48 * 0.2
        pos[slug] = (math.cos(angle) * radius, math.sin(angle) * radius)
    return pos


def link_map(edges: list[tuple[str, str]]) -> dict[str, list[str]]:
    touch: dict[str, list[str]] = {}
    for a, b in edges:
        touch.setdefault(a, []).append(b)
        touch.setdefault(b, []).append(a)
    return touch


def step_layout(state: dict) -> float:
    """One round of repulsion, springs and gravity. Returns total movement and
    the fastest node's movement in state['spread'] — the settle test."""
    pos: dict[str, tuple[float, float]] = state["pos"]
    vel: dict[str, tuple[float, float]] = state["vel"]
    touch: dict[str, list[str]] = state["links"]
    slugs = list(pos)
    fresh: dict[str, tuple[float, float]] = {}
    moved = 0.0
    spread = 0.0
    for slug in slugs:
        px, py = pos[slug]
        fx = fy = 0.0
        for other in slugs:
            if other == slug:
                continue
            ox, oy = pos[other]
            dx, dy = px - ox, py - oy
            dist = math.hypot(dx, dy) + 0.001
            strength = GRAPH_REPEL / (dist * dist)
            fx += strength * dx / dist
            fy += strength * dy / dist
        for other in touch.get(slug, ()):
            if other not in pos:
                continue
            ox, oy = pos[other]
            dx, dy = px - ox, py - oy
            dist = math.hypot(dx, dy) + 0.001
            strength = -GRAPH_SPRING * (dist - GRAPH_REST) / dist
            fx += strength * dx
            fy += strength * dy
        fx -= GRAPH_GRAVITY * px
        fy -= GRAPH_GRAVITY * py
        vx = max(-GRAPH_SPEED, min(GRAPH_SPEED, (vel[slug][0] + fx) * GRAPH_DAMP))
        vy = max(-GRAPH_SPEED, min(GRAPH_SPEED, (vel[slug][1] + fy) * GRAPH_DAMP))
        fresh[slug] = (px + vx, py + vy)
        vel[slug] = (vx, vy)
        moved += abs(vx) + abs(vy)
        spread = max(spread, abs(vx), abs(vy))
    state["pos"] = fresh
    state["moved"] = moved
    state["spread"] = spread
    return moved


def settle_layout(state: dict, max_steps: int = 300, eps: float = 0.002, seconds: float | None = None) -> dict:
    """Run steps until the average node is slow enough, capped by count and wall time.

    A total-movement threshold never fires on big graphs: five hundred nearly
    still nodes still add up. So compare the movement per node."""
    start = time.perf_counter()
    steps = 0
    count = max(1, len(state["pos"]))
    while steps < max_steps and state.get("moved", float("inf")) / count >= eps:
        step_layout(state)
        steps += 1
        if seconds is not None and time.perf_counter() - start >= seconds:
            break
    state["settled"] = True
    state["steps"] = steps
    return state


def content_dots(cols: int, rows: int) -> tuple[int, int]:
    return (max(1, cols - 1) * 2, max(1, rows - 1) * 4)


def fit_view(pos: dict, cols: int, rows: int, pad: float = 4.0) -> dict:
    """Scale and center that puts the whole graph on screen."""
    w, h = content_dots(cols, rows)
    if not pos:
        return {"cx": 0.0, "cy": 0.0, "z": 1.0, "zfit": 1.0}
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    span_x = max(0.001, max(xs) - min(xs))
    span_y = max(0.001, max(ys) - min(ys))
    z = max(0.001, min((w - 2 * pad) / span_x, (h - 2 * pad) / span_y))
    return {"cx": (min(xs) + max(xs)) / 2, "cy": (min(ys) + max(ys)) / 2, "z": z, "zfit": z, "auto": True}


def project(x: float, y: float, view: dict, cols: int, rows: int) -> tuple[float, float]:
    w, h = content_dots(cols, rows)
    return ((x - view["cx"]) * view["z"] + w / 2, (y - view["cy"]) * view["z"] + h / 2)


def unproject(dx: float, dy: float, view: dict, cols: int, rows: int) -> tuple[float, float]:
    w, h = content_dots(cols, rows)
    return (view["cx"] + (dx - w / 2) / view["z"], view["cy"] + (dy - h / 2) / view["z"])


def zoom_view(view: dict, factor: float, dx: float | None = None, dy: float | None = None, cols: int = 0, rows: int = 0) -> dict:
    """Zoom keeps the point under the pointer in place; without one, the center."""
    z = max(0.001, view["z"] * factor)
    w, h = content_dots(cols, rows)
    fresh = {"cx": view["cx"], "cy": view["cy"], "z": z, "zfit": view.get("zfit", view["z"])}
    if dx is not None and dy is not None:
        wx, wy = unproject(dx, dy, view, cols, rows)
        fresh["cx"] = wx - (dx - w / 2) / z
        fresh["cy"] = wy - (dy - h / 2) / z
    return fresh


def pan_view(view: dict, dx: float, dy: float, cols: int, rows: int) -> dict:
    return {
        "cx": view["cx"] - dx / view["z"],
        "cy": view["cy"] - dy / view["z"],
        "z": view["z"],
        "zfit": view.get("zfit", view["z"]),
    }


BRAILLE_BASE = 0x2800
BRAILLE_BITS = {
    (0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (0, 3): 0x40,
    (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20, (1, 3): 0x80,
}


def braille_char(bits: int) -> str:
    return chr(BRAILLE_BASE + (bits & 0xFF))


def canvas_new() -> dict:
    """Cells map to braille bits. Empty cell means an empty dot pattern."""
    return {}


def canvas_plot(canvas: dict, dx: float, dy: float) -> None:
    cell = (int(dx) // 2, int(dy) // 4)
    canvas[cell] = canvas.get(cell, 0) | BRAILLE_BITS[(int(dx) % 2, int(dy) % 4)]


def canvas_line(canvas: dict, x0: float, y0: float, x1: float, y1: float) -> set[tuple[int, int]]:
    """Bresenham in dot space. Returns the cells it touched, for edge colors."""
    touched: set[tuple[int, int]] = set()
    steps = max(abs(x1 - x0), abs(y1 - y0))
    count = int(steps)
    if count == 0:
        canvas_plot(canvas, x0, y0)
        touched.add((int(x0) // 2, int(y0) // 4))
        return touched
    for i in range(count + 1):
        x = x0 + (x1 - x0) * i / count
        y = y0 + (y1 - y0) * i / count
        canvas_plot(canvas, x, y)
        touched.add((int(x) // 2, int(y) // 4))
    return touched


def canvas_disc(canvas: dict, dx: float, dy: float, radius: float) -> list[tuple[int, int]]:
    """Filled dot cluster sized by degree, like Obsidian. Returns its cells."""
    span = max(1, int(radius) + 1)
    touched: list[tuple[int, int]] = []
    for iy in range(int(dy) - span, int(dy) + span + 1):
        for ix in range(int(dx) - span, int(dx) + span + 1):
            if (ix - dx) ** 2 + (iy - dy) ** 2 <= radius * radius:
                canvas_plot(canvas, ix, iy)
                touched.append((int(ix) // 2, int(iy) // 4))
    return touched


def canvas_ring(canvas: dict, dx: float, dy: float, radius: float) -> list[tuple[int, int]]:
    """Hollow dot circle for a page that does not exist. Returns its cells."""
    span = max(1, int(radius) + 1)
    touched: list[tuple[int, int]] = []
    for iy in range(int(dy) - span, int(dy) + span + 1):
        for ix in range(int(dx) - span, int(dx) + span + 1):
            if abs(math.hypot(ix - dx, iy - dy) - radius) <= 0.9:
                canvas_plot(canvas, ix, iy)
                touched.append((int(ix) // 2, int(iy) // 4))
    return touched


def canvas_rows(canvas: dict, cols: int, rows: int) -> list[str]:
    w = max(0, cols - 1)
    out: list[str] = []
    for ry in range(rows):
        out.append("".join(braille_char(canvas.get((rx, ry), 0)) for rx in range(w)))
    return out


def graph_colors(nodes: dict) -> dict[str, str]:
    """One palette color per folder, in sorted order; top-level is its own group."""
    folders = sorted({node["folder"] for node in nodes.values()})
    pick = {folder: GRAPH_COLOR_ROLES[i % len(GRAPH_COLOR_ROLES)] for i, folder in enumerate(folders)}
    return {slug: pick[node["folder"]] for slug, node in nodes.items()}


def node_radius(node: dict) -> float:
    if node["missing"]:
        return 2.5
    return 1.5 + min(5, node["degree"]) * 0.75


def graph_home(data: dict, want: str) -> str:
    """Where selection starts: the page you came from, else index, else most linked."""
    pages = data["pages"]
    if want in pages:
        return want
    if "index" in pages:
        return "index"
    if not pages:
        return ""
    return max(
        pages,
        key=lambda slug: len(pages[slug]["links"]) + len(pages[slug]["backlinks"]),
    )


def local_nodes(nodes: dict, edges: list[tuple[str, str]], slug: str, depth: int) -> set[str]:
    """Nodes within depth link steps, forwards and backwards."""
    touch = link_map(edges)
    kept = {slug}
    edge = {slug}
    for _ in range(max(1, depth)):
        near: set[str] = set()
        for here in edge:
            for there in touch.get(here, ()):
                if there not in kept:
                    near.add(there)
        kept |= near
        edge = near
    return kept


def filter_slugs(nodes: dict, query: str) -> set[str]:
    needle = query.lower()
    return {
        slug
        for slug, node in nodes.items()
        if needle in slug.lower() or needle in node["title"].lower()
    }


def graph_subset(
    data: dict,
    slug: str,
    depth: int | None,
    missing: bool,
    query: str,
) -> tuple[dict, list[tuple[str, str]]]:
    """The nodes and edges for the current local, missing and text filters."""
    nodes = graph_nodes(data)
    edges = graph_links(data, nodes)
    if depth is not None:
        keep = local_nodes(nodes, edges, slug, depth)
        nodes = {key: node for key, node in nodes.items() if key in keep}
        edges = [(a, b) for a, b in edges if a in nodes and b in nodes]
    if not missing:
        ghosts = {key for key, node in nodes.items() if node["missing"]}
        nodes = {key: node for key, node in nodes.items() if key not in ghosts}
        edges = [(a, b) for a, b in edges if a in nodes and b in nodes]
    if query:
        keep = filter_slugs(nodes, query)
        nodes = {key: node for key, node in nodes.items() if key in keep}
        edges = [(a, b) for a, b in edges if a in nodes and b in nodes]
    return nodes, edges


def cone_pick(screen: dict, origin: str, vector: tuple[int, int]) -> str:
    """Nearest node in a direction, in a 60 degree cone. Stays put if none."""
    dxn, dyn = vector
    sx, sy = screen[origin]
    best = origin
    best_score = 0.0
    for slug, (x, y) in screen.items():
        if slug == origin:
            continue
        dx, dy = x - sx, y - sy
        dist = math.hypot(dx, dy)
        if dist < 0.5:
            continue
        cos = (dx * dxn + dy * dyn) / dist
        if cos < 0.5:
            continue
        score = dist * (2 - cos)
        if best_score == 0.0 or score < best_score:
            best_score = score
            best = slug
    return best


def place_labels(
    items: list[tuple[str, float, float, float, int, str]],
    cols: int, rows: int, forced: set[str],
) -> list[tuple[str, int, int, str]]:
    """Rects for labels that fit. Forced ones (selected and neighbors) come first."""
    placed: list[tuple[str, int, int, str]] = []
    used: list[tuple[int, int, int, int]] = []
    order = sorted(items, key=lambda item: (item[0] not in forced, -item[1], item[0]))
    for slug, _, px, py, _, text in order:
        if not text or len(text) + 1 >= cols:
            continue
        col = max(0, min(cols - 1 - len(text), int(px) // 2 - len(text) // 2))
        row = max(0, min(rows - 2, int(py) // 4 + 1))
        rect = (col - 1, row - 1, col + len(text) + 1, row + 1)
        if used and any(
            rect[0] < other[2] and rect[2] > other[0] and rect[1] < other[3] and rect[3] > other[1]
            for other in used
        ):
            continue
        placed.append((slug, col, row, text))
        used.append(rect)
    return placed


def graph_screen(st: dict, view: dict, cols: int, rows: int) -> dict[str, tuple[float, float]]:
    """Dot-space positions for the nodes currently shown."""
    return {slug: project(x, y, view, cols, rows - 1) for slug, (x, y) in st["pos"].items() if slug in st["shown"]}


def graph_raster(
    st: dict,
    screen: dict[str, tuple[float, float]],
    selected: str,
    neighbors: set[str],
) -> tuple[dict, dict]:
    """Braille bit cells plus one style per cell. Edges first, nodes on top."""
    canvas: dict[tuple[int, int], int] = {}
    styles: dict[tuple[int, int], str] = {}
    for a, b in st["edges"]:
        first = screen.get(a)
        second = screen.get(b)
        if first is None or second is None:
            continue
        style = "graph_edge_hi" if a == selected or b == selected else "graph_edge"
        for cell in canvas_line(canvas, *first, *second):
            styles[cell] = style
    colors = st["colors"]
    for slug, node in st["shown"].items():
        at = screen.get(slug)
        if at is None:
            continue
        if node["missing"]:
            cells = canvas_ring(canvas, *at, node_radius(node))
        else:
            cells = canvas_disc(canvas, *at, node_radius(node))
        for cell in cells:
            styles[cell] = colors[slug]
    return canvas, styles


def place_cells(
    canvas: dict, styles: dict, labels: list[tuple[str, int, int, str]], rows: int,
) -> list[tuple[int, int, str, int]]:
    """Group bit cells into paintable runs. Cells under a label are dropped."""
    covered: set[tuple[int, int]] = set()
    for _, col, row, text in labels:
        covered |= {(col + i, row) for i in range(len(text))}
    runs: list[tuple[int, int, str, int]] = []
    order = sorted((cy, cx) for cx, cy in canvas)
    for cy, cx in order:
        if cx < 0 or cy < 0 or (cx, cy) in covered:
            continue
        bits = canvas[(cx, cy)]
        style = styles.get((cx, cy), "graph_edge")
        if runs:
            prev_cy, prev_cx, prev_style, span = runs[-1]
            if prev_cy == cy and prev_cx + span == cx and prev_style == style:
                runs[-1] = (prev_cy, prev_cx, prev_style, span + 1)
                continue
        runs.append((cy, cx, style, 1))
    _ = rows
    return runs


def graph_neighbors(st: dict, selected: str) -> set[str]:
    return {other for a, b in st["edges"] for other in (a, b) if other != selected and selected in (a, b)}


def label_attrs(st: dict, selected: str, focus: set[str]) -> dict[str, int]:
    return {slug: (style_attr(st["colors"][slug]) | (curses.A_BOLD if slug == selected else 0)) for slug in focus}


def draw_graph(stdscr: curses.window, app: App, rows: int, cols: int) -> None:
    st = app.gstate
    if not st or not st["shown"]:
        put(stdscr, 0, 0, "(no pages)", style_attr(DIM), cols, rows)
        app.glast = (cols, rows)
        return
    if app.gview is None:
        app.gview = fit_view(st["pos"], cols, rows - 1)
    view = app.gview
    screen = graph_screen(st, view, cols, rows)
    canvas, styles = graph_raster(st, screen, app.gsel, graph_neighbors(st, app.gsel))
    focus = {app.gsel} | graph_neighbors(st, app.gsel)
    shown_zoom = view["z"] > view.get("zfit", view["z"])
    items: list[tuple[str, float, float, float, int, str]] = []
    for slug, node in st["shown"].items():
        if slug not in focus and not (shown_zoom and not node["missing"]):
            continue
        if node["missing"] and slug != app.gsel:
            continue
        at = screen[slug]
        items.append((slug, node["degree"], at[0], at[1], node_radius(node), node["title"]))
    labels = place_labels(items, cols, rows - 1, focus)
    runs = place_cells(canvas, styles, labels, rows)
    for cy, cx, style, span in runs:
        put(
            stdscr,
            cy,
            cx,
            "".join(braille_char(canvas[(ccx, cy)]) for ccx in range(cx, cx + span)),
            style_attr(style),
            cols,
            rows,
        )
    attrs = label_attrs(st, app.gsel, focus)
    for slug, col, row, text in labels:
        put(stdscr, row, col, text, attrs.get(slug, style_attr(DIM) | curses.A_BOLD), cols, rows)
    app.glast = (cols, rows)


def style_attr(style: str) -> int:
    italic = getattr(curses, "A_ITALIC", 0)
    extra = {
        TITLE: curses.A_BOLD,
        HEADING: curses.A_BOLD,
        BOLD: curses.A_BOLD,
        ITALIC: italic,
        SELECTED: curses.A_BOLD,
        DIM: curses.A_DIM,
        FOCUS: curses.A_BOLD,
        CURSOR: curses.A_REVERSE | curses.A_BOLD,
        FOOTER: curses.A_DIM,
        TEXT: curses.A_DIM,
    }
    return THEME_PAIRS.get(style, 0) | extra.get(style, 0)


def init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    theme = load_theme()
    THEME_PAIRS.clear()
    limit = curses.COLORS if curses.has_colors() else 0
    for number, role in enumerate(DEFAULT_THEME, start=1):
        try:
            fg = color_index(theme[role])
        except ValueError:
            fg = None
        if fg is None or fg < 0 or fg >= limit:
            fg = -1
        try:
            curses.init_pair(number, fg, -1)
        except curses.error:
            curses.init_pair(number, -1, -1)
        THEME_PAIRS[role] = curses.color_pair(number)


def put(stdscr: curses.window, y: int, x: int, text: str, attr: int, cols: int, rows: int) -> None:
    if y < 0 or y >= rows - 0 or x >= cols - 1 or not text:
        return
    text = text[: cols - 1 - x]
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


def paint_spans(
    stdscr: curses.window,
    rows_out: list[tuple[int, int, list[tuple[str, str]]]],
    y_off: int,
    cols: int,
    rows: int,
) -> None:
    for y, x, spans in rows_out:
        cursor_x = x
        for text, style in spans:
            put(stdscr, y + y_off, cursor_x, text, style_attr(style), cols, rows)
            cursor_x += len(text)


def reader_status(lines: list[str], row: int, col: int) -> str:
    text = "\n".join(lines)
    chars = len(text)
    words = len(text.split())
    return f"{chars} ch  {words} w  {row + 1}:{col + 1}"


def fit_hints(parts: list[str], width: int, more: str = "?") -> str:
    """Join hints that fit. A narrow pane keeps the first ones and a way to the menu."""
    if width < 1:
        return ""
    full = " · ".join(part for part in parts if part)
    if len(full) <= width:
        return full
    tail = f" · {more}" if more else ""
    room = width - len(tail)
    if room < 1:
        return more[:width]
    chosen: list[str] = []
    for part in parts:
        if not part:
            continue
        trial = " · ".join(chosen + [part])
        if len(trial) <= room:
            chosen.append(part)
            continue
        break
    if not chosen:
        return more[:width]
    return " · ".join(chosen) + tail


def help_window(lines: list[str], height: int, scroll: int) -> tuple[int, list[str]]:
    if height < 1:
        return 0, []
    scroll = max(0, min(scroll, max(0, len(lines) - height)))
    return scroll, lines[scroll:scroll + height]


def spread_text(parts: list[str], width: int) -> str:
    """First part on the left edge, last part on the right edge."""
    parts = [part for part in parts if part]
    if width < 1 or not parts:
        return ""
    if sum(len(part) for part in parts) + len(parts) - 1 >= width or len(parts) == 1:
        text = " ".join(parts)
        return text[:width]
    gaps = len(parts) - 1
    extra = width - sum(len(part) for part in parts)
    base, rem = divmod(extra, gaps)
    out = []
    for index, part in enumerate(parts):
        out.append(part)
        if index < gaps:
            out.append(" " * (base + (1 if index < rem else 0)))
    return "".join(out)


def footer_bar(parts: list[str], width: int, more: str = "?", right: str = "") -> str:
    """Hints stay whole. The bar uses the full width. Counts sit on the right edge."""
    if width < 1:
        return ""
    if right and len(right) >= width:
        return right[:width]
    room = width - len(right) - 2 if right else width
    hints = fit_hints(parts, max(1, room), more=more)
    hint_parts = [part for part in hints.split(" · ") if part]
    if not right:
        return spread_text(hint_parts, width)
    left = " · ".join(hint_parts)
    gap = width - len(left) - len(right)
    return left + (" " * max(1, gap)) + right


def key_hint(keys: KeyMap, mode: str, action: str, label: str) -> str:
    return f"{keys.show(mode, action)} {label}".strip()


def bar_spec(app: App) -> tuple[list[str], str, str]:
    keys = app.keys
    if app.mode == "name":
        kind = "page" if app.naming == "page" else "folder"
        return [f"new {kind}: {app.query}", "enter make", "esc cancel"], "", ""
    if app.mode == "filter":
        return [f"/{app.query}", "enter open", "esc cancel"], "", ""
    if app.mode == "gfilter":
        return [f"/{app.gquery}", "enter keep", "esc cancel"], "", ""
    if app.mode == "graph":
        node = {}
        if app.gshown:
            node = app.gshown["nodes"].get(app.gsel, {})
        page = {}
        if app.gdata and app.gsel in app.gdata["pages"]:
            page = app.gdata["pages"][app.gsel]
        counts = ""
        if page:
            counts = f"{len(page['links'])} →  {len(page['backlinks'])} ←"
        if node.get("missing"):
            counts = "missing"
        if app.glocal is not None:
            scope = f"local, depth {app.glocal}"
        elif app.gshown:
            scope = f"{len(app.gshown['nodes'])} nodes"
        else:
            scope = ""
        extra = ""
        if app.gfilter:
            extra = f"{extra} /{app.gfilter}" if extra else f"/{app.gfilter}"
        if not app.gmissing:
            extra = f"{extra} missing off"
        parts = [
            node.get("title", "the shelf"),
            counts,
            extra,
            "enter open",
            key_hint(keys, "graph", "leave", "back"),
            f"{keys.show('graph', 'zoom_in')} {keys.show('graph', 'zoom_out')} zoom",
            key_hint(keys, "graph", "local_depth", "local"),
            key_hint(keys, "graph", "missing", "missing"),
            key_hint(keys, "graph", "filter", "filter"),
            key_hint(keys, "graph", "help", "keys"),
            key_hint(keys, "graph", "quit", "quit"),
        ]
        if app.message:
            parts.insert(0, app.message)
        return parts, keys.show("graph", "help"), scope
    if app.mode == "list":
        parts = [
            f"{keys.show('list', 'up')} {keys.show('list', 'down')} move",
            key_hint(keys, "list", "open", "open"),
            key_hint(keys, "list", "find", "file"),
            key_hint(keys, "list", "new_page", "page"),
            key_hint(keys, "list", "new_folder", "folder"),
            key_hint(keys, "list", "back", "back"),
            key_hint(keys, "list", "quit", "quit"),
            key_hint(keys, "list", "help", "keys"),
        ]
        if app.message:
            parts.insert(0, app.message)
        return parts, keys.show("list", "help"), ""
    right = ""
    if app.path is not None or app.lines:
        name = app.path.name if app.path else ""
        stats = reader_status(app.lines, app.row, app.col)
        right = f"{name}  {stats}" if name else stats
    if app.mode == "confirm":
        if app.message == "reload":
            parts = [
                key_hint(keys, "confirm", "yes", "reload"),
                key_hint(keys, "confirm", "no", "stay"),
                key_hint(keys, "confirm", "stay", "stay"),
            ]
        else:
            parts = [
                "unsaved",
                key_hint(keys, "confirm", "yes", "save"),
                key_hint(keys, "confirm", "no", "discard"),
                key_hint(keys, "confirm", "stay", "stay"),
            ]
        return parts, "", right
    if app.mode == "search":
        return ["/" + app.query], keys.show("normal", "help"), right
    if app.mode == "cell":
        table = app.table()
        label = table.header(app.tcol) if table else "cell"
        return [f"{label}: {app.cell_text}▏", "enter keep", "esc cancel"], "", ""
    if app.inserting:
        return [
            "insert",
            key_hint(keys, "insert", "normal", "normal"),
            key_hint(keys, "insert", "save", "save"),
            key_hint(keys, "normal", "help", "keys"),
        ], keys.show("normal", "help"), right
    table = app.table() if app.table_view() else None
    if table is not None:
        right = f"row {max(1, app.row)}/{max(0, len(table.rows) - 1)}  col {app.tcol + 1}/{table.columns}"
        value = table.cell(app.row, app.tcol)
        here = f"{table.header(app.tcol)}: {value}" if app.row > 0 else table.header(app.tcol)
        if len(here) > 60:
            here = here[:59] + "…"
        parts = [
            app.message or ("unsaved" if app.dirty else here),
            key_hint(keys, "normal", "record", "grid" if app.record else "record"),
            key_hint(keys, "normal", "view", "edit"),
            key_hint(keys, "normal", "insert", "raw"),
            key_hint(keys, "normal", "leave", "back"),
            key_hint(keys, "normal", "help", "keys"),
        ]
        return parts, keys.show("normal", "help"), right
    parts = [
        "normal",
        key_hint(keys, "normal", "insert", "insert"),
        key_hint(keys, "normal", "append", "append"),
        key_hint(keys, "normal", "leave", "back"),
        key_hint(keys, "normal", "help", "keys"),
    ]
    if app.message:
        parts.insert(0, app.message)
    elif app.dirty:
        parts.insert(0, "unsaved")
    return parts, keys.show("normal", "help"), right


def paint_bar(stdscr: curses.window, app: App, rows: int, cols: int) -> None:
    parts, more, right = bar_spec(app)
    text = footer_bar(parts, cols - 1, more=more, right=right)
    put(stdscr, rows - 1, 0, text, style_attr(FOOTER), cols, rows)


def draw(stdscr: curses.window, app: App) -> None:
    stdscr.erase()
    rows, cols = stdscr.getmaxyx()
    if rows < 8 or cols < 30:
        put(stdscr, 0, 0, "terminal too small", style_attr(FOOTER), cols, rows)
        stdscr.refresh()
        return
    if app.mode in ("list", "name", "filter"):
        draw_list(stdscr, app, rows, cols)
    elif app.mode in ("graph", "gfilter"):
        draw_graph(stdscr, app, rows, cols)
    else:
        draw_read(stdscr, app, rows, cols)
    if app.mode == "help":
        app.help_at = draw_help(stdscr, rows, cols, help_lines(app.name, app.keys), app.help_at)
    else:
        paint_bar(stdscr, app, rows, cols)
    stdscr.refresh()


def banner(text: str) -> list[str] | None:
    """Two-and-a-bit times a normal row. A cell grid cannot grow one word by 25%."""
    glyphs = {
        "A": ["..#..", ".#.#.", "#...#", "#####", "#...#", "#...#"],
        "B": ["####.", "#...#", "#...#", "####.", "#...#", "####."],
        "C": [".###.", "#...#", "#....", "#....", "#...#", ".###."],
        "D": ["####.", "#...#", "#...#", "#...#", "#...#", "####."],
        "E": ["#####", "#....", "####.", "#....", "#....", "#####"],
        "F": ["#####", "#....", "####.", "#....", "#....", "#...."],
        "G": [".###.", "#...#", "#....", "#.###", "#...#", ".###."],
        "H": ["#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
        "I": ["..#..", "..#..", "..#..", "..#..", "..#..", "..#.."],
        "J": ["...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."],
        "K": ["#...#", "#..#.", "###..", "#..#.", "#...#", "#...#"],
        "L": ["#....", "#....", "#....", "#....", "#....", "#####"],
        "M": ["#...#", "##.##", "#.#.#", "#...#", "#...#", "#...#"],
        "N": ["#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#"],
        "O": [".###.", "#...#", "#...#", "#...#", "#...#", ".###."],
        "P": ["####.", "#...#", "#...#", "####.", "#....", "#...."],
        "Q": [".###.", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"],
        "R": ["####.", "#...#", "#...#", "####.", "#...#", "#...#"],
        "S": [".####", "#....", ".###.", "....#", "#...#", ".###."],
        "T": ["#####", "..#..", "..#..", "..#..", "..#..", "..#.."],
        "U": ["#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
        "V": ["#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."],
        "W": ["#...#", "#...#", "#...#", "#.#.#", "##.##", "#...#"],
        "X": ["#...#", ".#.#.", "..#..", "..#..", ".#.#.", "#...#"],
        "Y": ["#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."],
        "Z": ["#####", "...#.", "..#..", ".#...", "#....", "#####"],
        " ": [".....", ".....", ".....", ".....", ".....", "....."],
    }
    block = {(False, False): " ", (True, False): "▀", (False, True): "▄", (True, True): "█"}
    letters = text.upper()
    if not letters or any(ch not in glyphs for ch in letters):
        return None
    lines = ["", "", ""]
    for index, ch in enumerate(letters):
        glyph = glyphs[ch]
        if index:
            lines = [line + " " for line in lines]
        for row in range(3):
            cell = ""
            for col in range(5):
                upper = glyph[row * 2][col] == "#"
                lower = glyph[row * 2 + 1][col] == "#"
                cell += block[(upper, lower)]
            lines[row] += cell
    return lines


def title_parts(title: str) -> tuple[str, str]:
    if " › " in title:
        name, rest = title.split(" › ", 1)
        return name, rest
    return title, ""


def home_top(rows: int, header: int, entries: int) -> int:
    """Vertical start of the banner plus the menu, when the whole block fits."""
    list_h = 0 if entries < 1 else entries * 3 - 1
    block = header + list_h
    usable = rows - 1
    if block >= usable or block < 1:
        return 0
    return (usable - block) // 2


def draw_list(stdscr: curses.window, app: App, rows: int, cols: int) -> None:
    name, suffix = title_parts(app.title())
    art = banner(name)
    shown = app.listing()
    if art and len(art[0]) < cols - 2 and rows >= 16:
        width = max(len(line) for line in art)
        left = max(0, (cols - 1 - width) // 2)
        top = home_top(rows, len(art) + 1, len(shown))
        for i, line in enumerate(art):
            put(stdscr, top + i, left, line, style_attr(TITLE), cols, rows)
        if suffix:
            label = "› " + suffix
            put(
                stdscr,
                top + len(art),
                max(0, (cols - 1 - len(label)) // 2),
                label,
                style_attr(DIM),
                cols,
                rows,
            )
        header = top + len(art) + 1
    else:
        title = app.title()
        top = home_top(rows, 2, len(shown))
        put(stdscr, top, max(0, (cols - len(title)) // 2), title, style_attr(TITLE), cols, rows)
        header = top + 2
    content_h = rows - 1 - header
    painted = list_layout(shown, app.selected, content_h, cols)
    paint_spans(stdscr, painted, header, cols, rows)
    if not shown:
        empty = "no file" if app.mode == "filter" and app.query else "empty"
        put(stdscr, min(rows - 2, header + 1), max(0, (cols - len(empty)) // 2), empty, style_attr(DIM), cols, rows)


def draw_read(stdscr: curses.window, app: App, rows: int, cols: int) -> None:
    link_row = 1 if app.backlinks else 0
    content_h = rows - 1 - link_row
    table = app.table() if app.table_view() else None
    if table is not None:
        layout = record_layout if app.record else table_layout
        painted = layout(table, app.row, app.tcol, content_h, cols - 1)
    else:
        painted = doc_layout(
            app.lines,
            app.row,
            app.col,
            content_h,
            cols - 1,
            markdown=app.markdown,
            fences=app.fences,
            renderer=app.renderer,
            note=app.path,
        )
    paint_spans(stdscr, painted, 0, cols, rows)
    if app.backlinks:
        put(
            stdscr,
            rows - 2,
            0,
            ("← " + ", ".join(app.backlinks))[: cols - 1],
            style_attr(DIM),
            cols,
            rows,
        )


def draw_help(stdscr: curses.window, rows: int, cols: int, lines: tuple[str, ...], scroll: int = 0) -> int:
    stdscr.erase()
    width = max(20, min(62, cols - 2))
    inner = max(8, width - 4)
    wrapped: list[str] = []
    for line in lines:
        if not line:
            wrapped.append("")
            continue
        for start, end in wrap_ranges(line, inner):
            piece = line[start:end].rstrip()
            if piece:
                wrapped.append(piece)
    height = min(max(6, len(wrapped) + 3), rows - 1)
    top = max(0, (rows - height) // 2)
    left = max(0, (cols - width) // 2)
    body = height - 3
    scroll, page = help_window(wrapped, body, scroll)
    more = scroll + body < len(wrapped)
    hint = "j k scroll · esc close" if more or scroll else "esc close"
    for y in range(height):
        put(stdscr, top + y, left, " " * (width - 1), curses.A_NORMAL, cols, rows)
    put(stdscr, top, left, "╭" + "─" * (width - 3) + "╮", style_attr(DIM), cols, rows)
    put(stdscr, top + height - 1, left, "╰" + "─" * (width - 3) + "╯", style_attr(DIM), cols, rows)
    for i, line in enumerate(page):
        y = top + 1 + i
        put(stdscr, y, left, "│", style_attr(DIM), cols, rows)
        put(stdscr, y, left + width - 2, "│", style_attr(DIM), cols, rows)
        style = TITLE if scroll == 0 and i == 0 else TEXT
        put(stdscr, y, left + 2, line[:inner], style_attr(style), cols, rows)
    hint_line = hint[:inner]
    hint_pad = max(0, (inner - len(hint_line)) // 2)
    put(stdscr, top + height - 2, left + 2, " " * hint_pad + hint_line, style_attr(DIM), cols, rows)
    return scroll


USAGE = """\
grimoire                  open ~/.grimoire/pages
grimoire -c               the folder you are in
grimoire --current        same as -c
grimoire -l, --local      same as -c
grimoire <path>           that file or folder
grimoire setup            choose the shelf and the name
grimoire graph             write the page graph and print it
grimoire graph --json      same graph, for an agent
grimoire graph <page>      links and backlinks for one page
grimoire keys             write ~/.grimoire/keys.json if missing, then print it
grimoire lint             orphans and links that point nowhere
grimoire pages [--json]    list pages, links, and backlinks
grimoire read <page>       print one page
grimoire write <page>      write a page from stdin, then refresh the graph
grimoire search <query>    find a line in the pages
grimoire mcp               stdio MCP server for agents
grimoire plugins          list plugins in ~/.grimoire/plugins
grimoire images           which image tools are installed
grimoire theme            write ~/.grimoire/theme.json if missing, then print it
grimoire --version        print the version

Config: ~/.grimoire/config.json
  name      text on the title row (default GRIMOIRE)
  library   shelf used by a bare launch (default ~/.grimoire/pages)
"""


LOCAL_FLAGS = {"-c", "--current", "-l", "--local"}


def resolve_launch(argv: list[str], cfg: dict[str, str], cwd: Path) -> tuple[Path, Path | None]:
    wants_local = any(arg in LOCAL_FLAGS for arg in argv)
    args = [arg for arg in argv if arg not in LOCAL_FLAGS]
    if not args:
        if wants_local:
            return cwd.resolve(), None
        return library_path(cfg), None
    if args[0] in ("library", "--library", "."):
        return (library_path(cfg), None) if args[0] != "." else (cwd.resolve(), None)
    return _path_target(args[0], cwd)


def _path_target(raw: str, cwd: Path) -> tuple[Path, Path | None]:
    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = cwd / target
    target = target.resolve()
    if target.is_dir():
        return target, None
    if target.is_file():
        return target.parent, target
    print(f"grimoire: no such path: {target}", file=sys.stderr)
    raise SystemExit(1)


def parse_setup_args(args: list[str]) -> dict[str, str] | None:
    if not args:
        return None
    out: dict[str, str] = {}
    i = 0
    while i < len(args):
        flag = args[i]
        if flag not in ("--open", "--library", "--name"):
            print(f"grimoire: unknown setup flag {flag}", file=sys.stderr)
            raise SystemExit(2)
        if i + 1 >= len(args):
            print(f"grimoire: {flag} needs a value", file=sys.stderr)
            raise SystemExit(2)
        out[flag[2:]] = args[i + 1]
        i += 2
    return out


def prompt_setup(cfg: dict[str, str]) -> dict[str, str]:
    print(f"Grimoire setup. Writes {config_path()}")
    print()
    print("When you run grimoire with no path, open:")
    print("  1  the folder you are in")
    print("  2  the library")
    current = "1" if cfg["open"] == "cwd" else "2" if cfg["open"] == "library" else cfg["open"]
    choice = input(f"[{current}]: ").strip()
    if choice in ("", "1") and current == "1":
        open_mode = "cwd"
    elif choice == "1":
        open_mode = "cwd"
    elif choice in ("", "2") and current == "2":
        open_mode = "library"
    elif choice == "2":
        open_mode = "library"
    elif choice == "":
        open_mode = cfg["open"]
    else:
        open_mode = choice
    library = input(f"Library folder [{cfg['library']}]: ").strip() or cfg["library"]
    name = input(f"Name on screen [{cfg['name']}]: ").strip() or cfg["name"]
    return {"open": open_mode, "library": library, "name": name}


def setup_config(args: list[str]) -> dict[str, str]:
    cfg = load_config()
    picked = parse_setup_args(args)
    if picked is None:
        if not sys.stdin.isatty():
            print(USAGE, end="")
            raise SystemExit(2)
        picked = prompt_setup(cfg)
    merged = {**cfg, **picked}
    path = save_config(merged)
    print(f"wrote {path}")
    print(f"  name     {merged['name']}")
    print(f"  open     {merged['open']}")
    print(f"  library  {merged['library']}")
    return merged


def show_graph(args: list[str]) -> None:
    library = library_path(load_config())
    path = write_graph(library)
    graph = json.loads(path.read_text(encoding="utf-8"))
    if "--json" in args:
        print(path.read_text(encoding="utf-8"), end="")
        return
    page = next((arg for arg in args if not arg.startswith("-")), None)
    if page:
        found = None
        for slug, info in graph["pages"].items():
            if slug.lower() == page.lower():
                found = (slug, info)
                break
        if found is None:
            print(f"grimoire: no page {page}", file=sys.stderr)
            raise SystemExit(1)
        slug, info = found
        print(slug)
        print("  links      " + (", ".join(info["links"]) or "-"))
        print("  backlinks  " + (", ".join(info["backlinks"]) or "-"))
        if info["missing"]:
            print("  missing    " + ", ".join(info["missing"]))
        return
    print(path)
    if not graph["pages"]:
        print("(no pages)")
        return
    for slug, info in sorted(graph["pages"].items()):
        outs = ", ".join(info["links"]) or "-"
        backs = ", ".join(info["backlinks"]) or "-"
        print(f"{slug}  -> {outs}  <- {backs}")


def show_lint() -> None:
    library = library_path(load_config())
    path = write_graph(library)
    graph = json.loads(path.read_text(encoding="utf-8"))
    missing: list[str] = []
    orphans: list[str] = []
    for slug, info in sorted(graph["pages"].items()):
        for name in info["missing"]:
            missing.append(f"{slug} -> {name}")
        if slug not in ("index", "log") and not info["backlinks"]:
            orphans.append(slug)
    print(path)
    if not missing and not orphans:
        print("clean")
        return
    for line in missing:
        print(f"missing  {line}")
    for slug in orphans:
        print(f"orphan   {slug}")


def page_file(slug: str, library: Path) -> Path:
    raw = strip_suffix(slug.strip())
    rel = Path(raw)
    if not raw or rel.is_absolute() or ".." in rel.parts or any(part.startswith(".") for part in rel.parts):
        raise ValueError(f"bad page {slug}")
    base = library / rel
    path = base.with_name(rel.name + ".md")  # with_suffix would turn v1.2 into v1.md
    for suffix in (".md", ".markdown"):  # an existing notes.markdown is the page notes
        found = base.with_name(rel.name + suffix)
        if found.is_file():
            path = found
            break
    path = path.resolve()
    path.relative_to(library.resolve())
    return path


def catalog(cfg: dict[str, str] | None = None) -> list[dict]:
    cfg = cfg or load_config()
    graph = build_graph(library_path(cfg))
    return [
        {
            "slug": slug,
            "title": info["title"],
            "path": info["path"],
            "links": info["links"],
            "backlinks": info["backlinks"],
        }
        for slug, info in sorted(graph["pages"].items())
    ]


def read_page(slug: str, cfg: dict[str, str] | None = None) -> str:
    cfg = cfg or load_config()
    path = page_file(slug, library_path(cfg))
    if not path.is_file():
        raise FileNotFoundError(slug)
    return path.read_text(encoding="utf-8")


def write_page(slug: str, text: str, cfg: dict[str, str] | None = None) -> dict[str, str]:
    cfg = cfg or load_config()
    library = library_path(cfg)
    path = page_file(slug, library)
    path.parent.mkdir(parents=True, exist_ok=True)
    if text and not text.endswith("\n"):
        text += "\n"
    atomic_write(path, text.encode("utf-8"))
    write_graph(library)
    return {"slug": page_slug(path, library), "path": str(path)}


def search_pages(query: str, cfg: dict[str, str] | None = None, limit: int = 40) -> list[dict]:
    cfg = cfg or load_config()
    needle = query.lower().strip()
    hits: list[dict] = []
    if not needle:
        return hits
    library = library_path(cfg)
    for path in sorted(list_pages(library).values()):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        slug = page_slug(path, library)
        for number, line in enumerate(lines, 1):
            if needle not in line.lower():
                continue
            hits.append({"slug": slug, "line": number, "text": line.strip()})
            if len(hits) >= limit:
                return hits
    return hits


def show_pages(args: list[str]) -> None:
    data = catalog()
    if "--json" in args:
        print(json.dumps(data, indent=2))
        return
    if not data:
        print("(no pages)")
        return
    for item in data:
        print(f"{item['slug']}  {item['title']}")


def show_read(args: list[str]) -> None:
    if not args:
        print("grimoire: read needs a page", file=sys.stderr)
        raise SystemExit(1)
    try:
        print(read_page(args[0]), end="")
    except (FileNotFoundError, ValueError):
        print(f"grimoire: no page {args[0]}", file=sys.stderr)
        raise SystemExit(1)


def show_write(args: list[str]) -> None:
    if not args:
        print("grimoire: write needs a page", file=sys.stderr)
        raise SystemExit(1)
    try:
        print(json.dumps(write_page(args[0], sys.stdin.read())))
    except ValueError:
        print(f"grimoire: bad page name {args[0]}", file=sys.stderr)
        raise SystemExit(1)


def show_search(args: list[str]) -> None:
    query = " ".join(arg for arg in args if arg != "--json")
    hits = search_pages(query)
    if "--json" in args:
        print(json.dumps(hits, indent=2))
        return
    if not hits:
        print("(none)")
        return
    for hit in hits:
        print(f"{hit['slug']}:{hit['line']}  {hit['text']}")


MCP_TOOLS = [
    {"name": "pages", "description": "List Grimoire pages with links and backlinks.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "read", "description": "Read one page by slug. Does not open the reader.", "inputSchema": {"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]}},
    {"name": "write", "description": "Write a markdown page and refresh the graph.", "inputSchema": {"type": "object", "properties": {"slug": {"type": "string"}, "text": {"type": "string"}}, "required": ["slug", "text"]}},
    {"name": "search", "description": "Search page text. Returns slug, line, and the line.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "graph", "description": "Rebuild and return the page graph.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "lint", "description": "Report orphan pages and links that point nowhere.", "inputSchema": {"type": "object", "properties": {}}},
]


def mcp_result(name: str, arguments: dict) -> object:
    if name == "pages":
        return catalog()
    if name == "read":
        slug = str(arguments.get("slug", ""))
        return {"slug": slug, "text": read_page(slug)}
    if name == "write":
        return write_page(str(arguments.get("slug", "")), str(arguments.get("text", "")))
    if name == "search":
        return search_pages(str(arguments.get("query", "")))
    if name == "graph":
        library = library_path(load_config())
        path = write_graph(library)
        return json.loads(path.read_text(encoding="utf-8"))
    if name == "lint":
        library = library_path(load_config())
        graph = build_graph(library)
        missing = [f"{slug} -> {name}" for slug, info in graph["pages"].items() for name in info["missing"]]
        orphans = [slug for slug, info in graph["pages"].items() if slug not in ("index", "log") and not info["backlinks"]]
        return {"missing": missing, "orphans": orphans}
    raise ValueError(f"unknown tool {name}")


def serve_mcp() -> None:
    """Newline-delimited JSON-RPC. Logs stay on stderr so they do not break the stream."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict) or "id" not in msg:
            continue
        method = msg.get("method")
        mid = msg["id"]
        params = msg.get("params")
        if not isinstance(params, dict):
            params = {}
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "grimoire", "version": __version__},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": MCP_TOOLS}
        elif method == "tools/call":
            try:
                payload = mcp_result(str(params.get("name")), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}
            except Exception as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        else:
            reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
        sys.stdout.flush()


def free_keys(fd: int) -> None:
    """Hand the app the keys the terminal driver would keep for itself.

    ctrl+s and ctrl+q stop pausing and resuming output. ctrl+z (undo) and, on macOS,
    ctrl+y (redo) stop being the suspend keys: the driver swallows them otherwise,
    even with the signal ignored.
    """
    attrs = termios.tcgetattr(fd)
    attrs[0] &= ~termios.IXON
    try:
        off = os.fpathconf(fd, "PC_VDISABLE")
    except (OSError, ValueError):
        off = 0
    for name in ("VSUSP", "VDSUSP"):
        index = getattr(termios, name, None)
        if index is not None and index < len(attrs[6]):
            attrs[6][index] = bytes([off])
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def no_flow_control() -> None:
    """endwin puts the shell's settings back."""
    if termios is None:
        return
    try:
        free_keys(sys.stdin.fileno())
        curses.def_prog_mode()  # coming back from a suspend keeps them
    except (termios.error, curses.error, OSError, ValueError):
        pass


TERM_FALLBACKS = ("xterm-256color", "xterm", "vt100")


def pick_term(setup=None, env=None) -> str | None:
    """A TERM this machine has an entry for. None when nothing works.

    Inside tmux, or over ssh from a newer terminal, TERM can name an entry the
    machine does not have. Close enough beats a traceback.
    """
    env = os.environ if env is None else env
    if setup is None:
        setup = lambda name: curses.setupterm(term=name, fd=sys.__stdout__.fileno())
    first = env.get("TERM", "")
    for name in (first, *TERM_FALLBACKS):
        if not name:
            continue
        try:
            setup(name)
        except curses.error:
            continue
        env["TERM"] = name
        return name
    return None


def main(stdscr: curses.window, app: App) -> None:
    no_flow_control()
    curses.curs_set(0)
    init_colors()
    stdscr.keypad(True)
    pending = True
    try:
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        except curses.error:
            pass
        while True:
            busy = app.mode == "graph" and app.gstate is not None and not app.gstate.get("settled", True)
            stdscr.timeout(33 if busy else 400 if app.mode == "list" else -1)
            if pending:
                draw(stdscr, app)
                pending = False
            key = read_key(stdscr)
            if key == -1:
                if busy:
                    app.step_graph()
                    pending = True
                    continue
                pending = app.poll_shelf()
                continue
            if not app.handle(key):
                return
            pending = True
    except KeyboardInterrupt:
        return


def utf8_pipes() -> None:
    """Windows pipes default to a legacy code page and CRLF. Pages and the MCP stream are UTF-8 with plain newlines."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", newline="\n" if stream is not sys.stdin else None)
        except (AttributeError, OSError, ValueError):
            pass


def run(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if os.name == "nt" and argv is None:
        utf8_pipes()
    if args and args[0] in ("help", "--help", "-h"):
        print(USAGE, end="")
        return
    if args and args[0] in ("--version", "-V"):
        print(f"grimoire {__version__}")
        return
    if args and args[0] == "plugins":
        host = load_plugins()
        folder = plugins_dir()
        print(folder)
        if host.loaded:
            print("\n".join(host.loaded))
        elif not host.errors:
            print("(none)")
        return
    if args and args[0] == "images":
        print(image_status())
        return
    if args and args[0] == "theme":
        path = theme_path()
        if not path.is_file():
            write_default_theme(path)
        print(path)
        print(path.read_text(encoding="utf-8"), end="")
        print("colors: black red green yellow blue magenta cyan white default")
        print("a number 0-255 also works, if the terminal has that many")
        print("the background stays the terminal's")
        return
    if args and args[0] == "graph":
        show_graph(args[1:])
        return
    if args and args[0] == "keys":
        path = keys_path()
        if not path.is_file():
            write_default_keys(path)
        print(path)
        return
    if args and args[0] == "lint":
        show_lint()
        return
    if args and args[0] == "pages":
        show_pages(args[1:])
        return
    if args and args[0] == "read":
        show_read(args[1:])
        return
    if args and args[0] == "write":
        show_write(args[1:])
        return
    if args and args[0] == "search":
        show_search(args[1:])
        return
    if args and args[0] == "mcp":
        serve_mcp()
        return
    if args and args[0] == "setup":
        setup_config(args[1:])
        return
    suspend = getattr(signal, "SIGTSTP", None)  # Windows has no suspend key
    if suspend is not None:
        previous = signal.getsignal(suspend)
        signal.signal(suspend, signal.SIG_IGN)
    try:
        cfg = load_config()
        plugins = load_plugins()
        root, open_file = resolve_launch(args, cfg, Path.cwd())
        if os.name != "nt" and pick_term() is None:
            print(f"grimoire: no terminal entry for TERM={os.environ.get('TERM', '')}. Try TERM=xterm-256color", file=sys.stderr)
            raise SystemExit(1)
        app = App(root, open_file, name=cfg["name"])
        app.plugins = plugins
        if open_file is not None:
            app.renderer = pick_renderer(open_file, app.lines, plugins)
            app.delimiter = table_delimiter(open_file, plugins)
            plugins.emit("open", app)
        curses.wrapper(lambda stdscr: main(stdscr, app))
    finally:
        if suspend is not None:
            signal.signal(suspend, previous)


if __name__ == "__main__":
    run()
