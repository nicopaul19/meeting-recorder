"""Record 1 s from the default mic and print the peak level (0 = blocked).

Run with the app's identity:
    open -n -a ~/meeting-recorder-proxy/"Meeting Recorder.app" --args --run mic_probe
    cat /tmp/mr_mic_probe.txt
"""
import numpy as np
import sounddevice as sd

try:
    audio = sd.rec(16000, samplerate=16000, channels=1, dtype="float32")
    sd.wait()
    print(f"peak={float(np.abs(audio).max()):.6f}")
except Exception as e:
    print(f"error={e}")
