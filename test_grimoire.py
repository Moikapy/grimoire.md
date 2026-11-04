#!/usr/bin/env python3
"""Renderer tests. No tty required."""

import curses
import tempfile
import unittest
import os
import sys
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

    def test_image_is_not_a_page_link(self):
        from grimoire import links_in
        text = "![shot](/home/x/pic.png)\n![rel](img/a.png)\nSee [home](welcome.md) and [[links]].\n"
        self.assertEqual(links_in(text), ["links", "welcome"])

    def test_linked_image_keeps_outer_link(self):
        from grimoire import links_in
        self.assertEqual(links_in("[![badge](img/b.png)](setup.md)"), ["setup"])

    def test_escaped_bang_is_still_a_link(self):
        from grimoire import links_in
        self.assertEqual(links_in("Note\\![see this](guide.md)"), ["guide"])

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

    def _csv_app(self, tmp, lines):
        path = Path(tmp) / "speed.csv"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return App(Path(tmp), path)

    @staticmethod
    def _text(painted):
        return {y: "".join(t for t, _ in spans) for y, _, spans in painted}

    def test_csv_grid_aligns_and_right_aligns_numbers(self):
        from grimoire import table_layout
        lines = ["Date,Upload,Server", "11/04/2026,851.31,Spectrum", '06/01/2026,"62.9",Spectrum']
        with tempfile.TemporaryDirectory() as tmp:
            app = self._csv_app(tmp, lines)
            self.assertEqual(app.delimiter, ",")
            self.assertEqual(app.row, 1)  # starts on the first data row, not the header
            painted = table_layout(app.table(), app.row, app.tcol, 6, 80)
            rows = self._text(painted)
            self.assertEqual(rows[0], "Date       │ Upload │ Server  ")
            self.assertEqual(rows[1], "11/04/2026 │ 851.31 │ Spectrum")
            self.assertEqual(rows[2], "06/01/2026 │   62.9 │ Spectrum")
            header_styles = {s for _, s in painted[0][2] if s != grimoire.DIM}
            self.assertEqual(header_styles, {HEADING})

    def test_csv_scrolls_sideways_with_frozen_first_column(self):
        from grimoire import table_layout
        head = "Date," + ",".join(f"Col{i}" for i in range(1, 13))
        body = "11/04/2026," + ",".join(f"v{i:03d}" for i in range(1, 13))
        with tempfile.TemporaryDirectory() as tmp:
            app = self._csv_app(tmp, [head, body, body])
            for _ in range(11):
                app.handle_read(curses.KEY_RIGHT)
            self.assertEqual(app.tcol, 11)
            rows = self._text(table_layout(app.table(), app.row, app.tcol, 6, 40))
            self.assertTrue(rows[0].startswith("Date "))
            self.assertIn("Col11", rows[0])
            self.assertNotIn("Col1 ", rows[0])
            self.assertIn(" ‹ ", rows[0])  # hidden columns on the left
            self.assertTrue(rows[1].startswith("11/04/2026"))
            for y in rows:
                self.assertLessEqual(len(rows[y]), 40)
            app.handle_read(curses.KEY_DOWN)
            self.assertEqual(app.row, 2)

    def test_csv_record_view_and_edit_lands_on_cell(self):
        from grimoire import record_layout
        lines = ["Date,Upload,Server", "11/04/2026,851.31,Spectrum", "06/01/2026,162.99,Spectrum"]
        with tempfile.TemporaryDirectory() as tmp:
            app = self._csv_app(tmp, lines)
            app.handle_read(ord("v"))
            self.assertTrue(app.record)
            app.handle_read(curses.KEY_DOWN)  # next field in record view
            self.assertEqual((app.row, app.tcol), (1, 1))
            app.handle_read(curses.KEY_RIGHT)  # next record
            self.assertEqual(app.row, 2)
            rows = self._text(record_layout(app.table(), app.row, app.tcol, 10, 60))
            self.assertEqual(rows[0], "record 2 of 2")
            self.assertEqual(rows[2], "Upload  162.99")
            app.handle_read(ord("i"))
            self.assertTrue(app.inserting)
            self.assertEqual(app.col, len("06/01/2026,"))
            self.assertFalse(app.table_view())  # editing shows the raw line

    def test_replace_cell_keeps_every_other_byte(self):
        from grimoire import replace_cell
        raw = '11/04/2026,12:36 PM,"a,b",851.31,'
        self.assertEqual(replace_cell(raw, ",", 3, "900.00"), '11/04/2026,12:36 PM,"a,b",900.00,')
        self.assertEqual(replace_cell(raw, ",", 2, "c,d"), '11/04/2026,12:36 PM,"c,d",851.31,')
        self.assertEqual(replace_cell(raw, ",", 0, 'say "hi"'), '"say ""hi""",12:36 PM,"a,b",851.31,')
        self.assertEqual(replace_cell("a,b", ",", 4, "z"), "a,b,,,z")  # short row gets padded
        self.assertEqual(replace_cell("a\tb", "\t", 1, "x\ty"), 'a\t"x\ty"')
        with self.assertRaises(ValueError):
            replace_cell('a,"open', ",", 0, "x")

    def test_cell_edit_commit_cancel_undo(self):
        lines = ["Date,Upload,Server", "11/04/2026,851.31,Spectrum,", "06/01/2026,162.99,Spectrum"]
        with tempfile.TemporaryDirectory() as tmp:
            app = self._csv_app(tmp, lines)
            app.handle_read(curses.KEY_RIGHT)
            app.handle_read(10)  # enter opens the cell
            self.assertEqual((app.mode, app.cell_text), ("cell", "851.31"))
            for _ in range(6):
                app.handle(127)
            for ch in "900":
                app.handle(ord(ch))
            app.handle(27)  # esc changes nothing
            self.assertEqual(app.lines[1], lines[1])
            self.assertFalse(app.dirty)
            app.handle(10)
            for _ in range(6):
                app.handle(127)
            for ch in "1,000":
                app.handle(ord(ch))
            app.handle(10)
            self.assertEqual(app.mode, "read")
            self.assertEqual(app.lines[1], '11/04/2026,"1,000",Spectrum,')
            self.assertEqual(app.lines[2], lines[2])
            self.assertTrue(app.dirty)
            self.assertEqual(app.table().cell(1, 1), "1,000")
            app.handle(26)  # ctrl+z
            self.assertEqual(app.lines, lines)
            app.save()
            self.assertEqual((Path(tmp) / "speed.csv").read_text(encoding="utf-8"), "\n".join(lines) + "\n")

    def test_save_keeps_line_endings(self):
        with tempfile.TemporaryDirectory() as tmp:
            for data in (b"a,b\r\n1,2\r\n", b"a,b\n1,2", b"# hi\n"):
                path = Path(tmp) / "f.csv"
                path.write_bytes(data)
                app = App(Path(tmp), path)
                app.save()
                self.assertEqual(path.read_bytes(), data)

    def test_multiline_cell_refuses_edit(self):
        lines = ["a,b", '1,"starts here', 'ends here",2']
        with tempfile.TemporaryDirectory() as tmp:
            app = self._csv_app(tmp, lines)
            app.handle_read(10)
            self.assertEqual(app.mode, "read")
            self.assertIn("multi-line", app.message)

    def test_plugin_renderer_beats_table_view(self):
        from grimoire import table_delimiter, PluginHost
        host = PluginHost()
        host.renderers[".csv"] = lambda line: [(line, "text")]
        self.assertIsNone(table_delimiter(Path("a.csv"), host))
        self.assertEqual(table_delimiter(Path("a.tsv"), host), "\t")
        self.assertIsNone(table_delimiter(Path("a.md")))

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

    def test_ctrl_q_quits_and_q_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["GRIMOIRE_KEYS"] = str(Path(tmp) / "none.json")
            try:
                app = App(Path(tmp))
                self.assertEqual(grimoire.parse_key("ctrl+q"), 17)
                self.assertTrue(app.handle(ord("q")))
                self.assertFalse(app.handle(17))
            finally:
                os.environ.pop("GRIMOIRE_KEYS", None)

    def test_keys_file_rebinds_quit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.json"
            path.write_text(json.dumps({"list": {"quit": ["x"]}}), encoding="utf-8")
            os.environ["GRIMOIRE_KEYS"] = str(path)
            try:
                app = App(Path(tmp))
                self.assertNotEqual(grimoire.parse_key("G"), grimoire.parse_key("g"))
                self.assertTrue(app.handle(17))  # ctrl+q no longer quits once keys.json rebinds it
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


class GraphTests(unittest.TestCase):
    def setUp(self):
        old = grimoire.THEME_PAIRS.copy()
        self.old_theme = old
        grimoire.THEME_PAIRS.clear()

    def tearDown(self):
        grimoire.THEME_PAIRS.clear()
        grimoire.THEME_PAIRS.update(self.old_theme)

    @staticmethod
    def pages(d: dict) -> dict:
        base = {
            "path": "",
            "title": "",
            "links": [],
            "missing": [],
            "backlinks": [],
        }
        return {slug: {**base, **spec} for slug, spec in d.items()}

    def test_braille_bits_and_rows(self):
        canvas = grimoire.canvas_new()
        grimoire.canvas_plot(canvas, 0, 0)
        grimoire.canvas_plot(canvas, 1, 0)
        grimoire.canvas_plot(canvas, 0, 3)
        grimoire.canvas_plot(canvas, 1, 3)
        self.assertEqual(canvas[(0, 0)], 0x01 | 0x08 | 0x40 | 0x80)
        self.assertEqual(grimoire.braille_char(0x01), "\u2801")
        self.assertEqual(grimoire.braille_char(0x40), "\u2840")
        self.assertEqual(grimoire.braille_char(0), "\u2800")
        line = grimoire.canvas_new()
        grimoire.canvas_line(line, 0, 0, 6, 0)
        rows = grimoire.canvas_rows(line, 5, 1)
        # a horizontal line fills the left and right dot of every cell
        self.assertEqual(rows, ["\u2809\u2809\u2809\u2801"])

    def test_layout_deterministic_and_linked_closer(self):
        data = {
            "pages": self.pages({
                "a": {"links": ["b"], "backlinks": ["b"]},
                "b": {"links": ["a"], "backlinks": ["a"]},
                "c": {"links": [], "backlinks": []},
            })
        }

        def run():
            state = {
                "pos": grimoire.seed_positions(["a", "b", "c"]),
                "vel": {},
                "edges": [("a", "b")],
                "links": {},
            }
            state["vel"] = {slug: (0.0, 0.0) for slug in state["pos"]}
            state["links"] = grimoire.link_map(state["edges"])
            grimoire.settle_layout(state, max_steps=200)
            return {slug: tuple(round(v, 6) for v in xy) for slug, xy in state["pos"].items()}

        first, second = run(), run()
        self.assertEqual(first, second)
        linked = math_dist(first["a"], first["b"])
        loose = math_dist(first["a"], first["c"])
        self.assertLess(linked, loose)

    def test_cone_pick_moves_to_the_right_node(self):
        screen = {
            "center": (10.0, 10.0),
            "above": (11.0, 0.0),
            "below": (9.0, 20.0),
            "far_right": (60.0, 70.0),
            "slant": (30.0, 4.0),
        }
        self.assertEqual(grimoire.cone_pick(screen, "center", (0, -1)), "above")
        # the aligned near node beats a distant one in the same cone
        self.assertEqual(grimoire.cone_pick(screen, "center", (1, 0)), "slant")
        self.assertEqual(grimoire.cone_pick(screen, "center", (0, 1)), "below")
        # no node in that direction: stay
        self.assertEqual(grimoire.cone_pick({"solo": (5.0, 5.0)}, "solo", (0, -1)), "solo")

    def test_label_placement(self):
        items = [
            ("sel", 0.0, 20.0, 40.0, 2.0, "Sel"),
            ("one", 1.0, 40.0, 60.0, 1.5, "One"),
            ("two", 2.0, 24.0, 39.0, 1.0, "Two"),
            ("wide", 0.5, 2.0, 30.0, 1.0, "Very Long Label Here"),
        ]
        placed = grimoire.place_labels(items, 80, 20, {"sel", "one"})
        rects = [(col - 1, row - 1, col + len(text) + 1, row + 1) for _, col, row, text in placed]
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                self.assertFalse(a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1])
        got = {slug for slug, _, _, _ in placed}
        self.assertIn("sel", got)
        self.assertIn("one", got)

    def test_flow_through_the_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "pages"
            library.mkdir()
            (library / "index.md").write_text("# Index\n\nSee [[beta]].\n", encoding="utf-8")
            (library / "beta.md").write_text("# Beta\n\nBack to [[index]].\n", encoding="utf-8")
            cfg_path = root / "cfg.json"
            cfg_path.write_text(json.dumps({"name": "GRIMOIRE", "open": "library", "library": str(library)}), encoding="utf-8")
            os.environ["GRIMOIRE_CONFIG"] = str(cfg_path)
            try:
                app = App(root)
                self.assertEqual(app.mode, "list")
                app.handle(ord("g"))
                self.assertEqual(app.mode, "graph")
                self.assertEqual(app.gsel, "index")
                self.assertTrue(app.handle(10))
                self.assertEqual(app.mode, "read")
                self.assertEqual(app.path.name, "index.md")
                self.assertTrue(app.handle(27))
                self.assertEqual(app.mode, "graph")
                self.assertTrue(app.handle(27))
                self.assertEqual(app.mode, "list")
            finally:
                os.environ.pop("GRIMOIRE_CONFIG", None)


    def test_zoom_and_pan_transforms(self):
        view = {"cx": 0.0, "cy": 0.0, "z": 10.0, "zfit": 10.0}
        dx, dy = grimoire.project(0.5, -0.5, view, 40, 12)
        self.assertEqual((dx, dy), (44.0, 17.0))
        back = grimoire.unproject(dx, dy, view, 40, 12)
        self.assertAlmostEqual(back[0], 0.5)
        self.assertAlmostEqual(back[1], -0.5)
        zoomed = grimoire.zoom_view(view, 2.0, 44.0, 17.0, 40, 12)
        point = grimoire.project(0.5, -0.5, zoomed, 40, 12)
        self.assertEqual(point, (44.0, 17.0))
        panned = grimoire.pan_view(view, 10, 0, 40, 12)
        self.assertAlmostEqual(panned["cx"], -1.0)
        self.assertEqual(view, {"cx": 0.0, "cy": 0.0, "z": 10.0, "zfit": 10.0})

    def test_local_missing_and_filter(self):
        data = {
            "pages": self.pages({
                "a": {"title": "Alpha", "links": ["b", "ghost"], "missing": ["ghost"], "backlinks": []},
                "b": {"title": "Beta", "links": ["a"], "backlinks": ["a"]},
                "c": {"title": "Gamma", "links": ["b"], "backlinks": []},
            })
        }
        nodes, edges = grimoire.graph_subset(data, "a", None, True, "")
        self.assertEqual(set(nodes), {"a", "b", "c", grimoire.MISSING_MARK + "ghost"})
        slim, _ = grimoire.graph_subset(data, "a", 1, True, "")
        self.assertEqual(set(slim), {"a", "b", grimoire.MISSING_MARK + "ghost"})
        nomiss, _ = grimoire.graph_subset(data, "a", None, False, "")
        self.assertNotIn(grimoire.MISSING_MARK + "ghost", nomiss)
        hit, edges_hit = grimoire.graph_subset(data, "a", None, True, "beta")
        self.assertEqual(set(hit), {"b"})
        self.assertEqual(edges_hit, [])

    def test_app_local_and_missing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "pages"
            library.mkdir()
            (library / "index.md").write_text("# Index\n\nSee [[beta]] and [[void]].\n", encoding="utf-8")
            (library / "beta.md").write_text("# Beta\n\nBack to [[index]].\n", encoding="utf-8")
            cfg_path = root / "cfg.json"
            cfg_path.write_text(json.dumps({"name": "GRIMOIRE", "open": "library", "library": str(library)}), encoding="utf-8")
            os.environ["GRIMOIRE_CONFIG"] = str(cfg_path)
            try:
                app = App(root)
                app.handle(ord("g"))
                bar = grimoire.bar_spec(app)
                self.assertFalse(any("depth" in part for part in bar[0]))
                app.handle(ord("L"))
                self.assertEqual(app.glocal, 1)
                self.assertTrue(app.handle(ord("]")))
                self.assertEqual(app.glocal, 2)
                self.assertTrue(app.handle(ord("[")))
                self.assertEqual(app.glocal, 1)
                footer = grimoire.bar_spec(app)
                self.assertIn("depth 1", " ".join(list(footer[0]) + list(footer[2:])))
                self.assertTrue(app.handle(ord("m")))
                self.assertFalse(app.gmissing)
                self.assertNotIn(grimoire.MISSING_MARK + "void", app.gstate["shown"])
            finally:
                os.environ.pop("GRIMOIRE_CONFIG", None)


def math_dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.library = Path(self.tmp.name)
        self.cfg = {"library": str(self.library)}

    def test_dotted_slugs_do_not_collide(self):
        grimoire.write_page("v1", "one", self.cfg)
        grimoire.write_page("v1.2", "two", self.cfg)
        self.assertEqual(grimoire.read_page("v1", self.cfg), "one\n")
        self.assertEqual(grimoire.read_page("v1.2", self.cfg), "two\n")
        self.assertTrue((self.library / "v1.2.md").is_file())

    def test_write_leaves_no_temp_file(self):
        grimoire.write_page("note", "hi", self.cfg)
        grimoire.write_page("note", "again", self.cfg)
        names = sorted(p.name for p in self.library.iterdir())
        self.assertEqual([n for n in names if n.endswith(".tmp")], [])
        self.assertIn("note.md", names)

    def test_write_keeps_page_mode(self):
        page = self.library / "private.md"
        page.write_text("x\n")
        os.chmod(page, 0o600)
        grimoire.write_page("private", "y", self.cfg)
        self.assertEqual(page.stat().st_mode & 0o777, 0o600)
        self.assertEqual(page.read_text(), "y\n")

    def test_new_page_follows_umask(self):
        old = os.umask(0o022)
        try:
            grimoire.write_page("fresh", "x", self.cfg)
        finally:
            os.umask(old)
        self.assertEqual((self.library / "fresh.md").stat().st_mode & 0o777, 0o644)

    def test_failed_write_keeps_old_page_and_no_temp(self):
        grimoire.write_page("note", "old", self.cfg)
        from unittest import mock
        with mock.patch.object(grimoire.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                grimoire.write_page("note", "new", self.cfg)
        self.assertEqual((self.library / "note.md").read_text(), "old\n")
        self.assertEqual([p.name for p in self.library.iterdir() if p.name.endswith(".tmp")], [])

    def test_markdown_suffix_page_is_reachable(self):
        (self.library / "notes.markdown").write_text("hi\n")
        self.assertEqual(grimoire.read_page("notes", self.cfg), "hi\n")
        grimoire.write_page("notes", "there", self.cfg)
        self.assertEqual((self.library / "notes.markdown").read_text(), "there\n")
        self.assertFalse((self.library / "notes.md").exists())

    def test_atomic_write_keeps_symlink(self):
        real = self.library / "real.md"
        real.write_text("old\n")
        link = self.library / "link.md"
        link.symlink_to(real)
        grimoire.atomic_write(link, b"new\n")
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(), "new\n")

    def test_atomic_write_keeps_hard_link(self):
        page = self.library / "page.md"
        page.write_text("old\n")
        other = self.library / "other.md"
        os.link(page, other)
        grimoire.atomic_write(page, b"new\n")
        self.assertEqual(other.read_text(), "new\n")

    def test_mcp_survives_non_object_lines(self):
        import io
        lines = [
            "[1]", "5", '"x"', "not json",
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "nope"}),
            json.dumps({"jsonrpc": "2.0", "id": 4, "method": "initialize", "params": "x"}),
            json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": [1]}),
        ]
        out = io.StringIO()
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO("\n".join(lines) + "\n"), out
        try:
            grimoire.serve_mcp()
        finally:
            sys.stdin, sys.stdout = old_in, old_out
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4, 5])
        self.assertEqual(replies[0]["result"], {})
        self.assertEqual(len(replies[1]["result"]["tools"]), len(grimoire.MCP_TOOLS))
        self.assertEqual(replies[2]["error"]["code"], -32601)
        self.assertEqual(replies[3]["result"]["protocolVersion"], "2024-11-05")
        self.assertTrue(replies[4]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()

