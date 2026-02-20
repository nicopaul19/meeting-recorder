from __future__ import annotations
"""
Enricher — merges AI transcript with existing manual meeting notes.

Uses GPT-4o to intelligently combine:
- User's hand-written notes (primary — always preserved)
- AI-generated transcript (secondary — adds missing context)
"""
from openai import OpenAI
from config import OPENAI_API_KEY

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


ENRICH_PROMPT = """You are a meeting notes editor. You have TWO sources:

1. **MANUAL NOTES** — hand-written by the user during the meeting. These are the PRIMARY source.

2. **AI TRANSCRIPT** — an automated transcript of the same meeting. This is supplementary.

⚠️ TEMPLATE PRESERVATION (highest priority):
If the manual notes already have a clear structure, framework, template, agenda, or headings (e.g. SPICED, All-hands, standup, OKR review, sprint retro, etc.):
- Do NOT reorganize, rename, reorder, merge, or remove ANY existing headings or sections
- Do NOT impose a different structure on top of the template
- INSERT new points from the transcript INTO the matching existing sections
- Overflow goes BELOW the template under "## Additional Context (from transcript)"

WHAT YOU MUST NEVER DO:
- Never DELETE any of the user's original content — every word they wrote must remain
- Never REPLACE the user's sentences with your own rephrasing — keep their voice
- Never REMOVE sections, headings, or bullet points the user created

WHAT YOU ARE ENCOURAGED TO DO:
- STRUCTURE the user's notes better: break long bullets into sub-bullets, split wall-of-text into clear bullet points
- ADD sub-headings (###) within sections to make content scannable
- BOLD key terms, names, numbers, dates, companies for easy scanning
- FIX typos and grammar mistakes
- TRANSLATE to English if notes are in German/Spanish (keep key terms in parentheses: "data protection officer (Datenschutzbeauftragter)")
- ADD important information the user missed — names, numbers, dates, decisions, action items from the transcript
- ADD context that makes existing points clearer — as sub-bullets under the relevant point
- FORMAT action items as: `- [ ] **[Person]**: [Action] *(deadline: [date])*`

Think of it this way: you are the user's assistant who takes their quick notes and makes them BETTER — more detailed, better formatted, easier to scan — without ever losing anything they wrote.

SECTIONS TO ADD AT THE BOTTOM (only if not already part of the template):
- "## Additional Context (from transcript)" — for important transcript-only points that don't fit the existing structure
- "## Miscellaneous" — for anything that doesn't fit elsewhere

Return ONLY the enriched markdown. No explanations."""


SUMMARY_PROMPT = """You write short WhatsApp-style briefing messages about meetings for a team chat.

RULES:
1. Start with a casual opener like: "Hey guys, just spoke to [person] from [company/org]. A couple of interesting points:"
   - Infer the person and company from the notes. If multiple external participants, mention the main one.
   - If you can't identify a specific person, adapt: "Hey guys, quick update from the [meeting name] meeting:"
2. Keep it SHORT and punchy — max 5-7 bullet points with the most important takeaways only.
3. Use WhatsApp formatting:
   - *bold* (single asterisks, NOT double)
   - _italic_ (underscores)
   - NO markdown headers, NO ** bold **
4. Use bullet points with • or -
5. Write in a conversational but professional tone.
6. Focus on: key insights, decisions, action items, interesting facts, next steps.
7. End with next steps if applicable.
8. Keep it under 800 characters — scannable in a chat.
9. If the meeting was in German/Spanish, write the summary in English but keep company names and proper nouns in original language.

Return ONLY the WhatsApp message text."""


def enrich_notes(
    manual_notes: str,
    transcript_text: str,
    meeting_title: str = "",
) -> str:
    """
    Merge manual notes with AI transcript.
    Returns enriched markdown.
    """
    client = _get_client()

    user_msg = f"""Meeting: {meeting_title}

--- MANUAL NOTES (PRIMARY — preserve these) ---
{manual_notes}

--- AI TRANSCRIPT (supplementary — add only important missing info) ---
{transcript_text}"""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": ENRICH_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.3,
        max_tokens=8000,
    )

    enriched = response.choices[0].message.content.strip()

    # Remove code fences
    if enriched.startswith("```markdown"):
        enriched = enriched[len("```markdown"):].strip()
    if enriched.startswith("```"):
        enriched = enriched[3:].strip()
    if enriched.endswith("```"):
        enriched = enriched[:-3].strip()

    return enriched


def create_notes_from_transcript(
    transcript_text: str,
    meeting_title: str = "",
) -> str:
    """
    Create structured meeting notes from a transcript only (no manual notes).
    Returns markdown.
    """
    client = _get_client()

    prompt = """You are a professional meeting notes editor. Convert this meeting transcript into well-structured notes.

RULES:
1. Translate to English if needed. Keep original-language key terms in parentheses: e.g. "data protection officer (Datenschutzbeauftragter)", "social impact (impacto social)"
2. Use ## (H2) for main sections, ### (H3) for sub-topics — be GENEROUS with sub-headings, prefer specific ones over broad ones
3. Use bullet lists with sub-bullets for details
4. Bold **key terms**, **names**, **companies**, **numbers**, **dates**, **percentages**
5. Group by topic, not chronologically
6. NEVER lose a data point — every name, number, and date from the transcript must appear
7. Filter out filler/greetings/chit-chat — keep only substantive content

REQUIRED SECTIONS:
- ## Key Discussion Points (with multiple ### sub-topic headings)
- ## Decisions (each decision as ### Decision: [title])
- ## Action Items (format: `- [ ] **[Person]**: [Action] *(deadline: [date])*`)
- ## Miscellaneous (side topics, observations, tangential info)

Return ONLY the markdown notes."""

    user_msg = f"Meeting: {meeting_title}\n\nTranscript:\n{transcript_text}"

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.3,
        max_tokens=8000,
    )

    notes = response.choices[0].message.content.strip()

    if notes.startswith("```markdown"):
        notes = notes[len("```markdown"):].strip()
    if notes.startswith("```"):
        notes = notes[3:].strip()
    if notes.endswith("```"):
        notes = notes[:-3].strip()

    return notes


def generate_summary(notes_markdown: str, meeting_title: str = "") -> str:
    """Generate WhatsApp-style briefing from notes."""
    client = _get_client()

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": f"Meeting: {meeting_title}\n\nNotes:\n{notes_markdown}"},
        ],
        temperature=0.3,
        max_tokens=2000,
    )

    return response.choices[0].message.content.strip()
