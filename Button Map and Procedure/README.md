# Spark — 8BitDo Micro Button Map & Procedure

The controller is an **8BitDo Micro** on **Profile 2** (keyboard mode). Each physical
button is set, in the **8BitDo Ultimate** app, to send a keyboard key (an F-key or a
Shift+F-key). Spark's page (`templates/chat.html`) listens for those keys in its
`keydown` handler and runs the matching action.

**`profile_2_map.jpg` is STALE (confirmed by Patrick 2026-08-07).** It predates
the shift-layer sync — it shows the face buttons on their old duplicate/dead
keys, but the shift-layer plan WAS completed on the controller and all buttons
work. The truth is the `keydown` handler in `templates/chat.html`: every button
maps 1:1 to a code line. Rebuild the map from the code, not from screenshots.

---

## The one hard rule: Android only delivers F1–F12

Mobile Chrome on Android **only passes F1 through F12 to the web page.** Anything the
controller sends as **F13 or higher arrives as "Unidentified"** and never reaches
Spark. So a button is only usable if it sends **F1–F12** (or Shift+F1–F12).

---

## Current map (live, 2026-09-29) — thumb on the face buttons

Remapped 2026-09-29. The four things done constantly — mic, send, and walking
left/right through terminals — moved off the d-pad onto **A / Y / X / B**,
where the thumb already rests. The d-pad took over the read-back jobs, which
are occasional. **The controller was not touched**: every button still sends
the key it always sent, only the action behind it changed.

| Button | Sends | Action | | Button | Sends | Action |
|---|---|---|---|---|---|---|
| A | Shift+F2 | Mic on/off | | ← | F1 | Text summary (SMS) |
| Y | Shift+F8 | Enter / send | | → | F2 | Voice summary |
| X | Shift+F4 | Terminal ← | | ↑ | F9 | Play / Pause read-back |
| B | Shift+F3 | Terminal → | | ↓ | F7 | **dead switch — unbound** |
| R | Shift+F5 | Escape | | L | F4 | Tab |
| R2 | Shift+F7 | Clear screen (C-l) | | L2 | F8 | Big/Normal text toggle |
| ★ | Shift+F1 | /new (fresh conversation) | | + | F10 | Page up |
| ❖ | F6 | unbound — leave (power) | | − | F12 | Page down |

### Notes
- **Clear is the SCREEN, not the conversation.** R2 sends `C-l`: wipes the
  visible scrollback, leaves the session intact. `/clear` and `/compact` stay
  in the hamburger, where they need a deliberate tap — throwing away context
  is too destructive for a thumb button.
- **`C-l` had to be added to `ALLOWED_KEYS` in app.py.** The backend rejects
  any key not on that list, so a new key binding is two edits, not one: the
  `keydown` handler AND the allow-list. A missing allow-list entry looks
  exactly like a dead button.
- **↓ (F7) is dead hardware** — a broken micro switch, sends no event at all.
  Nothing can live there. The second summary went to ← instead.
- **What lost its button:** Listen (fresh read 🎧) and Copy screen — both still
  in the hamburger. Play/Pause on ↑ covers playback.
- **The 8BitDo app stays out of the loop.** Future changes are code-only edits
  in chat.html — never re-open the app unless a physical button needs a
  brand-new key.
- **Design rule that decided the cuts:** buttons are for what the mic can't do
  (Tab, playback control, terminal switching). Anything speakable loses its
  button first.
- **Avoid ❖ (F6):** it is also the power button — holding it to power down
  spams F6. Leave it unbound.

---

## Procedure — how a button press becomes an action

1. Press a button → the 8BitDo Micro sends its assigned key (per `profile_2_map.jpg`).
2. Android Chrome delivers it to Spark **only if it is F1–F12**.
3. Spark's `keydown` handler in `templates/chat.html` matches the key and runs the
   action (e.g. `if (e.key === 'F1') { toggleMic(); }`). Shift combos are matched
   in the "shifted layer" block (e.g. `Shift+F1` → `/new`).

## Procedure — to add or change a button

1. **Pick a usable key.** It must be F1–F12 (or Shift+F1–F12) and not already used
   above. Right now the only free plain key is **F6 (the ❖ button)**. To use a
   different physical button, first reassign it to a free key in the 8BitDo app.
2. **(If reassigning) 8BitDo Ultimate app:** Buttons tab → tap the button → set its
   key → **Sync to device**. Update `profile_2_map.jpg` so this doc stays true.
3. **Wire it in code:** in `templates/chat.html`, in the `keydown` handler, add a
   line next to the other F-key checks, e.g.:
   `if (e.key === 'F6') { e.preventDefault(); listenMe(); return; }`
4. **Restart Spark** (via Radar, port 5023) and hard-refresh the phone.

## Procedure — to discover what a button actually sends (key debug mode)

**Always probe before believing this document.** The map above is a photo of the
past; the controller is the truth, and the two drift (see below).

Turn it on:

- **Phone:** create the file `C:\dev\spark\.keydebug`, then reload Spark. No
  restart needed — the flag is read on every page load. The phone opens Spark
  from a home-screen PWA shortcut with a fixed URL, which is why a `?keys=1`
  query param does **not** work there.
- **Desktop:** load Spark with `?keys=1`.

You'll see `KEY DEBUG ON` in the status line. Every press is echoed on screen
and logged to `spark.log` as `KEY: F9 [F9 #120]`, and **nothing else fires** —
no mic, no Enter, no session switch. Delete `.keydebug` (or reload without the
param) to turn it off.

A button that sends **nothing at all** — no key, not even `Unidentified` — is
not reaching the browser. That means the device is sending a key Android eats
outright (media/volume keys are swallowed before any web page sees them), or
the button is unassigned or failing. It is never a Spark-side problem: the
probe runs before any Spark logic and before any network call.

---

## Probe results — 2026-08-06 (SUPERSEDED — kept as history)

**Patrick corrected this on 2026-08-07:** the shift layer IS on the controller
and all buttons work — this probe run's shift-layer conclusions (and the
★=Shift+F5 reading) were wrong, most likely run against the wrong profile.
Only one finding stands: **↓ is a broken micro switch.** Original notes below.

Measured, not assumed:

| Button | Actually sends | Code does | Verdict |
|---|---|---|---|
| ← | `F1` | mic | ✅ correct |
| → | `F2` | Enter | ✅ correct |
| L | `F4` | Escape | ✅ correct |
| L2 | `F8` | Backspace | ✅ correct |
| ↑ | `F9` | next session | ✅ correct |
| **↓** | **nothing** | prev session | ❌ **not reaching the browser** |
| **★** | **`Shift+F5`** | `Shift+F5` = Tab | ⚠️ doc claimed `Shift+F1` = `/new` |

**↓ sends no event at all.** Turned out to be a broken micro switch (hardware,
2026-08-07) — this is what triggered the 180° flip in the map above.

**The ⏳ rows above were never done.** Per `profile_2_map.jpg`, those buttons
still send their old keys, and they are duplicates or dead:

| Button | Doc wanted | Really sends | Effect |
|---|---|---|---|
| Y | `Shift+F8` Listen | `F8` | Backspace (same as L2) |
| B | `Shift+F3` Play/Pause | `F7` | prev session (same as ↓) |
| X | `Shift+F4` Summary | `F13` | dead — Android drops >F12 |
| R | `Shift+F5` Tab | `F15` | dead |
| R2 | `Shift+F7` Copy screen | `F16` | dead |

So the audio buttons have never worked — not a regression. Either finish the
8BitDo app pass, or bend the code in `chat.html` to what the buttons already
send.

---

*Constraint to remember: the physical layout you hold is rotated ~90° from the app's
picture, so "the button that feels like Up" may not be the one labeled ↑. Trust the
key each button **sends** (the F-number), not its printed position.*
