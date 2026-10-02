"""Network change monitoring and reconnection handling for Input Reply.

Watches for network adapter and IP changes (e.g. Wi-Fi reconnection, VPN toggles,
DHCP renewal) to trigger cloud agent reconnection or service restart.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable


class NetworkMonitor(threading.Thread):
    """Monitors network interface changes in the background."""

    def __init__(self, on_change: Callable[[tuple, tuple], None] | None = None, check_interval: float = 4.0):
        super().__init__(daemon=True, name="InputReplyNetworkMonitor")
        self.on_change = on_change
        self.check_interval = check_interval
        self._stop_event = threading.Event()
        self._last_state = self.capture_state()

    @staticmethod
    def capture_state() -> tuple:
        """Capture current hostname and list of local IP addresses."""
        try:
            hostname = socket.gethostname()
            # Retrieve all IP addresses associated with this host
            ips = tuple(sorted(socket.gethostbyname_ex(hostname)[2]))
            return (hostname, ips)
        except Exception:
            return ()

    def run(self) -> None:
        while not self._stop_event.wait(self.check_interval):
            try:
                current = self.capture_state()
                if self._last_state and current and current != self._last_state:
                    old = self._last_state
                    self._last_state = current
                    if self.on_change:
                        try:
                            self.on_change(old, current)
                        except Exception:
                            pass
                elif not self._last_state and current:
                    self._last_state = current
            except Exception:
                pass

    def stop(self) -> None:
        self._stop_event.set()
