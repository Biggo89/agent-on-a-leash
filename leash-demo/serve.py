#!/usr/bin/env python3
"""Serve the demo and open it. One command, no dependencies, no network.

    ./serve.py            → http://127.0.0.1:8771
    ./serve.py 9000       → a different port

ES modules need an http origin, so this is not optional — but nothing here
leaves the machine, and the app itself makes no outbound request except the
Google Fonts stylesheet, which fails cleanly to the system font stack offline.
"""
import functools, http.server, socketserver, sys, threading, webbrowser
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8771
ROOT = Path(__file__).resolve().parent


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):  # keep the terminal quiet on stage
        pass


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    handler = functools.partial(Handler, directory=str(ROOT))
    with socketserver.TCPServer(("127.0.0.1", PORT), handler) as httpd:
        url = f"http://127.0.0.1:{PORT}/"
        print(f"Leash demo → {url}   (ctrl-c to stop)")
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
