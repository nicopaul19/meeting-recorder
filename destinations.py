from __future__ import annotations
"""
Destinations — read MY notes from, and write the summary to, a Notion page
or a Google Doc given its URL.
"""
import re
import time

from googleapiclient.errors import HttpError
from notion_client import Client
from notion_client.errors import APIResponseError

from config import NOTION_TOKEN
import google_auth
from synthesizer import MeetingSummary, to_notion_blocks, _meta_line

GDOC_RE = re.compile(r"docs\.google\.com/document/(?:u/\d+/)?d/([a-zA-Z0-9_-]{20,})")
NOTION_HOST_RE = re.compile(r"(?:^|//)(?:www\.)?(?:[\w-]+\.)?notion\.(?:so|site)/", re.I)
NOTION_ID_RE = re.compile(r"([0-9a-f]{32})|([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)


class DestinationError(Exception):
    """User-facing error (message is shown in the UI as-is)."""


SUMMARY_START = "🎙️ Meeting summary"
SUMMARY_END = "= from your notes · grey = added from the transcript"


def strip_previous_summaries(text: str) -> str:
    """Drop summary blocks this tool appended earlier (start heading → legend line)."""
    out, skipping = [], False
    for line in text.splitlines():
        if SUMMARY_START in line:
            skipping = True
            continue
        if skipping:
            if SUMMARY_END in line:
                skipping = False
            continue
        out.append(line)
    return "\n".join(out).strip()


def gdoc_tab(url: str) -> str:
    """Tab id from a Google Docs URL (…/edit?tab=t.0), or ''."""
    m = re.search(r"[?&#]tab=([\w.-]+)", url or "")
    return m.group(1) if m else ""


def detect(url: str) -> tuple[str, str] | None:
    """Return ("notion", page_id) or ("gdoc", doc_id), or None if unrecognised."""
    url = (url or "").strip()
    m = GDOC_RE.search(url)
    if m:
        return "gdoc", m.group(1)
    if NOTION_HOST_RE.search(url):
        # Database row opened as a peek: …?v=<view>&p=<page>
        p = re.search(r"[?&]p=([0-9a-f]{32})", url, re.I)
        path = url.split("?")[0].split("#")[0]
        ids = NOTION_ID_RE.findall(p.group(1) if p else path)
        if ids:
            raw = (ids[-1][0] or ids[-1][1]).replace("-", "")
            return "notion", f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    return None


def label(kind: str) -> str:
    return {"notion": "Notion page", "gdoc": "Google Doc"}.get(kind, "page")


# ─── Notion ───────────────────────────────────────────────────

def _notion() -> Client:
    return Client(auth=NOTION_TOKEN)


def _notion_error(e: APIResponseError) -> DestinationError:
    if e.code in ("object_not_found", "unauthorized", "restricted_resource"):
        return DestinationError(
            "Can't access that Notion page. Open it in Notion → ••• → Connections → "
            "add your meeting integration, then try again."
        )
    return DestinationError(f"Notion error: {e}")


def _notion_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    return ""


def read_notion(page_id: str) -> tuple[str, str]:
    """Return (title, markdown content) of a Notion page."""
    from notion_bridge import get_page_content_as_markdown
    try:
        page = _notion().pages.retrieve(page_id=page_id)
        return _notion_title(page), get_page_content_as_markdown(page_id)
    except APIResponseError as e:
        raise _notion_error(e)


def append_notion(page_id: str, summary: MeetingSummary, meta: dict) -> str:
    client = _notion()
    blocks = to_notion_blocks(summary, meta)
    try:
        for i in range(0, len(blocks), 90):
            client.blocks.children.append(block_id=page_id, children=blocks[i:i + 90])
            time.sleep(0.2)
    except APIResponseError as e:
        raise _notion_error(e)
    return f"https://www.notion.so/{page_id.replace('-', '')}"


# ─── Google Docs ──────────────────────────────────────────────

def _docs(allow_interactive: bool):
    try:
        return google_auth.docs_service(allow_interactive=allow_interactive)
    except google_auth.AuthRequired as e:
        raise DestinationError(f"Google Docs access isn't connected yet ({e}).")
    except google_auth.NetworkUnavailable:
        raise DestinationError("Can't reach Google right now. Check your connection and retry.")


def _gdoc_error(e: HttpError) -> DestinationError:
    status = getattr(e.resp, "status", 0)
    text = str(e)
    if "accessNotConfigured" in text or "has not been used" in text or "SERVICE_DISABLED" in text:
        return DestinationError(
            "The Google Docs API is off for this Google Cloud project. Enable it at "
            "console.cloud.google.com/apis/library/docs.googleapis.com, then retry."
        )
    if status in (403, 404):
        return DestinationError("Can't open that Google Doc with your Google account (no access, or wrong link).")
    return DestinationError(f"Google Docs error: {text[:200]}")


def _get_doc(service, doc_id: str) -> dict:
    try:
        return service.documents().get(documentId=doc_id, includeTabsContent=True).execute(num_retries=2)
    except HttpError as e:
        raise _gdoc_error(e)


def _pick_tab(doc: dict, tab_id: str) -> tuple[str, dict, str]:
    """(tab_id, body, tab_title) for the requested tab, else the first tab."""
    tabs = []

    def walk(items):
        for t in items or []:
            tabs.append(t)
            walk(t.get("childTabs"))
    walk(doc.get("tabs"))
    if not tabs:  # legacy response without tabs
        return "", doc.get("body", {}), ""
    chosen = next((t for t in tabs if t.get("tabProperties", {}).get("tabId") == tab_id), tabs[0])
    props = chosen.get("tabProperties", {})
    return props.get("tabId", ""), chosen.get("documentTab", {}).get("body", {}), props.get("title", "")


def _gdoc_text(body: dict) -> str:
    def walk(content: list) -> list[str]:
        out = []
        for el in content or []:
            if "paragraph" in el:
                para = el["paragraph"]
                text = "".join(r.get("textRun", {}).get("content", "") for r in para.get("elements", []))
                prefix = "- " if para.get("bullet") else ""
                if text.strip():
                    out.append(prefix + text.rstrip("\n"))
            elif "table" in el:
                for row in el["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        out += walk(cell.get("content"))
        return out
    return "\n".join(walk(body.get("content", [])))


def read_gdoc(doc_id: str, tab_id: str = "") -> tuple[str, str]:
    doc = _get_doc(_docs(allow_interactive=True), doc_id)
    _, body, tab_title = _pick_tab(doc, tab_id)
    title = doc.get("title", "") + (f" › {tab_title}" if tab_title and len(doc.get("tabs", [])) > 1 else "")
    return title, _gdoc_text(body)


def append_gdoc(doc_id: str, summary: MeetingSummary, meta: dict, tab_id: str = "") -> str:
    service = _docs(allow_interactive=True)
    doc = _get_doc(service, doc_id)
    tab_id, body, _ = _pick_tab(doc, tab_id)
    start = body["content"][-1]["endIndex"] - 1
    tab = {"tabId": tab_id} if tab_id else {}

    def R(a: int, b: int) -> dict:
        return {"startIndex": a, "endIndex": b, **tab}

    # Build the text once, remembering which ranges get which style.
    text_parts: list[str] = []
    styles: list[tuple] = []  # (kind, start, end)
    pos = start

    # The leading "\n" ends the doc's last paragraph; our paragraphs start after it.
    text_parts.append("\n")
    pos = first = start + 1

    def add(line: str, *kinds: str):
        nonlocal pos
        line = line.replace("\n", " ") + "\n"
        n = len(line.encode("utf-16-le")) // 2  # Docs indexes count UTF-16 units (emoji = 2+)
        text_parts.append(line)
        for k in kinds:
            styles.append((k, pos, pos + n))
        pos += n
    add(f"🎙️ Meeting summary: {summary.title}", "h2")
    add(_meta_line(meta), "gray")
    add(f"TL;DR  {summary.tldr}", "para")

    def points(pts):
        for p in pts:
            add(p.text, "bullet", "you" if p.from_user else "gray")
            for d in p.details:
                add(f"↳ {d}", "detail", "gray")

    for sec in summary.sections:
        add(sec.heading, "h3")
        points(sec.points)
    if summary.decisions:
        add("Decisions", "h3")
        points(summary.decisions)
    if summary.action_items:
        add("Action items", "h3")
        for a in summary.action_items:
            owner = f"{a.owner}: " if a.owner else ""
            due = f" (due {a.due})" if a.due else ""
            add(f"{owner}{a.task}{due}", "check", "you" if a.from_user else "para")
    add("Highlighted = from your notes · grey = added from the transcript", "gray", "small")

    requests = [{"insertText": {"location": {"index": start, **tab}, "text": "".join(text_parts)}}]
    # Reset inherited styles on the whole insert first.
    requests.append({"updateParagraphStyle": {
        "range": R(first, pos),
        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "fields": "namedStyleType"}})
    requests.append({"updateTextStyle": {
        "range": R(first, pos),
        "textStyle": {}, "fields": "bold,foregroundColor,backgroundColor,fontSize"}})
    # Text inserted after a list would otherwise inherit its bullets.
    requests.append({"deleteParagraphBullets": {"range": R(first, pos)}})

    gray = {"color": {"rgbColor": {"red": 0.43, "green": 0.43, "blue": 0.45}}}
    yellow = {"color": {"rgbColor": {"red": 1.0, "green": 0.95, "blue": 0.69}}}
    bullets = []
    for kind, s, e in styles:
        rng = R(s, e - 1 if e - 1 > s else e)
        if kind in ("h2", "h3"):
            requests.append({"updateParagraphStyle": {"range": R(s, e),
                "paragraphStyle": {"namedStyleType": "HEADING_2" if kind == "h2" else "HEADING_3"},
                "fields": "namedStyleType"}})
        elif kind == "gray":
            requests.append({"updateTextStyle": {"range": rng,
                "textStyle": {"foregroundColor": gray}, "fields": "foregroundColor"}})
        elif kind == "you":
            requests.append({"updateTextStyle": {"range": rng,
                "textStyle": {"bold": True, "backgroundColor": yellow}, "fields": "bold,backgroundColor"}})
        elif kind == "small":
            requests.append({"updateTextStyle": {"range": rng,
                "textStyle": {"fontSize": {"magnitude": 9, "unit": "PT"}}, "fields": "fontSize"}})
        elif kind == "detail":
            requests.append({"updateParagraphStyle": {"range": R(s, e),
                "paragraphStyle": {"indentStart": {"magnitude": 36, "unit": "PT"},
                                   "indentFirstLine": {"magnitude": 36, "unit": "PT"}},
                "fields": "indentStart,indentFirstLine"}})
        elif kind in ("bullet", "check"):
            bullets.append((kind, s, e))
    # Bullets last (no leading tabs, so indices don't shift). Consecutive
    # paragraphs of the same kind become one list.
    groups: list[list] = []
    for kind, s, e in bullets:
        if groups and groups[-1][0] == kind and groups[-1][2] == s:
            groups[-1][2] = e
        else:
            groups.append([kind, s, e])
    for kind, s, e in groups:
        requests.append({"createParagraphBullets": {"range": R(s, e),
            "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE" if kind == "bullet" else "BULLET_CHECKBOX"}})

    try:
        service.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute(num_retries=2)
    except HttpError as e:
        raise _gdoc_error(e)
    return f"https://docs.google.com/document/d/{doc_id}/edit" + (f"?tab={tab_id}" if tab_id else "")


# ─── Dispatch ─────────────────────────────────────────────────

def read_notes(url: str) -> tuple[str, str, str]:
    """Return (kind, title, text) for a Notion/Google Doc URL."""
    found = detect(url)
    if not found:
        raise DestinationError("That link isn't a Notion page or a Google Doc.")
    kind, ident = found
    title, text = read_notion(ident) if kind == "notion" else read_gdoc(ident, gdoc_tab(url))
    return kind, title, strip_previous_summaries(text)


def write_summary(url: str, summary: MeetingSummary, meta: dict) -> tuple[str, str]:
    """Append the summary to the page. Returns (kind, canonical_url)."""
    kind, ident = detect(url)
    link = append_notion(ident, summary, meta) if kind == "notion" else append_gdoc(ident, summary, meta, gdoc_tab(url))
    return kind, link
