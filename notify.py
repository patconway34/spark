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
    )


def summarize_for_voice(text):
    personality = Path("C:/dev/flint/personality.md").read_text(encoding="utf-8")
    return ask(
        text,
        system=(
            "You are summarizing a Claude Code terminal session for the developer. "
            "Deliver the summary as a short spoken piece in Alan Watts' voice. "
            "Keep it under 400 words since this becomes audio. "
            "Be warm, insightful, and conversational.\n\n"
            f"{personality}"
        ),
    )


def main():
    mode = sys.argv[1]
    input_file = Path(sys.argv[2])
    text = input_file.read_text(encoding="utf-8")

    if mode == "text":
        summary = summarize_for_text(text)
        from buzz import send
        send(summary, to="patrick")
        print(summary)

    elif mode == "play":
        summary = summarize_for_voice(text)
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
