# ttyd basic auth = blank white terminals on mobile

**Symptom:** Terminal iframes render fine on desktop but show **blank white**
on mobile Chrome. No error, no prompt — just white.

**Why:** the terminals are same-origin iframes (`spark.tradingdata.net/term/*`).
When ttyd runs with `-c user:pass` (HTTP Basic Auth), the iframe issues a 401
challenge — and **mobile Chrome silently blocks Basic-Auth prompts inside
iframes**. It never shows the dialog; the iframe just stays blank. Desktop
Chrome prompts (or has the creds cached), so it looks fine there.

**Fix:** ttyd runs with **no `-c` flag** (start.sh, since 2026-07-18).
Protection comes from:
1. Cloudflare Access on `spark.tradingdata.net` (covers `/term/*` — same host),
2. Spark's own token cookie for the app + API,
3. ttyd ports (7682-7685) only reachable via the local cloudflared tunnel.

**Rule:** never re-add `-c`/basic auth to ttyd while the terminals are
embedded as iframes. If extra auth is ever needed on `/term/*`, use a
cookie-based check (mobile-safe), not an HTTP auth challenge.

**Related history:** when the terminals lived on separate subdomains
(`terminal2.tradingdata.net` etc.), the basic-auth prompt DID show on mobile
(cross-origin top-level-ish behavior) — that was the old "weird login screen".
The same-origin migration (2026-07-17) made the prompt impossible, which
surfaced this gotcha.
