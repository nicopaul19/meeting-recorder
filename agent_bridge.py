from __future__ import annotations
"""
Agent Bridge — triggers the meeting-notes-agent to clean up a Notion page
after the meeting recorder has pushed transcript-based notes to it.

This module calls the agent's processing pipeline directly, reusing its
LLM cleanup and summary logic. It does NOT replace the existing content
but instead enhances it.
"""
import sys
import importlib.util
from pathlib import Path


def _get_agent_dir():
    """Lazily get MEETING_AGENT_DIR from our config to avoid import order issues."""
    import config as our_config
    return our_config.MEETING_AGENT_DIR


def _get_notion_token():
    """Lazily get NOTION_TOKEN from our config."""
    import config as our_config
    return our_config.NOTION_TOKEN


def _load_agent_module(module_name: str):
    """Load a module from the meeting-notes-agent directory by file path."""
    agent_dir = _get_agent_dir()
    module_path = agent_dir / f"{module_name}.py"
    if not module_path.exists():
        raise FileNotFoundError(f"Agent module not found: {module_path}")

    spec = importlib.util.spec_from_file_location(
        f"agent_{module_name}",
        str(module_path),
    )
    mod = importlib.util.module_from_spec(spec)

    # For modules that import from "config", temporarily inject the agent's config
    if module_name in ("notion_api", "llm_processor", "agent"):
        agent_config = _load_agent_module("config")
        old_config = sys.modules.get("config")
        sys.modules["config"] = agent_config

    spec.loader.exec_module(mod)

    # Restore our config
    if module_name in ("notion_api", "llm_processor", "agent"):
        import config as our_config
        sys.modules["config"] = our_config

    return mod


def trigger_agent_on_page(page_id: str) -> bool:
    """
    Trigger the meeting-notes-agent's process_page on a specific Notion page.
    
    This runs the agent's full pipeline:
    1. Fetch existing page content
    2. LLM cleanup (translate, restructure, add headings)
    3. Generate WhatsApp-style summary
    4. Write back to Notion
    
    Returns True if successful, False otherwise.
    """
    try:
        # Load the agent modules
        agent_notion_api = _load_agent_module("notion_api")
        agent_llm = _load_agent_module("llm_processor")

        # Step 1: Fetch the page object
        from notion_client import Client
        notion_token = _get_notion_token()
        client = Client(auth=notion_token)
        page = client.pages.retrieve(page_id=page_id)

        title = agent_notion_api.get_page_title(page)
        print(f"🤖 Agent Bridge: Processing '{title}'...")

        # Step 2: Fetch existing blocks
        blocks = agent_notion_api.get_page_blocks(page_id)
        if not blocks:
            print("⚠ Agent Bridge: Page has no content, skipping.")
            return False

        # Step 3: Convert to markdown
        markdown = agent_notion_api.blocks_to_markdown(blocks)
        if not markdown.strip():
            print("⚠ Agent Bridge: Page content is empty, skipping.")
            return False

        # Step 4: Backup original content
        agent_notion_api.backup_blocks(page_id, title, blocks)

        # Step 5: Detect template structure
        template_hint = agent_llm.detect_template_structure(markdown)

        # Step 6: LLM cleanup (translate, restructure, add granular headings)
        cleaned_markdown = agent_llm.cleanup_notes(markdown, template_hint)

        # Step 7: Generate WhatsApp-style summary
        summary = agent_llm.generate_summary(cleaned_markdown)

        # Step 8: Convert cleaned markdown back to Notion blocks
        new_blocks = agent_notion_api.markdown_to_blocks(cleaned_markdown)

        # Step 9: Replace page content with cleaned version
        agent_notion_api.replace_page_blocks(page_id, new_blocks)

        # Step 10: Update summary property
        agent_notion_api.update_summary_property(page_id, summary)

        # Step 11: Mark as processed so the daily agent run doesn't redo it
        agent_notion_api.mark_page_as_processed(page_id)

        print(f"✅ Agent Bridge: Successfully processed '{title}'")
        return True

    except Exception as e:
        print(f"❌ Agent Bridge: Error processing page {page_id}: {e}")
        import traceback
        traceback.print_exc()
        return False
