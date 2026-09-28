# Security model — as shipped

This describes what the code actually does, verified by reading
`hermes_plugin/{state,relay,approvals,tools,session_powers,vision,audit,config}.py`
and the extension's `content/redaction.ts`, `background/{cdp,act,fetch,cookies,network}.ts`.
**Updated 2026-09-22** for the approval-transport migration below — the
gateway actually runs **Hermes v0.21.4** (`/usr/local/lib/hermes-agent`); an
earlier pass of this document (and of `hermes_plugin/approvals.py`) was
written against `~/.hermes/hermes-agent`'s v0.19.1, a stale secondary install
that an interactive shell's `PATH` happens to find first. Where the two
disagree, this file says so and the code wins. If you want "what should I
grant a given site," see [`modes.md`](modes.md) instead; this file is "how is
that enforced, and what are the actual holes."

## Contents

1. [The three per-origin modes](#1-the-three-per-origin-modes)
2. [Grants are enforced gateway-side, not by the popup](#2-grants-are-enforced-gateway-side-not-by-the-popup)
3. [Redaction: on by default, user-configurable per kind, enforced end to end](#3-redaction-on-by-default-user-configurable-per-kind-enforced-end-to-end)
4. [The CDP method whitelist](#4-the-cdp-method-whitelist)
5. [The SSRF guard on `browser_bridge_fetch`](#5-the-ssrf-guard-on-browser_bridge_fetch)
6. [Device tokens: hashed at rest, cleartext on the wire and in the browser](#6-device-tokens-hashed-at-rest-cleartext-on-the-wire-and-in-the-browser)
7. [What the audit log does and does not contain](#7-what-the-audit-log-does-and-does-not-contain)
8. [Pairing-code brute-force protections](#8-pairing-code-brute-force-protections)
9. [The kill switch](#9-the-kill-switch)
10. [Residual risks, stated plainly](#10-residual-risks-stated-plainly)
11. [`Fetch.*` interception stays banned](#11-fetch-interception-stays-banned--this-plan-does-not-lift-it)
12. [Per-capability ceiling and the operator kill switch (G0.5 / G0.9)](#12-per-capability-ceiling-and-the-operator-kill-switch-g05--g09)
13. [Cookie writing (G3.6)](#13-cookie-writing-g36)
14. [`type`'s own params (G1.6)](#14-types-own-params-g16--no-new-capability-same-act-gate)
15. [Downloads: read-only, origin-scoped, and a permanent never-list (G3.5)](#15-downloads-read-only-origin-scoped-and-two-calls-this-plugin-never-makes-g35)
16. [JS dialogs: observed and reported, not auto-dismissed (G1.4)](#16-js-dialogs-observed-and-reported-not-auto-dismissed-g14)
17. [Node-handle targeting (G2.6.2–.4)](#17-node-handle-targeting-backendnodeid-content-quads-and-the-narrowed-toctou-g262-4)
18. [Drag and drop (G1.2)](#18-drag-and-drop-g12--no-new-capability-slider-captchas-stay-out-of-scope)
19. [File upload (G1.3)](#19-file-upload-g13--two-tiers-two-settings-each-checked-at-its-own-entry-point)
20. [Arbitrary JS evaluation (G1.5)](#20-arbitrary-js-evaluation-g15--the-devtools-console-handed-to-the-agent)
21. [Iframes: every frame is gated by its OWN origin (G2.2.13)](#21-iframes-every-frame-is-gated-by-its-own-origin-g2213)
22. [Limited (no-debugger) share mode](#22-limited-no-debugger-share-mode--automatic-fallback-same-security-gates)
23. [Replay: a local, off-by-default recording of what the agent did (G7)](#23-replay-a-local-off-by-default-recording-of-what-the-agent-did-g7)
24. [Silent fetch: a tab-less, unattended request lane (SF1–SF7)](#24-silent-fetch-a-tab-less-unattended-request-lane-sf1sf7)
25. [Origins are compared canonically everywhere a grant is looked up](#25-origins-are-compared-canonically-everywhere-a-grant-is-looked-up)

## Capability matrix

One row per `protocol/schema.json` `approval.request.capability` enum value.
**Setting/default** verified against `extension/src/lib/storage.ts`'s
`DEFAULT_SETTINGS`; **ceiling** and **standing grant** verified against
`hermes_plugin/approvals.py`'s `DANGEROUS_CAPABILITIES` (the ceiling set) and
`NO_STANDING_GRANT_CAPABILITIES` (the never-`always`/`session` subset);
**setting name** verified against `hermes_plugin/tools.py`'s
`_CAPABILITY_POWER_KEYS`.

| Capability | Tool(s) | Device setting (default) | Ceiling above `full`? | Standing grant | Enforced both layers? | What it exposes |
|---|---|---|---|---|---|---|
| `snapshot` | `browser_bridge_snapshot` | — (mode only) | No — `off`/`request` refuse outright, `full` allows | n/a (not approval-routed) | Gateway only (`_gate_reason`) | Accessibility-style tree of the page |
| `screenshot` | `browser_bridge_screenshot` | — (mode only) | No, same rule as `snapshot` | n/a | Gateway only (`_gate_reason`) | Pixels (or an OCR/layout description for a non-vision model) |
| `read` | `browser_bridge_read` | — (mode only) | No, same rule as `snapshot` | n/a | Gateway only (`_gate_reason`) | Page text/markdown |
| `act` | `browser_bridge_act` | — (mode only) | No | Approval scope (once/session/always→`full`) | Gateway (`_authorize`) + extension (dispatch, `expect`/viewport checks) | Clicks/types/etc.; `diff`/`fieldValue` |
| `cdp` | — (no tool sends this) | — | — | — | Allowlist exists gateway-side; **unreachable** — no extension handler for `cdp.send` (§4, §11) | Nothing today |
| `fetch` | `browser_bridge_fetch` | — (mode + SSRF guard) | No | Approval scope | Gateway (`_authorize` + `_ssrf_guard`) + extension (`validateUrl` pre-check, not a boundary) | Response body via the page's own session |
| `cookies` | `browser_bridge_cookies` | — (mode only) | No — `include_values` uses the same approval path as presence-only | Approval scope | Gateway (`_authorize`) | Cookie metadata, and values if `include_values`+approved |
| `network` | `browser_bridge_network` | — (mode only) | Bodies specifically need standing `full` (§1a) even after an approval | Approval scope (metadata only) | Gateway (`_authorize` + a direct `full` re-check for bodies) | Request/response metadata; bodies only at `full` |
| `open_tab` | `browser_bridge_open_tab` | — (mode only) | No | Approval scope | Gateway (`_authorize`) | Opens and attaches a new tab at an http(s) URL |
| `upload` | `browser_bridge_upload` | `allowFileUpload` (off, tier 1) / `allowFileUploadFromAgent` (off, tier 2) | **Yes** | **Never** (`NO_STANDING_GRANT_CAPABILITIES`) | Extension (`powers.ts` per-tier check, path allowlist / magic-byte sniff) + gateway (same checks, mirrored) | A local file's contents, handed to the page |
| `evaluate` | `browser_bridge_evaluate` | `allowEvaluate` (off) | **Yes** | **Never** (`NO_STANDING_GRANT_CAPABILITIES`) | Extension (wall-clock timeout, result redaction) + gateway (redaction, audit) | Full page JS execution — cookies, storage, `fetch()`, DOM (§20) |
| `cookies_write` | `browser_bridge_cookie_set` | `allowCookieWrite` (off) | **Yes** | once/session/always | Extension (`assertCookieWriteAllowed`, `attachedOrigins()` bound) + gateway (`_device_power_denial`, fresh `tabs.list` bound) | Writes/overwrites one named cookie |
| `http_auth` | `browser_bridge_http_auth_status` | `allowHttpAuth` (off) | **Yes** | **Never** (`NO_STANDING_GRANT_CAPABILITIES`) | Read-only; arming itself is extension-only, no wire method exists for it | Whether a credential is armed for an origin (never the credential) |
| `dialog` | `browser_bridge_dialog` (`accept` only — dismiss is ungated) | `allowDialogAccept` (off); `allowDialogDismiss` (on, not capability-gated) | **Yes** (accept only) | once/session/always | Extension (`ack_message` exact-match) + gateway (approval routing) | The dialog's own message text; accept/dismiss action |
| `downloads` | `browser_bridge_downloads` | `allowDownloadsRead` (**on**) | **Yes** | once/session/always | Extension (static never-list scan, origin scoping) + gateway (`_device_power_denial`) | Download history metadata, scoped to attached origins |
| `console` | `browser_bridge_console` | `allowConsoleRead` (off) | **Yes** | once/session/always | Extension (`assertConsoleReadAllowed`, unconditional secret redaction) + gateway (redaction re-check) | console.log/warn/error, exceptions, browser-generated messages |
| `silent_fetch` | `browser_bridge_silent_fetch` | — (per-origin popup "Background requests": off/ask/always, plus `silent_fetch.full_implies_silent` config default) | No | Session-scoped only (`ask` → once/session via `session_grants`; never promotes to a standing `always` row) | Gateway only (`silent_grants.authorize_silent_fetch` + its own SSRF guard + rate guard); the extension's worker pool trusts nothing but its own kill switch | Response body/headers via a hidden worker tab's own request — no tab ever attached or visible |

## 1. The three per-origin modes

Set per origin, per device, in the extension popup, and stored in
`state.db`'s `grants` table (`device_id, origin, mode`). An origin with no row
falls back to `browser_bridge.default_mode` in `config.yaml`.

**`default_mode` ships as `full` (changed 2026-09-22 at the user's request; it was
`off`).** This is the single widest-reaching setting in the product and it is
worth being precise about what it does and does not mean:

- It means an origin the user has never configured is **readable and drivable
  the moment a tab on it is attached** — no per-site opt-in, no approval
  prompt. `request` mode's approval cards do not appear for it, because
  `request` is not what it inherits.
- It does **not** widen what Hermes can reach on its own. Nothing happens on
  any tab the user has not attached, and attaching is a user action (popup or
  context menu) or a gateway call that is itself gated. The mode governs what
  may be done with an attached tab; it does not attach anything.
- It does **not** widen `browser_bridge_fetch`'s cross-origin reach. That path
  deliberately ignores `default_mode` entirely — an ungranted cross-origin
  target is always refused, whatever the default is (see §5 and the mode
  table in `modes.md`).
- An origin the user explicitly sets to `off` still refuses everything. An
  explicit answer always beats the default.

Set `browser_bridge.default_mode: off` in `config.yaml` and restart the
gateway to go back to per-site opt-in.

- **`off`** — every capability refused outright. You cannot even attach a tab
  whose current origin is `off`.
- **`request`** — capability-dependent, and this is the part that surprises
  people coming from `plan.md`'s original design table:
  - `browser_bridge_attach`, `browser_bridge_snapshot`, `browser_bridge_read`,
    and `browser_bridge_screenshot` are **refused outright**, exactly like
    `off`, with a message pointing at switching the origin to `full`. This is
    `tools.py`'s `_gate_reason()` gate, and its own docstring says why: it's
    the M1 gate, written before the M2 approval queue existed, and it was
    deliberately never upgraded to use that queue — reads never park for
    approval in this codebase, full stop.
  - `browser_bridge_act`, `browser_bridge_ask` (only when it carries
    `candidates`, which reveal page content), `browser_bridge_fetch`,
    `browser_bridge_cookies`, and `browser_bridge_network` go through
    `tools.py`'s `_authorize()` gate instead, which calls
    `hermes_plugin/approvals.py`'s `require()` and blocks the calling thread
    until the user answers in the popup or it times out. As of the
    native-transport migration (§1b), `require()` presents through the same
    popup as before but is built on Hermes' own
    `hermes_cli.approval_transport` primitives (`ApprovalRequest` /
    `invoke_approval_transport`) instead of a hand-rolled sqlite queue — see
    §1b for what that changes and what it doesn't.
- **`full`** — every capability allowed immediately, no approval, no
  standing extra check (except the two exceptions in §1a below).

### 1a. Two things that need `full` even when an approval said yes

- **`browser_bridge_network`'s bodies.** An approved `request`-mode call to
  `browser_bridge_network(include_bodies=true)` still gets metadata only
  (`method`/`url`/`status`/`resourceType`) — `session_powers.py`'s
  `handle_network` checks `state.get_mode(device_id, origin) == "full"`
  directly, independent of whatever `_authorize()` just decided, and reports
  why in `bodies_omitted_reason` when it downgrades. A one-off approval is not
  the same thing as a standing grant, by design.
- **`browser_bridge_cookies`' values are *not* in this bucket** — despite
  looking like they should be. `include_values=true` goes through the same
  `_authorize()` approval path as everything else in this list; a `request`-mode
  approval (`once`/`session`/`always`) does reveal cookie values, not just
  presence/metadata. If your threat model assumed cookie values needed a
  standing `full` grant the way network bodies do, they currently don't — flag
  this if it should.

See [`modes.md`](modes.md) for the full capability × mode table.

### 1b. `request`-mode approvals: Hermes' native transport, not a bespoke queue

`hermes_plugin/approvals.py` no longer owns a persisted, hand-rolled approval
queue. It presents through **Hermes' own approval-transport contract**
(`hermes_cli/approval_transport.py`, v0.21.4): `ApprovalRequest`/
`invoke_approval_transport` own request construction, digest binding, the
bounded worker thread, timeout enforcement, and decision validation
(stale/invalid/late responses are rejected by the **host**, not by anything
in this plugin). This plugin still owns exactly two things the host can't:
presenting the request as an `approval.request` wire frame the extension
already understands, and correlating the eventual `approval.respond` back to
the thread waiting on it.

**This plugin's own `request`-mode capability gate (`act`/`fetch`/`cookies`/
`network`/`ask`) always works this way, unconditionally** — it does not
depend on any operator configuration. `require()` builds its own
`ApprovalRequest` and drives `invoke_approval_transport` directly, rather
than going through Hermes' `security.approval.transport` selection gate,
because that gate's `ApprovalRequest` has no `origin`/`capability` fields
(protocol/schema.json's `approval.request` requires both, with `capability`
enum-constrained) — those only exist at this plugin's own call site. **There
is no "fallback" scenario where this plugin's approvals stop working because
an operator forgot a config flag**; the popup's approval inbox behaves
exactly as it did before the migration.

**What operator config *does* control:** this plugin also registers itself
as a general Hermes approval transport —
`ctx.register_approval_transport("browser-bridge", approvals.present)`,
called from `register()` and guarded so a registration failure can never
break plugin load. **It stays completely inactive until the operator
explicitly sets, in Hermes' own `config.yaml`:**

```yaml
security:
  approval:
    transport: browser-bridge
```

Once set, Hermes routes **its own, unrelated** human-approval needs — a
dangerous shell command, a plugin's `request_tool_approval` escalation, MCP
elicitation on the CLI/gateway path — through this popup too, instead of the
CLI prompt or whatever chat surface is driving that session. Those requests
have no real browser origin or capability (there's nothing browser-specific
about "may I run `rm -rf /tmp/x`"), so they show up under a fixed pseudo
origin (`hermes-agent://approval`) and pseudo capability (`act`) with the
real, already-redacted command/description text in the summary/detail
fields. **This plugin never writes that config key itself** — selecting
"browser-bridge" as the system-wide transport is an explicit, opt-in
operator action, not something this migration turns on silently.

**What got deleted, and why it's safe to have deleted it:** the old
`state.db` `approvals` table (pending-request rows with their own TTL/status
columns) and the functions built around it (`create_approval`,
`resolve_approval`, `list_pending_approvals`, `expire_due_approvals`,
`reap_all_pending`) are gone, along with the "reap orphaned approvals on
relay startup" step. That table existed only to survive a gateway restart so
`browser_bridge_status` wouldn't show a phantom pending row. Since
`invoke_approval_transport`'s wait is entirely in-process (a bounded worker
thread, never persisted), and a gateway restart kills the blocked
tool-call thread that owned a pending approval exactly like it kills
everything else in that process, there is nothing left to survive a restart
any more — an empty in-memory table after a restart is simply the truth, not
a phantom to sweep. `state.db`'s `session_grants` table (the `session`
scope's "don't ask again this Hermes session, bounded by a TTL safety net"
behavior) is **kept** — it is bridge-specific policy (keyed by
device+session+origin+capability, with no reliable session-end hook to key
off instead; see `Documentation/plugin-api-findings.md`), not something
Hermes' own approval engine has an equivalent for. Hermes' own
once/session/always persistence (used by the dangerous-command gate this
plugin does *not* call into) is keyed by `session_key`+`pattern_key` only,
with no notion of "origin," and its "always" writes into the **operator's
own `command_allowlist`** in `config.yaml` — the wrong place for a per-origin
browser grant, which is the concrete reason `require()` builds its own
`ApprovalRequest` rather than calling `tools.approval.request_tool_approval`.

**A known, deliberate, harmless trade-off:** the host's `ApprovalDecision`/
`ApprovalTransportResult` types carry only a scope choice
(`once`/`session`/`always`/`deny`) or a short failure code (`timeout`/
`busy`/`interrupted`/`error`/`invalid`/`stale`) — no free-text reason field.
A disconnect-triggered auto-deny, an unreachable-device auto-deny, and a
genuine human "deny" click all therefore collapse to the same `scope: deny`
once they've round-tripped through the host's contract; `approvals.py`
recovers the specific wording (e.g. "the device disconnected before the user
answered") through a small in-process side-channel
(`approvals._deny_meta`) precisely because the host doesn't have a slot for
it, but that channel is best-effort, not authoritative — `Decision.scope`/
`Decision.allowed` (what `tools.py`'s `_authorize()` actually branches on)
are always correct regardless.

## 2. Grants are enforced gateway-side, not by the popup

`state.py`'s own docstring: *"the popup is UX; this database is law."* Every
capability call re-reads `state.get_mode(device_id, origin)` (or, for
cross-origin `fetch`, `state.list_grants()` directly — see §5) on the gateway
before dispatching anything to the extension, regardless of what the popup UI
last showed. A compromised or modified extension build cannot widen its own
access by lying about the user's selection — the worst it can do is *refuse*
to enforce a mode locally, which the gateway's own check still catches. Every
decision, allow or deny, is written to the audit log under `grant_check`.

**Screenshot is gated like the other read-only tools.** `handle_screenshot`
(`vision.py`) calls `_gate_reason(device_id, origin, "screenshot")` before it
dispatches `page.screenshot`, resolving the origin from the attached tab's
current URL. `full` allows; `off` and `request` refuse, with no request ever
reaching the extension.

It deliberately uses `_gate_reason` (the read-only gate) rather than
`_authorize` (the approval-routing gate used by `act`): a screenshot discloses
the same page a snapshot does — more of it, in fact, since redaction cannot
blank what a canvas has already painted — so routing it through approvals while
`snapshot` refuses outright would make the *more* revealing tool the easier one
to reach.

The check is re-read from the grants table on **every call**, which is what
makes a mid-attach downgrade work. Attaching requires `full` (§1), but attach
state outlives the grant: a tab stays attached until released or reconciled. If
you set that origin back to `off` or `request` while the tab is still attached,
`snapshot`, `read` and `screenshot` all begin refusing on their very next call.
Earlier builds gated the first two and not the third, which left screenshot as
a way to keep reading a page you had just revoked; regression tests now cover
the full downgrade sequence (grant → attach → downgrade → refuse → restore →
allow) in `tests/test_m1_vision.py`.

## 3. Redaction: on by default, user-configurable per kind, enforced end to end

Two independent layers. Neither is unconditional any more — both honour a
per-kind policy the user sets once, in the extension's Options page, that
applies globally (not per-origin: see "still not per-origin" below):

1. **In the extension**, before a frame ever leaves the machine
   (`content/redaction.ts`, run from `content/walker.ts` on every text/value
   node the DOM snapshotter emits, and from `content/reader.ts` on `read`
   output). Five kinds, each an independent checkbox in Options
   (`popup/options.ts`), each mapped to a `Settings` field
   (`lib/storage.ts`):
   - **Password fields** (`redactPasswords`, default **on**) — matched by
     field *identity*, not content: `type="password"`, an `autocomplete` of
     `current-password`/`new-password`, or a `name`/`id` matching
     `/pass(?:word|code)?|pwd|secret|pin(?:code)?/i`. The entire value
     becomes the marker regardless of what it contains — "we don't wait to
     see a password-shaped string, because passwords have no fixed shape."
   - **Credit-card-shaped values** (`redactCreditCards`, default **on**) — a
     digit run of 13–22 characters (allowing single space/dash separators) is
     only redacted if, after stripping separators, it's 13–19 digits **and
     passes a Luhn checksum** — this is specifically to avoid flagging every
     long order/tracking/phone number on an admin page as a card.
   - **SSN-shaped values** (`redactSsn`, default **on**) — `\d{3}[- ]\d{2}[- ]\d{4}`,
     no Luhn-equivalent validation (there isn't one for SSNs), so this is
     shape-only and can false-positive on similarly-shaped IDs.
   - **Email addresses** (`redactEmails`, default **off**) — a standard
     `local@domain.tld` shape requiring a dotted, letters-only TLD, which is
     also what keeps it off a Slack/Twitter-style `@handle` or a
     version-pinned package spec like `left-pad@1.2.3`. Off by default
     because ordinary pages mention addresses constantly — turning it on
     trades page fidelity for coverage, and that trade belongs to the user.
   - **Phone numbers** (`redactPhones`, default **off**) — North-American-ish
     shapes only, and a separator is required between every group (mirroring
     the SSN pattern's own false-positive guard) so a bare, unpunctuated
     10-digit run — an order number, a tracking ID — is left alone. Off by
     default for the same reason as email: real false-positive exposure on
     ordinary pages.
   - The marker is literally `[redacted:password]` / `[redacted:card]` /
     `[redacted:ssn]` / `[redacted:email]` / `[redacted:phone]`
     (`redactionMarker()` in `redaction.ts`). A kind that's off simply never
     runs its pattern — the raw value passes through untouched, marker and
     all skipped, not blanked-then-restored.
2. **At the gateway**, a second, independent pass (`tools.py`'s
   `_gateway_redact`, called on every `snapshot`/`read`/`act` diff result,
   plus `session_powers.py`'s `_redact_fetch_body` for `fetch`/`network`
   bodies) re-scans for card/SSN/email/phone (shape-based, same patterns in
   spirit as the extension's — see "ReDoS fix" below for why they're not
   byte-identical) and a password heuristic (a line mentioning "password"
   with two or more quoted spans gets its trailing value blanked) —
   **honouring the same per-kind policy**, not re-running unconditionally.
   All five kinds are gated here, not just card/ssn/password: email/phone
   parity was added at the same time as the ReDoS fix below, once the
   gateway-side patterns could be shown to have the same provably-linear
   property the extension-side fix required (see that fix's reasoning for
   why "provably linear" was the bar, not just "fast on the test suite's
   fixtures"). The gateway learns a device's policy from the `redaction`
   field on `device.hello`/`device.heartbeat`/`state.report` (`relay.py`'s
   `_apply_reported_redaction`, persisted per-device in `state.py`'s
   `device_redaction_policy` table so it survives a gateway restart) and
   looks it up via `state.get_redaction_policy(device_id)` before deciding
   what to re-scan for. A device that has never reported a policy — an older
   extension build predating this feature — gets every kind treated as
   enabled, so the gateway's original unconditional behaviour is exactly
   preserved for it; this "absent means enabled" rule is also protocol/
   schema.json's own stated semantics for `redactionPolicy`. Any *new* hit
   found here for a kind that **is** enabled — meaning the extension-side pass
   missed something it should have caught — is logged loudly as
   `redaction_gateway_catch`, not silently patched over, exactly as before; a
   kind the user switched off produces no hit and no catch event, because
   there is nothing to catch that both layers agree should be caught.

**A present-but-malformed policy value never disables a kind.** The wire
carries `redaction` as a plain JSON object; nothing in this package validates
it against protocol/schema.json's `"type": "boolean"` before `state.py`
stores it — the schema is documentation here, not enforcement. `state.py`'s
`_kind_enabled` is the actual enforcement point: a kind is disabled ONLY by
the literal JSON boolean `false` (checked with Python's `is`, not `==`, so
`0` can't sneak through `0 == False`). A key that's present but `null`, `0`,
`""`, `[]`, `{}`, or any non-boolean value reads exactly the same as an
absent key: enabled. An earlier version of this code used a bare `bool()`
coercion, which silently disabled a kind on any JSON-falsy value — caught in
review before it shipped, not found in production, but real enough that a
client sending `{"password": null}` would have quietly turned password
redaction off. Covered by `tests/test_m1_tools.py`'s malformed-value block
(null/0/empty-string/empty-list/empty-dict/the-string-"false"/a truthy
string/the number 1 — none of them disable a kind; only literal `false`
does).

**ReDoS fix (both sides):** the extension's original `EMAIL_PATTERN`
(`[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}`) had unbounded
backtracking on adversarial input — measured freezing the page for several
seconds on a 100-120K-character string of digits and dots with no trailing
letters, from two independent causes (an ambiguous domain character class,
and a separate failure mode where a missing `@` forces the local-part class
to walk to end-of-string and back off one character at a time at every
starting offset `.test()`/`.exec()` try — see `redaction.ts`'s comment on
`EMAIL_PATTERN` for the full mechanism). The fix bounds every quantifier in
both the local part and the domain to a small, RFC-realistic maximum instead
of an unbounded `+`/`*`, which caps the backtracking work possible from any
single starting offset at a constant — making total cost provably O(n)
regardless of input shape, verified by measurement across adversarial inputs
from 10K to 1M characters, not just argued from the pattern's shape. The
gateway's Python `_EMAIL`/`_PHONE` patterns (`tools.py`) use the identical
bounded-quantifier construction — Python's `re` module is a backtracking
engine with the same complexity class for this pattern shape as V8's, so the
same fix applies, and was verified the same way (linear from 50K to 1M
characters against the same adversarial shapes) before email/phone were
added to the gateway pass at all. Neither `walker.ts` (which redacts every
visited node during traversal, before the ≤4KB snapshot budget is applied)
nor `reader.ts` (which redacts the whole rendered page before `capToBytes`
trims it) caps input size before redaction runs — that's a deliberate choice
to rely on "provably linear in total text" rather than "bound the input
first", documented at each call site, not an oversight; redacting
already-truncated text would also risk leaking half of a real value that
straddled the truncation boundary. Card/SSN/phone patterns on both sides
were checked at the same time and are linear already (bounded repetition
counts from the start); only the email pattern needed the fix.

**What "end to end" means in practice:** switching a kind off in Options is
not just a cosmetic UI change. `options.ts` pushes `{ type: "redaction.changed" }`
to the offscreen document (deliberately *not* `settings.changed`, which
reconnects the socket) and `client.ts`'s `reportRedactionPolicy()` sends a
`state.report` frame carrying the new policy immediately — the gateway does
not have to wait for the next heartbeat to stop re-redacting a kind the user
just disabled. Every actual policy change (a value that differs from what was
persisted before) is written to the audit log as `redaction_policy_changed`
with the full `before`/`after` policy, because "the user turned a data
protection off" is itself a security-relevant event worth a durable record —
even though the *change itself* is not gated or refusable (the checkbox is
the user's own, always-honoured, privacy preference for their own machine,
not something the gateway approves or denies).

**Still not per-origin:** `plan.md` §6.3 originally described a per-origin,
explicit opt-out. What actually shipped is a single, global-per-device
policy — five checkboxes that apply to every origin the same way, not five
checkboxes per origin. This is a real, intentional scope reduction from the
original design, not a bug: a per-origin redaction matrix is a materially
bigger UI and storage surface, and nothing about the M1-era failure mode
("a checkbox that looked wired up but wasn't," see the prior revision of this
section) required solving that harder problem to fix the actual defect,
which was "the checkboxes do nothing at all." If true per-origin redaction is
wanted later, this global policy is the right layer to extend, not replace.

**Documented gap #2 (tracked in `ProjectRules/Plan.md`'s M4 section already,
repeated here for completeness):** the gateway's "already redacted, skip"
guard (`_line_already_redacted`) matches the extension's `[redacted:<kind>]`
marker text *anywhere in the line*, including inside content the page itself
rendered. A hostile page that renders the literal string `[redacted:password]`
next to a real, un-redacted password value could suppress the gateway's
second-layer catch for that line. This does not defeat the extension-side
redaction (layer 1 still runs and still catches real password fields by
identity, independent of page content) — it only weakens the belt-and-braces
layer, and only for content the extension's own pass didn't already redact by
field identity.

### 3a. `browser_bridge_console`'s extra kind: bearer tokens/JWTs/API keys/secret URL params (G2.4.4)

`browser_bridge_console` (console.log/warn/error output, uncaught exceptions,
and browser-generated Log entries) is not just another read gated by the five
kinds above. Console output realistically carries **bearer tokens, JWTs, API
keys, and URLs whose own query string embeds a secret** (an
`exceptionThrown` stack frame's `url` is exactly the file a script threw
from, verbatim, including whatever `?access_token=...` it was loaded with) —
none of the five kinds in §3 would ever match these, and card/ssn/email/phone
being individually switchable is the wrong model for them: nothing
legitimate depends on an agent seeing a raw bearer token, unlike a phone
number's genuine page-fidelity trade-off.

So a **sixth, unconditional pass** exists, scoped to this one tool only —
`extension/src/lib/redaction.ts`'s `redactSecrets` (extension-side, in
`background/console.ts`'s `redactEntry`, run at read time rather than at
capture) and `hermes_plugin/session_powers.py`'s `_redact_console_secrets`
(the gateway-side re-check, called from `handle_console`) — two independent
implementations of the same four shapes, same "neither trusts the other"
posture as the existing card/ssn/email/phone pair. It is **not** a
`RedactionPolicy` row and has no Options checkbox: it cannot be switched off.
Marker: `[redacted:token]`, deliberately distinct from the `[redacted:<kind>]`
family in §3 so an audit reader can tell which pass caught what.

This pass runs over an entry's `text` **and** every captured stack frame's
`url` (and the entry's own top-level `url`, for a Log/exception entry) — a
secret can arrive through either. It is layered on top of, not instead of,
the ordinary card/ssn/email/phone re-check (`_redact_fetch_body` is reused
for that half, same as the fetch/network bodies).

`console` was already one of G0.5's `DANGEROUS_CAPABILITIES` and already had
a device-power-policy entry (`allowConsoleRead`, off by default) before this
task — the local, extension-side refusal that setting drives lives in the
new `extension/src/background/powers.ts` (`assertConsoleReadAllowed`),
called from `background/console.ts`'s `handleConsoleEntries` before the
attach check even runs, so it fires with the gateway entirely uninvolved
(`tests/background/console.test.ts` proves this).

## 4. The CDP method whitelist

`tools.py`'s `is_cdp_method_allowed()` (plan §6.5), enforced gateway-side:

- **Allowed by exact name:** `Page.navigate`, `Page.reload`,
  `Page.captureScreenshot`, `Network.enable`, `Runtime.evaluate`,
  `Emulation.setFocusEmulationEnabled` (background-tab focus bug fix — see
  below), `DOMDebugger.getEventListeners` (G3, `browser_bridge_inspect`'s
  `listeners` question — see the paragraph below the `DOMDebugger` list
  item)), below), `Emulation.setDeviceMetricsOverride` and
  `Emulation.clearDeviceMetricsOverride` (speedimprovements.md G2's
  taller-page-for-one-call viewport override — see below).
- **Allowed by prefix:** `Input.*`, `DOM.*`.
- **Explicitly denied, regardless of prefix rules:** `Fetch.*` — full
  request/response interception is MITM-grade and stays phase-3/opt-in only,
  never default-on. Every refusal is audited (`cdp_method_check`, `decision:
  "deny"`).
- Only `Runtime.evaluate` is allowed from the `Runtime.*` family —
  `Runtime.callFunctionOn`, `Runtime.awaitPromise`, etc. are deliberately
  **not** swept in by a prefix rule, because they'd widen exactly the sandbox
  `Runtime.evaluate`'s own limits exist to bound.
- G2.6.2–.4's node-handle targeting (§17) adds five new CDP methods —
  `DOM.getDocument`, `DOM.querySelector`, `DOM.describeNode`,
  `DOM.scrollIntoViewIfNeeded`, `DOM.getContentQuads` — all already covered
  by the existing `DOM.*` prefix rule above, and all already enabled on
  every attached tab (`background/cdp.ts`'s `DOMAINS` has included `DOM`
  since M1). No change to the domain-enable list or the whitelist was
  needed for this work.
- **Background-tab focus bug fix:** `background/cdp.ts`'s `attach()` calls
  `Emulation.setFocusEmulationEnabled({ enabled: true })` on every tab right
  after the domain-enable loop (and again, best-effort, for any tab
  `resyncAttachments()` adopts from a prior worker instance) — it makes the
  page believe it has real OS focus (focus/blur, `:focus-visible`,
  `document.hasFocus()`) without Chrome ever having to raise the actual
  window, which is what lets the agent act in a background tab without
  stealing it. `Emulation` was added to `_CDP_KNOWN_DOMAINS` and
  `Emulation.setFocusEmulationEnabled` to `CDP_EXACT_ALLOW` in `tools.py` for
  this — not because anything routes through the gateway whitelist today
  (see the next paragraph), but so the day a raw passthrough IS exposed,
  this specific, narrow, focus-only method is already correctly classified
  rather than refused by the generic `Emulation.*` deny-by-default. No other
  `Emulation.*` method is allowed, by prefix or otherwise —
  `Emulation.setFocusEmulationEnabled`,
  `Emulation.setDeviceMetricsOverride` and
  `Emulation.clearDeviceMetricsOverride` are exact-allowed, and nothing else
  in that domain is.
- **G2 viewport override (speedimprovements.md):**
  `background/viewport-override.ts`'s `withViewportOverride` calls
  `Emulation.setDeviceMetricsOverride({ width, height, deviceScaleFactor: 0,
  mobile: false })` directly via `chrome.debugger.sendCommand` for the span
  of a single `dom.snapshot`/`page.screenshot` call whose `viewport` param
  was given (width 320–3840, height 240–8000, validated extension-side
  before any CDP call — see `validateViewport`), then ALWAYS clears it again
  in a `finally`, even when the wrapped capture throws. Calls on the same
  tab are serialised (`runExclusive`, a promise-chained per-tab mutex) so a
  second overlapping call can never interleave its own override/clear with
  the first's. It changes only what the ATTACHED TAB's own renderer believes
  its viewport is — never the user's real OS window — and is refused
  outright (`LIMITED_MODE_CAPABILITY_UNAVAILABLE`) in limited (no
  `chrome.debugger`) mode, since there is no CDP session to apply it
  against. Like the focus-emulation fix above, both methods were added to
  `CDP_EXACT_ALLOW` even though nothing routes them through the gateway
  whitelist today, so a future raw `cdp.send` passthrough classifies them
  correctly from day one.
- **`DOMDebugger.getEventListeners` (G3, `browser_bridge_inspect`'s
  `listeners` question) is read-only.** It returns the event listeners bound
  to a node — type, capture/passive/once flags, source location — the exact
  same information DevTools' own Elements > Event Listeners panel shows a
  human operator; it never adds, removes, or fires a listener, and never
  reads or invokes the listener FUNCTION itself (no `functionLocation`
  handler text is surfaced back to the agent, only the type/flags).
  `DOMDebugger` was added to `_CDP_KNOWN_DOMAINS` and
  `DOMDebugger.getEventListeners` to `CDP_EXACT_ALLOW` for this reason — no
  other `DOMDebugger.*` method (e.g. `setDOMBreakpoint`, which sets real
  debugger state) is allowed, by prefix or otherwise, exactly like the
  `Emulation.*` entry above. `background/background.ts`'s
  `handleDomInspect` calls it directly via `chrome.debugger.sendCommand`
  (same as every other `dom.inspect` question and `page.act`'s own CDP
  calls) — nothing routes it through this gateway-side whitelist today, per
  the "no tool sends a raw `cdp.send` frame" note below. `listeners` also
  needs a real `chrome.debugger` session (full share mode): a limited-mode
  tab gets the same `LIMITED_MODE_CAPABILITY_UNAVAILABLE` refusal §22
  documents for every other CDP-only capability.

**As shipped, no tool sends a raw `cdp.send` frame at all.**
`browser_bridge_act`'s click/type/select/submit/scroll/key/navigate/wait_for/hover
are implemented as a higher-level `page.act` verb the extension itself turns
into CDP calls (real `Input.dispatchMouseEvent`/`dispatchKeyEvent` sequences —
not `element.click()` — specifically so sites gating behavior on
`isTrusted` see a real, trusted input event). The gateway-side whitelist
function and its audit trail exist and are unit-tested, ready for the day a
raw passthrough ships, but today it's a dead-code safety net, not an active
gate on live traffic. Don't assume "the whitelist is enforced" implies "raw
CDP is reachable and gated" — it means "raw CDP isn't reachable at all yet,
and if it becomes reachable, this is where it'll be gated."

## 5. The SSRF guard on `browser_bridge_fetch`

`browser_bridge_fetch` runs a real HTTP request inside the attached tab's own
page context (`Runtime.evaluate` calling the page's own `fetch()`), which is
the entire point — it inherits cookies, CSRF headers, and TLS/HTTP2
fingerprint automatically. That also makes the browser a confused deputy: it
can reach the LAN and any localhost admin panel regardless of what the model
was ever meant to touch. `session_powers.py`'s `_ssrf_guard`, checked before
any wire frame is built:

1. **Same-origin as the attached tab** → always allowed through to the normal
   `_authorize()` gate. This is the flagship case (a console tab fetching its
   own API).
2. **Cross-origin, with an explicit row in the grants table** (`request` or
   `full` — checked via `state.list_grants()`, **not** the config
   default-mode fallback `state.get_mode()` would apply) → allowed through to
   `_authorize()`.
3. **Cross-origin, no explicit grant, and the target looks like loopback /
   link-local / RFC1918-or-ULA private space / an internal hostname**
   (`localhost`, `*.local`, `*.internal`, `*.localhost`, `*.home.arpa`) →
   refused outright with a dedicated `fetch_ssrf_refused` audit event and a
   message naming the specific reason (e.g. "private-network (RFC1918/ULA)
   address").
4. **Cross-origin, no explicit grant, doesn't look private** → still refused
   (`cross_origin_ungranted`), just with generic wording instead of the SSRF
   one — the private/public distinction only changes which message and audit
   event fire, not the outcome; an ungranted origin is refused either way.
5. **Any non-`http(s)` scheme** (`file:`, `chrome:`, `javascript:`, `data:`,
   `ftp:`, ...) → refused outright regardless of origin.

A caller-supplied `Cookie` header is silently stripped (not rejected) before
the request is built — the entire point of page-context fetch is inheriting
the browser's real cookie jar; letting a caller hand-craft one would defeat
that.

**Documented limitation (the code says this plainly, repeated here because
it's the single most important caveat in this document):** step 3's
classification is **string/literal only** — IP literals and a short list of
internal hostname suffixes, not a live DNS resolution. The actual network
connection happens inside the browser, not on the gateway, so a DNS lookup
done here wouldn't reliably reflect what the browser actually contacts (and a
blocking DNS call on every fetch isn't something this plugin's 2-vCPU/3GB
budget should be spending anyway). **A public-looking hostname that resolves
— or is later rebound via DNS — to an internal address is not caught by this
guard.** Full protection against that class of attack needs response-side or
DNS-pinned enforcement, which is squarely `Fetch.*` CDP interception territory
— explicitly out of scope until the phase-3, opt-in-only MITM mode `plan.md`
already defers this to. If your threat model includes an attacker who
controls DNS for a domain you've granted `request`/`full`, this guard does
not protect you from DNS rebinding.

The extension's own `fetch.ts` does a parse-and-scheme pre-check too
(`validateUrl()`), but its own comment is explicit that this is not a
security boundary — "the real SSRF/grant enforcement is the gateway's job;
this is just 'don't hand the page context garbage.'" Don't rely on it.

## 6. Device tokens: hashed at rest, cleartext on the wire and in the browser

- **Gateway (`state.py`):** a device token is `secrets.token_urlsafe(32)`
  (256 bits), shown to the extension exactly once (at pairing or on a token
  rotation), and stored server-side only as `hashlib.sha256(token).hexdigest()`
  in `state.db`'s `devices.token_hash` column. `authenticate()` compares with
  `secrets.compare_digest` (constant-time) and checks token match **before**
  checking revocation status, on purpose — revealing "revoked" vs. "unknown"
  to someone who doesn't hold the correct token would turn revocation into a
  scanning oracle.
- **Extension (`lib/storage.ts`):** the raw token is stored in
  `chrome.storage.local` under the `"credentials"` key, **in cleartext** —
  there is no client-side hashing or encryption of it anywhere in the
  extension code. The only stated protection is "never sync" (i.e.
  `chrome.storage.local`, never `chrome.storage.sync`, so it doesn't leave
  the machine via Chrome's own sync). Anyone with local access to that
  Chrome profile's storage can read the live device token. This matches how
  most browser-extension credential storage works (there's no better native
  primitive without an OS keychain integration this extension doesn't have),
  but it's worth stating plainly rather than letting "hashed at rest" (true
  server-side) imply "never in cleartext anywhere" (not true — it has to be
  cleartext somewhere for the extension to present it).
- **On the wire:** the token travels once, in the `device.hello` frame,
  device→gateway, over the LAN in plaintext WebSocket (`ws://`, not `wss://`
  — remote `wss://` via nginx is a deferred M4 item, not shipped). Anyone who
  can sniff LAN traffic between the Chrome machine and `your-gateway-host:8765`
  during a hello can capture a live device token. This is an accepted
  LAN-trust-boundary risk per `plan.md` §6.1 ("outbound-only relay... LAN
  binds"), not a bug, but it means this bridge should not be run across an
  untrusted network segment without the deferred `wss://` work.
- **Revocation is immediate and doesn't require a gateway restart** —
  `revoke_device()` sets `revoked_at`, and the next `device.hello` (or any
  authenticated frame) with that token gets `TOKEN_REVOKED`; a currently-open
  connection is also proactively dropped (`Relay.disconnect()`).

## 7. What the audit log does and does not contain

Every gated decision, pairing event, grant change, and revocation is an
append-only JSONL line in `~/.hermes/browser_bridge/audit.jsonl`
(`hermes_plugin/audit.py`). Writes are best-effort-but-loud: a write failure
is logged, never raised into the caller, so a full disk degrades logging
before it degrades the product.

**Never written to the audit log, by explicit design:**
- Cookie **values** — `handle_cookies` never puts a cookie value in an audit
  line, even when `include_values=true` was requested and granted. Cookie
  **names** are withheld too, as defense in depth beyond what the plan
  strictly requires ("the audit trail gains nothing from them an operator
  can't already get from origins + count").
- Device tokens — only the SHA-256 hash ever exists server-side at all (§6);
  nothing token-shaped is logged.
- Raw network response/request **bodies** by default — `browser_bridge_network`
  only logs a `redaction_gateway_catch` count when a body is actually
  returned (i.e. the origin was `full` and bodies were requested), never the
  body text itself.
- Approval `summary`/`detail` text is passed through `approvals.py`'s own
  independent redaction pass (`_scrub` — password/API-key-shaped
  `key: value` pairs, card-shaped digit runs, SSN-shaped runs) *before* it's
  ever sent to the extension or written to the audit log, deliberately
  self-contained from `tools.py`'s own redaction code so a bug in one
  doesn't silently disable the other. (Pending-approval state itself is no
  longer persisted to `state.db` at all as of §1b's migration — it lives
  only in memory for the duration of one `require()` call.)

**Can appear in the audit log** (this is the honest disclosure `modes.md`
and the CLI's `export` command both point back to here for): URLs, page
titles, CSS selectors, form field names/labels, `act()`/`ask()` argument
summaries (e.g. what text was typed, what URL was navigated to), grant/mode
changes, and approval request/response metadata (origin, capability, scope,
timestamps). Treat an exported or shared audit log as **operationally
sensitive** even though it holds no credentials — it's a fairly complete
record of what the agent looked at and did.

### Audit rotation runbook

Rotation is size-based, keep-N, checked on every write
(`_rotate_if_needed`, called before each `record()` appends a line):

- `browser_bridge.audit_max_bytes` (default 25 MiB) — once the live
  `audit.jsonl` reaches this size, it's rotated.
- `browser_bridge.audit_keep` (default 5) — up to this many rotated files
  are kept (`audit.jsonl.1` through `audit.jsonl.5`), oldest evicted (the
  chain shifts: `.4→.5`, `.3→.4`, `.2→.3`, `.1→.2`, then the live file
  becomes the new `.1`) when a rotation would exceed the cap.
- **Verified by reading the code (this workstream's review, 2026-09-22):**
  rotation cannot lose entries — the live file is only ever *renamed* to
  `.1` (never truncated or deleted before the rename completes), and every
  file in the shift chain that moves gets `os.chmod(..., 0o600)` applied
  immediately after, so a rotated file is never left world-readable even
  transiently. The one file *not* re-chmod'd on a given rotation is one that
  didn't move in that rotation — but it was already `0600` from when it was
  created, and nothing else in this codebase ever changes an audit file's
  mode, so that's not a gap.
- **Operating it:**
  - Check current size/rotation state: `ls -la ~/.hermes/browser_bridge/audit.jsonl*`.
  - Nothing to run manually — rotation is opportunistic on the next write,
    not cron-driven (there's no gateway-startup hook to hang a scheduled job
    off; see `Documentation/plugin-api-findings.md`). A quiet bridge with no
    tool traffic simply won't rotate until the next write crosses the
    threshold, even if the file is technically over `audit_max_bytes` — this
    is a real (if minor) inconsistency worth knowing about if you're
    monitoring disk usage precisely rather than glancing at it.
  - To search across rotated history from the CLI, use `hermes browser-bridge logs
    --include-rotated` (added in M4) or `hermes browser-bridge export
    --include-rotated --out <dir>` to pull the whole chain into one place.
  - To force a rotation before it would happen naturally (e.g. before a
    planned audit review), lower `audit_max_bytes` in `config.yaml`
    temporarily and restart the gateway, or just `mv` the live file aside by
    hand — the next `record()` call recreates `audit.jsonl` fresh with 0600
    permissions.
  - Rotated files are **not** compressed and **not** shipped anywhere; they
    sit in `~/.hermes/browser_bridge/` until evicted by the keep-N chain.
    There is currently no long-term archival story beyond `hermes browser-bridge
    export` for a point-in-time snapshot — if you need audit history retained
    longer than `audit_keep` rotations' worth of activity, export
    periodically.

## 8. Pairing-code brute-force protections

Two independent layers (`relay.py`), both exercised by `tests/test_relay_gate_and_caps.py`:

1. **Per-connection:** `MAX_HELLO_FAILURES = 5` — a single WebSocket
   connection that fails `device.hello` 5 times is closed
   (`pairing_abuse_disconnect`, close code 1008).
2. **Per-IP sliding window:** keyed by remote IP rather than by connection —
   reconnecting doesn't reset an attacker's count the way the per-connection
   counter alone would. `HELLO_RATE_LIMIT_MAX_FAILURES = 10` failures within
   `HELLO_RATE_LIMIT_WINDOW_SECONDS = 600` (10 minutes) trips `RATE_LIMITED`
   and the connection is closed before the hello is even dispatched.
   - **Tracked-IP cap:** the failure-tracking dict is capped at
     `HELLO_RATE_LIMIT_MAX_TRACKED_IPS = 2048` distinct IPs, since the window
     is time-pruned but otherwise unbounded — a distributed attack spread
     across many source IPs could otherwise grow this dict without limit
     against the plugin's <30MB RSS budget. Past the cap, the
     least-recently-active IPs are evicted in batches of 128 (not one at a
     time — an eviction that fires per-new-IP under sustained attack would
     itself become a log-amplification vector), audited as
     `hello_tracking_cap_evicted`.

## 9. The kill switch

- **Popup ("Release all now"):** detaches `chrome.debugger` from every
  attached tab first, *then* tells the gateway (`kill.switch` notification)
  and disconnects the socket (`device.goodbye` + close) — deliberately in
  that order, so the gateway is never told tabs are released before they
  actually are.
- **Server-side equivalent:** `hermes browser-bridge revoke <device_id>` marks the
  device revoked in `state.db` and proactively closes any live connection —
  effective immediately, no gateway restart required, no cooperation needed
  from a possibly-compromised or unresponsive extension.
- The gateway can also push `kill.switch` to the extension unprompted (used
  by revoke); this variant releases tabs but does not itself disconnect the
  socket — that's the popup-initiated version's job.

## 10. Residual risks, stated plainly

- **Per-origin redaction opt-out still doesn't exist** despite `plan.md`
  describing it (§3) — the options-page toggles are real and wired end to
  end now (extension and gateway both honour them, changes are audited), but
  they're global-per-device, not per-origin. Turning email redaction off
  turns it off for every origin, not just the one you meant. This is a
  deliberate scope reduction from the original design, documented in §3, not
  an oversight.
- **Turning a redaction kind off is a real, un-gated privacy trade the user
  makes for themselves** — nothing on the gateway can refuse it or require
  approval for it (it's a data-protection setting on the user's own machine,
  not a capability grant), so the only safeguard against an accidental or
  coerced toggle is the audit trail (`redaction_policy_changed`, with
  before/after) and the Options page copy explaining what each kind catches.
  There is no confirmation dialog before saving a weakened policy.
- **Gateway-side redaction re-check can be suppressed by page content**
  spoofing the `[redacted:...]` marker text (§3) — extension-side redaction is
  unaffected; only the belt-and-braces layer is weakened, and only for values
  the extension's own identity-based pass didn't already catch.
- **DNS rebinding is out of scope for the SSRF guard** (§5) — accepted,
  documented, deferred to phase-3 `Fetch.*` interception.
- **No `wss://` yet** — device tokens and all traffic are plaintext on the
  LAN (§6); don't run this across an untrusted network segment.
- **`hermes_plugin/state.py`'s SQLite I/O runs synchronously on the relay's
  own asyncio event loop** (a deferred item from the M0 hardening pass,
  `ProjectRules/ChangeLog.md`) — fine at today's traffic, but a slow disk
  under M3-level `fetch`/`network` volume could stall pairing/heartbeat
  processing for every connected device, which is availability, not
  confidentiality, but worth knowing before scaling usage up.
- **Screenshot by `idx` has no element-identity check**: unlike `act`'s `expect` role/name check,
  the idx is resolved only via the URL-scoped index map, so a same-page re-render can crop the wrong element (read-only; no action taken).
- **The popup's per-origin mode dropdown never displays the current mode** —
  it can only set one. This isn't a vulnerability, but it does mean a user
  reviewing what they've granted has to go check `browser_bridge_status` or
  the audit log rather than trusting the popup UI at a glance — worth fixing
  before this is handed to a less technical user than the person who built it.

## 11. `Fetch.*` interception stays banned — this plan does not lift it

`plan.md` §6.5 settled this and nothing in G0–G3 revisits it: the gateway will
not present a raw request/response-interception surface to the model. That
decision is enforced in four places today, not one:

1. `cdp.ts`'s `DOMAINS` comment (extension side) — documents the same ban at
   the point where the extension itself decides what CDP domains to enable.
2. `hermes_plugin/tools.py`'s `CDP_EXPLICIT_DENY_PREFIX = ("Fetch.",)` and its
   surrounding comment (§4 above) — named explicitly rather than left to fall
   through a missing prefix rule, specifically so a future edit that widens
   `CDP_PREFIX_ALLOW` can't accidentally sweep `Fetch.*` back in.
3. The refusal text itself, which every caller of `_send_cdp`/
   `is_cdp_method_allowed` gets verbatim — required (not just conventional) to
   contain "MITM" or "interception", enforced by
   `tests/test_m2_act.py`'s "the refusal names Fetch.* as MITM-grade
   interception" check (`grep -n 'MITM-grade interception' tests/test_m2_act.py`
   to find the current line — see `ProjectRules/coveragegaps.md` §0.0 on why
   this doc doesn't pin a line number that will drift).
4. This file, both here and in §4.

**Basic auth does not need `Fetch.*` and will not get it.** The `http_auth`
capability (§0.6's row 8 / G0.5's `DANGEROUS_CAPABILITIES`) is deliberately
scoped to *reading* whether an HTTP auth challenge is currently open on the
attached tab, gated by `_authorize` like any other dangerous capability.
Arming a saved credential against that challenge is planned to go through
`chrome.webRequest.onAuthRequired` under the `webRequestAuthProvider`
extension permission (G3.7) — a purpose-built, non-interception API Chrome
exposes for exactly this, not a side door into general request/response
tampering. If `webRequestAuthProvider` ever turns out to be insufficient,
that is a reason to have the `Fetch.*` design conversation explicitly and in
the open, not a reason to quietly route around this ban.

**The wrong-password loop, and how it's closed.** A staged credential the
server rejects looks, on the wire, identical to "answer again" —
`chrome.webRequest.onAuthRequired` re-fires for the SAME `requestId` when a
just-supplied Basic credential was bad, and naively answering it again would
loop forever (and can trip a site's own failed-attempt lockout).
`extension/src/background/auth.ts` tracks which `requestId`s it has already
answered, in memory only (deliberately not `chrome.storage.session` — this
is per-in-flight-request bookkeeping, not a credential, and gains nothing
from surviving a worker restart). A second challenge for a tracked
`requestId` is answered with nothing (Chrome's own prompt takes over), the
bad credential is disarmed immediately, and the origin's next `auth.status`
read reports `armed: false, lastResult: "rejected"` so
`browser_bridge_http_auth_status` can tell the agent — and through it, the
user — that the staged password was wrong instead of silently retrying.
Tracking is bounded two ways: entries are freed on `webRequest.onCompleted`/
`onErrorOccurred` for that `requestId` (the common case — most requests
settle before any cap pressure), and a hard cap (256 entries, oldest evicted)
bounds it regardless. **Residual risk, stated plainly:** the tracking is
in-memory, so a service-worker restart landing between the first and second
`onAuthRequired` for the same `requestId` forgets the first answer — the
second challenge then reads as a first, and the (possibly still-bad)
credential can be replayed one additional time before the loop guard would
have caught it. Bounded to one extra attempt per restart landing in that
exact (narrow) window, never an unbounded loop.

**The `Fetch.*` deny-list (and the whole CDP allow/deny list it lives in) is
latent, not a live gate on today's traffic** — see `ProjectRules/
coveragegaps.md` §0.5, restated here because it is the fact most likely to be
misread from §4 alone: `is_cdp_method_allowed()` governs only the `cdp.send`
wire method, and **the extension has no handler for `cdp.send` at all** — a
gateway that sent one would get `METHOD_NOT_FOUND` back, not a CDP round
trip. Proof, not assertion: the extension routinely sends
`Page.getLayoutMetrics` (`extension/src/background/act.ts:679` and
`extension/src/background/background.ts:462`, current lines — re-grep before
citing, per §0.0), and `is_cdp_method_allowed("Page.getLayoutMetrics")`
returns **`False`** (it matches none of `CDP_EXACT_ALLOW`/`CDP_PREFIX_ALLOW`);
screenshots and scrolling still work in production because that call never
goes anywhere near this gate — `browser_bridge_act`'s verbs are implemented as
the higher-level `page.act` wire method, with the extension itself owning the
CDP calls underneath. A method reached from `background/`'s own code is
subject to **no gateway method filter today**. Editing the deny-list gates
the day a raw `cdp.send` passthrough ships; it gates nothing on the wire that
exists right now. A task or a report that says "the `Fetch.*` ban is
enforced" without this caveat is describing readiness, not an active control
— say which one you mean.

## 12. Per-capability ceiling and the operator kill switch (G0.5 / G0.9)

**The defect this closes:** `config.py`'s `default_mode` is `"full"` (the user
asked for this default 2026-09-22, §1 above), and before G0.5 the popup's
"always" response to *any* approval promoted the whole origin to `full`
(`state.set_grant(device_id, origin, "full")`). Put together, an origin the
user never configured — or configured once, for one capability, via "always"
— would auto-allow *every* capability this plugin ever grows, forever, with
no prompt. That is fine for `snapshot`/`read`/`act`/`fetch`-shaped
capabilities, where `full` is the whole point of the mode. It is not fine for
a capability that is destructive, credential-adjacent, or a broad read with
no natural scope of its own.

**`DANGEROUS_CAPABILITIES`** (`hermes_plugin/approvals.py`, beside `SCOPES`):
`upload`, `evaluate`, `cookies_write`, `http_auth`, `dialog`, `downloads`,
`console`. For every one of these, a `full` origin grant is **not** sufficient
— `tools.py`'s two gates enforce this as a ceiling that a "full" mode cannot
rise above:

- **`_authorize`** (the approval-aware gate used by `act`/`fetch`/`ask`/
  `open_tab`/etc.): its `mode == "full"` short-circuit only fires when the
  capability is *not* in `DANGEROUS_CAPABILITIES`. For a dangerous one, a
  `full` origin falls through to exactly the same `approvals.require(...)`
  path `request` mode uses — which itself checks
  `state.get_capability_grant()` (an earlier per-capability "always", see
  below) and the existing per-session grant before ever presenting a live
  prompt to the extension.
- **`_gate_reason`** (the read-only M1 gate for `snapshot`/`read`/
  `screenshot`/`attach` — none of which are dangerous today): the same
  `mode == "full"` short-circuit is guarded the same way, so that if a future
  capability in `DANGEROUS_CAPABILITIES` is ever wired through this gate by
  mistake, it fails closed (refuses, with a reason naming the mismatch)
  instead of silently allowing under `full` — this gate has no
  approval-transport integration of its own to fall back on, unlike
  `_authorize`.

**What "always" means for a dangerous capability.** `approvals.py`'s
`_apply_result` no longer calls `state.set_grant(..., "full")` for these — it
writes a row to the new `capability_grants(device_id, origin, capability,
granted_at, expires_at)` table (`state.py`, beside `session_grants`) instead.
Consequences, each a deliberate choice, not an accident:

- **The ceiling wins.** A pre-existing `full` origin row never implies a
  dangerous capability (that's the whole point of this section), and a
  capability grant never implies `full` for anything else — `console`
  "always" does not touch `downloads`, `cookies_write`, or the origin's mode.
- **TTL-bounded**, the same `approval_session_grant_ttl_hours` config value
  that already bounds the `session` scope — there is no reliable
  session-boundary or revoke-on-demand hook (`Documentation/
  plugin-api-findings.md`), so an unbounded standing grant would outlive
  whatever the user actually intended to give it.
- **Checked ahead of the session grant** in `require()`, so a capability
  grant outranks (and outlives, across different `session_key`s) a
  session-scoped one for the same origin+capability.

**`evaluate`, `upload`, and `http_auth` cannot get a standing grant at
all** — not `always`, not even `session`. Arbitrary code execution, a file
handed to a site off the operator's own disk, and a saved credential prompt
are not things one approve-once click can meaningfully authorize for later.
This is enforced **server-side**, not just by hiding the popup's buttons:
`require()` builds the host's `ApprovalRequest` with `allow_session=
allow_permanent=False` for these three, so the host's own decision validation
(`resolve()`'s `choice not in waiter.request.allowed_choices` check — the
same path that already rejects a stale or replayed response) rejects an
"always"/"session" choice before it ever reaches this plugin's own logic. The
popup (`approvals-ui.ts`) additionally omits the buttons for these three, and
changes the "always" confirmation copy for the other four dangerous
capabilities to say what actually happens now (a per-capability grant, not
full access to the site) instead of the "FULL access" wording every other
capability's "always" still shows accurately.

**The operator kill switch (G0.9), layered on top of all of the above:**
`browser_bridge.powers.<capability>: false` in `config.yaml` (`config.py`'s
`DEFAULTS["powers"]`, an empty dict by default) is checked first in
`_authorize`, before the mode/ceiling logic runs at all, and is **ANDed** with
everything else — no per-device grant, per-capability grant, or live approval
answer can override it. It is a **nested dict**, not `N` flat
`powers_<capability>` keys: `config.yaml` naturally nests
`browser_bridge.powers.evaluate: false`, and it keeps every future capability
name out of `DEFAULTS`'s own top-level namespace. It is checked with `is
False` specifically (not `not value`) — the *inverse* of a redaction kind's
fail-closed read (§3): this switch is fail-**open** by design (missing,
`True`, or a malformed value all mean "enabled"), because it only ever
**subtracts** from what a device's grants would otherwise allow, and an
operator who never touches it must get today's behaviour unchanged. Revert
path: delete (or set back to a non-`False` value) the `browser_bridge.powers.
<capability>` line in `config.yaml` and no extension reload or gateway
restart choreography is needed beyond Hermes' own config-reload story — this
is a plain config read on every `_authorize` call, not a cached or
persisted flag.

Tests: `tests/test_m2_approvals.py` — a grep-based check that every
`mode == "full"` early-return in `tools.py` is guarded against
`DANGEROUS_CAPABILITIES` (so a third short-circuit added later without the
guard fails the build, not just this behavioural suite); a `full` origin
still reaching `approvals.require()`/refusing outright for each of the seven
dangerous capabilities, through both `_gate_reason` and `_authorize`;
`always` writing a `capability_grants` row instead of promoting the origin;
that row being honoured (no re-prompt) and expiring; `always`/`session`
being rejected server-side for `evaluate`/`upload`/`http_auth`; and the
operator kill switch overriding a `full` origin for a named capability while
leaving every other capability untouched.

## 13. Cookie writing (G3.6)

`browser_bridge_cookie_set` (`hermes_plugin/session_powers.py`'s
`handle_cookies_set`, wire method `cookies.set`, extension handler
`extension/src/background/cookies.ts`'s `handleCookiesSet`) is the one tool
in this product that mutates a site's authentication state directly. Three
independent gates run, in this order, before `chrome.cookies.set` is ever
called — and every one of them is enforced **twice**, once in the extension
and once in the gateway, per §2's "neither layer trusts the other":

1. **The device power gate.** `cookies_write` is one of `_CAPABILITY_POWER_KEYS`
   (`allowCookieWrite`, default off) — checked gateway-side by
   `_device_power_denial` and extension-side by
   `background/powers.ts`'s `assertCookieWriteAllowed`, independently, before
   either the origin bound or an approval is ever considered.
2. **The origin bound (G3.6.3) — the IDOR control for this tool.** A set is
   refused unless the URL's origin matches a tab this device currently has
   **attached** (`chrome.debugger`-attached, not merely open in a tab).
   Without this bound, a single approval for "write cookies" would let the
   agent write a cookie for *any* origin on the internet — a far larger grant
   than the prompt to the user implies, and exactly the object-level access
   check OWASP's Top 10 (A01: Broken Access Control / IDOR) calls out: the
   *capability* (cookies_write) is authorized, but that says nothing about
   *which object* (which origin's cookie jar) the call may act on without
   this separate check. Enforced twice, independently:
   `cookies.ts`'s `attachedOrigins()` (extension-side, against
   `cdp.list()`) and `session_powers.py`'s `handle_cookies_set` (gateway-
   side, against a fresh `tabs.list`) — a device that lies about what's
   attached is still bound by the gateway's own, freshly-fetched view.
   **Known, documented gap this bound cannot close:** a device with
   `allowEvaluate` on can write `document.cookie` directly through
   `Runtime.evaluate`, which never passes through `cookies.set` at all and
   has no origin check of its own (see §1a and G1.5.0) — the origin bound
   here is real, but it is not the only way to write a cookie on a paired
   device.
3. **The capability ceiling.** `cookies_write` is in `DANGEROUS_CAPABILITIES`
   (§12): a `full` origin does **not** cover it — every call either hits a
   standing per-capability grant (an earlier "always") or raises a live
   approval prompt, same as `evaluate`/`upload`. `always`/`session` ARE
   permitted for this capability (unlike `evaluate`/`upload`/`http_auth`,
   which are `once`-only per G0.5.4) because writing one named cookie is a
   bounded action a single standing grant can meaningfully describe, unlike
   arbitrary code execution.

**G3.6.1's three `chrome.cookies` param traps**, enforced (not just
documented) on both sides so a mistake fails clearly instead of however
Chrome's own generic rejection happens to word it:

- `chrome.cookies.SameSiteStatus` has **no `"none"` member** — it is
  `no_restriction | lax | strict | unspecified`. Passing the literal string
  `"none"` (the value from the `Set-Cookie` HTTP header's own vocabulary) is
  refused before the call is attempted, with a message pointing at
  `no_restriction`.
- `sameSite: "no_restriction"` **requires** `secure: true` **and** an
  `https://` URL, or `chrome.cookies.set` fails outright. Checked before the
  call, both sides.
- Omitting `partitionKey` operates on the **unpartitioned** cookie jar only —
  a CHIPS-partitioned cookie the page itself set is invisible to both
  `browser_bridge_cookies` and `browser_bridge_cookie_set` unless the
  matching `partitionKey` is supplied. This is a real gap for a page using
  partitioned storage, not a bug; there is no way to enumerate partition keys
  from `chrome.cookies` at all.

**The value never appears in an audit line, an error message, a console
line, or the approval `detail` — on any path, including errors.** The audit
record and the approval `detail` are built from exactly one dict,
`{origin, name, httpOnly, secure, sameSite, expires}`, with no `value` key
ever added to it — this is a structural guarantee (the value is simply never
put in), not a redaction pass removing it afterwards.
`tests/test_m3_session.py`'s G3.6.4 checks prove this by inspecting the
literal detail string passed into `approvals.require()`. A **breaking test**
in the same file additionally proves the limits of the belt-and-braces
`_gateway_redact()` re-check: fed a bare high-entropy token that isn't
card/SSN/email/phone/password-shaped, `_gateway_redact` reports zero hits and
returns it unchanged — so `_gateway_redact` is not, and cannot be, what keeps
a cookie value out of the audit log here. What keeps it out is that `detail`
is never built with a `value` field in the first place.
`extension/tests/background/cookies.test.ts` proves the same thing
extension-side, including a defensive `scrubValueFromMessage()` pass over
`cookies.ts`'s own error text (belt-and-braces on top of the structural
guarantee, in case a future Chrome error message or refactor ever did echo
the value back).

**Where the value should come from (G3.6.5).** Reading a cookie's value off
a page the agent is already attached to (via `browser_bridge_cookies` with
`include_values`, or a snapshot/read) and replaying it through
`browser_bridge_cookie_set` is ordinary session continuity, not
exfiltration — the whole point of this tool. A cookie a **user** wants to
supply from somewhere else (a value copied from another device, a support
ticket, a password manager export) must be typed into the extension itself —
the popup's "paste a cookie for this origin" field
(`extension/public/popup.html`'s `#cookiePasteSection`,
`extension/src/popup/popup.ts`'s `cookiePasteButton` click handler) calls
`chrome.cookies.set` directly, entirely locally, with no gateway round trip
and nothing sent to the model — **never** into the chat with the agent. A
session cookie in a chat transcript is exactly the credential exposure this
whole design exists to avoid, and unlike `browser_bridge_cookie_set` (which
is audited, capability-gated, and origin-bound), a value typed into chat
would sit in the conversation history and any transcript/log the gateway or
model provider keeps of it, with none of those controls.

**Session fixation.** Setting a cookie whose name matches a site's own
session-cookie convention (e.g. a fixed `sessionid` value the attacker
already knows) is the textbook session-fixation attack when done to a
*victim's* browser to make them authenticate under an attacker-controlled
session id. That attack model does not apply to this tool as shipped:
`browser_bridge_cookie_set` only ever writes into the browser the **paired
device's own user** is sitting in front of, gated by that same user's own
approval — there is no cross-user reach here, the same reason
`page.fetch`'s SSRF guard (§5) protects the operator's LAN rather than a
victim's. The residual risk is local, not remote: an agent (or a user
pasting a cookie via the popup affordance above) can fixate the *user's own*
session by setting a known value, which matters if that value was obtained
somewhere the user did not fully trust. `docs/security.md` names this so it
is a known, considered risk rather than an unexamined one — there is no
technical control against a user fixating their own session with their own
credential, any more than there is one against a user pasting one into
`chrome://settings` by hand.

**`__Host-` and `__Secure-` cookie name prefixes.** Chrome enforces these
prefixes' rules itself (not this tool): a `__Host-`-prefixed cookie must be
`Secure`, must have `Path: /`, and must **not** carry a `Domain` attribute at
all (host-only); a `__Secure-`-prefixed cookie must be `Secure`. Passing
`domain` alongside a `__Host-`-prefixed `name`, or omitting `secure` for
either prefix, makes `chrome.cookies.set` itself reject the call — the same
"fails outright, Chrome's own structural error" path the `no_restriction`
trap uses, surfaced through `cookies.ts`'s existing error handling rather
than a bespoke check. This tool does not special-case these prefixes because
Chrome already refuses to violate them; `ACT_SCHEMA`-equivalent prose
(`COOKIES_SET_SCHEMA`) does not repeat Chrome's own enforcement, but this
document names it so an agent's failed call against a `__Host-`/`__Secure-`
name is recognisable as "the prefix's own rule," not a mystery rejection.

**What was not built:** nothing from G3.6.1–G3.6.6 was skipped. The popup
paste affordance (G3.6.5) shipped as a small, local-only feature —
see `extension/src/popup/popup.ts` for its full extent; it does not attempt
a general cookie-editor UI (listing/deleting existing cookies), only "paste
one in for the current origin," which is what the plan asked for.

Tests: `extension/tests/background/cookies.test.ts` (the extension-side
power gate, origin bound, the three param traps, the value-never-leaks
guarantee including a breaking test against the error path) and
`tests/test_m3_session.py`'s G3.6 block (the device power gate, the origin
bound against a freshly-fetched `tabs.list`, the capability ceiling denying
a `full` origin without an approval, the exact `detail` shape, and the
`_gateway_redact` breaking test described above).

## 14. `type`'s own params (G1.6) — no new capability, same `act` gate

`type_mode`/`press_enter`/`dispatch`/`key_delay_ms` (protocol/schema.json's
`page.act` params) change HOW an already-authorized `type` action enters
text, not WHETHER one is authorized — they carry no new capability string,
no new setting, and no new approval prompt; a `type` call with them is gated
exactly like one without.

Two refusals worth naming precisely, because they look like new gates but
are not capability checks:

- **`FIELD_NOT_EDITABLE` (4210)** — a disabled/read-only field, or a selector
  with nothing editable at or above the match, refuses locally in the
  extension (`content/targets.ts`'s `resolveEditableTarget`,
  `background/act.ts`'s `actType`) before any CDP input is dispatched. This
  is the page's own state being honoured, not an authorization decision —
  there is no setting to flip and no bypass; the field really is disabled.
- **`fieldValue` (G1.6.5)** is a fourth payload path alongside evaluate
  results/dialog messages/console entries (§0.7 of `ProjectRules/
  coveragegaps.md`) that carries page-authored text off the machine: the
  extension redacts it (`content/targets.ts`'s `readFieldValue`, via the same
  `redactFieldValue`/`redactText` a snapshot's field values already go
  through) before it reaches the wire, and `hermes_plugin/tools.py`'s
  `handle_act` runs `_gateway_redact` over it as the belt-and-braces
  re-check, same as `diff`. A password field's value is never returned
  regardless of which kinds are enabled — `redactFieldValue` treats a
  password-shaped field identity as an outright blank, the same rule a
  snapshot's field values already follow.

`dispatch: "keys"` sends real per-character `Input.dispatchKeyEvent` pairs
instead of one `Input.insertText` call — this is a fidelity choice (some
sites only react to keydown/keypress), not a new CDP surface: both paths use
`Input.*` methods already on the whitelist (§4), and neither adds a method
name to it.

A field inside a same-origin iframe is not reachable by `type` yet — the
pierce selector resolver (`content/pierce.ts`) never crosses a frame
boundary, so its selector is refused as not found (CDP_ERROR), not silently
mistyped into the top document. This is the same limitation G2.2 (iframes,
unbuilt) will close for every action, not something specific to `type`.
## 15. Downloads: read-only, origin-scoped, and two calls this plugin never makes (G3.5)

`browser_bridge_downloads` reads Chrome's own download history through
`chrome.downloads.search` (`extension/src/background/downloads.ts`), gated
like every other dangerous capability in §12 (`downloads` is in
`DANGEROUS_CAPABILITIES`; `allowDownloadsRead` is the device-side toggle,
row 9 of the powers table, on by default because it's a read that is already
origin-scoped, not a full-history browse). Two calls are a **permanent
never-list**, not a configuration option, an approval scope, or something a
future capability toggle could ever re-enable:

- Launching a completed download through the browser's own file-opening
  API — doing so executes the file with whatever handler the OS associates
  with its type, which is a materially different (and far more dangerous)
  action than reading metadata about it.
- Overriding Chrome's own danger interstitial for a download it has flagged
  as unsafe — Chrome raised that interstitial for a reason; a tool that could
  silently dismiss it on the model's behalf would be worse than not gating
  downloads at all.

Neither identifier is spelled out here in code-styled text on purpose, and
neither appears anywhere in `extension/src/` in any form — a direct call, a
computed/bracket property lookup, or a destructured reference —
`extension/tests/background/downloads.test.ts` statically scans the whole
`extension/src` tree to enforce this (not just the one file that owns
`chrome.downloads`), and the two evasions above (bracket access, destructure)
were each proven caught by temporarily reintroducing them, confirming the
test fails, then restoring from a backup and confirming `git status` is
clean before this was reported as done. `browser_bridge_downloads` is a
read-only tool: it returns `id`/`filename` (an absolute local path)/`url`
(query string redacted before the frame leaves the machine)/`mime`/
`bytesReceived`/`totalBytes`/`state`/`startTime`/`endTime`/`danger`, and
nothing in this plugin ever acts on a `filename` it returns beyond handing it
back to the model as text — "upload what you just downloaded" (G3.5.5 of
`coveragegaps.md`) is the sanctioned route from there, through G1.3's
separately-gated local-path upload tier (`allowFileUpload` + `uploadRoots`),
not built as of this section.

## 16. JS dialogs: observed and reported, not auto-dismissed (G1.4)

**Corrected premise.** A JS dialog (`alert`/`confirm`/`prompt`, plus a
`beforeunload` confirm) in an ordinary headful Chrome tab is not "unowned" —
Chromium notifies the DevTools handler this bridge attaches (`Page.
javascriptDialogOpening`) and **still shows the native modal on the user's
own screen** afterward; that belief (dialogs are auto-dismissed, or nobody
sees them) comes from Playwright/Puppeteer, which explicitly call
`handleJavaScriptDialog` themselves — this bridge does not inherit that. The
real defect is narrower and worse: `Runtime.evaluate` is excluded from
Blink's V8-interrupt escape hatch, so while a dialog is open, `settle()`'s
fingerprint poll (`background/act.ts`) queues behind the blocked main thread
and **every** subsequent CDP call on that tab times out, with nothing telling
the agent why.

**Default policy: observe and report, never dismiss on the agent's behalf as
the FIRST response.** `background/dialogs.ts` records every dialog
(`{id, type, message, defaultPrompt, url, hasBrowserHandler}`, `message`/
`defaultPrompt` redacted extension-side per §3's per-kind policy before this
frame is ever built — the fourth of the redaction-invariant payload paths)
and attaches the buffer to the in-flight `browser_bridge_act` call's result
(`page.act`'s `dialogs` field) — never as a separate gateway notification, so
a dialog that opens after `performAction` dispatches but before
`handlePageAct` returns is still attributed to the call that triggered it.
The buffer is cleared at the start of every `page.act` and on a top-level
`Page.frameNavigated`, so a dialog from an earlier action, or an earlier
page, is never misattributed to a later one.

**Auto-dismiss exists only where nothing is being read out from under
anyone.** `allowDialogDismiss` (on by default) governs it; dismissing is
always the safe direction (a `beforeunload` dismiss cancels the pending
navigation) and is never gated by a capability or an approval — requiring a
prompt to close a wedged dialog would leave that wedge with no way out. When
`hasBrowserHandler` is false (nothing shows the user anything — the "old
Playwright belief" case, still possible in some embedded/headless contexts),
it is dismissed immediately. **When `hasBrowserHandler` is true, the
extension never auto-dismisses it, not after any wait.** An earlier revision
of this control used a bounded (15s) timer here; that was a self-contradiction
against this same section's "leave the dialog for the user" and was removed
before merge — a timer that can cancel someone's `confirm()` while they are
still reading it is exactly the "yanked out from under them" failure the
consent model this product is built on forbids. The record carries a `note`
telling the agent the dialog is staying open so it says so instead of
retrying whatever just timed out; the dialog stays open until the user
answers it themselves or `browser_bridge_dialog` resolves it.

**Accepting is the gated direction.** `dialog` is one of §12's
`DANGEROUS_CAPABILITIES` (a `full` origin grant does not cover it —
`allowDialogAccept`, off by default, gates it, and it falls through to
`approvals.require(...)` the same as any other dangerous capability under
`full`). On top of that, `browser_bridge_dialog`'s `accept: true` requires
`ack_message` to equal the dialog's own recorded `message` **exactly** —
checked extension-side (`dialogs.ts`'s `handleDialogWire`) against the
specific dialog instance still open under that `id`, not a string match
against any historical message. There is no way to "guess" past this: an id
whose dialog has already resolved (by the user, by auto-dismiss, or by an
earlier `browser_bridge_dialog` call) is refused as not-found regardless of
what `ack_message` says, so an accept can never be issued against a dialog
the caller has not actually read the current text of.

Tests: `extension/tests/background/dialogs.test.ts` (the recorder,
redaction, auto-dismiss timing, and the accept/dismiss gate, including the
G0.6.6 obligation — accept-with-`allowDialogAccept`-off is refused with no
gateway object anywhere in the test at all) and `extension/tests/background/
act-dialog-seam.test.ts` (the G1.4.5 seam: a dialog opening mid-action still
lands on that call's result; a leftover buffered dialog never leaks onto the
next call or across a navigation; a page.act whose `Runtime.evaluate` fails
throughout — modeling an open dialog — still returns by its own deadline
instead of hanging). Gateway side: `tests/test_g14_dialogs.py` (the
`dialogs` field surfacing on `browser_bridge_act`, gateway-side redaction
re-check, dismiss needing neither device power nor approval, accept's
fail-closed device-power gate, and accept falling through to the approval
queue under `full` per §12's ceiling).

## 17. Node-handle targeting: backendNodeId, content quads, and the narrowed TOCTOU (G2.6.2–.4)

**What changed.** An `idx`-targeted `act` used to resolve, at act time, only
through a CSS `selector` re-queried against the live page (G2.6.1's durable
selectors) — safe against reflow, but only as safe as the selector itself,
and dependent on a content-script `querySelector` round trip that a page
mutation can race. Three additions narrow this:

- **`backendNodeId` (G2.6.2).** Alongside `selector` in the gateway's index
  map (`attach.py`'s `_node_maps`), the extension now also records the CDP
  `backendNodeId` for each indexed element — resolved in the background
  worker (`act.ts`'s `resolveBackendNodeIds`), never by the content script,
  which has no `chrome.debugger` session and so cannot see one at all. A
  `backendNodeId` survives reflow/resize exactly (it names a specific node
  Chrome already knows about), where a selector only survives if it happened
  to be durable.

  **Cost, bounded three ways after review flagged the first version as an
  unbounded regression on the most-used tool.** The naive approach — resolve
  every index-map entry, unconditionally, on every `dom.snapshot` AND every
  `page.act`'s after-snapshot — is `2N+1` sequential `chrome.debugger.sendCommand`
  round trips for an N-entry map, and N scales with the page's
  interactive-element count, not with the fixed 4KB text budget (a busy
  admin console can plausibly have 150). That is real, unbounded, added
  latency on every action. The shipped design instead:
  - Runs the bulk pass (`resolveBackendNodeIds`) **only** from `dom.snapshot`
    (`handleDomSnapshot`), never from `page.act`'s own after-snapshot — an
    act's after-snapshot is rarely the map the very next act targets by idx,
    so bulk-resolving the whole page on every action bought little for a
    real, recurring cost.
  - Bounds that bulk pass three ways: **bounded concurrency** (16 entries in
    flight at once — `chrome.debugger.sendCommand` calls pipeline, so
    wall-clock is roughly `2N/16` round trips, not `2N`), a **150ms time
    budget** (no new entry starts once the deadline passes; already-dispatched
    calls are allowed to finish, since they can't be usefully cancelled), and
    a **100-entry cap** (entries beyond it are dropped before any CDP call at
    all). An entry that gets no handle from any of the three degrades to
    exactly the pre-existing behaviour (selector re-resolution) — never a
    hard failure — and the count of entries dropped this way rides the wire
    as `dom.snapshot.result.nodeMapSkipped` so it's diagnosable, not silent.
  - Adds an **on-demand, single-selector** resolution at act time
    (`resolveNodeHandleOnDemand`, inside `resolvePoint`) for whenever the
    target has no cached handle — a flat `DOM.getDocument` + `DOM.querySelector`
    + `DOM.describeNode`, three round trips, regardless of page size, for
    only the one element actually being acted on. This is what gives an idx
    the bulk pass skipped (cap, budget, or a shadow-piercing selector) the
    same fused resolve+verify+dispatch treatment anyway, just paid for once,
    on demand, instead of proactively for the whole page.

  Shadow-piercing selectors (`>>>`) are excluded from both paths outright —
  `DOM.querySelector` cannot evaluate them — so shadow-DOM targets never get
  a handle and always resolve by selector, in both the bulk and on-demand
  cases.

  **Measured (extension/tests/background/act.test.ts's "G2.6.2 cost bound"
  tests, simulated CDP latency, node's own `Date.now()` — not real Chrome,
  see `ProjectRules/Handoff.md`'s "genuinely unverified" list):** N=150 at
  5ms/CDP-call — the reviewer's own worked example — resolves in **~80–120ms**
  wall-clock (100 entries resolved, 50 skipped by the entry cap) versus a
  naive fully-serial cost of `(2*150+1)*5ms ≈ 1505ms`, roughly a 12–19x
  reduction, and the entry cap is what's binding at this N, not the time
  budget. A second case (N=80, under the cap, 40ms/call) shows the time
  budget binding instead: **~200–450ms** wall-clock versus a naive
  `(2*80+1)*40ms ≈ 6440ms`, with some but not all 80 entries resolved. Both
  bounds were proven load-bearing by temporarily removing them (`cp`-backed
  up, never `git checkout`) and re-running: with the budget and cap both
  set effectively unlimited, every entry resolved and the "some must be
  skipped" assertions failed — restored immediately after.
- **`DOM.getContentQuads` for the click point (G2.6.3).** `getBoundingClientRect`
  reports a single axis-aligned box, which is wrong for a CSS-transformed
  element or one wrapped across lines. The handle path instead reads the
  actual hit-testable quads and dispatches at the largest on-screen,
  non-zero-area quad's centre; when every quad is degenerate or entirely
  off-screen, the call refuses with `NO_HIT_TESTABLE_TARGET` (4217) instead
  of dispatching into nothing — a real behaviour change from the selector
  path, which has no equivalent refusal and can silently "succeed" against a
  clipped element's phantom centre.
- **Fused resolve+verify+dispatch (G2.6.4).** `DOM.describeNode` (verify),
  `DOM.scrollIntoViewIfNeeded` and `DOM.getContentQuads` (dispatch) are all
  issued against the SAME `backendNodeId` — closing the specific failure
  mode the two-`querySelector` design had: verify matches element A, a page
  mutation lands, dispatch's fresh query matches element B, and the call
  reports success against the wrong target. Chrome's own "no node with that
  id" answer for a removed/replaced node is treated as staleness (fall back
  to selector re-resolution), not a silent wrong click.

**How much of the documented TOCTOU actually closed — stated plainly, not
claimed as solved.** The gap between `DOM.describeNode` returning and the
`DOM.scrollIntoViewIfNeeded`/`DOM.getContentQuads`/dispatch calls that follow
is still several separate CDP round trips, not one atomic step. What closed
is the specific failure mode above: a wrong-element dispatch caused by two
independent queries disagreeing. What remains open: a mutation racing
exactly between `DOM.describeNode` and the dispatch that follows can still
happen — the difference is what that race now produces. `DOM.getContentQuads`
against a node that no longer exists fails cleanly and falls back to
selector re-resolution (the documented safe default), where the old design's
second `querySelector` would silently re-match a different live element and
report success. The window is narrower, not gone.

**The identity check at the handle is coarser than the content-script's.**
`nodeMatchesExpect` (`act.ts`) infers a role from `DOM.describeNode`'s tag
name/attributes and compares an explicit `aria-label` attribute — it does
**not** compute a full accessible name (no `aria-labelledby` resolution, no
composed text-content fallback), because that needs the `Accessibility`
domain, which G2.6.5 explicitly defers enabling. This is asymmetric on
purpose: a role mismatch is always a genuinely different control and always
refuses; an absent `aria-label` means "name not checked," never "refused" —
so this can only ADD a refusal for a confirmed wrong role, never manufacture
a false one for a name it has no way to compute here. The pre-existing
content-script `expect` check (`checkExpectedElement`, ELEMENT_MISMATCH)
still runs first, unchanged, with its full accessible-name comparison; this
is an additional, narrower check at the point coordinates are actually
resolved, not a replacement for it.

**Scope note:** the handle path covers `click`/`hover`/`key`-style point
resolution (`resolvePoint`) only. `type`'s own resolver
(`resolveEditableTarget`) still goes through the content script exclusively,
because it needs disabled/read-only/editable-ancestor detection that a CDP
node description alone doesn't give — extending the handle path to `type`
is out of scope here.

Tests: `extension/tests/background/act.test.ts` ("click by node handle: ...")
— the exact CDP call sequence for a successful handle resolution (and that
it never falls back to the content script), a stale handle (`DOM.describeNode`
failing) falling back to selector re-resolution, every content quad
zero-area/off-screen refusing `NO_HIT_TESTABLE_TARGET` with no
`Input.dispatchMouseEvent` ever sent, a coarse role mismatch refusing
`ELEMENT_MISMATCH` before `DOM.scrollIntoViewIfNeeded` or any dispatch runs,
on-demand resolution succeeding with no gateway-supplied `backendNodeId` at
all (and falling back silently, with no note, when it finds nothing). "G2.6.2
cost bound: ..." — the concurrency/cap/budget behaviour above under
simulated CDP latency, at both the entry-cap-binding size (N=150, 5ms/call)
and the time-budget-binding size (N=80 under the cap, 40ms/call), with both
bounds proven load-bearing by temporarily removing them and confirming every
entry then resolves (restored immediately after via a `cp` backup, never
`git checkout`). Gateway side: `tests/test_m2_act.py` (`nodeMap` storage/
resolution/wholesale-replacement in lockstep with `indexMap`/`indexMeta`,
`backendNodeId` forwarded alongside `selector`/`expect`, and back-compat when
no `nodeMap` was ever sent — this plumbing is exercised even though
`page.act`'s own result never populates `nodeMap` in practice, see above).
Not built here (S1 owns `fixtures/pages/*`): a live CSS-transform or
scrolling-container fixture — the unit tests above validate the quad
geometry and CDP call sequencing a transform/scrolled target would produce,
but the fixture and a real-Chrome pass against it (including the actual,
non-simulated CDP round-trip latency the cost-bound numbers above stand in
for) remain on the "genuinely unverified" list.

## 18. Drag and drop (G1.2) — no new capability; slider CAPTCHAs stay out of scope

`action: "drag"` (`browser_bridge_act`) drives a real
`Input.dispatchMouseEvent` press → ≥10 interpolated `mouseMoved` waypoints
(`buttons: 1` held) → release sequence at CDP level — the same `act`
capability and gates as click/type/etc, nothing dangerous-capability-shaped
added. Two mechanisms were considered; only one is built:

**Only the pointer path is implemented.** Chromium 117 added
`InputHandler::DragController`: with drag interception disabled (the
default — nothing in this codebase enables it), a plain CDP mouse sequence
drives real native drag-and-drop directly, for both pointer-event-based
sortable lists and `draggable="true"` HTML5 drag-and-drop alike. A separate
`Input.setInterceptDrags`/`dispatchDragEvent` mechanism exists in CDP but is
**not built here**: those surfaces are marked experimental, `setInterceptDrags`
is **tab-global state** that silently breaks the user's own manual dragging
on that tab until something disables it again (and a `finally` block cannot
guarantee that once Chrome kills the service worker mid-drag), and the
`dragIntercepted` → `dispatchDragEvent` round trip **loses file drag
payloads** outright. None of that risk buys anything the pointer path
doesn't already cover for an ordinary drag. `mode: "html5"` is therefore
refused outright with `DRAG_HTML5_UNSUPPORTED` (4219) rather than silently
falling back — an agent that explicitly asked for the html5 mechanism gets a
named refusal, not a different mechanism it didn't ask for.

**The same staleness guards `click`/`type` get, applied to the destination
too.** `to_idx`/`to_selector`/`to_xy` get their own `expectTo` (the
ELEMENT_MISMATCH counterpart of `expect` — a same-page re-render must not
silently redirect a drop onto a different destination, e.g. a reordered
kanban column) and `expectToViewport` (the VIEWPORT_MISMATCH counterpart of
`expectViewport`, see G2.5 in `ProjectRules/coveragegaps.md`) — checked once right before the
drag starts moving and **again immediately before the drop**, so a resize
that lands partway through the waypoint loop is still caught rather than only
one that happened before the drag began. An abort past the mouse-press
always releases the button at the last point reached first, so a refused
drag never leaves Chrome (or the user's own next click) believing the left
button is still held down.

**Slider CAPTCHAs and other bot-detection challenges are permanently out of
scope.** This is stated as guidance to the model (`ACT_SCHEMA`'s `action` and
`mode` descriptions), not a technical control — nothing about the pointer
path specifically targets or defeats a CAPTCHA, and this bridge will not be
extended to help with one.

Tests: `extension/tests/background/act.test.ts` (the exact pointer CDP call
sequence, ≥10 waypoints with `buttons: 1`, the destination resolved fresh
*after* the press rather than cached from before it, the mid-drag viewport
abort releasing the button at the last waypoint reached, the destination
ELEMENT_MISMATCH pre-flight, and `mode: "html5"` refused with no CDP dispatch
at all) and `extension/tests/offscreen/offscreen.test.ts` (the §0.4
silent-drop seam for every new param). Gateway side: `tests/test_m2_act.py`
(enum acceptance, the destination-required local refusal, `to_idx` resolving
through the same registry as `idx` and carrying `expectTo` from the
snapshot's `indexMeta`, and `mode` validation/marshalling).

## 19. File upload (G1.3) — two tiers, two settings, each checked at its own entry point

`browser_bridge_upload` is the one tool this product has for picking a file
for a page's `<input type=file>` — deliberately **not** a `browser_bridge_act`
action reachable through the ordinary `act` capability, even though tier 1
(below) rides `page.act`'s own wire frame and reuses `act.ts`'s element
resolution end to end. `upload` is one of G0.5's `DANGEROUS_CAPABILITIES`: a
`full` origin grant never covers it, `always`/`session` are refused
server-side (`approvals.py`'s `allow_session=allow_permanent=False` for this
capability, per G0.5.4 — the same three-in-`NO_STANDING_GRANT_CAPABILITIES`
set as `evaluate`/`http_auth`), and every call raises a live approval prompt
naming the file's **basename and origin only**, never a full path (which can
embed a username or home-directory layout) and never the file's contents.

**Two tiers, gated by two independent device settings (§0.6 rows 1/2), and —
this is the point the earlier settings scaffold's own comment already
called out — each tier's entry point checks ONLY its own setting, never the
generic "either" check `_CAPABILITY_POWER_KEYS["upload"]` /
`CAPABILITY_POWER_KEYS.upload` answers:**

1. **Tier 1 — `file_path`, a local path off the browser's own host machine.**
   Gated by `allowFileUpload` (off by default). `hermes_plugin/upload.py`'s
   `_handle_tier1` calls `state.power_enabled(device_id, "allowFileUpload")`
   directly — never `_device_power_denial`'s generic upload check, which
   would incorrectly pass on `allowFileUploadFromAgent` alone.
   `extension/src/background/act.ts`'s `actUpload` mirrors this with
   `powers.ts`'s `assertLocalFileUploadAllowed` (checks `allowFileUpload`
   only). The path then goes through the allowlist below. On success, the
   wire frame is an ordinary `page.act` call with `action: "upload"` and a
   new `filePath` field, resolved to a CDP node handle (never a point — there
   is nothing to click) and passed to `DOM.setFileInputFiles`.
2. **Tier 2 — `content_base64`, bytes the agent already holds.** Gated by
   `allowFileUploadFromAgent` (off by default), capped at `maxUploadBytes`
   (default 10 MB), enforced on **both** sides before the frame is even sent
   (`hermes_plugin/upload.py`'s `_handle_tier2` decodes and measures the
   base64 payload; `extension/src/background/upload.ts`'s
   `handleUploadBytes` re-measures it independently). No local path is
   involved at all — the extension builds a `File`/`DataTransfer` in the
   page's **MAIN world** (`chrome.scripting.executeScript({world: "MAIN"})`,
   not the isolated content-script world, so a page's own `instanceof File`
   check sees what it expects) and assigns `input.files` directly, over a
   **distinct wire method, `page.upload`** — never `page.act`, since there is
   no CDP dispatch, no destination, and no settle/diff to share with that
   switch. `mimeType` is advisory only: the decoded bytes' own magic number
   is sniffed, an executable signature (`MZ`/`ELF`/Mach-O, any of the four
   byte orders) is refused **regardless of the declared type**, and a
   declared PNG/JPEG/GIF/PDF whose signature doesn't match is refused as a
   mismatch — anything outside that small known-signature table is
   unverifiable and passes through un-refused, per the size cap and mandatory
   approval being the real bound for what this check cannot verify.

**The path allowlist (tier 1 only) — string validation, and why it can never
be more than that.** This gateway has **no filesystem access to the path at
all**: `file_path` names a file on the browser's own host machine, a
*different computer* on the LAN (`ProjectFiles/CLAUDE.md`) than the one this
Python process runs on, and the extension itself has no general filesystem
API beyond the one CDP call that actually reads the file. So both
`hermes_plugin/upload.py`'s `_validate_upload_path` and
`extension/src/background/upload.ts`'s `validateLocalUploadPath` — kept in
lockstep by hand, mirroring the same discipline `_CAPABILITY_POWER_KEYS` /
`CAPABILITY_POWER_KEYS` and `POWER_KINDS` already require — are pure string
checks, in this order:
  - any control character (U+0000–U+001F, or U+007F) anywhere in the path is
    refused outright — a NUL specifically can truncate the string a
    lower-level filesystem call sees (the OS call underneath
    `DOM.setFileInputFiles` may stop at the first `\0` even though this JS/
    Python string keeps every character after it), so a path that *looks*
    like it ends safely inside an allowed root could, in the OS's own
    reading of it, name something entirely different
  - the path must be **absolute** — POSIX: a literal leading `/`; Windows: a
    single drive letter immediately followed by `:` and a separator (`C:\`
    or `C:/`). Everything else is refused, including an ordinary relative
    path (`Users/you/Downloads/report.csv` — found in a post-merge probe to
    pass both validators, because the segment/root comparison below never
    looked at whether the path had a leading separator at all, and a
    relative path's segments can coincide exactly with an absolute root's),
    a Windows drive-relative path (`C:foo`, no separator right after the
    colon) and a Windows root-relative path (`\foo`, no drive letter). This
    matters because `DOM.setFileInputFiles` resolves a relative path against
    **Chrome's own process working directory**, which this allowlist has no
    way to see or bound — a path that "matches" the allowlist's segment
    comparison could still land anywhere on disk
  - any raw `..` path segment is refused **outright, never resolved**
    (collapsing it algorithmically would still only be operating on the
    string; a flat rejection is simpler and no weaker)
  - a hidden segment (leading `.`) or a small named list of known
    secret/profile locations (`.ssh`, `.aws`, `.gnupg`, `keychains`,
    `user data`, …) is refused **even inside an otherwise-allowed root**
  - the remaining path must sit under one of the device's `uploadRoots`
    entries, compared **segment-by-segment**, never a bare string prefix (a
    root `/Users/you/Downloads` must not match the unrelated sibling
    `/Users/you/Downloads-evil-twin`, which shares every character of the
    prefix but is a different directory)
  - `uploadRoots` empty (the shipped default) denies every path — enabling
    `allowFileUpload` alone grants nothing

`fixtures/upload-path-cases.json` is the single source of truth for the
expected verdict of every hostile path this allowlist has been tested
against (26 cases, sourced from an orchestrator probe plus the defects it
found) — consumed by BOTH `tests/test_g13_upload.py` and
`extension/tests/background/upload.test.ts`, so the two validators can never
silently drift on one without the other's test suite catching it.

**What this cannot close, stated plainly rather than implied away:** a
symlink inside an allowlisted root that points outside it is a real,
unclosed gap. Neither side of this socket can `realpath()`/`readlink()` the
target machine's filesystem to see through it — the gateway has no access to
that disk at all, and the extension has no such API either. This is not an
oversight; it is the corrected reading of an earlier draft's mistaken plan to
"reject symlink-looking segments," which is undecidable from a string alone.
`tests/test_g13_upload.py` and `extension/tests/background/upload.test.ts`
each prove this gap is real today (a genuine symlink, created on the test
runner's own disk, pointing out of an allowlisted root) rather than silently
assuming it, so a future change that claims to close it fails an explicit
regression test until it actually adds real filesystem resolution on both
ends — which the current architecture (a gateway on one machine, a browser
on another) has no path to doing at all.

**The manual step (G1.3.2):** `DOM.setFileInputFiles` checks Chrome's
`MayReadLocalFiles`, which for an extension driving `chrome.debugger`
resolves to that extension's own **"Allow access to file URLs"** toggle on
`chrome://extensions` — off by default for every extension Chrome installs.
`act.ts`'s `actUpload` detects this specifically
(`chrome.extension.isAllowedFileSchemeAccess()`) and refuses with the named
error `UPLOAD_FILE_URL_ACCESS_DISABLED` (4223) rather than surfacing
`DOM.setFileInputFiles`'s own opaque `"Not allowed"` failure — see
`docs/troubleshooting.md`'s upload section for where to flip it. This check
is a detection, not a security boundary of its own: a false "allowed" reading
(the API missing entirely, e.g. in a test harness or a future Chrome build)
fails open on the DETECTION only, leaving `DOM.setFileInputFiles` itself as
the real, unavoidable gate either way.

**Verification (G1.3.6):** both tiers read back
`input.files[0].{name,size,type}` through the content script
(`readFileInputValue` in `content/targets.ts`) after the pick —
**never** `input.value`, which every browser reports as the fake
`C:\fakepath\...` string regardless of what was actually chosen. This is the
same "evidence, not a bare success flag" principle `type`'s `fieldValue`
follows: a read-back failure never turns an already-successful
`DOM.setFileInputFiles`/`input.files` assignment into a failed action, but a
successful one is exactly the proof "the form accepted a file" needs.

**Error codes 4222 (`UPLOAD_PATH_DENIED`) and 4223
(`UPLOAD_FILE_URL_ACCESS_DISABLED`)** are this feature's, allocated in
`coveragegaps.md`'s §0.4a table.

Tests: `extension/tests/background/upload.test.ts` (the path allowlist's
string rules including both named bypasses, tier 2's power gate/size
cap/magic-byte sniff, the seam from real `chrome.storage.local` to
`handleUploadBytes`), `extension/tests/background/act.test.ts` (tier 1 wired
at the real `page.act` entry point: the power gate, the path allowlist, the
file-url-access toggle, the exact `DOM.setFileInputFiles` call, and the
read-back attached as `fileInfo`), `extension/tests/background/powers.test.ts`
(the two tier-specific hooks each check only their own setting, and disagree
with the generic either-check by construction), and `tests/test_g13_upload.py`
(both tiers end to end against a fake extension: each tier's own gate,
cross-tier leakage refused, the traversal and hidden-directory refusals, the
symlink gap proven real, the dangerous-capability ceiling, and the approval
summary never carrying the full path).
## 20. Arbitrary JS evaluation (G1.5) — the DevTools console, handed to the agent

`browser_bridge_evaluate` (wire method `page.evaluate`) runs an arbitrary JS
expression inside the attached tab's own page context via CDP
`Runtime.evaluate`. This is deliberately the single most powerful capability
in the bridge, and it is stated here in plain terms before anything else about
it, per this plan's own rule that the blast radius is written down before the
feature is built.

**What `world: "main"` — the only world this ships with — actually hands the
agent, in the page's own security context, with the user's own cookies and
session already attached:**

- **`document.cookie`** — every non-`HttpOnly` cookie for the origin, readable
  **and writable**. `HttpOnly` cookies stay out of reach (that is what the flag
  is for), but everything else in the jar is not.
- **`localStorage` / `sessionStorage`** — read and write, including whatever a
  site's own frontend stashes there (auth tokens, feature flags, cached API
  responses).
- **`fetch()`, in the page's own context** — with the page's own cookies,
  CSRF header conventions, and Origin/Referer, indistinguishable from a request
  the page's own script made. This is `browser_bridge_fetch` (§5) without the
  SSRF guard: that guard is enforced in `session_powers.py`, entirely outside
  the page, and an evaluated expression's `fetch()` call never passes through
  it. **`allowEvaluate` is therefore not an independent seventh capability —
  it is a superset of `cookies`, `cookies_write` and `fetch` combined.**
  Turning it on makes those three gates advisory for this origin: an agent
  that cannot get an approval for `cookies_write` directly can still write a
  cookie via `document.cookie = ...` inside an evaluated expression, and the
  origin-attachment bound §13 places on `browser_bridge_cookie_set`
  (`COOKIE_WRITE_ORIGIN_UNATTACHED`) does not apply to a page-context write —
  the page can only ever write cookies for its own origin anyway, which is a
  narrower, not wider, scope than the cookie-set tool's, but it is a
  **different** enforcement mechanism, not the same one. Say this in the
  tool's own schema text, not only here, so the model's context window carries
  the same warning the human approving it sees.
- **DOM and page state** — everything `browser_bridge_read`/`snapshot` expose
  and more: full innerHTML, event listeners a script attached, in-memory
  application state a framework holds off the DOM entirely (Redux stores,
  React fiber internals, anything reachable from `window`).
- **Everything CDP's `Runtime.evaluate` executes with** — no separate sandbox
  beyond what the page's own CSP and origin isolation already provide.
  `Runtime.callFunctionOn`, `Runtime.awaitPromise` and friends stay off the
  CDP whitelist (§4); only `Runtime.evaluate` itself is allowed, and it always
  runs in the page's existing execution context, never a detached one.

**This is not a new hole opened by a bug — it is the DevTools console,
verbatim, and is why it ships off by default** (`allowEvaluate`, row 5 of the
Powers table, default **off**), gated by the `evaluate` capability (one of
`approvals.py`'s seven `DANGEROUS_CAPABILITIES`, §12), and — unlike every
other capability — **NEVER available as a standing grant**: `evaluate` is one
of the three members of `NO_STANDING_GRANT_CAPABILITIES` (alongside `upload`
and `http_auth`), so `always` and `session` are not offered as approval
choices at all (§12), and a `full`-mode origin still prompts for it every
time (the per-capability ceiling, §12, applies in full here). Every single
evaluation is a fresh, individually-reviewed decision.

**The approval prompt shows the expression itself, not just a label.** The
extension's popup renders only the approval's `summary` field (not `detail` —
current UI has no surface for `detail` at all), so the expression is placed
in `summary`, truncated for display length, with the same secret-shaped
substrings masked that the audit trail masks (see below) — never swapped for
a different string between what is shown and what runs. `hermes_plugin/evaluate.py`
captures `expression` into one local variable once, before the approval call,
and every later reference (the wire payload sent to the extension, the audit
record) reads that same variable — there is no code path that re-reads the
tool's `args` after the approval decision comes back, so there is no seam for
an approved-then-swapped expression to exist in.

**Auditing the expression, and why it is redacted first.** G1.5.5's brief is
to audit the expression verbatim — the log is what makes this capability
reviewable after the fact. But the expression is not always something a human
typed: the agent composes it, and it can legitimately contain a secret the
agent itself just read off the page a turn earlier (replaying a bearer token
it saw in a response header, or a session cookie value, back into a
`fetch()` call). The live approval prompt is seen once, by the one person who
already has the page open and legitimate access to whatever secret it
contains; `audit.jsonl` is a persistent file, rotated and kept for
`audit_keep` generations, that anyone doing an operational or security review
of this device will read later, out of that context. Those are different
exposure profiles, so this plan does not treat them the same way: the
expression written to `audit.jsonl` (`tool_evaluate`'s `expression` field) is
passed through the same secret-shaped-substring redaction the console-reading
capability already applies to page-authored text (JWTs, `Bearer` tokens,
`api_key=`/`access_token=`-style assignments and URL query parameters, bare
AWS access key ids — `hermes_plugin/evaluate.py`'s own copy of the patterns
`session_powers.py`'s `_redact_console_secrets` and
`extension/src/lib/redaction.ts`'s `redactSecrets` already carry, plus
`_gateway_redact`'s card/SSN/email/phone/password-hint pass) before it is
ever written. This redacts *values*, not *code shape*: the structure of the
expression — which functions it calls, which URL it fetches, which field it
reads — survives in full; only a literal secret-shaped span inside it is
masked. The **wire payload actually executed is never touched by this pass**
— only the audit copy and the approval-prompt display copy are.

**The result is capped and redacted the same way, extension-side, before the
frame ever leaves the machine (§0.7 of `ProjectRules/coveragegaps.md`).**
`returnByValue: true` is always set — a live `objectId` handle back into the
page is never returned on any path, closing off a second way to reach page
state that bypasses every gate above. The serialised result is capped at 32
KB (truncated, not rejected, with `truncated: true` on the response) before
`redactText` (the user's own card/SSN/email/phone/password policy) and
`redactSecrets` (the unconditional JWT/bearer/API-key/AWS pass) both run over
it, exactly as they already run over console entries (§16's `console`
capability). `hermes_plugin/evaluate.py` re-checks with `_gateway_redact` as
the belt-and-braces second pass, per §0.7's stated invariant. A thrown
exception's `exceptionDetails` is serialised into a real tool-level error
(mirroring `browser_bridge_fetch`'s pattern, §5) rather than surfacing a raw
CDP structure.

**A hard timeout on every evaluation, bounded two ways, because a dialog can
wedge this specific CDP method in a way no other call on this whitelist is
vulnerable to.** §16 already records that `Runtime.evaluate` is explicitly
excluded from Blink's V8-interrupt escape hatch — while a native JS dialog is
open on the page, `Runtime.evaluate` does not merely fail, it **cannot be
interrupted by CDP's own `timeout` parameter either**, because that
parameter's termination mechanism is itself a V8 interrupt. So this capability
carries a **second, independent bound that does not depend on the page's main
thread cooperating at all**: a background-side wall-clock `Promise.race`
around the `chrome.debugger.sendCommand` call, which resolves a `TIMEOUT`
error to the caller the moment the deadline passes, whether or not Chrome's
own command ever answers. This is a bound on **waiting**, not a bound on
**execution** — a `while(true)` loop, a promise that never settles, or an
expression that opens a `confirm()` can all continue running/wedged in the
page after this bound fires; the extension gives up on that one call rather
than leaving every subsequent `browser_bridge_*` call on the tab hung behind
it forever, which is the exact failure §16 documents for dialogs and this
capability inherits by construction (any evaluate can open one). A best-effort
`Runtime.terminateExecution` is fired (never awaited, never allowed to affect
the response already sent) after the wall-clock bound trips, to reduce —
not guarantee — how long a runaway loop keeps running after the caller has
already moved on.

**`world: "isolated"` does not ship in this change.** It is the world that
can see closed shadow roots (`chrome.dom.openOrClosedShadowRoot`, useful) but
also the world the content script's own `chrome.runtime.sendMessage` channel
lives in — an isolated-world expression could forge a message the background
service worker would otherwise only ever receive from this extension's own
content script, and closing that hazard (a dedicated `chrome.scripting`
world, or sender authentication in the `onMessage` handler) is its own piece
of work, not a checkbox on this one. `world` is refused to any value other
than `"main"` today, both extension-side and gateway-side
(`EVAL_WORLD_UNSUPPORTED`, 4224) — and the tool schema the model reads does
not expose the parameter at all yet, so the model cannot ask for something
that would just be refused.

**Expression size is capped (~4 KB) before an approval is ever raised**
(`EVAL_EXPRESSION_TOO_LARGE`, 4225) — both so the approval prompt stays
readable (an unreadable prompt is not consent, per G1.5.4) and so the audit
log's `tool_evaluate` volume stays bounded against `audit_max_bytes`.

Tests: `extension/tests/background/evaluate.test.ts` (the local
`assertEvaluateAllowed` refusal with the gateway entirely uninvolved; the
wall-clock timeout firing against a `chrome.debugger.sendCommand` that never
settles at all — the dialog-wedge case, the infinite-loop case and the
never-resolving-promise case are all the same code path from this module's
point of view and are each exercised directly; `world` values other than
`"main"` refused; the expression cap refused; the 32 KB result cap with
`truncated: true`; `redactText`+`redactSecrets` both running over a result
that echoes `document.cookie`-shaped and bearer-token-shaped content; no
`objectId` ever present on the wire). Gateway side: `tests/test_g15_evaluate.py`
(the expression sent to `relay.call` is byte-identical to the one supplied,
even when the audit/approval-display copy has been redacted; `evaluate` is
refused on a `full` origin without an approval; `always`/`session` are
refused for `evaluate` — the existing `NO_STANDING_GRANT_CAPABILITIES`
machinery in `tests/test_m2_approvals.py` already covers this generically and
is not re-derived here; the operator kill switch and device-power-policy
denial both fire before any approval is raised).

## 21. Iframes: every frame is gated by its OWN origin (G2.2.13)

**The bug this section documents the fix for.** The first G2.2.2–4 build
included every cross-origin iframe's content in a snapshot of a `full` origin,
whatever that frame's own grant was — a `full` grant for `site.example`
silently read an embedded `bank.example` widget the user never separately
approved. `frames.ts`'s `fetchChild()` fetched a mapped cross-origin frame
with no origin check at all, and `tools.py`'s `handle_snapshot` only ever
gated the top tab's own origin.

**The rule now:** a frame's content is collected only when **its own origin**
is granted `full` — a same-site subdomain (`widgets.site.example`) is a
different origin from its apex (`site.example`) and needs its own grant, full
stop. An ungranted frame is never walked or injected into at all; it appears
only as a placeholder naming its own origin (`[frame https://bank.example —
not granted]`), plus a `frameGaps` entry (`reason: "not_granted"`) so the
agent can ask the user to grant exactly that origin. The content is **never
collected-then-dropped** — enforced at the point of collection
(`extension/src/content/walker.ts`, `extension/src/background/frames.ts`),
not as an afterthought.

Two layers, deliberately asymmetric in what they can and can't catch:

1. **The extension is told which origins are granted** — `dom.snapshot`'s
   `granted_origins`/`denied_origins`/`default_full` params
   (`hermes_plugin/tools.py`'s `_origin_policy_for_device`) — and must never
   inject into, walk, or otherwise collect from an ungranted frame. `default_full`
   is **always sent as `false`**: `config.py`'s `default_mode` answers "may
   the device read the tab the user navigated to", which is the right
   default for the top tab (already gated separately, before this RPC is
   sent) but the wrong one for a third-party frame embedded on that page —
   an origin the user never separately approved must never read as granted
   purely because the fleet's tab-level default happens to be permissive.
2. **The gateway re-checks what actually came back** (`tools.py`'s
   `_reconcile_frame_origins`) — belt-and-braces against a stale policy
   snapshot, a race between grant and fetch, or an extension build that is
   out of date or has a reporting bug. It verifies every frame in the
   result's `frame_origins` map (a frame-hop selector prefix -> origin) has
   an **explicit** `full` grant (never `_gate_reason`'s default-mode
   fallback — see point 1's reasoning, applied gateway-side too), drops any
   `indexMap` entry and marker-delimited text block belonging to a
   not-granted frame (wholesale, including anything nested inside it,
   regardless of what THAT nested frame's own origin says — nothing inside
   an unverified frame's subtree is trustworthy), and fails closed —
   dropping every frame-qualified `indexMap` entry — when `frame_origins` is
   missing, empty, or doesn't mention a given entry's frame at all. A denied
   origin always beats a granted one when (through a stale or conflicting
   state) the same origin somehow appears in both lists.
   Origin comparison is **normalized** identically on both sides (scheme/host
   lowercased, the scheme's default port stripped, a trailing DNS-root dot
   stripped, a unicode IDN host compared by its punycode form —
   `extension/src/lib/origin-policy.ts`'s `canonicalizeOrigin` and
   `hermes_plugin/tools.py`'s `_canonicalize_origin`, checked against the
   same shared vector file, `fixtures/origin-normalization-cases.json`) so a
   grant recorded with one spelling still matches an origin reported with an
   equivalent one.

**Marker forgery (a page cannot escape or fake a frame boundary).** The
gateway's belt-and-braces text stripping relies on an internal
U+0000-delimited marker pair the extension wraps each separately-fetched
frame's content in (`FRAME_MARKER_START`/`FRAME_MARKER_END`, always stripped
before the model ever sees one, granted or not). A page script CAN set a
literal U+0000 into `textContent`/an attribute value, so without a fix a
hostile page could plant the exact marker sequence and close a real frame
block early or open a fake one. Fixed **at the source**, not at the
marker-parsing end: every line of page-derived text the walker prints (every
name, value, text node, attribute-derived string) has U+0000 replaced with
U+FFFD before it is ever assembled into the snapshot tree
(`content/walker.ts`'s `sanitizeControlChars`), so a real marker can only
ever originate from `frames.ts`'s own serialization. As a second, independent
layer, the gateway also validates that markers are **balanced** before
trusting any of them (`_frame_markers_balanced`): an END with nothing open,
a START left open at end of text, or an END before its matching START are
all treated as malformed, and the gateway drops **all** frame content from
that snapshot rather than guessing which block is real (audited as
`frame_markers_malformed`).

**Accepted limit, stated plainly — not fixed, because it cannot be from
here.** The gateway's re-check can only act on what the extension *reports*.
A misbehaving extension build that mislabels its own `frame_origins` map, or
that places a frame's content directly into the snapshot text **outside**
any marker block entirely, is not detectable from the gateway side — the
extension is the sole collection point, and the gateway has no independent
way to observe the page the extension is looking at. This whole section's
gateway layer catches an extension that is **stale or buggy about
reporting** (an older build, a race, a bookkeeping slip); it is not, and
cannot be, a defence against an extension that is actively compromised or
deliberately lying about what it collected. The primary defence against that
is layer 1 — never collecting the content in the first place — which is why
it is not optional.

## 22. Limited (no-debugger) share mode — automatic fallback, same security gates

**The problem this section documents the fix for.** `chrome.debugger.attach()`
is refused ("Cannot access a chrome-extension:// URL of different extension")
whenever another extension has a frame anywhere in the tab's WebContents —
including a Chrome-preloaded background/inline page that never appears in
the tab strip — and Chrome can also drop an *already-attached* session the
instant such a frame reappears (`FOREIGN_EXTENSION_FRAME_DETECTED` /
`FOREIGN_FRAME_SESSION_DROPPED`, §14 of `hermes_plugin/skill/SKILL.md`). A password manager
is the common real-world trigger, and it cannot be disabled or unpaired from
here.

**The fix:** `background/cdp.ts`'s `attach()` no longer fails the share on
either of those two CONFIRMED outcomes — it falls back to `attachLimited()`
instead. The tab stays shared, `attachMode` (protocol/schema.json's
`tabRef.attachMode`) reports `"limited"` (plus `attachModeReason`), and every
capability that never actually needed `chrome.debugger` keeps working,
driven through `chrome.scripting`/content scripts and `chrome.tabs`:
`dom.snapshot`/`page.read` (the walk was always content-script based —
`cdp.isAttached()` was only ever a precondition, not a dependency),
`cookies.get`/`cookies.set` (`chrome.cookies`, never CDP), and `page.act`'s
`click`/`type`/`select`/`submit`/`scroll`/`key`/`hover`/`wait_for`/`navigate`
(a real `el.focus()`+`el.click()`, a native value-setter + `input`/`change`,
a synthetic `KeyboardEvent`, `execCommand('insertText')` for contenteditable,
`chrome.tabs.update`) plus the new mode-agnostic `back`/`forward`/`reload`
actions. A plain whole-viewport `page.screenshot` (no `selector`/`region`/
`full`) uses `chrome.tabs.captureVisibleTab`, refused clearly when the tab
isn't the focused window's active tab (§2.3.1's `activeTab`-needs-a-gesture
finding rules out anything more general).

Refused, through the SAME `limited_mode_capability_unavailable` G0.8
catalogue entry (`LIMITED_MODE_CAPABILITY_UNAVAILABLE`, code 4239) every
call site formats identically via `cdpOnlyGate()` (`background/cdp.ts`): a
clipped/selector/region/full-page screenshot, `drag`, `upload`,
`page.evaluate`, JS dialogs, `network.log`, `console.entries` and
`downloads.search` — every one of these still needs a real `chrome.debugger`
session and has no DOM-native substitute. `browser_bridge_http_auth_status`
and cookie ops are unaffected either way — neither was ever CDP-based.

**Security parity (no separate, weaker gate for limited mode).** Every
limited-mode action goes through the EXACT SAME gateway-side authorization
as a full-mode one — origin mode, approvals, `NO_STANDING_GRANT`
capabilities and power toggles (§0, §1, §12 above) never branch on
`attachMode` at all, because the gateway's own tool handlers
(`hermes_plugin/tools.py`) don't distinguish it; `attachMode` is reported
for the AGENT's information (which capabilities to expect refused), never
consulted as an authorization input. Extension-side, `page.act`'s
frame-origin authorization (§21's TOCTOU fix, `checkAuthorizedFrameOrigin`)
and frame resolution (`background/frames.ts`'s `resolveFrameTarget`, itself
`chrome.webNavigation`-based, never CDP) run identically before
`performAction()` ever picks between the CDP and DOM-native dispatch
families — an ungranted or navigated-away frame is refused
(`FRAME_ORIGIN_DENIED`) before any dispatch, limited or full
(`extension/tests/background/act-limited-mode.test.ts`). The redaction
paths (§3) are untouched: the content-script snapshot/read/diff machinery a
limited tab uses is the SAME code a full tab's before/after diff already ran
through.

**Opportunistic upgrade.** `cdp.ts`'s `maybeUpgrade()` quietly retries the
real `chrome.debugger.attach()` for a limited tab — called before every
`page.act` on that tab, throttled to at most once per
`UPGRADE_RETRY_INTERVAL_MS` (5s) per tab, never a hot loop. The moment it
succeeds, the tab is moved out of the limited set, the persistent
foreign-frame guard is armed exactly like any other successful attach, and
an `attach.modeChanged` notification reports the change (mirrors
`attach.sessionDropped`'s own audit-only, never-page-content shape).

## 23. Replay: a local, off-by-default recording of what the agent did (G7)

**What it is.** An Options toggle, **off by default**: "Record agent
activity for replay." When on, `background/replay-capture.ts` hooks the one
point every `page.act` call already funnels through (`act.ts`'s
`handlePageAct`) and, best-effort, captures one small frame plus a short
action record after each action on a shared tab.

**What is stored, per action:**
- a JPEG frame, downscaled to half size and re-encoded at quality 50 (a
  "low-cost" frame by design — this is a scrubbable log, not a lossless
  record) — the SAME `Page.captureScreenshot` (full mode) /
  `chrome.tabs.captureVisibleTab` (limited mode) path `page.screenshot`
  already uses, with the presence overlay hidden first exactly the way a
  screenshot hides it (`presenceHideForCapture`/`presenceRestoreAfterCapture`
  — visibility, not removal, restored whether or not the capture succeeded).
  No set-of-marks badges are ever drawn for a replay frame.
- the action's kind (`click`, `type`, `fill`, ...), the tab id, a timestamp,
  and whether it succeeded.
- a short caption naming what was acted on — an accessible role+name for
  click/hover (already redacted by `act.ts`'s own D3 pass, then redacted
  AGAIN by `replay-store.ts`'s `redactCaption`, belt-and-braces), or a field
  selector for `type`/`fill`.

**What is never stored:** the text typed into a field, a `fill` field's
value, or any other value the agent entered. Only the SELECTOR/field name is
recorded, never the value — see `replay-capture.ts`'s `ReplayActContext` and
`describeReplayTarget`, which never read `msg.text` or a fill field's
`value` in the first place (there is no redaction step to forget; the value
never reaches this code path at all).

**Where it lives.** Entirely local, in this extension's own IndexedDB
database (`hermes-replay`, `src/lib/replay-store.ts`) — never sent to the
gateway, never part of `device.hello`/`state.report`, no new
`protocol/schema.json` field. Bounded two ways, oldest-first: the "keep the
last N actions" retention setting (default 200, max 1000) and a hard 50MB
total-byte cap, whichever is tighter (`pickEvictions`).

**Where it's read.** Only the popup's own "Replay" view
(`src/popup/replay-ui.ts`), which opens the same IndexedDB database directly
(same extension origin — no message round trip needed) to render the
scrubber. "Export" writes a zip of the frames plus an `actions.json`
manifest via `chrome.downloads.download`, invoked only from that button's own
click handler, never automatically. "Clear" empties the database.

**Focus/cost discipline.** Capture never activates or focuses a tab (see
`background/focus-audit.ts`'s own header on why that matters here); a
limited-mode tab that isn't currently visible is simply skipped for that one
action, never forced to the front. At most one extra `chrome.debugger` round
trip (`Page.captureScreenshot`) is added per action, and only when the
setting is on — a device that leaves it off pays exactly one
`chrome.storage.local.get` per act and nothing else.

## 24. Silent fetch: a tab-less, unattended request lane (SF1–SF7)

Full detail (grant model, config keys, spill/cache, troubleshooting) lives in
[`silent-fetch.md`](silent-fetch.md); this section is the "what's enforced
and where" companion the rest of this file follows.

`browser_bridge_silent_fetch` is the one tool in this product with **no tab
surface at all** — `tab_id` is refused outright if passed, not silently
ignored, because origin → worker resolution is entirely internal to the
extension's hidden worker pool and this plugin's grants. Everything that
gates it lives gateway-side (`hermes_plugin/silent_grants.py`); the
extension's pool is deliberately treated as untrusted plumbing that "trusts
nothing but its own kill switch" (`ProjectRules/silentfetch.md`'s own design
rule 4).

**Two gates, not one, and they compose rather than substitute:**

1. The origin's ordinary access mode (`off`/`request`/`full`) — an `off`
   origin is unreachable by this tool exactly like every other one.
2. The origin's own **"Background requests"** popup setting
   (off/ask/always), which is checked independently of, and can override,
   what the ordinary mode alone would imply. A `full` origin with
   "Background requests" set to `off` is refused for silent fetch even
   though every other tool on it runs unprompted; an origin only sitting at
   `request` can still be set to "Always allow" for this lane specifically.

**Approvals here use the `silent_fetch` capability**, distinct from the
existing `fetch` capability `browser_bridge_fetch` uses — a standing session
grant for one never silently covers the other, even against the identical
origin. Like every other capability in `NO_STANDING_GRANT_CAPABILITIES`'s
spirit (though implemented as its own explicit rule in `authorize_silent_fetch`
rather than that shared constant), a granted "ask" here is session-scoped
only: it never promotes an origin to a standing "always" popup row the way
an `act` approval's `always` scope promotes to standing `full`. The user has
to explicitly flip the popup control to "Always allow" for that to happen.

**SSRF guard is a separate, stricter instance, not a reuse of
`browser_bridge_fetch`'s.** There is no attached tab to treat as an
automatic same-origin allow, so every target is evaluated purely on its own
merits: `https` is required outright except for an explicitly granted
private/internal origin (see §5's classifier, reused verbatim), and an
ungranted private/internal target is refused as SSRF before the scheme check
even runs. This is a strictly narrower default than the interactive lane's
same-origin carve-out, which is appropriate for a lane that runs completely
unattended. §5's own **DNS-rebinding limitation applies identically here**:
the classifier is string/literal only, never a live resolution, so a
public-looking hostname that resolves to a private address is not caught.

**Rate guard and per-origin serialization are new gates this lane alone
has** (§4.4 of the build plan): a token bucket per (device, origin), default
2 req/s burst 10, refusing with `SILENT_RATE_LIMITED` (4260) and an honest
`retry_after_s` rather than an unbounded queue; and a per-(device, origin)
lock held for the duration of a relay call, so two concurrent calls to the
same origin never race the same hidden worker tab — different origins never
contend with each other.

**Chunked reassembly is trusted only after independent verification.**
Large bodies travel the wire as `silent.fetch.chunk` frames
(`relay.py` reassembles them per request id, in order, with a byte ceiling);
the gateway then recomputes its own sha256 over the reassembled bytes and
compares it against what the extension reported, refusing the entire
response (`INTERNAL_ERROR`) on a mismatch rather than silently trusting a
possibly-tampered or truncated reassembly.

**Audit is more thorough here than the interactive lane by design.** Every
completed call (`silent_fetch`) and every refusal at any gate
(`silent_fetch_refused`, with a `reason_class`) is written, and so is every
worker-pool lifecycle point: launch, launch failure, TTL/origin-drift
recycle, LRU eviction, an explicit or global-kill-switch kill, a tab/window
the user closed, and a service-worker-restart re-adopt each reach the
gateway as the `silent.worker` wire notification (`protocol/schema.json`)
and land in `audit.jsonl` as `silent_worker` (device, origin, `action`,
`reason`, and — when available — `served`/`age_ms`), attributed to the
*authenticated connection's own* device id regardless of anything `params`
claims. An unrecognized action is refused and audited as
`silent_worker_rejected` instead. See `silent-fetch.md` §4 for the full
event/action table.

**Redaction is the same end-to-end pipeline §3 already describes** —
per-device policy, applied to the preview, the echoed response headers, and
the on-disk spill file alike, including a re-redaction pass on every cache
hit against the device's *current* policy rather than whatever was in force
when the entry was first written.

## 25. Origins are compared canonically everywhere a grant is looked up

Every grant table in `state.py` (`grants`, `session_grants`,
`capability_grants`, `origin_silent_mode`) is keyed on an origin string. Until
this fix, every read of those tables — `get_mode`, `has_explicit_grant`,
`get_session_grant`, `get_capability_grant`, `get_origin_silent_mode`, and two
call sites that re-scanned `list_grants` themselves instead of going through
`state.py` (`session_powers._explicit_grant_mode`, the fetch cross-origin/SSRF
gate; `navigation._has_grant_row`, the `open_tab` bootstrap check) — compared
origin strings exactly as written. Chrome treats `https://example.com`,
`https://EXAMPLE.com`, `https://example.com:443` (the default port, spelled
out), and `https://example.com.` (a trailing DNS-root dot) as the same site;
this plugin did not. An origin set to `off` was therefore only actually
refused for the one exact spelling it was set under — any agent-supplied URL
in an equivalent spelling missed the row and fell through to the device
default, which is `full` by default (the user's 2026-09-23 "access mode by
origin" ask, §1). A malicious or merely careless agent turn did not even need
to try: `https://EXAMPLE.com/x` for an origin set `off` at `https://
example.com` was allowed outright.

The canonicalization itself (`hermes_plugin/origins.py`'s
`canonicalize_origin` — lowercases scheme and host, strips the scheme's
default port, strips a trailing host dot, IDNA-encodes a unicode host to its
punycode form, and passes the opaque `"null"` sentinel and anything
unparseable through unchanged) already existed, mirrored by
`extension/src/lib/origin-policy.ts` for the G2.2.13 frame-origin fix and
shared with it via `fixtures/origin-normalization-cases.json` — but it lived
in `tools.py`, applied only at a handful of call sites (`_origin_policy_for_
device`, `silent_grants`/`silent_fetch`'s own origin resolution, `relay.py`'s
`origin_silent_mode` report handling), and `state.py` could not import it
without creating an import cycle (`tools.py` already imports `state`). It is
now `hermes_plugin/origins.py`, a dependency-free module `state.py` imports
directly; `tools._canonicalize_origin` is kept as a re-export so no existing
caller or test needed to change.

**Canonicalize on write.** `set_grant`, `set_session_grant`,
`set_capability_grant`, and `set_origin_silent_mode` all canonicalize the
origin before it reaches SQL. Because each of those tables' primary key
includes the origin, this also means a fresh write under any spelling variant
now upserts the SAME row a canonical-spelling write would have — no more
accumulating one row per spelling ever seen.

Each of those four setters also deletes, in the same transaction as the
upsert, this device's OTHER rows in that table which canonicalize to the
same origin (narrowed by `session_key`/`capability` where those are part of
the table's key) via a shared `_delete_other_canonical_rows` helper — DML
against existing rows, never a migration. Without this, a legacy
non-canonical row (say `https://Example.com` = `off`, on disk from before
this fix shipped) would never go away: the user re-granting `full` for that
origin in the popup would upsert only the fresh canonical row alongside it,
most-restrictive-wins on read would keep resolving to the legacy row's
`off` forever, and the origin would be stuck refused with no UI path to ever
lift it. Fails closed, but leaves the user stranded — this cleanup is what
actually retires a stale row once a write means to supersede it, rather than
just being permanently out-ranked by it. A row no write has ever touched is
still resolved by most-restrictive-wins on read, exactly as below.

**Canonicalize on read, both sides.** `get_mode`, `has_explicit_grant`,
`get_session_grant`, `get_capability_grant`, and `get_origin_silent_mode` all
canonicalize the origin they were asked about, then canonicalize every stored
row's origin before comparing — so a row written before this fix shipped, or
inserted directly against the database, is still found and honoured under
its canonical spelling, without any `ALTER TABLE` or migration (this
module's `CREATE TABLE IF NOT EXISTS`-only migration policy is unchanged).

**Most restrictive wins on a genuine conflict.** If two rows in the same
table canonicalize to the same real origin but disagree — a pre-existing
non-canonical `full` row alongside a fresh canonical `off` row, say — the
grants table resolves to whichever of {off, request, full} is most
restrictive, and `origin_silent_mode` resolves the same way over its own
{off, ask, always} enum. A spelling variant can raise a stored refusal's
*visibility* (make sure it is actually found) but can never *lower* it by
sitting next to a more permissive spelling of the same origin. Session and
capability grants are existence grants, not a graded mode, so a conflict
there is resolved by taking whichever still-unexpired matching row was
granted most recently, not a restrictiveness ordering.

See `tests/test_canonical_origins.py` for the regression coverage: spelling
variants (case, `:443`, trailing dot, userinfo, unicode IDN vs. punycode) of
an `off` origin refused across `browser_bridge_fetch` (both the plain
`_authorize` gate and `handle_fetch`'s cross-origin SSRF/explicit-grant
path), `browser_bridge_open_tab`, and the `evaluate`/`cookies_write`
capability paths; a pre-existing non-canonical row still refusing; the
most-restrictive-wins rule under a genuine conflict; a session/capability
grant set under one spelling honoured under another; and a legacy
non-canonical row actually being retired (not just out-ranked) the moment a
fresh write means to supersede it, for both `grants` and
`origin_silent_mode`.
