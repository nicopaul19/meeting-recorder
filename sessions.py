from __future__ import annotations
"""
Sessions — one per recorded meeting, persisted to data/sessions/<id>.json
after every change. A crash or restart never loses the notepad, the audio
path or the transcript. Unfinished sessions come back as "Needs your notes".

status: recording → transcribing → awaiting_notes → generating → done
        (or error at any step; retry resumes from what's already done)
"""
import json
import threading
import uuid
from datetime import datetime, timedelta

from config import SESSIONS_DIR

PERSISTED = (
    "id", "title", "meeting", "status", "started_at", "ended_at", "audio_path",
    "notepad", "dest_type", "dest_url", "transcript", "transcript_error",
    "summary", "markdown", "text", "html", "outputs", "saved_paths", "error", "step",
)


def _json_meeting(m: dict | None) -> dict | None:
    if not m:
        return None
    out = dict(m)
    for k in ("start", "end"):
        if isinstance(out.get(k), datetime):
            out[k] = out[k].isoformat()
    return out


class Session:
    def __init__(self, title: str, meeting: dict | None = None, dest_type: str = "", dest_url: str = ""):
        self.id = uuid.uuid4().hex[:10]
        self.title = title
        self.meeting = _json_meeting(meeting)
        self.status = "recording"
        self.started_at = datetime.now().isoformat()
        self.ended_at = ""
        self.audio_path = ""
        self.notepad = ""
        self.dest_type = dest_type  # "" | "local" | "link"
        self.dest_url = dest_url
        self.transcript: dict | None = None
        self.transcript_error = ""
        self.summary: dict | None = None
        self.markdown = self.text = self.html = ""
        self.outputs: list[dict] = []
        self.saved_paths: list[str] = []
        self.error = ""
        self.step = ""
        # runtime only
        self.auto_stop_at: datetime | None = None
        self.end_prompted = False
        self.max_at: datetime | None = None
        self.max_warned = False
        self.audio_checked = False
        self.audio_blocked = False
        self.steps: list[dict] = []
        self.tx_state = "done" if self.transcript else "idle"  # idle|running|done|error
        self.tx_text = ""
        self.tx_done = threading.Event()

    @property
    def attendees(self) -> list[str]:
        return (self.meeting or {}).get("attendees", [])

    def save(self):
        data = {k: getattr(self, k) for k in PERSISTED}
        path = SESSIONS_DIR / f"{self.id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str))
        tmp.replace(path)

    def delete(self):
        (SESSIONS_DIR / f"{self.id}.json").unlink(missing_ok=True)

    @classmethod
    def load(cls, data: dict) -> "Session":
        s = cls.__new__(cls)
        cls.__init__(s, data.get("title", "Meeting"))
        for k in PERSISTED:
            if k in data:
                setattr(s, k, data[k])
        if s.transcript:
            s.tx_state = "done"
            s.tx_done.set()
        return s


def load_recent(days: int = 7) -> list[Session]:
    """Sessions from the last N days, newest first. Crashed recordings become errors."""
    cutoff = datetime.now() - timedelta(days=days)
    out = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            s = Session.load(json.loads(f.read_text()))
            if datetime.fromisoformat(s.started_at) < cutoff:
                continue
        except Exception:
            continue
        if s.status == "recording":  # app died mid-recording; audio is in memory only
            s.status, s.error = ("awaiting_notes" if s.notepad.strip() else "error"), \
                "The app stopped during this recording, so the audio was lost. Your notepad was kept."
        elif s.status == "transcribing":
            s.status = "awaiting_notes"  # transcription restarts from audio_path
        elif s.status == "generating":
            s.status = "awaiting_notes"
        out.append(s)
    out.sort(key=lambda s: s.started_at, reverse=True)
    return out
