"""Static server for the fixture site, shared by the tests and manual runs.

Fixture files use a __BASE__ placeholder wherever an absolute URL is required
(sitemaps and robots.txt must use absolute URLs), rewritten on the fly so the
site works on whatever ephemeral port it lands on.
"""

from __future__ import annotations

import functools
import http.server
import mimetypes
import os
import socketserver
import threading

mimetypes.add_type("application/vnd.google-earth.kml+xml", ".kml")
mimetypes.add_type("application/vnd.google-earth.kmz", ".kmz")
mimetypes.add_type("application/geo+json", ".geojson")

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixture_site")
REWRITE_SUFFIXES = (".xml", "robots.txt")


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def guess_type(self, path):
        if str(path).endswith(".esri.json"):
            return "application/json"
        if os.path.basename(str(path)) == "home":
            return "text/html"
        return super().guess_type(path)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.endswith(REWRITE_SUFFIXES):
            local = self.translate_path(path)
            if os.path.isfile(local):
                base = f"http://{self.headers.get('Host', self.server.server_address[0])}"
                body = open(local, "rb").read().replace(b"__BASE__", base.encode())
                self.send_response(200)
                self.send_header("Content-Type", self.guess_type(local))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        super().do_GET()


def start(port: int = 0):
    """Start the fixture server in a daemon thread; returns (base_url, httpd)."""
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", port),
                                   functools.partial(Handler, directory=ROOT))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd
