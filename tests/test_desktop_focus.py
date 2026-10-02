import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from input_reply import desktop


class DesktopFocusTests(unittest.TestCase):
    def test_foreground_permission_is_granted_only_to_local_service(self):
        allow = Mock(return_value=1)
        native = SimpleNamespace(user32=SimpleNamespace(AllowSetForegroundWindow=allow))
        with patch.object(desktop, "os", SimpleNamespace(name="nt")), \
             patch.object(desktop, "access_token", return_value="test-token"), \
             patch.object(desktop.urllib.request, "urlopen", return_value=io.BytesIO(b'{"pid": 42}')) as request, \
             patch.object(desktop.ctypes, "windll", native, create=True):
            self.assertTrue(desktop.DesktopBridge(8765).allow_focus())
        allow.assert_called_once_with(42)
        self.assertEqual(request.call_args.args[0].full_url,
                         "http://127.0.0.1:8765/api/desktop-process")

    def test_bad_pid_is_never_granted_permission(self):
        with patch.object(desktop, "os", SimpleNamespace(name="nt")), \
             patch.object(desktop, "access_token", return_value="test-token"), \
             patch.object(desktop.urllib.request, "urlopen", return_value=io.BytesIO(b'{"pid": -1}')):
            self.assertFalse(desktop.DesktopBridge(8765).allow_focus())

    def test_service_failure_keeps_manual_focus_available(self):
        with patch.object(desktop, "os", SimpleNamespace(name="nt")), \
             patch.object(desktop, "access_token", return_value="test-token"), \
             patch.object(desktop.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertFalse(desktop.DesktopBridge(8765).allow_focus())
