from __future__ import annotations
"""
Meeting Recorder — macOS Native Floating Window App.

One-click recording, transcription, and Notion integration.
Uses native AppKit for a compact floating window.
"""
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import AppKit
import objc
from Foundation import NSObject, NSTimer, NSRunLoop, NSDefaultRunLoopMode

# Ensure our project dir is on the path
PROJECT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_DIR))

from config import OPENAI_API_KEY, NOTION_TOKEN, TRANSCRIPTS_DIR, SUMMARIES_DIR
from recorder import MeetingRecorder
from transcriber import transcribe_audio, format_transcript_as_markdown
from enricher import enrich_notes, create_notes_from_transcript, generate_summary
from notion_bridge import (
    search_meeting_page,
    get_page_content_as_markdown,
    create_meeting_page,
    update_page_content,
    update_summary,
    _get_page_title,
    sync_contact_emails,
)
from meeting_watcher import MeetingWatcher
from local_store import save_meeting
from agent_bridge import trigger_agent_on_page


# ─── Colors ───────────────────────────────────────────────────
def rgba(r, g, b, a=1.0):
    return AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(r/255, g/255, b/255, a)

BG_DARK    = rgba(26, 26, 46)
BG_CARD    = rgba(22, 33, 62)
BG_HOVER   = rgba(31, 48, 86)
ACCENT     = rgba(233, 69, 96)
ACCENT_DIM = rgba(233, 69, 96, 0.8)
SUCCESS    = rgba(46, 204, 113)
WARNING    = rgba(243, 156, 18)
TEXT_WHITE = rgba(234, 234, 234)
TEXT_SEC   = rgba(136, 146, 164)
TEXT_DIM   = rgba(90, 100, 120)
WHITE      = AppKit.NSColor.whiteColor()
CLEAR      = AppKit.NSColor.clearColor()
STOP_RED   = rgba(192, 57, 43)
STOP_RED_H = rgba(231, 76, 60)


# ─── Rounded Button ──────────────────────────────────────────
class RoundedButton(AppKit.NSButton):
    """Custom button with rounded corners and hover effects."""

    def initWithFrame_(self, frame):
        self = objc.super(RoundedButton, self).initWithFrame_(frame)
        if self is None:
            return None
        self.setBordered_(False)
        self.setWantsLayer_(True)
        self.layer().setCornerRadius_(10)
        self._bg_color = ACCENT
        self._hover_color = rgba(255, 107, 129)
        self.layer().setBackgroundColor_(self._bg_color.CGColor())

        # Text styling
        self.setFont_(AppKit.NSFont.systemFontOfSize_weight_(14, AppKit.NSFontWeightSemibold))
        self.setAlignment_(AppKit.NSTextAlignmentCenter)
        self.setContentTintColor_(WHITE)

        # Track mouse
        area = AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            AppKit.NSTrackingMouseEnteredAndExited | AppKit.NSTrackingActiveAlways | AppKit.NSTrackingInVisibleRect,
            self, None,
        )
        self.addTrackingArea_(area)
        return self

    def mouseEntered_(self, event):
        self.layer().setBackgroundColor_(self._hover_color.CGColor())

    def mouseExited_(self, event):
        self.layer().setBackgroundColor_(self._bg_color.CGColor())

    def setColors_hover_(self, bg, hover):
        self._bg_color = bg
        self._hover_color = hover
        self.layer().setBackgroundColor_(bg.CGColor())


# ─── Small Button ────────────────────────────────────────────
class SmallButton(AppKit.NSButton):
    """Smaller secondary button."""

    def initWithFrame_(self, frame):
        self = objc.super(SmallButton, self).initWithFrame_(frame)
        if self is None:
            return None
        self.setBordered_(False)
        self.setWantsLayer_(True)
        self.layer().setCornerRadius_(8)
        self.layer().setBackgroundColor_(BG_CARD.CGColor())
        self.setFont_(AppKit.NSFont.systemFontOfSize_weight_(11, AppKit.NSFontWeightMedium))
        self.setContentTintColor_(TEXT_SEC)

        area = AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            AppKit.NSTrackingMouseEnteredAndExited | AppKit.NSTrackingActiveAlways | AppKit.NSTrackingInVisibleRect,
            self, None,
        )
        self.addTrackingArea_(area)
        return self

    def mouseEntered_(self, event):
        self.layer().setBackgroundColor_(BG_HOVER.CGColor())
        self.setContentTintColor_(TEXT_WHITE)

    def mouseExited_(self, event):
        self.layer().setBackgroundColor_(BG_CARD.CGColor())
        self.setContentTintColor_(TEXT_SEC)


# ─── Main App Delegate ───────────────────────────────────────
class AppDelegate(NSObject):

    def init(self):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None

        self.recorder = MeetingRecorder()
        self._meeting_title = ""
        self._attendee_emails = []
        self._processing = False
        self._timer = None
        self._blink_on = True

        self.watcher = MeetingWatcher(on_meeting_detected=self._on_meeting_detected)
        self._auto_detect = True

        return self

    def applicationDidFinishLaunching_(self, notification):
        self._build_status_item()
        self._build_window()
        self._init_watcher()
        self._register_global_hotkey()
        self._dashboard_process = None

    # ───────────────────────────────────────────────────────
    # Menu bar status item
    # ───────────────────────────────────────────────────────

    @objc.python_method
    def _build_status_item(self):
        """Create the menu bar icon with a dropdown menu."""
        status_bar = AppKit.NSStatusBar.systemStatusBar()
        self._status_item = status_bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self._status_item.button().setTitle_("\U0001f399\ufe0f")  # mic emoji

        # Build the menu (clicking the status item opens this menu)
        menu = AppKit.NSMenu.alloc().init()

        toggle_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Show/Hide Window", objc.selector(self.onToggleWindow_, signature=b'v@:@'), "")
        toggle_item.setTarget_(self)
        menu.addItem_(toggle_item)

        start_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Start Recording", objc.selector(self.onStartRecording_, signature=b'v@:@'), "")
        start_item.setTarget_(self)
        menu.addItem_(start_item)

        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        dashboard_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Open Data Folder", objc.selector(self.onOpenDataFolder_, signature=b'v@:@'), "")
        dashboard_item.setTarget_(self)
        menu.addItem_(dashboard_item)

        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", objc.selector(self.onQuit_, signature=b'v@:@'), "q")
        quit_item.setTarget_(self)
        menu.addItem_(quit_item)

        self._status_item.setMenu_(menu)

    @objc.python_method
    def _set_menu_bar_recording(self, recording: bool):
        """Update the menu bar icon to indicate recording state."""
        def _do():
            if recording:
                self._status_item.button().setTitle_("\U0001f534")  # red circle
            else:
                self._status_item.button().setTitle_("\U0001f399\ufe0f")  # mic
        self._on_main(_do)

    def onToggleWindow_(self, sender):
        """Toggle the floating window visibility."""
        if self.window.isVisible():
            self.window.orderOut_(None)
        else:
            button = self._status_item.button()
            if button:
                # Re-position window just under the menu bar item
                btn_frame = button.window().frame()
                win_frame = self.window.frame()
                w = win_frame.size.width
                h = win_frame.size.height
                x = btn_frame.origin.x - w / 2 + btn_frame.size.width / 2
                y = btn_frame.origin.y - h - 10
                
                screen_width = AppKit.NSScreen.mainScreen().frame().size.width
                if x + w > screen_width:
                    x = screen_width - w - 10
                    
                self.window.setFrameOrigin_(AppKit.NSMakePoint(x, y))

            self.window.makeKeyAndOrderFront_(None)
            AppKit.NSApp.activateIgnoringOtherApps_(True)

    # ───────────────────────────────────────────────────────
    # Global hotkey (Cmd+Shift+R)
    # ───────────────────────────────────────────────────────

    @objc.python_method
    def _register_global_hotkey(self):
        """Register Cmd+Shift+R as a global hotkey to toggle recording."""
        # Key code 15 = 'R' on US keyboard layout
        R_KEY_CODE = 15
        REQUIRED_FLAGS = AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagShift

        def _handle_hotkey(event):
            if event.keyCode() != R_KEY_CODE:
                return
            flags = event.modifierFlags() & AppKit.NSEventModifierFlagDeviceIndependentFlagsMask
            if flags != REQUIRED_FLAGS:
                return
            self._on_main(lambda: self._toggle_recording_quick())

        # Global monitor: catches events when app is NOT focused
        AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskKeyDown, _handle_hotkey
        )

        # Local monitor: catches events when app IS focused
        def _handle_local(event):
            if event.keyCode() != R_KEY_CODE:
                return event
            flags = event.modifierFlags() & AppKit.NSEventModifierFlagDeviceIndependentFlagsMask
            if flags != REQUIRED_FLAGS:
                return event
            self._toggle_recording_quick()
            return None  # Consume the event

        AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskKeyDown, _handle_local
        )

    @objc.python_method
    def _toggle_recording_quick(self):
        """Toggle recording instantly — no title dialog. Auto-fills from calendar."""
        if self._processing:
            return

        if self.recorder.is_recording:
            # Stop recording
            self.onStopRecording_(None)
            return

        # Auto-fill title from the nearest calendar event
        title = self._get_current_meeting_title()
        self._meeting_title = title

        try:
            self.recorder.start()
        except RuntimeError as e:
            self._show_alert("Recording Error", str(e))
            return

        self._set_recording_ui()
        self._set_menu_bar_recording(True)
        display = title or "Meeting"
        self._set_status("Recording...", ACCENT)
        self._set_subtitle(f"Recording: {display}")
        self._start_timer()

        # Show the window briefly so user sees it started
        self.onToggleWindow_(None)

    @objc.python_method
    def _get_current_meeting_title(self) -> str:
        """Get the title of the current or next upcoming meeting from the calendar."""
        try:
            meetings = self.watcher.get_upcoming_meetings(within_minutes=30)
            if meetings:
                return meetings[0]["title"]
        except Exception:
            pass
        return f"Meeting {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    # ───────────────────────────────────────────────────────
    # Window construction
    # ───────────────────────────────────────────────────────

    def _build_window(self):
        W, H = 320, 400
        screen_frame = AppKit.NSScreen.mainScreen().frame()
        # Default fallback coords
        x = screen_frame.size.width - W - 20
        y = screen_frame.size.height - H - 40
        frame = ((x, y), (W, H))

        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            AppKit.NSWindowStyleMaskTitled |
            AppKit.NSWindowStyleMaskClosable |
            AppKit.NSWindowStyleMaskFullSizeContentView,
            AppKit.NSBackingStoreBuffered,
            False,
        )

        self.window.setTitle_("Meeting Recorder")
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        self.window.setLevel_(AppKit.NSNormalWindowLevel)
        self.window.setBackgroundColor_(BG_DARK)
        self.window.setMovableByWindowBackground_(True)
        self.window.setDelegate_(self)

        # Content view
        content = self.window.contentView()
        content.setWantsLayer_(True)
        content.layer().setBackgroundColor_(BG_DARK.CGColor())

        y_pos = H - 50  # Start from top (accounting for titlebar)

        # ─── Title ───
        title = self.makeLabel("🎙️  Meeting Recorder", 16, AppKit.NSFontWeightBold, TEXT_WHITE)
        title.setFrame_(((16, y_pos), (W - 32, 24)))
        content.addSubview_(title)
        y_pos -= 20

        self.subtitle = self.makeLabel("Ready to record", 11, AppKit.NSFontWeightRegular, TEXT_SEC)
        self.subtitle.setFrame_(((16, y_pos), (W - 32, 18)))
        content.addSubview_(self.subtitle)
        y_pos -= 30

        # ─── Timer Card ───
        card = AppKit.NSView.alloc().initWithFrame_(((16, y_pos - 60), (W - 32, 75)))
        card.setWantsLayer_(True)
        card.layer().setBackgroundColor_(BG_CARD.CGColor())
        card.layer().setCornerRadius_(12)
        content.addSubview_(card)

        self.timer_label = self.makeLabel("00:00", 38, AppKit.NSFontWeightBold, TEXT_DIM)
        self.timer_label.setAlignment_(AppKit.NSTextAlignmentCenter)
        self.timer_label.setFont_(AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(38, AppKit.NSFontWeightBold))
        self.timer_label.setFrame_(((0, 8), (W - 32, 50)))
        card.addSubview_(self.timer_label)
        y_pos -= 75

        # ─── Status Row ───
        y_pos -= 8
        self.status_dot = self.makeLabel("●", 9, AppKit.NSFontWeightRegular, TEXT_DIM)
        self.status_dot.setFrame_(((16, y_pos), (14, 16)))
        content.addSubview_(self.status_dot)

        self.status_label = self.makeLabel("Idle", 11, AppKit.NSFontWeightRegular, TEXT_SEC)
        self.status_label.setFrame_(((32, y_pos), (W - 48, 16)))
        content.addSubview_(self.status_label)
        y_pos -= 28

        # ─── Main Button ───
        self.record_btn = RoundedButton.alloc().initWithFrame_(((16, y_pos - 40), (W - 32, 44)))
        self.record_btn.setTitle_("▶  Start Recording")
        self.record_btn.setTarget_(self)
        self.record_btn.setAction_(objc.selector(self.onStartRecording_, signature=b'v@:@'))
        content.addSubview_(self.record_btn)
        y_pos -= 54

        # ─── Secondary Buttons ───
        half_w = (W - 32 - 8) / 2

        self.meetings_btn = SmallButton.alloc().initWithFrame_(((16, y_pos - 30), (half_w, 34)))
        self.meetings_btn.setTitle_("📅 Meetings")
        self.meetings_btn.setTarget_(self)
        self.meetings_btn.setAction_(objc.selector(self.onShowMeetings_, signature=b'v@:@'))
        content.addSubview_(self.meetings_btn)

        self.setup_btn = SmallButton.alloc().initWithFrame_(((16 + half_w + 8, y_pos - 30), (half_w, 34)))
        self.setup_btn.setTitle_("⚙️  Setup")
        self.setup_btn.setTarget_(self)
        self.setup_btn.setAction_(objc.selector(self.onCheckSetup_, signature=b'v@:@'))
        content.addSubview_(self.setup_btn)
        y_pos -= 44

        # ─── Checkboxes ───
        def _set_checkbox_title(btn, text):
            # Change text color using attributed string
            attr_title = AppKit.NSAttributedString.alloc().initWithString_attributes_(
                text,
                {
                    AppKit.NSForegroundColorAttributeName: TEXT_WHITE,
                    AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(10)
                }
            )
            btn.setAttributedTitle_(attr_title)

        # ─── Checkboxes ───
        self.pin_check = AppKit.NSButton.alloc().initWithFrame_(((16, y_pos - 16), (half_w, 20)))
        self.pin_check.setButtonType_(AppKit.NSButtonTypeSwitch)
        _set_checkbox_title(self.pin_check, "Always on top")
        self.pin_check.setState_(AppKit.NSControlStateValueOff)
        self.pin_check.setTarget_(self)
        self.pin_check.setAction_(objc.selector(self.onTogglePin_, signature=b'v@:@'))
        content.addSubview_(self.pin_check)

        self.detect_check = AppKit.NSButton.alloc().initWithFrame_(((16 + half_w + 8, y_pos - 16), (half_w, 20)))
        self.detect_check.setButtonType_(AppKit.NSButtonTypeSwitch)
        _set_checkbox_title(self.detect_check, "Auto-detect")
        self.detect_check.setState_(AppKit.NSControlStateValueOn)
        self.detect_check.setTarget_(self)
        self.detect_check.setAction_(objc.selector(self.onToggleDetect_, signature=b'v@:@'))
        content.addSubview_(self.detect_check)
        y_pos -= 30

        # ─── Footer ───
        hotkey_label = self.makeLabel("⌘⇧R to toggle recording", 9, AppKit.NSFontWeightRegular, TEXT_DIM)
        hotkey_label.setFrame_(((16, 26), (W - 32, 14)))
        content.addSubview_(hotkey_label)

        self.footer_label = self.makeLabel("🔔 Calendar: checking...", 9, AppKit.NSFontWeightRegular, TEXT_DIM)
        self.footer_label.setFrame_(((16, 12), (W - 80, 14)))
        content.addSubview_(self.footer_label)

        quit_btn = SmallButton.alloc().initWithFrame_(((W - 64, 8), (48, 22)))
        quit_btn.setTitle_("Quit")
        quit_btn.setFont_(AppKit.NSFont.systemFontOfSize_(10))
        quit_btn.setTarget_(self)
        quit_btn.setAction_(objc.selector(self.onQuit_, signature=b'v@:@'))
        content.addSubview_(quit_btn)

        # Window starts hidden until someone requests it
        # self.window.makeKeyAndOrderFront_(None)
        # AppKit.NSApp.activateIgnoringOtherApps_(True)

    @objc.python_method
    def makeLabel(self, text, size, weight, color):
        """Create a styled NSTextField (label)."""
        label = AppKit.NSTextField.alloc().init()
        label.setStringValue_(text)
        label.setFont_(AppKit.NSFont.systemFontOfSize_weight_(size, weight))
        label.setTextColor_(color)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        return label

    # ───────────────────────────────────────────────────────
    # Thread-safe UI updates
    # ───────────────────────────────────────────────────────

    @objc.python_method
    def _on_main(self, block):
        """Execute block on main thread."""
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(block)

    @objc.python_method
    def _set_status(self, text, color=None):
        def _do():
            self.status_label.setStringValue_(text)
            if color:
                self.status_dot.setTextColor_(color)
        self._on_main(_do)

    @objc.python_method
    def _set_timer(self, text, color=None):
        def _do():
            self.timer_label.setStringValue_(text)
            if color:
                self.timer_label.setTextColor_(color)
        self._on_main(_do)

    @objc.python_method
    def _set_subtitle(self, text):
        self._on_main(lambda: self.subtitle.setStringValue_(text))

    @objc.python_method
    def _set_footer(self, text):
        self._on_main(lambda: self.footer_label.setStringValue_(text))

    # ───────────────────────────────────────────────────────
    # Recording
    # ───────────────────────────────────────────────────────

    @objc.python_method
    def _ask_title(self):
        """Show input dialog for meeting title."""
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Meeting Recorder")
        alert.setInformativeText_("Enter meeting title (optional — used to match Notion page):")
        alert.addButtonWithTitle_("Start Recording")
        alert.addButtonWithTitle_("Cancel")

        field = AppKit.NSTextField.alloc().initWithFrame_(((0, 0), (300, 24)))
        field.setStringValue_("")
        field.setPlaceholderString_("e.g. Weekly Standup")
        alert.setAccessoryView_(field)
        alert.window().setInitialFirstResponder_(field)

        result = alert.runModal()
        if result == AppKit.NSAlertFirstButtonReturn:
            return field.stringValue()
        return None

    def onStartRecording_(self, sender):
        if self.recorder.is_recording or self._processing:
            return

        title = self._ask_title()
        if title is None:
            return

        self._meeting_title = title.strip()

        try:
            self.recorder.start()
        except RuntimeError as e:
            self._show_alert("Recording Error", str(e))
            return

        # Update UI
        self._set_recording_ui()
        self._set_menu_bar_recording(True)
        display = self._meeting_title or "Meeting"
        self._set_status("Recording...", ACCENT)
        self._set_subtitle(f"Recording: {display}")

        # Start timer
        self._start_timer()

    def onStopRecording_(self, sender):
        if not self.recorder.is_recording:
            return

        self._stop_timer()
        self._set_status("Saving recording...", WARNING)
        self._set_subtitle("Processing...")

        def _saving_ui():
            self.record_btn.setTitle_("⏳ Saving...")
            self.record_btn.setEnabled_(False)
        self._on_main(_saving_ui)

        audio_path = self.recorder.stop(self._meeting_title)

        if not audio_path:
            self._reset_ui()
            self._show_alert("No Audio", "No audio was captured.")
            return

        self._processing = True
        threading.Thread(target=self._process_recording, args=(audio_path,), daemon=True).start()

    @objc.python_method
    def _set_recording_ui(self):
        def _do():
            self.record_btn.setTitle_("⏹  Stop Recording")
            self.record_btn.setAction_(objc.selector(self.onStopRecording_, signature=b'v@:@'))
            self.record_btn.setColors_hover_(STOP_RED, STOP_RED_H)
            
            # Update menu bar item to say Stop Recording
            menu = self._status_item.menu()
            if menu:
                for item in menu.itemArray():
                    if item.title() == "Start Recording":
                        item.setTitle_("Stop Recording")
                        item.setAction_(objc.selector(self.onStopRecording_, signature=b'v@:@'))
                        
        self._on_main(_do)

    @objc.python_method
    def _reset_ui(self):
        def _do():
            self.record_btn.setTitle_("▶  Start Recording")
            self.record_btn.setAction_(objc.selector(self.onStartRecording_, signature=b'v@:@'))
            self.record_btn.setColors_hover_(ACCENT, rgba(255, 107, 129))
            self.record_btn.setEnabled_(True)
            self.timer_label.setStringValue_("00:00")
            self.timer_label.setTextColor_(TEXT_DIM)
            self.status_dot.setTextColor_(TEXT_DIM)
            self.status_label.setStringValue_("Idle")
            self.subtitle.setStringValue_("Ready to record")
            
            # Update menu bar item back to Start Recording
            menu = self._status_item.menu()
            if menu:
                for item in menu.itemArray():
                    if item.title() == "Stop Recording":
                        item.setTitle_("Start Recording")
                        item.setAction_(objc.selector(self.onStartRecording_, signature=b'v@:@'))

        self._on_main(_do)
        self._set_menu_bar_recording(False)
        self._processing = False

    # ───────────────────────────────────────────────────────
    # Timer
    # ───────────────────────────────────────────────────────

    @objc.python_method
    def _start_timer(self):
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, objc.selector(self.timerTick_, signature=b'v@:@'), None, True
        )

    @objc.python_method
    def _stop_timer(self):
        if self._timer:
            self._timer.invalidate()
            self._timer = None

    def timerTick_(self, timer):
        if not self.recorder.is_recording:
            self._stop_timer()
            return

        elapsed = self.recorder.elapsed_str
        self.timer_label.setStringValue_(elapsed)
        self.timer_label.setTextColor_(ACCENT)

        # Blink dot
        self._blink_on = not self._blink_on
        self.status_dot.setTextColor_(ACCENT if self._blink_on else BG_DARK)

    # ───────────────────────────────────────────────────────
    # Processing pipeline
    # ───────────────────────────────────────────────────────

    @objc.python_method
    def _process_recording(self, audio_path):
        try:
            def _processing_ui():
                self.record_btn.setEnabled_(False)
                self.record_btn.setTitle_("⏳ Processing...")
                self.record_btn.setColors_hover_(WARNING, WARNING)
            self._on_main(_processing_ui)

            # Step 1: Transcribe
            self._set_status("Transcribing with GPT-4o...", WARNING)
            self._set_timer("⏳", WARNING)

            transcript = transcribe_audio(
                audio_path,
                on_progress=lambda msg: self._set_status(msg, WARNING),
            )

            transcript_text = transcript.get("text", "")
            duration = transcript.get("duration_seconds", 0)

            if not transcript_text.strip():
                self._set_status("No speech detected", WARNING)
                self._set_timer("--:--", TEXT_DIM)
                self._show_alert("Transcription Complete", "No speech detected in the recording.")
                self._reset_ui()
                return

            # Step 2: Create enriched notes + summary
            title = self._meeting_title or f"Meeting {datetime.now().strftime('%Y-%m-%d %H:%M')}"

            self._set_status("Creating notes...", WARNING)
            enriched_md = create_notes_from_transcript(
                transcript_text=transcript_text,
                meeting_title=title,
            )

            self._set_status("Generating briefing...", WARNING)
            summary = generate_summary(enriched_md, title)

            # Step 3: Always save locally
            self._set_status("Saving locally...", WARNING)
            meeting = save_meeting(
                title=title,
                date=datetime.now(),
                duration_seconds=duration,
                audio_path=str(audio_path),
                transcript=transcript,
                enriched_notes=enriched_md,
                summary=summary,
            )
            dur_str = f"{int(duration // 60)}m"

            # Step 4: Ask user whether to push to Notion
            push_to_notion = self._ask_notion_push(title, dur_str)

            if push_to_notion:
                self._push_to_notion(meeting, title, enriched_md, summary, transcript_text, dur_str, self._attendee_emails)
            else:
                # Save transcript and summary as readable files
                self._save_local_files(title, transcript, enriched_md, summary)
                
                self._set_status(f"✅ Saved locally: {title[:25]}", SUCCESS)
                self._set_timer("✅", SUCCESS)
                self._set_subtitle(f"Done — {dur_str} saved locally")
                self._show_alert("✅ Meeting Saved", f"{title}\nSaved locally ({dur_str}).\n\n📁 Transcript → data/transcripts/\n📁 Summary → data/summaries/\n\nOpen Data Folder to browse.")

        except Exception as e:
            self._set_status("❌ Error", ACCENT)
            self._set_timer("❌", ACCENT)
            self._show_alert("Processing Error", str(e)[:300])
            print(f"Processing error: {e}")
            import traceback
            traceback.print_exc()

        finally:
            time.sleep(2)
            self._reset_ui()

    @objc.python_method
    def _save_local_files(self, title, transcript, enriched_md, summary):
        """Save transcript and summary as readable .md files in their respective directories."""
        import re

        # Create safe filename from title
        safe_title = re.sub(r'[^\w\s-]', '', title)[:50].strip().replace(' ', '_')
        date_str = datetime.now().strftime('%Y-%m-%d_%H%M')
        base_name = f"{date_str}_{safe_title}"

        # Save transcript as readable markdown
        transcript_path = TRANSCRIPTS_DIR / f"{base_name}_transcript.md"
        transcript_text = transcript.get("text", "") if isinstance(transcript, dict) else str(transcript)
        transcript_md = format_transcript_as_markdown(transcript) if isinstance(transcript, dict) else transcript_text
        transcript_path.write_text(
            f"# Transcript: {title}\n"
            f"*Recorded: {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
            f"{transcript_md}\n",
            encoding="utf-8"
        )

        # Save enriched notes (structured meeting notes from transcript)
        notes_path = TRANSCRIPTS_DIR / f"{base_name}_notes.md"
        notes_path.write_text(
            f"# Meeting Notes: {title}\n"
            f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
            f"{enriched_md}\n",
            encoding="utf-8"
        )

        # Save summary in summaries directory
        summary_path = SUMMARIES_DIR / f"{base_name}_summary.md"
        summary_path.write_text(
            f"# Summary: {title}\n"
            f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
            f"{summary}\n",
            encoding="utf-8"
        )

        print(f"📁 Saved locally: {transcript_path.name}, {notes_path.name}, {summary_path.name}")

    @objc.python_method
    def _ask_notion_push(self, title, dur_str):
        """Show dialog asking whether to push to Notion. Returns True/False."""
        result = [False]

        def _do():
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("Push to Notion?")
            alert.setInformativeText_(
                f"'{title}' ({dur_str}) has been saved locally.\n\n"
                "Would you also like to push it to Notion?"
            )
            alert.addButtonWithTitle_("Push to Notion")
            alert.addButtonWithTitle_("Keep Local Only")
            result[0] = alert.runModal() == AppKit.NSAlertFirstButtonReturn

        # Run on main thread and wait
        AppKit.NSOperationQueue.mainQueue().addOperations_waitUntilFinished_(
            [AppKit.NSBlockOperation.blockOperationWithBlock_(_do)], True
        )
        return result[0]

    @objc.python_method
    def _push_to_notion(self, meeting, title, enriched_md, summary, transcript_text, dur_str, attendee_emails=None):
        """Push a meeting to Notion (enrich existing page or create new)."""
        from local_store import update_meeting as update_local

        self._set_status("Searching Notion...", WARNING)
        matching_page = search_meeting_page(
            title=title,
            date=datetime.now(),
        )

        if matching_page:
            page_id = matching_page["id"]
            page_title = _get_page_title(matching_page)

            self._set_status(f"Enriching: {page_title[:25]}...", WARNING)
            existing_md = get_page_content_as_markdown(page_id)

            if existing_md.strip():
                enriched_md = enrich_notes(
                    manual_notes=existing_md,
                    transcript_text=transcript_text,
                    meeting_title=page_title,
                )

            self._set_status("Updating Notion...", WARNING)
            update_page_content(page_id, enriched_md)
            update_summary(page_id, summary)

            update_local(meeting["id"], pushed_to_notion=True, notion_page_id=page_id)

            # Sync attendee emails to linked contact pages (only on confident match)
            if attendee_emails:
                self._set_status("📇 Syncing contact emails...", WARNING)
                n_synced = sync_contact_emails(page_id, attendee_emails)
                if n_synced:
                    print(f"📇 Updated {n_synced} contact email(s)")

            # Trigger the meeting-notes-agent to clean up and polish
            self._set_status("🤖 Agent polishing notes...", WARNING)
            agent_ok = trigger_agent_on_page(page_id)

            if agent_ok:
                self._set_status(f"✅ Enriched & polished: {page_title[:20]}", SUCCESS)
                self._set_timer("✅", SUCCESS)
                self._set_subtitle(f"Done — {dur_str} recording → Notion (polished)")
                self._show_alert("✅ Notes Enriched & Polished", f"{page_title}\nTranscript added + agent cleaned up ({dur_str} recording)")
            else:
                self._set_status(f"✅ Enriched: {page_title[:20]}", SUCCESS)
                self._set_timer("✅", SUCCESS)
                self._set_subtitle(f"Done — {dur_str} recording → Notion")
                self._show_alert("✅ Notes Enriched", f"{page_title}\nAdded insights ({dur_str} recording)\n⚠ Agent polish skipped")

        else:
            self._set_status("Creating Notion page...", WARNING)
            page_id = create_meeting_page(
                title=title,
                content_markdown=enriched_md,
                summary=summary,
            )

            update_local(meeting["id"], pushed_to_notion=True, notion_page_id=page_id)

            # Trigger the meeting-notes-agent to clean up and polish
            self._set_status("🤖 Agent polishing notes...", WARNING)
            agent_ok = trigger_agent_on_page(page_id)

            if agent_ok:
                self._set_status(f"✅ Created & polished: {title[:25]}", SUCCESS)
                self._set_timer("✅", SUCCESS)
                self._set_subtitle(f"Done — new page from {dur_str} (polished)")
                self._show_alert("✅ Notes Created & Polished", f"{title}\nCreated from {dur_str} recording + agent cleaned up")
            else:
                self._set_status(f"✅ Created: {title[:25]}", SUCCESS)
                self._set_timer("✅", SUCCESS)
                self._set_subtitle(f"Done — new page from {dur_str}")
                self._show_alert("✅ Notes Created", f"{title}\nCreated from {dur_str} recording\n⚠ Agent polish skipped")

    # ───────────────────────────────────────────────────────
    # Meeting watcher
    # ───────────────────────────────────────────────────────

    @objc.python_method
    def _init_watcher(self):
        def _do():
            import os
            if not os.path.exists("credentials.json") and not os.path.exists("token.json"):
                self._show_alert("Google Calendar Setup", 
                    "To use Google Calendar integration, you need a 'credentials.json' file.\n\n"
                    "1. Go to Google Cloud Console\n"
                    "2. Enable Google Calendar API\n"
                    "3. Create OAuth Desktop Client credentials\n"
                    "4. Download and save as 'credentials.json' in the meeting-recorder folder.\n\n"
                    "We will try to open a browser window to authenticate if the credentials exist."
                )
            
            if self.watcher.request_calendar_access():
                self.watcher.start()
                self._update_footer_with_meetings()
            else:
                self._set_footer("🔔 Calendar: no access")
                self._auto_detect = False
                self._on_main(lambda: self.detect_check.setState_(AppKit.NSControlStateValueOff))
        threading.Thread(target=_do, daemon=True).start()

    @objc.python_method
    def _update_footer_with_meetings(self):
        def _loop():
            from datetime import datetime
            import time
            while self.watcher._running:
                try:
                    meetings = self.watcher.get_upcoming_meetings(within_minutes=120)
                    if meetings:
                        # Find the first meeting that hasn't ended yet
                        now = datetime.now()
                        upcoming = [m for m in meetings if m["end"] > now]
                        if upcoming:
                            next_mtg = upcoming[0]
                            time_str = next_mtg["start"].strftime("%H:%M")
                            title = next_mtg["title"]
                            display_text = f"🔔 Next: {title} @ {time_str}"
                            if len(display_text) > 40:
                                display_text = display_text[:37] + "..."
                            self._set_footer(display_text)
                        else:
                            self._set_footer("🔔 Calendar: no upcoming")
                    else:
                        self._set_footer("🔔 Calendar: connected (no upcoming)")
                except Exception:
                    self._set_footer("🔔 Calendar: error reading meetings")
                time.sleep(15)
        
        threading.Thread(target=_loop, daemon=True).start()

    @objc.python_method
    def _on_meeting_detected(self, event_info):
        if not self._auto_detect or self.recorder.is_recording:
            return

        title = event_info["title"]
        attendees = event_info["attendees"]
        start = event_info["start"]
        names = ", ".join(attendees[:3])
        if len(attendees) > 3:
            names += f" +{len(attendees) - 3}"
        time_str = start.strftime("%H:%M")

        def _silent_start():
            self._meeting_title = title
            self._attendee_emails = event_info.get("attendee_emails", [])
            try:
                self.recorder.start()
                self._set_recording_ui()
                self._set_menu_bar_recording(True)
                self._set_status("Recording...", ACCENT)
                self._set_subtitle(f"Recording: {title}")
                self._start_timer()
                
                # Send a subtle macOS notification so they know it started
                try:
                    import Foundation
                    notification = Foundation.NSUserNotification.alloc().init()
                    notification.setTitle_("Meeting Recorder")
                    notification.setInformativeText_(f"Silently recording: {title}")
                    Foundation.NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(notification)
                except Exception:
                    pass

            except RuntimeError as e:
                print(f"Failed to auto-start recording: {e}")

        self._on_main(_silent_start)

    # ───────────────────────────────────────────────────────
    # Secondary actions
    # ───────────────────────────────────────────────────────

    def onOpenDataFolder_(self, sender):
        """Launch the Finder folder containing Summaries and Transcripts."""
        import subprocess
        subprocess.run(["open", str(PROJECT_DIR / "data")])

    def onShowMeetings_(self, sender):
        meetings = self.watcher.get_upcoming_meetings(within_minutes=120)
        if not meetings:
            self._show_alert("Upcoming Meetings", "No meetings with guests in the next 2 hours.")
            return

        lines = []
        for m in meetings[:5]:
            t = m["start"].strftime("%H:%M")
            names = ", ".join(m["attendees"][:2])
            if len(m["attendees"]) > 2:
                names += f" +{len(m['attendees']) - 2}"
            lines.append(f"🕐 {t}  {m['title']}")
            lines.append(f"      with {names}")
            lines.append("")

        self._show_alert("Upcoming Meetings (next 2h)", "\n".join(lines))

    def onCheckSetup_(self, sender):
        status = self.recorder.check_setup()
        lines = []
        lines.append(f"{'✅' if status['blackhole_found'] else '❌'} BlackHole 2ch")
        lines.append(f"{'✅' if status['mic_found'] else '❌'} Microphone")
        lines.append(f"{'✅' if status['multi_output_found'] else '❌'} Multi-Output Device")
        lines.append(f"{'✅' if status['switchaudio_installed'] else '❌'} SwitchAudioSource")
        lines.append("")
        lines.append(f"{'✅' if self.watcher._access_granted else '❌'} Calendar Access")

        all_good = all(status.values())
        if all_good:
            lines.append("\n🎉 Everything is set up correctly!")
        else:
            lines.append("\n⚠️ Some items need attention.")
            if not status["blackhole_found"]:
                lines.append("→ Reboot for BlackHole to appear")
            if not status["multi_output_found"]:
                lines.append("→ Create Multi-Output in Audio MIDI Setup")

        self._show_alert("Audio Setup", "\n".join(lines))

    def onTogglePin_(self, sender):
        on = self.pin_check.state() == AppKit.NSControlStateValueOn
        level = AppKit.NSFloatingWindowLevel if on else AppKit.NSNormalWindowLevel
        self.window.setLevel_(level)

    def onToggleDetect_(self, sender):
        self._auto_detect = self.detect_check.state() == AppKit.NSControlStateValueOn

    def onQuit_(self, sender):
        if self.recorder.is_recording:
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("Quit")
            alert.setInformativeText_("Recording in progress. Stop and quit?")
            alert.addButtonWithTitle_("Quit")
            alert.addButtonWithTitle_("Cancel")
            if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
                return
            self.recorder.stop()

        self.watcher.stop()
        if self._dashboard_process and self._dashboard_process.poll() is None:
            self._dashboard_process.terminate()
        AppKit.NSApp.terminate_(None)

    # ─── Window close → hide (app stays in menu bar) ───
    def windowWillClose_(self, notification):
        self.window.orderOut_(None)

    # ─── Helpers ───

    @objc.python_method
    def _show_alert(self, title, message):
        def _do():
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_(title)
            alert.setInformativeText_(message)
            alert.addButtonWithTitle_("OK")
            alert.runModal()
        self._on_main(_do)


def main():
    """Entry point."""
    if not OPENAI_API_KEY:
        print("❌ OPENAI_API_KEY not found in tum_sales_agent/.env")
        return
    if not NOTION_TOKEN:
        print("❌ NOTION_TOKEN not found in tum_sales_agent/.env")
        return

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()


if __name__ == "__main__":
    main()
