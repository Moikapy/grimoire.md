#!/usr/bin/env python3
"""Renderer tests. No tty required."""

import tempfile
import unittest
import os
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
            app.handle_read(9)
            self.assertTrue(app.inserting)
            app.handle_read(ord("!"))
            self.assertEqual(app.lines[0], "!hello")
            app.handle_read(27)
            self.assertFalse(app.inserting)
            self.assertEqual(app.mode, "read")
            app.handle_read(1)
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

    def test_banner_is_taller_than_the_name(self):
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

    def test_bare_launch_follows_open_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "here"
            shelf = Path(tmp) / "shelf"
            cwd.mkdir()
            shelf.mkdir()
            cfg = {"name": "GRIMOIRE", "open": "cwd", "library": str(shelf)}
            root, opened = resolve_launch([], cfg, cwd)
            self.assertEqual(root, cwd.resolve())
            self.assertIsNone(opened)
            cfg["open"] = "library"
            root, opened = resolve_launch([], cfg, cwd)
            self.assertEqual(root, shelf.resolve())
            root, opened = resolve_launch(["library"], cfg, cwd)
            self.assertEqual(root, shelf.resolve())

    def test_setup_writes_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            os.environ["GRIMOIRE_CONFIG"] = str(path)
            try:
                setup_config(["--open", "cwd", "--library", "~/scripts", "--name", "GRIMOIRE"])
                saved = load_config()
            finally:
                os.environ.pop("GRIMOIRE_CONFIG", None)
            self.assertEqual(saved["open"], "cwd")
            self.assertEqual(saved["name"], "GRIMOIRE")
            self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
