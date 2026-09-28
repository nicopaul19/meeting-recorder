from __future__ import annotations
"""
Synthesizer — merges MY notes with the AI transcript into one summary.

My bullets win: every point I wrote appears (my wording, typos fixed),
flagged from_user=True so every output format can highlight it. The
transcript adds context underneath and fills in what I missed. When the two
disagree, my version stands and the transcript's view is added as a note.

Renderers: markdown, plain text, HTML (UI + rich clipboard), Notion blocks.
Google Docs rendering lives in destinations.py.
"""
import html
from datetime import datetime

from pydantic import BaseModel, Field

from llm import parse
from enricher import PITCH_DECK_CONTEXT


class Point(BaseModel):
    text: str = Field(description="One concise bullet.")
    from_user: bool = Field(description="True if this point comes from MY notes (keep my wording).")
    details: list[str] = Field(description="Optional sub-bullets with transcript context. Empty list if none.")


class Section(BaseModel):
    heading: str
    points: list[Point]


class ActionItem(BaseModel):
    owner: str = Field(description="Person responsible, or '' if unclear.")
    task: str
    due: str = Field(description="Deadline as stated, or ''.")
    from_user: bool


class MeetingSummary(BaseModel):
    title: str
    tldr: str = Field(description="2-3 sentence overview.")
    sections: list[Section]
    decisions: list[Point]
    action_items: list[ActionItem]


SYNTH_PROMPT = """You write the final summary of a meeting by merging two sources.

1. MY NOTES: bullets I (Nicolas) wrote. This is the PRIMARY source.
2. TRANSCRIPT: an automatic transcript of the same meeting. Secondary.

HARD RULES FOR MY NOTES
- Every point from MY NOTES must appear in the summary, flagged from_user=true.
- Keep my wording. Only fix typos, expand obvious abbreviations, and translate to
  English if I wrote in German or Spanish (key terms in parentheses).
- Never drop, soften or reinterpret my points. If the transcript contradicts me,
  keep my point and add a detail such as "Transcript suggests: …".
- Put transcript context that explains or completes one of my points in that
  point's `details`.
- Merge a transcript point into mine rather than repeating it when both say the
  same thing (from_user=true).

WHAT THE TRANSCRIPT ADDS (from_user=false)
- Important points I missed: names, numbers, dates, decisions, commitments.
- Group everything by topic into a few clear sections with specific headings.
  Order sections by importance; sections that contain my points come first.
- Skip greetings, small talk and filler.
- Action items: owner, task, and deadline if one was stated. Set from_user=true
  when the action came from my notes.
- MY NOTES may come from a running document with notes for many meetings. Then
  use only the notes that belong to THIS meeting (match date, title, people and
  topics) and ignore notes for other meetings.
- If there are no notes from me, build the summary from the transcript alone.
- If the transcript is empty or missing, build it from my notes alone.

EXCLUDE the standard TUM Social AI pitch and background (below). Always write
"TUM Social AI"; never "tum.ai" or "tumai".
{pitch}

Write in English. Be concise: short bullets with bold-free plain text."""


def synthesize(
    title: str,
    user_notes: str,
    transcript_text: str,
    attendees: list[str] | None = None,
    date: str = "",
) -> MeetingSummary:
    user_msg = (
        f"MEETING: {title}\n"
        f"DATE: {date or datetime.now().strftime('%A %d %B %Y')}\n"
        f"ATTENDEES: {', '.join(attendees or []) or 'unknown'}\n\n"
        f"=== MY NOTES (primary) ===\n{user_notes.strip() or '(none)'}\n\n"
        f"=== TRANSCRIPT (secondary) ===\n{transcript_text.strip() or '(no transcript)'}"
    )
    summary = parse(SYNTH_PROMPT.format(pitch=PITCH_DECK_CONTEXT), user_msg, MeetingSummary)
    if not summary.title.strip():
        summary.title = title
    return summary


# ─── Renderers ────────────────────────────────────────────────

LEGEND = "✍️ = from your notes · everything else was added from the transcript"


def _meta_line(meta: dict) -> str:
    parts = [meta.get("date") or datetime.now().strftime("%a %d %b %Y, %H:%M")]
    if meta.get("attendees"):
        parts.append("with " + ", ".join(meta["attendees"][:6]))
    return " · ".join(parts)


def _action_md(a: ActionItem) -> str:
    owner = f"**{a.owner}**: " if a.owner else ""
    due = f" *(due {a.due})*" if a.due else ""
    mark = "✍️ " if a.from_user else ""
    return f"- [ ] {mark}{owner}{a.task}{due}"


def _point_md(p: Point) -> list[str]:
    lines = [f"- ✍️ **{p.text}**" if p.from_user else f"- {p.text}"]
    lines += [f"  - {d}" for d in p.details]
    return lines


def to_markdown(s: MeetingSummary, meta: dict) -> str:
    out = [f"# {s.title}", f"*{_meta_line(meta)}*", "", f"> **TL;DR** {s.tldr}", ""]
    for sec in s.sections:
        out.append(f"## {sec.heading}")
        for p in sec.points:
            out += _point_md(p)
        out.append("")
    if s.decisions:
        out.append("## Decisions")
        for p in s.decisions:
            out += _point_md(p)
        out.append("")
    if s.action_items:
        out.append("## Action items")
        out += [_action_md(a) for a in s.action_items]
        out.append("")
    out += ["---", f"*{LEGEND}*"]
    return "\n".join(out).strip() + "\n"


def to_text(s: MeetingSummary, meta: dict) -> str:
    def pt(p: Point) -> list[str]:
        lines = [f"• {'[YOU] ' if p.from_user else ''}{p.text}"]
        return lines + [f"    – {d}" for d in p.details]

    out = [s.title.upper(), _meta_line(meta), "", f"TL;DR: {s.tldr}", ""]
    for sec in s.sections:
        out.append(sec.heading.upper())
        for p in sec.points:
            out += pt(p)
        out.append("")
    if s.decisions:
        out.append("DECISIONS")
        for p in s.decisions:
            out += pt(p)
        out.append("")
    if s.action_items:
        out.append("ACTION ITEMS")
        for a in s.action_items:
            owner = f"{a.owner}: " if a.owner else ""
            due = f" (due {a.due})" if a.due else ""
            out.append(f"[ ] {'[YOU] ' if a.from_user else ''}{owner}{a.task}{due}")
        out.append("")
    out.append("[YOU] = from your notes; everything else was added from the transcript")
    return "\n".join(out).strip() + "\n"


def to_html(s: MeetingSummary, meta: dict) -> str:
    """Self-contained HTML with inline styles (pastes nicely into Docs/Notion)."""
    e = html.escape
    you = "background:#FFF3B0;color:#1d1d1f;font-weight:600;padding:0 2px;border-radius:3px"
    ai = "color:#6e6e73"

    def pt(p: Point) -> str:
        body = f'<span style="{you}">{e(p.text)}</span>' if p.from_user else f'<span style="{ai}">{e(p.text)}</span>'
        cls = "you" if p.from_user else "ai"
        sub = ""
        if p.details:
            sub = "<ul>" + "".join(f'<li class="ai"><span style="{ai}">{e(d)}</span></li>' for d in p.details) + "</ul>"
        return f'<li class="{cls}">{body}{sub}</li>'

    parts = [
        f"<h1>{e(s.title)}</h1>",
        f'<p class="meta" style="color:#8e8e93">{e(_meta_line(meta))}</p>',
        f'<p class="tldr"><b>TL;DR</b> {e(s.tldr)}</p>',
    ]
    for sec in s.sections:
        parts.append(f"<h2>{e(sec.heading)}</h2><ul>{''.join(pt(p) for p in sec.points)}</ul>")
    if s.decisions:
        parts.append(f"<h2>Decisions</h2><ul>{''.join(pt(p) for p in s.decisions)}</ul>")
    if s.action_items:
        items = []
        for a in s.action_items:
            owner = f"<b>{e(a.owner)}</b>: " if a.owner else ""
            due = f' <i style="color:#8e8e93">(due {e(a.due)})</i>' if a.due else ""
            text = f"{owner}{e(a.task)}{due}"
            style = you if a.from_user else ai
            items.append(f'<li class="todo {"you" if a.from_user else "ai"}">☐ <span style="{style}">{text}</span></li>')
        parts.append(f'<h2>Action items</h2><ul class="todos">{"".join(items)}</ul>')
    parts.append(
        f'<p class="legend" style="color:#8e8e93;font-size:12px">'
        f'<span style="{you}">Highlighted</span> = from your notes · grey = added from the transcript</p>'
    )
    return "\n".join(parts)


# ─── Notion blocks ────────────────────────────────────────────

def _rt(text: str, you: bool = False, gray: bool = False, bold: bool = False) -> list[dict]:
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)] or [""]
    color = "yellow_background" if you else ("gray" if gray else "default")
    return [{
        "type": "text",
        "text": {"content": c},
        "annotations": {"bold": bold or you, "color": color},
    } for c in chunks]


def _bullet(p: Point) -> dict:
    block = {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _rt(p.text, you=p.from_user, gray=not p.from_user)},
    }
    if p.details:
        block["bulleted_list_item"]["children"] = [{
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rt(d, gray=True)},
        } for d in p.details]
    return block


def to_notion_blocks(s: MeetingSummary, meta: dict) -> list[dict]:
    def heading(level: int, text: str) -> dict:
        key = f"heading_{level}"
        return {"object": "block", "type": key, key: {"rich_text": _rt(text)}}

    blocks = [
        {"object": "block", "type": "divider", "divider": {}},
        heading(2, f"🎙️ Meeting summary: {s.title}"),
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt(_meta_line(meta), gray=True)}},
        {"object": "block", "type": "callout", "callout": {
            "rich_text": _rt(s.tldr), "icon": {"type": "emoji", "emoji": "💡"}, "color": "gray_background"}},
    ]
    for sec in s.sections:
        blocks.append(heading(3, sec.heading))
        blocks += [_bullet(p) for p in sec.points]
    if s.decisions:
        blocks.append(heading(3, "Decisions"))
        blocks += [_bullet(p) for p in s.decisions]
    if s.action_items:
        blocks.append(heading(3, "Action items"))
        for a in s.action_items:
            owner = f"{a.owner}: " if a.owner else ""
            due = f" (due {a.due})" if a.due else ""
            blocks.append({"object": "block", "type": "to_do", "to_do": {
                "rich_text": _rt(f"{owner}{a.task}{due}", you=a.from_user), "checked": False}})
    blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text":
        _rt("Highlighted", you=True) + _rt(" = from your notes · grey = added from the transcript", gray=True)}})
    return blocks
