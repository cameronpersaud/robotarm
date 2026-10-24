#!/usr/bin/env python3
"""
Spoken feedback for the robot arm, voiced by ElevenLabs.

Clips are generated ONCE (before the demo) and saved as WAV files, so the app
never needs the internet while running:

    python3 voice.py --generate        # needs ELEVENLABS_API_KEY in config.env
    python3 voice.py --test            # play every clip

Playback uses tools that ship with the OS (afplay on macOS, aplay on Raspberry Pi /
Linux, winsound on Windows), so there's nothing extra to install.
"""

import argparse
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave

import hand_control as hc

CLIP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_clips")
MANIFEST = os.path.join(CLIP_DIR, "manifest.json")

API_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"     # a premade ElevenLabs voice; override in config.env
DEFAULT_MODEL_ID = "eleven_multilingual_v2"
SAMPLE_RATE = 24000                            # pcm_24000: 16-bit mono PCM

# Event -> what gets said. Edit freely, then re-run --generate (only changed lines are redone).
PHRASES = {
    "session_start": "Hand tracking ready. Raise one hand to start.",
    "hand_found": "Hand detected.",
    "hand_lost": "Hand lost. Holding position.",
    "grip": "Gripping.",
    "release": "Released.",
    "floor_limit": "Floor limit. Raise your hand.",
    "paused": "Paused.",
    "resumed": "Resumed.",
    "home": "Returning home.",
    "session_end": "Session ended. Nice work.",
}

# Minimum seconds between repeats of the same announcement, so it never nags.
COOLDOWN = {"floor_limit": 4.0, "hand_found": 2.0, "hand_lost": 2.0,
            "grip": 0.6, "release": 0.6}


# --------------------------------------------------------------------------
# Generating clips (run once, needs internet + API key)
# --------------------------------------------------------------------------
def _tts(text, api_key, voice_id, model_id):
    url = f"{API_BASE}/text-to-speech/{voice_id}?output_format=pcm_{SAMPLE_RATE}"
    body = json.dumps({"text": text, "model_id": model_id}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "xi-api-key": api_key, "Content-Type": "application/json", "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, context=hc._ssl_context(), timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")[:300]
        raise RuntimeError(f"ElevenLabs returned {e.code}: {detail}") from None


def _write_wav(path, pcm):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


def generate(force=False):
    api_key = hc.get_setting("ELEVENLABS_API_KEY")
    if not api_key:
        sys.exit("Set ELEVENLABS_API_KEY in config.env (see config.env.example) first.")
    voice_id = hc.get_setting("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID)
    model_id = hc.get_setting("ELEVENLABS_MODEL_ID", DEFAULT_MODEL_ID)
    os.makedirs(CLIP_DIR, exist_ok=True)
    try:
        with open(MANIFEST) as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        manifest = {}

    made = 0
    for event, text in PHRASES.items():
        path = os.path.join(CLIP_DIR, f"{event}.wav")
        want = {"text": text, "voice": voice_id, "model": model_id}
        if not force and manifest.get(event) == want and os.path.exists(path):
            continue
        print(f"  generating {event:14s} \"{text}\"")
        _write_wav(path, _tts(text, api_key, voice_id, model_id))
        manifest[event] = want
        made += 1
        with open(MANIFEST, "w") as f:
            json.dump(manifest, f, indent=2)
    print(f"Done: {made} new clip(s), {len(PHRASES)} total in {CLIP_DIR}")


def clips_ready():
    return all(os.path.exists(os.path.join(CLIP_DIR, f"{e}.wav")) for e in PHRASES)


# --------------------------------------------------------------------------
# Playback
# --------------------------------------------------------------------------
def _player_command():
    system = platform.system()
    if system == "Windows":
        return "winsound"
    candidates = [["afplay"]] if system == "Darwin" else []
    candidates += [["aplay", "-q"], ["paplay"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]]
    for cmd in candidates:
        if shutil.which(cmd[0]):
            return cmd
    return None


def play_file(path, cmd=None):
    cmd = cmd or _player_command()
    if cmd is None:
        return False
    if cmd == "winsound":
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME)
    else:
        subprocess.run(cmd + [path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


class VoiceFeedback:
    """Tracker listener: speaks announcements for events, never blocking tracking.

    Only the newest pending announcement is kept, so speech never lags behind
    what the arm is actually doing.
    """

    name = "voice"

    def __init__(self):
        self.cmd = _player_command()
        self.enabled = clips_ready() and self.cmd is not None
        self.muted = False
        self._last = {}
        self._q = queue.Queue(maxsize=1)
        self._stop = False
        if self.cmd is None:
            print("[voice] no audio player found (need afplay / aplay / paplay / ffplay)")
        elif not clips_ready():
            print("[voice] clips missing: run  python3 voice.py --generate")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def status(self):
        if not self.enabled:
            return "off (clips or audio player missing)"
        return "muted" if self.muted else "on"

    def say(self, event):
        if not self.enabled or self.muted or event not in PHRASES:
            return
        now = time.monotonic()
        if now - self._last.get(event, -1e9) < COOLDOWN.get(event, 0.0):
            return
        self._last[event] = now
        try:
            self._q.get_nowait()          # drop an older pending announcement
        except queue.Empty:
            pass
        try:
            self._q.put_nowait(event)
        except queue.Full:
            pass

    def handle(self, state):
        for event in state.get("events", ()):
            self.say(event)

    def _run(self):
        while not self._stop:
            try:
                event = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                play_file(os.path.join(CLIP_DIR, f"{event}.wav"), self.cmd)
            except Exception as e:
                print(f"[voice] playback failed: {e}")

    def close(self):
        self.say("session_end")
        deadline = time.monotonic() + 3.0
        while not self._q.empty() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.1)
        self._stop = True
        self._thread.join(timeout=3.0)
        return None


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ElevenLabs voice clips for the robot arm")
    ap.add_argument("--generate", action="store_true", help="create clips with ElevenLabs")
    ap.add_argument("--force", action="store_true", help="regenerate every clip")
    ap.add_argument("--test", action="store_true", help="play every clip")
    args = ap.parse_args()
    if args.generate:
        generate(force=args.force)
    if args.test:
        cmd = _player_command()
        for event, text in PHRASES.items():
            path = os.path.join(CLIP_DIR, f"{event}.wav")
            if not os.path.exists(path):
                print(f"  missing {event}.wav")
                continue
            print(f"  {event:14s} {text}")
            if not play_file(path, cmd):
                sys.exit("No audio player found.")
    if not (args.generate or args.test):
        ap.print_help()
