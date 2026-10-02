import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from input_reply import lockfile, network
from input_reply.actuator import generate_human_path


class LockfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.test_lock = Path(self.tmp.name) / "test_service.lock"
        self.patcher = patch.object(lockfile, "lock_file_path", return_value=self.test_lock)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_write_and_read_lock(self):
        self.assertIsNone(lockfile.read_service_lock())
        lockfile.write_service_lock(port=8766, host="127.0.0.1", pid=12345)
        self.assertTrue(self.test_lock.is_file())

        data = lockfile.read_service_lock()
        self.assertIsNotNone(data)
        self.assertEqual(data["port"], 8766)
        self.assertEqual(data["host"], "127.0.0.1")
        self.assertEqual(data["pid"], 12345)

    def test_clear_service_lock(self):
        lockfile.write_service_lock(port=8766, host="127.0.0.1", pid=12345)
        # Should not clear if PID does not match
        lockfile.clear_service_lock(only_if_pid=99999)
        self.assertTrue(self.test_lock.is_file())

        # Clears when PID matches
        lockfile.clear_service_lock(only_if_pid=12345)
        self.assertFalse(self.test_lock.is_file())

    def test_get_active_service_port_with_live_lock(self):
        lockfile.write_service_lock(port=8899, host="127.0.0.1", pid=os.getpid())
        with patch.object(lockfile, "probe", return_value=True):
            port = lockfile.get_active_service_port(default=8765)
            self.assertEqual(port, 8899)

    def test_get_active_service_port_with_stale_lock(self):
        lockfile.write_service_lock(port=8899, host="127.0.0.1", pid=os.getpid())
        # If probed port fails, it cleans up stale lock and checks default
        with patch.object(lockfile, "probe", side_effect=lambda p, h="127.0.0.1": p == 8765):
            port = lockfile.get_active_service_port(default=8765)
            self.assertEqual(port, 8765)
            self.assertFalse(self.test_lock.is_file())


class NetworkMonitorTests(unittest.TestCase):
    def test_network_monitor_detects_change_and_triggers_callback(self):
        changes = []
        states = [
            ("host-1", ("192.168.1.10",)),
            ("host-1", ("192.168.1.10",)),
            ("host-1", ("192.168.1.25",)),
        ]
        state_idx = 0

        def fake_capture():
            nonlocal state_idx
            val = states[min(state_idx, len(states) - 1)]
            state_idx += 1
            return val

        with patch.object(network.NetworkMonitor, "capture_state", side_effect=fake_capture):
            mon = network.NetworkMonitor(on_change=lambda old, new: changes.append((old, new)), check_interval=0.04)
            mon.start()
            import time
            time.sleep(0.25)
            mon.stop()
            mon.join(timeout=1.0)

        self.assertTrue(len(changes) >= 1)
        self.assertEqual(changes[0][0], ("host-1", ("192.168.1.10",)))
        self.assertEqual(changes[0][1], ("host-1", ("192.168.1.25",)))


class HumanMovementTests(unittest.TestCase):
    def test_generate_human_path_endpoints_and_smoothing(self):
        start = (100, 100)
        target = (800, 600)
        path = generate_human_path(start, target, steps=20)
        
        self.assertTrue(len(path) >= 20)
        # Final point should match target exactly
        self.assertEqual(path[-1], target)
        # All points should be integers within reasonable bounds
        for x, y in path:
            self.assertIsInstance(x, int)
            self.assertIsInstance(y, int)


if __name__ == "__main__":
    unittest.main()
