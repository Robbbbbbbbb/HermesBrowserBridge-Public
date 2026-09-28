# Silent fetch — headless background origin requests

`browser_bridge_silent_fetch` runs an HTTP request from a hidden, minimized
worker tab on the target origin instead of an attached, visible one. Chrome's
own network stack does the work (cookies, TLS/H2/H3 fingerprint,
`sec-fetch-*`), which is what gets it past bot-manager edge fingerprinting
that a plain HTTP client (even one impersonating Chrome's TLS handshake)
fails — the motivating case was `tesla.com/api/findus/get-locations`
returning 200 through the bridge and 403 through `curl_cffi` from the same
IP. Unlike `browser_bridge_fetch`, this lane never attaches, never holds a
lease, and never puts a `tab_id` on the tool surface at all — origin → worker
resolution is entirely internal to the extension's own worker pool and the
gateway's grants.

**What you'll see, if you ever look.** Every worker tab lives in one
dedicated, minimized, unfocused browser window titled "Hermes Browser
Bridge — background", inside a collapsed tab group named "Hermes
background" — it never steals focus, and restoring or focusing it is
logged once but never fought. That window's own first tab (kept open so the
window has something to title itself with) is a real extension page as of
0.2.3, not a bare `data:` URL — it explains what the window is for in plain
language and lists the live background workers (origin, state, age,
requests served) with a "Stop all" button, the same controls as the popup's
own "Background workers" panel. Neither that tab nor any worker tab ever
shows up in `tabs.list`, the share UI, presence, or the agent's own
`tabs.create` workspace.

This is additive: `attach`/`snapshot`/`act`/`fetch`/`evaluate` are unchanged.
Design source: `ProjectRules/silentfetch.md` (the build plan) and
`Resources/headless-sync-fetch.md` (the original spec) — where the two
disagree, the code follows the plan's own corrections (§1 of that file), and
this document follows the code. See `hermes_plugin/skill/SKILL.md` §9f /
`hermes_plugin/skill/references/headless-fetch.md` for the agent-facing
version of this same material (params/result table, paging pattern).

## Contents

1. [The grant model](#1-the-grant-model)
2. [Config keys](#2-config-keys)
3. [Spill/cache: location, retention, redaction](#3-spillcache-location-retention-redaction)
4. [Audit events](#4-audit-events)
5. [Security model](#5-security-model)
6. [Troubleshooting](#6-troubleshooting)

## 1. The grant model

A silent-fetch call is gated by **two** independent settings, checked in this
order (`hermes_plugin/silent_grants.py`'s `authorize_silent_fetch`):

1. **The origin's ordinary grant mode** (`off`/`request`/`full`, same table
   `modes.md` documents for every other tool). `off` refuses outright with
   `SILENT_ORIGIN_NOT_GRANTED` (4257) — nothing below this point is even
   consulted, because an origin the ordinary grants gate has closed is not
   reachable by any tool, silent or otherwise.
2. **The origin's own per-origin "Background requests" setting** (the popup
   control SF6 shipped), one of:
   - **`off`** — refused the same way as an ordinary `off` grant, even if the
     ordinary mode is `full`.
   - **`always`** — zero-touch. No prompt, ever, for this origin.
   - **`ask`** — one approval per Hermes agent session (capability
     `silent_fetch`, distinct from the `fetch` capability — a standing grant
     for one lane never silently covers the other), then silent for the rest
     of that session.
   - **Default** (no explicit popup override on record) — falls through to
     the plugin-wide default below.

**Default resolution**, when there's no explicit per-origin override:

- Origin's ordinary mode is `full` **and** `silent_fetch.full_implies_silent`
  (config, default **true**) → zero-touch.
  `full_implies_silent: false` makes a `full` origin ask once per session
  instead — the "ask once, then silent for the rest of the session" behavior
  every `request`-mode origin already gets.
- Origin's ordinary mode is `request` → ask once per session (same
  `silent_fetch` capability, same session-grant memory as the `always` case
  above once granted).

**An explicit per-origin override always wins over the default, both ways.**
A row set to `off` is never re-derived as allowed just because the origin's
ordinary mode is `full`; a row set to `always` is never downgraded to "ask"
just because `full_implies_silent` is `false`. Only the *absence* of an
explicit row falls through to the default.

Popup UI: **Default / Off / Ask first / Always allow** per origin — "Default"
shows what the plugin setting currently resolves to for that origin, it
doesn't mean "no setting." Clearing back to "Default" deletes the override
row outright rather than storing a fourth literal value.

## 2. Config keys

Under `browser_bridge.silent_fetch` in the gateway's `config.yaml`
(`hermes_plugin/config.py` DEFAULTS, merged one level deep — a partial block
keeps every key it doesn't mention):

| Key | Default | What it controls |
|---|---|---|
| `enabled` | `true` | Fleet-wide kill switch for the whole tool. `false` refuses every call with `SILENT_ORIGIN_NOT_GRANTED` before any grant/SSRF check runs. |
| `full_implies_silent` | `true` | See §1. |
| `max_workers` | `5` | Per-device cap on concurrent hidden worker tabs, sent to the extension on `device.hello`. LRU-evicted past this. |
| `worker_ttl_s` | `1800` (30 min) | A worker idle this long is recycled; the next call to that origin transparently relaunches one. |
| `bootstrap_timeout_ms` | `30000` | How long a worker's launch (creating its tab and bootstrapping a committed, interactive document at the target origin) may take before failing with `SILENT_WORKER_LAUNCH_FAILED` (4258). Clamped to `[5000, 120000]` regardless of what's configured. Sent to the extension on `device.hello`/`device.heartbeat` as `bootstrap_timeout_ms`, next to `max_workers`/`worker_ttl_s`. A per-call `bootstrap_timeout_ms` tool param (same clamp) overrides it for one launch only — see §4. Raised in rev 3 from a fixed, non-configurable 20s (the tool's own `timeout_ms` never reached the bootstrap at all — only the fetch after it). |
| `rate_per_s` | `2` | Token-bucket refill rate, per (device, origin). |
| `burst` | `10` | Token-bucket capacity, per (device, origin). Exceeding it → `SILENT_RATE_LIMITED` (4260) with `retry_after_s`. |
| `max_bytes_cap` | `100 MiB` | Single source of truth for every ceiling this feature enforces: a call's own `max_bytes` clamp, the relay's chunk-reassembly per-request ceiling (`max_bytes_cap` + one frame), and the `max_body_bytes` figure sent to the extension on `device.hello`/`device.heartbeat`. Raisable with no code change, up to a 2 GiB correctness ceiling (`config.SILENT_FETCH_SANITY_MAX_BYTES`). Raised from 16 MiB to 100 MiB in rev 2 after a bulk-fetch workload hit the old ceiling. |
| `max_buffered_bytes` | `max_bytes_cap` + one frame | Global ceiling on bytes buffered across every concurrent silent.fetch chunk stream on this gateway process — room for one max-size body in flight at a time by default (sized for this host's ~1.6 GB free with the gateway's own ~560 MB RSS already accounted for). Raise it to allow more concurrent large bodies in flight at once. |
| `cache_retention_hours` | `24` | Spill files older than this are pruned on the next write. |
| `cache_max_bytes` | `500 MiB` | Total spill directory size ceiling; oldest-mtime-first pruning once retention alone isn't enough. |

A misconfigured individual value (wrong type, zero, negative) falls back to
its shipped default rather than producing nonsensical or silently-disabled
behavior (`silent_grants.silent_fetch_config`'s `_positive_number` guard).

## 3. Spill/cache: location, retention, redaction

Responses over `preview_chars` (or any call that passes `cache_ttl_s`) are
written whole to:

```
~/.hermes/browser_bridge/fetch_cache/<sha256 digest>.json   # {meta, body}
~/.hermes/browser_bridge/fetch_cache/<sha256 digest>.bin    # binary body, only when expect:'binary'
```

Directory mode `0700`, files mode `0600`, written atomically (temp file in
the same directory, `chmod` before content lands, then `os.replace`) so a
reader never sees a half-written record. The digest covers device, method,
URL, `credentials`, the canonicalized post-strip request headers, and the
body — two calls that differ only in `Accept`/`Authorization`/`Content-Type`,
or in cookie inclusion, never share a cache entry, and two different devices
never share one either.

**Redaction runs before anything is spilled**, following the requesting
device's currently-reported redaction policy (`device_redaction_policy`,
same mechanism `_redact_fetch_body` already uses for `browser_bridge_fetch`)
— for the inline preview, the echoed response headers, and the on-disk spill
body alike. A cache **hit** re-runs redaction under the device's *current*
policy (not whatever was in force when the response was first written) and
rewrites the spill record in place if that changes it, so a later relaxed-or-
tightened policy is honored by a cache hit too, not just by a fresh fetch.

Retention: pruned oldest-first on every write, in two passes — anything
older than `cache_retention_hours` is deleted outright regardless of total
size, then oldest-mtime-first until the directory is back under
`cache_max_bytes`.

## 4. Audit events

`~/.hermes/browser_bridge/audit.jsonl` (`hermes_plugin/silent_grants.py`):

- **`silent_fetch`** — every completed call (including a cache hit):
  device, holder, origin, method, url, status, bytes, `from_cache`,
  redactions.
- **`silent_fetch_refused`** — every refusal, at any gate (grant, SSRF, rate,
  worker launch/kill, sha256 mismatch, timeout): device, origin, method, url,
  `reason_class`.
- **`silent_worker`** — the extension's worker pool (`extension/src/
  background/silent-pool.ts`) reports every lifecycle point over the wire as
  a `silent.worker` notification (`protocol/schema.json`), which
  `hermes_plugin/relay.py`'s `_on_silent_worker` turns into this audit line
  under the authenticated connection's own device id: device, origin,
  `action`, `reason`, and (when the worker instance is available) `served`/
  `age_ms`. `action` is one of:

  | `action` | Fires when | Typical `reason` |
  |---|---|---|
  | `launch` | a worker tab finished loading and is ready to serve | — |
  | `launch_failed` | `chrome.tabs.create`/grouping/navigation failed (4258) | `nav_error` for a navigation failure |
  | `recycle` | the worker was closed and will relaunch on next use | `ttl` (idle past `worker_ttl_s`), `origin_drift` (navigated off its own origin), or `window_drift` (its tab, or its whole tab group, is no longer inside the pool window — e.g. dragged out into the user's own window) |
  | `evict` | the LRU idle worker was closed to make room at `max_workers` | `lru` |
  | `kill` | `silent.kill` or the popup's Stop / Stop all | `user_kill` |
  | `kill` | The global kill switch (every worker, then the pool window) | `kill_switch` |
  | `closed` | the user closed the worker's tab or the pool window themselves | `user_closed` |
  | `adopted` | a service-worker restart re-adopted a still-live worker | `sw_restart` |

  Rev 3 adds a set of fields present only on `launch_failed`, replacing the
  old "every failure is `nav_error` with nothing else to go on" shape:

  | Field | Meaning |
  |---|---|
  | `failure` | One of `timeout` / `nav_error` / `redirect_foreign` / `tab_closed` / `create_failed` / `group_failed` — the actual reason this launch failed, distinct from (and more specific than) `reason` above. |
  | `chrome_error` | Chrome's own net error token (e.g. `net::ERR_ABORTED`) for a `nav_error` failure, sanitized to `/^net::[A-Z_]+$/` — anything else is dropped, never forwarded as free text. |
  | `last_url` | The last committed top-frame URL as origin + path only — query and fragment always stripped, capped at 128 chars. Never a session token or SSO state parameter. |
  | `redirects` | How many top-frame navigations committed after the first, for this one launch attempt. |
  | `redirect_kinds` | `{server_redirect, client_redirect}` counts from `webNavigation.onCommitted`'s own `transitionQualifiers`. |
  | `ready_state_reached` | How far the top frame got before settling: `none` / `committed` / `interactive` / `complete`. |
  | `elapsed_ms` | Milliseconds from the start of this launch attempt to the point it settled. |

- **`silent_worker_rejected`** — a `silent.worker` notification with an
  unrecognized `action` or no `origin`; audited instead of `silent_worker`,
  never silently dropped.
- **`silent_fetch_failed`** (rev 4) — a `TIMEOUT` (4300) failure that carried
  the extension's own diagnostic snapshot (`silent-fetch.ts`'s
  `buildTimeoutResult`, forwarded as the wire error's `data`): device,
  origin, `phase` (one of `acquire`/`preflight`/`inject`/
  `page_fetch_headers`/`draining`/`slicing`/`pushing_chunk`/`done`, or
  `unknown` for anything else), `elapsed_ms` (time spent in that phase),
  `bytes_so_far`, and `fetch_native` (whether `fetch` was still native code
  at call time). Distinct from `silent_fetch_refused`'s bare `reason_class`
  — this is what lets you tell "the extension itself gave up cleanly at
  phase X" from every other refusal reason. See §6.
- **`seq_gap`** (not silent-fetch-specific, but silent fetch's own RPC
  traffic is what most often reveals it) now carries `revealing_method` —
  the method (or `<response>` for a bare reply to a gateway→extension
  request) of the frame whose sequence number didn't match what the gateway
  expected. Rev 4 fixed the most common cause: a bare response frame used to
  bypass sequence tracking entirely (`hermes_plugin/relay.py`'s `_handle`),
  so the gateway's own counter silently fell one behind every RPC response
  the extension sent — making the *next* frame look like a lost one that
  was never actually lost. If you still see genuine `seq_gap` lines after
  this fix, check for an accompanying `conn_send_failed` (below) from the
  same device around the same time.
- **`conn_send_failed`** (rev 4) — the extension's own report
  (`offscreen/client.ts`'s `serializeAndSend`) that a frame took a sequence
  number but never actually reached the wire (a `JSON.stringify` failure on
  a non-serialisable value, or a `WebSocket.send` failure) — reported on the
  next frame that *does* go out successfully: `device`, `method` (what the
  failed frame was), `seq`, `error_kind` (`serialize_error`/`send_error`).
  Purely informational; never itself a refusal.

## 5. Security model

- **Grants enforced gateway-side only** (`silent_grants.authorize_silent_fetch`)
  — the extension's pool is plumbing; it trusts nothing but its own kill
  switch. A modified extension build cannot widen its own access.
- **SSRF guard** (`silent_grants.ssrf_guard`), independent of
  `browser_bridge_fetch`'s own guard because there is no attached tab to
  treat as an automatic same-origin allow here:
  - Only `http`/`https` schemes; anything else (`file:`, `chrome:`,
    `javascript:`, `data:`, `ftp:`, ...) is refused outright.
  - A target that looks like loopback/link-local/RFC1918-or-ULA private
    space, or an internal hostname suffix, is refused unless the origin has
    an **explicit** grant (not the config default-mode fallback).
  - `https` is required except for an explicitly granted private/internal
    origin — plaintext to the public internet is never allowed just because
    this lane runs unattended.
  - **Known limitation, inherited unchanged from `browser_bridge_fetch`'s own
    guard:** this check is string/literal only — IP literals and a short
    list of internal hostname suffixes, never a live DNS resolution. A
    public-looking hostname that resolves (or is later rebound via DNS) to a
    private/internal address is **not** caught by this guard. The actual
    network connection happens inside the browser, not on the gateway, so a
    blocking DNS lookup here wouldn't reliably reflect what the browser
    actually contacts anyway. Full protection against DNS rebinding needs
    response-side or DNS-pinned enforcement, which is `Fetch.*` CDP
    interception territory — explicitly out of scope (see `security.md` §11).
    If your threat model includes an attacker who controls DNS for a domain
    you've granted background-request access to, this guard does not protect
    you from DNS rebinding, exactly as it does not for `browser_bridge_fetch`.
- **Rate guard**: a token bucket per (device, origin) — see §2 — refusing
  with `SILENT_RATE_LIMITED` (4260) and an honest `retry_after_s` rather than
  queuing indefinitely.
- **Per-origin serialization**: calls to the same origin are queued (Chrome
  would serialize same-origin worker use anyway); different origins never
  contend.
- **Honesty checks, not trust**: the gateway reassembles the chunked body
  itself and recomputes its own sha256 against whatever the extension
  reported, refusing the whole response (`INTERNAL_ERROR`) on a mismatch
  rather than passing on bytes it can't vouch for. `total_bytes_source`
  (`"stream"` / `"content-length"` / `"partial"`) says which measurement
  produced the reported total, rather than presenting one number as if it
  were always the true stream length.
- **No new JS surface**: `silent.fetch` carries method/URL/headers/body/
  credentials only — never arbitrary code. If a site needs computed headers
  (a CSRF token read, say), that stays `browser_bridge_evaluate`'s job.
- **`Cookie` request headers are always stripped**, same as
  `browser_bridge_fetch` — cookies come from the worker tab's own jar, never
  from a caller-supplied header. Response `Set-Cookie` values are never
  returned, only names and a count.

## 6. Troubleshooting

**`SILENT_ORIGIN_NOT_GRANTED` (4257) even though the origin is `full`.**
Check the origin's own "Background requests" popup setting first — an
explicit `off` row there beats `full` outright, and `full` alone only
implies silent access when `silent_fetch.full_implies_silent` is `true`
(the default). If neither explains it, `silent_fetch.enabled: false` in
`config.yaml` refuses every call with this same code — check that too.

**A call that should be zero-touch instead parks for an approval once per
session.** Either the origin's own setting is "Ask first" (which always
wins over the default), or its ordinary mode is `request` (asks regardless
of `full_implies_silent`, which only ever applies to `full` origins), or
`full_implies_silent` has been set to `false` in `config.yaml`.

**`SILENT_WORKER_LAUNCH_FAILED` (4258).** The hidden minimized window or its
worker tab couldn't be created, or the bootstrap navigation (the origin
root, or an explicit `bootstrap_path`) didn't settle within its budget
(`silent_fetch.bootstrap_timeout_ms`, default 30s, clamped `[5000,120000]`;
a per-call `bootstrap_timeout_ms` tool param overrides it for one call).
The error message names which of these happened and includes a one-line
summary of the same diagnostics the `silent_worker` audit line carries
(`reached=...`, `last_url=...` — origin + path, never a query string —
`redirects=...`, `chrome_error=...`, `elapsed_ms=...`); the `silent_worker`
audit line itself (see §4) has the full, structured version. Rev 3 no longer
treats every navigation hiccup as fatal:

- A top-frame error (e.g. `net::ERR_ABORTED`) is tolerated when a follow-up
  navigation for the same frame commits within ~1.5s — an interstitial or
  bot-check page that redirects mid-load is normal, not a failure. Only a
  genuinely terminal error (nothing else ever arrives) fails, with
  `failure: "nav_error"` and `chrome_error` naming Chrome's own error token.
- A bootstrap that bounces through a foreign origin (a consent host, an SSO
  hop) and back to the target origin still succeeds. Only a foreign-origin
  document that reaches `DOMContentLoaded` and then sits still for that same
  ~1.5s grace window fails, with `failure: "redirect_foreign"` naming the
  origin it landed on (never the path or query, which could carry a session
  token or SSO state parameter).
- Readiness itself no longer waits for the full window `load` event — a
  page with long-lived subresources, beacons, or a consent banner that never
  fully "completes" now resolves once the top frame has committed a document
  at the target origin and reached `DOMContentLoaded` (`readyState`
  `interactive` or `complete`).

If a launch still fails after all that, a persistent one suggests the
bootstrap path itself is unreachable, redirecting somewhere that never comes
back, or genuinely needs longer than the configured budget — try passing an
explicit `bootstrap_path` that's known to return 200 quickly, or a larger
`bootstrap_timeout_ms` for a site with a slow interstitial/redirect chain.

**`SILENT_WORKER_KILLED` (4259) mid-loop.** Someone used the popup's
per-worker kill button or "stop all," or the extension's global kill switch
fired. The in-flight call fails outright rather than silently retrying; the
pool rebuilds cleanly on the next call to that origin.

**`SILENT_RATE_LIMITED` (4260) during a paging loop.** Expected behavior at
the default 2 req/s, burst 10 — a tight loop with no delay between pages
will hit this. Back off for the returned `retry_after_s` rather than
retrying immediately; raising `silent_fetch.rate_per_s`/`burst` in
`config.yaml` is a gateway-wide, not per-call, change.

**A response that should have been small came back with `body_file` instead
of an inline `body`/`body_json`.** Check `preview_chars` — the default
(20000) is intentionally conservative; large JSON pages routinely exceed it.
This isn't a failure, it's the paging-without-blowing-context design (see
the SKILL.md reference for the `execute_code` pattern).

**Two devices, one origin, and a cache/rate mismatch between them.** Both
the response cache key and the rate-limit/serialization buckets are keyed
per `(device_id, ...)` — each paired device has its own pool, its own rate
budget, and its own cache entries for the same URL. This is intentional
(SF7's acceptance test 7), not a bug.

**A call hangs silently for the full `timeout_ms` (or longer) with nothing
in the audit trail until a bare `TIMEOUT` (4300) at the end.** Before rev 4,
the only deadline on the fetch itself was a timer *inside* the worker
page — and the worker tab always lives in a minimized, unfocused window
(§1), so it is continuously "hidden" from Chrome's own perspective and
subject to background-tab timer throttling. A page whose own script (or a
site's bot-manager sensor wrapping `fetch`/timers) holds the event loop, or
simply never lets that timer callback run, could make the page's own
deadline never fire — the call then hung until the *gateway's* relay wait
gave up, with no diagnostic saying where. Rev 4 adds a second, independent
deadline tracked entirely in the extension's own service worker (never a
page timer), so the extension always answers first. Read the failure's
`data` (surfaced by `_bridge_err` as `detail`, and mirrored in the
`silent_fetch_failed` audit line above):

- `phase` says where it was stuck: `page_fetch_headers` (the fetch itself
  never got a response), `draining` (got headers, but the body never
  finished), `slicing`/`pushing_chunk` (the fetch succeeded, but the
  chunk-relay round trip afterward stalled), or `inject` (the initial
  `executeScript` call itself never returned).
- `bytes_so_far` and `fetch_native` ride along. **`fetch_native: false`**
  means the target page had already wrapped/monkey-patched `window.fetch`
  before this call ran — a strong signal the site's own bot-manager sensor
  is involved and may be the reason the drain never finished.

**When to try `fetch_impl: "native"`.** If a `silent_fetch_failed`/TIMEOUT
audit line shows `fetch_native: false` at `phase: "page_fetch_headers"` or
`"draining"` — i.e. the page's own (wrapped) `fetch` is what stalled, not
the worker tab or the chunk relay — retry the same call with
`fetch_impl: "native"`. This fetches from a fresh same-origin `about:blank`
iframe created for that one call, whose `fetch` was never touched by the
parent page's own scripts, so a wrapper that stalls (waiting on sensor data
that needs a visible/focused tab, say) is bypassed entirely. It still runs
in the same origin with the same cookies — only the specific `fetch`
implementation changes. The result (and any TIMEOUT failure) always reports
which impl actually ran as `fetch_impl`, so a native retry that still stalls
tells you the drain itself (network-level, or the target simply being slow)
is the real bottleneck, not the page's wrapper.
