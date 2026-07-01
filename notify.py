"""Summarize terminal scrollback and deliver via SMS or Alan Watts voice.

Called by app.py via subprocess under Windows Python.

Usage:
    python notify.py text <scrollback_file>    # summarize + SMS to patrick
    python notify.py play <scrollback_file>    # Alan Watts voice + Telegram
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "C:/dev")

from mente.simple import ask


def summarize_for_text(text):
    return ask(
        text,
        system=(
            "Summarize this Claude Code terminal output in 2-3 short sentences. "
            "What happened and any key results. Keep it under 300 characters for SMS."
        ),
        model="claude-haiku-4-5-20251001",
    )


def summarize_for_voice(text):
    personality = Path("C:/dev/flint/personality.md").read_text(encoding="utf-8")
    return ask(
        text,
        system=(
            "You are summarizing a Claude Code terminal session for the developer. "
            "Deliver the summary as a short spoken piece in Alan Watts' voice. "
            "Keep it under 100 words since this becomes audio. "
            "Be warm, insightful, and conversational.\n\n"
            f"{personality}"
        ),
        model="claude-haiku-4-5-20251001",
    )


def main():
    mode = sys.argv[1]
    input_file = Path(sys.argv[2])
    text = input_file.read_text(encoding="utf-8")

    if mode == "text":
        from buzz import send
        try:
            summary = summarize_for_text(text)
        except Exception as e:
            # AI summary failed — send raw last 280 chars instead of nothing
            summary = text.strip()[-280:]
            print(f"SUMMARIZE_FAILED ({e}), sending raw")
        send(summary, to="patrick")
        print(summary)

    elif mode == "play":
        try:
            summary = summarize_for_voice(text)
        except Exception as e:
            # AI summary failed — use raw last 800 chars as spoken text
            summary = "Here's what happened in the terminal. " + text.strip()[-800:]
            print(f"SUMMARIZE_FAILED ({e}), using raw")
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        story_path = Path(f"C:/dev/yarn/stories/{ts}_spark.txt")
        story_path.parent.mkdir(parents=True, exist_ok=True)
        story_path.write_text(summary, encoding="utf-8")

        sys.path.insert(0, "C:/dev/yarn")
        from speak import speak_and_send
        speak_and_send(story_path, persona="flint")
        print(f"Audio sent: {story_path.name}")


if __name__ == "__main__":
    main()
