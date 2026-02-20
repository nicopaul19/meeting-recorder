#!/bin/bash
cd "/Users/nicolasemidat/Library/CloudStorage/GoogleDrive-nicopaul19@gmail.com/My Drive/AI Projects & Agents/meeting-recorder"
# Force-refresh venv config so macOS Google Drive sandbox doesn't block it
rm venv/pyvenv.cfg
cat > venv/pyvenv.cfg << VENVCFG
home = /usr/local/bin
include-system-site-packages = false
version = 3.9.6
VENVCFG
nohup ./venv/bin/python app.py > ~/Desktop/meeting_recorder_app.log 2>&1 &
