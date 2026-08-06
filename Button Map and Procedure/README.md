# Spark — 8BitDo Micro Button Map & Procedure

The controller is an **8BitDo Micro** on **Profile 2** (keyboard mode). Each physical
button is set, in the **8BitDo Ultimate** app, to send a keyboard key (an F-key or a
Shift+F-key). Spark's page (`templates/chat.html`) listens for those keys in its
`keydown` handler and runs the matching action.

Reference image: `profile_2_map.jpg` (source of truth for what each button sends).

---

## The one hard rule: Android only delivers F1–F12

Mobile Chrome on Android **only passes F1 through F12 to the web page.** Anything the
controller sends as **F13 or higher arrives as "Unidentified"** and never reaches
Spark. So a button is only usable if it sends **F1–F12** (or Shift+F1–F12).

---

## Current map (every button unique, 2026-07-23)

Base layer = plain F-keys (the 10 buttons that already worked, unchanged).
Shift layer = the 6 buttons that used to be duplicates or dead, now each on a
clean `Shift+F-key` so every physical button has its own action.

| Physical button | Sends | Spark action (chat.html) | Usable? |
|---|---|---|---|
| D-pad ← (Left)  | F1        | Mic toggle            | ✅ |
| D-pad → (Right) | F2        | Enter                 | ✅ |
| L               | F4        | Escape / clear        | ✅ |
| D-pad ↓ (Down)  | F7        | Previous session      | ✅ |
| L2              | F8        | Backspace             | ✅ |
| D-pad ↑ (Up)    | F9        | Next session          | ✅ |
| +               | F10       | Scroll terminal down  | ✅ |
| −               | F12       | Scroll terminal up    | ✅ |
| ★ (star)        | Shift+F1  | /new                  | ✅ |
| **Y**           | **Shift+F8** | **Listen (fresh read-back)** | ✅ |
| **A**           | **Shift+F2** | **Text me (SMS summary)** | ⏳ reassign in app |
| **B**           | **Shift+F3** | **Play / Pause read-back** | ⏳ reassign in app |
| **X**           | **Shift+F4** | **Summary read (1-3 sentences, voice)** | ⏳ reassign in app |
| **R**           | **Shift+F5** | **Tab** | ⏳ reassign in app |
| **R2**          | **Shift+F7** | **Copy screen** | ⏳ reassign in app |
| ❖ (checkered)   | F6 (leave)   | — unbound (F6 = controller power-off) | ⚠️ avoid |

### Notes
- **⏳ = code is wired and waiting; the button still needs to be reassigned once in
  the 8BitDo Ultimate app** to the "Sends" key above (A,B,X,R,R2). After that one
  pass, every button is unique and future changes are code-only.
- **Y is now a FRESH read-back every press** (changed 2026-07-23) — it no longer
  toggles play/pause. Play/pause moved to its own button (B).
- **Shift layer is proven clean:** Shift+F1 (★) and Shift+F8 (Y) already pass through
  Android Chrome untouched, so Shift+F2–F5 and Shift+F7 are safe too.
- **Avoid ❖ (F6):** F6 is fine key-wise, but ❖ is also the controller's power-off
  button — holding it to power down spams F6. Leave it unbound.

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

## Procedure — to discover what a button actually sends (debug probe)

If a mapping is ever unclear, temporarily re-add this line at the top of the
`keydown` handler in `chat.html` (right after the type-bar skip), restart Spark,
hard-refresh the phone, press the button, then read `spark.log` for `BTNMAP`:

```js
if (/^F\d+$/.test(e.key) || e.key === 'Unidentified') {
    fetch('/api/log', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({msg:'BTNMAP key='+e.key+' code='+e.code+' keyCode='+e.keyCode})}).catch(()=>{});
}
```

Remove it once you have the answer (it's noise otherwise).

---

*Constraint to remember: the physical layout you hold is rotated ~90° from the app's
picture, so "the button that feels like Up" may not be the one labeled ↑. Trust the
key each button **sends** (the F-number), not its printed position.*
