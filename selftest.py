from __future__ import annotations
"""
Self-test — checks the whole chain the way a real meeting uses it.

Run it with the app's identity (so it has the app's microphone permission):
    open -n -a ~/meeting-recorder-proxy/"Meeting Recorder.app" --args --run selftest
    cat /tmp/mr_selftest.txt
or from the menu bar: "Run Self-Test".

The audio check plays a short spoken phrase through your speakers while
recording, then transcribes it. That proves microphone permission, call-audio
capture (BlackHole + Multi-Output) and transcription in one go.
"""
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import config  # noqa: E402  (fixes PATH before pydub is imported)

PHRASE = "This is the Meeting Recorder self test. The secret word is pineapple."
KEYWORD = "pineapple"


def run_all(play_audio: bool = True, log=print) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = ""):
        results.append((name, ok, detail))
        log(f"{'✅' if ok else '❌'} {name}{' · ' + detail if detail else ''}")

    def attempt(name: str, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {str(e)[:180]}"
        record(name, ok, detail)
        return ok

    # 1. Command-line tools launchd has to find
    for tool in ("ffmpeg", "SwitchAudioSource"):
        path = shutil.which(tool)
        record(f"Tool: {tool}", bool(path), path or "not found (brew install ffmpeg switchaudio-osx)")

    # 2. Audio devices
    from recorder import MeetingRecorder
    rec = MeetingRecorder()
    setup = rec.check_setup()
    record("BlackHole 2ch input", setup["blackhole_found"], "" if setup["blackhole_found"] else "brew install blackhole-2ch, then reboot")


    # 3. End-to-end audio → transcript
    def audio_check():
        from llm import get_client
        phrase_path = config.STATE_DIR / "selftest_phrase.mp3"
        if not phrase_path.exists():
            speech = get_client().audio.speech.create(model="gpt-4o-mini-tts", voice="alloy", input=PHRASE)
            phrase_path.write_bytes(speech.content)
        rec.start()
        try:
            time.sleep(1.0)
            if play_audio:
                subprocess.run(["afplay", "-v", "0.6", str(phrase_path)], timeout=20)
            time.sleep(1.2)
        finally:
            wav = rec.stop("selftest")
        mic, system = rec.mic_peak, rec.system_peak
        levels = f"mic peak {mic:.3f}, call-audio peak {system:.3f}"
        record("Automatic call-audio route", rec.output_switched, rec.route.note or "current output + BlackHole")
        record("Microphone permission", mic > 0, levels if mic > 0 else
               "mic delivers pure silence → allow “Meeting Recorder” in Privacy & Security › Microphone")
        record("Call audio (BlackHole)", system > 0.001 if play_audio else True,
               "captured the test phrase" if system > 0.001 else "no system audio reached BlackHole")
        if not wav:
            return False, "no audio file written"
        from transcriber import transcribe_audio
        try:
            text = transcribe_audio(wav).get("text", "")
        finally:
            Path(wav).unlink(missing_ok=True)
            for f in config.TRANSCRIPTS_DIR.glob(f"{Path(wav).stem}_transcript.json"):
                f.unlink(missing_ok=True)
        heard = KEYWORD in text.lower()
        return heard, f"heard: “{text.strip()[:90]}”" if text.strip() else "transcript empty"
    attempt("Record → transcribe", audio_check)

    # 4. Google Calendar
    def calendar_check():
        import google_auth
        import meeting_watcher as mw
        svc = google_auth.calendar_service()
        now = datetime.now(timezone.utc)
        items, meetings = [], []
        for cal in config.CALENDAR_IDS:
            evs = svc.events().list(calendarId=cal, timeMin=now.isoformat(),
                                    timeMax=(now + timedelta(hours=48)).isoformat(),
                                    singleEvents=True, orderBy="startTime").execute(num_retries=2).get("items", [])
            items += evs
            meetings += [m for m in (mw._meeting_from_event(e, cal) for e in evs) if m]
        meetings.sort(key=lambda m: m["start"])
        nxt = f"; next: {meetings[0]['title'][:40]} {meetings[0]['start'].strftime('%a %H:%M')}" if meetings else ""
        return True, f"{len(config.CALENDAR_IDS)} calendars, {len(items)} events in 48 h, {len(meetings)} meetings{nxt}"
    attempt("Google Calendar", calendar_check)

    # 5. LLM
    def llm_check():
        from llm import complete
        reply = complete("Reply with exactly: OK", "ping")
        return "OK" in reply.upper(), f"{config.LLM_MODEL} ({config.LLM_REASONING_EFFORT} reasoning) replied “{reply[:20]}”"
    attempt("Summary model", llm_check)

    # 6. Notion
    def notion_check():
        from notion_client import Client
        me = Client(auth=config.NOTION_TOKEN).users.me()
        return True, f"integration “{me.get('name') or me.get('bot', {}).get('owner', {}).get('type', '?')}”"
    attempt("Notion token", notion_check)

    # 7. Google Docs
    def docs_check():
        if not config.DOCS_TOKEN_PATH.exists():
            return False, "not connected yet; the first Google Doc link opens a one-time Google consent page"
        import google_auth
        google_auth.docs_service()
        return True, "token valid"
    attempt("Google Docs access", docs_check)

    # 8. Output folder (Google Drive)
    def output_check():
        probe = config.TRANSCRIPTS_DIR / ".write_test"
        probe.write_text("ok")
        try:
            probe.unlink()
        except OSError:
            pass
        return True, str(config.OUTPUT_DIR)
    attempt("Output folder writable", output_check)

    passed = sum(ok for _, ok, _ in results)
    log(f"\n{passed}/{len(results)} checks passed · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return results


if __name__ == "__main__":
    run_all(play_audio="--silent" not in sys.argv, log=lambda line: print(line, flush=True))
