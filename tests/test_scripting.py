import json
import tempfile
import threading
import unittest
from pathlib import Path

from input_reply import core, actions, scripting
from input_reply.backends.windows import WindowsBackend


class FakeBackend:
    name = "windows"

    def __init__(self):
        self.emitted = []
        self.cursor = (0, 0)
        self.windows_list = [{"id": "12345", "title": "Chrome Window"}]
        self.active_id = "12345"

    def available(self):
        return True

    def active_window(self):
        return self.windows_list[0]

    def focus(self, window_id, cancel=None):
        return self.windows_list[0]

    def focus_recorded(self, data, window_id=None, cancel=None):
        return self.windows_list[0]

    def window_center(self, ident):
        return (400, 300)

    def center_cursor_on_window(self, ident):
        center = self.window_center(ident)
        if center:
            self.cursor = center

    def set_cursor_position(self, x, y):
        self.cursor = (x, y)

    def cursor_position(self):
        return self.cursor

    def emit(self, event):
        self.emitted.append(dict(event))

    def type_text(self, text, cancel=None):
        self.emitted.append({"type": "text", "text": text})

    def open_player(self):
        pass

    def close_player(self):
        pass

    def record(self, duration, path, cancel=None):
        return {
            "format": core.FORMAT,
            "duration": 1.0,
            "events": [
                {"type": "motion", "x": 100, "y": 200, "t": 0.1},
                {"type": "button_down", "button": "left", "x": 100, "y": 200, "t": 0.2},
                {"type": "button_up", "button": "left", "x": 100, "y": 200, "t": 0.25},
                {"type": "key_down", "key": "h", "t": 0.5},
                {"type": "key_up", "key": "h", "t": 0.55},
                {"type": "key_down", "key": "i", "t": 0.6},
                {"type": "key_up", "key": "i", "t": 0.65},
            ]
        }


class ScriptingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_data_dir = core.data_dir
        core.data_dir = lambda: Path(self.temp_dir.name)
        core.set_account(None)

    def tearDown(self):
        core.data_dir = self.old_data_dir
        self.temp_dir.cleanup()

    def test_center_cursor_on_window(self):
        backend = FakeBackend()
        backend.center_cursor_on_window("12345")
        self.assertEqual(backend.cursor, (400, 300))

    def test_record_once_centers_cursor(self):
        backend = FakeBackend()
        data = actions.record_once(backend, "test-rec.json", 1, 0, window_id="12345")
        self.assertIsNotNone(data)
        # Cursor was centered during record_once
        self.assertEqual(backend.cursor, (400, 300))

    def test_macro_to_python_raw_events(self):
        backend = FakeBackend()
        data = backend.record(1, "path")
        code = scripting.macro_to_python(data, "test_raw.json")
        self.assertIn("def run(context):", code)
        self.assertIn("context.click(100, 200", code)
        self.assertIn("context.type_text(\"hi\")", code)

    def test_macro_to_python_agent(self):
        data = {
            "format": core.FORMAT_AGENT,
            "steps": [
                {"type": "click", "x": 150, "y": 250, "button": "left"},
                {"type": "type", "text": "hello", "param": "greeting"},
                {"type": "wait", "seconds": 0.5},
            ]
        }
        code = scripting.macro_to_python(data, "test_agent.json")
        self.assertIn("def run(context):", code)
        self.assertIn("context.click(150, 250", code)
        self.assertIn('if "greeting" in params:', code)
        self.assertIn("context.sleep(0.5)", code)

    def test_execute_python_macro_with_logic(self):
        backend = FakeBackend()
        code = """
def run(context):
    if context.params.get("mode") == "special":
        context.click(500, 600)
        context.type_text("Special Mode")
    else:
        context.type_text("Normal Mode")
"""
        success = scripting.execute_python_macro(code, backend, params={"mode": "special"})
        self.assertTrue(success)
        types = [e.get("text") for e in backend.emitted if e.get("type") == "text"]
        self.assertIn("Special Mode", types)

        backend.emitted.clear()
        success2 = scripting.execute_python_macro(code, backend, params={"mode": "other"})
        self.assertTrue(success2)
        types2 = [e.get("text") for e in backend.emitted if e.get("type") == "text"]
        self.assertIn("Normal Mode", types2)

    def test_execute_python_macro_cancellation(self):
        backend = FakeBackend()
        cancel = threading.Event()
        cancel.set()
        code = """
def run(context):
    context.type_text("Should not type")
"""
        success = scripting.execute_python_macro(code, backend, cancel=cancel)
        self.assertFalse(success)
        self.assertEqual(len(backend.emitted), 0)

    def test_get_save_and_reset_macro_script(self):
        name = "test_script_rec.json"
        data = {
            "format": core.FORMAT,
            "duration": 1.0,
            "events": [
                {"type": "motion", "x": 10, "y": 20, "t": 0.1},
                {"type": "button_down", "button": "left", "x": 10, "y": 20, "t": 0.2},
                {"type": "button_up", "button": "left", "x": 10, "y": 20, "t": 0.25}
            ]
        }
        core.write_recording(name, data)

        # 1. Get initial auto-generated script
        initial = scripting.get_macro_script(name)
        self.assertFalse(initial["custom"])
        self.assertIn("def run(context):", initial["python_code"])

        # 2. Save custom python script
        custom_code = """
def run(context):
    context.type_text("Custom Python Logic")
"""
        saved = scripting.save_macro_script(name, custom_code)
        self.assertTrue(saved["custom"])

        # Verify .py file was created
        py_file = core.safe_path(name).with_suffix(".py")
        self.assertTrue(py_file.exists())
        self.assertIn("Custom Python Logic", py_file.read_text(encoding="utf-8"))

        # Verify inspect shows has_python
        inspect_res = core.inspect(name)
        self.assertTrue(inspect_res["has_python"])

        # 3. Get script now returns custom
        got = scripting.get_macro_script(name)
        self.assertTrue(got["custom"])
        self.assertEqual(got["python_code"], custom_code)

        # 4. Replay executes custom python
        backend = FakeBackend()
        actions.replay_once(backend, name, values={}, countdown=0)
        types = [e.get("text") for e in backend.emitted if e.get("type") == "text"]
        self.assertIn("Custom Python Logic", types)

        # 5. Reset script removes custom logic
        reset_res = scripting.reset_macro_script(name)
        self.assertFalse(reset_res["custom"])
        self.assertFalse(py_file.exists())
        inspect_after_reset = core.inspect(name)
        self.assertFalse(inspect_after_reset["has_python"])

    def test_save_macro_script_syntax_error(self):
        name = "test_syntax.json"
        core.write_recording(name, {"format": core.FORMAT, "duration": 0, "events": []})
        bad_code = "def run(context):\n    if True\n"
        with self.assertRaises(ValueError) as ctx:
            scripting.save_macro_script(name, bad_code)
        self.assertIn("Python syntax error", str(ctx.exception))

    def test_actions_script_dispatch(self):
        name = "test_dispatch.json"
        core.write_recording(name, {"format": core.FORMAT, "duration": 0, "events": []})
        backend = FakeBackend()
        state = actions.MacroState(backend)

        res = actions.dispatch("script", {"name": name, "op": "get"}, backend, state)
        self.assertIn("python_code", res)

        save_res = actions.dispatch("script", {"name": name, "op": "save", "code": "def run(context):\n    pass\n"}, backend, state)
        self.assertTrue(save_res["custom"])

        reset_res = actions.dispatch("script", {"name": name, "op": "reset"}, backend, state)
        self.assertFalse(reset_res["custom"])


if __name__ == "__main__":
    unittest.main()
