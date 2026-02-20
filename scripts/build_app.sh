#!/bin/bash
# build_app.sh — Creates a macOS .app bundle for Meeting Recorder
# The .app gets proper calendar/mic permissions from macOS

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="Meeting Recorder"
APP_DIR="${SCRIPT_DIR}/${APP_NAME}.app"
CONTENTS="${APP_DIR}/Contents"
MACOS="${CONTENTS}/MacOS"
RESOURCES="${CONTENTS}/Resources"

echo "🔨 Building ${APP_NAME}.app..."

# Clean old build
rm -rf "${APP_DIR}"

# Create structure
mkdir -p "${MACOS}" "${RESOURCES}"

# ─── Info.plist ───
cat > "${CONTENTS}/Info.plist" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>launcher</string>
    <key>CFBundleIdentifier</key>
    <string>com.nicolas.meeting-recorder</string>
    <key>CFBundleName</key>
    <string>Meeting Recorder</string>
    <key>CFBundleDisplayName</key>
    <string>Meeting Recorder</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSCalendarsUsageDescription</key>
    <string>Meeting Recorder needs calendar access to detect upcoming meetings and auto-prompt you to record them.</string>
    <key>NSCalendarsFullAccessUsageDescription</key>
    <string>Meeting Recorder reads your calendar to detect meetings with guests and remind you to start recording.</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>Meeting Recorder needs microphone access to record meetings.</string>
</dict>
</plist>
EOF

# ─── Launcher script ───
cat > "${MACOS}/launcher" << LAUNCHER
#!/bin/bash
# Launch the Meeting Recorder Python app using venv
cd "${SCRIPT_DIR}"
if [ -f "${SCRIPT_DIR}/venv/bin/python3" ]; then
    exec "${SCRIPT_DIR}/venv/bin/python3" "${SCRIPT_DIR}/app.py"
else
    exec /usr/bin/python3 "${SCRIPT_DIR}/app.py"
fi
LAUNCHER

chmod +x "${MACOS}/launcher"

echo "✅ Built: ${APP_DIR}"
echo ""
echo "To install on Desktop:"
echo "  cp -r '${APP_DIR}' ~/Desktop/"
echo ""
echo "Or just double-click it right here to launch!"
