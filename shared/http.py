"""Stdlib-only JSON HTTP. No web framework: the runtime's guarantees should not depend on one."""

import json
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

Route = tuple[str, re.Pattern, callable]


def route(method: str, pattern: str) -> tuple:
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
    return method.upper(), regex


class _QuietServer(ThreadingHTTPServer):
    """A browser dropping the connection mid-request (reload, closed tab) just leaves no one
    to answer — not an error. Only that case is passed over quietly; other errors keep the
    standard handling (traceback)."""

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class JsonServer:
    def __init__(self, port: int):
        self.port = port
        self._routes: list[Route] = []

    def add(self, method: str, pattern: str, handler) -> None:
        verb, regex = route(method, pattern)
        self._routes.append((verb, regex, handler))

    def _dispatch(self, verb: str, path: str, query: dict, body: dict):
        for method, regex, handler in self._routes:
            match = regex.match(path)
            if match and method == verb:
                return handler(body=body, query=query, **match.groupdict())
        return 404, {"error": "no such route", "path": path}

    def serve_forever(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep the demo log clean
                pass

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _respond(self, verb: str):
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {}
                try:
                    status, payload = server._dispatch(verb, parsed.path, query, body)
                except Exception as exc:  # noqa: BLE001 - better the demo server never dies
                    status, payload = 500, {"error": str(exc)}
                # A string body is sent as is (the markdown report); everything else is JSON.
                if isinstance(payload, str):
                    encoded, content_type = payload.encode(), "text/markdown; charset=utf-8"
                else:
                    encoded = json.dumps(payload, ensure_ascii=False, default=str).encode()
                    content_type = "application/json; charset=utf-8"
                self.send_response(status)
                self._cors()
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    # If the screen closes the tab or reloads mid-poll, there's no one to take
                    # the answer. The threading server used to print a stack trace and litter
                    # the log, so this folds quietly. Unrelated to judgement.
                    return

            def do_GET(self):
                self._respond("GET")

            def do_POST(self):
                self._respond("POST")

        _QuietServer(("0.0.0.0", self.port), Handler).serve_forever()

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread


def get_json(url: str, timeout: float = 5.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def post_json(url: str, payload: dict, timeout: float = 20.0, headers: dict | None = None):
    return post_json_status(url, payload, timeout=timeout, headers=headers)[1]


def post_json_status(url: str, payload: dict, timeout: float = 20.0,
                     headers: dict | None = None) -> tuple[int, dict | None]:
    """(HTTP status, body). (0, None) if unreachable.

    A model server rejecting some argument (400) and one that is just slow (timeout) need
    different handling: the first is fixed by resending without that argument, the second
    only waits again if resent. Lumped into a single None they'd be indistinguishable, so the
    status code comes back too.
    """
    data = json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return 0, None
