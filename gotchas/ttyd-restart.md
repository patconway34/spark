# ttyd restart races and self-killing pkill

**1. Port rebind race.** After killing ttyd, ports (seen: 7685) sometimes fail
to rebind if relaunch happens too fast. `start.sh` sleeps ~2-4s after the
pkill for this reason — don't remove the sleep. After any restart, verify all
four ports are LISTENING (7682-7685); a silent single-port failure looks like
"one tab broken".

**2. pkill can kill its own caller.** Running
`wsl bash -c 'pkill -f "ttyd.*-p 768" && ...'` matches the *bash -c command
line itself* (it contains the pattern) and pkill kills its own shell — script
dies mid-run with exit 15/143. **Always restart via the script file:**
`wsl -e bash /mnt/c/dev/spark/start.sh` — the file path doesn't match the
pattern.

**3. tmux sessions survive, ttyd doesn't need to.** start.sh only creates tmux
sessions if missing (`tmux has-session`), so restarting ttyd never kills
running Claude conversations. Restarting ttyd is always safe; killing tmux
sessions is what loses work.

**4. ttyd is read-only (no `-W`) on purpose** (2026-07-18): a writable ttyd
auto-focuses its xterm textarea on load, which pops the mobile soft keyboard on
every tab switch. All input reaches tmux via the backend
(`/api/key`, `/api/paste-text`), never through ttyd. Don't add `-W` back.
