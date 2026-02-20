from __future__ import annotations
"""
Notion Bridge — connects the meeting recorder to Notion.

Imports and reuses functions from the existing meeting-notes-agent,
handles page search/matching, and creates or enriches pages.
"""
import sys
import re
import json
import time
from pathlib import Path
from datetime import datetime, timezone
from difflib import SequenceMatcher

PROJECT_DIR = Path(__file__).parent.resolve()

from notion_client import Client
import requests as http_requests

from config import (
    NOTION_TOKEN,
    NOTION_DB_MEETINGS_ID,
    MEETING_AGENT_DIR,
)

# ---------------------------------------------------------------------------
# Import from meeting-notes-agent using importlib to avoid config.py clash.
# Their notion_api.py imports from THEIR config.py — if we just add to sys.path,
# Python finds OUR config.py first. So we load theirs explicitly by file path.
# ---------------------------------------------------------------------------
import importlib.util


def _load_agent_module(module_name: str):
    """Load a module from the meeting-notes-agent directory by file path."""
    spec = importlib.util.spec_from_file_location(
        module_name,
        str(MEETING_AGENT_DIR / f"{module_name}.py"),
    )
    mod = importlib.util.module_from_spec(spec)

    # For notion_api, it imports from "config" — we need to ensure it gets
    # the meeting-notes-agent config, not ours. Temporarily inject it.
    if module_name == "notion_api":
        agent_config = _load_agent_module("config")
        sys.modules["config"] = agent_config

    spec.loader.exec_module(mod)

    # Restore our config
    import config as our_config
    sys.modules["config"] = our_config

    return mod


_notion_api = _load_agent_module("notion_api")

markdown_to_blocks = _notion_api.markdown_to_blocks
blocks_to_markdown = _notion_api.blocks_to_markdown
get_page_blocks = _notion_api.get_page_blocks
replace_page_blocks = _notion_api.replace_page_blocks


NOTION_API_VERSION = "2022-06-28"
SUMMARY_PROPERTY = "Summary (Comms Briefing)"


def _get_client() -> Client:
    return Client(auth=NOTION_TOKEN)


def _api_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


def search_meeting_page(
    title: str = "",
    date: datetime | None = None,
) -> dict | None:
    """
    Search for a matching meeting page in the Notion DB.

    Matches by:
    1. Title similarity (fuzzy match, threshold 0.5)
    2. Date (same day)

    Returns the best matching page dict, or None if no match.
    """
    # Build filter
    filter_conditions = []

    if date:
        date_str = date.strftime("%Y-%m-%d")
        filter_conditions.append({
            "timestamp": "created_time",
            "created_time": {
                "on_or_after": f"{date_str}T00:00:00Z",
            },
        })
        filter_conditions.append({
            "timestamp": "created_time",
            "created_time": {
                "before": f"{date_str}T23:59:59Z",
            },
        })

    query_payload = {
        "page_size": 100,
    }

    if filter_conditions:
        if len(filter_conditions) > 1:
            query_payload["filter"] = {"and": filter_conditions}
        else:
            query_payload["filter"] = filter_conditions[0]

    try:
        url = f"https://api.notion.com/v1/databases/{NOTION_DB_MEETINGS_ID}/query"
        resp = http_requests.post(url, headers=_api_headers(), json=query_payload)
        resp.raise_for_status()
        response = resp.json()
    except Exception as e:
        print(f"⚠ Notion query error: {e}")
        return None

    pages = response.get("results", [])

    if not pages:
        return None

    if not title:
        # No title to match — return most recent page from the date
        return pages[0]

    # Fuzzy match by title
    best_match = None
    best_score = 0.0

    for page in pages:
        page_title = _get_page_title(page)
        score = SequenceMatcher(None, title.lower(), page_title.lower()).ratio()

        if score > best_score:
            best_score = score
            best_match = page

    if best_score >= 0.4:
        return best_match

    return None


def _get_page_title(page: dict) -> str:
    """Extract title from a Notion page object."""
    props = page.get("properties", {})
    for prop_name, prop_val in props.items():
        if prop_val.get("type") == "title":
            title_arr = prop_val.get("title", [])
            return "".join(t.get("plain_text", "") for t in title_arr)
    return ""


def get_page_content_as_markdown(page_id: str) -> str:
    """Fetch a page's content and convert to markdown."""
    blocks = get_page_blocks(page_id)
    if not blocks:
        return ""
    return blocks_to_markdown(blocks)


def create_meeting_page(
    title: str,
    content_markdown: str,
    summary: str = "",
) -> str:
    """
    Create a new meeting page in the Notion DB.
    Returns the page ID.
    """
    client = _get_client()

    # Build page properties
    properties = {
        "Meeting name": {
            "title": [
                {"text": {"content": title}}
            ]
        },
    }

    # Create page
    page = client.pages.create(
        parent={"database_id": NOTION_DB_MEETINGS_ID},
        properties=properties,
    )

    page_id = page["id"]

    # Add content blocks
    blocks = markdown_to_blocks(content_markdown)
    if blocks:
        for i in range(0, len(blocks), 100):
            batch = blocks[i:i + 100]
            client.blocks.children.append(block_id=page_id, children=batch)
            time.sleep(0.1)

    # Set summary
    if summary:
        update_summary(page_id, summary)

    return page_id


def update_page_content(page_id: str, content_markdown: str):
    """Replace a page's content with new markdown."""
    blocks = markdown_to_blocks(content_markdown)
    replace_page_blocks(page_id, blocks)


def update_summary(page_id: str, summary: str):
    """Update the Summary (Comms Briefing) property."""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = _api_headers()

    if len(summary) > 2000:
        summary = summary[:1997] + "..."

    payload = {
        "properties": {
            SUMMARY_PROPERTY: {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": summary},
                    }
                ]
            }
        }
    }

    resp = http_requests.patch(url, headers=headers, json=payload)
    resp.raise_for_status()


def sync_contact_emails(page_id: str, attendee_emails: list[str]) -> int:
    """
    Sync attendee emails from Google Calendar to Notion contact pages.

    For a matched meeting page:
    1. Read the "Meeting Partner External" relation (linked contact pages)
    2. For each linked contact, check if it already has an email
    3. If not, try to match by comparing the contact name against attendee emails
    4. Only update if there's a confident single match

    Returns the number of contacts updated.
    """
    if not attendee_emails:
        return 0

    headers = _api_headers()
    updated = 0

    try:
        # Step 1: Get the meeting page to read the relation property
        url = f"https://api.notion.com/v1/pages/{page_id}"
        resp = http_requests.get(url, headers=headers)
        resp.raise_for_status()
        page = resp.json()

        # Step 2: Get related contact page IDs
        meeting_partner_prop = page.get("properties", {}).get("Meeting Partner External", {})
        relation_items = meeting_partner_prop.get("relation", [])

        if not relation_items:
            print("📇 No Meeting Partner External contacts linked, skipping email sync.")
            return 0

        # Step 3: For each contact page, check and potentially update email
        for rel in relation_items:
            contact_page_id = rel.get("id")
            if not contact_page_id:
                continue

            # Fetch the contact page
            contact_url = f"https://api.notion.com/v1/pages/{contact_page_id}"
            contact_resp = http_requests.get(contact_url, headers=headers)
            contact_resp.raise_for_status()
            contact_page = contact_resp.json()

            contact_props = contact_page.get("properties", {})

            # Get contact name
            contact_name = ""
            name_prop = contact_props.get("Contact Name", {})
            if name_prop.get("type") == "title":
                title_arr = name_prop.get("title", [])
                contact_name = "".join(t.get("plain_text", "") for t in title_arr)

            # Check if email already exists
            email_prop = contact_props.get("Email", {})
            existing_email = email_prop.get("email", None)

            if existing_email:
                print(f"📇 '{contact_name}' already has email: {existing_email}, skipping.")
                continue

            if not contact_name:
                print(f"📇 Contact has no name, skipping email match.")
                continue

            # Step 4: Try to match contact name against attendee emails
            # Strategy: check if any part of the contact name appears in the email
            contact_parts = contact_name.lower().split()
            matches = []

            for email in attendee_emails:
                email_local = email.split("@")[0].lower()
                # Check if first name, last name, or full name appears in email
                match_score = 0
                for part in contact_parts:
                    if len(part) >= 3 and part in email_local:
                        match_score += 1

                if match_score >= 1:
                    matches.append((email, match_score))

            # Only update if exactly one confident match
            if len(matches) == 1:
                matched_email = matches[0][0]
                print(f"📇 Matched '{contact_name}' → {matched_email}")

                # Update the contact's Email property
                update_payload = {
                    "properties": {
                        "Email": {
                            "email": matched_email,
                        }
                    }
                }
                update_resp = http_requests.patch(
                    contact_url, headers=headers, json=update_payload
                )
                update_resp.raise_for_status()
                updated += 1
                print(f"✅ Updated email for '{contact_name}'")

            elif len(matches) > 1:
                print(f"📇 Multiple email matches for '{contact_name}', skipping (safety).")
            else:
                print(f"📇 No email match found for '{contact_name}'.")

    except Exception as e:
        print(f"⚠ Email sync error: {e}")

    return updated

