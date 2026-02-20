from __future__ import annotations
import threading
import time
import subprocess
from datetime import datetime, timedelta, timezone
import os.path

# Google API imports
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

class MeetingWatcher:
    """Watches for upcoming calendar meetings with guests via Google Calendar API."""

    def __init__(self, on_meeting_detected=None):
        self.on_meeting_detected = on_meeting_detected
        self._running = False
        self._thread = None
        self._service = None
        self._access_granted = False
        self._notified_events = set()
        self.alert_minutes_before = 0
        self.check_interval = 30

    def request_calendar_access(self) -> bool:
        creds = None
        # The file token.json stores the user's access and refresh tokens
        if os.path.exists('token.json'):
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
            
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception:
                    creds = None
                    
            if not creds:
                if not os.path.exists('credentials.json'):
                    print("⚠ credentials.json not found! Please place credentials.json in this directory to use Google Calendar.")
                    return False
                print("⏳ Opening browser to authenticate Google Calendar...")
                flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                creds = flow.run_local_server(port=0)
                
            with open('token.json', 'w') as token:
                token.write(creds.to_json())

        try:
            self._service = build('calendar', 'v3', credentials=creds)
            print("✅ Google Calendar access granted")
            self._access_granted = True
            return True
        except Exception as e:
            print(f"❌ Custom Calendar access error: {e}")
            return False

    def start(self):
        if not self._access_granted:
            print("⚠ Calendar access not granted. Call request_calendar_access() first.")
            return

        self._running = True
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()
        print("🔍 Meeting watcher started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        print("⏹ Meeting watcher stopped")

    def get_upcoming_meetings(self, within_minutes: int = 60) -> list[dict]:
        if not self._service or not self._access_granted:
            return []

        now = datetime.now(timezone.utc).isoformat()
        end = (datetime.now(timezone.utc) + timedelta(minutes=within_minutes)).isoformat()

        try:
            events_result = self._service.events().list(
                calendarId='primary', timeMin=now, timeMax=end,
                singleEvents=True, orderBy='startTime').execute()
            events = events_result.get('items', [])
            
            meetings = []
            for ev in events:
                attendees_data = ev.get('attendees', [])
                if len(attendees_data) <= 1:
                    continue
                    
                attendee_names = []
                attendee_emails = []
                for att in attendees_data:
                    # skip self
                    if att.get('self', False):
                        continue
                    name = att.get('displayName') or att.get('email') or "Unknown"
                    email = att.get('email', '')
                    attendee_names.append(str(name))
                    if email:
                        attendee_emails.append(str(email))
                    
                if not attendee_names:
                    continue

                start_str = ev.get('start', {}).get('dateTime') or ev.get('start', {}).get('date')
                end_str = ev.get('end', {}).get('dateTime') or ev.get('end', {}).get('date')
                
                if not start_str or not end_str:
                    continue

                start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))

                # Remove tzinfo to match original code returning naive local datetime
                start_dt = start_dt.astimezone().replace(tzinfo=None)
                end_dt = end_dt.astimezone().replace(tzinfo=None)

                # Find meet link
                meet_link = ev.get('hangoutLink', "")
                if not meet_link:
                    notes = ev.get('description', '')
                    import re
                    # Look for google meet, zoom, teams, etc.
                    match = re.search(r'https://(?:meet\.google\.com|.*zoom\.us|.*teams\.microsoft\.com)/[\w-?/=&]+', notes)
                    if match:
                        meet_link = match.group(0)

                meetings.append({
                    "id": str(ev.get('id')),
                    "title": str(ev.get('summary', 'Untitled Meeting')),
                    "start": start_dt,
                    "end": end_dt,
                    "attendees": attendee_names,
                    "attendee_emails": attendee_emails,
                    "meet_link": meet_link,
                    "calendar": "Google Calendar"
                })
            return meetings
            
        except Exception as e:
            print(f"Error fetching Google Calendar: {e}")
            return []

    def _watch_loop(self):
        while self._running:
            try:
                meetings = self.get_upcoming_meetings(
                    within_minutes=30
                )

                for meeting in meetings:
                    event_id = meeting["id"]
                    if event_id in self._notified_events:
                        continue
                        
                    time_until = (meeting["start"] - datetime.now()).total_seconds()
                    
                    # Only trigger if the meeting is starting right now (within a 1 minute grace period)
                    # We don't want to auto-join a meeting that's been running for 20 minutes already
                    if -60 <= time_until <= self.alert_minutes_before * 60:
                        self._notified_events.add(event_id)
                        if self.on_meeting_detected:
                            self.on_meeting_detected(meeting)

                if len(self._notified_events) > 100:
                    self._notified_events.clear()

            except Exception as e:
                print(f"⚠ Meeting watcher error: {e}")

            time.sleep(self.check_interval)

    def is_google_meet_active(self) -> bool:
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 'tell application "Google Chrome" to get URL of active tab of first window'],
                capture_output=True, text=True, timeout=3
            )
            if "meet.google.com" in result.stdout:
                return True
        except Exception:
            pass

        try:
            result = subprocess.run(
                ["osascript", "-e",
                 'tell application "Safari" to get URL of current tab of first window'],
                capture_output=True, text=True, timeout=3
            )
            if "meet.google.com" in result.stdout:
                return True
        except Exception:
            pass

        return False
