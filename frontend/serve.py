"""Static server for the map and the manual approval page, with caching turned off.

python3 -m http.server sends no Cache-Control, so browsers kept an old map.html for days.
Instead of asking for a hard reload after every change, tell them not to store anything.

The root IS the map: a request for "/" is served map.html without a redirect, so the address
stays http://localhost:3100 and every relative link on the page still resolves from the root.
"""
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

MAP_PAGE = "/map.html"
ROOT_PATHS = {"/", "/index.html"}


def served_path(path: str) -> str:
    """The file a request asks for. The root paths read the map; everything else is itself."""
    parts = urlsplit(path)
    if parts.path not in ROOT_PATHS:
        return path
    return f"{MAP_PAGE}?{parts.query}" if parts.query else MAP_PAGE


class NoCacheHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        self.path = served_path(self.path)
        super().do_GET()

    def do_HEAD(self) -> None:
        self.path = served_path(self.path)
        super().do_HEAD()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — parent signature
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3100
    directory = sys.argv[2] if len(sys.argv) > 2 else "."
    handler = partial(NoCacheHandler, directory=directory)
    ThreadingHTTPServer(("", port), handler).serve_forever()


if __name__ == "__main__":
    main()
