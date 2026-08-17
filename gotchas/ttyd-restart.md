# ttyd restart races and self-killing pkill

**1. Port rebind race.** After killing ttyd, ports sometimes fail to rebind if
relaunch happens too fast. A ttyd that loses this race dies **silently** —
start.sh prints success and the tab is simply blank.

Seen twice: 7685 alone at a 1s sleep, then **three of seven at once**
(7685/7686/7688) on 2026-08-17 when the count went 5 -> 7. More ports means
more of them racing, so the failure scales with terminal count.

Fixed 2026-08-17 — don't undo either half:
- the post-pkill sleep is now **4s** (was 2s)
- start.sh **verifies every port is LISTENING** after launch and relaunches any
  that are not, up to 3 rounds, then prints `Terminals listening: N/M` and
  names anything `STILL DOWN`

So a silent partial failure is no longer possible: trust the final count line.
If it does not say `7/7`, the named tabs will be blank.

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
