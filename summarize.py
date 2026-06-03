"""Lightweight summarizer for Spark. Uses Claude Agent SDK (subscription auth).

Falls back gracefully — if SDK not installed or auth fails, returns original text.
"""

from __future__ import annotations

import asyncio
import os

# Strip API keys — subscription auth only
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

DEFAULT_MODEL = os.getenv("SPARK_MODEL", "claude-sonnet-4-6")


def ask(prompt: str, system: str = "") -> str:
    """One-shot Claude call. Returns response text."""
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, TextBlock

        async def _run():
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            options = ClaudeAgentOptions(
                model=DEFAULT_MODEL,
                system_prompt=system,
                allowed_tools=[],
                max_turns=1,
                permission_mode="bypassPermissions",
            )
            final = ""
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            final = block.text
            return final

        return asyncio.run(_run())
    except Exception:
        return ""
