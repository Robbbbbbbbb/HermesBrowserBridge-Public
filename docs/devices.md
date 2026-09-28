# Devices — liveness, priority and resolution

Companion to `ProjectRules/devices.md` (the build plan); this is the shipped
behaviour, not the task list. Covers DV1 (liveness), DV2 (priority state),
DV3 (resolution/failover), DV4 (the agent-facing `browser_bridge_devices`
tool), DV5 (the user's CLI controls) and DV6 (the protocol fields the popup
reads).

## Why "connected" isn't enough

A WebSocket can stay open long after the extension on the other end has gone
quiet — the laptop went to sleep, the offscreen document froze, or a network
drop hasn't reached TCP's own timeout yet. Before this, `relay.status()`'s
`connected` list only knew "the socket is open"; nothing distinguished a
browser that's actually there from one that's merely still plugged in.

## The three states

| State | Meaning |
|---|---|
| `alive` | Socket open, and a heartbeat (or the `device.hello` that established the connection — it counts too) within `device_alive_after_seconds`. |
| `stale` | Socket open, but the last heartbeat is older than that. Still usable — an explicit `device_id` targeting a stale device is allowed — just not a candidate for implicit selection (DV3). |
| `offline` | No open socket at all. |

`device_alive_after_seconds` (config, default 50 — 2.5x the 20s heartbeat
interval) is clamped to `[2 * heartbeat_interval, device_offline_after_seconds]`
by `config.effective_device_alive_after_seconds()`: never so tight that
ordinary heartbeat jitter alone flips a device stale, and never looser than
the point the device gets swept offline entirely.

## The stale sweep

Every 10 seconds (`relay.STALE_SWEEP_INTERVAL_SECONDS`), the relay closes any
open socket whose last heartbeat exceeds `device_offline_after_seconds`
(default 90s), with WebSocket close code **1001 (Going Away)** — a plain "not
listening any more", not a refusal of any specific request, so it didn't need
a bridge-specific code. The close is the whole mechanism: it makes the
connection's own receive loop end exactly as a real network drop would, so
every pending gateway→extension call on it fails immediately through the
same `Connection.fail_all` path a genuine disconnect already takes — nothing
waits out its own timeout.

The sweep only ever iterates already-authenticated connections (anything
still negotiating `device.hello` isn't in the registry yet), so it can never
interrupt a pairing attempt in flight.

## Clock skew

Every `device.hello`/`device.heartbeat` frame's own protocol envelope already
carries a `ts` (no new wire field). The gateway compares that against its own
clock and keeps the difference as `clock_skew_ms` (positive = the device's
clock is behind the gateway's). A skew past ±120s is audited once per
excursion (`clock_skew_excessive`), not on every frame while it stays past
that line.

## Where it shows up

- `relay.status()["connected"][*]`: `liveness`, `last_heartbeat_at`, `clock_skew_ms`.
- `relay.liveness(device_id)` / `relay.alive_devices()`: the API DV3's
  implicit-selection resolver reads.
- `browser_bridge_status`'s per-device entries: `liveness`, `last_heartbeat_age_s`, `clock_skew_ms` (all `None`/`"offline"` for a device that isn't connected).
- `hermes browser-bridge devices` / `status`: a `LIVENESS` column, computed
  from `state.db`'s `last_seen` against the same thresholds (this command has
  no live relay to ask — it's a separate, short-lived process — so it's a
  time-based approximation, not a live read).

## Audited events

`device_stale`, `device_alive_again` (liveness transitions, each logged
exactly once whether a `status()` read or the sweep noticed it first),
`device_swept` (the sweep closing a connection), `clock_skew_excessive`.

## DV3 — resolution and failover

`tools._resolve_device(args, kwargs)` is the one function every tool handler
that takes a `device_id` argument goes through (`sessions.py`, `tabs.py`,
`session_powers.py`, `silent_fetch.py`, `vision.py`, `navigation.py`,
`dialogs.py`, `http_auth.py`, `inspect_tool.py`, `evaluate.py`, `upload.py`,
plus every M1/M2 handler in `tools.py` itself). It still returns the same
`(device_id, error_json)` pair every call site already unpacked before this
task — no call site's unpacking changed — but it now picks between several
paired devices instead of refusing outright, and it reports what it picked
through a thread-local `_wrap_handler_with_timing` (the same central wrapper
that adds `timing` to every result) splices into the JSON result.

### Selection order

1. **An explicit `device_id`, or an unambiguous device NAME** (case-insensitive;
   an ambiguous name is refused with the candidates, never guessed) — used if
   it names a known device. `alive` or `stale`, it's used (`stale: true` in
   the result for the stale case, so the caller knows to expect a possibly
   quiet extension); `offline`, refused with **4262 `DEVICE_NOT_ALIVE`** and
   the current `alive_devices`.
2. **A session that already has a device is pinned to it, UNCONDITIONALLY**
   (D0/§2 rule 6: an agent session never fails over). If this call's Hermes
   session kwarg already resolves to an OPEN `agent_sessions` row, that
   row's device is used no matter what — alive/stale, use it (stale
   flagged); revoked, entirely unrecognised (a foreign/stale identity),
   offline, or paused, refuse with 4262 (paused: 4103, see below) naming
   the specific reason, and NEVER fall through to (3)'s fresh scan. There is
   no "is this device known to this call's world" escape hatch: a session
   that was talking to a device the gateway has since forgotten (revoked,
   or otherwise) is exactly the case D0 exists to refuse, not skip past.
   A tab-bound call (`tab_id`/`tab`) is resolved through the SAME session
   (tab keys are scoped to a session's own device), so this one rule covers
   both DV3.3 cases: tab-bound and session-bound calls never fail over.
   `handle_release` passes `ignore_paused=True` (releasing a lease was never
   gated on pause, even pre-DV3, since it frees the caller's own
   bookkeeping rather than reading/driving anything) — pause never
   disqualifies or refuses a device for that one call.
3. **Otherwise** (a device-less call, or this session's very first call,
   which is what *establishes* its device): walk
   `state.ordered_candidates(session_id)` and pick the first device that is
   both `alive` and not `paused` (§2 rules 1 and 2 — a `stale` or `paused`
   device is not a candidate for IMPLICIT selection, only for an
   explicit/session-pinned target). Every higher-ranked device skipped along
   the way is recorded in `failed_over_from` with its skip reason
   (`stale`/`offline`/`paused`); `selected_by` names the tier that won
   (`session_priority`/`global_priority`/`most_recent`).
4. **Nothing alive and unpaused.** If every skipped candidate was skipped
   for being paused ALONE (none stale/offline) → **4103 `SHARING_PAUSED`**,
   the clearer, more specific refusal, listing `paused_devices`. This also
   covers the *solo* paused device case (no new friction lost there: same
   code, same hint as the tool-specific refusal it replaces at this layer).
   Otherwise (nothing alive at all, or a genuine mix of paused/offline/stale)
   → **4263 `NO_ALIVE_DEVICE`**, listing every candidate's own
   liveness/paused state AND its specific skip `reason`.

With exactly one alive, unpaused device connected and no priority order set
at all, this degrades to today's behaviour with no new friction: the sole
candidate wins immediately, `selected_by: "most_recent"`, `failed_over_from`
empty.

### The result block (DV3.2)

Every tool result produced by an IMPLICIT selection (bullets 2 and 3 above —
not an explicit `device_id`/name) carries:

```json
{"device": {"id": "dev_...", "name": "...", "selected_by": "global_priority"},
 "failed_over_from": [{"device_id": "dev_...", "name": "...", "reason": "stale"}]}
```

`failed_over_from` is only present when non-empty. An explicit selection
carries neither key. `stale: true` is added whenever the device actually
used (explicit, session-pinned, or implicit) is stale.

### Audit

`device_selected` is recorded only when a failover happened or a non-default
order actually decided the pick (`selected_by != "most_recent"` or
`failed_over_from` non-empty) — not on every call. `device_selection_refused`
covers the 4262/4263 refusal paths.

### Error codes

| Code | Name | When |
|---|---|---|
| 4261 | `DEVICE_PRIORITY_PINNED` | An agent write tried to reorder the pinned GLOBAL priority order (DV4's `set_priority`). |
| 4262 | `DEVICE_NOT_ALIVE` | An explicit `device_id`/name, or the device a session/tab-bound call is unconditionally pinned to, is revoked, unrecognised, or offline. |
| 4263 | `NO_ALIVE_DEVICE` | An implicit (device-less) call found no alive, unpaused device among every paired one, and it wasn't a paused-only case (see 4103 below). |

A pinned session/tab-bound target, or every alive candidate of an implicit
scan, that is disqualified ONLY by being paused gets **4103
`SHARING_PAUSED`** instead of 4262/4263 — the same code and hint a
capability-specific refusal (`_paused_refusal`) already used; this layer
simply reaches it first now, before the tool's own check would.

## DV4 — the agent tool `browser_bridge_devices`

`hermes_plugin/devices_tool.py` registers one tool, `browser_bridge_devices`,
with a single `action` parameter (`list` default, `set_priority`,
`clear_priority`). It never renames or revokes a device (D1) — that stays a
CLI-only command (DV5).

### `list`

Every paired, non-revoked device: `device_id`, `name`, `browser`,
`platform`, `ext_version`, `protocol_version`/`protocol_up_to_date`,
`liveness` (`alive`/`stale`/`offline`), `last_heartbeat_age_s`,
`clock_skew_ms`, `paused`, `global_rank` (its 0-based index in the global
order, or `null` if unranked), `session_rank` (this call's own session
override rank, or `null`), and `current_pick: true` on the one device an
implicit call would select right now. Never a device row's `token_hash` or
raw token — only the display fields above are ever copied out. A revoked
device is absent entirely (`state.list_devices()`'s own default).

`current_pick` is computed by calling `tools._resolve_device` itself —
literally the same function every other tool's implicit selection goes
through, not a re-implementation — with a new `audit_writes=False` keyword
that suppresses every `device_selected`/`device_selection_refused` audit
line _resolve_device would otherwise write, since a `list` preview never
made a real call. Its thread-local resolution meta is reset immediately
after reading the device id, so a preview can never leak a stray `device`/
`failed_over_from` block into `browser_bridge_devices`'s OWN result via
`_wrap_handler_with_timing`'s splice (DV3.2's same central place).

`list` also returns `priority: {global: [...], session: [...], pinned,
pinned_by}` — the stored global order, this call's own session override (or
`[]` with no session identity), and the pin state (`state.get_priority_pin`).
`list` is never audited (a read has no side effect worth a line).

### `set_priority {order, scope}`

`order` is a list of device ids OR names, most-preferred first. A name
resolves case-insensitively against every non-revoked paired device; an
ambiguous or unknown name is refused (`candidates` lists every match) —
never guessed. Duplicates are rejected. Both checks, plus "unknown/revoked
device id", are `state.py`'s own `_validate_priority_order` (DV2) — this
module only adds the name→id step on top.

- `scope: "global"` writes with `set_by="agent"`. While the user has the global
  order pinned, `state.set_global_priority` raises `PriorityPinnedError`,
  which this tool turns into **4261 `DEVICE_PRIORITY_PINNED`** with a hint
  that only the operator can change it — and writes nothing.
- `scope: "session"` writes the calling session's own override
  (`state.set_session_priority`), keyed on the SAME raw Hermes session
  identifier DV3's own `_resolve_device` reads for its session-priority tier
  — not a resolved `agent_sessions.id`. This needs a session identity at
  all (a call with no session kwarg is refused with a clear error); a raw
  key that has never yet been bound to a device is accepted and USEFUL —
  devices.md D0/§2 rule 6 pins a session to its device from its very first
  real `browser_bridge_*` call onward, so calling `set_priority` with
  `scope: "session"` BEFORE that first call is the only time a session
  override can actually steer which device that first call binds to. A
  write against an already-bound session is accepted too, just inert for
  that session's own future picks (D0 never re-homes it). A closed session
  is refused.

Both scopes return the new `priority` block plus the new `current_pick`
(same computation as `list`'s, one device id or `null`).

### `clear_priority {scope}`

`scope: "session"` clears the calling session's own override
(`state.clear_session_priority`) and returns the refreshed `priority`/
`current_pick`, same shape as `set_priority`. `scope: "global"` is **always
refused** — clearing the operator's global order from the agent is not
allowed regardless of pin state; the refusal uses the same
`DEVICE_PRIORITY_PINNED` code and hint as a pinned `set_priority` write,
since both say "only the operator can change this."

### Audit

Every write: `device_priority_set` (`scope`, `set_by="agent"`, `order`,
`session` — `None` for a global write) on success; `device_priority_refused`
(`reason`: `"pinned"` for the 4261 case, `"operator_only"` for an agent's
global `clear_priority`) on refusal. `list` is never audited.

### The manual

`hermes_plugin/skill/SKILL.md` gets one short paragraph (after the
`browser_bridge_status` guidance) plus a routing-table row: when several
devices are paired, call `browser_bridge_devices`; to prefer one for a task,
call `set_priority` with `scope: "session"` as the FIRST tool call (per the
binding rule above); watch `failed_over_from` on any result and tell the
user when a preferred device was skipped; a write against the pinned global
order is refused with 4261.

### Tests

`tests/test_dv4_devices_tool.py` — a real relay plus three `FakeExtension`
devices (the `test_dv3_resolution.py` pattern). Covers `list`'s fields and
`current_pick` (checked against a real `_resolve_device` call made
separately), a revoked device hidden, no secret fields, a global
`set_priority` changing the next implicit pick, a pinned global write
refused with the stored order unchanged, case-insensitive name resolution
and an ambiguous-name refusal (order unchanged), a session override beating
the global order (and needing a session identity at all), `clear_priority`
restoring the global order, an agent's global `clear_priority` refused, the
audit lines, and a rejected duplicate. Two red/green proofs: removing the
`except state.PriorityPinnedError` catch (the pinned write then raises
instead of refusing cleanly) and swapping `current_pick`'s call to the real
`_resolve_device` for a naive re-implementation (its answer then disagrees
with what an implicit call actually picks).

## DV5 — the user's CLI controls

`hermes browser-bridge devices` gained a RANK column and a pin summary,
plus two sub-subcommands: `devices priority` and `devices rename`. Every
write this section describes goes through `state.py` with `set_by`/`by`
`"operator"` — the CLI is never subject to DV2's pin the way an agent write
is (§2 rule 5: "The CLI and popup can always write").

### Listing: RANK and the pin marker

```
$ hermes browser-bridge devices
Global priority: pinned by operator
RANK  DEVICE ID            NAME                   PLATFORM   LAST SEEN              LIVENESS STATUS
#1*   dev_1a2b3c4d5e6f7890  my-laptop            mac        2026-09-28 14:02:11Z   alive    active
#2*   dev_2b3c4d5e6f789012  my-desktop            mac        2026-09-28 13:58:44Z   alive    active
-     dev_3c4d5e6f78901234  win-laptop             windows    2026-09-27 09:11:02Z   offline  active

* part of the pinned global order
```

`--json` extends each device's existing fields with `rank` (this device's
1-based position in the global order, or `null`) and `pinned` (the global
order's pin state, the same value on every row — pinning applies to the
order as a whole, not per device).

### Setting, clearing and pinning priority

```
$ hermes browser-bridge devices priority my-laptop my-desktop
Global priority set: dev_1a2b3c4d5e6f7890, dev_2b3c4d5e6f789012

$ hermes browser-bridge devices priority my-laptop my-desktop --pin
Global priority set: dev_1a2b3c4d5e6f7890, dev_2b3c4d5e6f789012
Global priority pinned.

$ hermes browser-bridge devices priority --unpin
Global priority unpinned.

$ hermes browser-bridge devices priority --clear
Cleared the global device priority order.
```

- Targets may be device ids or names; a name resolves case-insensitively
  against paired, non-revoked devices. An unknown or ambiguous name is
  refused outright — an ambiguous one lists every matching device id and
  name so the operator can retype with the id — never guessed.
- `--pin`/`--unpin` may be combined with a new order (applied after the
  order is set) or given alone, which toggles the pin without touching
  whatever order is already stored. Re-pinning an already-pinned order (or
  unpinning an already-unpinned one) is a no-op: no duplicate audit entry.
- `--clear` cannot be combined with an explicit order.
- Every write is audited: `device_priority_set` (`order`, `set_by:
  "operator"`, `source: "cli"`) and `device_priority_pin_changed` (`pinned`,
  `by: "operator"`, `source: "cli"`), the latter only on an actual change.
- While the order is pinned, an agent's own write
  (`state.set_global_priority(..., set_by="agent")`) still raises
  `PriorityPinnedError` (DV2); the CLI's own writes above are `set_by:
  "operator"` and are never subject to that check.

### Renaming (D1: CLI/popup only, never the agent)

```
$ hermes browser-bridge devices rename win-laptop "the user's Windows laptop"
Renamed dev_3c4d5e6f78901234: 'win-laptop' -> "the user's Windows laptop".
This rename is PERMANENT -- it survives every future reconnect and is never
overwritten by the extension's own self-reported name, until `hermes
browser-bridge devices rename dev_3c4d5e6f78901234 --clear` drops the
override. A running gateway's live connection (relay.py's
Connection.device_name) picks up the new name on this device's next
device.hello; every other read (state.db, `devices`, `browser_bridge_status`)
sees it immediately, since they all read the devices table directly.

$ hermes browser-bridge devices rename win-laptop --clear
Cleared dev_3c4d5e6f78901234's name override (was "the user's Windows laptop").
The device's own self-reported name (from its extension settings) applies
again starting with its next device.hello/device.heartbeat.
```

- The target resolves the same way `priority`'s targets do (id or
  case-insensitive name).
- The new name is 1-64 printable characters; control characters are
  stripped before that check (so `"the user\x07 Laptop"` becomes `"the user
  Laptop"`), not counted toward the length limit. A name that is empty
  after stripping, or still over 64 characters, is refused — never
  truncated or otherwise coerced.
- **The rename is permanent, not a one-time write.** It is backed by a new
  `device_name_override` table (`state.py`; `CREATE TABLE IF NOT EXISTS`
  only), not just the `devices.name` column. Without this table, every
  `device.hello`/`device.heartbeat` calls `touch_device(device_id,
  name=<the extension's own self-reported name>, ...)`, which used to
  overwrite `devices.name` right back on the device's very next reconnect
  — the CLI's rename appeared to work and then silently reverted within
  one heartbeat interval, with no error. `touch_device` now skips the
  `name` column entirely while an override row exists; `set_by`/`by:
  "operator"` and `set_at` are recorded alongside it. `devices.name` itself
  is still kept in sync on every write, for any reader that queries that
  column directly.
- Every name-showing read path returns the override while it's active:
  `hermes browser-bridge devices`, `browser_bridge_status`,
  `browser_bridge_devices`'s agent-facing `list`, and DV3's own device-NAME
  resolution in `tools._resolve_device` — all of them read through
  `state.list_devices()`, which overlays the override onto `name` in one
  place, so no caller needs to know the override table exists. The live
  gateway's in-memory `Connection.device_name` (relay.py, used while the
  socket is open) is refreshed to the override at hello time too, so it
  never regresses to the extension's raw report while the socket stays
  open.
- `devices rename <id|name> --clear` drops the override; `devices.name`
  itself is left as it is by the clear (no reset to any prior value) —
  the device's own self-reported name takes over again starting with its
  next `device.hello`/`device.heartbeat`, not immediately.
- Revoking a device drops its override in the same transaction as the
  revoke (an id can never be re-paired, so nothing is left to apply it to).
- Audited as `device_renamed` (`device`, `before`, `after`, `by:
  "operator"`, `source: "cli"`) and `device_rename_cleared` (`device`,
  `before`, `by: "operator"`, `source: "cli"`).

## DV6 — protocol: read-only priority fields

`device.hello` and `device.heartbeat` results both gained three fields,
purely additive (no `PROTOCOL_VERSION` bump — every existing field's meaning
is unchanged, and an older extension build that doesn't read them keeps
working exactly as before):

| Field | Type | Meaning |
|---|---|---|
| `device_rank` | integer or `null` | This device's 1-based position in the global priority order (DV2), or `null` if it has no explicit rank. |
| `priority_pinned` | boolean | Whether the global order is currently pinned by the operator. |
| `device_alive_after_s` | integer | The same `effective_device_alive_after_seconds` threshold DV1's own liveness check uses. |

Resent on every hello **and** heartbeat (the same "an operator's CLI change
must be visible with no reconnect required" reasoning `default_mode` and the
Silent Fetch worker-pool fields already follow) — `relay.py`'s
`_priority_hello_fields(device_id)` is the single point that assembles them
for both. Read-only and display-only: the gateway alone still decides which
device answers an implicit call (DV3); these fields never widen or narrow
what a device may do.

The extension absorbs them the same way it absorbs `grants`/`default_mode`
(`offscreen/client.ts`'s `absorbGrants`), and the popup's Host / Net panel
renders them read-only, next to the gateway URL:

```
Priority #1 (pinned by operator)
Priority not ranked
```

No write control ships in the popup in this pass — `hermes browser-bridge
devices priority` is the write path.
