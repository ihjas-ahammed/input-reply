"""Check the assistant against the REAL Gemini Live API. Needs a key; not part of the unit tests.

    GEMINI_API_KEY=... python tests/live_check.py [--model gemini-3.8-flash-live]

Uses the key from GEMINI_API_KEY or the one saved in Settings. It never prints the key. It confirms, in order:
  1. the models list, and whether the chosen model supports Live (bidiGenerateContent)
  2. the WebSocket connection and setup message are accepted
  3. a tool call arrives
  4. a frame sent as realtimeInput video, then our tool response, is understood (the assistant's real flow)
  5. spoken audio and a text transcript come back
"""

import argparse
import base64
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from input_reply import ai, settings  # noqa: E402


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    return ok


def _reads_427(text):
    import re
    words = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return bool(re.search(r"\b4\b.*\b2\b.*\b7\b", words) or re.search(r"\bfour\b.*\btwo\b.*\bseven\b", words) or "427" in words)


def collect(session, timeout=60, after_tool=False):
    """Read one model turn. Returns (tool_calls, transcript, audio_bytes)."""
    calls, text, audio, deadline = [], "", 0, time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = session.recv(1.0)
        if message is None:
            continue
        if "serverContent" in message:
            content = message["serverContent"]
            for part in (content.get("modelTurn") or {}).get("parts", []):
                inline = part.get("inlineData")
                if inline and inline.get("mimeType", "").startswith("audio/"):
                    audio += len(base64.b64decode(inline["data"]))
            text += (content.get("outputTranscription") or {}).get("text", "")
            if content.get("turnComplete"):
                if after_tool and not text and not audio:
                    after_tool = False   # the API's empty acknowledgement of our tool response
                    continue
                break
        elif "toolCall" in message:
            calls.extend(message["toolCall"]["functionCalls"])
            break
        elif "error" in message:
            raise RuntimeError(message["error"])
    return calls, text, audio


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=settings.load()["ai_model"])
    model = parser.parse_args().model
    key = ai.api_key()
    if not key:
        sys.exit("No key. Set GEMINI_API_KEY or save one in Settings, then run this again.")
    results = []

    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?pageSize=200&key={key}"
        models = json.load(urllib.request.urlopen(url, timeout=20))["models"]
        live = sorted(m["name"].removeprefix("models/") for m in models
                      if "bidiGenerateContent" in m.get("supportedGenerationMethods", []))
        results.append(check("key accepted by the models API", True))
        results.append(check(f"{model} supports Live", model in live, "Live-capable models: " + ", ".join(live)))
    except urllib.error.HTTPError as error:
        results.append(check("key accepted by the models API", False, f"HTTP {error.code}"))
        sys.exit(1)

    try:
        session = ai.LiveSession(f"{ai.ENDPOINT}?key={key}", model)
        results.append(check("connect and setup accepted", True))
    except Exception as error:
        results.append(check("connect and setup accepted", False, str(error).replace(key, "***")))
        sys.exit(1)

    try:
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (640, 360), "white")
        ImageDraw.Draw(image).text((200, 130), "4 2 7", fill="black", font_size=96)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG")
        frame = {"data": base64.b64encode(buffer.getvalue()).decode(), "mimeType": "image/jpeg"}

        # The assistant's real flow: the model calls screenshot, we send a frame, wait, then answer the call.
        session.send({"realtimeInput": {"text": "Call the screenshot tool, then tell me in one short sentence "
                                                "which digits are written in the picture."}})
        calls, _, _ = collect(session)
        results.append(check("tool call received", any(c["name"] == "screenshot" for c in calls),
                             ", ".join(c["name"] for c in calls) or "none"))
        if calls:
            session.send({"realtimeInput": {"video": frame}})
            time.sleep(ai.FRAME_SETTLE)
            session.send({"toolResponse": {"functionResponses": [
                {"id": c.get("id"), "name": c["name"], "response": {"image_width": 640, "image_height": 360}} for c in calls]}})
            _, transcript, audio = collect(session, after_tool=True)
            results.append(check("frame seen: the model read the digits", _reads_427(transcript),
                                 transcript.strip()[:80]))
            results.append(check("spoken audio reply", audio > 0, f"{audio} bytes"))
            results.append(check("text transcript of the reply", bool(transcript.strip())))
    except Exception as error:
        results.append(check("conversation", False, str(error).replace(key, "***")))
    finally:
        session.close()

    print("\nAll checks passed." if all(results) else "\nSome checks failed. Paste this output back so the client can be adjusted.")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
