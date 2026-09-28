# Troubleshooting

Symptom → cause → fix, drawn from what's actually in this codebase (verified
2026-09-22 against Hermes v0.19.1 on the gateway, the version this was built
and verified against) rather than generic MV3/CDP advice. If your symptom
isn't here, check the audit log first — `~/.hermes/browser_bridge/audit.jsonl`
(or `hermes browser-bridge logs`) records every gated decision and every
pairing/connection event, and usually says exactly why something was refused.

## Pairing and connection

### `hermes browser pair` (and devices/revoke/logs/status/export) fail with "not a `hermes browser` command"

**This happened for real — it isn't hypothetical.** This plugin's CLI used to
register itself under the top-level name `browser`. That worked fine against
Hermes v0.19.1, but the gateway host was later upgraded to v0.21.4, which
ships its own hardcoded `hermes browser` subparser in core (real-Chrome-profile
helpers: `browser close-profile`, an unrelated feature). Core registers its
subparser *after* the dynamic plugin-CLI loop runs, and argparse silently
**overwrites** on a name collision instead of raising an error — so core's
`browser` won, this plugin's registration was silently discarded, and
`pair`/`devices`/`revoke`/`logs`/`status`/`export` all became unreachable with
no warning anywhere in the logs. Confirmed live on the gateway:

```
$ hermes browser pair
hermes browser: 'pair' is not a `hermes browser` command.
$ hermes browser --help
usage: hermes browser [-h] {close-profile} ...
```

**The fix that resolved it:** this plugin's CLI namespace was renamed from
`browser` to `browser-bridge` in `hermes_plugin/__init__.py`'s
`register_cli_command(name="browser-bridge", ...)` call. The subcommands
themselves were not renamed — it's `hermes browser-bridge pair`,
`hermes browser-bridge devices`, etc. `browser-bridge` was checked against the
full v0.21.4 top-level command list (`hermes --help`) and does not collide
with anything else core registers. If you're reading this because
`hermes browser-bridge ...` is *also* failing now, check `hermes --help`
again for a newer core collision before assuming something else broke — and
if you rename the namespace again, update this section too.

### Popup stuck on "Connecting…" or "Pairing…" and never reaches "Connected"

**Symptom:** you enter a pairing code (or click Reconnect) and the dot just
sits on "Pairing…"/"Connecting…" — no error, no timeout, and clicking
Connect/Reconnect again visibly does nothing.

**Cause (fixed as of this writing; if you're on an older build, this is
exactly what you're hitting):** `offscreen/client.ts` used to have **no
timeout at all on the WebSocket `open` phase**. `new WebSocket(url)` was
created and its `open`/`close`/`error` listeners registered, but nothing ever
gave up on it — if the gateway was unreachable (wrong host, firewall,
gateway down, different VLAN), the socket just sat in `CONNECTING` for as
long as the OS felt like, often 1–2 minutes, sometimes indefinitely, with
the popup showing "Pairing…"/"Connecting…" the whole time. Worse, because
`this.ws` was still that same stuck `CONNECTING` socket, `_connect()`'s own
guard (`if (this.ws && ... CONNECTING) { if (!pairCode) return; }`) meant a
plain Reconnect click (no code) silently no-opped against it — the UI could
never recover on its own, only a full extension reload freed it.

**The fix:** `client.ts` now arms a **15-second connect-attempt timeout**
(`CONNECT_TIMEOUT_MS`) the instant it creates the socket, covering both the
`open` wait and the `device.hello` round trip that follows it (which already
had its own 30-second `REQUEST_TIMEOUT_MS`, but that only starts once hello
is actually sent — it never covered a socket that never opened at all). On
expiry the client closes the stuck socket, clears the in-flight guard, and
sets an actionable error naming the URL it tried:

> could not reach the gateway at ws://your-gateway-host:8765/bridge within 15s.
> Check: 1) the gateway is running, 2) the URL is correct, 3) this machine
> can reach that host and port (firewall/VLAN).

The state returns to `Disconnected` (not stuck), and — because the socket
guard is cleared unconditionally — clicking Connect/Reconnect again always
starts a genuinely fresh attempt.

**You don't have to wait 15 seconds either way**, now: a **Cancel** button
appears in the popup any time the state is `connecting`/`pairing`. It closes
the real socket immediately rather than just hiding the "Pairing…" text.
**If you cancel mid-pairing (after typing a code), assume that code is
spent** — the gateway marks a pairing code consumed the instant
`device.hello` submits it, which happens before the extension ever hears
back, so cancelling doesn't and can't un-consume it server-side. Generate a
fresh one with `hermes browser-bridge pair` rather than retyping the same
code — you'll otherwise just see `PAIR_CODE_INVALID`.

**Likely underlying causes, once you've confirmed the timeout/Cancel above
are working as described (i.e. this is now a real "can't reach the
gateway", not a UI hang):**

1. **Wrong gateway URL.** Options page → gateway URL must exactly match where
   the relay binds, *including the `/bridge` path* (default
   `ws://your-gateway-host:8765/bridge`). A bare `ws://your-gateway-host:8765` with no
   path, or a stray trailing slash, will connect-then-fail rather than fail
   fast, because the WebSocket handshake itself can still succeed against the
   wrong path on some setups before `device.hello` gets a chance to run.
2. **Firewall / wrong network.** The extension dials *out* from Chrome; if
   `your-gateway-host:8765` isn't reachable from wherever Chrome is running
   (different VLAN, a host firewall on `.201`, a captive/guest Wi-Fi that
   blocks non-standard ports), the socket never opens and you'll see
   `Connecting…` with **no corresponding line at all** in the gateway's audit
   log — the connection never arrived server-side. Confirm reachability with
   `nc -zv your-gateway-host 8765` from the Chrome machine, or check
   `ss -ltnp | grep 8765` on the gateway to confirm it's actually listening on
   `0.0.0.0` and not just `127.0.0.1`.
3. **Gateway not listening at all** — see the dedicated section below.

### Popup shows "not paired" but I definitely paired this device before

**Cause:** almost always a *transient* storage or reachability hiccup, not a
real unpair. `client.ts`'s credential-clearing paths are deliberately narrow —
`clearCredentials()` only ever runs on an explicit **Unpair** click, or on a
`TOKEN_INVALID`/`TOKEN_REVOKED` response from the gateway (a real
revocation). A connect timeout, a cancelled attempt, an ordinary dropped
connection, a `PROTOCOL_MISMATCH`, or even a failed *read* of
`chrome.storage.local` all leave the stored token exactly alone. The popup
now reflects that distinction: `status.paired` falls back to the last
successfully-observed credential state instead of flipping to `false` on a
bare read failure, and the status line says **"paired, not connected —
`<gateway url>`"** rather than the generic "not paired" whenever a token is
known to exist but the socket isn't currently connected. If you're on a
build old enough to show a bare "not paired" for a device you know is
paired, that's this defect — the popup should now say which case you're in.

**Fix:** if the status line genuinely says "not paired" (not "paired, not
connected"), the token really is gone — check the audit log
(`device_offline`/`auth_rejected` with `reason: revoked`) for whether someone
ran `hermes browser-bridge revoke`, and re-pair. If it says "paired, not
connected", the device is fine — it's a connectivity problem, see the section
above.

### The popup shows "Paused" and nothing Hermes does seems to work

**Not a bug.** The user paused sharing from the popup's **Pause sharing**
toggle. While paused, the extension refuses every inbound capability request
— snapshot, read, screenshot, act, fetch, cookies, network, ask — with
`SHARING_PAUSED` (code 4103), *before* any of it reaches `chrome.debugger` or
the page. The connection, pairing, and any attached tabs are all left alone —
only data flow stops — so this is a deliberate one-click privacy switch (for
"I'm about to open my bank / a payroll site / a private document"), not a
connection problem. `browser_bridge_status` surfaces `paused: true` for the
device so a Hermes session can see this on its own and stop retrying; a
gated tool call's refusal carries the hint "the user paused sharing in the
extension popup; ask them to resume." **Fix:** click **Resume sharing** in
the popup — resuming is instant, nothing needs to reconnect or re-pair.

### Popup shows "Not connected" with an error, or reverts from paired to unpaired

**Cause:** one of three terminal states in `offscreen/client.ts`, each with a
specific, user-actionable message rather than a silent retry loop:

| What happened | Message shown | What to do |
|---|---|---|
| Device was revoked (`hermes browser-bridge revoke`, or the popup's own "Unpair") | "this device's pairing was revoked — pair again to reconnect" | Get a new pairing code and pair again. |
| Token rejected but not explicitly revoked (stale/corrupted storage) | "pairing token was rejected — pair again to reconnect" | Same — pair again. |
| Protocol version mismatch (extension build is older/newer than the gateway expects) | "extension is out of date for this gateway — update the extension" | Rebuild the extension from the matching repo commit and reload it (`chrome://extensions` → reload icon) — the stored *credential* is left alone in this case, only the connection is refused, so re-pairing usually isn't needed once the versions match. |

All three are **dead ends until you act** — the client deliberately stops
retrying (no exponential-backoff reconnect loop for a terminal error), because
retrying a dead token or a mismatched protocol version forever would just spam
the gateway's audit log with `auth_rejected`/`PROTOCOL_MISMATCH` lines for no
benefit. Click Reconnect (or re-enter a pairing code) once you've fixed the
underlying cause.

### The gateway is not listening / relay_started is false

Check the latest `plugin_loaded` line: `ssh your-gateway-host 'tail -5
~/.hermes/browser_bridge/audit.jsonl'`. If `relay_started` is `false`:

- Check `relay_error` in the same line, or the immediately-preceding
  `relay_bind_failed` line's `error` field — almost always "address already
  in use," meaning something else is on port 8765 (another gateway process
  that didn't fully stop, or a leftover `hermes browser-bridge ...` process
  from *before* the M0 fix that made CLI invocations stop trying to bind the
  port — see `ProjectRules/ChangeLog.md`'s "1b.1" defect if you're on an old
  build).
  `ss -ltnp | grep 8765` on the gateway shows what's actually holding it.
- Check `browser_bridge.enabled` in `config.yaml` isn't `false`.
- Confirm you're looking at the process that's actually running the gateway —
  this host has had two Hermes install directories present at once during an
  in-place upgrade (`/usr/local/lib/hermes-agent` vs. an older
  `~/.hermes/hermes-agent`); `hermes --version`'s "Install directory" line
  tells you which one the current `hermes` command resolves to, which may not
  be the one the running gateway *service* was started from if a restart is
  overdue.

### Chrome sleep/resume leaves the badge stuck disconnected

**Cause:** understood and handled, but worth knowing the mechanism so you
don't "fix" it by force-reloading the extension unnecessarily. `client.ts`
arms a 5-second **close watchdog** every time it closes a socket
(`CLOSE_WATCHDOG_MS`); if Chrome sleeping/resuming leaves a dead/partitioned
connection where the browser never delivers a `close` event, the watchdog
fires anyway and manually drives the same reconnect path a real close event
would have. Separately, `chrome.alarms` wakes the service worker every 30
seconds (`bridge-keepalive`) specifically to recreate the offscreen document
if MV3 killed it, and every wake reconciles `chrome.debugger`'s live attach
state against what the worker remembers. **If it's still stuck after roughly
30–40 seconds** (one keepalive cycle plus the watchdog), something else is
wrong — check the gateway URL/firewall causes above before assuming this
recovery path failed.

## Content script and page compatibility

### A call fails with `CONTENT_SCRIPT_ERROR` (code 4203)

**Cause:** the page refused script injection. This is not a bug to chase —
Chrome itself blocks `chrome.scripting.executeScript` on `chrome://` pages,
the Chrome Web Store, and the built-in PDF viewer, and there's no extension
permission that overrides this. `dom.snapshot`, `page.read`, and `annotate`
(`browser_bridge_ask`'s highlighting) all funnel through the same content-script
bridge and will all report this on such a page. Deliberately reported as a
*distinct* error from "tab not attached" — the tab genuinely is attached
(`chrome.debugger` is fine on these pages), only the content-script-dependent
tools fail.

**Fix:** use `browser_bridge_screenshot` instead — it only needs
`chrome.debugger` (`Page.captureScreenshot` over CDP), not the content script,
so it **works on `chrome://`/Web Store/PDF pages** where snapshot/read do not.
You lose the interactive-element bounding-box overlay (that part *does* use
the content script and silently degrades to an empty box list on these pages
— "a bounding-box hint is a nice-to-have, the pixels are not"), but the pixels
themselves come through fine.

### `browser_bridge_act` fails with `CDP_ERROR: selector not found: ...`

**Cause:** the element the selector (or the CSS selector an `idx` resolved
to) pointed at is no longer in the DOM — the page re-rendered, navigated, or
the element was conditionally removed since the snapshot that produced that
`idx`. This is a **real, surfaced failure**, not a silent no-op — the action
never ran. For `select`/`submit`, the same situation is reported with a
`reason` field (`not_found`/`not_a_select`/`no_matching_option`) instead of a
bare message.

**Fix:** call `browser_bridge_snapshot` again and act on a fresh `idx`. Don't
retry the same stale `idx`/selector expecting the page to change back.

### `browser_bridge_act` fails with `INVALID_PARAMS: idx N is not in tab T's current index map`

**Cause:** `idx N`'s element is no longer on the page — it was removed (a
row deleted, a panel closed) — or the tab has navigated to a new document
since that idx was minted. An `idx` does **not** go stale merely because the
tab was snapshotted again: `browser_bridge_snapshot`/`find`/`inspect` and a
successful `act` all *merge* freshly-walked indices into the tab's existing
map (an element the walk still recognizes keeps the SAME idx it always had),
so yesterday's `[3]` is still `[3]` today as long as that element is still
there. idx numbers are also never reused on a tab until it actually
navigates — so this refusal always means "gone", never "reassigned to
something else". This is intentional: a stale idx silently resolving to the
*wrong* element would be far worse than a clean refusal.

**Fix:** call `browser_bridge_snapshot` (or `find`) again and act on a fresh
`idx` for the element you meant — it may have moved, been re-rendered under
a different subtree, or the tab may have navigated out from under you.

## Modes and approvals

### An origin in `request` mode refuses `attach`/`snapshot`/`read`/`screenshot` outright, with no approval prompt

**This is not a bug** — see `modes.md`'s lead section. `request` mode never
gates the read-side tools at all in the shipped design; they behave exactly
like `off` for those four tools. If you want Hermes to see a page, the origin
needs `full`. Only `act`, `ask` (with candidates), `fetch`, `cookies`, and
`network` ever park for an approval.

### An `act`/`fetch`/`cookies`/`network` call is taking a long time / appears hung

**Cause:** the origin is in `request` mode and the call is parked waiting for
a human to answer the popup approval card. The calling tool call genuinely
blocks — up to `browser_bridge.approval_ttl_seconds` (default 120s) — this is
by design, not a stall. **Check the popup for a pending-approval card** before
assuming something's broken.

**If it then reports "timed out":** nobody answered within the TTL. This
resolves to a denial (never a silent allow) and is audited as
`approval_resolved` with `scope: "timeout"`. Retry only after confirming a
human is actually watching the popup, or switch the origin to `full` if
per-call approval isn't actually needed for this workflow.

### An `act` call is refused with `TARGET_BUSY` / "tab is driven by session X"

**Cause:** another Hermes session (or the same one, from a previous, still-live
call) holds the driving lease on that tab. Reads don't need the lease
(`plan.md` §3.3: "reads are lock-free"), but every `act` call takes or renews
it, and only one session can hold it at a time — this is deliberate, to
avoid two agents interleaving clicks on the same tab.

The lease duration defaults to 60 seconds but is a per-device Chrome
extension setting (Options → "Tab lease", 10–1200 seconds, or unlimited) —
check the refusal's own `hint` field, which names this device's actual
duration, rather than assuming 60s. `browser_bridge_status`'s `leases` array
reports the same thing per attached tab: `expires_in_s` (an integer), or
`expires_in_s: null` plus `lease: "unlimited"` when the device's lease never
expires on its own.

**Fix:** wait for the lease to expire (per the hint's actual duration), ask
the user to have the other session release it (`browser_bridge_release`), or
confirm you're not issuing two overlapping tool calls against the same tab
from parallel agent turns. If the device's lease is set to unlimited, waiting
never resolves it — only an explicit release, the user's Stop/Release-all,
or a detach frees the tab.

### The popup's per-origin dropdown doesn't show what mode is currently set

**Fixed — this entry used to describe it as a permanent UX gap.** The popup
genuinely had no read path for the grants table, so the picker was set-only
and rendered blank. It now shows the effective mode, with a line under the
origin saying where that mode came from:

- **"set for this site"** — the user chose it; there is a row in the grants
  table for this origin.
- **"gateway default"** — no row exists, so the origin inherits
  `browser_bridge.default_mode` (`full` as shipped). Same permission as an
  explicit `full`, different decision, which is why the picker distinguishes
  them.
- **"not reported yet"** — the device has not completed a `device.hello`, so
  nothing is known. Blank rather than a guess, as before.

This display is fed by the grants `device.hello` returns plus the gateway's
`default_mode`, refreshed when the user changes a mode. It is **display only**
— every call is still re-checked against the gateway's own table, so a stale
value here can misinform but can never permit something. For the authoritative
answer, read `browser_bridge_status`'s `grants` field from a Hermes session,
or grep the audit log for `grant_set`/`grant_check`.

### The "Access mode by origin" dropdown closes on its own

**Fixed.** It was not random: the popup re-polls every 2 seconds and each list
used to rebuild by clearing its container and recreating every child, which
destroys an open `<select>` and closes its dropdown — so it shut roughly two
seconds after you opened it, every time. Lists now skip the rebuild when
nothing displayed has changed, and never rebuild a list that currently holds
focus. A change that arrives while you have a dropdown open is drawn as soon
as you close it, not dropped. Covered by
`extension/tests/popup/render-guard.test.ts`.

### The redaction checkboxes in Options don't seem to do anything

**Fixed — they used to be exactly that, and this entry used to say so.** The
three settings were written to `chrome.storage.local` and read by nothing, so
unticking one changed nothing while looking like it had. They are live now,
alongside two new kinds (email and phone, both off by default): the content
script reads the policy before each snapshot/read, and the gateway's
belt-and-braces pass honours the same policy rather than re-redacting a kind
you switched off. That last part is what makes the checkbox real rather than
cosmetic — if the gateway still re-redacted unconditionally, unticking a box
would change nothing you could observe.

Two things to be clear about before you debug one:

- **The policy is global for this device, not per-origin.** There is one set
  of five checkboxes, and they apply to every origin identically. If you were
  expecting to have turned something off for one site only, that is not what
  happened — see `security.md` §3's "Still not per-origin", and use the
  origin's **mode** if you want site-specific protection.
- **Turning a kind off is recorded.** Each change writes
  `redaction_policy_changed` to the audit log with the full before/after
  policy. Nothing blocks or second-guesses the change; it is simply durable.

If a checkbox still appears to do nothing, check in this order:

1. **Did you press Save?** The checkboxes do not apply on click.
2. **Is the kind actually matching?** Card redaction requires a Luhn-valid
   digit run, so an invoice number that merely looks card-shaped is left
   alone on purpose. Phone matching requires separators between groups, so a
   bare ten-digit run is not caught. A kind that is on but never matches
   looks identical to one that is off.
3. **Did the gateway hear about it?** Grep the audit log for
   `redaction_policy_changed` to confirm your change arrived, and for
   `redaction_gateway_catch` to see what the gateway's own pass is still
   redacting. A kind you disabled should stop appearing in the latter. If no
   `redaction_policy_changed` entry appears at all, the extension never
   reached the gateway — treat it as a connection problem, not a redaction
   one, and start from the connection entries above.
4. **Is this an older extension build?** A device that reports no policy at
   all gets every kind treated as enabled, exactly preserving the original
   unconditional behaviour. Reload the extension from `dist/` if the Options
   page predates this feature.

Full mechanism, including the wire frames and the malformed-value handling,
is in `security.md` §3.

## Silent fetch (`browser_bridge_silent_fetch`)

Full reference (grant model, config keys, spill/cache location, audit
events) is `silent-fetch.md`; these are the quick fixes for the errors you'll
actually hit.

### `SILENT_ORIGIN_NOT_GRANTED` (4257) even though the origin is `full`

Check the origin's own "Background requests" popup control before anything
else — it can override `full` in either direction (an explicit `off` row
beats `full`; an explicit "Always allow" row beats a `request`-mode origin
that would otherwise ask). If there's no per-origin override and the origin
is `full`, this code means `silent_fetch.full_implies_silent` is `false` in
`config.yaml` (default `true`) — in that case you should see one approval
prompt per session instead of a refusal, so also check
`silent_fetch.enabled` isn't set to `false`, which refuses every call
outright with this same code regardless of any grant.

### `SILENT_WORKER_LAUNCH_FAILED` (4258)

The hidden minimized window/tab couldn't be created or didn't finish its
bootstrap navigation (the origin root, or an explicit `bootstrap_path`)
within 20 seconds. Retry once — transient failures happen the same way any
tab load can be slow. If it keeps failing for one specific origin, try
passing `bootstrap_path` explicitly at a URL you know returns 200; the
default root path may be redirecting somewhere that never settles.

### `SILENT_WORKER_KILLED` (4259) mid-loop

Expected when the popup's kill button (per-worker or "stop all") or the
extension's global kill switch fired while a call was in flight. Not a bug —
retry only if the task still applies; the pool relaunches cleanly on the
next call to that origin.

### `SILENT_RATE_LIMITED` (4260) during a paging loop

Expected at the default 2 req/s, burst 10, if you're paging faster than
that. Back off for the returned `retry_after_s`, don't loop immediately.
Raising `silent_fetch.rate_per_s`/`burst` is a gateway-wide config change
(`config.yaml`, restart required), not something a single call can request.

### A big JSON response came back as `body_file` instead of `body_json`

Not a bug — `preview_chars` (default 20000) is intentionally conservative so
bulk paging doesn't blow the calling agent's context. Read `body_file` with
`execute_code` instead of raising `preview_chars`; see
`hermes_plugin/skill/references/headless-fetch.md` for the worked pattern.

### No `silent_worker` audit lines show up for a real worker launch/recycle/kill

Check that the extension build is current — a pre-SF-audit build's
`silent-pool.ts` never sends the `silent.worker` notification at all, so its
worker lifecycle is invisible past `silent_fetch`/`silent_fetch_refused`
regardless of gateway version. On a current build, every launch, recycle,
eviction, kill, user-close and SW-restart re-adopt writes a `silent_worker`
line (device, origin, `action`, `reason`); an unrecognized `action` instead
writes `silent_worker_rejected`. See `security.md` §24 / `silent-fetch.md`
§4 for the full event/action table.

## File upload (G1.3)

### `browser_bridge_upload` (tier 1, a local path) fails with "this extension's 'Allow access to file URLs' toggle is off" (`UPLOAD_FILE_URL_ACCESS_DISABLED`, 4223)

**Cause:** `DOM.setFileInputFiles` (the CDP call tier 1 uses) checks
Chrome's `MayReadLocalFiles`, which for an extension driving
`chrome.debugger` resolves to that extension's own **per-extension** file-URL
toggle — off by default for every extension Chrome installs, independent of
every other setting in this product.

**Fix, on the machine running the Chrome the extension is installed in (the
browser's own host, not the gateway):**
1. Open `chrome://extensions`.
2. Find this extension's card (enable Developer mode, top-right, if the
   card doesn't show a Details button).
3. Click **Details**.
4. Enable **"Allow access to file URLs"**.
5. Retry — no extension reload or gateway restart is needed; Chrome applies
   this immediately.

This is a one-time, per-machine, per-install setting: reinstalling the
extension (a fresh unpacked load, not just a reload) resets it and needs
this step again. Tier 2 (`content_base64`, bytes the agent already holds)
never touches `DOM.setFileInputFiles` and is unaffected by this toggle.

### `browser_bridge_upload` refuses with `UPLOAD_PATH_DENIED` even though the path looks right

**Cause, in order of likelihood:**
1. **`uploadRoots` is empty.** Enabling `allowFileUpload` alone grants
   nothing — the device must also list at least one root, one per line,
   under Options → Powers.
2. **The path isn't actually under any configured root**, once compared
   segment-by-segment (not a bare string prefix — a root
   `/Users/you/Downloads` does not match `/Users/you/Downloads-evil-twin`,
   even though one is a string-prefix of the other).
3. **The path contains a `..` segment anywhere**, or passes through a hidden
   (`.`-prefixed) or named-secret directory (`.ssh`, `.aws`, `.gnupg`,
   `keychains`, a browser profile's `User Data` folder, …) — refused even
   when the rest of the path is inside an allowed root. This is deliberate,
   not a bug to work around by renaming a directory.

**Getting a valid path:** there is no directory-listing tool. The sanctioned
route is `browser_bridge_downloads`, whose `filename` field is always an
absolute path this device just downloaded to — see `hermes_plugin/skill/SKILL.md` §6g.

### `browser_bridge_upload` (tier 2) refuses with a magic-byte / executable message

**Cause:** the decoded bytes' own signature doesn't match what was claimed
(a declared `image/png` whose bytes aren't actually a PNG), or the bytes
themselves are a native executable (`MZ`/`ELF`/Mach-O) — refused
**regardless of the declared `mime_type`**, since there is no legitimate
"upload a native executable to a web form" workflow this product supports.
This is not a false positive to retry past; if the file genuinely is what
it claims to be, double-check it wasn't corrupted (e.g. re-encoded, or
double-base64'd) before re-sending.

## Build and test

### `npx tsc --noEmit` or `npm test` fails in the extension

**Take it seriously — this entry used to tell you not to.** It previously
described 4 "confirmed pre-existing" type errors in `src/background/network.ts`
and a stale `cookies.test.ts` expectation, and advised treating both as noise.
All of those were fixed some time ago; the entry outlived them and was still
telling readers to ignore a failure it could no longer see. A doc that
pre-authorises ignoring a build failure is worse than no doc, because the next
*real* breakage gets waved through by it.

Verified on 2026-09-22 against the current tree:

```
npx tsc --noEmit    # clean, no output
npx vite build      # clean
npm test            # 366/366 pass
```

So a failure in any of the three is yours, or is new. Two things still worth
knowing when one does fail:

- `vite build` succeeding while `tsc` fails is normal and not reassuring:
  esbuild transpiles without full type-checking, so a loadable `dist/` proves
  nothing about type errors.
- If you are running builds while another agent or editor is mid-write, you
  can see transient errors in files you did not touch. Re-run once before
  investigating; if it reproduces, it is real.

## Reading this doc's own limits

Everything above was verified by reading the actual shipped code and, where
possible, exercising it against the live gateway or a local build — not
inferred from what the design docs say *should* happen. If you hit something
not listed here, the audit log is the first place to look, and the relevant
module's own docstring (most of this codebase is heavily commented with *why*,
not just *what*) is usually the second.
