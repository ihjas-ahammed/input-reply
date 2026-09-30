"""Minimal Firebase Auth and Realtime Database client using only the standard library.

Both services expose REST APIs, so the desktop agent needs no Firebase SDK.
Realtime Database streams changes over server-sent events, which delivers remote
commands immediately without polling.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import core

IDENTITY = "https://identitytoolkit.googleapis.com/v1"
SECURETOKEN = "https://securetoken.googleapis.com/v1"
ROOT = "inputReply"

FRIENDLY = {
    "EMAIL_EXISTS": "An account with this email already exists. Sign in instead.",
    "EMAIL_NOT_FOUND": "Email or password is incorrect.",
    "INVALID_PASSWORD": "Email or password is incorrect.",
    "INVALID_LOGIN_CREDENTIALS": "Email or password is incorrect.",
    "INVALID_EMAIL": "Enter a valid email address.",
    "MISSING_PASSWORD": "Enter a password.",
    "USER_DISABLED": "This account has been disabled.",
    "TOO_MANY_ATTEMPTS_TRY_LATER": "Too many attempts. Wait a few minutes and try again.",
    "OPERATION_NOT_ALLOWED": "Email/password sign-in is not enabled for this Firebase project.",
    "WEAK_PASSWORD": "Password must be at least 6 characters.",
}


class CloudError(RuntimeError):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status, self.code = status, code


def load_config() -> dict:
    """Firebase web config from $INPUT_REPLY_FIREBASE_CONFIG or <data folder>/cloud.json.

    The values are not stored in this repository; see firebase/cloud.example.json.
    """
    candidates = [os.environ.get("INPUT_REPLY_FIREBASE_CONFIG"), core.data_dir() / "cloud.json"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            config = json.loads(Path(candidate).read_text(encoding="utf-8"))
            break
    else:
        raise CloudError(f"No Firebase config found. Copy firebase/cloud.example.json to "
                         f"{core.data_dir() / 'cloud.json'} and fill in your project's web config.")
    for key in ("apiKey", "databaseURL"):
        if not isinstance(config.get(key), str) or not config[key] or config[key].startswith("YOUR_"):
            raise CloudError(f"Firebase config is missing {key}")
    config["databaseURL"] = config["databaseURL"].rstrip("/")
    return config


def _error_code(body: bytes) -> str:
    try:
        message = json.loads(body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return ""
    return str(message).split(" ", 1)[0].split(":", 1)[0]


def _open(request, timeout):
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        body = error.read()
        error.close()
        code = _error_code(body)
        detail = body.decode("utf-8", "replace")[:200]
        raise CloudError(FRIENDLY.get(code) or (code and code.replace("_", " ").capitalize()) or
                         f"Firebase returned HTTP {error.code}: {detail}", error.code, code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise CloudError(f"Cannot reach Firebase: {getattr(error, 'reason', error)}") from None


def _json_post(url, payload, timeout=15, form=False):
    if form:
        data, kind = urllib.parse.urlencode(payload).encode(), "application/x-www-form-urlencoded"
    else:
        data, kind = json.dumps(payload).encode(), "application/json"
    request = urllib.request.Request(url, data=data, headers={"Content-Type": kind}, method="POST")
    with _open(request, timeout) as response:
        return json.load(response)


class Session:
    """Signed-in Firebase user. Only the refresh token is stored, never the password."""

    def __init__(self, config: dict):
        self.config = config
        self.lock = threading.Lock()
        self.uid = self.email = self.refresh_token = self._id_token = None
        self._expires = 0.0
        self.identity = config.get("identityUrl", IDENTITY).rstrip("/")
        self.securetoken = config.get("secureTokenUrl", SECURETOKEN).rstrip("/")

    @staticmethod
    def path() -> Path:
        return core.data_dir() / "session.json"

    @property
    def signed_in(self) -> bool:
        return bool(self.refresh_token)

    def _key(self):
        return urllib.parse.quote(self.config["apiKey"], safe="")

    def _adopt(self, reply: dict):
        self.uid = reply.get("localId") or reply.get("user_id") or self.uid
        self.email = reply.get("email") or self.email
        self.refresh_token = reply.get("refreshToken") or reply.get("refresh_token") or self.refresh_token
        self._id_token = reply.get("idToken") or reply.get("id_token")
        self._expires = time.monotonic() + int(reply.get("expiresIn") or reply.get("expires_in") or 3600)
        if not self.uid or not self._id_token:
            raise CloudError("Firebase returned an incomplete sign-in response")

    def _save(self):
        destination = self.path()
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as out:
            json.dump({"uid": self.uid, "email": self.email, "refresh_token": self.refresh_token}, out)
        if os.name != "nt":
            os.chmod(destination, 0o600)

    def sign_in(self, email: str, password: str, create=False):
        if not isinstance(email, str) or not isinstance(password, str) or not email or not password:
            raise CloudError("Enter your email and password.")
        endpoint = "accounts:signUp" if create else "accounts:signInWithPassword"
        reply = _json_post(f"{self.identity}/{endpoint}?key={self._key()}",
                           {"email": email.strip(), "password": password, "returnSecureToken": True})
        with self.lock:
            self._adopt(reply)
            self._save()

    def reset_password(self, email: str):
        if not isinstance(email, str) or not email.strip():
            raise CloudError("Enter your email address.")
        _json_post(f"{self.identity}/accounts:sendOobCode?key={self._key()}",
                   {"requestType": "PASSWORD_RESET", "email": email.strip()})

    def restore(self) -> bool:
        try:
            saved = json.loads(self.path().read_text(encoding="utf-8"))
            self.uid, self.email, self.refresh_token = saved["uid"], saved.get("email"), saved["refresh_token"]
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return bool(self.refresh_token)

    def sign_out(self):
        with self.lock:
            self.uid = self.email = self.refresh_token = self._id_token = None
            self._expires = 0.0
            self.path().unlink(missing_ok=True)

    def id_token(self, force=False) -> str:
        with self.lock:
            if not self.refresh_token:
                raise CloudError("Not signed in", code="NOT_SIGNED_IN")
            if force or not self._id_token or time.monotonic() > self._expires - 120:
                try:
                    reply = _json_post(f"{self.securetoken}/token?key={self._key()}",
                                       {"grant_type": "refresh_token", "refresh_token": self.refresh_token}, form=True)
                except CloudError as error:
                    if error.code in {"TOKEN_EXPIRED", "USER_DISABLED", "USER_NOT_FOUND", "INVALID_REFRESH_TOKEN"}:
                        error.code = "SESSION_REVOKED"
                    raise
                self._adopt(reply)
                self._save()
            return self._id_token


SAFE_PATH = re.compile(r"^[A-Za-z0-9_\-./]+$")


class Database:
    """Realtime Database REST access as the signed-in user, so security rules apply."""

    def __init__(self, session: Session):
        self.session = session
        self.base = session.config["databaseURL"]
        self.server_offset = 0.0   # server clock minus local clock, seconds

    def _url(self, path, token, **query):
        if not SAFE_PATH.fullmatch(path) or ".." in path:
            raise ValueError("Invalid database path")
        query = {k: v for k, v in query.items() if v is not None} | {"auth": token}
        return f"{self.base}/{path.strip('/')}.json?{urllib.parse.urlencode(query)}"

    def server_time(self) -> float:
        return time.time() + self.server_offset

    def _note_clock(self, response):
        try:
            stamp = parsedate_to_datetime(response.headers["Date"]).timestamp()
            self.server_offset = stamp - time.time()
        except (KeyError, TypeError, ValueError):
            pass

    def request(self, method, path, body=None, timeout=20, **query):
        for attempt in (0, 1):
            token = self.session.id_token(force=attempt == 1)
            data = None if body is None else json.dumps(body).encode()
            request = urllib.request.Request(self._url(path, token, **query), data=data, method=method,
                                             headers={"Content-Type": "application/json"} if data else {})
            try:
                with _open(request, timeout) as response:
                    self._note_clock(response)
                    return json.load(response)
            except CloudError as error:
                if error.status == 401 and attempt == 0:
                    continue
                if error.status in (401, 403):
                    raise CloudError("Firebase rejected this request. Deploy the database rules from "
                                     "firebase/database.rules.json and check you are signed in.",
                                     error.status) from None
                raise

    def get(self, path, **query):
        return self.request("GET", path, **query)

    def put(self, path, value):
        return self.request("PUT", path, value)

    def patch(self, path, value):
        return self.request("PATCH", path, value)

    def delete(self, path):
        return self.request("DELETE", path)

    def push(self, path, value):
        return self.request("POST", path, value)["name"]

    def stream(self, path, handle, stop: threading.Event):
        """Deliver ``handle(event, path, data)`` for a path until ``stop`` is set. Reconnects on failure."""
        delay = 1.0
        while not stop.is_set():
            try:
                token = self.session.id_token()
                request = urllib.request.Request(self._url(path, token), headers={"Accept": "text/event-stream"})
                with _open(request, 75) as response:
                    self._note_clock(response)
                    delay = 1.0
                    event = None
                    while not stop.is_set():
                        line = response.readline()
                        if not line:
                            break
                        line = line.decode("utf-8").rstrip("\r\n")
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:") and event:
                            payload = line[5:].strip()
                            if event in {"cancel", "auth_revoked"}:
                                break
                            if event in {"put", "patch"}:
                                body = json.loads(payload)
                                handle(event, body.get("path", "/"), body.get("data"))
                            event = None
            except CloudError as error:
                if error.code == "SESSION_REVOKED":
                    raise
                if error.status in (401, 403):
                    self.session.id_token(force=True)
            except (OSError, ValueError):
                pass
            stop.wait(delay)
            delay = min(delay * 2, 30.0)
