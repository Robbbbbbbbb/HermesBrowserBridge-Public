# Pairing — zero to a working browser in one pass

This walks a person who has never touched this project from a fresh checkout
to a paired Chrome tab that Hermes can snapshot. Every command below was run
against the live gateway (`your-gateway-host`, Hermes v0.19.1 — see
`hermes --version` in Step 1, the version this was built and verified
against) or a local build while writing this doc.

If you just want the security/permission model, see [`security.md`](security.md)
and [`modes.md`](modes.md). If something below doesn't work, jump to
[`troubleshooting.md`](troubleshooting.md).

## What you need before you start

- SSH access to the gateway host (`your-gateway-host`) running `hermes-gateway.service`.
- A Chrome (or Chromium-family) browser, **version 116 or newer** (the extension
  uses `chrome.offscreen`, which doesn't exist before 116), on a machine that can
  reach `your-gateway-host:8765` over the LAN.
- Node 18+ and npm on whatever machine builds the extension (doesn't have to be
  the gateway — it's a static Chrome extension, build it anywhere and copy
  `dist/` over, or build it right on your laptop next to Chrome).

## Step 1 — confirm the plugin is enabled

On the gateway, `browser_bridge` ships as `~/.hermes/plugins/browser-bridge/` and
must be listed in `config.yaml` under `plugins.enabled`. On the current gateway
this is already done:

```
$ ssh your-gateway-host
$ python3 -c "import yaml; d=yaml.safe_load(open('$HOME/.hermes/config.yaml')); \
  print(d.get('plugins',{}).get('enabled')); print(d.get('browser_bridge'))"
['mnemosyne-dashboard', 'browser-bridge']
{'enabled': True, 'host': '0.0.0.0', 'port': 8765, 'default_mode': 'full', 'vision': 'auto'}
```

If you're setting this up somewhere new, add `browser-bridge` to
`plugins.enabled` and add a `browser_bridge:` block (defaults are sane — see
["Changing configuration later"](#changing-configuration-later) below for
every key and what it does). Then restart the gateway:

```
$ ssh your-gateway-host 'systemctl --user restart hermes-gateway'
```

Confirm the relay bound and the plugin loaded from the audit log (the last
`plugin_loaded` line should show `"relay_started":true` and 11 tool names):

```
$ ssh your-gateway-host 'tail -3 ~/.hermes/browser_bridge/audit.jsonl'
{"ts":...,"event":"relay_started","host":"0.0.0.0","port":8765,"protocol_version":"1.0"}
{"ts":...,"event":"plugin_loaded","version":"0.2.4","tools":["browser_bridge_status","browser_bridge_attach","browser_bridge_release","browser_bridge_snapshot","browser_bridge_read","browser_bridge_act","browser_bridge_ask","browser_bridge_screenshot","browser_bridge_fetch","browser_bridge_cookies","browser_bridge_network"],"relay_started":true,"relay_error":"","port":8765,"devices":0}
```

If `relay_started` is `false`, see **"the gateway is not listening"** in
troubleshooting.md — usually port 8765 is already taken by something else, or
`browser_bridge.enabled` is `false` in config.

## Step 2 — get a pairing code

`hermes browser-bridge pair` prints a one-time 6-digit code tied to this plugin's
own `pair`/`devices`/`revoke`/`logs`/`status`/`export` subcommands
(`hermes browser-bridge --help` lists all six). Run it on the gateway:

```
$ ssh your-gateway-host '~/.local/bin/hermes browser-bridge pair'

  Hermes Browser Bridge — pair a Chrome device
  ─────────────────────────────────────────────
  Pairing code : 929786
  Gateway URL  : ws://your-gateway-host:8765/bridge
  Valid for    : 10 minutes (single use)

  In Chrome: open the Hermes Browser Bridge popup, paste the gateway
  URL if it differs from the default, enter the code, and press Connect.

```

(Verified live on the gateway, 2026-09-22, Hermes v0.19.1 — the code above is
real but will have already expired by the time you read this; run the
command yourself to mint a fresh one.) Take the printed code and gateway URL
into Step 6.

## Step 3 — build the extension

On any machine with Node 18+ (doesn't need to be the gateway):

```
$ cd extension
$ npm install
$ npx vite build
```

Sample output (regenerated 2026-09-22 against this checkout, Node v26.7.0,
npm 11.19.0, vite 6.4.3). The module count and every file size below will
drift as the extension grows — treat the shape of the output (which entry
points exist, that it ends in `✓ built in <N>ms` with no errors) as the
thing to match, not these exact numbers:

```
vite v6.4.3 building for production...
transforming...
✓ 20 modules transformed.
rendering chunks...
computing gzip size...
dist/chunks/messages.js    0.31 kB │ gzip:  0.21 kB │ map:  10.93 kB
dist/chunks/storage.js     1.58 kB │ gzip:  0.63 kB │ map:   6.99 kB
dist/chunks/generated.js   1.87 kB │ gzip:  1.00 kB │ map:   4.18 kB
dist/options.js            2.66 kB │ gzip:  0.98 kB │ map:   5.35 kB
dist/popup.js             21.17 kB │ gzip:  5.33 kB │ map:  50.00 kB
dist/offscreen.js         41.56 kB │ gzip: 11.20 kB │ map:  97.65 kB
dist/background.js        66.29 kB │ gzip: 16.37 kB │ map: 191.32 kB
✓ built in 126ms
```

**Note on `npx tsc --noEmit`:** the documented verify loop (CLAUDE.md) also
runs `npx tsc --noEmit` before `vite build`. Re-run yourself before trusting
this: this repo has more than one workstream landing changes concurrently,
so "clean" here is a snapshot, not a standing guarantee. As last actually
run against this checkout (2026-09-22), `npx tsc --noEmit`, `npx vite
build`, and `npm test` (334 tests) all passed with no failures. If you hit a
`tsc` failure that `vite build` doesn't reproduce, esbuild transpiles without full
type-checking, so a real type error can still ship a loadable `dist/` —
don't treat a `tsc`-only failure as "the build is broken" until you've also
tried `vite build`. See troubleshooting.md.

## Step 4 — load the extension in Chrome

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked**, and select `extension/dist` (not `extension/` —
   `dist/` is the built output; `extension/manifest.json` at the repo root is
   the source manifest vite copies in, not something Chrome loads directly).
4. Chrome shows the extension card: **Hermes Browser Bridge**, manifest v3,
   permissions `debugger, tabs, tabGroups, cookies, scripting, storage,
   notifications, offscreen, alarms`, host permissions `<all_urls>`.

## Step 5 — onboard through the popup

A fresh install (or any state where the device isn't paired yet) walks you
through this directly in the popup — you don't need to visit Options first.
Click the extension's icon and it shows two steps, one at a time so the
current one is always obvious:

1. **Step 1 of 2 · Gateway URL.** A field pre-filled with the default
   `ws://your-gateway-host:8765/bridge` (baked into
   `extension/src/lib/storage.ts` as `DEFAULT_SETTINGS.gatewayUrl` — this is
   this deployment's specific gateway address; change it, including the
   `/bridge` path, if yours differs). Edit it if needed and click
   **Continue**. This is saved via the same settings the Options page uses
   (`Settings.gatewayUrl`) and marks it confirmed
   (`Settings.gatewayUrlConfirmed`), so closing and reopening the popup
   later doesn't re-ask.
2. **Step 2 of 2 · Pair this device.** Shows the exact command to run on the
   gateway (`hermes browser-bridge pair`, with a **Copy** button) and a
   6-digit code field. A **Back** link returns to step 1 if you got the URL
   wrong — the status line under the code field always shows which gateway
   you're about to connect to.

You can still set a **device name** (e.g. `my-laptop` — it's what shows up
in `hermes browser-bridge devices`) from **Options** at any point; it isn't
part of the required onboarding path since it only affects display, not
whether pairing works.

## Step 6 — pair

1. From step 2 of the popup onboarding above, type in the code from Step 2
   of this guide (the one `hermes browser-bridge pair` printed) and click
   **Connect**.
2. Expect, in order: status dot goes to `Pairing…`, then `Connecting…`, then
   **`Connected`** with a green dot, and the status line switches from
   `not paired` to the gateway URL (with `ws://` stripped) until the first
   heartbeat lands, after which it reads `heartbeat 0s ago` (updating live).
   The onboarding steps are replaced by the normal paired view once this
   happens.
3. On the gateway, confirm:

```
$ ssh your-gateway-host 'tail -3 ~/.hermes/browser_bridge/audit.jsonl'
{"ts":...,"event":"device_paired","device":"dev_...","name":"my-laptop","remote":"...","platform":"..."}
{"ts":...,"event":"device_connected","device":"dev_...","session":"ses_...","remote":"..."}
```

and (`ssh your-gateway-host '~/.local/bin/hermes browser-bridge devices'`):

```
DEVICE ID            NAME                   PLATFORM   LAST SEEN              STATUS
dev_a1b2c3d4e5f6a7b8 my-laptop            macOS      2026-09-22 05:30:00Z   active
```

If the popup sticks on `Connecting…`/`Pairing…` or flips to `Not connected`,
see troubleshooting.md's pairing section — the most common causes are a wrong
gateway URL, a firewall between your machine and `your-gateway-host:8765`, or an
expired/already-used pairing code.

**A connect attempt that can't reach the gateway no longer hangs forever.**
`Connecting…`/`Pairing…` times out after 15 seconds with an actionable
message naming the URL and what to check (gateway running, URL correct,
network reachability) — see troubleshooting.md for the exact wording. You
can also click **Cancel**, shown in the popup the whole time the state is
`connecting`/`pairing`, to abort immediately instead of waiting. **If you
cancel after entering a pairing code, treat that code as spent** — the
gateway marks a code consumed the moment it processes `device.hello`, which
happens before the extension hears back, so cancelling can't un-consume it.
Get a fresh code with `hermes browser-bridge pair` rather than retyping the
same one.

### Pairing persists — you do this once, not every session

The device token issued in Step 6 **does not expire**. It's stored in
`chrome.storage.local`, which survives a popup close, a service-worker
restart (MV3 kills the worker after ~30s idle; the token isn't in it), and a
full Chrome restart. Only two things ever remove it:

- You click **Unpair** in the popup.
- The gateway operator runs `hermes browser-bridge revoke` for this device —
  at which point the popup will say so explicitly ("this device's pairing
  was revoked — pair again to reconnect"), not just silently stop working.

Nothing else clears it — not a connect timeout, not a cancelled attempt, not
an ordinary dropped connection, not a storage read hiccup. If the popup ever
shows the plain "not paired" onboarding flow for a device you know you
already paired, that's a real revocation or an actual unpair, not a fluke —
see troubleshooting.md's "popup shows 'not paired'" section. A device that's
paired but simply can't currently reach the gateway shows **"paired, not
connected — `<gateway url>`"** instead, so the two situations don't look the
same.

## Sharing a tab

Once paired, sharing a tab is one click: open the popup and click **Share
this tab** — that attaches whichever tab you were just looking at, and
Hermes can now see and act in it. Pairing (Steps 2 and 6 above) connects the
extension to the gateway once; sharing is the separate, per-tab decision
about what that connection is actually allowed to touch, and you make it as
often as you like.

The paired popup's very first section, **Share a tab**, gives you three ways
in:

- **Share this tab** — attaches the tab you had focused right before opening
  the popup (the popup itself is never a tab Hermes could share — this
  always resolves to the ordinary browser tab behind it). A line under the
  button names that tab, so it's never a guess which one "this tab" means.
  If it's already attached, the button reads **Already sharing this tab**
  and does nothing further.
  Sharing a tab also puts it into a Chrome tab group called **Shared with
  Hermes** (cyan), so the tab strip itself shows you what is shared — you do
  not have to open the popup to check. Releasing the tab takes it back out and
  restores whatever group it was in before. Turn this off with **Highlight
  shared tabs** in Options if you would rather nothing moved in your tab strip.
- The list of **other open tabs** underneath — for something you want
  Hermes to see that isn't the tab in front of you right now. Each row shows
  the tab's favicon, title and origin with its own **Share** button. This
  list is capped and scrolls rather than growing the popup to match however
  many tabs you have open; tabs already attached, and the one you're
  currently looking at, don't appear in it (they're already covered by the
  button above and by **Attached tabs** further down).

There is deliberately no "share this tab group" button in the popup. Sharing
one tab already creates the shared group, so the common case needs one button;
sharing a group *you* already made is a deliberate, less frequent action and
lives on the right-click menu instead of taking up space in the popup.

**Right-click anywhere on a page** works too: **Share this tab with
Hermes**, and (only when that tab is grouped) **Share this tab's group with
Hermes**. This is the same attach underneath, just reachable without
opening the popup first — useful if you're already mid-task on the tab you
want to hand over. A tab the browser won't let Hermes attach to shows a
Chrome notification saying so, since a right-click has no popup open to
report back into.

**Some tabs refuse the attach, and you'll be told so either way.** A
`chrome://` page, the Chrome Web Store, and Chrome's built-in PDF viewer all
block the debugger API this bridge is built on — Chrome enforces this, not
the extension. From the popup, the error shows up right under the Share
button you clicked; from the context menu, as the notification mentioned
above. Either way it's a real refusal, not a silent no-op — if a click
seems to do nothing, check for the message.

**Sharing a group is all-or-nothing per tab, not per group.** If one tab in
a group refuses (say a `chrome://` tab got swept into an otherwise ordinary
group), the rest of the group still shares — you're told exactly which
tab(s) were left out and why, via the group item on the context menu, rather
than the group either silently reporting as fully shared or the whole action
failing because of one uncooperative tab.

**Chrome's own "Hermes Browser Bridge started debugging this browser"
banner appears on every tab the instant it's attached — this is by design,
not a bug to work around.** It's Chrome telling you, unmistakably, which
tabs an extension can currently drive; the whole point of a per-tab share
model is that it's never ambiguous which tabs that includes. One banner per
tab, even when a whole group is shared at once.

**Taking a tab back** is symmetric: each row in the popup's **Attached
tabs** list has its own **Release** button for just that tab, and **Release
all now** (further down, styled as a danger action) releases everything at
once — same as the gateway's own kill switch, and it does not unpair the
device or drop the connection. Releasing (either way) closes that tab's
debugger banner immediately; sharing it again later is just another click
on **Share this tab**.

## Pause sharing when you don't want to unpair or release tabs

Sometimes you want Hermes to stop seeing/acting *right now* — you're about to
open your bank, a payroll site, or a private document in a tab it might be
attached to or driving — without unpairing the device or releasing whatever's
attached (which you'd then have to re-attach and re-grant later). The
paired popup view has a **Pause sharing** toggle for exactly this:

- Click it, and the popup makes the state hard to miss — a "Paused" badge
  next to the status dot, the toolbar icon itself switches to a paused badge,
  and a note explains what's suspended.
- While paused, the extension refuses **every** inbound capability request —
  snapshot, read, screenshot, act, fetch, cookies, network, `ask` — before
  any of it reaches `chrome.debugger` or the page. A Hermes session sees this
  as `browser_bridge_status`'s `paused: true` and gets an actionable refusal
  ("the user paused sharing in the extension popup; ask them to resume") on
  any gated call rather than a bare failure.
- **The connection, the pairing, and any attached tabs are all left running**
  — heartbeats keep going, `chrome.debugger` stays attached, the WebSocket
  stays up. Pause suspends *data flow* only; it deliberately does not tear
  anything down, so clicking **Resume sharing** afterward costs nothing —
  no reconnect, no re-attach, no re-grant.
- Tab-activity events (which tab you switched to, which page just loaded)
  also stop going to the gateway while paused — pausing is meant to stop
  leaking what you're doing, not just what Hermes can read.
- The toggle itself is a setting (`chrome.storage.local`), so it survives a
  popup close, a service-worker restart, and a full browser restart the same
  way pairing does — if you paused and closed Chrome, sharing is still
  paused when you reopen it.

## Step 7 — grant a mode for the site you want to use

Every origin starts at `off` (the config default) — the bridge can't see or
touch anything until you say so, **per site**. In the popup's paired view,
under "Access mode by origin", type the origin (e.g.
`https://console.example.com`) and pick a mode from the dropdown:
`off` / `request` / `full`. See [`modes.md`](modes.md) for exactly what each
mode permits — short version: **attach, snapshot, read, and screenshot all
require `full`** today; `request` mode only ever gates `act`, `ask` (with
highlighted candidates), `fetch`, `cookies`, and `network` behind a popup
approval. If you just want to try this end to end, set the tab's origin to
`full`.

The popup's dropdown is a setter only — it never shows you the *current* mode
for an origin (the client has no read path for the grants table). To check
what's actually granted, read the grants list from `browser_bridge_status` in
a Hermes chat session, or the `grant_set`/`grant_check` lines in the audit log.

## Step 8 — attach a tab and run a first snapshot

From a Hermes chat session with the `browser_bridge` toolset available (it's
service-gated — it only appears once a device is paired and online):

```
browser_bridge_status
```

should list your device as `online: true` and show the grant you just set.
Then:

```
browser_bridge_attach {"url": "securityconsole"}
```

(or `tab_id`/`title`/`group_id` — see the tool's own description) attaches
the matching tab. Chrome will show its "**Hermes Browser Bridge** started
debugging this browser" banner on that tab — this is by design (plan.md §8:
"the banner scares users; document it, don't fight it"), one per attached
tab, and it means the agent's actions on that tab are visible to you, not a
malfunction.

```
browser_bridge_snapshot
```

returns a compact accessibility-style tree like:

```
[3] textbox "Search" ""
[4] button "Submit"
[7] link "Security Console" "https://console.example.com/..."
```

with `indexed_elements` telling you how many `[idx]` markers are live. That's
the "paired, working browser" this guide promised — see `hermes_plugin/skill/SKILL.md` for
how to actually drive it (click/type/fetch/etc.) from a session, and
`security.md`/`modes.md` for what each capability requires and what's
redacted along the way.

## Changing configuration later

Nothing above is a one-shot setup — every setting stays editable.

**Extension settings** (gateway URL, device name, auto-connect) live on the
**Options** page (right-click the extension icon → Options, or the "Options"
link on the popup's onboarding step 1) for the life of the install, not just
during first-run onboarding. This includes the gateway URL *after* you've
paired: if the gateway moves to a new host or port, open Options, change
**Gateway WebSocket URL**, and click **Save** — no reinstall or unpair
needed. Saving reconnects automatically if the bridge was connected (it
disconnects and immediately redials with the new URL), and the status text
next to **Save** says so (`Saved — reconnecting to the new gateway…`) when
the URL actually changed.

**Gateway settings** (everything under `browser_bridge.*` in Hermes'
`config.yaml` — pairing code TTL, device-offline timeout, default access
mode, vision handling, audit rotation, approval TTLs, screenshot retention)
are **not** editable from the extension — Chrome has no way to write to a
file on the gateway host, and this project doesn't introduce one. The
Options page has a read-only **Gateway configuration** section listing every
`browser_bridge.*` key, its default, and what it does, so you know what
exists without leaving the extension; the same list, generated from
`hermes_plugin/config.py`'s `DEFAULTS`, is below for reference. To actually
change one, edit `config.yaml` on the gateway host and restart:

```
$ ssh your-gateway-host 'systemctl --user restart hermes-gateway'
```

| Key | Default | What it does |
| --- | --- | --- |
| `enabled` | `true` | Whether the browser-bridge plugin is active at all. |
| `host` | `0.0.0.0` | Network interface the relay WebSocket server binds to. |
| `port` | `8765` | Port the relay WebSocket server listens on. |
| `path` | `/bridge` | URL path component of the WebSocket endpoint. |
| `pair_code_ttl_minutes` | `10` | Minutes a printed pairing code stays usable (single use). |
| `device_offline_after_seconds` | `90` | Seconds without a heartbeat before a device is considered offline. |
| `default_mode` | `full` | Access mode (`off` / `request` / `full`) applied to an origin you've never configured. `full` means a tab you attach is readable and drivable immediately; set `off` for per-site opt-in. |
| `vision` | `auto` | Vision handling for screenshots — `auto` probes the model's vision capability; `force_*` skips the probe. |
| `vision_model` | *(unset)* | Optional dedicated model for the vision delegation path, if different from the main model. |
| `vision_probe_ttl_hours` | `24` | Hours a probed model's vision capability is trusted before re-probing. |
| `vision_probe_max_tokens` | `512` | Max tokens allowed for the one-time vision capability probe call. |
| `vision_probe_timeout_s` | `20` | Timeout, in seconds, for the vision capability probe call. |
| `vision_screenshot_dir` | *(unset → under `~/.hermes/browser_bridge`)* | Directory saved screenshots are stored under. |
| `vision_screenshot_retention_hours` | `24` | Hours a saved screenshot is kept before being purged. |
| `vision_screenshot_max_bytes` | `200 MB` | Total retained screenshot bytes before older ones are purged. |
| `vision_ocr_enabled` | `true` | Set `false` to force-disable OCR even if pytesseract/tesseract are installed. |
| `audit_max_bytes` | `25 MB` | Audit log (`audit.jsonl`) rotation size threshold. |
| `audit_keep` | `5` | Number of rotated audit log files kept. |
| `approval_ttl_seconds` | `120` | How long a pending `request`-mode approval blocks the gateway before defaulting to deny. |
| `approval_delivery_timeout_seconds` | `10` | How long to wait for the extension to acknowledge an approval request before treating it as unreachable (denied). |
| `approval_session_grant_ttl_hours` | `12` | How long a "this session" approval keeps auto-approving the same device+origin+capability. |

## When it doesn't work

Short version — see `troubleshooting.md` for the full symptom → cause → fix
table:

- **Wrong gateway URL** — the popup's Options page gateway URL must match
  where the relay actually binds (`hermes browser-bridge status`, or check
  `config.yaml`'s `browser_bridge.host`/`port`); a typo just sits at
  `Connecting…` forever.
- **Firewall** — the extension dials *out* from Chrome to the gateway on
  `your-gateway-host:8765`; if that port isn't reachable from the Chrome machine
  (different VLAN, a host firewall on `.201`, corporate Wi-Fi blocking
  non-standard ports), you'll see `Connecting…` → `Not connected` with no
  audit line on the gateway at all (the connection never arrived).
- **Expired pairing code** — codes are single-use and expire after
  `pair_code_ttl_minutes` (10 by default). A stale/reused code fails with
  `PAIR_CODE_INVALID`; mint a new one.
- **Extension shows "not paired" after previously working** — the device was
  revoked (`hermes browser-bridge revoke`), or the gateway pushed `device.offline`.
  Pair again.
- **Gateway not listening** — `relay_started: false` in the last
  `plugin_loaded` audit line; usually port 8765 already in use, or
  `browser_bridge.enabled: false` in `config.yaml`.
