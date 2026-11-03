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

import curses
import importlib.util
import json
import locale
import os
import signal
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

locale.setlocale(locale.LC_ALL, "")
os.environ.setdefault("ESCDELAY", "25")

TEXT_EXT = {
    ".md", ".markdown", ".txt", ".sh", ".bash", ".zsh", ".fish",
    ".py", ".lua", ".js", ".ts", ".mjs", ".cjs", ".rb", ".go",
    ".rs", ".toml", ".yaml", ".yml", ".json", ".css", ".html",
    ".conf", ".ini", ".service", ".desktop",
}
MARKDOWN_EXT = {".md", ".markdown"}
SKIP_DIRS = {"__pycache__", "node_modules", "dist", "build", ".venv", "venv"}
MAX_LINES = 8000
CARD_W = 60

# Styles the painter understands.
TITLE = "title"
HEADING = "heading"
BOLD = "bold"
ITALIC = "italic"
CODE = "code"
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
    return {"name": "GRIMOIRE", "open": "cwd", "library": "~/scripts"}


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


def save_config(cfg: dict[str, str]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
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
    key = name.lower().strip()
    if key in ("esc", "escape"):
        return 27
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
    if len(key) == 1:
        return ord(key)
    raise ValueError(f"unknown key {name}")


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
        while nxt < n and text[nxt] not in "_*`":
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
) -> list[tuple[int, int, list[tuple[str, str]]]]:
    """Screen rows. The cursor line starts at the top, then stays centered."""
    if height < 1 or not lines:
        return []
    row = max(0, min(row, len(lines) - 1))
    flags = fences if fences is not None else fence_flags(lines)
    focus_y = focus_anchor(row, len(lines), height)
    inner = max(1, width - margin)
    painted: list[tuple[int, int, list[tuple[str, str]]]] = []
    for screen_y in range(height):
        line_i = row - focus_y + screen_y
        if line_i < 0 or line_i >= len(lines):
            continue
        if line_i == row:
            visible, cursor = slice_focus(lines[line_i], col, inner)
            spans = focus_spans(visible, cursor)
        else:
            spans = fit(
                render_unfocused(
                    lines[line_i],
                    markdown=markdown,
                    in_fence=flags[line_i] if line_i < len(flags) else False,
                    renderer=renderer,
                ),
                inner,
            )
        if spans:
            painted.append((screen_y, margin, spans))
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


def help_lines(name: str) -> tuple[str, ...]:
    return (
        name,
        "the book that will not stay shut",
        "",
        "List",
        "  up down j k     move",
        "  enter right     open",
        "  left esc        back, or quit at the top",
        "  q               quit",
        "  ?               this help",
        "",
        "Reader, normal",
        "  h j k l         move, like vim",
        "  ctrl+i  i       insert at the cursor",
        "  ctrl+a  a       append, then type",
        "  esc  q          back, asks if unsaved",
        "",
        "Reader, insert",
        "  type            edit the raw line",
        "  enter           new line",
        "  backspace       delete",
        "  esc             back to normal, not out",
        "",
        "Either mode",
        "  arrows          move",
        "  home end        ends of the line",
        "  pgup pgdn       a page",
        "  F2              save",
        "  ctrl+z          undo    ctrl+y redo",
        "  ctrl+f  /       find, then ctrl+n and ctrl+p",
        "  F5              reload from disk",
        "  ctrl+c          quit",
        "  F1              help",
        "",
        "The line you are on is the source.",
        "Other lines of a .md file hide #  **  and _ markers.",
        "Other scripts stay plain text.",
        "",
        "grimoire setup    where a bare launch opens",
        "plugins           ~/.grimoire/plugins",
        "",
        "esc closes help",
    )


class App:
    def __init__(self, root: Path, open_file: Path | None = None, name: str = "GRIMOIRE") -> None:
        self.name = name
        self.root = root.resolve()
        self.cwd = self.root
        self.entries: list[Entry] = []
        self.selected = 0
        self.mode = "list"
        self.help_from = "list"
        self.lines = [""]
        self.path: Path | None = None
        self.markdown = False
        self.fences: list[bool] = [False]
        self.row = 0
        self.col = 0
        self.pref_col = 0
        self.query = ""
        self.note = ""
        self.message = ""
        self.dirty = False
        self.inserting = False
        self.plugins: PluginHost | None = None
        self.renderer = None
        self.undo: list[tuple[list[str], int, int]] = []
        self.redo: list[tuple[list[str], int, int]] = []
        self._coalesce: str | None = None
        if open_file is not None:
            self.open_path(open_file)
        else:
            self.refresh()

    def refresh(self) -> None:
        self.entries = scan(self.cwd)
        if self.selected >= len(self.entries):
            self.selected = max(0, len(self.entries) - 1)

    def open_path(self, path: Path) -> None:
        self.lines, self.note = read_lines(path)
        self.fences = fence_flags(self.lines)
        self.path = path
        self.markdown = path.suffix.lower() in MARKDOWN_EXT
        self.renderer = self.plugins.renderer_for(path) if self.plugins else None
        self.row = 0
        self.col = 0
        self.pref_col = 0
        self.mode = "read"
        self.inserting = False
        self.dirty = False
        self.undo.clear()
        self.redo.clear()
        self._coalesce = None
        self.message = self.note
        if self.plugins:
            self.plugins.emit("open", self)

    def clamp(self) -> None:
        if not self.lines:
            self.lines = [""]
            self.fences = [False]
        self.row = max(0, min(self.row, len(self.lines) - 1))
        self.col = max(0, min(self.col, len(self.lines[self.row])))

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
        text = "\n".join(self.lines)
        if text and not text.endswith("\n"):
            text += "\n"
        try:
            self.path.write_text(text, encoding="utf-8")
        except OSError as exc:
            self.message = f"save failed: {exc}"
            return False
        self.dirty = False
        self.message = "saved"
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

    def leave_reader(self) -> bool:
        if self.plugins:
            self.plugins.emit("leave", self)
        self.mode = "list"
        self.dirty = False
        self.message = ""
        self.refresh()
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
        if not self.entries:
            return
        entry = self.entries[self.selected]
        if entry.kind == "folder":
            self.cwd = entry.path
            self.selected = 0
            self.refresh()
            return
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
        """Return False to quit."""
        if key == curses.KEY_RESIZE:
            return True
        if key == 3:
            return False
        if self.mode == "help":
            if key in (27, ord("q"), ord("?"), curses.KEY_F1):
                self.mode = self.help_from
            return True
        if self.mode == "search":
            return self.handle_search(key)
        if self.mode == "confirm":
            return self.handle_confirm(key)
        if self.mode == "list":
            return self.handle_list(key)
        return self.handle_read(key)

    def handle_list(self, key: int | str) -> bool:
        if key in (ord("q"),):
            return False
        if key in (27, curses.KEY_LEFT, ord("h")):
            return self.go_up()
        if key in (curses.KEY_UP, ord("k")):
            self.selected = max(0, self.selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.selected = min(max(0, len(self.entries) - 1), self.selected + 1)
        elif key in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT, ord("l")):
            self.open_selected()
        elif key in (ord("?"), curses.KEY_F1):
            self.help_from = "list"
            self.mode = "help"
        elif self.plugins:
            hit = self.plugins.dispatch(self, "list", key)
            if hit is not None:
                return hit
        return True

    def begin_insert(self, *, append: bool) -> None:
        if append:
            self.col = len(self.lines[self.row])
            self.pref_col = self.col
        self.inserting = True
        self._coalesce = None
        self.message = ""

    def handle_read(self, key: int | str) -> bool:
        page = 20
        if key in (curses.KEY_F2, 19):
            self.save()
            return True
        if key == curses.KEY_F5:
            self.reload()
            return True
        if key in (curses.KEY_F1,):
            self.help_from = "read"
            self.mode = "help"
            return True
        if key in (6, ord("/")) and not self.inserting:
            self.query = ""
            self.mode = "search"
            return True
        if key == 14 and self.query:
            self.jump_match(1)
            return True
        if key == 16 and self.query:
            self.jump_match(-1)
            return True
        if key == 26:
            self.undo_edit()
            return True
        if key == 25:
            self.redo_edit()
            return True
        if key == curses.KEY_UP or (key == ord("k") and not self.inserting):
            self.move_row(-1)
        elif key == curses.KEY_DOWN or (key == ord("j") and not self.inserting):
            self.move_row(1)
        elif key == curses.KEY_LEFT or (key == ord("h") and not self.inserting):
            self.move_col(-1)
        elif key == curses.KEY_RIGHT or (key == ord("l") and not self.inserting):
            self.move_col(1)
        elif key == curses.KEY_HOME or (key == ord("0") and not self.inserting):
            self.col = 0
            self.pref_col = 0
            self._coalesce = None
        elif key == curses.KEY_END or (key == ord("$") and not self.inserting):
            self.col = len(self.lines[self.row])
            self.pref_col = self.col
            self._coalesce = None
        elif key == curses.KEY_PPAGE:
            self.move_row(-page)
        elif key == curses.KEY_NPAGE:
            self.move_row(page)
        elif not self.inserting:
            return self.handle_normal(key)
        elif key == 27:
            self.inserting = False
            self._coalesce = None
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self.erase_back()
        elif key == curses.KEY_DC:
            self.erase_forward()
        elif key in (10, 13, curses.KEY_ENTER):
            self.break_line()
        elif key == 9:
            self.insert_text("  ")
        elif self.plugins:
            hit = self.plugins.dispatch(self, "insert", key)
            if hit is not None:
                return hit
        elif isinstance(key, int) and 32 <= key < 127:
            self.insert_text(chr(key))
        return True

    def handle_normal(self, key: int | str) -> bool:
        if key == 27 or key == ord("q"):
            if self.dirty:
                self.mode = "confirm"
                self.message = "leave"
                return True
            return self.leave_reader() if key == 27 else False
        # ctrl+i is tab on a normal terminal. ctrl+a is its own key.
        if key in (9, ord("i")):
            self.begin_insert(append=False)
            return True
        if key in (1, ord("a"), ord("A")):
            self.begin_insert(append=True)
            return True
        if key == ord("G"):
            self.row = len(self.lines) - 1
            self.col = 0
            self.pref_col = 0
        elif key == ord("g"):
            self.row = 0
            self.col = 0
            self.pref_col = 0
        elif self.plugins:
            hit = self.plugins.dispatch(self, "normal", key)
            if hit is not None:
                return hit
        return True

    def handle_confirm(self, key: int | str) -> bool:
        if key in (ord("s"), ord("y"), ord("S"), ord("Y")):
            if self.message == "reload":
                row = self.row
                self.open_path(self.path) if self.path else None
                self.row = min(row, len(self.lines) - 1)
                self.mode = "read"
                self.message = "reloaded"
                return True
            if self.save():
                return self.leave_reader()
            self.mode = "read"
            return True
        if key in (ord("d"), ord("n"), ord("D"), ord("N")):
            if self.message == "reload":
                self.mode = "read"
                self.message = ""
                return True
            return self.leave_reader()
        if key == 27:
            self.mode = "read"
            self.message = ""
            return True
        return True

    def handle_search(self, key: int | str) -> bool:
        if key in (27,):
            self.mode = "read"
            return True
        if key in (curses.KEY_ENTER, 10, 13):
            self.mode = "read"
            if self.query:
                self.jump_match(1)
            return True
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.query = self.query[:-1]
            return True
        if isinstance(key, int) and 32 <= key < 127:
            self.query += chr(key)
        return True


def style_attr(style: str) -> int:
    italic = getattr(curses, "A_ITALIC", 0)
    table = {
        TITLE: curses.color_pair(1) | curses.A_BOLD,
        HEADING: curses.color_pair(1) | curses.A_BOLD,
        BOLD: curses.color_pair(1) | curses.A_BOLD,
        ITALIC: curses.color_pair(2) | italic,
        CODE: curses.color_pair(3),
        FOLDER: curses.color_pair(4),
        SELECTED: curses.A_BOLD,
        DIM: curses.A_DIM,
        FOCUS: curses.A_BOLD,
        CURSOR: curses.A_REVERSE | curses.A_BOLD,
        FOOTER: curses.A_DIM,
        TEXT: curses.A_DIM,
    }
    return table.get(style, curses.A_DIM)


def init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_BLUE, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_CYAN, -1)
    curses.init_pair(4, curses.COLOR_MAGENTA, -1)


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


def footer_line(left: str, right: str, width: int) -> str:
    if width <= 0:
        return ""
    if not right or len(left) + len(right) + 2 > width:
        return left[:width]
    gap = width - len(left) - len(right)
    return left + (" " * gap) + right


def draw(stdscr: curses.window, app: App) -> None:
    stdscr.erase()
    rows, cols = stdscr.getmaxyx()
    if rows < 8 or cols < 30:
        put(stdscr, 0, 0, "terminal too small", style_attr(FOOTER), cols, rows)
        stdscr.refresh()
        return
    if app.mode == "list":
        draw_list(stdscr, app, rows, cols)
    else:
        draw_read(stdscr, app, rows, cols)
    if app.mode == "help":
        draw_help(stdscr, rows, cols, help_lines(app.name))
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


def draw_list(stdscr: curses.window, app: App, rows: int, cols: int) -> None:
    name, suffix = title_parts(app.title())
    art = banner(name)
    if art and len(art[0]) < cols - 2 and rows >= 16:
        left = max(0, (cols - 1 - len(art[0])) // 2)
        for i, line in enumerate(art):
            put(stdscr, i, left, line, style_attr(TITLE), cols, rows)
        if suffix:
            label = "› " + suffix
            put(
                stdscr,
                len(art),
                max(0, (cols - 1 - len(label)) // 2),
                label,
                style_attr(DIM),
                cols,
                rows,
            )
        header = len(art) + 1
    else:
        title = app.title()
        put(stdscr, 0, max(0, (cols - len(title)) // 2), title, style_attr(TITLE), cols, rows)
        header = 2
    content_h = rows - 1 - header
    painted = list_layout(app.entries, app.selected, content_h, cols)
    paint_spans(stdscr, painted, header, cols, rows)
    if not app.entries:
        empty = "empty"
        put(stdscr, rows // 2, max(0, (cols - len(empty)) // 2), empty, style_attr(DIM), cols, rows)
    left = "↑↓ move · enter open · ← back · q quit · ? help"
    put(stdscr, rows - 1, 0, footer_line(left, "", cols - 1), style_attr(FOOTER), cols, rows)


def draw_read(stdscr: curses.window, app: App, rows: int, cols: int) -> None:
    content_h = rows - 1
    painted = doc_layout(
        app.lines,
        app.row,
        app.col,
        content_h,
        cols - 1,
        markdown=app.markdown,
        fences=app.fences,
        renderer=app.renderer,
    )
    paint_spans(stdscr, painted, 0, cols, rows)
    if app.mode == "confirm":
        if app.message == "reload":
            left = "reload discards edits   s reload   d stay   esc stay"
        else:
            left = "unsaved   s save   d discard   esc stay"
    elif app.mode == "search":
        left = "/" + app.query
    elif app.inserting:
        left = "insert · esc normal · F2 save"
    else:
        left = "normal · ctrl+i insert · ctrl+a append · esc back"
        if app.message:
            left = app.message + "  ·  " + left
        elif app.dirty:
            left = "unsaved  ·  " + left
    name = app.path.name if app.path else ""
    right = f"{name}  {app.row + 1}/{len(app.lines)}"
    put(stdscr, rows - 1, 0, footer_line(left, right, cols - 1), style_attr(FOOTER), cols, rows)


def draw_help(stdscr: curses.window, rows: int, cols: int, lines: tuple[str, ...]) -> None:
    stdscr.erase()
    width = min(62, cols - 4)
    height = min(len(lines) + 2, rows - 2)
    top = max(0, (rows - height) // 2)
    left = max(0, (cols - width) // 2)
    for y in range(height):
        put(stdscr, top + y, left, " " * (width - 1), curses.A_NORMAL, cols, rows)
    put(stdscr, top, left, "╭" + "─" * (width - 3) + "╮", style_attr(DIM), cols, rows)
    put(stdscr, top + height - 1, left, "╰" + "─" * (width - 3) + "╯", style_attr(DIM), cols, rows)
    for i, line in enumerate(lines):
        y = top + 1 + i
        if y >= top + height - 1:
            break
        put(stdscr, y, left, "│", style_attr(DIM), cols, rows)
        put(stdscr, y, left + width - 2, "│", style_attr(DIM), cols, rows)
        put(stdscr, y, left + 2, line[: width - 4], style_attr(TEXT if i else TITLE), cols, rows)


USAGE = """\
grimoire                  open the folder you are in, or whatever setup saved
grimoire .                this folder
grimoire library          the shelf in config
grimoire <path>           that file or folder
grimoire setup            choose where a bare launch opens
grimoire plugins          list plugins in ~/.grimoire/plugins

Config: ~/.grimoire/config.json
  name      text on the title row (default GRIMOIRE)
  open      cwd, library, or a path
  library   shelf used by `grimoire library` (default ~/scripts)
"""


def resolve_launch(argv: list[str], cfg: dict[str, str], cwd: Path) -> tuple[Path, Path | None]:
    if not argv:
        mode = cfg["open"]
        if mode == "cwd":
            return cwd.resolve(), None
        if mode == "library":
            return library_path(cfg), None
        return _path_target(mode, cwd)
    if argv[0] in ("library", "--library"):
        return library_path(cfg), None
    if argv[0] == ".":
        return cwd.resolve(), None
    return _path_target(argv[0], cwd)


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


def main(stdscr: curses.window, app: App) -> None:
    curses.curs_set(0)
    init_colors()
    stdscr.keypad(True)
    try:
        while True:
            draw(stdscr, app)
            key = stdscr.getch()
            if not app.handle(key):
                return
    except KeyboardInterrupt:
        return


def run(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("help", "--help", "-h"):
        print(USAGE, end="")
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
    if args and args[0] == "setup":
        setup_config(args[1:])
        return
    previous = signal.getsignal(signal.SIGTSTP)
    signal.signal(signal.SIGTSTP, signal.SIG_IGN)
    try:
        cfg = load_config()
        plugins = load_plugins()
        root, open_file = resolve_launch(args, cfg, Path.cwd())
        app = App(root, open_file, name=cfg["name"])
        app.plugins = plugins
        if open_file is not None:
            app.renderer = plugins.renderer_for(open_file)
            plugins.emit("open", app)
        curses.wrapper(lambda stdscr: main(stdscr, app))
    finally:
        signal.signal(signal.SIGTSTP, previous)


if __name__ == "__main__":
    run()
