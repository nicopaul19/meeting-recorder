from __future__ import annotations
"""
Audio Recorder — captures system audio (BlackHole) + microphone simultaneously.

Records both streams at 16kHz mono and mixes them into a single WAV file.
Automatically switches macOS audio output to Multi-Output Device during recording.
"""
import wave
import threading
import time
import subprocess
from typing import Optional, List
import numpy as np
import sounddevice as sd
from pathlib import Path
from datetime import datetime

from config import (
    SAMPLE_RATE,
    CHANNELS,
    BLACKHOLE_DEVICE_NAME,
    MULTI_OUTPUT_DEVICE_NAME,
    RECORDINGS_DIR,
)


def find_device(name: str, kind: str = "input") -> int | None:
    """Find audio device index by name. kind = 'input' or 'output'."""
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        if name.lower() in dev["name"].lower():
            if kind == "input" and dev["max_input_channels"] > 0:
                return i
            elif kind == "output" and dev["max_output_channels"] > 0:
                return i
    return None


def get_default_output_device() -> str:
    """Get current default output device name."""
    try:
        result = subprocess.run(
            ["SwitchAudioSource", "-c", "-t", "output"],
            capture_output=True, text=True
        )
        return result.stdout.strip()
    except FileNotFoundError:
        return ""


def switch_audio_output(device_name: str) -> bool:
    """Switch macOS audio output to specified device."""
    try:
        result = subprocess.run(
            ["SwitchAudioSource", "-s", device_name, "-t", "output"],
            capture_output=True, text=True
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


class MeetingRecorder:
    """Records system audio + microphone simultaneously."""

    def __init__(self):
        self.is_recording = False
        self.start_time: float | None = None
        self._mic_data: list[np.ndarray] = []
        self._system_data: list[np.ndarray] = []
        self._mic_stream = None
        self._system_stream = None
        self._original_output: str = ""
        self._lock = threading.Lock()
        # Peak levels since start. Exactly 0.0 after several seconds means
        # macOS is blocking audio input (no microphone permission).
        self.mic_peak = 0.0
        self.system_peak = 0.0
        self.output_switched = False
        from audio_route import AudioRoute
        self.route = AudioRoute()

    def _mic_callback(self, indata, frames, time_info, status):
        """Callback for microphone input stream."""
        if self.is_recording:
            with self._lock:
                self._mic_data.append(indata.copy())
            self.mic_peak = max(self.mic_peak, float(np.abs(indata).max()))

    def _system_callback(self, indata, frames, time_info, status):
        """Callback for BlackHole system audio stream."""
        if self.is_recording:
            with self._lock:
                self._system_data.append(indata.copy())
            self.system_peak = max(self.system_peak, float(np.abs(indata).max()))

    def check_setup(self) -> dict:
        """Check if audio setup is correct. Returns status dict."""
        status = {
            "blackhole_found": False,
            "mic_found": False,
            "multi_output_found": False,
            "switchaudio_installed": False,
        }

        # Check BlackHole
        bh_idx = find_device(BLACKHOLE_DEVICE_NAME, "input")
        status["blackhole_found"] = bh_idx is not None

        # Check default mic
        try:
            default_input = sd.query_devices(kind="input")
            status["mic_found"] = default_input is not None
        except Exception:
            pass

        # Check Multi-Output Device
        # The app builds its own multi-output route per recording (audio_route.py)
        status["multi_output_found"] = status["blackhole_found"]

        # Check SwitchAudioSource
        try:
            subprocess.run(["SwitchAudioSource", "-h"],
                           capture_output=True, timeout=2)
            status["switchaudio_installed"] = True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        return status

    def start(self) -> bool:
        """
        Start recording from both microphone and BlackHole.
        Returns True if recording started successfully.
        """
        if self.is_recording:
            return False

        # Find BlackHole device
        bh_idx = find_device(BLACKHOLE_DEVICE_NAME, "input")
        if bh_idx is None:
            raise RuntimeError(
                f"BlackHole not found! Make sure '{BLACKHOLE_DEVICE_NAME}' is installed.\n"
                "Run: brew install blackhole-2ch (and reboot)"
            )

        self.mic_peak = self.system_peak = 0.0

        # Mirror whatever you're listening on into BlackHole (temporary device)
        self.output_switched = self.route.start()
        if not self.output_switched:
            print(f"⚠ Call audio won't be captured: {self.route.note}")

        # Reset buffers
        self._mic_data = []
        self._system_data = []

        # Open microphone stream (default input device)
        try:
            self._mic_stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                callback=self._mic_callback,
                blocksize=1024,
            )
        except Exception as e:
            print(f"⚠ Mic stream error: {e}")
            self._mic_stream = None

        # Open BlackHole stream (system audio)
        try:
            self._system_stream = sd.InputStream(
                device=bh_idx,
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                callback=self._system_callback,
                blocksize=1024,
            )
        except Exception as e:
            raise RuntimeError(f"BlackHole stream error: {e}")

        # Start both streams
        if self._mic_stream:
            self._mic_stream.start()
        self._system_stream.start()

        self.is_recording = True
        self.start_time = time.time()
        return True

    def stop(self, meeting_title: str = "") -> Path | None:
        """
        Stop recording, mix audio, save to WAV.
        Returns path to the saved WAV file.
        """
        if not self.is_recording:
            return None

        self.is_recording = False

        # Stop streams
        if self._mic_stream:
            self._mic_stream.stop()
            self._mic_stream.close()
        if self._system_stream:
            self._system_stream.stop()
            self._system_stream.close()

        # Restore your original output and remove the temporary device
        try:
            self.route.stop()
        except Exception as e:
            print(f"⚠ Audio route cleanup failed: {e}")

        # Mix audio
        with self._lock:
            mic_audio = np.concatenate(self._mic_data) if self._mic_data else None
            system_audio = np.concatenate(self._system_data) if self._system_data else None

        if mic_audio is None and system_audio is None:
            return None

        # Align lengths and mix
        if mic_audio is not None and system_audio is not None:
            min_len = min(len(mic_audio), len(system_audio))
            mic_audio = mic_audio[:min_len]
            system_audio = system_audio[:min_len]
            # Mix: system audio louder (0.7) + mic (0.5), prevent clipping
            mixed = system_audio * 0.7 + mic_audio * 0.5
            mixed = np.clip(mixed, -1.0, 1.0)
        elif system_audio is not None:
            mixed = system_audio
        else:
            mixed = mic_audio

        # Generate filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_title = "".join(c if c.isalnum() or c in " -_" else "" for c in meeting_title)
        safe_title = safe_title.strip().replace(" ", "_")[:50]
        if safe_title:
            filename = f"{timestamp}_{safe_title}.wav"
        else:
            filename = f"{timestamp}_meeting.wav"

        filepath = RECORDINGS_DIR / filename

        # Save as WAV
        audio_int16 = (mixed * 32767).astype(np.int16)
        with wave.open(str(filepath), "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_int16.tobytes())

        duration = len(mixed) / SAMPLE_RATE
        print(f"✓ Saved recording: {filepath.name} ({duration:.0f}s)")

        self.start_time = None
        return filepath

    @property
    def elapsed_seconds(self) -> float:
        """Get elapsed recording time in seconds."""
        if self.start_time and self.is_recording:
            return time.time() - self.start_time
        return 0.0

    @property
    def elapsed_str(self) -> str:
        """Get elapsed time as MM:SS string."""
        secs = int(self.elapsed_seconds)
        return f"{secs // 60:02d}:{secs % 60:02d}"
