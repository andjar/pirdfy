#!/usr/bin/env python3
"""PIRDFY_V1_SHIM: stand-in for the Pirdfy 1.x entry point.

The 1.x updater (`pirdfy update` from version 1) only runs `git pull` and restarts
the old service, which starts this file. Pirdfy 2 needs a one-time install step
(new service, Python packages, model), so instead of crash-looping, this serves a
page explaining what to run. Standard library only. The 2.x installer recognises
this file and replaces the old setup; nothing here touches your data.
"""

import argparse
import html
import http.server
import logging
import re
import time

COMMAND = "curl -fsSL https://raw.githubusercontent.com/andjar/pirdfy/main/install.sh | sudo bash"
MESSAGE = ("Pirdfy 2 has been downloaded but needs a one-time install step. On the Pi, run: " + COMMAND +
           "  (your old photos are kept)")

PAGE = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Pirdfy: finish upgrading</title>
<style>body{{font:17px/1.5 system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;
background:#f3f5f0;color:#18322b}}code{{display:block;background:#18322b;color:#e6eee8;padding:1rem;
border-radius:8px;overflow-wrap:anywhere}}</style></head><body>
<h1>Almost there: Pirdfy 2 needs one install step</h1>
<p>The new version has been downloaded, but the old updater cannot finish the upgrade. Open a terminal
on the Raspberry Pi (for example over SSH) and run:</p>
<code>{html.escape(COMMAND)}</code>
<p>It takes a few minutes. Your old photos and videos are kept in <em>/opt/pirdfy-v1-backup-…</em>.
Afterwards, updates work from the dashboard (Settings, Install update) or with
<em>sudo pirdfy update</em>.</p></body></html>""".encode()


def port_from_config(path: str) -> int:
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return 8080
    m = re.search(r"^web:\s*$(?:\n[ \t]+.*)*?\n[ \t]+port:\s*(\d+)", text, re.M)
    return int(m.group(1)) if m else 8080


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(503)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("retry-after", "600")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/config.yaml")
    args, _ = ap.parse_known_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    port = port_from_config(args.config)
    logging.warning(MESSAGE)
    while True:
        try:
            server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
            break
        except OSError as e:  # port still in use right after a restart
            logging.warning("Waiting for port %d: %s", port, e)
            time.sleep(5)
    logging.warning("Serving upgrade instructions on port %d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
