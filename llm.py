from __future__ import annotations
"""
Shared LLM access — one place for the model, key and reasoning effort.

All text generation (notes, briefings, synthesis) goes through the OpenAI
Responses API with the model from config.LLM_MODEL (default gpt-6-sol,
reasoning effort high). Transcription stays on the speech models.
"""
from typing import Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from config import OPENAI_API_KEY, LLM_MODEL, LLM_REASONING_EFFORT

_client = None
T = TypeVar("T", bound=BaseModel)


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY, timeout=600, max_retries=3)
    return _client


def _strip_fences(text: str) -> str:
    text = text.strip()
    for fence in ("```markdown", "```"):
        if text.startswith(fence):
            text = text[len(fence):].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return text


def complete(system: str, user: str) -> str:
    """Free-text completion. Returns the model's text output."""
    response = get_client().responses.create(
        model=LLM_MODEL,
        reasoning={"effort": LLM_REASONING_EFFORT},
        instructions=system,
        input=user,
    )
    return _strip_fences(response.output_text)


def parse(system: str, user: str, schema: Type[T]) -> T:
    """Structured completion validated against a Pydantic schema."""
    response = get_client().responses.parse(
        model=LLM_MODEL,
        reasoning={"effort": LLM_REASONING_EFFORT},
        instructions=system,
        input=user,
        text_format=schema,
    )
    parsed = response.output_parsed
    if parsed is None:
        raise RuntimeError("The model returned no structured output (possibly a refusal).")
    return parsed
