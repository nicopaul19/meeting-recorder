#!/bin/bash
# Deploy the Drive source to the launchd runtime copy (~/meeting-recorder-proxy).
# launchd can't run code from Google Drive (macOS privacy sandbox), so the app
# runs from a local copy. Run this after every code change:
#   bash scripts/deploy.sh
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/meeting-recorder-proxy"
LABEL="com.nicolas.meetingrecorder"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if grep -q "Recording started" <(tail -n 200 /tmp/meeting_recorder.log 2>/dev/null) && \
   ! grep -q -E "Saved recording|✓ Saved" <(tail -n 50 /tmp/meeting_recorder.log 2>/dev/null); then
  read -r -p "The recorder may be recording right now. Restart anyway? [y/N] " ok
  [[ "$ok" == "y" ]] || exit 1
fi

BACKUP="$DEST/.backup/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP"
cp "$DEST"/*.py "$BACKUP"/ 2>/dev/null || true
[ -d "$DEST/ui" ] && cp -R "$DEST/ui" "$BACKUP"/

rsync -a \
  --include='*.py' --include='ui/***' --include='requirements.txt' --exclude='*' \
  "$SRC/" "$DEST/"
echo "✓ Code synced to $DEST (backup: $BACKUP)"

"$DEST/venv/bin/pip" install -q -r "$DEST/requirements.txt" 2>&1 | grep -v -i "pip version\|upgrade" || true
"$DEST/venv/bin/python" -c "import WebKit, googleapiclient, pydantic" && echo "✓ Dependencies OK"

# App bundle = the identity macOS attaches the microphone permission to
bash "$SRC/scripts/build_runtime_app.sh" "$DEST"
APP="$DEST/Meeting Recorder.app"

# launchd starts the bundle via LaunchServices (open -W) and restarts it if it
# dies. "Quit" in the menu unloads the job, so it stays quit until next login.
/usr/libexec/PlistBuddy -c "Delete :ProgramArguments" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments array" \
  -c "Add :ProgramArguments:0 string /usr/bin/open" \
  -c "Add :ProgramArguments:1 string -W" \
  -c "Add :ProgramArguments:2 string -a" \
  -c "Add :ProgramArguments:3 string $APP" "$PLIST"
/usr/libexec/PlistBuddy -c "Delete :KeepAlive" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :KeepAlive bool true" "$PLIST"
/usr/libexec/PlistBuddy -c "Delete :ThrottleInterval" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :ThrottleInterval integer 15" "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
pkill -f "$DEST/app.py" 2>/dev/null || true
sleep 2
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "✓ Restarted $LABEL"
sleep 4
tail -n 5 /tmp/meeting_recorder.log
