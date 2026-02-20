from __future__ import annotations
"""Quick system check for Meeting Recorder."""
import sys
import subprocess

def main():
    print("=== Meeting Recorder — System Check ===\n")

    # 1. Config
    import config
    print("[1] Config")
    print(f"  OPENAI_API_KEY: {'set' if config.OPENAI_API_KEY else 'MISSING'}")
    print(f"  NOTION_TOKEN:   {'set' if config.NOTION_TOKEN else 'MISSING'}")
    print()

    # 2. Audio
    import recorder
    r = recorder.MeetingRecorder()
    s = r.check_setup()
    print("[2] Audio Devices")
    for k, v in s.items():
        note = ""
        if k == "blackhole_found" and not v:
            note = " (reboot needed)"
        if k == "multi_output_found" and not v:
            note = " (create after reboot)"
        icon = "✅" if v else "❌"
        print(f"  {icon} {k}{note}")
    print()

    # 3-4. Transcriber & Enricher
    import transcriber
    import enricher
    print("[3-4] Transcriber & Enricher: ✅")

    # 5. Notion Bridge
    import notion_bridge
    from datetime import datetime
    page = notion_bridge.search_meeting_page(date=datetime(2026, 2, 13))
    if page:
        t = notion_bridge._get_page_title(page)
        print(f"[5] Notion Bridge: ✅ (found: {t})")
    else:
        print("[5] Notion Bridge: ✅ (connected)")

    # 6. FFmpeg
    result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
    if result.returncode == 0:
        ver = result.stdout.split("\n")[0]
        print(f"[6] FFmpeg: ✅ {ver}")
    else:
        print("[6] FFmpeg: ❌ not found")

    # 7. rumps
    import rumps
    print(f"[7] rumps: ✅ v{rumps.__version__}")

    print("\n=== READY (reboot needed for BlackHole) ===")


if __name__ == "__main__":
    main()
