# Session export: using a browser-issued session from the gateway host

`browser_bridge_cookies` with `include_values: true` lets the agent run origin
requests directly from the gateway host, when the origin's edge protection
accepts a browser-issued session. This page is the decision procedure. The
same steps are in the bundled skill (`hermes_plugin/skill/SKILL.md`, section
9g).

## When to use it

| Job | Use |
|---|---|
| A small burst of requests (tens) | Session export: 1 cookie pull + N local requests |
| A long, paginated or unattended sweep | `browser_bridge_silent_fetch` |
| Interactive work on a tab already on screen | `browser_bridge_fetch` |

A cookie pull plus N local requests is cheaper on context and on the rate
bucket than N bridge round trips.

## Both pieces are required together

Edge-protection sites check transport characteristics and session state at the
same time. Exporting only the session (with an ordinary HTTP client) fails, and
so does using a Chrome-compatible client without the session. Send the exported
cookies with a Chrome-compatible HTTP client.

## Consent precondition

The session is only meaningful if the paired browser has already loaded the
origin and passed its first-visit checks (interstitial, consent banner,
challenge). Have the user open the site in the paired browser first.

## Refresh discipline

- Cookie metadata exposes expiries. `bm_sz` lasts about 2 hours on
  Akamai-fronted sites. Re-export before expiry.
- On a 403, retry once with a fresh export.
- Persistent 429 or challenge HTML means the site wants its own browser for
  this job. Switch to `browser_bridge_silent_fetch`. Do not hammer.

## Security posture

- Values are gated by the `cookies` capability grant (`full` mode or approval).
- Cookie values are never written to the audit log, even with `include_values`.
- A session cookie is a credential. Write values to a temp file with mode
  0600, delete it when done, and never echo values to chat or logs. Values
  never enter the conversation.
