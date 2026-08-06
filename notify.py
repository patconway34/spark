"""Summarize terminal scrollback and deliver via SMS or Alan Watts voice.

Called by app.py via subprocess under Windows Python.

Usage:
    python notify.py text <scrollback_file>     # summarize + SMS to patrick
    python notify.py play <scrollback_file>     # Alan Watts voice + Telegram
    python notify.py speak <scrollback_file>    # gTTS mp3 for in-browser playback
    python notify.py listen <scrollback_file>   # API summary of LAST response only + gTTS mp3

Auth split (intentional): `text`/`play`/`speak` go through mente = Claude
subscription. `listen` is the ONLY path that uses Patrick's own Claude API
key (see summarize_last_response) — it's his API-practice surface.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "C:/dev")

from mente.simple import ask


# --- Listen button: API path on Patrick's own Claude API key ---------------
# Spark's terminals stay on subscription; ONLY this summarizer uses the API.
# Reason: Patrick is practicing API-centered builds for FDE work — you can't
# use someone's subscription in production. Mirrors plato/faro's setup.
LISTEN_MODEL = "claude-sonnet-4-6"

LISTEN_SYSTEM = (
    "You are the voice reader for Spark. Patrick is BLIND and often driving; you "
    "read Claude's reply to him aloud.\n\n"
    "You are given exactly one clean turn, already extracted for you:\n"
    "  - After '[PATRICK ASKED]' is Patrick's message.\n"
    "  - After '[CLAUDE REPLIED]' is Claude's reply.\n"
    "You do NOT need to hunt for it — this is already the right, single turn.\n\n"
    "Read it like this:\n"
    "1. Open with ONE short, natural line naming what he asked, e.g. 'You asked "
    "about the voice log.' Keep it brief.\n"
    "2. Then deliver Claude's reply faithfully and fully — do not summarize away "
    "detail, but say it as natural speech.\n"
    "3. For anything visual — a table, code block, list, file diff, or error — "
    "say what it is and read its key contents in plain spoken words, e.g. 'There's "
    "a table with three rows: ...' or 'a short code block that does ...'. Never "
    "read raw symbols.\n"
    "4. Output CLEAN SPOKEN text only. Strip ALL markdown and symbols — no "
    "asterisks, backticks, pound signs, bullets, or dashes read aloud; never read "
    "dots or box characters. It goes straight to text-to-speech. Natural and clear."
)


# X button: a SHORT spoken summary (1-3 sentences) of Claude's reply — the same
# gist the Text button sends, but voiced instead of texted.
VSUMMARY_SYSTEM = (
    "You are the voice reader for Spark. Patrick is BLIND and often driving. "
    "You are given one clean turn: Patrick's message after '[PATRICK ASKED]' and "
    "Claude's reply after '[CLAUDE REPLIED]'. Read Patrick a SHORT spoken summary "
    "of Claude's reply — the gist and any key result, as a complete, self-contained "
    "thought. This is a real summary, NOT the reply cut off: 1 to 3 sentences, "
    "never trailing off mid-thought. Natural spoken English — no markdown, "
    "asterisks, backticks, or bullets; never read dots or symbols. For a "
    "table/code/diff, say what it is in a few words. It goes straight to "
    "text-to-speech."
)


def _api_summarize(text, system, max_tokens):
    """Summarize `text` under `system` via Patrick's own Claude API key.

    Deliberately uses the Anthropic API (not mente/subscription): the Listen
    button is Patrick's API-practice surface. Loads the key from C:/dev/.env,
    same as plato and faro.
    """
    import anthropic
    from dotenv import load_dotenv
    load_dotenv("C:/dev/.env")  # provides ANTHROPIC_API_KEY

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=LISTEN_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": text}],
    )
    return "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()


def summarize_last_response(text):
    """Full spoken read of the last turn (Y button)."""
    return _api_summarize(text, LISTEN_SYSTEM, 900)


def summarize_short_voice(text):
    """1-3 sentence spoken summary of the last turn (X button)."""
    return _api_summarize(text, VSUMMARY_SYSTEM, 400)


def summarize_for_text(text):
    return ask(
        text,
        system=(
            "You are given one clean turn: Patrick's message after '[PATRICK ASKED]' "
            "and Claude's reply after '[CLAUDE REPLIED]'. Write Patrick a text message "
            "that SUMMARIZES Claude's reply — the gist and any key results, as a "
            "complete, self-contained thought. This is a real summary, NOT the reply "
            "truncated: never cut off mid-sentence, never trail off. If the reply is "
            "long, compress it into a shorter whole; if it's already short, lightly "
            "clean it. Plain SMS text: NO markdown, asterisks, backticks, or bullets. "
            "For a table/code/diff, say what it is in words. Aim for 1-3 sentences, "
            "roughly 320 characters or less — but a finished message always wins over "
            "hitting a length."
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


def _log_listen(input_text, summary):
    """Append what the Listen button fed the LLM and what it got back, so
    Patrick can engineer the prompt. One human-readable block per press."""
    try:
        log_path = Path("C:/dev/spark/listen_log.txt")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        block = (
            "\n" + "=" * 70 + "\n"
            f"TIME: {ts}\n"
            f"MODEL: {LISTEN_MODEL}\n"
            + "-" * 70 + "\n"
            "SENT TO LLM (scrollback fed in with LISTEN_SYSTEM prompt):\n"
            + "-" * 70 + "\n"
            + input_text.rstrip() + "\n"
            + "-" * 70 + "\n"
            "LLM REPLY (spoken back):\n"
            + "-" * 70 + "\n"
            + (summary or "").rstrip() + "\n"
        )
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(block)
    except Exception as e:
        print(f"LISTEN_LOG_FAILED ({e})")


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

    elif mode == "speak":
        # Free/fast path: summarize + gTTS mp3, returned to the browser.
        # Prints "MP3:<path>" on the last line for app.py to pick up.
        try:
            summary = summarize_for_voice(text)
        except Exception as e:
            summary = "Here's what happened in the terminal. " + text.strip()[-800:]
            print(f"SUMMARIZE_FAILED ({e}), using raw")
        from gtts import gTTS
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        mp3_path = Path(f"C:/dev/spark/_listen_{ts}.mp3")
        gTTS(text=summary, lang="en", slow=False).save(str(mp3_path))
        print(f"MP3:{mp3_path}")

    elif mode == "listen":
        # Listen button: API summary of ONLY what's below Patrick's last input,
        # cleaned of terminal chrome, then gTTS mp3 for in-browser playback.
        # Prints "MP3:<path>" on the last line for app.py to pick up.
        try:
            summary = summarize_last_response(text)
            if not summary:
                summary = "Nothing new has happened in the terminal yet."
        except Exception as e:
            # Don't read raw terminal noise aloud — keep it clean.
            summary = "Sorry, I couldn't summarize the terminal right now. Please try again."
            print(f"SUMMARIZE_FAILED ({e})")
        _log_listen(text, summary)
        from gtts import gTTS
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        mp3_path = Path(f"C:/dev/spark/_listen_{ts}.mp3")
        gTTS(text=summary, lang="en", slow=False).save(str(mp3_path))
        print(f"MP3:{mp3_path}")

    elif mode == "vsummary":
        # X button: 1-3 sentence SPOKEN summary (same gist as the Text button),
        # then gTTS mp3 for in-browser playback. Prints "MP3:<path>".
        try:
            summary = summarize_short_voice(text)
            if not summary:
                summary = "Nothing new has happened yet."
        except Exception as e:
            summary = "Sorry, I couldn't summarize right now. Please try again."
            print(f"SUMMARIZE_FAILED ({e})")
        _log_listen(text, summary)
        from gtts import gTTS
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        mp3_path = Path(f"C:/dev/spark/_listen_{ts}.mp3")
        gTTS(text=summary, lang="en", slow=False).save(str(mp3_path))
        print(f"MP3:{mp3_path}")


if __name__ == "__main__":
    main()
