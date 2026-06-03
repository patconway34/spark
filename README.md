# Spark

Voice-controlled mobile terminal for Claude Code. Talk to your AI from your phone, switch between sessions, and control everything hands-free.

## What it does

- Voice input via Groq Whisper (say "go ahead" to send, "send key enter" for keys)
- Text-to-speech readback when Claude finishes responding
- 6 terminal sessions with instant switching (colored flame buttons)
- Works from any browser, designed for mobile

## Requirements

- **WSL/Linux** with tmux installed
- **Python 3.10+**
- **Claude Code** — install from https://claude.ai/claude-code
- **ttyd** — web terminal (`apt install ttyd` or https://github.com/tsl0922/ttyd)
- **Groq API key** — free at https://console.groq.com

## Setup

### 1. Install Python packages

```bash
pip install -r requirements.txt
```

### 2. Log into Claude Code

```bash
claude /login
```

This uses your Max subscription. No API key needed.

### 3. Set up your Groq key

Create `spark/.env`:

```
GROQ_API_KEY=your_key_here
```

### 4. Create tmux sessions

```bash
tmux new-session -d -s claude
```

Add more if you want multiple sessions:

```bash
tmux new-session -d -s claude2
tmux new-session -d -s claude3
```

### 5. Start ttyd for each session

```bash
ttyd -W -p 7682 tmux attach -t claude &
ttyd -W -p 7683 tmux attach -t claude2 &
ttyd -W -p 7684 tmux attach -t claude3 &
```

### 6. Start Claude Code in each session

```bash
tmux send-keys -t claude 'claude' Enter
tmux send-keys -t claude2 'claude' Enter
tmux send-keys -t claude3 'claude' Enter
```

### 7. Configure sessions

Edit the `DEFAULT_SESSIONS` list in `app.py` to match your tmux sessions, ttyd ports, and terminal URLs.

### 8. Start Spark

```bash
python app.py
```

Open `http://localhost:5023` on your phone (same network) or set up a Cloudflare tunnel for remote access.

## Voice commands

| Say | Action |
|-----|--------|
| [message] ... go ahead | Transcribe and send to Claude |
| send key enter | Send Enter key |
| send key tab | Send Tab key |
| send key yes / no | Send y/n + Enter |
| send key escape | Send Escape |
| send key up / down / left / right | Arrow keys |
| send key control c / z / v | Ctrl+C / Ctrl+Z / Ctrl+V |
| send key main menu | Go to lobby |
| send key clear | Clear current recording |
| send key [session name] | Switch to another session |

## Remote access (optional)

To access Spark from outside your network, set up a Cloudflare tunnel:

1. Install cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
2. Create a tunnel and add routes for Spark (port 5023) and each ttyd instance (ports 7682+)
3. Point your phone browser to your tunnel URL

## Files

| File | Purpose |
|------|---------|
| `app.py` | Flask server, voice routing, screen capture |
| `transcribe.py` | Groq Whisper speech-to-text |
| `summarize.py` | Claude-powered text summarization |
| `templates/chat.html` | Terminal UI with voice controls |
| `templates/lobby.html` | Session picker (optional) |
| `.env` | Your Groq API key |
| `transcripts/` | Chat transcripts per session |
| `spark.log` | Debug log |
