from __future__ import annotations
"""
Config for Meeting Recorder.
Loads API keys from the shared tum_sales_agent/.env file.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).parent.resolve()
DATA_DIR = PROJECT_DIR / "data"
RECORDINGS_DIR = DATA_DIR / "recordings"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
SUMMARIES_DIR = DATA_DIR / "summaries"

# Ensure directories exist
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
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
