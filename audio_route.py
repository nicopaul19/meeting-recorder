from __future__ import annotations
"""
Audio route — sends call audio to BlackHole without any manual setup.

At recording start: build a temporary multi-output device "Meeting Recorder
Output" = whatever you're listening on (AirPods, speakers, headset) + BlackHole,
and make it the system output. You still hear everything; the recorder gets a
copy via BlackHole. At stop: switch back and destroy the device.

- If you switch headphones mid-call, ensure() rebuilds the route for the new device.
- The route is remembered in data/state/audio_route.json, so a crash is cleaned
  up on the next launch (original output restored, device removed).
- Limitation: macOS has no volume control on multi-output devices, so the
  volume keys don't work while recording.
"""
import ctypes
import json
import os
import subprocess
import time

import objc
from Foundation import NSDictionary, NSArray, NSNumber

from config import STATE_DIR, BLACKHOLE_DEVICE_NAME

ROUTE_NAME = "Meeting Recorder Output"
UID_PREFIX = "com.nicolas.meetingrecorder.output"
STATE_PATH = STATE_DIR / "audio_route.json"

_ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
_ca.AudioHardwareCreateAggregateDevice.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
_ca.AudioHardwareCreateAggregateDevice.restype = ctypes.c_int32
_ca.AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]
_ca.AudioHardwareDestroyAggregateDevice.restype = ctypes.c_int32


def _sas(*args) -> str:
    return subprocess.run(["SwitchAudioSource", *args], capture_output=True, text=True, timeout=5).stdout


def _devices(kind: str = "output") -> list[dict]:
    out = []
    for line in _sas("-a", "-t", kind, "-f", "json").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def current_output() -> dict | None:
    try:
        return json.loads(_sas("-c", "-t", "output", "-f", "json"))
    except (json.JSONDecodeError, subprocess.SubprocessError, FileNotFoundError):
        return None


def _set_output(uid: str) -> bool:
    try:
        subprocess.run(["SwitchAudioSource", "-t", "output", "-u", uid], capture_output=True, timeout=5)
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    cur = current_output()
    return bool(cur and cur.get("uid") == uid)


def _is_ours(dev: dict | None) -> bool:
    return bool(dev and str(dev.get("uid", "")).startswith(UID_PREFIX))


def _create(listen_uid: str, blackhole_uid: str) -> tuple[int, str]:
    uid = f"{UID_PREFIX}.{os.getpid()}.{int(time.time())}"
    desc = NSDictionary.dictionaryWithDictionary_({
        "name": ROUTE_NAME,
        "uid": uid,
        "stacked": NSNumber.numberWithInt_(1),     # multi-output (mirror), not a merged device
        "private": NSNumber.numberWithInt_(0),     # must be public to be the system output
        "master": listen_uid,                      # your headphones/speakers drive the clock
        "subdevices": NSArray.arrayWithArray_([
            {"uid": listen_uid},
            {"uid": blackhole_uid, "drift": NSNumber.numberWithInt_(1)},
        ]),
    })
    dev_id = ctypes.c_uint32(0)
    status = _ca.AudioHardwareCreateAggregateDevice(ctypes.c_void_p(objc.pyobjc_id(desc)), ctypes.byref(dev_id))
    if status != 0 or not dev_id.value:
        raise RuntimeError(f"Core Audio refused to create the output device (status {status})")
    return dev_id.value, uid


def _destroy(dev_id: int):
    if dev_id:
        _ca.AudioHardwareDestroyAggregateDevice(ctypes.c_uint32(int(dev_id)))


def _save(state: dict | None):
    try:
        if state:
            STATE_PATH.write_text(json.dumps(state))
        else:
            STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


class AudioRoute:
    def __init__(self):
        self.device_id = 0
        self.uid = ""
        self.original_uid = ""   # what you were listening on before recording
        self.listen_uid = ""     # the device currently mirrored with BlackHole
        self.active = False
        self.note = ""

    def start(self) -> bool:
        """Route system output to (current device + BlackHole). True if call audio is captured."""
        cur = current_output()
        if not cur:
            self.note = "Couldn't read the current output device"
            return False
        blackhole = next((d for d in _devices() if BLACKHOLE_DEVICE_NAME.lower() in d["name"].lower()), None)
        if not blackhole:
            self.note = "BlackHole isn't installed"
            return False
        if _is_ours(cur):  # leftover from a crash: rebuild cleanly
            cleanup_leftovers()
            cur = current_output() or cur
        if cur["uid"] == blackhole["uid"]:
            self.note = "Output is set to BlackHole itself, so you'd hear nothing. Pick your speakers or headphones first."
            return False
        if "multi-output" in cur["name"].lower():
            self.note = "Using your own Multi-Output Device"
            self.active = False
            return True
        self.original_uid = cur["uid"]
        return self._build(cur["uid"], blackhole["uid"])

    def _build(self, listen_uid: str, blackhole_uid: str) -> bool:
        try:
            dev_id, uid = _create(listen_uid, blackhole_uid)
        except Exception as e:
            self.note = str(e)
            return False
        time.sleep(0.4)  # let Core Audio publish the device
        if not _set_output(uid):
            _destroy(dev_id)
            self.note = "Couldn't switch the system output to the recording route"
            return False
        old = self.device_id
        self.device_id, self.uid, self.listen_uid, self.active = dev_id, uid, listen_uid, True
        _save({"device_id": dev_id, "uid": uid, "original_uid": self.original_uid})
        if old and old != dev_id:
            _destroy(old)
        self.note = ""
        print(f"🔀 Audio route: {listen_uid} + BlackHole", flush=True)
        return True

    def ensure(self):
        """Call periodically while recording: follow headphone changes, repair the route."""
        if not self.active:
            return
        cur = current_output()
        if not cur or cur.get("uid") == self.uid:
            return
        blackhole = next((d for d in _devices() if BLACKHOLE_DEVICE_NAME.lower() in d["name"].lower()), None)
        if not blackhole or cur["uid"] == blackhole["uid"] or _is_ours(cur):
            return
        # You switched output (e.g. connected AirPods): mirror the new device instead.
        print(f"🔀 Output changed to {cur['name']}, rebuilding route", flush=True)
        self._build(cur["uid"], blackhole["uid"])

    def stop(self):
        if not self.active:
            return
        restore = self.listen_uid or self.original_uid
        if restore and not _set_output(restore):
            _set_output(self.original_uid)
        _destroy(self.device_id)
        self.device_id, self.uid, self.active = 0, "", False
        _save(None)


def cleanup_leftovers():
    """After a crash: restore the original output and remove our leftover device."""
    try:
        state = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        state = None
    cur = current_output()
    if state:
        if _is_ours(cur) and state.get("original_uid"):
            _set_output(state["original_uid"])
        _destroy(state.get("device_id", 0))
        _save(None)
        print("🧹 Cleaned up a leftover recording audio route", flush=True)
    elif _is_ours(cur):
        speakers = next((d for d in _devices() if "speaker" in d["name"].lower()), None)
        if speakers:
            _set_output(speakers["uid"])
