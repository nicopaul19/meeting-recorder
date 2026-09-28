from __future__ import annotations
"""
Enricher — merges AI transcript with existing manual meeting notes.

Uses the configured LLM (config.LLM_MODEL) to combine:
- User's hand-written notes (primary — always preserved)
- AI-generated transcript (secondary — adds missing context)
"""
from llm import complete

PITCH_DECK_CONTEXT = """
--- TUM SOCIAL AI PITCH DECK CONTEXT TO BE EXCLUDED ---
AI FOR GOOD - THE STUDENT INITIATIVE WHERE AI MEETS SOCIAL IMPACT
Top AI Adoption Barriers for NON PROFITS: 1. COST 2. LACK OF CLARITY 3. LACK OF KNOWLEDGE
AI ACCELERATES BUSINESS BUT LEAVES SOCIAL IMPACT BEHIND!
WE SEE A GAP IN THE ECOSYSTEM
SOCIAL IMPACT, ENTREPRENEURSHIP, ARTIFICIAL INTELLIGENCE
WE BRIDGE THIS GAP BY CONNECTING TOP STUDENT TALENT, EXPERT MENTORS, SOCIAL PARTNERS
OUR CURRENT PROJECTS: IG - InitiativGruppe Interkulturelle Begegnung, DEMOCRATIZING FUNDING OPPORTUNITES, IMPROVING ACCESS TO CLEAN WATERSOURCES, RESEARCHING GENDER BIAS IN LLMS, ENABLING INCLUSION THROUGH CUSTOM WEB-LLMS.
OUR OFFER FOR YOU: EXPLORE -> CO-CREATE -> FINALIZE -> SHARE & SUPPORT (FROM IDEAS TO REAL-WORLD SOLUTIONS AT NO COST).
ABOUT SPONSORING: Provide ACCESS to TOOLS or COMPUTE RESOURCES, Offer MENTORING or host EVENTS, FUNDING.
WHAT'S IN FOR THEM: RECOGNITION, ACCESS to TOP STUDENT TALENT, VISIBILITY.
-------------------------------------------------------
"""


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
- Never INCLUDE the standard pitching or background information about "TUM Social AI" or "TUM Social AI Club". Ignore anything that sounds like the Pitch Deck context provided below.

{pitch_deck_context}

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
3. EXCLUDE the standard pitch/background about "TUM Social AI". Ignore anything that sounds like the pitch context below.
4. Use WhatsApp formatting:
   - *bold* (single asterisks, NOT double)
   - _italic_ (underscores)
   - NO markdown headers, NO ** bold **
4. Use bullet points with • or -
5. Write in a conversational but professional tone.
6. Focus on: key insights, decisions, action items, interesting facts, next steps.
7. End with next steps if applicable.
8. Keep it under 800 characters — scannable in a chat.
9. If the meeting was in German/Spanish, write the summary in English but keep company names and proper nouns in original language.

{pitch_deck_context}

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
    user_msg = f"""Meeting: {meeting_title}

--- MANUAL NOTES (PRIMARY — preserve these) ---
{manual_notes}

--- AI TRANSCRIPT (supplementary — add only important missing info) ---
{transcript_text}"""

    return complete(ENRICH_PROMPT.format(pitch_deck_context=PITCH_DECK_CONTEXT), user_msg)


def create_notes_from_transcript(
    transcript_text: str,
    meeting_title: str = "",
) -> str:
    """
    Create structured meeting notes from a transcript only (no manual notes).
    Returns markdown.
    """
    prompt = """You are a professional meeting notes editor. Convert this meeting transcript into well-structured notes.

RULES:
1. Translate to English if needed. Keep original-language key terms in parentheses: e.g. "data protection officer (Datenschutzbeauftragter)", "social impact (impacto social)"
2. Use ## (H2) for main sections, ### (H3) for sub-topics — be GENEROUS with sub-headings, prefer specific ones over broad ones
3. Use bullet lists with sub-bullets for details
4. Bold **key terms**, **names**, **companies**, **numbers**, **dates**, **percentages**
5. Group by topic, not chronologically
6. NEVER lose a data point — every name, number, and date from the transcript must appear
7. Filter out filler/greetings/chit-chat — keep only substantive content
8. EXCLUDE any standard pitching and background information about "TUM Social AI" or "TUM Social AI Club". Ignore anything that sounds like the Pitch Deck context provided below.

{pitch_deck_context}

REQUIRED SECTIONS:
- ## Key Discussion Points (with multiple ### sub-topic headings)
- ## Decisions (each decision as ### Decision: [title])
- ## Action Items (format: `- [ ] **[Person]**: [Action] *(deadline: [date])*`)
- ## Miscellaneous (side topics, observations, tangential info)

Return ONLY the markdown notes."""

    user_msg = f"Meeting: {meeting_title}\n\nTranscript:\n{transcript_text}"

    return complete(prompt.format(pitch_deck_context=PITCH_DECK_CONTEXT), user_msg)


def generate_summary(notes_markdown: str, meeting_title: str = "") -> str:
    """Generate WhatsApp-style briefing from notes."""
    return complete(
        SUMMARY_PROMPT.format(pitch_deck_context=PITCH_DECK_CONTEXT),
        f"Meeting: {meeting_title}\n\nNotes:\n{notes_markdown}",
    )
