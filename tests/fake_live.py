"""Scripted stand-in for the Gemini Live WebSocket API."""

import base64
import json
import threading
from collections import deque

from websockets.sync.server import serve


def call(tool, /, **args):
    return {"toolCall": {"functionCalls": [{"id": f"call-{tool}", "name": tool, "args": args}]}}


def say(text, samples=240):
    pcm = b"\x01\x00" * samples
    return {"serverContent": {"modelTurn": {"parts": [{"inlineData": {
        "mimeType": "audio/pcm;rate=24000", "data": base64.b64encode(pcm).decode()}}]},
        "outputTranscription": {"text": text}}}


DONE = {"serverContent": {"turnComplete": True}}


class FakeLive:
    """``script`` is a list of batches. Each user message or tool response releases the next batch."""

    def __init__(self, script):
        self.script = deque(script)
        self.received = []
        self.setup = None
        self.connections = 0
        self.lock = threading.Lock()
        self.server = serve(self.handle, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self.server.socket.getsockname()[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def extend(self, script):
        with self.lock:
            self.script.extend(script)

    def handle(self, ws):
        self.connections += 1
        self.setup = json.loads(ws.recv())["setup"]
        ws.send(json.dumps({"setupComplete": {}}))
        for raw in ws:
            message = json.loads(raw)
            self.received.append(message)
            realtime = message.get("realtimeInput", {})
            if "text" in realtime or "toolResponse" in message:
                with self.lock:
                    batch = self.script.popleft() if self.script else [DONE]
                for item in batch:
                    ws.send(json.dumps(item(self) if callable(item) else item))

    def frames(self):
        return [m for m in self.received if "video" in m.get("realtimeInput", {})]

    def texts(self):
        return [m["realtimeInput"]["text"] for m in self.received if "text" in m.get("realtimeInput", {})]

    def tool_responses(self):
        return [r for m in self.received if "toolResponse" in m for r in m["toolResponse"]["functionResponses"]]

    def close(self):
        self.server.shutdown()
