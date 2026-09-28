# 🎙️ Meeting Recorder

A Granola-style meeting recorder for macOS. About a minute before each calendar
meeting with other people, a clean white panel pops up: record it, jot notes
while you talk, then get one summary that merges **your** notes with the
transcript. Your points are kept and highlighted. Copy it, save it as
`.md`/`.txt`, or have it added straight to a Notion page or Google Doc.

## How it works

1. **1 min before a meeting** (Google Calendar, at least one other person invited),
   the panel asks where the summary should go: copy/save a file, or a Notion /
   Google Doc link.
2. **During the call:** a notepad with Markdown shortcuts (`#` heading, `-` bullet,
   ⌘B/⌘I/⌘U, Tab to indent). It records your mic **and** the call audio. For call
   audio the app builds a temporary output (your headphones/speakers + BlackHole)
   and removes it when you stop.
3. **At the scheduled end:** "Wrap up?" (+10 min, or auto-stop 5 min later).
   There's a 90-min cap with a warning and a "+30 min" button.
4. **After the call:** add your notes as bullets or as a Notion / Google Doc link.
   The model (default `gpt-6-sol`, high reasoning) merges them with the
   transcript (`gpt-4o-transcribe`), keeping your bullets verbatim and highlighted.
5. **Result:** Copy (rich text), Save `.md` / `.txt`, or append to the Notion page
   or Google Doc (tab-aware).

Reliability: the calendar is cached locally and prompts fire from the cache every
5 s. The app re-syncs on wake, catches meetings that started while the Mac slept,
dedupes prompts across restarts, and never opens a sign-in page on its own. A
self-test checks the whole chain (mic permission, call audio, transcription,
calendar, model, Notion, Docs).

## Setup

```bash
brew install blackhole-2ch switchaudio-osx ffmpeg   # reboot after BlackHole
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.template .env                                # add your keys
```

Google: create an OAuth **Desktop** client in Google Cloud Console, enable the
Calendar API (and the Docs API if you use Google Doc links), and save it as
`credentials.json`. The first run asks you to sign in.

Run it directly with `python app.py`. To run it in the background at login as an
app bundle (needed so macOS grants the microphone), use `bash scripts/deploy.sh`,
which copies the code to `~/meeting-recorder-proxy`, builds and signs
`Meeting Recorder.app`, and (re)starts the launchd job.

Self-test: menu bar → **Run Self-Test**, or
`open -n -a ~/meeting-recorder-proxy/"Meeting Recorder.app" --args --run selftest`.

## Project structure

```
app.py              menu bar app, floating 🎙 button, panel (WKWebView), session flow
ui/panel.html       the panel UI (views, notes editor, paste handling)
meeting_watcher.py  cached calendar sync + prompt scheduler
google_auth.py      Calendar/Docs OAuth (separate tokens, no surprise browser pop-ups)
recorder.py         mic + BlackHole capture
audio_route.py      temporary multi-output device (headphones/speakers + BlackHole)
transcriber.py      gpt-4o-transcribe (diarized)
synthesizer.py      notes + transcript → structured summary; md/txt/html/Notion renderers
destinations.py     read notes from / append summaries to Notion pages and Google Docs
sessions.py         crash-safe per-meeting state
llm.py              shared OpenAI Responses API client
selftest.py         end-to-end checks
scripts/            deploy.sh, build_runtime_app.sh, native launcher (launcher/launcher.c)
```

## License

MIT
