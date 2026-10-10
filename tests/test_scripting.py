import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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
        scripting.execute_python_macro(code, backend, speed=50)
        self.assertEqual(backend.emitted, data["events"])

    def test_macro_to_python_agent(self):
        data = {
            "format": core.FORMAT_AGENT,
            "steps": [
                {"type": "click", "x": 150, "y": 250, "button": "left"},
                {"type": "type", "text": "hello", "param": "greeting"},
                {"type": "wait", "seconds": 0.5},
            ],
            "parameters": [{"name": "greeting"}],
        }
        code = scripting.macro_to_python(data, "test_agent.json")
        self.assertIn("def run(context):", code)
        with patch("input_reply.scripting.MacroContext.play_recording") as play:
            scripting.execute_python_macro(code, FakeBackend(), params={"greeting": "Hi"})
        self.assertEqual(play.call_args.args[0], data)

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

    def test_parameters_added_after_recording_apply_with_generated_python(self):
        backend = FakeBackend()
        name = "parameter-regression.json"
        actions.record_once(backend, name, 1, 0, window_id="12345")
        self.assertFalse(scripting.get_macro_script(name)["custom"])
        core.add_parameter(name, 1, "recipient")
        actions.replay_once(backend, name, {"recipient": "Alex"}, 0, speed=50)
        self.assertEqual([e["text"] for e in backend.emitted if e["type"] == "text"], ["Alex"])
        self.assertFalse(any(e["type"].startswith("key_") for e in backend.emitted))
        backend.emitted.clear()
        actions.replay_once(backend, name, {"recipient": ""}, 0, speed=50)
        self.assertFalse(any(e["type"].startswith("key_") for e in backend.emitted))
        core.remove_parameter(name, "recipient")
        backend.emitted.clear()
        actions.replay_once(backend, name, {}, 0, speed=50)
        self.assertTrue(any(e.get("key") == "h" for e in backend.emitted))

    def test_old_generated_python_uses_updated_json_parameters(self):
        backend = FakeBackend()
        name = "old-generated.json"
        data = backend.record(1, "unused")
        core.write_recording(name, data)
        core.safe_path(name).with_suffix(".py").write_text(scripting._legacy_macro_to_python(data, name))
        # Reproduce files created before the fix: only JSON was updated.
        data["parameters"] = [{"name": "friend", "block": 1}]
        core.write_recording(name, data)
        actions.replay_once(backend, "old-generated", {"friend": "New text"}, 0, speed=50)
        self.assertIn({"type": "text", "text": "New text"}, backend.emitted)
        self.assertFalse(scripting.get_macro_script(name)["custom"])

    def test_custom_script_is_preserved_when_parameters_change(self):
        backend = FakeBackend()
        name = "custom.json"
        core.write_recording(name, backend.record(1, "unused"))
        code = "def run(context):\n    context.type_text(context.params['friend'])\n"
        scripting.save_macro_script(name, code)
        core.add_parameter(name, 1, "friend")
        self.assertEqual(scripting.get_macro_script(name)["python_code"], code)
        actions.replay_once(backend, name, {"friend": "Alex"}, 0)
        self.assertEqual(backend.emitted, [{"type": "text", "text": "Alex"}])

    def test_saved_generated_template_uses_current_parameters(self):
        backend = FakeBackend()
        name = "saved-template.json"
        core.write_recording(name, backend.record(1, "unused"))
        scripting.save_macro_script(name, scripting.get_macro_script(name)["python_code"])
        core.add_parameter(name, 1, "friend")
        actions.replay_once(backend, name, {"friend": "Alex"}, 0, speed=50)
        self.assertIn({"type": "text", "text": "Alex"}, backend.emitted)

    def test_external_python_edits_are_detected_and_catalog_updates(self):
        backend = FakeBackend()
        name = "external.json"
        actions.record_once(backend, name, 1, 0)
        self.assertFalse(core.catalog()[0]["custom_python"])
        path = core.safe_path(name).with_suffix(".py")
        path.write_text(path.read_text() + "\n# my edit\n")
        self.assertTrue(core.catalog()[0]["custom_python"])
        self.assertTrue(scripting.get_macro_script(name)["custom"])

    def test_generated_script_preserves_modifier_backspace_drag_and_scroll(self):
        backend = FakeBackend()
        data = backend.record(1, "unused")
        data["events"] = [
            {"type": "key_down", "key": "shift", "t": 0},
            {"type": "key_down", "key": "a", "t": 0},
            {"type": "key_up", "key": "a", "t": 0},
            {"type": "key_up", "key": "shift", "t": 0},
            {"type": "key_down", "key": "backspace", "t": 0},
            {"type": "key_up", "key": "backspace", "t": 0},
            {"type": "button_down", "button": "left", "x": 10, "y": 20, "t": 0},
            {"type": "motion", "x": 100, "y": 200, "t": 0},
            {"type": "button_up", "button": "left", "x": 100, "y": 200, "t": 0},
            {"type": "scroll", "dx": 0, "dy": 1, "t": 0},
        ]
        scripting.execute_python_macro(scripting.macro_to_python(data), backend)
        self.assertEqual(backend.emitted, data["events"])

    def test_x11_python_actions_use_native_keys_buttons_and_focus(self):
        backend = Mock(name="backend")
        backend.name = "x11"
        backend.focus.return_value = {"id": "42", "title": "Editor"}
        backend.windows.return_value = [{"id": "42", "title": "Editor"}]
        context = scripting.MacroContext(backend, speed=50)
        context.click(10, 20)
        context.press_key("enter")
        context.hotkey("ctrl", "a")
        self.assertEqual(context.focus("42")["id"], "42")
        self.assertEqual(context.focus("Editor")["id"], "42")
        backend.focus.assert_called_with("42")
        calls = [call.args for call in backend.cmd.call_args_list]
        self.assertIn(("mousedown", "1"), calls)
        self.assertIn(("mouseup", "1"), calls)
        self.assertIn(("keydown", "Return"), calls)
        self.assertIn(("keyup", "Control_L"), calls)

    def test_python_failure_releases_held_input(self):
        backend = FakeBackend()
        with self.assertRaisesRegex(RuntimeError, "boom"):
            scripting.execute_python_macro("def run(context):\n    context.key_down('ctrl')\n    context.mouse_down()\n    raise ValueError('boom')\n", backend)
        self.assertEqual(backend.emitted[-2:], [{"type": "key_up", "key": "ctrl"}, {"type": "button_up", "button": "left"}])

    def test_player_open_failure_and_invalid_params_are_reported(self):
        backend = FakeBackend()
        backend.open_player = Mock(side_effect=RuntimeError("Cannot open display"))
        with self.assertRaisesRegex(RuntimeError, "Cannot open display"):
            scripting.execute_python_macro("def run(context):\n    pass\n", backend)
        with self.assertRaisesRegex(ValueError, "JSON object"):
            scripting.execute_python_macro("pass", backend, params=["bad"])

    def test_wayland_shortcuts_are_sent_as_one_chord(self):
        backend = Mock()
        backend.name = "wayland"
        context = scripting.MacroContext(backend)
        context.hotkey("ctrl", "shift", "s")
        context.press_key("enter")
        self.assertEqual([call.args for call in backend._run.call_args_list],
                         [("key", "ctrl+shift+s"), ("key", "enter")])
        with self.assertRaisesRegex(RuntimeError, "Dragging is not supported"):
            context.drag(1, 2, 3, 4)

    def test_agent_playback_keeps_script_controllers_open(self):
        backend = FakeBackend()
        backend.open_player = Mock()
        backend.close_player = Mock()
        data = {"format": core.FORMAT_AGENT, "steps": [{"type": "type", "text": "Hi"}], "parameters": []}
        code = scripting.macro_to_python(data) + "\noriginal_run = run\ndef run(context):\n    original_run(context)\n    context.type_text('after')\n"
        with patch("input_reply.actuator.Actuator.screen_size", return_value=None), \
             patch("input_reply.actuator.Actuator.perform"):
            scripting.execute_python_macro(code, backend, speed=50)
        backend.open_player.assert_called_once()
        backend.close_player.assert_called_once()
        self.assertIn({"type": "text", "text": "after"}, backend.emitted)

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
