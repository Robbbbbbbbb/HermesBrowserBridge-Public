# `browser_bridge_silent_fetch` — headless background fetch

Bridge mechanics, not a product lesson — see `SKILL.md` §9f for when to reach
for this instead of `browser_bridge_fetch`. This file is the detail: exact
params/result, the paging pattern, and what the user actually sees while a
call is in flight.

## 1. Params

| Param | Notes |
|---|---|
| `url` | Required. Absolute URL. Its origin is what gets granted/rate-limited/audited — there is no tab, so no other origin is ever implicitly in play. |
| `method` | Default `GET`. |
| `headers` | String → string. A `Cookie` header is always stripped — cookies come from the worker tab's own jar, not from you. |
| `body` / `json_body` | Mutually exclusive. `json_body` is JSON-encoded for you and gets `Content-Type: application/json` if you didn't set one. |
| `credentials` | `include` (default) / `same-origin` / `omit` — cookie/session inclusion policy for the worker tab. |
| `expect` | `auto` (default, sniffs) / `text` / `json` / `binary`. `binary` never attempts UTF-8 decode and is always spilled to `body_file` rather than summarized. |
| `max_bytes` | Cap on bytes captured off the wire. Default 2 MiB, hard-capped at this gateway's configured `silent_fetch.max_bytes_cap` (`config.yaml`; 100 MiB by default, raisable with no code change). |
| `timeout_ms` | Default 30000. |
| `preview_chars` | Default 20000. Above this, the full body is spilled and you get `body_preview` + `body_file` instead of the whole thing inline. |
| `cache_ttl_s` | If set and a cached response for this exact (device, method, url, body, credentials, headers) is younger than this many seconds, returns it with `from_cache: true` and skips the browser round trip. Still audited. |
| `bootstrap_path` | Overrides the worker's default bootstrap path (the origin root `/`) it navigates to once before the first fetch, so the site's own bot-manager JS runs and sets cookies. Use this if the origin root itself 404s or redirects somewhere unhelpful. |
| `bootstrap_timeout_ms` | Overrides how long the worker's one-time launch/bootstrap (not the fetch itself — that's `timeout_ms` above) may take before failing with `SILENT_WORKER_LAUNCH_FAILED`, for this call only. Clamped `[5000,120000]`ms. Default: this gateway's configured `silent_fetch.bootstrap_timeout_ms` (30000ms unless raised). Raise this for a site with a slow interstitial, bot-check, or redirect chain (an Akamai challenge, an SSO hop) rather than raising `timeout_ms`, which only covers the request after bootstrap. |
| `fetch_impl` | `page` (default) / `native`. `page` fetches with whatever `window.fetch` the target page's own scripts have installed — today's behavior, including any site wrapper. `native` fetches from a fresh same-origin `about:blank` iframe instead, whose `fetch` the page's own scripts never touch — same origin, same cookies, but immune to a site's fetch-wrapping bot-manager sensor. Try `native` when a call times out (4300) with `fetch_native: false` at phase `page_fetch_headers`/`draining` in its error detail — see `docs/silent-fetch.md` §6. The result always reports which impl actually ran. |
| `device_id` | Same as every other tool — omit when exactly one device is paired. |

**Never pass `tab_id` or a `tab` alias.** Origin → worker resolution is
entirely internal (the extension's hidden worker pool); passing either is
refused outright (`INVALID_PARAMS`), not silently ignored.

## 2. Result

`status` / `ok` (`200 <= status < 300`) / `headers` / `content_type` /
`total_bytes` / `total_bytes_source` (`"stream"` / `"content-length"` /
`"partial"` — honest about which one produced the number) / `wire_truncated`
/ `binary` / `redactions` / `timing` (`{total_ms, ttfb_ms}`), plus exactly one
of:

- `body_json` — parsed JSON, when it decoded cleanly and fit under
  `preview_chars`.
- `body` — decoded text, same size condition.
- `body_preview` + `body_file` — the body is bigger than `preview_chars` (or
  is `binary` with `expect: 'binary'`): a truncated preview inline, the whole
  thing at the path in `body_file`.

`Set-Cookie` never comes back as values — only `set_cookie_names` and
`set_cookie_count`. `from_cache` tells you whether this skipped the browser
entirely.

## 3. Paging worked example

Page a JSON endpoint until it stops returning a `next_cursor`, without ever
holding more than one page of text in context:

```json
{"tool": "browser_bridge_silent_fetch", "args": {
  "url": "https://console.example.com/api/v2/items?limit=200",
  "expect": "json"
}}
```

If the result comes back as `body_json` inline (small page), read
`body_json.next_cursor` directly and loop. Once pages get big enough to spill
(`body_file` present instead of `body_json`), read and advance from disk
instead of asking for a larger `preview_chars`:

```json
{"tool": "execute_code", "args": {"code":
  "import json\nwith open(body_file) as f: page = json.load(f)\ncursor = page['next_cursor']\nlen(page['items'])"
}}
```

Feed `cursor` into the next call's `json_body`/query string and repeat until
the field is absent or null. This is the same shape as `browser_bridge_fetch`
pagination (§9a) — the only difference is where the body lands.

## 4. Header ordering (a non-goal, not a bug)

There is no control over request header order or casing — `fetch()`
normalizes both, and that's deliberate: default Chrome header shape is
exactly what a fingerprinting site expects to see. If a site's bot-manager
cares about header order beyond what `fetch()` itself produces, this tool
cannot help; that's `Resources/headless-sync-fetch.md`'s §5 non-goals,
inherited unchanged.

## 5. What the user actually sees

- A minimized, unfocused Chrome window titled "Hermes Browser Bridge —
  background." Its tabs live in a collapsed tab group named "Hermes
  background" — nothing appears in the user's normal window/tab list.
- The extension badge blinks briefly on activity (rate-limited to about once
  a second), so there's always a physical, on-screen signal that a
  background request just happened even though no tab was visible.
- The popup's worker list (origin, age, requests served) and its per-worker
  kill button, plus "stop all." Killing a worker mid-request fails that
  in-flight call with `4259`; the pool rebuilds cleanly on the next call to
  that origin.
- The popup's per-origin "Background requests" control (Default / Off / Ask
  first / Always allow) — this is the grant SF4 checks, independent of the
  origin's ordinary attach/read/act mode.

## 6. Error codes and what to say

| Code | Tell the user |
|---|---|
| 4257 `SILENT_ORIGIN_NOT_GRANTED` | This origin isn't allowed for background requests. Ask them to set it to "Always allow" (or "Ask first") in the popup, or use `browser_bridge_fetch` against an attached tab instead. |
| 4258 `SILENT_WORKER_LAUNCH_FAILED` | The hidden background tab for this origin couldn't start in time. Worth a retry; if it keeps failing, say so plainly rather than looping. |
| 4259 `SILENT_WORKER_KILLED` | Someone (or the global kill switch) stopped the background worker mid-request. Only retry if the task still applies. |
| 4260 `SILENT_RATE_LIMITED` | Too many background requests to this origin too fast (default 2/s, burst 10). Back off for `retry_after_s` and retry — don't loop immediately. |
| 4300 `TIMEOUT` | The worker's own service-worker deadline (`timeout_ms` plus a small margin) ended the call. The error detail names `phase`/`bytes_so_far`/`fetch_native` — see `docs/silent-fetch.md` §6 for how to read them, and try `fetch_impl: "native"` when `fetch_native` is `false`. |
