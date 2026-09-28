"""Ask macOS for microphone permission (shows the system prompt once)."""
import time
import AVFoundation
from Foundation import NSDate, NSRunLoop
M = AVFoundation.AVMediaTypeAudio
print("status before:", AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(M), "(0=not asked, 2=denied, 3=allowed)")
done = []
AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(M, lambda ok: done.append(ok))
for _ in range(600):
    if done: break
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
print("granted:", done, "| status after:", AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(M))
