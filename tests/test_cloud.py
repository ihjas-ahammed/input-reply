import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fake_firebase import FakeFirebase
from test_server import FakeBackend

from input_reply import cloud, core, firebase, settings
from input_reply.actions import MacroState

DEMO = {"format": core.FORMAT, "backend": "windows", "duration": 0.2,
        "target_window": {"id": "1", "title": "Test editor"},
        "events": [{"type": "key_down", "key": "h", "t": 0.05},
                   {"type": "key_up", "key": "h", "t": 0.08}]}


def wait_for(condition, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


class CloudTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = patch.object(core, "data_dir", return_value=Path(temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(core.set_account, None)
        self.fake = FakeFirebase()
        self.addCleanup(self.fake.close)
        self.backend = FakeBackend()
        self.state = MacroState(self.backend)
        core.write_recording("demo.json", DEMO)   # recorded before any account existed
        self.service = cloud.CloudService(self.backend, self.state, self.fake.config())
        self.addCleanup(self.service.shutdown)

    def sign_up(self):
        status = self.service.sign_in("me@example.com", "secret123", create=True)
        self.uid = self.service.session.uid
        self.device = cloud.device_id()
        self.base = f"inputReply/users/{self.uid}"
        return status

    def push_command(self, action, args=None, created=None):
        return self.fake.tree.push(f"{self.base}/commands/{self.device}", {
            "action": action, "args": args or {}, "status": "pending",
            "createdAt": created if created is not None else {".sv": "timestamp"}})

    def command(self, key):
        return self.fake.tree.get(f"{self.base}/commands/{self.device}/{key}") or {}

    def test_sign_in_errors_are_readable(self):
        self.sign_up()
        self.service.sign_out()
        with self.assertRaisesRegex(firebase.CloudError, "incorrect"):
            self.service.sign_in("me@example.com", "wrong-password")
        with self.assertRaisesRegex(firebase.CloudError, "already exists"):
            self.service.sign_in("me@example.com", "secret123", create=True)

    def test_recordings_are_scoped_to_the_account(self):
        self.assertEqual([r["name"] for r in core.catalog()], ["demo.json"])
        self.sign_up()
        self.assertEqual([r["name"] for r in core.catalog()], ["demo.json"], "first account adopts old recordings")
        self.service.sign_out()
        self.assertEqual([r["name"] for r in core.catalog()], ["demo.json"], "offline folder is untouched")
        second = cloud.CloudService(self.backend, self.state, self.fake.config())
        self.addCleanup(second.shutdown)
        second.sign_in("other@example.com", "secret123", create=True)
        self.assertEqual(core.catalog(), [], "a second account does not see the first account's data")
        self.assertNotEqual(core.recordings_dir(), core.data_dir() / "recordings")

    def test_session_survives_restart_without_password(self):
        self.sign_up()
        self.service.shutdown()
        text = firebase.Session.path().read_text()
        self.assertNotIn("secret123", text)
        resumed = cloud.CloudService(self.backend, self.state, self.fake.config())
        self.addCleanup(resumed.shutdown)
        core.set_account(None)
        self.assertTrue(resumed.start_saved())
        self.assertEqual(core.current_account(), self.uid)
        wait_for(lambda: resumed.status()["connected"])

    def test_remote_replay_end_to_end(self):
        self.sign_up()
        wait_for(lambda: self.fake.tree.get(f"{self.base}/devices/{self.device}/online") is True)
        catalog = wait_for(lambda: self.fake.tree.get(f"{self.base}/devices/{self.device}/recordings"))
        self.assertEqual(catalog[0]["name"], "demo.json")

        added = self.push_command("parameter_add", {"name": "demo.json", "block": 1, "parameter": "friend"})
        wait_for(lambda: self.command(added).get("status") == "done")
        key = self.push_command("replay", {"name": "demo.json", "countdown": 0, "params": {"friend": "Alex"}})
        wait_for(lambda: self.command(key).get("status") == "done")
        wait_for(lambda: self.backend.typed == ["Alex"])
        self.assertNotIn("params", self.command(key)["args"], "replacement text is removed from the database")
        wait_for(lambda: (self.fake.tree.get(f"{self.base}/devices/{self.device}/job") or {}).get("phase") == "done")
        recordings = wait_for(lambda: [r for r in self.fake.tree.get(f"{self.base}/devices/{self.device}/recordings")
                                       if r["parameters"]])
        self.assertEqual(recordings[0]["parameters"][0]["name"], "friend")

    def test_stale_and_disabled_commands_are_not_run(self):
        self.sign_up()
        wait_for(lambda: self.service.status()["connected"])
        old = self.push_command("replay", {"name": "demo.json", "countdown": 0},
                                created=int((time.time() - 3600) * 1000))
        wait_for(lambda: self.command(old).get("status") == "expired")
        settings.update(remote_enabled=False)
        blocked = self.push_command("replay", {"name": "demo.json", "countdown": 0})
        wait_for(lambda: self.command(blocked).get("status") == "error")
        self.assertIn("turned off", self.command(blocked)["error"])
        settings.update(remote_enabled=True)
        unknown = self.push_command("format_disk")
        wait_for(lambda: self.command(unknown).get("status") == "error")
        self.assertEqual(self.backend.typed, [])
        self.assertEqual(self.backend.emitted, [])

    def test_remote_client_sends_commands(self):
        self.sign_up()
        wait_for(lambda: self.service.status()["connected"])
        with patch.object(cloud, "load_config", return_value=self.fake.config()):
            client = cloud.RemoteClient()
        self.assertEqual(client.resolve(), self.device)
        result = client.send(self.device, "recordings", wait=5)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["result"]["recordings"][0]["name"], "demo.json")

    def test_other_users_cannot_read_a_subtree(self):
        self.sign_up()
        other = firebase.Session(self.fake.config())
        other.sign_in("intruder@example.com", "secret123", create=True)
        db = firebase.Database(other)
        with self.assertRaises(firebase.CloudError):
            db.get(f"{self.base}/devices")

    def test_missing_config_explains_setup(self):
        with patch.dict("os.environ", {}, clear=False):
            with self.assertRaisesRegex(firebase.CloudError, "cloud.example.json"):
                firebase.load_config()


if __name__ == "__main__":
    unittest.main()
