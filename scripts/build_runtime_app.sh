#!/bin/bash
# Build "Meeting Recorder.app" around the local runtime copy.
# macOS only grants microphone access to an app it can ask you about; a bare
# python started by launchd silently records zeros. The bundle gives the
# recorder its own identity ("Meeting Recorder" in Privacy → Microphone).
set -euo pipefail
RT="${1:-$HOME/meeting-recorder-proxy}"
FINAL="$RT/Meeting Recorder.app"
# macOS ties the microphone grant to the bundle's code signature, so the
# bundle is only rebuilt/re-signed when its content actually changes.
APP="$(mktemp -d)/Meeting Recorder.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>launcher</string>
    <key>CFBundleIdentifier</key><string>com.nicolas.meeting-recorder</string>
    <key>CFBundleName</key><string>Meeting Recorder</string>
    <key>CFBundleDisplayName</key><string>Meeting Recorder</string>
    <key>CFBundleVersion</key><string>2.0</string>
    <key>CFBundleShortVersionString</key><string>2.0</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>LSUIElement</key><true/>
    <key>LSArchitecturePriority</key><array><string>arm64</string></array>
    <key>LSRequiresNativeExecution</key><true/>
    <key>NSMicrophoneUsageDescription</key><string>Meeting Recorder records your meetings (your microphone plus call audio) to transcribe and summarise them.</string>
    <key>NSAppleEventsUsageDescription</key><string>Meeting Recorder shows notifications before your meetings.</string>
</dict>
</plist>
PLIST
cat > "$APP/Contents/Resources/launcher.sh" <<LAUNCHER
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$RT"
# Script bundles can start under Rosetta; the venv is arm64-only.
PY=(/usr/bin/arch -arm64 "$RT/venv/bin/python")
# "--run <tool>" runs $RT/<tool>.py with the app's identity (same microphone
# permission), e.g. --run selftest. Kept generic so this file never changes:
# any edit here re-signs the bundle and can reset the microphone grant.
if [ "\${1:-}" = "--run" ] && [[ "\${2:-}" =~ ^[a-z_]+$ ]] && [ -f "$RT/\$2.py" ]; then
  exec "\${PY[@]}" "$RT/\$2.py" > "/tmp/mr_\$2.txt" 2>&1
fi
exec "\${PY[@]}" "$RT/app.py" >> /tmp/meeting_recorder.log 2>> /tmp/meeting_recorder_error.log
LAUNCHER
chmod +x "$APP/Contents/Resources/launcher.sh"
# Native main executable (see scripts/launcher/launcher.c for why)
clang -O2 -arch arm64 -o "$APP/Contents/MacOS/launcher" "$(dirname "$0")/launcher/launcher.c"

if [ -d "$FINAL" ] && cmp -s "$APP/Contents/Info.plist" "$FINAL/Contents/Info.plist" \
   && cmp -s "$APP/Contents/Resources/launcher.sh" "$FINAL/Contents/Resources/launcher.sh" \
   && [ -f "$FINAL/Contents/MacOS/launcher" ] && file "$FINAL/Contents/MacOS/launcher" | grep -q Mach-O \
   && codesign -v "$FINAL" 2>/dev/null; then
  echo "✓ App bundle unchanged (microphone permission kept)"
else
  rm -rf "$FINAL"
  mv "$APP" "$FINAL"
  codesign --force --deep -s - "$FINAL" 2>/dev/null
  echo "✓ Built + ad-hoc signed: $FINAL (if recordings are silent, re-allow it in Privacy › Microphone)"
fi
rm -rf "$(dirname "$APP")" 2>/dev/null || true
