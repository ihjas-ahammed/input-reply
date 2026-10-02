import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from input_reply import actions
from input_reply.backends.windows import WindowsBackend


class WindowsFocusTests(unittest.TestCase):
    def backend(self, foreground):
        backend = WindowsBackend.__new__(WindowsBackend)
        backend.user32 = SimpleNamespace(
            IsWindow=Mock(return_value=True), IsIconic=Mock(return_value=False),
            GetForegroundWindow=Mock(side_effect=foreground),
            ShowWindowAsync=Mock(), SetForegroundWindow=Mock(return_value=0))
        backend.active_window = Mock(return_value={"id": "42", "title": "Editor"})
        return backend

    def test_already_active_window_is_not_reactivated(self):
        backend = self.backend([42])
        self.assertEqual(backend.focus("42")["id"], "42")
        backend.user32.SetForegroundWindow.assert_not_called()
        backend.user32.ShowWindowAsync.assert_not_called()

    def test_manual_focus_after_windows_refuses_is_accepted(self):
        backend = self.backend([7, 7, 42])
        with patch("input_reply.backends.windows.time.sleep"):
            self.assertEqual(backend.focus("42")["id"], "42")
        backend.user32.SetForegroundWindow.assert_called_once()

    def test_closed_window_is_rejected(self):
        backend = self.backend([7])
        backend.user32.IsWindow.return_value = False
        with self.assertRaisesRegex(RuntimeError, "closed"):
            backend.focus("42")
        backend.user32.SetForegroundWindow.assert_not_called()

    def test_focus_wait_can_be_cancelled(self):
        backend = self.backend([7, 7])
        cancel = threading.Event()
        cancel.set()
        self.assertIsNone(backend.focus("42", cancel=cancel))

    def test_timeout_does_not_accept_wrong_window(self):
        backend = self.backend([7, 7])
        with self.assertRaisesRegex(RuntimeError, "click the selected window"):
            backend.focus("42", timeout=0)
        backend.active_window.assert_not_called()

    def test_cancelled_focus_never_starts_recording(self):
        backend = self.backend([7])
        backend.focus = Mock(return_value=None)
        backend.record = Mock()
        started = Mock()
        self.assertIsNone(actions.record_once(backend, "test.json", 1, 0,
                                              window_id="42", on_started=started))
        started.assert_not_called()
        backend.record.assert_not_called()

    def test_saved_target_timeout_is_not_retried(self):
        backend = self.backend([7])
        backend._title = Mock(return_value="Editor")
        backend.focus = Mock(side_effect=RuntimeError("Target not activated"))
        backend.windows = Mock()
        with self.assertRaisesRegex(RuntimeError, "not activated"):
            backend.focus_recorded({"target_window": {"id": "42", "title": "Editor"}})
        backend.focus.assert_called_once()
        backend.windows.assert_not_called()


if __name__ == "__main__":
    unittest.main()
