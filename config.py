from __future__ import annotations
"""
Config for Meeting Recorder.
Loads API keys from the shared tum_sales_agent/.env file.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Tool PATH — launchd starts us with a bare PATH, which hides Homebrew's
# ffmpeg (transcription) and SwitchAudioSource (system-audio capture).
# Must run before pydub is imported anywhere.
# ---------------------------------------------------------------------------
for _bin in ("/usr/local/bin", "/opt/homebrew/bin"):
    if _bin not in os.environ.get("PATH", "").split(":"):
        os.environ["PATH"] = f"{_bin}:{os.environ.get('PATH', '')}"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).parent.resolve()
DATA_DIR = PROJECT_DIR / "data"          # app-internal state (always local)
RECORDINGS_DIR = DATA_DIR / "recordings"
SESSIONS_DIR = DATA_DIR / "sessions"     # crash-safe per-meeting state
STATE_DIR = DATA_DIR / "state"           # watcher bookkeeping
UI_DIR = PROJECT_DIR / "ui"

# User-facing output (transcripts + summaries) goes to the project folder in
# Google Drive, even when launchd runs the local runtime copy. launchd may
# write there but not read, so the app never reads these back.
def _default_output_project_dir() -> Path:
    """The project folder inside Google Drive (auto-detected), else this folder."""
    for drive in sorted(Path.home().glob("Library/CloudStorage/GoogleDrive-*")):
        candidate = drive / "My Drive" / "AI Projects & Agents" / "meeting-recorder"
        if candidate.parent.exists():
            return candidate
    return PROJECT_DIR


DRIVE_PROJECT_DIR = Path(os.getenv("MEETING_OUTPUT_PROJECT_DIR", "") or _default_output_project_dir())
OUTPUT_DIR = DRIVE_PROJECT_DIR / "data" if DRIVE_PROJECT_DIR.parent.exists() else DATA_DIR
TRANSCRIPTS_DIR = OUTPUT_DIR / "transcripts"
SUMMARIES_DIR = OUTPUT_DIR / "summaries"

# Google OAuth files (absolute, so launchd's working dir never matters)
GOOGLE_CREDENTIALS_PATH = PROJECT_DIR / "credentials.json"
CALENDAR_TOKEN_PATH = PROJECT_DIR / "token.json"
DOCS_TOKEN_PATH = PROJECT_DIR / "token_docs.json"

# Ensure directories exist (fall back to local if Drive isn't writable)
for _d in (RECORDINGS_DIR, SESSIONS_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
try:
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    OUTPUT_DIR = DATA_DIR
    TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
    SUMMARIES_DIR = DATA_DIR / "summaries"
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Load environment variables (cascade: root → project)
# ---------------------------------------------------------------------------
WORKSPACE_ROOT = PROJECT_DIR.parent  # AI Projects & Agents/
load_dotenv(WORKSPACE_ROOT / ".env")  # Shared keys (fallback)
load_dotenv(PROJECT_DIR / ".env")     # Project-specific (overrides)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_DB_MEETINGS_ID = os.getenv("NOTION_DB_MEETINGS_ID", "")

# ---------------------------------------------------------------------------
# LLM (notes, summaries, synthesis) — Responses API with reasoning
# ---------------------------------------------------------------------------
LLM_MODEL = os.getenv("MEETING_LLM_MODEL", "gpt-6-sol")
LLM_REASONING_EFFORT = os.getenv("MEETING_LLM_REASONING", "high")

# ---------------------------------------------------------------------------
# Calendar watcher
# ---------------------------------------------------------------------------
def _ids(env: str, default: str = "") -> list[str]:
    return [c.strip() for c in os.getenv(env, default).split(",") if c.strip()]

# Imported calendars (e.g. a university timetable ICS feed) carry no guest lists.
# Events there count when they look like a real meeting (see meeting_watcher).
# Set MEETING_ALL_EVENTS_CALENDAR_IDS in .env (calendar IDs, comma-separated).
ALL_EVENTS_CALENDAR_IDS = set(_ids("MEETING_ALL_EVENTS_CALENDAR_IDS"))
# Calendars to watch: primary + the all-events ones + any extras
CALENDAR_IDS = list(dict.fromkeys(["primary", *ALL_EVENTS_CALENDAR_IDS, *_ids("MEETING_CALENDAR_IDS")]))
# Events whose title contains one of these words never prompt (comma-separated)
SKIP_TITLE_WORDS = [w.lower() for w in _ids("MEETING_SKIP_TITLE_WORDS")]
PROMPT_LEAD_SECONDS = 60        # pop up 1 min before start
PROMPT_LATE_GRACE_SECONDS = 300  # still pop up if we were asleep/offline at start
MAX_RECORDING_MINUTES = 90      # hard stop unless extended
MAX_WARNING_MINUTES = 5         # pop up this long before the hard stop
EXTEND_MINUTES = 30             # "Keep recording" adds this much

# ---------------------------------------------------------------------------
# Audio settings
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000       # 16kHz — optimal for speech recognition
CHANNELS = 1              # Mono
BLACKHOLE_DEVICE_NAME = "BlackHole 2ch"
MULTI_OUTPUT_DEVICE_NAME = "Multi-Output Device"

# ---------------------------------------------------------------------------
# Transcription settings
# ---------------------------------------------------------------------------
TRANSCRIBE_MODEL = "gpt-4o-transcribe"
DIARIZE_MODEL = "gpt-4o-transcribe"  # Use base model; diarize if available
MAX_FILE_SIZE_MB = 25     # OpenAI API limit
CHUNK_DURATION_SEC = 600  # 10-minute chunks for long recordings

# ---------------------------------------------------------------------------
# Meeting Notes Agent path (for enrichment integration)
# ---------------------------------------------------------------------------
MEETING_AGENT_DIR = PROJECT_DIR.parent / "meeting-notes-agent"
