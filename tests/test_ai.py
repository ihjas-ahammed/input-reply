"""Tests for the experimental Gemini Live assistant, run against a fake Live server and a simulated desktop."""

import base64
import http.client
import io
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fake_live import DONE, FakeLive, call, say
from PIL import Image, ImageDraw
from test_server import FakeBackend

from input_reply import actions, ai, core, macros, server, settings
from input_reply.actuator import Actuator

SCREEN = (1920, 1080)
CONTACTS = ["Farsan", "Alex", "Me"]


class SimDesktop(Actuator):
    """A tiny desktop with WhatsApp, Paint, and a screenshot app. It follows the same steps a real desktop would."""

    def __init__(self):
        super().__init__(FakeBackend())
        self.log, self.strokes, self.clipboard = [], [], False
        self.app = self.chat = self.dialog = None
        self.field, self.search, self.draft, self.attachment = "search", "", "", None
        self.sent = []
        self.screens_ok = True

    def screen_size(self):
        return SCREEN

    def screenshot(self):
        if not self.screens_ok:
            raise RuntimeError("no screenshot tool")
        image = Image.new("RGB", SCREEN, "white")
        draw = ImageDraw.Draw(image)
        for stroke in self.strokes:
            draw.line([tuple(p) for p in stroke], fill="black", width=8)
        return image

    def perform(self, step, cancel=None):
        kind = step["type"]
        if kind == "wait":
            if cancel:
                cancel.wait(step["seconds"])
            return
        if kind == "launch" and step["app"] not in {"whatsapp", "mspaint", "spectacle"}:
            raise RuntimeError(f"Cannot find an application named {step['app']}")
        self.log.append(step)
        if kind == "launch":
            self.app = step["app"]
            if self.app == "whatsapp":
                self.field, self.search, self.draft, self.chat, self.dialog = "search", "", "", None, None
        elif kind == "drag" and self.app == "mspaint":
            self.strokes.append([list(self._xy(x, y)) for x, y in step["points"]])
        elif kind == "type" and self.app == "whatsapp":
            if self.dialog is not None:
                self.dialog += step["text"]
            elif self.field == "search":
                self.search += step["text"]
            else:
                self.draft += step["text"]
        elif kind == "key":
            self.key(step["keys"])

    def key(self, keys):
        if self.app == "spectacle" and keys == "Print":
            self.clipboard = True
        elif self.app == "whatsapp" and keys == "ctrl+u":
            self.dialog = ""
        elif self.app == "whatsapp" and keys == "ctrl+v":
            if not self.clipboard:
                raise RuntimeError("clipboard is empty")
            self.attachment = "clipboard-image"
        elif self.app == "whatsapp" and keys == "Return":
            if self.dialog is not None:
                self.attachment, self.dialog = self.dialog, None
            elif self.field == "search":
                matches = [c for c in CONTACTS if self.search.lower() in c.lower()]
                if not matches:
                    raise RuntimeError(f"no contact matches {self.search!r}")
                self.chat, self.field = matches[0], "message"
            else:
                self.sent.append((self.chat, self.draft or None, self.attachment))
                self.draft, self.attachment = "", None


class AiCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for target in (patch.object(core, "data_dir", return_value=Path(temp.name)),
                       patch.object(ai, "SETTLE", 0), patch.object(macros, "PAUSE", 0),
                       patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""})):
            target.start()
            self.addCleanup(target.stop)
        self.addCleanup(core.set_account, None)
        settings.update(ai_enabled=True, ai_voice=True)
        ai.set_api_key("test-key")
        self.desktop = SimDesktop()
        self.state = actions.MacroState(self.desktop.backend)
        self.spoken = []
        self.player = type("Player", (), {"error": None, "play": lambda _, pcm, rate=24000: self.spoken.append((len(pcm), rate))})()
        self.live = None

    def assistant(self, script):
        self.live = FakeLive(script)
        self.addCleanup(self.live.close)
        assistant = ai.Assistant(self.desktop.backend, self.state, player=self.player, actuator=self.desktop,
                                 connect_factory=lambda model: ai.LiveSession(self.live.url, model))
        self.addCleanup(assistant.close)
        return assistant

    def ask(self, assistant, text, timeout=15):
        assistant.ask(text)
        deadline = time.monotonic() + timeout
        while self.state.busy():
            self.assertLess(time.monotonic(), deadline, "assistant did not finish")
            time.sleep(0.01)
        return self.state.snapshot()


class DesignAndReuseTests(AiCase):
    def whatsapp_script(self):
        return [[call("begin_macro")],
                [call("launch_app", app="whatsapp")],
                [call("wait", seconds=0.05)],
                [call("type_text", text="Farsan")],
                [call("press_key", keys="Return")],
                [call("type_text", text="Hi")],
                [call("press_key", keys="Return")],
                [call("save_macro", name="whatsapp-message", description="Send a WhatsApp message to a contact",
                      parameters=[{"name": "friend", "value": "Farsan"}, {"name": "message", "value": "Hi"}])],
                [say("Done. I sent Hi to Farsan."), DONE]]

    def test_designs_a_macro_once_then_reuses_it_instantly(self):
        assistant = self.assistant(self.whatsapp_script())
        job = self.ask(assistant, "open WhatsApp and send Hi to Farsan")
        self.assertEqual(job["phase"], "done")
        self.assertEqual(self.desktop.sent, [("Farsan", "Hi", None)])

        setup = self.live.setup
        self.assertEqual(setup["model"], "models/gemini-3.8-flash-live")
        self.assertEqual(setup["generationConfig"]["responseModalities"], ["AUDIO"])
        self.assertIn("outputAudioTranscription", setup)
        tools = {t["name"] for t in setup["tools"][0]["functionDeclarations"]}
        self.assertTrue({"screenshot", "click", "drag", "type_text", "run_macro", "save_macro"} <= tools)
        self.assertTrue(self.live.frames(), "the model must be shown the screen while designing")
        self.assertEqual(self.spoken, [(480, 24000)], "the answer is spoken on this computer")
        replies = [e["text"] for e in assistant.status()["entries"] if e["role"] == "assistant"]
        self.assertEqual(replies, ["Done. I sent Hi to Farsan."], "and shown as text")

        saved = core.read_recording("whatsapp-message.json")
        self.assertEqual(saved["format"], core.FORMAT_AGENT)
        self.assertEqual([p["name"] for p in saved["parameters"]], ["friend", "message"])
        self.assertEqual([s["type"] for s in saved["steps"]], ["launch", "wait", "type", "key", "type", "key"])
        self.assertEqual([s.get("param") for s in saved["steps"] if s["type"] == "type"], ["friend", "message"])
        info = core.inspect("whatsapp-message.json")
        self.assertTrue(info["ai"])
        self.assertEqual([p["name"] for p in info["parameters"]], ["friend", "message"])

        frames_before, calls_before = len(self.live.frames()), len(self.live.tool_responses())
        self.live.extend([[call("run_macro", name="whatsapp-message.json",
                                params=[{"name": "friend", "value": "Alex"}, {"name": "message", "value": "Hello"}])],
                          [say("Sent Hello to Alex."), DONE]])
        job = self.ask(assistant, "send Hello to Alex on WhatsApp")
        self.assertEqual(job["phase"], "done")
        self.assertEqual(self.desktop.sent[-1], ("Alex", "Hello", None))
        self.assertIn("whatsapp-message.json", self.live.texts()[-1], "saved macros are offered to the model")
        self.assertEqual(len(self.live.tool_responses()) - calls_before, 1, "one tool call, no exploring")
        self.assertEqual(len(self.live.frames()), frames_before, "no screenshots are needed to reuse a macro")
        self.assertEqual(self.live.connections, 1, "the live session is reused")

    def test_macro_keeps_original_text_for_omitted_parameters(self):
        assistant = self.assistant(self.whatsapp_script())
        self.ask(assistant, "send Hi to Farsan")
        self.live.extend([[call("run_macro", name="whatsapp-message.json", params=[{"name": "friend", "value": "Alex"}])], [DONE]])
        self.ask(assistant, "say hi to Alex")
        self.assertEqual(self.desktop.sent[-1], ("Alex", "Hi", None))

    def test_parameter_that_was_never_typed_is_rejected(self):
        script = [[call("begin_macro")], [call("launch_app", app="whatsapp")], [call("type_text", text="Farsan")],
                  [call("save_macro", name="x", description="d", parameters=[{"name": "friend", "value": "Nobody"}])],
                  [DONE]]
        assistant = self.assistant(script)
        self.ask(assistant, "go")
        error = self.live.tool_responses()[-1]["response"]["error"]
        self.assertIn("never typed", error)
        self.assertEqual(core.catalog(), [])


class ScenarioTests(AiCase):
    def test_draws_in_paint_and_replays_the_drawing(self):
        square = [{"x": 400, "y": 200}, {"x": 800, "y": 200}, {"x": 800, "y": 500}, {"x": 400, "y": 500}, {"x": 400, "y": 200}]
        script = [[call("begin_macro")], [call("launch_app", app="mspaint")], [call("wait", seconds=0.05)],
                  [call("drag", points=square)], [call("screenshot")],
                  [call("save_macro", name="draw-square", description="Draw a square in Paint")],
                  [say("Drew a square."), DONE]]
        assistant = self.assistant(script)
        self.assertEqual(self.ask(assistant, "draw a square in paint")["phase"], "done")
        # The model works in a 1280 px wide frame; the desktop is 1920 px wide, so coordinates scale by 1.5.
        self.assertEqual(self.desktop.strokes, [[[600, 300], [1200, 300], [1200, 750], [600, 750], [600, 300]]])
        frame = Image.open(io.BytesIO(base64.b64decode(self.live.frames()[-1]["realtimeInput"]["video"]["data"])))
        self.assertEqual(frame.size, (1280, 720))
        self.assertLess(frame.getpixel((600, 200))[0], 128, "the drawn line is visible to the model")
        self.assertGreater(frame.getpixel((600, 400))[0], 200)

        self.desktop.strokes.clear()
        self.live.extend([[call("run_macro", name="draw-square.json")], [DONE]])
        self.ask(assistant, "draw the square again")
        self.assertEqual(len(self.desktop.strokes), 1, "the macro draws it again with no exploring")

    def test_replay_scales_to_a_different_screen_size(self):
        core.write_recording("m.json", {"format": core.FORMAT_AGENT, "screen": [1000, 500],
                                        "steps": [{"type": "drag", "points": [[100, 100], [500, 250]]}], "parameters": []})
        self.desktop.app = "mspaint"
        with patch.object(self.desktop, "screen_size", return_value=(2000, 1000)):
            macros.play(core.read_recording("m.json"), {}, self.desktop)
        self.assertEqual(self.desktop.strokes, [[[200, 200], [1000, 500]]])

    def test_screenshot_app_then_send_the_picture_on_whatsapp(self):
        script = [[call("begin_macro")], [call("launch_app", app="spectacle")], [call("press_key", keys="Print")],
                  [call("launch_app", app="whatsapp")], [call("type_text", text="Farsan")], [call("press_key", keys="Return")],
                  [call("press_key", keys="ctrl+v")], [call("press_key", keys="Return")],
                  [call("save_macro", name="send-screenshot", description="Screenshot and send it on WhatsApp",
                        parameters=[{"name": "friend", "value": "Farsan"}])],
                  [say("Sent you the screenshot."), DONE]]
        assistant = self.assistant(script)
        self.assertEqual(self.ask(assistant, "take a screenshot and send it to Farsan on whatsapp")["phase"], "done")
        self.assertEqual(self.desktop.sent, [("Farsan", None, "clipboard-image")])
        self.desktop.clipboard = False   # the replay must take a fresh screenshot itself
        self.live.extend([[call("run_macro", name="send-screenshot.json", params=[{"name": "friend", "value": "Alex"}])], [DONE]])
        self.ask(assistant, "screenshot to Alex")
        self.assertEqual(self.desktop.sent[-1], ("Alex", None, "clipboard-image"))

    def test_sends_a_saved_screenshot_file_to_my_own_number(self):
        def type_path(server):
            path = next(r for r in server.tool_responses() if r["name"] == "save_screenshot")["response"]["path"]
            return call("type_text", text=path)
        script = [[call("begin_macro")], [call("save_screenshot")], [call("launch_app", app="whatsapp")],
                  [call("type_text", text="Me")], [call("press_key", keys="Return")],
                  [call("press_key", keys="ctrl+u")], [type_path], [call("press_key", keys="Return")],
                  [call("press_key", keys="Return")],
                  [lambda server: call("save_macro", name="send-file", description="Send a file to my own number",
                                       parameters=[{"name": "file", "value": next(
                                           r for r in server.tool_responses() if r["name"] == "save_screenshot")["response"]["path"]}])],
                  [say("Sent the file to your number."), DONE]]
        assistant = self.assistant(script)
        self.assertEqual(self.ask(assistant, "save a screenshot and send the file to my own whatsapp number")["phase"], "done")
        shot = next(r for r in self.live.tool_responses() if r["name"] == "save_screenshot")["response"]["path"]
        self.assertTrue(Path(shot).is_file())
        with Image.open(shot) as saved:
            self.assertEqual(saved.size, SCREEN, "the file is the full-resolution screen")
        self.assertEqual(self.desktop.sent, [("Me", None, shot)])
        other = Path(shot).with_name("other.txt")
        other.write_text("hello")
        self.live.extend([[call("run_macro", name="send-file.json", params=[{"name": "file", "value": str(other)}])], [DONE]])
        self.ask(assistant, "send other.txt to my number")
        self.assertEqual(self.desktop.sent[-1], ("Me", None, str(other)))


class SafetyTests(AiCase):
    def test_unsafe_tool_arguments_never_reach_the_desktop(self):
        script = [[call("launch_app", app="x; rm -rf /")], [call("open_url", url="file:///etc/passwd")],
                  [call("press_key", keys="ctrl+;rm")], [call("click", x=-5, y=10)], [call("no_such_tool")], [DONE]]
        assistant = self.assistant(script)
        self.ask(assistant, "do bad things")
        responses = self.live.tool_responses()
        self.assertEqual(len(responses), 5)
        self.assertTrue(all("error" in r["response"] for r in responses), responses)
        self.assertEqual(self.desktop.log, [])

    def test_step_limit_stops_a_runaway_model(self):
        assistant = self.assistant([[call("wait", seconds=0)]] * 100)
        job = self.ask(assistant, "loop forever")
        self.assertEqual(job["phase"], "error")
        self.assertIn("too many steps", job["message"])
        self.assertEqual(len(self.desktop.log), 0)   # waits are not logged; nothing ran beyond the cap
        self.assertEqual(sum(1 for r in self.live.tool_responses() if "ok" in r["response"]), ai.MAX_TOOL_CALLS)

    def test_stop_button_interrupts_the_assistant(self):
        assistant = self.assistant([[call("wait", seconds=10)], [DONE]])
        assistant.ask("wait around")
        time.sleep(0.4)
        started = time.monotonic()
        self.state.stop()
        while self.state.busy():
            time.sleep(0.01)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.state.snapshot()["phase"], "cancelled")

    def test_missing_screenshot_tool_degrades_instead_of_failing(self):
        self.desktop.screens_ok = False
        script = [[call("launch_app", app="whatsapp")], [call("type_text", text="Farsan")], [DONE]]
        assistant = self.assistant(script)
        self.assertEqual(self.ask(assistant, "go")["phase"], "done")
        first, second = (r["response"] for r in self.live.tool_responses())
        self.assertIn("warning", first)
        self.assertNotIn("warning", second, "screenshots are not retried after they fail")

    def test_off_by_default_and_needs_a_key(self):
        settings.update(ai_enabled=False)
        assistant = self.assistant([[DONE]])
        with self.assertRaisesRegex(RuntimeError, "turned off"):
            assistant.ask("hello")
        settings.update(ai_enabled=True)
        ai.set_api_key(None)
        with self.assertRaisesRegex(RuntimeError, "API key"):
            assistant.ask("hello")
        self.assertFalse(self.state.busy())
        self.assertEqual(self.live.connections, 0)

    def test_connection_failure_is_reported_not_raised(self):
        assistant = ai.Assistant(self.desktop.backend, self.state, player=self.player, actuator=self.desktop,
                                 connect_factory=lambda model: (_ for _ in ()).throw(RuntimeError("model not found")))
        job = self.ask(assistant, "hello")
        self.assertEqual(job["phase"], "error")
        self.assertIn("model not found", [e["text"] for e in assistant.status()["entries"] if e["role"] == "error"][0])


class UnitTests(unittest.TestCase):
    def test_parameterize_splits_partial_matches(self):
        steps = [{"type": "launch", "app": "x"}, {"type": "type", "text": "Hi Farsan, how are you"}]
        result, declared = macros.parameterize(steps, [{"name": "friend", "value": "Farsan"}])
        self.assertEqual([s["text"] for s in result if s["type"] == "type"], ["Hi ", "Farsan", ", how are you"])
        self.assertEqual([s.get("param") for s in result if s["type"] == "type"], [None, "friend", None])
        self.assertEqual(declared, [{"name": "friend"}])
        with self.assertRaises(ValueError):
            macros.parameterize(steps, [{"name": "friend", "value": "Alex"}])
        with self.assertRaises(ValueError):
            macros.parameterize(steps, [{"name": "bad name", "value": "Farsan"}])

    def test_step_validation(self):
        good = [{"type": "click", "x": 1, "y": 2}, {"type": "type", "text": "a"}, {"type": "key", "keys": "ctrl+shift+s"},
                {"type": "open_url", "url": "https://web.whatsapp.com"}, {"type": "launch", "app": "mspaint"}]
        for step in good:
            core.validate_step(step)
        bad = [{"type": "click", "x": "1", "y": 2}, {"type": "click", "x": 1, "y": 2, "button": "x"},
               {"type": "open_url", "url": "javascript:alert(1)"}, {"type": "launch", "app": "a b"},
               {"type": "launch", "app": "../bin/sh"}, {"type": "key", "keys": "ctrl+"}, {"type": "wait", "seconds": 999},
               {"type": "drag", "points": [[1, 1]]}, {"type": "shell", "cmd": "ls"}, "click"]
        for step in bad:
            with self.assertRaises(ValueError, msg=step):
                core.validate_step(step)

    def test_ai_macros_have_fixed_parameters(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(core, "data_dir", return_value=Path(temp)):
            core.write_recording("m.json", {"format": core.FORMAT_AGENT, "steps": [{"type": "type", "text": "a", "param": "p"}],
                                            "parameters": [{"name": "p"}], "description": "demo"})
            with self.assertRaisesRegex(ValueError, "fixed parameters"):
                core.add_parameter("m.json", 1, "q")
            row = core.catalog()[0]
            self.assertTrue(row["ai"])
            self.assertEqual(row["steps"], 1)
            with self.assertRaises(ValueError):
                core.write_recording("n.json", {"format": core.FORMAT_AGENT, "steps": [{"type": "type", "text": "a", "param": "zz"}],
                                                "parameters": []})

    def test_x11_actuator_runs_the_expected_commands(self):
        calls = []

        class X11:
            name = "x11"
            cmd = staticmethod(lambda *parts: calls.append([str(p) for p in parts]) or "")
            type_text = staticmethod(lambda text, cancel=None: calls.append(["type", text]))

        actuator = Actuator(X11)
        actuator.set_scale([1000, 1000], [2000, 2000])
        actuator.perform({"type": "click", "x": 10, "y": 20, "count": 2})
        actuator.perform({"type": "drag", "points": [[0, 0], [5, 5]], "button": "left"})
        actuator.perform({"type": "key", "keys": "ctrl+l"})
        actuator.perform({"type": "type", "text": "hello"})
        actuator.perform({"type": "scroll", "dx": 0, "dy": 3})
        self.assertEqual(calls, [
            ["mousemove", "20", "40"], ["click", "--repeat", "2", "--delay", "80", "1"],
            ["mousemove", "0", "0"], ["mousedown", "1"], ["mousemove", "10", "10"], ["mouseup", "1"],
            ["key", "--clearmodifiers", "ctrl+l"], ["type", "hello"], ["click", "--repeat", "3", "5"]])

    def test_real_screen_capture_when_a_tool_is_available(self):
        class Real:
            name = "x11"
        try:
            image = Actuator(Real).screenshot()
        except RuntimeError as error:
            self.skipTest(str(error))
        self.assertGreater(image.width, 100)
        self.assertGreater(image.height, 100)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for target in (patch.object(core, "data_dir", return_value=Path(temp.name)),
                       patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""})):
            target.start()
            self.addCleanup(target.stop)
        self.httpd = server.make_server("127.0.0.1", 0, backend=FakeBackend())
        import threading
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        conn.request(method, path, json.dumps(body) if body is not None else None,
                     {"Authorization": "Bearer " + self.httpd.access_token, "Content-Type": "application/json"})
        response = conn.getresponse()
        raw = response.read().decode()
        conn.close()
        return response.status, raw

    def test_settings_hide_the_key_and_gate_the_assistant(self):
        status, raw = self.call("GET", "/api/ai")
        self.assertEqual((status, json.loads(raw)["enabled"], json.loads(raw)["has_key"]), (200, False, False))
        status, raw = self.call("POST", "/api/ai/ask", {"text": "hi"})
        self.assertEqual(status, 400)
        self.assertIn("turned off", raw)
        status, raw = self.call("POST", "/api/ai/settings", {"enabled": True, "api_key": "AIza-secret-key", "voice": False})
        self.assertEqual(status, 200)
        self.assertNotIn("secret", raw)
        data = json.loads(raw)
        self.assertEqual((data["enabled"], data["has_key"], data["voice"]), (True, True, False))
        self.assertNotIn("secret", self.call("GET", "/api/ai")[1])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(ai.key_path().stat().st_mode), 0o600)
        self.assertEqual(self.call("POST", "/api/ai/settings", {"model": "bad model!"})[0], 400)
        self.assertEqual(self.call("POST", "/api/ai/settings", {"api_key": "has space"})[0], 400)
        self.call("POST", "/api/ai/settings", {"api_key": ""})
        self.assertFalse(json.loads(self.call("GET", "/api/ai")[1])["has_key"])


if __name__ == "__main__":
    unittest.main()
