"""Run the assistant against REAL apps on an isolated virtual X display (never your main screen).

    Xvfb :99 -screen 0 1280x800x24 & ;  xfwm4 (optional window manager) on :99
    GEMINI_API_KEY=... python tests/live_display_check.py

Real xdotool input, real screenshots of :99, and real Chrome pages (a canvas Paint app and a WhatsApp-style chat
page served locally). The real Gemini Live model designs each macro, then a second request reuses it.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

DISPLAY = os.environ.get("CHECK_DISPLAY", ":99")
os.environ.update({"DISPLAY": DISPLAY, "INPUT_REPLY_DISPLAY": DISPLAY, "INPUT_REPLY_BACKEND": "x11"})
os.environ.pop("WAYLAND_DISPLAY", None)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from input_reply import actions, ai, core, settings  # noqa: E402
from input_reply.backends import select_backend  # noqa: E402

EVENTS = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        name = self.path.strip("/") or "chat.html"
        page = HERE / "live_display" / name
        if not page.is_file() or page.suffix != ".html":
            self.send_error(404)
            return
        body = page.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        event = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ok = True
        if event["type"] == "file":
            ok = os.path.isfile(event["path"])
        if ok:
            EVENTS.append(event)
        body = json.dumps({"ok": ok}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    if not ai.api_key():
        sys.exit("Set GEMINI_API_KEY first.")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    profile = tempfile.mkdtemp()
    os.environ["BROWSER"] = (f"google-chrome --no-sandbox --user-data-dir={profile} --window-size=1280,800 "
                             "--window-position=0,0 --no-first-run --disable-features=Translate %s")
    with tempfile.TemporaryDirectory() as temp, patch.object(core, "data_dir", return_value=Path(temp)):
        settings.update(ai_enabled=True)
        backend = select_backend()
        state = actions.MacroState(backend)
        assistant = ai.Assistant(backend, state, player=None)

        def ask(text, timeout=240):
            assistant.ask(text)
            end = time.time() + timeout
            while state.busy() and time.time() < end:
                time.sleep(0.3)
            job = state.snapshot()
            print(f"\n> {text}\n  result: {job['phase']} - {job['message']}")
            print("  app received:", [{k: v for k, v in e.items() if k in ("type", "to", "text", "path", "x0", "x1", "y0", "y1")} for e in EVENTS])
            for entry in assistant.status()["entries"]:
                if entry["id"] > ask.seen:
                    print(f"  [{entry['role']}] {entry['text'][:160]}")
                    ask.seen = entry["id"]
            return job

        ask.seen = 0
        results = []

        def messages():
            return [(e["to"], e["text"]) for e in EVENTS if e["type"] == "message"]

        ask(f"Open the WhatsApp page {base}/chat.html in the browser and send Hi to Farsan.")
        results.append(("chat: message reached Farsan", ("Farsan", "Hi") in messages()))
        results.append(("chat: macro saved", len(core.catalog()) >= 1))
        EVENTS.clear()
        ask("Send Hello to Alex on WhatsApp.")
        results.append(("chat: reuse delivered exactly one message to Alex", messages() == [("Alex", "Hello")]))

        EVENTS.clear()
        ask(f"Open the Paint page {base}/paint.html in the browser and draw a large square on the canvas.")
        strokes = [e for e in EVENTS if e["type"] == "stroke"]
        results.append(("paint: a square-like stroke was drawn", any(e["x1"] - e["x0"] > 200 and e["y1"] - e["y0"] > 200 for e in strokes)))

        EVENTS.clear()
        ask(f"Save a screenshot to a file, then on the WhatsApp page {base}/chat.html send that file to my own number "
            "(the contact Me) using the Attach button.")
        files = [e for e in EVENTS if e["type"] == "file"]
        results.append(("file: screenshot file delivered to Me", any(e["to"] == "Me" and e["path"].endswith(".png") for e in files)))

        print("\nResults")
        for label, passed in results:
            print(f"[{'PASS' if passed else 'FAIL'}] {label}")
        assistant.close()
        subprocess.run(["pkill", "-f", f"user-data-dir={profile}"])
        sys.exit(0 if all(p for _, p in results) else 1)


if __name__ == "__main__":
    main()
