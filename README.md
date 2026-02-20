# 🎙️ Meeting Recorder

A native macOS floating-window app that records meetings, transcribes them with OpenAI's GPT-4o-transcribe, and pushes structured notes directly to Notion — built with Python + AppKit.

## What It Does

- **One-click recording** from the macOS menu bar (or global hotkey `⌘⇧R`)
- **Auto-detects meetings** from your Google Calendar
- **Transcribes** audio using OpenAI's GPT-4o-transcribe model
- **Enriches** transcripts with structured meeting notes
- **Pushes to Notion** — creates meeting note pages in your database
- **Floating window** — always-on-top compact UI

## Features

- 🎤 Records system audio via BlackHole virtual audio device
- 🗓️ Google Calendar integration — auto-fills meeting titles
- 📝 AI-powered transcription with speaker diarization
- 📊 Live recording dashboard with waveform visualization
- ⌨️ Global hotkey `⌘⇧R` for instant start/stop
- 🔔 macOS native notifications

## Prerequisites

- **macOS** (uses AppKit for the native window)
- **BlackHole 2ch** — virtual audio device for capturing system audio
  - Install: `brew install blackhole-2ch`
  - Set up a **Multi-Output Device** in Audio MIDI Setup (system audio + BlackHole)
- **Python 3.8+**

## Quick Start

### 1. Clone & install

```bash
git clone https://github.com/nicopaul19/meeting-recorder.git
cd meeting-recorder
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.template .env
# Edit .env with your API keys
```

You'll need:
- **OpenAI API Key** — [Get one here](https://platform.openai.com/api-keys)
- **Notion Integration Token** — [Create here](https://www.notion.so/my-integrations)
- **Google Calendar OAuth** — Set up a project in [Google Cloud Console](https://console.cloud.google.com/), enable Calendar API, download `credentials.json`

### 3. Audio Setup

1. Install BlackHole: `brew install blackhole-2ch`
2. Open **Audio MIDI Setup** (Spotlight → "Audio MIDI Setup")
3. Click `+` → **Create Multi-Output Device**
4. Check both your speakers/headphones AND BlackHole 2ch
5. Set the Multi-Output Device as your system output

### 4. Run

```bash
python app.py
```

The app appears as a 🔴 icon in your macOS menu bar.

## Project Structure

```
meeting-recorder/
├── app.py                    # Main AppKit floating window app
├── recorder.py               # Audio recording (BlackHole)
├── transcriber.py            # OpenAI GPT-4o-transcribe
├── enricher.py               # Transcript → structured notes
├── notion_bridge.py          # Notion API integration
├── meeting_watcher.py        # Google Calendar integration
├── agent_bridge.py           # Bridge to meeting-notes-agent
├── dashboard.py              # Recording dashboard UI
├── local_store.py            # Local meeting data storage
├── config.py                 # Configuration & env loading
├── requirements.txt          # Python dependencies
├── start_background.command  # macOS launcher script
├── scripts/                  # Utility scripts
└── .env.template             # Environment variable template
```

## How It Works

1. **Record** — Captures system audio via BlackHole virtual device
2. **Transcribe** — Sends audio to OpenAI's GPT-4o-transcribe API
3. **Enrich** — Structures transcript into meeting notes with key points
4. **Push** — Creates a new page in your Notion meeting notes database

## Requirements

- macOS (AppKit-based)
- Python 3.8+
- BlackHole 2ch (virtual audio device)
- OpenAI API account
- Notion workspace
- Google Calendar API (optional, for auto-detect)

## License

MIT
