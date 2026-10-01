"""Regression coverage for countdown visibility, cancelled speech, and desktop scaling."""
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from input_reply import actions, ai, core
from input_reply.actuator import Actuator


class AuditRegressionTests(unittest.TestCase):
    def test_countdown_remains_visible_until_operation_starts(self):
        for kind in ("record", "replay", "agent"):
            with self.subTest(kind=kind):
                self._check_countdown(kind)

    def _check_countdown(self, kind):
        state = actions.MacroState(SimpleNamespace(name="x11"))
        entered, proceed = threading.Event(), threading.Event()
        phases = []

        def countdown(value, cancel):
            entered.set()
            assert proceed.wait(2)
            return not cancel.is_set()

        def operation(*args):
            phases.append(state.job["phase"])
            return {} if kind == "record" else True

        backend = state.backend
        backend.active_window = lambda: {"title": "test"}
        backend.record = operation
        backend.focus_recorded = lambda *args: {"title": "test"}
        backend.open_player = lambda: None
        backend.close_player = lambda: None
        recording = {"format": core.FORMAT_AGENT if kind == "agent" else core.FORMAT, "events": []}
        with patch.object(actions, "wait_countdown", countdown), patch.object(core, "safe_path"), \
             patch.object(core, "write_recording"), patch.object(core, "read_recording", return_value=recording), \
             patch.object(actions.macros, "resolve"), patch.object(actions.macros, "play", side_effect=operation), \
             patch.object(core, "substitute", return_value=[]), patch.object(core, "play", side_effect=operation):
            state.begin("record" if kind == "record" else "replay", "test", countdown=3)
            assert entered.wait(2)
            assert state.job["phase"] == "countdown"
            proceed.set()
            deadline = time.monotonic() + 2
            while state.busy() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert state.job["phase"] == "done"
            assert phases == ["recording" if kind == "record" else "replaying"]

    def test_cancelled_countdown_never_records(self):
        backend = SimpleNamespace(name="x11", record=Mock())
        state = actions.MacroState(backend)
        state.begin("record", "test", countdown=10)
        state.stop()
        deadline = time.monotonic() + 2
        while state.busy() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert state.job["phase"] == "cancelled"
        backend.record.assert_not_called()

    def test_player_stop_kills_active_process_drops_queue_and_can_restart(self):
        started = threading.Event()
        processes = []

        real_popen = subprocess.Popen

        def fake_spawn(*args, **kwargs):
            process = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            processes.append(process)
            started.set()
            return process

        with patch.object(ai.shutil, "which", return_value="player"), \
             patch.object(ai.subprocess, "Popen", fake_spawn), patch.object(ai.sys, "platform", "linux"):
            player = ai.Player()
            try:
                player.play(b"\0\0" * 100)
                assert started.wait(2)
                player.play(b"\0\0" * 100)
                player.play(b"\0\0" * 100)
                player.stop()
                player.queue.join()
                assert len(processes) == 1
                assert processes[0].poll() is not None
                assert player.queue.empty()
                assert player.error is None
                started.clear()
                player.play(b"\0\0" * 100)
                assert started.wait(2)
            finally:
                player.stop()
                player.queue.join()
            assert len(processes) == 2
            assert all(process.poll() is not None for process in processes)

    def test_windows_stop_purges_async_speech(self):
        started = threading.Event()
        sounds = []

        def play_sound(sound, flags):
            sounds.append(sound)
            if sound is not None:
                assert Path(sound).is_file()
                started.set()

        winsound = SimpleNamespace(PlaySound=play_sound, SND_FILENAME=1, SND_ASYNC=2)
        with patch.dict(sys.modules, {"winsound": winsound}), patch.object(ai.sys, "platform", "win32"):
            player = ai.Player()
            try:
                player.play(b"\0\0" * 240000)
                assert started.wait(2)
                player.play(b"\0\0" * 240000)
                player.stop()
                player.queue.join()
            finally:
                player.stop()
            assert sum(sound is not None for sound in sounds) == 1
            assert sounds[-1] is None
            assert player.error is None

    def test_stop_also_clears_speech_after_ai_turn_finishes(self):
        state = actions.MacroState(SimpleNamespace(name="x11"))
        stop = Mock()
        event = state.claim("ai", "assistant", "Working", on_stop=stop)
        state.release("done", "Finished")
        assert state.stop()
        assert event.is_set()
        stop.assert_called_once()

    def test_unknown_action_has_explicit_error(self):
        assistant = ai.Assistant(None, None, player=None, actuator=Mock())
        with self.assertRaisesRegex(ValueError, "Unknown action: unexpected"):
            assistant._step("unexpected", {})
        assert assistant._step("focus_window", {"title": "Editor"}) == {"type": "focus", "title": "Editor"}

    def test_wayland_screen_size_scales_macro_coordinates(self):
        actuator = Actuator(SimpleNamespace(name="wayland"))
        with patch.object(actuator, "screenshot", return_value=Image.new("RGB", (2560, 1440))):
            actuator.set_scale((1280, 720), actuator.screen_size())
        assert actuator._xy(100, 200) == (200, 400)

    def test_wayland_size_unavailable_does_not_break_replay(self):
        actuator = Actuator(SimpleNamespace(name="wayland"))
        with patch.object(actuator, "screenshot", side_effect=RuntimeError("no capture tool")):
            assert actuator.screen_size() is None
