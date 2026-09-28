from __future__ import annotations
"""
Meeting Watcher — reliable "1 minute before" prompts from Google Calendar.

How it stays reliable:
- Sync and prompt are separate. The calendar is synced into a local cache
  (every 60 s, 10 s retries while failing). A 5-second scheduler fires prompts
  from that cache, so a flaky network at T-60s never causes a missed prompt.
- Wake-aware. The app calls wake() on macOS wake; the watcher re-syncs right
  away and still prompts for meetings that started up to 5 minutes ago.
- Deduped on disk. Prompted (event id, start time) pairs are persisted, so a
  restart never double-prompts, and a rescheduled meeting prompts again.
- It never opens a browser by itself (see google_auth.py). A revoked token
  shows up as status "reauth" in the UI instead of hanging a thread.

Qualifying meetings: timed (not all-day), not cancelled, not declined by me,
not focus/OOO/working-location blocks, and with at least one other human
invited (rooms and resources don't count).
"""
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from config import (
    CALENDAR_IDS,
    ALL_EVENTS_CALENDAR_IDS,
    SKIP_TITLE_WORDS,
    PROMPT_LEAD_SECONDS,
    PROMPT_LATE_GRACE_SECONDS,
    STATE_DIR,
)
import google_auth

PROMPTED_PATH = STATE_DIR / "prompted_events.json"
SKIP_EVENT_TYPES = {"focusTime", "outOfOffice", "workingLocation", "birthday"}
LINK_RE = re.compile(
    r"https://(?:meet\.google\.com|[^\s<>\"]*zoom\.us|[^\s<>\"]*teams\.microsoft\.com|"
    r"teams\.live\.com|[^\s<>\"]*webex\.com)/[^\s<>\")']+"
)


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()


def _other_people(ev: dict) -> list[dict] | None:
    """Other human invitees, or None if the event doesn't qualify."""
    attendees = ev.get("attendees", []) or []
    for att in attendees:
        if att.get("self") and att.get("responseStatus") == "declined":
            return None

    others = [a for a in attendees if not a.get("self") and not a.get("resource")]

    # Guest lists can be hidden from invitees. If someone else organised it
    # and I'm invited, that organiser is the "other person".
    organizer = ev.get("organizer", {}) or {}
    if not others and organizer and not organizer.get("self") and organizer.get("email"):
        if not organizer["email"].endswith("calendar.google.com"):
            others = [organizer]

    return others or None


PERSON_IN_TITLE = re.compile(r"\bwith\b|<>|\b1o1\b|\b1:1\b|\bmeet-?up\b", re.I)


def _looks_like_session(ev: dict) -> bool:
    """
    For guest-less imported feeds: is this a meeting with other people?
    - Solo blocks (physio, commute, writing) have no location and no real description.
    - Timetable entries (lectures, fairs) come from the uni system with a
      "Module: …" description; nobody invited you personally, so they don't
      count unless the title names a person ("… with Rob", "Zsanett <> Nico").
    - Everything else with a room/Teams link or invitation text counts.
    """
    title = ev.get("summary") or ""
    location = (ev.get("location") or "").strip()
    description = re.sub(r"\s+", " ", ev.get("description") or "").strip()
    if description.startswith("Module:"):
        return bool(PERSON_IN_TITLE.search(title))
    has_location = location not in ("", "-")
    has_description = bool(description) and not re.fullmatch(r"https?://\S+", description)
    return has_location or has_description


def _meeting_from_event(ev: dict, calendar_id: str) -> dict | None:
    if ev.get("status") == "cancelled" or ev.get("eventType") in SKIP_EVENT_TYPES:
        return None
    start_raw = (ev.get("start") or {}).get("dateTime")
    end_raw = (ev.get("end") or {}).get("dateTime")
    if not start_raw or not end_raw:  # all-day events
        return None

    title = str(ev.get("summary") or "Untitled meeting")
    if any(w in title.lower() for w in SKIP_TITLE_WORDS):
        return None
    if calendar_id in ALL_EVENTS_CALENDAR_IDS:
        # Imported feeds hide guest lists. Sessions with other people always carry a
        # room/Teams link, module info or invitation text; blocks "with myself"
        # (physio, commute, writing time) have neither, so skip those.
        if not _looks_like_session(ev):
            return None
        others = [a for a in (ev.get("attendees") or []) if not a.get("self") and not a.get("resource")]
    else:
        others = _other_people(ev)
        if not others:
            return None

    link = ev.get("hangoutLink", "")
    if not link:
        for ep in (ev.get("conferenceData") or {}).get("entryPoints", []) or []:
            if ep.get("entryPointType") == "video" and ep.get("uri"):
                link = ep["uri"]
                break
    if not link:
        match = LINK_RE.search(f"{ev.get('location', '')} {ev.get('description', '')}")
        if match:
            link = match.group(0)

    start = _parse_dt(start_raw)
    return {
        "id": str(ev.get("id")),
        "key": f"{ev.get('id')}|{start.isoformat()}",
        "title": title,
        "start": start,
        "end": _parse_dt(end_raw),
        "attendees": [str(a.get("displayName") or a.get("email") or "Guest") for a in others],
        "attendee_emails": [str(a["email"]) for a in others if a.get("email")],
        "meet_link": link,
        "html_link": ev.get("htmlLink", ""),
        "calendar": calendar_id,
    }


class MeetingWatcher:
    """Caches upcoming meetings and fires on_meeting_soon ~1 min before each."""

    SYNC_INTERVAL = 60
    RETRY_INTERVAL = 10
    TICK = 5

    def __init__(self, on_meeting_soon=None, on_status_change=None):
        self.on_meeting_soon = on_meeting_soon
        self.on_status_change = on_status_change
        self._events: list[dict] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._force_sync = True
        self._running = False
        self._thread = None
        self._last_sync_ok: datetime | None = None
        self._last_attempt = 0.0
        self._consecutive_failures = 0
        # "starting" | "ok" | "offline" | "reauth"
        self.status = "starting"
        self.status_detail = ""
        self._prompted = self._load_prompted()

    # ─── Public API ────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="MeetingWatcher", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self._wake.set()

    def wake(self):
        """Force an immediate sync + check (on wake from sleep, reconnect, etc.)."""
        self._force_sync = True
        self._wake.set()

    def upcoming(self, within_minutes: int = 36 * 60) -> list[dict]:
        """Cached meetings that haven't ended, starting within the window."""
        now = _now()
        horizon = now + timedelta(minutes=within_minutes)
        with self._lock:
            return [m for m in self._events if m["end"] > now and m["start"] <= horizon]

    def current_or_next(self) -> dict | None:
        """The meeting happening now (or starting within 15 min), if any."""
        now = _now()
        for m in self.upcoming(within_minutes=15):
            if m["start"] - timedelta(minutes=15) <= now < m["end"]:
                return m
        return None

    def mark_prompted(self, meeting: dict):
        self._prompted[meeting["key"]] = _now().isoformat()
        self._save_prompted()

    @property
    def last_sync(self) -> datetime | None:
        return self._last_sync_ok

    def status_text(self) -> str:
        if self.status == "ok" and self._last_sync_ok:
            return f"Calendar synced {self._last_sync_ok.strftime('%H:%M')}"
        if self.status == "offline":
            since = f" · schedule from {self._last_sync_ok.strftime('%H:%M')}" if self._last_sync_ok else ""
            return f"Offline, retrying{since}"
        if self.status == "reauth":
            return "Calendar disconnected"
        return "Connecting to calendar…"

    # ─── Loop ──────────────────────────────────────────────

    def _loop(self):
        while self._running:
            try:
                interval = self.RETRY_INTERVAL if self._consecutive_failures else self.SYNC_INTERVAL
                if self.status == "reauth" and not self._force_sync:
                    interval = 300  # don't hammer; the UI shows a Reconnect button
                if self._force_sync or time.time() - self._last_attempt >= interval:
                    self._force_sync = False
                    self._sync()
                self._check_due()
            except Exception as e:  # never let the watcher thread die
                print(f"⚠ Meeting watcher loop error: {e}", flush=True)
            self._wake.wait(self.TICK)
            self._wake.clear()

    def _sync(self):
        self._last_attempt = time.time()
        try:
            service = google_auth.calendar_service()
            now = _now()
            meetings: dict[str, dict] = {}
            for cal_id in CALENDAR_IDS:
                result = service.events().list(
                    calendarId=cal_id,
                    timeMin=(now - timedelta(hours=3)).isoformat(),
                    timeMax=(now + timedelta(hours=48)).isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=250,
                ).execute(num_retries=2)
                for ev in result.get("items", []):
                    m = _meeting_from_event(ev, cal_id)
                    if m and m["key"] not in meetings:
                        meetings[m["key"]] = m
            with self._lock:
                self._events = sorted(meetings.values(), key=lambda m: m["start"])
            self._last_sync_ok = _now()
            self._consecutive_failures = 0
            self._set_status("ok", "")
        except google_auth.AuthRequired as e:
            self._consecutive_failures += 1
            self._set_status("reauth", str(e))
        except Exception as e:
            # NetworkUnavailable, DNS errors, socket timeouts, 5xx: keep the
            # cached schedule and keep prompting from it.
            self._consecutive_failures += 1
            if self._consecutive_failures in (1, 10) or self._consecutive_failures % 60 == 0:
                print(f"⚠ Calendar sync failed ({self._consecutive_failures}x): {e}", flush=True)
            self._set_status("offline", str(e))

    def _check_due(self):
        now = _now()
        due = []
        with self._lock:
            for m in self._events:
                seconds_until = (m["start"] - now).total_seconds()
                if -PROMPT_LATE_GRACE_SECONDS <= seconds_until <= PROMPT_LEAD_SECONDS and m["end"] > now:
                    if m["key"] not in self._prompted:
                        due.append(m)
        for m in due:
            self.mark_prompted(m)
            print(f"🔔 Meeting due: {m['title']} @ {m['start'].strftime('%H:%M')}", flush=True)
            if self.on_meeting_soon:
                try:
                    self.on_meeting_soon(m)
                except Exception as e:
                    print(f"⚠ on_meeting_soon callback failed: {e}", flush=True)

    # ─── Helpers ───────────────────────────────────────────

    def _set_status(self, status: str, detail: str):
        changed = status != self.status
        self.status, self.status_detail = status, detail
        if changed:
            print(f"📅 Calendar status: {status} {('· ' + detail[:120]) if detail else ''}", flush=True)
        if self.on_status_change and (changed or status == "ok"):
            try:
                self.on_status_change()
            except Exception:
                pass

    def _load_prompted(self) -> dict:
        try:
            data = json.loads(PROMPTED_PATH.read_text())
        except Exception:
            return {}
        cutoff = _now() - timedelta(days=2)
        return {k: v for k, v in data.items() if _parse_dt(v) > cutoff}

    def _save_prompted(self):
        try:
            tmp = PROMPTED_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._prompted, indent=1))
            tmp.replace(PROMPTED_PATH)
        except Exception as e:
            print(f"⚠ Could not persist prompted events: {e}", flush=True)
