from meeting_watcher import MeetingWatcher

def check():
    watcher = MeetingWatcher()
    if watcher.request_calendar_access():
        meetings = watcher.get_upcoming_meetings(within_minutes=120)
        print(f"Detected {len(meetings)} meetings in the next 2 hours:")
        for m in meetings:
            print(m)
    else:
        print("Access denied.")

check()
