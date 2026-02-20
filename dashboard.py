from __future__ import annotations
"""
Dashboard — local web UI for browsing meeting recordings.

Run standalone:  python dashboard.py
Or launched from the menu bar app's "Open Dashboard" action.

Serves on http://localhost:5555
"""
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_DIR))

from flask import Flask, request, redirect, url_for, Response

from local_store import list_meetings, get_meeting, update_meeting

app = Flask(__name__)


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"


def _fmt_date(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%b %d, %Y  %H:%M")
    except Exception:
        return iso


# ─── HTML Templates (inline) ─────────────────────────────────────

STYLE = """
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f0f1a; color: #eaeaea; padding: 24px; max-width: 900px; margin: 0 auto; }
  a { color: #e94560; text-decoration: none; }
  a:hover { text-decoration: underline; }
  h1 { font-size: 24px; margin-bottom: 4px; }
  .subtitle { color: #889; font-size: 14px; margin-bottom: 24px; }
  .card { background: #162240; border-radius: 12px; padding: 16px 20px; margin-bottom: 12px;
          display: flex; justify-content: space-between; align-items: center; }
  .card:hover { background: #1f3056; }
  .card-left h3 { font-size: 16px; margin-bottom: 4px; }
  .card-left .meta { color: #889; font-size: 12px; }
  .badge { font-size: 11px; padding: 3px 10px; border-radius: 20px; font-weight: 600; }
  .badge-notion { background: #2ecc7140; color: #2ecc71; }
  .badge-local { background: #e9456040; color: #e94560; }
  .back { display: inline-block; margin-bottom: 16px; font-size: 13px; color: #889; }
  .back:hover { color: #eaeaea; }
  .detail-header { margin-bottom: 24px; }
  .detail-header h1 { margin-bottom: 6px; }
  .detail-meta { color: #889; font-size: 13px; margin-bottom: 12px; }
  .section { background: #162240; border-radius: 12px; padding: 20px; margin-bottom: 16px; }
  .section h2 { font-size: 16px; margin-bottom: 12px; color: #e94560; }
  .section pre { white-space: pre-wrap; word-wrap: break-word; font-size: 13px;
                 line-height: 1.6; color: #ccc; }
  .section .md { font-size: 14px; line-height: 1.7; }
  .section .md h2 { font-size: 15px; color: #fff; margin: 16px 0 8px; }
  .section .md h3 { font-size: 14px; color: #ddd; margin: 12px 0 6px; }
  .section .md ul { padding-left: 20px; }
  .section .md li { margin-bottom: 4px; }
  .btn { display: inline-block; padding: 8px 20px; border-radius: 8px; border: none;
         font-size: 13px; font-weight: 600; cursor: pointer; color: #fff; }
  .btn-notion { background: #2ecc71; }
  .btn-notion:hover { background: #27ae60; }
  .empty { text-align: center; color: #556; padding: 60px 20px; }
  .empty h2 { font-size: 18px; margin-bottom: 8px; color: #889; }
</style>
"""


@app.route("/")
def index():
    meetings = list_meetings()

    if not meetings:
        return f"""<!DOCTYPE html><html><head><title>Meeting Recorder</title>{STYLE}</head><body>
        <h1>Meeting Recorder</h1>
        <p class="subtitle">Local meeting dashboard</p>
        <div class="empty">
            <h2>No meetings yet</h2>
            <p>Record a meeting and it will appear here.</p>
        </div>
        </body></html>"""

    rows = []
    for m in meetings:
        mid = m["id"]
        title = m["title"] or "Untitled Meeting"
        date = _fmt_date(m["date"])
        dur = _fmt_duration(m["duration_seconds"])
        if m["pushed_to_notion"]:
            badge = '<span class="badge badge-notion">Notion</span>'
        else:
            badge = '<span class="badge badge-local">Local only</span>'

        rows.append(f"""
        <a href="/meeting/{mid}" style="text-decoration:none;color:inherit;">
        <div class="card">
            <div class="card-left">
                <h3>{title}</h3>
                <div class="meta">{date} &middot; {dur}</div>
            </div>
            <div>{badge}</div>
        </div>
        </a>""")

    return f"""<!DOCTYPE html><html><head><title>Meeting Recorder</title>{STYLE}</head><body>
    <h1>Meeting Recorder</h1>
    <p class="subtitle">{len(meetings)} meeting{"s" if len(meetings) != 1 else ""} recorded</p>
    {"".join(rows)}
    </body></html>"""


@app.route("/meeting/<meeting_id>")
def meeting_detail(meeting_id):
    m = get_meeting(meeting_id)
    if not m:
        return "Meeting not found", 404

    title = m.get("title") or "Untitled Meeting"
    date = _fmt_date(m.get("date", ""))
    dur = _fmt_duration(m.get("duration_seconds", 0))

    if m.get("pushed_to_notion"):
        notion_status = f'<span class="badge badge-notion">Pushed to Notion</span>'
    else:
        notion_status = f"""<span class="badge badge-local">Local only</span>
        <form method="POST" action="/meeting/{meeting_id}/push-notion" style="display:inline;margin-left:12px;">
            <button type="submit" class="btn btn-notion">Push to Notion</button>
        </form>"""

    # Enriched notes
    notes_html = ""
    if m.get("enriched_notes"):
        notes_html = f"""<div class="section">
            <h2>Enriched Notes</h2>
            <pre>{_escape(m["enriched_notes"])}</pre>
        </div>"""

    # Summary
    summary_html = ""
    if m.get("summary"):
        summary_html = f"""<div class="section">
            <h2>WhatsApp Briefing</h2>
            <pre>{_escape(m["summary"])}</pre>
        </div>"""

    # Transcript
    transcript_html = ""
    transcript = m.get("transcript", {})
    text = transcript.get("text", "")
    if text:
        transcript_html = f"""<div class="section">
            <h2>Full Transcript</h2>
            <pre>{_escape(text)}</pre>
        </div>"""

    return f"""<!DOCTYPE html><html><head><title>{title} - Meeting Recorder</title>{STYLE}</head><body>
    <a href="/" class="back">&larr; All meetings</a>
    <div class="detail-header">
        <h1>{_escape(title)}</h1>
        <div class="detail-meta">{date} &middot; {dur}</div>
        <div style="margin-top:8px;">{notion_status}</div>
    </div>
    {summary_html}
    {notes_html}
    {transcript_html}
    </body></html>"""


@app.route("/meeting/<meeting_id>/push-notion", methods=["POST"])
def push_to_notion(meeting_id):
    m = get_meeting(meeting_id)
    if not m:
        return "Meeting not found", 404

    if m.get("pushed_to_notion"):
        return redirect(url_for("meeting_detail", meeting_id=meeting_id))

    try:
        from notion_bridge import (
            search_meeting_page,
            get_page_content_as_markdown,
            create_meeting_page,
            update_page_content,
            update_summary,
            _get_page_title,
        )
        from enricher import enrich_notes

        title = m.get("title") or f"Meeting {m.get('date', '')}"
        enriched_md = m.get("enriched_notes", "")
        summary = m.get("summary", "")

        # Search for existing Notion page
        meeting_date = datetime.fromisoformat(m["date"]) if m.get("date") else datetime.now()
        matching_page = search_meeting_page(title=title, date=meeting_date)

        if matching_page:
            page_id = matching_page["id"]
            page_title = _get_page_title(matching_page)
            existing_md = get_page_content_as_markdown(page_id)

            if existing_md.strip() and enriched_md:
                enriched_md = enrich_notes(
                    manual_notes=existing_md,
                    transcript_text=m.get("transcript", {}).get("text", ""),
                    meeting_title=page_title,
                )

            update_page_content(page_id, enriched_md)
            if summary:
                update_summary(page_id, summary)
        else:
            page_id = create_meeting_page(
                title=title,
                content_markdown=enriched_md,
                summary=summary,
            )

        update_meeting(meeting_id, pushed_to_notion=True, notion_page_id=page_id)

    except Exception as e:
        return f"""<!DOCTYPE html><html><head><title>Error</title>{STYLE}</head><body>
        <a href="/meeting/{meeting_id}" class="back">&larr; Back</a>
        <div class="section"><h2>Error pushing to Notion</h2><pre>{_escape(str(e))}</pre></div>
        </body></html>""", 500

    return redirect(url_for("meeting_detail", meeting_id=meeting_id))


def _escape(text: str) -> str:
    """Basic HTML escaping."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def main():
    print("Starting Meeting Recorder Dashboard on http://localhost:5555")
    app.run(host="127.0.0.1", port=5555, debug=False)


if __name__ == "__main__":
    main()
