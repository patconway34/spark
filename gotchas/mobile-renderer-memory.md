# 4 same-origin terminal iframes can crash mobile Chrome to white

**Symptom:** Page loads (JS runs, layout computes, beacons fire) but the phone
screen shows white — Chrome's renderer stops painting. Thumbnail in the tab
switcher may still look correct.

**Why:** since the same-origin migration, all 4 terminal iframes + the main
page share **one renderer process** (Chrome site isolation groups by origin).
Four xterm.js instances, each with a big scrollback buffer, can exceed the
phone's per-process memory budget.

**Fixes in place (keep both):**
1. **Lazy loading** (chat.html): only the ACTIVE session's iframe has a src;
   the other three are `about:blank` and load on tab switch
   (`loadActiveTerminal()` / `unloadInactiveTerminals()`). Never "preload all
   terminals" — that's the regression to avoid.
2. **ttyd scrollback=10000** (start.sh), down from 100000. The tmux-side
   scrollback is mostly dead weight anyway because Claude runs alt-screen
   (see scroll-alt-screen.md).

**Rule:** any change that loads multiple terminal iframes simultaneously must
be tested on the phone, not just desktop. Desktop has headroom; the phone does
not.
