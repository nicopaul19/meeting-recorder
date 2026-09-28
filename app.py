from __future__ import annotations
"""
Meeting Recorder — macOS menu-bar app with a Granola-style panel.

Flow:
  1. ~1 min before a calendar meeting with at least one other person, the panel
     pops up: where should the summary go (copy/save file, or a Notion / Google
     Doc link)? → Start recording.
  2. While recording: a notepad for my own bullets (autosaved).
  3. At the scheduled end: "Wrap up?" (auto-stops 5 min later unless extended).
  4. After stopping: "How did it go?" asks for MY notes, as bullets or a
     Notion / Google Doc link. Transcription runs in the background meanwhile.
  5. The synthesized summary (my bullets highlighted) → Copy / Save .md / .txt,
     or appended straight to the Notion page / Google Doc.

UI is HTML (ui/panel.html) inside a native floating NSPanel via WKWebView.
All state lives on the main thread; workers post results back via _on_main.
"""
import os
import errno
import fcntl
import json
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import AppKit
import objc
import WebKit
from Foundation import NSObject, NSTimer, NSURL

PROJECT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_DIR))
os.chdir(PROJECT_DIR)

INSTANCE_LOCK_PATH = Path("/tmp/meeting_recorder_app.lock")
AUTOSTART_REQUEST_PATH = Path("/tmp/meeting_recorder_autostart.json")
SHOW_REQUEST_PATH = Path("/tmp/meeting_recorder_show")
_INSTANCE_LOCK_FILE = None


def _acquire_single_instance_lock():
    global _INSTANCE_LOCK_FILE

    lock_file = open(INSTANCE_LOCK_PATH, "a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lock_file.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            print("Meeting Recorder is already running; asking it to show its window.")
            SHOW_REQUEST_PATH.write_text("1")
            return False
        raise

    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    _INSTANCE_LOCK_FILE = lock_file
    return True


def _consume_autostart_request():
    try:
        raw = AUTOSTART_REQUEST_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None

    try:
        AUTOSTART_REQUEST_PATH.unlink(missing_ok=True)
    except OSError:
        pass

    if not raw:
        return {}

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"title": raw}

from config import (
    OPENAI_API_KEY, NOTION_TOKEN, TRANSCRIPTS_DIR, SUMMARIES_DIR, UI_DIR, DATA_DIR,
    LLM_MODEL, LLM_REASONING_EFFORT, MAX_RECORDING_MINUTES, MAX_WARNING_MINUTES, EXTEND_MINUTES,
)
from recorder import MeetingRecorder
from transcriber import transcribe_audio, format_transcript_as_markdown
from meeting_watcher import MeetingWatcher
from local_store import save_meeting
from sessions import Session, load_recent
from synthesizer import MeetingSummary, synthesize, to_markdown, to_text, to_html
import destinations
import google_auth

PANEL_W, PANEL_H = 420, 640
TITLEBAR_H = 28
BG = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(251 / 255, 251 / 255, 249 / 255, 1.0)
END_GRACE_MINUTES = 5
KEEP_GOING_MINUTES = 10


def _slug(text: str) -> str:
    return re.sub(r"[^\w\s-]", "", text)[:50].strip().replace(" ", "_") or "meeting"


def _fmt_dur(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m} min"


def _write_output(folder: Path, name: str, content: str) -> Path:
    """Write to the Google Drive output folder; fall back to local data/ if Drive fails."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(content, encoding="utf-8")
        return path
    except OSError as e:
        local = DATA_DIR / folder.name
        local.mkdir(parents=True, exist_ok=True)
        print(f"⚠ Couldn't write to {folder} ({e}); saved locally instead", flush=True)
        path = local / name
        path.write_text(content, encoding="utf-8")
        return path


def _notify(title: str, message: str):
    """macOS banner notification (works from a background app via osascript)."""
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)} sound name \"Glass\""
    try:
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _day_label(dt: datetime, now: datetime) -> str:
    days = (dt.date() - now.date()).days
    return "Today" if days == 0 else "Tomorrow" if days == 1 else dt.strftime("%A")


def _py(obj):
    """NSDictionary/NSArray/NSString from the JS bridge → plain Python."""
    if isinstance(obj, (str, bool, int, float)) or obj is None:
        return obj
    if hasattr(obj, "items"):
        return {str(k): _py(v) for k, v in obj.items()}
    if hasattr(obj, "__iter__"):
        return [_py(v) for v in obj]
    return str(obj)


WKScriptMessageHandler = objc.protocolNamed("WKScriptMessageHandler")


class Bridge(NSObject, protocols=[WKScriptMessageHandler]):
    """Receives window.webkit.messageHandlers.bridge.postMessage(...) calls."""

    def initWithDelegate_(self, delegate):
        self = objc.super(Bridge, self).init()
        if self is None:
            return None
        self._delegate = delegate
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        try:
            self._delegate.handle_message(_py(message.body()))
        except Exception:
            traceback.print_exc()


class FloatingButton(AppKit.NSButton):
    """Always-visible 🎙 pill: click opens the panel, drag to move it."""

    def mouseDown_(self, event):
        self._down = event.locationInWindow()
        self._moved = False

    def mouseDragged_(self, event):
        win = self.window()
        p = event.locationInWindow()
        o = win.frame().origin
        win.setFrameOrigin_((o.x + p.x - self._down.x, o.y + p.y - self._down.y))
        self._moved = True

    def mouseUp_(self, event):
        if getattr(self, "_moved", False):
            o = self.window().frame().origin
            AppKit.NSUserDefaults.standardUserDefaults().setObject_forKey_([o.x, o.y], "pillOrigin")
        else:
            self.target().onPillClick_(self)


class AppDelegate(NSObject):

    def init(self):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.recorder = MeetingRecorder()
        self.watcher = MeetingWatcher(
            on_meeting_soon=lambda m: self._on_main(lambda: self._meeting_soon(m)),
            on_status_change=lambda: self._on_main(self._push),
        )
        self.sessions: dict[str, Session] = {s.id: s for s in load_recent()}
        self.active_id: str | None = None     # session currently recording
        self.view_id: str | None = None       # session shown in the panel
        self.prompt: dict | None = None       # meeting awaiting "start recording?"
        self.up_next: dict | None = None      # next meeting while recording
        self.new_recording: dict | None = None  # "name this recording" form
        self.mic_status = "unknown"            # unknown | ok | blocked
        self._selftest_running = False
        self._stopping = False
        self._ui_ready = False
        self._tick_count = 0
        self._launch_request = _consume_autostart_request()
        return self

    # ───────────────────────────────────────────────────────
    # Lifecycle
    # ───────────────────────────────────────────────────────

    def applicationDidFinishLaunching_(self, notification):
        self._build_status_item()
        self._build_edit_menu()
        self._build_panel()
        self._build_pill()
        self._register_global_hotkey()
        self._observe_wake()
        self.watcher.start()
        threading.Thread(target=self._probe_mic, daemon=True).start()
        threading.Thread(target=self._cleanup_audio_route, daemon=True).start()
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, objc.selector(self.tick_, signature=b"v@:@"), None, True
        )
        # Resume transcriptions that were interrupted by a restart
        for s in self.sessions.values():
            if s.status == "awaiting_notes" and not s.transcript and s.audio_path and Path(s.audio_path).exists():
                self._start_transcription(s)
        if self._launch_request is not None:
            req = self._launch_request if isinstance(self._launch_request, dict) else {}
            self._start_session(title=str(req.get("title", "")).strip() or None, meeting=self.watcher.current_or_next())
        # Open the panel on launch too: the menu bar icon can hide behind the notch.
        self._on_main(lambda: self._show_panel(activate=True))
        print(f"Meeting Recorder ready · LLM {LLM_MODEL} ({LLM_REASONING_EFFORT} reasoning)", flush=True)

    @objc.python_method
    def _probe_mic(self):
        """0.3 s mic check at launch: exact silence means macOS blocks input."""
        # macOS permission status is the source of truth (a level check gives false
        # alarms: Bluetooth mics like AirPods deliver silence while waking up).
        status = "unknown"
        try:
            import AVFoundation
            media = AVFoundation.AVMediaTypeAudio
            auth = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(media)
            if auth == 0:  # never asked: show the system prompt once
                done = threading.Event()
                AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(media, lambda ok: done.set())
                done.wait(120)
                auth = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(media)
            status = "ok" if auth == 3 else "blocked"
        except Exception as e:
            print(f"⚠ Mic permission check failed: {e}", flush=True)
        print(f"🎤 Microphone: {status}", flush=True)

        def apply():
            self.mic_status = status
            self._push()
            if status == "blocked":
                _notify("Microphone blocked",
                        "Meeting Recorder can't hear anything. Open it from the menu bar to fix.")
        self._on_main(apply)

    @objc.python_method
    def _cleanup_audio_route(self):
        try:
            import audio_route
            audio_route.cleanup_leftovers()
        except Exception as e:
            print(f"⚠ Audio route cleanup failed: {e}", flush=True)

    @objc.python_method
    def _observe_wake(self):
        center = AppKit.NSWorkspace.sharedWorkspace().notificationCenter()
        center.addObserver_selector_name_object_(
            self, objc.selector(self.didWake_, signature=b"v@:@"),
            AppKit.NSWorkspaceDidWakeNotification, None)
        center.addObserver_selector_name_object_(
            self, objc.selector(self.didWake_, signature=b"v@:@"),
            AppKit.NSWorkspaceSessionDidBecomeActiveNotification, None)

    def didWake_(self, notification):
        print("💤→☀️  Wake/unlock: resyncing calendar", flush=True)
        self.watcher.wake()

    # ───────────────────────────────────────────────────────
    # Menu bar
    # ───────────────────────────────────────────────────────

    @objc.python_method
    def _build_status_item(self):
        self._status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength)
        self._status_item.button().setTitle_("🎙️")
        menu = AppKit.NSMenu.alloc().init()
        for title, sel, key in (
            ("Open Meeting Recorder", "onOpen:", ""),
            ("Start / Stop Recording  ⌘⇧R", "onToggleRecording:", ""),
            ("Sync Calendar Now", "onSyncCalendar:", ""),
            (None, None, None),
            ("Open Summaries Folder", "onOpenSummaries:", ""),
            ("Check Audio Setup", "onCheckSetup:", ""),
            ("Run Self-Test (plays a short test phrase)", "onSelfTest:", ""),
            (None, None, None),
            ("Quit (restarts at next login)", "onQuit:", "q"),
        ):
            if title is None:
                menu.addItem_(AppKit.NSMenuItem.separatorItem())
                continue
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, key)
            item.setTarget_(self)
            menu.addItem_(item)
        self._status_item.setMenu_(menu)

    @objc.python_method
    def _build_edit_menu(self):
        """Standard Edit menu so ⌘C/⌘V/⌘X/⌘A/⌘Z reach the web view's text fields."""
        main = AppKit.NSMenu.alloc().init()
        edit_item = AppKit.NSMenuItem.alloc().init()
        main.addItem_(edit_item)
        edit = AppKit.NSMenu.alloc().initWithTitle_("Edit")
        for title, sel, key in (("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), ("Cut", "cut:", "x"),
                                ("Copy", "copy:", "c"), ("Paste", "paste:", "v"),
                                ("Select All", "selectAll:", "a")):
            edit.addItemWithTitle_action_keyEquivalent_(title, sel, key)
        edit_item.setSubmenu_(edit)
        AppKit.NSApp.setMainMenu_(main)

    def onOpen_(self, sender):
        self._show_panel(activate=True)

    def onToggleRecording_(self, sender):
        self._toggle_recording()

    def onSyncCalendar_(self, sender):
        self.watcher.wake()

    def onOpenSummaries_(self, sender):
        subprocess.run(["open", str(SUMMARIES_DIR)])

    def onCheckSetup_(self, sender):
        status = self.recorder.check_setup()
        lines = [
            f"{'✅' if status['blackhole_found'] else '❌'} BlackHole 2ch",
            f"{'✅' if status['mic_found'] else '❌'} Microphone",
            f"{'✅' if status['blackhole_found'] else '❌'} Call-audio route (automatic)",
            f"{'✅' if status['switchaudio_installed'] else '❌'} SwitchAudioSource",
            f"{'✅' if self.watcher.status == 'ok' else '❌'} Google Calendar ({self.watcher.status_text()})",
        ]
        if not status["blackhole_found"]:
            lines.append("\n→ Reboot for BlackHole to appear")
        if not status["multi_output_found"]:
            lines.append("→ Create a Multi-Output Device in Audio MIDI Setup")
        self._alert("Audio Setup", "\n".join(lines))

    def onSelfTest_(self, sender):
        if self.recorder.is_recording or self._stopping or self._selftest_running:
            self._alert("Self-Test", "Stop the current recording first.")
            return
        self._selftest_running = True
        _notify("Self-test running", "You'll hear a short test phrase. Results in about 30 seconds.")

        def work():
            lines = []
            try:
                import selftest
                selftest.run_all(log=lines.append)
            except Exception as e:
                lines.append(f"Self-test crashed: {e}")
            print("\n".join(["── Self-test ──"] + lines), flush=True)

            def done():
                self._selftest_running = False
                self._alert("Self-Test", "\n".join(lines))
                self.mic_status = "ok" if any(l.startswith("✅ Microphone") for l in lines) else self.mic_status
                self._push()
            self._on_main(done)
        threading.Thread(target=work, daemon=True).start()

    def onQuit_(self, sender):
        if self.recorder.is_recording:
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("Recording in progress")
            alert.setInformativeText_("Stop the recording and quit? The audio will be saved first.")
            alert.addButtonWithTitle_("Stop & Quit")
            alert.addButtonWithTitle_("Cancel")
            AppKit.NSApp.activateIgnoringOtherApps_(True)
            if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
                return
            s = self.sessions.get(self.active_id)
            path = self.recorder.stop(s.title if s else "")
            if s:
                s.audio_path = str(path or "")
                s.status = "awaiting_notes"
                s.ended_at = datetime.now().isoformat()
                s.save()
        self.watcher.stop()
        # launchd keeps the app alive; unload the job so Quit sticks until next login.
        subprocess.Popen(["launchctl", "bootout", f"gui/{os.getuid()}/com.nicolas.meetingrecorder"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        AppKit.NSApp.terminate_(None)

    # ───────────────────────────────────────────────────────
    # Panel (NSPanel + WKWebView)
    # ───────────────────────────────────────────────────────

    @objc.python_method
    def _build_panel(self):
        # A regular (activating) window: clicking it brings Meeting Recorder to the
        # front. A non-activating panel can't be raised by clicking once another
        # app's window covers it.
        style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable |
                 AppKit.NSWindowStyleMaskResizable | AppKit.NSWindowStyleMaskFullSizeContentView)
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (PANEL_W, PANEL_H)), style, AppKit.NSBackingStoreBuffered, False)
        panel.setTitle_("Meeting Recorder")
        panel.setTitlebarAppearsTransparent_(True)
        panel.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        panel.setMovableByWindowBackground_(True)
        panel.setBackgroundColor_(BG)
        panel.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameAqua))
        panel.setFloatingPanel_(False)
        panel.setLevel_(AppKit.NSNormalWindowLevel)
        panel.setHidesOnDeactivate_(False)
        panel.setBecomesKeyOnlyIfNeeded_(False)
        panel.setReleasedWhenClosed_(False)
        panel.setMinSize_((360, 460))
        panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces |
            AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
        for btn in (AppKit.NSWindowMiniaturizeButton, AppKit.NSWindowZoomButton):
            b = panel.standardWindowButton_(btn)
            if b:
                b.setHidden_(True)
        panel.setDelegate_(self)

        config = WebKit.WKWebViewConfiguration.alloc().init()
        self._bridge = Bridge.alloc().initWithDelegate_(self)
        config.userContentController().addScriptMessageHandler_name_(self._bridge, "bridge")
        # Leave a native strip at the top so the titlebar stays draggable.
        frame = ((0, 0), (PANEL_W, PANEL_H - TITLEBAR_H))
        webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(frame, config)
        webview.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        webview.setValue_forKey_(False, "drawsBackground")
        panel.contentView().addSubview_(webview)
        html = (UI_DIR / "panel.html").read_text(encoding="utf-8")
        webview.loadHTMLString_baseURL_(html, NSURL.fileURLWithPath_(str(UI_DIR) + "/"))

        self.panel, self.webview = panel, webview

    @objc.python_method
    def _build_pill(self):
        W, H = 46, 30
        pill = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (W, H)),
            AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered, False)
        pill.setLevel_(AppKit.NSStatusWindowLevel)
        pill.setOpaque_(False)
        pill.setBackgroundColor_(AppKit.NSColor.clearColor())
        pill.setHasShadow_(True)
        pill.setHidesOnDeactivate_(False)
        pill.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces |
            AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary |
            AppKit.NSWindowCollectionBehaviorStationary)
        btn = FloatingButton.alloc().initWithFrame_(((0, 0), (W, H)))
        btn.setBordered_(False)
        btn.setWantsLayer_(True)
        btn.layer().setCornerRadius_(H / 2)
        btn.setFont_(AppKit.NSFont.systemFontOfSize_(15))
        btn.setTarget_(self)
        btn.setToolTip_("Meeting Recorder: click to open, drag to move")
        pill.setContentView_(btn)
        saved = AppKit.NSUserDefaults.standardUserDefaults().objectForKey_("pillOrigin")
        if saved:
            pill.setFrameOrigin_((float(saved[0]), float(saved[1])))
        else:
            vf = AppKit.NSScreen.mainScreen().visibleFrame()
            pill.setFrameOrigin_((vf.origin.x + vf.size.width - W - 12, vf.origin.y + vf.size.height - H - 8))
        pill.orderFrontRegardless()
        self.pill, self.pill_btn = pill, btn
        self._style_pill()

    @objc.python_method
    def _style_pill(self):
        recording = self.recorder.is_recording
        if recording:
            secs = int(self.recorder.elapsed_seconds)
            title, bg, fg = f"{secs // 60}:{secs % 60:02d}", (0.90, 0.28, 0.30), AppKit.NSColor.whiteColor()
        else:
            title, bg, fg = "🎙", (1, 1, 1), AppKit.NSColor.blackColor()
        w = 64 if recording else 46
        f = self.pill.frame()
        if f.size.width != w:
            self.pill.setFrame_display_(((f.origin.x + f.size.width - w, f.origin.y), (w, f.size.height)), True)
            self.pill_btn.setFrame_(((0, 0), (w, f.size.height)))
        self.pill_btn.layer().setBackgroundColor_(
            AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*bg, 0.96).CGColor())
        self.pill_btn.setAttributedTitle_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
            title, {AppKit.NSForegroundColorAttributeName: fg,
                    AppKit.NSFontAttributeName: AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(13, AppKit.NSFontWeightSemibold)}))

    def onPillClick_(self, sender):
        if self.panel.isVisible() and self.panel.isKeyWindow():
            self.windowShouldClose_(None)
        else:
            self._show_panel(activate=True)

    @objc.python_method
    def _show_panel(self, activate: bool = False, alert: bool = False):
        if not self.panel.isVisible():
            screen = AppKit.NSScreen.mainScreen().visibleFrame()
            w, h = self.panel.frame().size.width, self.panel.frame().size.height
            x = screen.origin.x + screen.size.width - w - 16
            y = screen.origin.y + screen.size.height - h - 12
            self.panel.setFrameOrigin_((x, y))
        if activate:
            AppKit.NSApp.activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        self.panel.orderFrontRegardless()
        if alert:
            sound = AppKit.NSSound.soundNamed_("Glass")
            if sound:
                sound.play()
        self._push()

    def applicationDidBecomeActive_(self, notification):
        # Clicking the panel (or Dock/⌘-Tab) activates the app: make sure it's in front.
        if self.panel.isVisible():
            self.panel.makeKeyAndOrderFront_(None)

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, has_visible):
        self._show_panel(activate=True)
        return True

    def windowShouldClose_(self, sender):
        self.panel.orderOut_(None)
        if self.view_id and self.view_id != self.active_id:
            s = self.sessions.get(self.view_id)
            if s and s.status == "done":
                self.view_id = None  # next open shows home
        return False

    # ───────────────────────────────────────────────────────
    # State → UI
    # ───────────────────────────────────────────────────────

    @objc.python_method
    def _on_main(self, block):
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(block)

    @objc.python_method
    def _toast(self, msg: str):
        if self._ui_ready:
            self.webview.evaluateJavaScript_completionHandler_(f"window.toast({json.dumps(msg)})", None)

    @objc.python_method
    def _needs_attention(self) -> bool:
        s = self.sessions.get(self.active_id)
        return bool(self.prompt or (s and (s.end_prompted or s.max_warned or s.audio_blocked)))

    @objc.python_method
    def _update_level(self):
        """Float above other apps only while something needs you; otherwise behave
        like a normal window so Notion etc. can go on top (the 🎙 pill brings it back)."""
        level = AppKit.NSFloatingWindowLevel if self._needs_attention() else AppKit.NSNormalWindowLevel
        if self.panel.level() != level:
            self.panel.setLevel_(level)

    @objc.python_method
    def _push(self):
        self._update_level()
        if not self._ui_ready:
            return
        try:
            state = json.dumps(self._state(), default=str)
        except Exception:
            traceback.print_exc()
            return
        self.webview.evaluateJavaScript_completionHandler_(f"window.setState({state})", None)

    @objc.python_method
    def _view_session(self) -> Session | None:
        return self.sessions.get(self.view_id) or self.sessions.get(self.active_id)

    @objc.python_method
    def _state(self) -> dict:
        now = datetime.now().astimezone()
        state = {
            "calendar": {"status": self.watcher.status, "text": self.watcher.status_text()},
            "model": f"{LLM_MODEL} ({LLM_REASONING_EFFORT} reasoning)",
            "today": now.strftime("%A, %d %B"),
            "locked": bool(self.active_id and self.view_id in (None, self.active_id)),
            "mic_status": self.mic_status,
        }

        if self.prompt:
            m = self.prompt
            mins = round((m["start"] - now).total_seconds() / 60)
            label = ("Starts in 1 min" if mins == 1 else f"Starts in {mins} min") if mins > 0 else \
                ("Starting now" if mins == 0 else f"Started {-mins} min ago")
            state["view"] = "prompt"
            state["prompt"] = {
                "meeting": {
                    "key": m["key"], "title": m["title"], "attendees": m["attendees"],
                    "meet_link": m.get("meet_link", ""), "starts_label": label,
                    "time_range": f"{m['start'].strftime('%H:%M')} – {m['end'].strftime('%H:%M')}",
                },
                "dest_type": "local", "dest_url": "",
            }
            return state

        if self.new_recording and not self.active_id:
            state["view"] = "new"
            state["new"] = self.new_recording
            return state

        s = self._view_session()
        if s:
            state["view"] = {
                "recording": "recording", "awaiting_notes": "notes", "transcribing": "notes",
                "generating": "working", "done": "result", "error": "error",
            }.get(s.status, "home")
            state["session"] = self._session_state(s)
            if self.up_next and s.id == self.active_id:
                state["up_next"] = {"title": self.up_next["title"],
                                    "time": self.up_next["start"].strftime("%H:%M")}
            return state

        state["view"] = "home"
        state["upcoming"] = [{
            "key": m["key"], "title": m["title"], "attendees": m["attendees"],
            "time": m["start"].strftime("%H:%M"),
            "day": _day_label(m["start"], now),
            "now": m["start"] - timedelta(minutes=15) <= now < m["end"],
        } for m in self.watcher.upcoming()[:10]]
        state["sync_interval"] = self.watcher.SYNC_INTERVAL
        state["pending"] = [{
            "id": p.id, "title": p.title,
            "time": datetime.fromisoformat(p.started_at).strftime("%a %H:%M"),
            "status_text": {"awaiting_notes": "Add your notes to finish",
                            "generating": "Writing summary…"}.get(p.status, p.error or "Needs attention"),
        } for p in self.sessions.values() if p.status in ("awaiting_notes", "generating", "error")]
        return state

    @objc.python_method
    def _session_state(self, s: Session) -> dict:
        started = datetime.fromisoformat(s.started_at)
        ended = datetime.fromisoformat(s.ended_at) if s.ended_at else datetime.now()
        dest_label = ""
        if s.dest_type == "link" and s.dest_url:
            found = destinations.detect(s.dest_url)
            dest_label = destinations.label(found[0]) if found else "link"
        elif s.dest_type == "local":
            dest_label = "copy / save file"
        tx = {"state": s.tx_state, "text": s.tx_text or "Transcribing…"}
        if s.tx_state == "error":
            tx["text"] = s.transcript_error
        return {
            "id": s.id, "title": s.title, "attendees": s.attendees, "status": s.status,
            "started_epoch": started.timestamp(), "notepad": s.notepad,
            "dest_type": s.dest_type, "dest_url": s.dest_url, "dest_label": dest_label,
            "duration_label": _fmt_dur((ended - started).total_seconds()),
            "transcription": tx, "steps": s.steps, "html": s.html,
            "outputs": s.outputs, "saved_paths": s.saved_paths, "error": s.error,
            "end_prompt": s.end_prompted and s.id == self.active_id,
            "auto_stop_label": s.auto_stop_at.strftime("at %H:%M") if s.auto_stop_at and s.end_prompted else "",
            "max_warning": s.max_warned and s.id == self.active_id,
            "max_stop_label": s.max_at.strftime("%H:%M") if s.max_at else "",
            "max_minutes": MAX_RECORDING_MINUTES,
            "audio_blocked": s.audio_blocked and s.id == self.active_id,
        }

    # ───────────────────────────────────────────────────────
    # Bridge messages (main thread)
    # ───────────────────────────────────────────────────────

    @objc.python_method
    def handle_message(self, msg: dict):
        action = msg.get("action")
        sid = msg.get("session_id")
        s = self.sessions.get(sid) if sid else None

        if action == "ready":
            self._ui_ready = True
            self._push()
        elif action == "close_window":
            self.windowShouldClose_(None)
        elif action == "home":
            self.view_id = None
            if self.active_id:  # home while recording = show the recording
                self.view_id = self.active_id
            self._push()
        elif action == "start_prompted" and self.prompt:
            m, self.prompt = self.prompt, None
            dest_type = msg.get("dest_type") or "local"
            dest_url = (msg.get("dest_url") or "").strip()
            if dest_type == "link" and dest_url and not destinations.detect(dest_url):
                self._toast("That link isn't a Notion page or Google Doc. You can add it after the call.")
                dest_url = ""
            self._start_session(meeting=m, dest_type=dest_type, dest_url=dest_url)
            if msg.get("join") and m.get("meet_link"):
                subprocess.run(["open", m["meet_link"]])
        elif action == "skip_prompt":
            self.prompt = None
            self._push()
            self.panel.orderOut_(None)
        elif action == "start_for_meeting":
            m = next((x for x in self.watcher.upcoming() if x["key"] == msg.get("key")), None)
            if m:
                self.watcher.mark_prompted(m)
            self._start_session(meeting=m)
        elif action == "start_manual":
            m = self.watcher.current_or_next()
            self.new_recording = {"title": m["title"] if m else "", "meeting_key": m["key"] if m else ""}
            self._push()
        elif action == "cancel_new":
            self.new_recording = None
            self._push()
        elif action == "start_new":
            form, self.new_recording = self.new_recording or {}, None
            m = next((x for x in self.watcher.upcoming() if x["key"] == form.get("meeting_key")), None)
            title = (msg.get("title") or "").strip()
            if m and title and title != m["title"]:
                m = dict(m, title=title)
            dest_url = (msg.get("dest_url") or "").strip()
            dest_type = "link" if dest_url and destinations.detect(dest_url) else "local"
            if m:
                self.watcher.mark_prompted(m)
            self._start_session(meeting=m, title=title or None, dest_type=dest_type,
                                dest_url=dest_url if dest_type == "link" else "")
        elif action == "rename":
            a = self._view_session()
            title = (msg.get("title") or "").strip()
            if a and title:
                a.title = title
                a.save()
        elif action == "extend_max":
            a = self.sessions.get(self.active_id)
            if a and a.max_at:
                a.max_at += timedelta(minutes=EXTEND_MINUTES)
                a.max_warned = False
                self._toast(f"Recording until {a.max_at.strftime('%H:%M')}")
                self._push()
        elif action == "fix_mic":
            subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"])
        elif action == "recheck_mic":
            self.mic_status = "unknown"
            self._push()
            threading.Thread(target=self._probe_mic, daemon=True).start()
        elif action == "sync_now":
            self.watcher.wake()
            self._toast("Syncing calendar…")
        elif action == "stop":
            self._stop_active()
        elif action == "keep_going":
            a = self.sessions.get(self.active_id)
            if a:
                a.end_prompted = False
                a.auto_stop_at = datetime.now() + timedelta(minutes=KEEP_GOING_MINUTES)
                self._push()
        elif action == "switch_to_next" and self.up_next:
            nxt, self.up_next = self.up_next, None
            self._stop_active(then=lambda: self._start_session(meeting=nxt))
        elif action == "dismiss_next":
            self.up_next = None
            self._push()
        elif action == "notepad":
            a = self.sessions.get(self.active_id) or self._view_session()
            if a:
                a.notepad = msg.get("text", "")
                a.save()
        elif action == "open_session" and s:
            self.view_id = s.id
            self._push()
        elif action == "discard_session" and s:
            if s.id != self.active_id:
                s.delete()
                self.sessions.pop(s.id, None)
                if self.view_id == s.id:
                    self.view_id = None
            self._push()
        elif action == "generate" and s:
            self._generate(s, msg.get("mode", "bullets"), msg.get("notes", ""), msg.get("url", ""))
        elif action in ("retry", "regenerate") and s:
            s.status, s.error = "awaiting_notes", ""
            s.save()
            if not s.transcript and s.audio_path and s.tx_state not in ("running", "saving"):
                self._start_transcription(s)
            self._push()
        elif action == "retry_write" and s:
            self._retry_write(s)
        elif action == "copy" and s:
            self._copy(s)
        elif action == "save" and s:
            self._save_file(s, msg.get("fmt", "md"))
        elif action == "reveal":
            subprocess.run(["open", "-R", msg.get("path", "")])
        elif action == "open_url":
            url = msg.get("url", "")
            if url.startswith("https://"):
                subprocess.run(["open", url])
        elif action == "connect_calendar":
            self._connect_calendar()

    # ───────────────────────────────────────────────────────
    # Calendar events
    # ───────────────────────────────────────────────────────

    @objc.python_method
    def _meeting_soon(self, meeting: dict):
        if self.active_id:
            active = self.sessions.get(self.active_id)
            if active and (active.meeting or {}).get("key") == meeting["key"]:
                return
            self.up_next = meeting
            self._show_panel(alert=True)
            _notify("Up next", f"{meeting['title']} starts at {meeting['start'].strftime('%H:%M')}")
            return
        self.prompt = meeting
        self.new_recording = None
        self._show_panel(activate=True, alert=True)
        _notify("Meeting in 1 min", f"{meeting['title']}. Record it and choose where the summary goes.")

    @objc.python_method
    def _connect_calendar(self):
        self._toast("Opening Google sign-in in your browser…")

        def work():
            try:
                google_auth.connect_calendar_interactive()
                self.watcher.wake()
                self._on_main(lambda: self._toast("Calendar connected"))
            except Exception as e:
                msg = str(e)
                self._on_main(lambda: self._toast(f"Calendar sign-in failed: {msg[:120]}"))
        threading.Thread(target=work, daemon=True).start()

    # ───────────────────────────────────────────────────────
    # Recording
    # ───────────────────────────────────────────────────────

    @objc.python_method
    def _register_global_hotkey(self):
        R_KEY_CODE = 15
        REQUIRED = AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagShift

        def matches(event):
            flags = event.modifierFlags() & AppKit.NSEventModifierFlagDeviceIndependentFlagsMask
            return event.keyCode() == R_KEY_CODE and flags == REQUIRED

        def global_handler(event):
            if matches(event):
                self._on_main(self._toggle_recording)

        EDIT_KEYS = {"v": "paste:", "c": "copy:", "x": "cut:", "a": "selectAll:", "z": "undo:"}

        def local_handler(event):
            if matches(event):
                self._toggle_recording()
                return None
            flags = event.modifierFlags() & AppKit.NSEventModifierFlagDeviceIndependentFlagsMask
            if flags in (AppKit.NSEventModifierFlagCommand,
                         AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagShift) \
                    and event.window() is self.panel:
                key = (event.charactersIgnoringModifiers() or "").lower()
                if flags & AppKit.NSEventModifierFlagShift and key != "z":
                    return event  # ⌘⇧X (strikethrough) etc. belong to the editor
                sel = EDIT_KEYS.get(key)
                if sel == "undo:" and flags & AppKit.NSEventModifierFlagShift:
                    sel = "redo:"
                if sel and AppKit.NSApp.sendAction_to_from_(sel, None, self):
                    return None
            return event

        AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(AppKit.NSEventMaskKeyDown, global_handler)
        AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(AppKit.NSEventMaskKeyDown, local_handler)

    @objc.python_method
    def _toggle_recording(self):
        if self._stopping:
            return
        if self.recorder.is_recording:
            self._stop_active()
        elif self.prompt:
            m, self.prompt = self.prompt, None
            self._start_session(meeting=m, dest_type="local")
        else:
            self._start_session(meeting=self.watcher.current_or_next())

    @objc.python_method
    def _start_session(self, meeting: dict | None = None, title: str | None = None,
                       dest_type: str = "", dest_url: str = "") -> bool:
        if self.recorder.is_recording or self._stopping or self._selftest_running:
            self._show_panel(activate=True)
            return False
        title = title or (meeting or {}).get("title") or f"Meeting {datetime.now().strftime('%d %b %H:%M')}"
        try:
            self.recorder.start()
        except RuntimeError as e:
            self._alert("Recording Error", str(e))
            return False

        s = Session(title=title, meeting=meeting, dest_type=dest_type, dest_url=dest_url)
        if meeting and meeting.get("end"):
            # Wrap-up prompt fires at the scheduled end (local naive time)
            s.auto_stop_at = meeting["end"].astimezone().replace(tzinfo=None)
        s.max_at = datetime.now() + timedelta(minutes=MAX_RECORDING_MINUTES)
        self.sessions[s.id] = s
        self.active_id = self.view_id = s.id
        self.new_recording = None
        if self.prompt and meeting and self.prompt.get("key") == meeting.get("key"):
            self.prompt = None
        s.save()
        print(f"● Recording started: {title}", flush=True)
        self._show_panel()
        return True

    @objc.python_method
    def _stop_active(self, then=None):
        s = self.sessions.get(self.active_id)
        if not s or not self.recorder.is_recording or self._stopping:
            return
        self._stopping = True
        s.status = "awaiting_notes"
        s.ended_at = datetime.now().isoformat()
        s.end_prompted = False
        s.tx_state, s.tx_text = "saving", "Saving audio…"
        s.tx_done.clear()
        s.save()
        self.active_id = None
        self.view_id = s.id
        self._status_item.button().setTitle_("🎙️")
        self._show_panel(activate=then is None, alert=False)

        def work():
            path = None
            try:
                path = self.recorder.stop(s.title)
            except Exception as e:
                traceback.print_exc()
                s.transcript_error = f"Couldn't save the audio: {e}"

            def done():
                self._stopping = False
                if path:
                    s.audio_path = str(path)
                    s.save()
                    self._start_transcription(s)
                else:
                    s.tx_state = "error"
                    s.transcript_error = s.transcript_error or "No audio was captured."
                    s.tx_done.set()
                    s.save()
                self._push()
                if then:
                    then()
            self._on_main(done)
        threading.Thread(target=work, daemon=True).start()

    # ───────────────────────────────────────────────────────
    # Timer tick (main thread, 1 Hz)
    # ───────────────────────────────────────────────────────

    def tick_(self, timer):
        self._tick_count += 1
        self._style_pill()
        if not self.pill.isVisible():
            self.pill.orderFrontRegardless()
        if SHOW_REQUEST_PATH.exists():  # "open Meeting Recorder" while already running
            SHOW_REQUEST_PATH.unlink(missing_ok=True)
            self._show_panel(activate=True)
        s = self.sessions.get(self.active_id)
        if s and self.recorder.is_recording:
            secs = int(self.recorder.elapsed_seconds)
            self._status_item.button().setTitle_(f"🔴 {secs // 60:02d}:{secs % 60:02d}")
            now = datetime.now()
            if not s.end_prompted and s.auto_stop_at and now >= s.auto_stop_at:
                # Scheduled end (or the end of a +10 min extension) reached: ask to wrap up.
                s.end_prompted = True
                s.auto_stop_at = now + timedelta(minutes=END_GRACE_MINUTES)
                self._show_panel(alert=True)
            elif s.end_prompted and s.auto_stop_at and now >= s.auto_stop_at:
                print("Auto-stopping: no response to wrap-up prompt", flush=True)
                self._stop_active()
                return
            if secs % 5 == 0:  # follow headphone changes mid-call
                threading.Thread(target=self.recorder.route.ensure, daemon=True).start()
            if not s.audio_checked and secs >= 15:
                s.audio_checked = True
                if self.recorder.mic_peak == 0 and self.recorder.system_peak == 0:
                    s.audio_blocked = True
                    self.mic_status = "blocked"
                    print("⚠ 15 s of pure silence: audio input is blocked", flush=True)
                    self._show_panel(activate=True, alert=True)
                    _notify("Recording is silent", "macOS is blocking the microphone. Open Meeting Recorder to fix it.")
                else:
                    self.mic_status = "ok"
            if s.max_at and not s.max_warned and s.max_at - timedelta(minutes=MAX_WARNING_MINUTES) <= now < s.max_at:
                s.max_warned = True
                self._show_panel(activate=True, alert=True)
                _notify("Recording stops in 5 min",
                        f"{s.title} hits the {MAX_RECORDING_MINUTES}-min limit at {s.max_at.strftime('%H:%M')}. "
                        f"Open the recorder to keep going.")
            if s.max_at and now >= s.max_at:
                print("Auto-stopping: max duration reached", flush=True)
                _notify("Recording stopped", f"{s.title} reached the time limit and was saved. Add your notes to finish.")
                self._stop_active()
                return

        # Drop a stale prompt (meeting over, or ignored for 10 min after start)
        if self.prompt:
            now_tz = datetime.now().astimezone()
            if now_tz >= self.prompt["end"] or now_tz >= self.prompt["start"] + timedelta(minutes=10):
                self.prompt = None
                self._push()
        if self.up_next and datetime.now().astimezone() >= self.up_next["end"]:
            self.up_next = None

        if self._tick_count % 15 == 0 and self.panel.isVisible():
            self._push()

    # ───────────────────────────────────────────────────────
    # Transcription (background)
    # ───────────────────────────────────────────────────────

    @objc.python_method
    def _start_transcription(self, s: Session):
        if s.tx_state == "running":
            return
        s.tx_state, s.tx_text = "running", "Transcribing…"
        s.tx_done.clear()
        self._push()

        def progress(msg):
            s.tx_text = msg

            def upd():
                self._push()
            self._on_main(upd)

        def work():
            try:
                transcript = transcribe_audio(s.audio_path, on_progress=progress)
                s.transcript = transcript
                if transcript.get("text", "").strip():
                    s.transcript_error, s.tx_state = "", "done"
                    self._write_transcript_file(s)
                else:
                    s.tx_state = "error"
                    s.transcript_error = ("No speech found in the recording. If you did talk, the microphone "
                                          "was probably blocked; run Self-Test from the menu bar.")
            except Exception as e:
                traceback.print_exc()
                s.transcript_error = f"Transcription failed: {str(e)[:160]}"
                s.tx_state = "error"
            s.save()
            s.tx_done.set()
            self._on_main(self._push)
        threading.Thread(target=work, daemon=True).start()

    @objc.python_method
    def _write_transcript_file(self, s: Session):
        started = datetime.fromisoformat(s.started_at)
        base = f"{started.strftime('%Y-%m-%d_%H%M')}_{_slug(s.title)}"
        try:
            path = _write_output(TRANSCRIPTS_DIR, f"{base}_transcript.md",
                                 f"# Transcript: {s.title}\n*Recorded: {started.strftime('%Y-%m-%d %H:%M')}*\n\n"
                                 f"{format_transcript_as_markdown(s.transcript)}\n")
            print(f"📝 Transcript saved: {path}", flush=True)
        except Exception as e:  # never let a file problem mark a good transcript as failed
            print(f"⚠ Transcript file not written: {e}", flush=True)

    # ───────────────────────────────────────────────────────
    # Summary generation (background)
    # ───────────────────────────────────────────────────────

    @objc.python_method
    def _meta(self, s: Session) -> dict:
        when = datetime.fromisoformat(s.started_at)
        return {"date": when.strftime("%a %d %b %Y, %H:%M"), "attendees": s.attendees}

    @objc.python_method
    def _generate(self, s: Session, mode: str, notes: str, url: str):
        if s.status == "generating":
            return
        url = url.strip()
        if mode == "link":
            if not destinations.detect(url):
                s.error = "That link isn't a Notion page or a Google Doc."
                self._push()
                return
            s.dest_type, s.dest_url = "link", url
        else:
            s.dest_type, s.dest_url = "local", ""
            if mode == "bullets":
                s.notepad = notes
        kind = destinations.detect(url)[0] if mode == "link" else ""
        where = destinations.label(kind)

        steps = [("tx", "Transcript")]
        if mode == "link":
            steps.append(("read", f"Reading your notes from the {where}"))
        steps.append(("llm", f"Merging your notes with the transcript ({LLM_MODEL})"))
        if mode == "link":
            steps.append(("write", f"Adding the summary to your {where}"))
        s.steps = [{"id": k, "label": lbl, "state": "todo"} for k, lbl in steps]
        s.status, s.error, s.outputs = "generating", "", []
        s.save()
        self._push()

        def step(key, state="now"):
            for st in s.steps:
                if st["id"] == key:
                    st["state"] = state
            self._on_main(self._push)

        def work():
            try:
                # 1. Transcript (wait for the background job, retry once if it failed)
                step("tx")
                if s.tx_state in ("running", "saving"):
                    s.tx_done.wait()
                if not s.transcript and s.audio_path and Path(s.audio_path).exists():
                    try:
                        s.transcript = transcribe_audio(s.audio_path)
                        s.tx_state, s.transcript_error = "done", ""
                        self._write_transcript_file(s)
                    except Exception as e:
                        s.transcript_error = f"Transcription failed: {str(e)[:160]}"
                transcript_text = (s.transcript or {}).get("text", "")
                step("tx", "done")

                # 2. My notes
                user_notes = s.notepad if mode != "none" else ""
                if mode == "link":
                    step("read")
                    _, page_title, page_text = destinations.read_notes(url)
                    extra = s.notepad.strip()
                    user_notes = page_text + (f"\n\n(Notes typed in the recorder)\n{extra}" if extra else "")
                    step("read", "done")

                if not transcript_text.strip() and not user_notes.strip():
                    raise RuntimeError(s.transcript_error or "No transcript and no notes, so there's nothing to summarize.")

                # 3. Synthesis
                step("llm")
                summary = synthesize(s.title, user_notes, transcript_text, s.attendees, self._meta(s)["date"])
                meta = self._meta(s)
                s.summary = summary.model_dump()
                s.markdown = to_markdown(summary, meta)
                s.text = to_text(summary, meta)
                s.html = to_html(summary, meta)
                step("llm", "done")
                s.status = "done"

                # 4. Write to the page
                if mode == "link":
                    step("write")
                    try:
                        k, link = destinations.write_summary(url, summary, meta)
                        s.outputs = [{"kind": k, "label": destinations.label(k), "url": link}]
                        step("write", "done")
                    except Exception as e:
                        s.error = f"Summary is ready, but writing to the {where} failed: {e}"

                self._archive_local(s)
            except destinations.DestinationError as e:
                s.status, s.error = "awaiting_notes", str(e)
            except Exception as e:
                traceback.print_exc()
                s.status, s.error = "awaiting_notes", f"Couldn't generate the summary: {str(e)[:200]}"
            s.save()

            def finish():
                self.view_id = s.id
                self._show_panel(alert=s.status == "done")
                if s.status == "done":
                    where = f" and added to your {s.outputs[0]['label']}" if s.outputs else ""
                    _notify("Summary ready", f"{s.title}{where}.")
            self._on_main(finish)
        threading.Thread(target=work, daemon=True).start()

    @objc.python_method
    def _retry_write(self, s: Session):
        if not s.summary or not s.dest_url:
            return
        s.error = ""
        self._toast("Retrying…")

        def work():
            try:
                k, link = destinations.write_summary(
                    s.dest_url, MeetingSummary.model_validate(s.summary), self._meta(s))
                s.outputs = [{"kind": k, "label": destinations.label(k), "url": link}]
            except Exception as e:
                s.error = f"Writing to the page failed again: {e}"
            s.save()
            self._on_main(self._push)
        threading.Thread(target=work, daemon=True).start()

    @objc.python_method
    def _archive_local(self, s: Session):
        """Keep a record in data/meetings for the dashboard (best effort)."""
        try:
            started = datetime.fromisoformat(s.started_at)
            ended = datetime.fromisoformat(s.ended_at) if s.ended_at else started
            save_meeting(
                title=s.title, date=started, duration_seconds=(ended - started).total_seconds(),
                audio_path=s.audio_path, transcript=s.transcript or {},
                enriched_notes=s.markdown, summary=(s.summary or {}).get("tldr", ""),
            )
        except Exception as e:
            print(f"⚠ local archive failed: {e}", flush=True)

    # ───────────────────────────────────────────────────────
    # Copy / Save
    # ───────────────────────────────────────────────────────

    @objc.python_method
    def _copy(self, s: Session):
        pb = AppKit.NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(s.markdown, AppKit.NSPasteboardTypeString)
        pb.setString_forType_(
            f'<meta charset="utf-8"><div style="font-family:-apple-system,Helvetica,sans-serif">{s.html}</div>',
            AppKit.NSPasteboardTypeHTML)
        self._toast("Copied. Paste anywhere")

    @objc.python_method
    def _save_file(self, s: Session, fmt: str):
        started = datetime.fromisoformat(s.started_at)
        ext = "txt" if fmt == "txt" else "md"
        path = _write_output(SUMMARIES_DIR, f"{started.strftime('%Y-%m-%d_%H%M')}_{_slug(s.title)}_summary.{ext}",
                             s.text if ext == "txt" else s.markdown)
        if str(path) not in s.saved_paths:
            s.saved_paths.append(str(path))
        s.save()
        self._push()
        self._toast(f"Saved {path.name}")

    # ───────────────────────────────────────────────────────
    # Helpers
    # ───────────────────────────────────────────────────────

    @objc.python_method
    def _alert(self, title, message):
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("OK")
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        alert.runModal()


def main():
    if not _acquire_single_instance_lock():
        return
    if not OPENAI_API_KEY:
        print("❌ OPENAI_API_KEY not found (.env)")
        return
    if not NOTION_TOKEN:
        print("⚠ NOTION_TOKEN not found: Notion links won't work")

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()


if __name__ == "__main__":
    main()
