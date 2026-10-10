"""Real Linux input checks on a private Xvfb display, never the user's desktop."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from input_reply import actions, core, scripting
from input_reply.backends.x11 import X11Backend, X11StopListener

TOOLS = ("Xvfb", "xdotool", "xinput", "xmodmap", "xterm")
SUPPORTED = sys.platform.startswith("linux") and all(shutil.which(t) for t in TOOLS)
EDITOR = """
import json, sys, termios
from pathlib import Path
attrs = termios.tcgetattr(sys.stdin)
attrs[6][termios.VERASE] = b"\\x08"
termios.tcsetattr(sys.stdin, termios.TCSANOW, attrs)
snapshot = Path(sys.argv[1])
snapshot.write_text(json.dumps({"text": ""}))
for line in sys.stdin:
    snapshot.write_text(json.dumps({"text": line}))
"""


@unittest.skipUnless(SUPPORTED, "Requires Linux, Xvfb, xdotool, xinput, xmodmap and xterm")
class LinuxIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.snapshot = self.root / "editor.json"
        xvfb = subprocess.Popen(["Xvfb", "-displayfd", "1", "-screen", "0", "800x600x24", "-nolisten", "tcp"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self.stop_process, xvfb)
        display = ":" + xvfb.stdout.readline().strip()
        self.assertNotEqual(display, ":", "Xvfb failed to start")
        xvfb.stdout.close()
        env = patch.dict(os.environ, {"DISPLAY": display, "INPUT_REPLY_DISPLAY": display,
                                     "INPUT_REPLY_BACKEND": "x11", "XDG_SESSION_TYPE": "x11",
                                     "XAUTHORITY": str(self.root / "auth"),
                                     "INPUT_REPLY_XAUTHORITY": str(self.root / "auth"),
                                     "XDG_DATA_HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        core.set_account(None)
        editor = subprocess.Popen(["xterm", "-T", "Input Reply isolated test editor", "-geometry", "80x24+0+0",
                                  "-e", sys.executable, "-c", EDITOR, str(self.snapshot)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_process, editor)
        self.backend = X11Backend()
        self.wait_for(lambda: self.snapshot.is_file())
        self.window = next(w for w in self.backend.windows() if w["title"] == "Input Reply isolated test editor")
        self.backend.focus(self.window["id"])
        self.backend.cmd("mousemove", 80, 80)
        self.backend.cmd("click", "1")

    @staticmethod
    def stop_process(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

    def wait_for(self, check, timeout=5):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if check():
                return
            time.sleep(.03)
        self.fail(f"Timed out waiting for isolated desktop; received {self.text()!r}")

    def text(self):
        try:
            return json.loads(self.snapshot.read_text())["text"]
        except (ValueError, FileNotFoundError):
            return None

    def test_record_parameterize_and_replay_through_cli(self):
        errors = []
        def record():
            try:
                actions.record_once(self.backend, "linux.json", 1.4, 0, self.window["id"])
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=record)
        worker.start()
        time.sleep(.5)  # Allow XInput's recorder to subscribe before typing.
        self.backend.cmd("type", "--delay", "30", "--", "Hi")
        self.backend.cmd("key", "Return")
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.wait_for(lambda: self.text() == "Hi\n")
        data = core.read_recording("linux.json")
        self.assertGreater(len(data["events"]), 0)
        mapping = self.backend.keymap()
        self.assertEqual(len(core.typing_blocks(data, mapping)), 1)
        core.add_parameter("linux.json", 1, "message", mapping)
        result = subprocess.run([sys.executable, "-m", "input_reply", "run", "linux", "--set", "message=Linux works",
                                 "--countdown", "0", "--speed", "10", "--json"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["completed"])
        self.wait_for(lambda: self.text() == "Linux works\n")

    def test_custom_python_click_focus_hotkey_and_uppercase(self):
        code = '''def run(context):
    context.focus("Input Reply isolated test editor")
    context.click(80, 80)
    context.type_text("discard this")
    context.hotkey("ctrl", "u")
    context.type_text("Z")
    context.press_key("backspace")
    context.key_down("shift")
    context.press_key("a")
    context.key_up("shift")
    context.type_text(context.params["message"])
    context.press_key("enter")
'''
        scripting.execute_python_macro(code, self.backend, {"message": " Linux"}, speed=10)
        self.wait_for(lambda: self.text() == "A Linux\n")

    def test_ctrl_escape_cancels_without_pynput(self):
        cancel = threading.Event()
        listener = X11StopListener(cancel)
        self.addCleanup(listener.stop)
        self.backend.cmd("keydown", "Control_L", "Escape")
        try:
            self.assertTrue(cancel.wait(2))
        finally:
            self.backend.cmd("keyup", "Escape", "Control_L")
