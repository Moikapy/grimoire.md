#!/usr/bin/env python3
"""Renderer tests. No tty required."""

import tempfile
import unittest
import os
import json
from pathlib import Path

import grimoire
from grimoire import (
    App,
    Entry,
    doc_layout,
    fence_flags,
    find_line,
    fit,
    list_layout,
    load_config,
    parse_inline,
    render_unfocused,
    resolve_launch,
    scan,
    setup_config,
    slice_focus,
    BOLD,
    HEADING,
    ITALIC,
    TEXT,
)


class ParseTests(unittest.TestCase):
    def test_video_bold_line_hides_markers(self):
        spans = render_unfocused(
            "hello **my name is simone**, how are you?",
            markdown=True,
            in_fence=False,
        )
        self.assertEqual(
            spans,
            [
                ("hello ", TEXT),
                ("my name is simone", BOLD),
                (", how are you?", TEXT),
            ],
        )
        self.assertNotIn("**", "".join(t for t, _ in spans))

    def test_heading_hides_hashes(self):
        spans = render_unfocused("# NOTE", markdown=True, in_fence=False)
        self.assertEqual(spans, [("NOTE", HEADING)])

    def test_italic_underscores_hidden(self):
        spans = render_unfocused("_hello_", markdown=True, in_fence=False)
        self.assertEqual(spans, [("hello", ITALIC)])

    def test_initial_view_starts_at_top(self):
        lines = ["# NOTE", "b2b molto meglio che b2c", "hello **name**"]
        painted = doc_layout(lines, 0, 1, height=9, width=40, markdown=True)
        top = [row for row in painted if row[0] == 0]
        self.assertEqual(len(top), 1)
        self.assertIn("# NOTE", "".join(t for t, _ in top[0][2]))
        nxt = [row for row in painted if row[0] == 1]
        self.assertIn("b2b", "".join(t for t, _ in nxt[0][2]))

    def test_deep_line_stays_centered(self):
        lines = [f"line {i}" for i in range(40)]
        painted = doc_layout(lines, 20, 0, height=9, width=40, markdown=False)
        mid = [row for row in painted if row[0] == 4]
        self.assertIn("line 20", "".join(t for t, _ in mid[0][2]))

    def test_heading_renders_on_the_line_above(self):
        lines = ["# NOTE", "hello **name**"]
        painted = doc_layout(lines, 1, 0, height=9, width=40, markdown=True)
        above = [spans for y, _, spans in painted if y == 0]
        flat = "".join(t for t, _ in above[0])
        self.assertEqual(flat, "NOTE")
        focus = [spans for y, _, spans in painted if y == 1]
        self.assertIn("hello **name**", "".join(t for t, _ in focus[0]))

    def test_fence_is_not_a_heading(self):
        lines = ["```python", "# not a heading", "```", "# real"]
        flags = fence_flags(lines)
        self.assertEqual(flags, [True, True, True, False])
        spans = render_unfocused("# not a heading", markdown=True, in_fence=True)
        self.assertEqual(spans, [("# not a heading", "code")])
        real = render_unfocused("# real", markdown=True, in_fence=False)
        self.assertEqual(real, [("real", HEADING)])

    def test_plain_script_does_not_render_hashes(self):
        spans = render_unfocused("# comment", markdown=False, in_fence=False)
        self.assertEqual(spans, [("# comment", TEXT)])

    def test_truncation(self):
        spans = fit([("hello world", TEXT)], 6)
        self.assertEqual("".join(t for t, _ in spans), "hello…")

    def test_long_line_wraps_instead_of_cutting(self):
        raw = "alpha beta gamma delta epsilon zeta eta"
        lines = [raw, "next"]
        painted = doc_layout(lines, 1, 0, height=8, width=22, markdown=False)
        rows = {y: "".join(t for t, _ in spans) for y, _, spans in painted}
        self.assertIn("alpha beta", rows[0])
        self.assertNotIn("…", rows[0])
        self.assertTrue(any("zeta" in text or "eta" in text for text in rows.values()))
        focus = [spans for y, _, spans in painted if any(style == "cursor" for _, style in spans)]
        self.assertEqual(len(focus), 1)
        self.assertIn("next", "".join(t for t, _ in focus[0]))

    def test_slice_keeps_cursor_in_window(self):
        vis, cur = slice_focus("abcdefghij", 9, 5)
        self.assertLess(cur, 5)
        self.assertGreaterEqual(cur, 0)
        vis, cur = slice_focus("hello", 5, 10)
        self.assertEqual(vis, "hello")
        self.assertEqual(cur, 5)

    def test_short_list_starts_at_top(self):
        entries = [
            Entry(f"n{i}", "detail", "file", Path(f"/tmp/n{i}"))
            for i in range(5)
        ]
        painted = list_layout(entries, 2, 21, 80)
        names = [(y, "".join(t for t, _ in spans)) for y, _, spans in painted]
        self.assertIn((0, "n0"), names)
        self.assertIn((6, "n2"), names)

    def test_normal_mode_does_not_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.md"
            path.write_text("hello\n", encoding="utf-8")
            app = App(Path(tmp), path)
            self.assertFalse(app.inserting)
            app.handle_read(ord("x"))
            self.assertEqual(app.lines[0], "hello")
            app.handle_read(ord("i"))
            self.assertTrue(app.inserting)
            app.handle_read(ord("!"))
            self.assertEqual(app.lines[0], "!hello")
            app.handle_read(27)
            self.assertFalse(app.inserting)
            self.assertEqual(app.mode, "read")
            app.handle_read(ord("a"))
            self.assertTrue(app.inserting)
            self.assertEqual(app.col, len(app.lines[0]))

    def test_edit_inserts_and_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.md"
            path.write_text("# Hi\n", encoding="utf-8")
            app = App(Path(tmp), path)
            app.col = len(app.lines[0])
            app.insert_text("!")
            self.assertEqual(app.lines[0], "# Hi!")
            self.assertTrue(app.dirty)
            app.break_line()
            app.insert_text("next")
            self.assertTrue(app.save())
            self.assertEqual(path.read_text(encoding="utf-8"), "# Hi!\nnext\n")
            self.assertFalse(app.dirty)
            app.undo_edit()
            self.assertEqual(app.lines[1], "")

    def test_find_wraps(self):
        lines = ["alpha", "beta", "alpha two"]
        self.assertEqual(find_line(lines, "alpha", 0, 1), 2)
        self.assertEqual(find_line(lines, "alpha", 2, 1), 0)

    def test_scan_skips_hidden_and_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.md").write_text("# Hi\n\nwords here\n", encoding="utf-8")
            (root / ".hidden.md").write_text("nope\n", encoding="utf-8")
            (root / "blob.png").write_bytes(b"\x89PNG\0\0")
            (root / "sub").mkdir()
            found = scan(root)
            names = [e.name for e in found]
            self.assertIn("sub", names)
            self.assertIn("ok.md", names)
            self.assertNotIn(".hidden.md", names)
            self.assertNotIn("blob.png", names)
            md = next(e for e in found if e.name == "ok.md")
            self.assertIn("word", md.detail)

    def test_app_back_rescans(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.md").write_text("one\n", encoding="utf-8")
            app = App(root)
            self.assertEqual(app.mode, "list")
            app.selected = 0
            app.open_selected()
            self.assertEqual(app.mode, "read")
            (root / "b.md").write_text("two\n", encoding="utf-8")
            app.handle(27)
            self.assertEqual(app.mode, "list")
            self.assertIn("b.md", [e.name for e in app.entries])

    def test_wiki_link_hides_brackets(self):
        spans = render_unfocused("see [[welcome]] now", markdown=True, in_fence=False)
        self.assertEqual([t for t, _ in spans], ["see ", "welcome", " now"])

    def test_graph_backlinks(self):
        from grimoire import build_graph, write_graph
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp)
            (library / "welcome.md").write_text("# Welcome\n\nSee [[links]].\n", encoding="utf-8")
            (library / "links.md").write_text("# Links\n\nBack to [home](welcome.md).\n", encoding="utf-8")
            graph = build_graph(library)
            self.assertEqual(graph["pages"]["welcome"]["links"], ["links"])
            self.assertEqual(graph["pages"]["welcome"]["backlinks"], ["links"])
            self.assertEqual(graph["pages"]["links"]["links"], ["welcome"])
            path = write_graph(library)
            self.assertTrue(path.name.startswith("."))
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("links", saved["pages"])
        from grimoire import banner
        art = banner("GRIMOIRE")
        self.assertIsNotNone(art)
        self.assertEqual(len(art), 3)
        self.assertGreater(len(art[0]), len("GRIMOIRE"))
        self.assertTrue(all(len(line) == len(art[0]) for line in art))
        with tempfile.TemporaryDirectory() as tmp:
            app = App(Path(tmp), name="GRIMOIRE")
            self.assertEqual(app.title(), "GRIMOIRE")

    def test_plugin_key_and_renderer(self):
        from grimoire import load_plugins
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "mark.py").write_text(
                "def register(api):\n"
                "    api.suffix('.todo')\n"
                "    api.render('.todo', lambda line: [('!', 'bold'), (line, 'text')])\n"
                "    api.key('ctrl+w', lambda app: (setattr(app, 'message', 'plugin'), True)[1])\n",
                encoding="utf-8",
            )
            import grimoire
            saved = set(grimoire.TEXT_EXT)
            try:
                host = load_plugins(folder)
                self.assertEqual(host.loaded, ["mark"])
                self.assertIn(".todo", grimoire.TEXT_EXT)
                path = folder / "note.todo"
                path.write_text("task\n", encoding="utf-8")
                app = App(folder, path)
                app.plugins = host
                app.renderer = host.renderer_for(path)
                app.handle_read(23)
                self.assertEqual(app.message, "plugin")
                painted = doc_layout(
                    ["task", "next"],
                    1,
                    0,
                    height=5,
                    width=40,
                    markdown=False,
                    renderer=app.renderer,
                )
                above = "".join(t for y, _, spans in painted if y == 0 for t, _ in spans)
                self.assertIn("!task", above)
            finally:
                grimoire.TEXT_EXT.clear()
                grimoire.TEXT_EXT.update(saved)

    def test_bare_launch_opens_the_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "here"
            shelf = Path(tmp) / "shelf"
            cwd.mkdir()
            shelf.mkdir()
            cfg = {"name": "GRIMOIRE", "open": "cwd", "library": str(shelf)}
            root, opened = resolve_launch([], cfg, cwd)
            self.assertEqual(root, shelf.resolve())
            self.assertIsNone(opened)
            for flag in ("-c", "--current", "-l", "--local"):
                root, opened = resolve_launch([flag], cfg, cwd)
                self.assertEqual(root, cwd.resolve())
            root, opened = resolve_launch(["library"], cfg, cwd)
            self.assertEqual(root, shelf.resolve())

    def test_setup_writes_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            os.environ["GRIMOIRE_CONFIG"] = str(path)
            try:
                setup_config(["--open", "cwd", "--library", "~/.grimoire/pages", "--name", "GRIMOIRE"])
                saved = load_config()
            finally:
                os.environ.pop("GRIMOIRE_CONFIG", None)
            self.assertEqual(saved["open"], "cwd")
            self.assertEqual(saved["name"], "GRIMOIRE")
            self.assertTrue(path.is_file())

    def test_keys_file_rebinds_quit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.json"
            path.write_text(json.dumps({"list": {"quit": ["x"]}}), encoding="utf-8")
            os.environ["GRIMOIRE_KEYS"] = str(path)
            try:
                app = App(Path(tmp))
                self.assertNotEqual(grimoire.parse_key("G"), grimoire.parse_key("g"))
                self.assertTrue(app.handle(ord("q")))
                self.assertEqual(app.mode, "list")
                self.assertFalse(app.handle(ord("x")))
            finally:
                os.environ.pop("GRIMOIRE_KEYS", None)

    def test_image_line_is_alt_and_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / "note.md"
            picture = root / "pics" / "cat.png"
            picture.parent.mkdir()
            picture.write_bytes(b"png")
            self.assertEqual(grimoire.image_file(note, "pics/cat.png"), picture.resolve())
            self.assertIsNone(grimoire.image_file(note, "https://example.com/a.png"))
            with self.assertRaises(ValueError):
                grimoire.image_file(note, "../../etc/passwd")
            painted = doc_layout(
                ["![a cat](pics/cat.png)", "after"],
                1,
                0,
                height=8,
                width=40,
                markdown=True,
                note=note,
                preview=lambda _path, _width: ["@@@@"],
            )
            texts = ["".join(part for part, _ in spans) for _, _, spans in painted]
            self.assertIn("@@@@", texts)
            self.assertTrue(any(text == "a cat  pics/cat.png" for text in texts))
            focused = doc_layout(
                ["![a cat](pics/cat.png)"],
                0,
                0,
                height=4,
                width=40,
                markdown=True,
                note=note,
                preview=lambda _path, _width: ["@@@@"],
            )
            raw = "".join(part for _, _, spans in focused for part, _ in spans)
            self.assertIn("![", raw)
            self.assertNotIn("@@@@", raw)

    def test_image_tools_prefer_imv_and_say_when_missing(self):
        tools = grimoire.image_tools(which=lambda name: {"imv": "/usr/bin/imv", "xdg-open": "/usr/bin/xdg-open"}.get(name))
        self.assertEqual(tools["open"], "/usr/bin/imv")
        self.assertIsNone(tools["preview"])
        self.assertIn("not installed (chafa)", grimoire.image_status(tools))
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            note.write_text("![a cat](cat.png)\n", encoding="utf-8")
            picture = Path(tmp) / "cat.png"
            picture.write_bytes(b"not really")
            seen = []
            message = grimoire.open_image(
                note,
                "![a cat](cat.png)",
                tools={"preview": None, "open": "/usr/bin/imv"},
                spawn=lambda argv, **_kw: seen.append(argv),
            )
            self.assertEqual(message, "opened cat.png")
            self.assertEqual(seen, [["/usr/bin/imv", str(picture.resolve())]])
            self.assertEqual(
                grimoire.open_image(note, "![a cat](cat.png)", tools={"preview": None, "open": None}),
                "install imv to view images",
            )

    def test_narrow_bar_keeps_a_whole_hint(self):
        text = grimoire.fit_hints(["up k move", "enter open", "n page"], 24, more="?")
        self.assertLessEqual(len(text), 24)
        self.assertTrue(text.endswith("?"))
        self.assertIn("move", text)
        self.assertNotIn("enter", text)
        wide = grimoire.fit_hints(["up k move", "enter open"], 40, more="?")
        self.assertEqual(wide, "up k move · enter open")
        scroll, page = grimoire.help_window(["a", "b", "c", "d"], 2, 9)
        self.assertEqual((scroll, page), (2, ["c", "d"]))
        self.assertIn("Settings", grimoire.help_lines("GRIMOIRE"))
        bar = grimoire.footer_bar(["up k move", "enter open"], 40, more="?")
        self.assertEqual(len(bar), 40)
        self.assertTrue(bar.startswith("up k move"))
        self.assertTrue(bar.endswith("enter open"))
        counted = grimoire.footer_bar(["i insert"], 30, more="", right="1:1")
        self.assertTrue(counted.startswith("i insert"))
        self.assertTrue(counted.endswith("1:1"))
        self.assertEqual(len(counted), 30)

    def test_menu_creates_a_page_and_notices_a_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = grimoire.create_page(root, "War Notes")
            self.assertEqual(page.name, "war-notes.md")
            self.assertTrue(page.read_text(encoding="utf-8").startswith("# War Notes"))
            folder = grimoire.create_folder(root, "Research")
            self.assertTrue(folder.is_dir())
            with self.assertRaises(FileExistsError):
                grimoire.create_page(root, "war notes")
            with self.assertRaises(ValueError):
                grimoire.create_folder(root, "../nope")
            app = grimoire.App(root)
            names = [entry.name for entry in app.entries]
            self.assertIn("war-notes.md", names)
            self.assertFalse(app.poll_shelf())
            (root / "later.md").write_text("# Later\n", encoding="utf-8")
            self.assertTrue(app.poll_shelf())
            self.assertIn("later.md", [entry.name for entry in app.entries])

    def test_shelf_finds_a_file_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "welcome.md").write_text("# Hi\n", encoding="utf-8")
            (root / "notes").mkdir()
            (root / "notes" / "war.md").write_text("# War\n", encoding="utf-8")
            hits = grimoire.file_matches(root, "war")
            self.assertEqual([hit.name for hit in hits], ["notes/war.md"])
            app = grimoire.App(root)
            app.handle_list(ord("/"))
            self.assertEqual(app.mode, "filter")
            for char in "war":
                app.handle(ord(char))
            self.assertEqual(app.listing()[0].path.name, "war.md")
            app.handle(10)
            self.assertEqual(app.path, (root / "notes" / "war.md").resolve())
            self.assertEqual(app.query, "")

    def test_theme_file_overrides_a_color(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "theme.json"
            path.write_text('{"heading": "red", "text": "nope", "extra": "blue"}\n', encoding="utf-8")
            os.environ["GRIMOIRE_THEME"] = str(path)
            try:
                theme = grimoire.load_theme()
            finally:
                os.environ.pop("GRIMOIRE_THEME", None)
        self.assertEqual(theme["heading"], "red")
        self.assertEqual(theme["text"], "default")
        self.assertEqual(theme["italic"], "green")
        self.assertEqual(grimoire.color_index("cyan"), 6)
        self.assertIsNone(grimoire.color_index("default"))
        self.assertEqual(grimoire.color_index("196"), 196)

    def test_reader_status_counts_the_buffer(self):
        status = grimoire.reader_status(["hello there", "x"], 1, 0)
        self.assertEqual(status, "13 ch  3 w  2:1")
        self.assertGreater(grimoire.home_top(24, 4, 2), 0)
        self.assertEqual(grimoire.home_top(10, 4, 20), 0)

    def test_write_page_stays_in_the_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp)
            cfg = {"name": "GRIMOIRE", "open": "library", "library": str(library)}
            saved = grimoire.write_page("note", "# Note\n\nSee the word wrap.\n", cfg)
            self.assertTrue(saved["path"].startswith(str(library)))
            hits = grimoire.search_pages("wrap", cfg)
            self.assertEqual(hits[0]["slug"], "note")
            with self.assertRaises(ValueError):
                grimoire.page_file("../outside", library)


if __name__ == "__main__":
    unittest.main()
