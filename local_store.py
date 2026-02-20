from __future__ import annotations
"""
Local Store — saves meeting records as JSON files.

Every recording is stored locally regardless of whether it's pushed to Notion.
Files are saved in data/meetings/YYYY-MM-DD_title.json.
"""
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from config import DATA_DIR

MEETINGS_DIR = DATA_DIR / "meetings"
MEETINGS_DIR.mkdir(parents=True, exist_ok=True)


def _slugify(text: str) -> str:
    """Convert text to a safe filename slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "_", text)
    return text[:60] or "untitled"


def save_meeting(
    title: str,
    date: datetime,
    duration_seconds: float,
    audio_path: str,
    transcript: dict,
    enriched_notes: str = "",
    summary: str = "",
    pushed_to_notion: bool = False,
    notion_page_id: str = "",
) -> dict:
    """
    Save a meeting record to a local JSON file.

    Returns the meeting dict (including the generated id and file path).
    """
    meeting_id = uuid.uuid4().hex[:12]
    date_str = date.strftime("%Y-%m-%d")
    slug = _slugify(title)
    filename = f"{date_str}_{slug}_{meeting_id}.json"

    meeting = {
        "id": meeting_id,
        "title": title,
        "date": date.isoformat(),
        "duration_seconds": duration_seconds,
        "audio_path": str(audio_path),
        "transcript": transcript,
        "enriched_notes": enriched_notes,
        "summary": summary,
        "pushed_to_notion": pushed_to_notion,
        "notion_page_id": notion_page_id,
    }

    filepath = MEETINGS_DIR / filename
    filepath.write_text(json.dumps(meeting, indent=2, ensure_ascii=False))
    meeting["_filepath"] = str(filepath)
    return meeting


def list_meetings() -> list[dict]:
    """
    List all saved meetings, sorted by date descending.

    Returns lightweight dicts (no full transcript) for listing.
    """
    meetings = []
    for f in MEETINGS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            meetings.append({
                "id": data["id"],
                "title": data.get("title", "Untitled"),
                "date": data.get("date", ""),
                "duration_seconds": data.get("duration_seconds", 0),
                "pushed_to_notion": data.get("pushed_to_notion", False),
                "notion_page_id": data.get("notion_page_id", ""),
                "_filepath": str(f),
            })
        except (json.JSONDecodeError, KeyError):
            continue

    meetings.sort(key=lambda m: m["date"], reverse=True)
    return meetings


def get_meeting(meeting_id: str) -> dict | None:
    """Load a full meeting record by its ID."""
    for f in MEETINGS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if data.get("id") == meeting_id:
                data["_filepath"] = str(f)
                return data
        except (json.JSONDecodeError, KeyError):
            continue
    return None


def update_meeting(meeting_id: str, **updates) -> dict | None:
    """
    Update fields on an existing meeting record.

    Commonly used to set pushed_to_notion=True and notion_page_id after
    a deferred push to Notion.
    """
    for f in MEETINGS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if data.get("id") == meeting_id:
                data.update(updates)
                f.write_text(json.dumps(data, indent=2, ensure_ascii=False))
                data["_filepath"] = str(f)
                return data
        except (json.JSONDecodeError, KeyError):
            continue
    return None
