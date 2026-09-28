import json
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from input_reply import core, server


class FakeBackend:
    name = "windows"

    def __init__(self):
        self.emitted = []
        self.typed = []

    def available(self):
        return True

    def windows(self):
        return [{"id": "1", "title": "Test editor"}]

    def active_window(self):
        return self.windows()[0]

    def focus(self, ident):
        if str(ident) != "1":
            raise ValueError("Unknown window")
        return self.active_window()

    def focus_recorded(self, data, override=None):
        return self.focus(override or data["target_window"]["id"])

    def open_player(self):
        return self

    def close_player(self):
        pass

    def emit(self, event):
        self.emitted.append(event)

    def type_text(self, value, cancel=None):
        self.typed.append(value)


class ServerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = patch.object(core, "data_dir", return_value=Path(temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend = FakeBackend()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.httpd.daemon_threads = True
        self.httpd.backend = self.backend
        self.httpd.state = server.MacroState(self.backend)
        self.httpd.access_token = "test-code"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        core.write_recording("demo.json", {
            "format": core.FORMAT, "backend": "windows", "duration": 0.2,
            "target_window": {"id": "1", "title": "Test editor"},
            "events": [{"type": "key_down", "key": "h", "t": 0.05},
                       {"type": "key_up", "key": "h", "t": 0.08}],
        })

    def request(self, path, body=None):
        payload = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{self.httpd.server_port}{path}",
                                     data=payload, headers={"Authorization": "Bearer test-code",
                                                            "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.load(response)

    def test_parameter_api_and_replay(self):
        status, public = self.request("/api/inspect?name=demo.json")
        self.assertEqual(status, 200)
        self.assertEqual(len(public["blocks"]), 1)
        self.assertNotIn("events", public)
        status, changed = self.request("/api/parameter/add", {"name": "demo.json", "block": 1,
                                                               "parameter": "friend"})
        self.assertEqual(status, 200)
        self.assertEqual(changed["parameters"], [{"name": "friend", "block": 1}])
        status, _ = self.request("/api/replay", {"name": "demo.json", "countdown": 0,
                                                 "params": {"friend": "Alex"}})
        self.assertEqual(status, 202)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.request("/api/status")[1]
            if not job["busy"]:
                break
            time.sleep(0.02)
        self.assertEqual(job["phase"], "done")
        self.assertEqual(self.backend.typed, ["Alex"])
        self.assertEqual(self.backend.emitted, [])
        status, changed = self.request("/api/parameter/remove", {"name": "demo.json",
                                                                  "parameter": "friend"})
        self.assertEqual(changed["parameters"], [])


if __name__ == "__main__":
    unittest.main()
