#!/usr/bin/env python3
"""Static file server for app/ with SPA fallback.

Client-side routes (/chat/<id>, /chat/, /memory/) aren't real files, so
plain `python -m http.server` 404s on direct navigation or refresh. This
serves the real file when one exists and falls back to index.html
otherwise, letting app.js's own router take it from there.
"""
import http.server
import os

PORT = 5500


class SPARequestHandler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        requested = super().translate_path(path)
        if os.path.isfile(requested):
            return requested
        return super().translate_path("/index.html")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    http.server.HTTPServer(("", PORT), SPARequestHandler).serve_forever()
