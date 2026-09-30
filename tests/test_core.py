import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from input_reply import core
from input_reply.backends.wayland import WaylandBackend


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_patch = patch.object(core, "data_dir", return_value=Path(self.temp.name))
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)

    def test_parameters_replace_only_named_typing_blocks(self):
        events = [
            {"type": "key_down", "key": "h", "t": 0.1},
            {"type": "key_up", "key": "h", "t": 0.13},
            {"type": "key_down", "key": "apostrophe", "t": 0.2},
            {"type": "key_up", "key": "apostrophe", "t": 0.23},
            {"type": "button_down", "button": "left", "x": 310, "y": 240, "t": 0.4},
            {"type": "button_up", "button": "left", "x": 310, "y": 240, "t": 0.42},
            {"type": "key_down", "key": "m", "t": 0.5},
            {"type": "key_up", "key": "m", "t": 0.53},
        ]
        data = {"format": core.FORMAT, "backend": "windows", "duration": 0.53, "events": events}
        core.write_recording("demo.json", data)
        public = core.inspect("demo.json")
        self.assertEqual([b["keys"] for b in public["blocks"]], [2, 1])
        self.assertNotIn("events", public)
        self.assertNotIn("indices", public["blocks"][0])
        core.add_parameter("demo.json", 1, "friend")
        core.add_parameter("demo.json", 2, "message")
        saved = core.read_recording("demo.json")
        replaced = core.substitute(saved, {"friend": "Alex", "message": ""})
        self.assertEqual([e["type"] for e in replaced],
                         ["text", "button_down", "button_up", "text"])
        self.assertEqual([e["text"] for e in replaced if e["type"] == "text"], ["Alex", ""])
        self.assertEqual(core.substitute(saved, {}), events)
        if os.name != "nt":
            self.assertEqual((core.recordings_dir() / "demo.json").stat().st_mode & 0o777, 0o600)

    def test_wayland_trace_keeps_coordinates_and_chord_blocks(self):
        trace = [
            {"kind": "move_abs", "t_ms": 10, "x": 100, "y": 200},
            {"kind": "key", "t_ms": 100, "chord": "shift+h"},
            {"kind": "key", "t_ms": 120, "chord": "i"},
            {"kind": "click", "t_ms": 200, "button": 1},
            {"kind": "move_delta", "t_ms": 300, "dx": 5, "dy": -2},
            {"kind": "scroll", "t_ms": 400, "dx": 0, "dy": 3},
        ]
        events = WaylandBackend.convert_trace(trace)
        data = {"format": core.FORMAT, "backend": "wayland", "events": events}
        core.validate_recording(data)
        self.assertEqual(events[5]["x"], 100)
        self.assertEqual(events[5]["y"], 200)
        self.assertEqual(events[5]["coordinate_known"], True)
        self.assertEqual(core.typing_blocks(data)[0]["keys"], 2)
        self.assertEqual(events[-2]["type"], "motion_delta")

    def test_replay_drops_repeat_key_down(self):
        events = [{"type": "key_down", "key": "e", "t": 0.1},
                  {"type": "key_down", "key": "e", "t": 0.3},
                  {"type": "key_up", "key": "e", "t": 0.5}]
        normalized = core.normalize_repeats(events)
        self.assertEqual([e["type"] for e in normalized], ["key_down", "key_up"])
        self.assertLessEqual(normalized[1]["t"], 0.16)
        x11_repeat = [{"type": "key_down", "key": "e", "code": 26, "t": 0.1},
                      {"type": "key_up", "key": "e", "code": 26, "t": 0.3},
                      {"type": "key_down", "key": "e", "code": 26, "t": 0.32},
                      {"type": "key_up", "key": "e", "code": 26, "t": 0.5}]
        self.assertEqual([e["type"] for e in core.normalize_repeats(x11_repeat)],
                         ["key_down", "key_up"])


    def test_recordings_are_cached_until_the_file_changes(self):
        events = [{"type": "key_down", "key": "h", "t": 0.1}, {"type": "key_up", "key": "h", "t": 0.13}]
        core.write_recording("a.json", {"format": core.FORMAT, "events": events, "duration": 1})
        first = core.read_recording("a.json")
        self.assertIs(core.read_recording("a.json"), first)
        self.assertIsNot(core.read_recording("a.json", fresh=True), first)
        self.assertEqual(core.catalog()[0]["keys"], 1)
        core.add_parameter("a.json", 1, "friend")
        self.assertNotIn("parameters", first, "editing must not change a shared cached copy")
        self.assertEqual(core.read_recording("a.json")["parameters"], [{"name": "friend", "block": 1}])
        core.write_recording("a.json", {"format": core.FORMAT, "events": events * 2 and events + [
            {"type": "key_down", "key": "i", "t": 0.2}, {"type": "key_up", "key": "i", "t": 0.22}], "duration": 2})
        self.assertEqual(core.catalog()[0]["keys"], 2)
        core.delete_recording("a.json")
        self.assertEqual(core.catalog(), [])

    def test_account_folders_are_separate_and_validated(self):
        self.addCleanup(core.set_account, None)
        core.write_recording("a.json", {"format": core.FORMAT, "events": [], "duration": 1})
        core.set_account("user1")
        self.assertEqual(core.catalog(), [])
        self.assertEqual(core.adopt_legacy_recordings("user1"), 1)
        self.assertEqual(core.adopt_legacy_recordings("user2"), 0, "only the first account adopts old files")
        with self.assertRaises(ValueError):
            core.set_account("../escape")


if __name__ == "__main__":
    unittest.main()
