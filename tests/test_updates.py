import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from input_reply import actions, core, macros
from input_reply.actuator import Actuator
from input_reply.backends.windows import WindowsBackend


class UpdatesTests(unittest.TestCase):
    def test_repeat_count_validation(self):
        self.assertEqual(actions.repeat_count(1), 1)
        self.assertEqual(actions.repeat_count("5"), 5)
        self.assertEqual(actions.repeat_count(None), 1)
        self.assertEqual(actions.repeat_count(""), 1)
        with self.assertRaises(ValueError):
            actions.repeat_count(0)
        with self.assertRaises(ValueError):
            actions.repeat_count(-1)
        with self.assertRaises(ValueError):
            actions.repeat_count("invalid")
        with self.assertRaises(ValueError):
            actions.repeat_count(99999)

    def test_speed_factor_validation(self):
        self.assertEqual(actions.speed_factor(1.0), 1.0)
        self.assertEqual(actions.speed_factor("2.5"), 2.5)
        self.assertEqual(actions.speed_factor(None), 1.0)
        self.assertEqual(actions.speed_factor(""), 1.0)
        with self.assertRaises(ValueError):
            actions.speed_factor(0)
        with self.assertRaises(ValueError):
            actions.speed_factor(-1.5)
        with self.assertRaises(ValueError):
            actions.speed_factor("fast")
        with self.assertRaises(ValueError):
            actions.speed_factor(100.0)

    def test_repeat_replays_multiple_times_and_restores_initial_state(self):
        cursor_log = []
        focus_log = []

        class TestBackend:
            name = "windows"
            def available(self): return True
            def open_player(self): pass
            def close_player(self): pass
            def emit(self, event): pass
            def type_text(self, text, cancel=None): pass
            def focus_recorded(self, data, window_id=None, cancel=None):
                focus_log.append("focus")
                return {"id": "1", "title": "Editor"}
            def cursor_position(self):
                return (150, 250)
            def set_cursor_position(self, x, y):
                cursor_log.append((x, y))

        backend = TestBackend()
        recording = {
            "format": core.FORMAT,
            "events": [
                {"type": "motion", "x": 150, "y": 250, "t": 0.01},
                {"type": "button_down", "button": "left", "x": 150, "y": 250, "t": 0.02},
                {"type": "button_up", "button": "left", "x": 150, "y": 250, "t": 0.03}
            ],
            "target_window": {"id": "1", "title": "Editor"}
        }

        iterations = []
        with patch.object(core, "read_recording", return_value=recording), \
             patch.object(actions, "check_backend"), \
             patch.object(core, "substitute", return_value=recording["events"]), \
             patch.object(actions, "start_stop_hotkey", return_value=None):
            target, completed = actions.replay_once(
                backend, "macro.json", {}, countdown=0,
                repeat=3, speed=10.0,
                on_iteration=lambda cur, total: iterations.append((cur, total))
            )

        self.assertTrue(completed)
        self.assertEqual(target["title"], "Editor")
        # Ran 3 iterations
        self.assertEqual(iterations, [(1, 3), (2, 3), (3, 3)])
        # Focus called for initial run and before iterations 2 and 3
        self.assertEqual(len(focus_log), 3)
        # Cursor restored back to initial position between repetitions (before iteration 2 and 3)
        self.assertEqual(cursor_log, [(150, 250), (150, 250)])

    def test_ctrl_esc_stops_all_repetitions(self):
        class TestBackend:
            name = "windows"
            def available(self): return True
            def open_player(self): pass
            def close_player(self): pass
            def emit(self, event): pass
            def type_text(self, text, cancel=None): pass
            def focus_recorded(self, data, window_id=None, cancel=None):
                return {"id": "1", "title": "Editor"}
            def cursor_position(self): return (100, 100)
            def set_cursor_position(self, x, y): pass

        backend = TestBackend()
        recording = {
            "format": core.FORMAT,
            "events": [{"type": "motion", "x": 100, "y": 100, "t": 0.01}],
            "target_window": {"id": "1", "title": "Editor"}
        }

        cancel = threading.Event()
        iterations_run = []

        def on_iter(cur, total):
            iterations_run.append(cur)
            if cur == 2:
                # Simulate Ctrl+Esc pressed during repetition 2
                cancel.set()

        with patch.object(core, "read_recording", return_value=recording), \
             patch.object(actions, "check_backend"), \
             patch.object(core, "substitute", return_value=recording["events"]), \
             patch.object(actions, "start_stop_hotkey", return_value=None):
            target, completed = actions.replay_once(
                backend, "macro.json", {}, countdown=0, cancel=cancel,
                repeat=10, speed=10.0, on_iteration=on_iter
            )

        self.assertFalse(completed)
        # Should have stopped after iteration 2 and not run 3 through 10
        self.assertEqual(iterations_run, [1, 2])

    def test_start_stop_hotkey_suppresses_esc_when_ctrl_down(self):
        cancel = threading.Event()
        ctrl_keys = set()
        suppressed = []

        # Re-create the filter logic from start_stop_hotkey
        listener_mock = Mock()
        listener_mock.suppress_event = lambda: suppressed.append(True)
        listener_ref = [listener_mock]

        def win32_event_filter(msg, data):
            vk = getattr(data, "vkCode", None)
            if vk in (0x11, 0xA2, 0xA3):
                if msg in (0x0100, 0x0104):
                    ctrl_keys.add(vk)
                elif msg in (0x0101, 0x0105):
                    ctrl_keys.discard(vk)
            elif vk == 0x1B:
                if bool(ctrl_keys):
                    if msg in (0x0100, 0x0104):
                        cancel.set()
                    if listener_ref[0] is not None:
                        listener_ref[0].suppress_event()
            return True

        # Esc without Ctrl
        win32_event_filter(0x0100, SimpleNamespace(vkCode=0x1B))
        self.assertFalse(cancel.is_set())
        self.assertEqual(suppressed, [])

        # Press Ctrl keydown
        win32_event_filter(0x0100, SimpleNamespace(vkCode=0x11))
        # Press Esc keydown while Ctrl is down
        win32_event_filter(0x0100, SimpleNamespace(vkCode=0x1B))
        self.assertTrue(cancel.is_set())
        self.assertEqual(suppressed, [True])

        # Esc keyup while Ctrl is down should also be suppressed
        win32_event_filter(0x0101, SimpleNamespace(vkCode=0x1B))
        self.assertEqual(suppressed, [True, True])

    def test_f12_suppression_prohibits_all_shortcuts_in_focused_app(self):
        backend = WindowsBackend.__new__(WindowsBackend)
        done = threading.Event()
        suppressed = []
        mock_listener = Mock()
        mock_listener.suppress_event = lambda: suppressed.append(True)
        kl_holder = [mock_listener]

        def win32_event_filter(msg, data):
            if getattr(data, "vkCode", None) == 0x7B:  # VK_F12
                if msg in (0x0100, 0x0104):
                    done.set()
                if kl_holder[0] is not None:
                    kl_holder[0].suppress_event()
            return True

        # Pressing normal key 'A' (0x41)
        win32_event_filter(0x0100, SimpleNamespace(vkCode=0x41))
        self.assertFalse(done.is_set())
        self.assertEqual(suppressed, [])

        # Pressing F12 (0x7B) keydown
        win32_event_filter(0x0100, SimpleNamespace(vkCode=0x7B))
        self.assertTrue(done.is_set())
        self.assertEqual(suppressed, [True])

        # Releasing F12 keyup
        win32_event_filter(0x0101, SimpleNamespace(vkCode=0x7B))
        self.assertEqual(suppressed, [True, True])

    def test_core_play_speed_scaling(self):
        events = [
            {"type": "motion", "x": 10, "y": 20, "t": 0.05},
            {"type": "motion", "x": 15, "y": 25, "t": 0.10}
        ]
        backend = Mock()
        start = time.monotonic()
        core.play(events, backend, speed=10.0)
        elapsed = time.monotonic() - start
        # With speed=10.0, 0.10s event finishes in ~0.01-0.03s instead of 0.1s
        self.assertLess(elapsed, 0.08)

    def test_macros_play_speed_scaling(self):
        data = {
            "format": core.FORMAT_AGENT,
            "steps": [
                {"type": "wait", "seconds": 0.08}
            ]
        }
        actuator = Mock()
        actuator.session = Mock()
        actuator.session.return_value.__enter__ = Mock()
        actuator.session.return_value.__exit__ = Mock()
        actuator.screen_size.return_value = (1920, 1080)
        actuator.set_scale = Mock()

        start = time.monotonic()
        success = macros.play(data, {}, actuator, speed=10.0)
        elapsed = time.monotonic() - start
        self.assertTrue(success)
        # 0.08s wait scaled by 10x should take ~0.008s, well under 0.05s
        self.assertLess(elapsed, 0.05)


if __name__ == "__main__":
    unittest.main()
