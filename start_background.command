#!/bin/bash
# Start Meeting Recorder in the background (when not managed by launchd).
cd "$(dirname "$0")"
if launchctl list | /usr/bin/awk '$3 == "com.nicolas.meetingrecorder" { found=1 } END { exit !found }'; then
    launchctl kickstart "gui/$(id -u)/com.nicolas.meetingrecorder" >/dev/null 2>&1
    echo "Meeting Recorder is managed by launchd; asked launchd to start it."
    exit 0
fi
nohup ./venv/bin/python app.py > /tmp/meeting_recorder.log 2>&1 &
