"""Count the lines in the open file. Copy this to ~/.grimoire/plugins/ to enable it.

    cp examples/linecount.py ~/.grimoire/plugins/
"""


def register(api):
    def count(app):
        n = len(app.lines)
        app.message = f"{n} line" if n == 1 else f"{n} lines"
        return True

    api.key("ctrl+w", count)
