# Per-origin modes — cheat sheet

Written for the moment you're staring at the extension popup's "Access mode
by origin" field deciding what to type for a site. For *why* each row is
enforced the way it is (and the gaps), see [`security.md`](security.md); this
file is just "what happens." Verified against `hermes_plugin/tools.py`,
`hermes_plugin/session_powers.py`, and `hermes_plugin/vision.py` as shipped
(2026-09-22) — several rows below do **not** match `plan.md`'s original
design table, and this file follows the code.

## The one thing to know before the table

**`request` mode does not gate reading.** Attaching a tab, taking a
snapshot, reading text, and taking a screenshot all require a standing
**`full`** grant — `request` mode refuses them exactly like `off` does, with
no approval prompt, ever. `request` mode only ever means something for
capabilities that *change* the page or *reveal something beyond structure*:
clicking/typing (`act`), pointing at elements (`ask`, only when it highlights
candidates), replaying a request (`fetch`), or pulling cookies/network data.
If you want Hermes to be able to look at a page at all, that origin needs
`full` — there's no middle ground for looking, only for acting.

**The shipped default is `full`** (`browser_bridge.default_mode`, changed
2026-09-22). An origin you have never configured is therefore readable and
drivable as soon as you attach a tab on it — you are opting in by attaching,
not per site. Two things this does *not* change: nothing at all happens on a
tab you have not attached, and cross-origin `fetch` still refuses any target
with no explicit grant row, because that path ignores `default_mode` on
purpose (see the `fetch` rows below). Set `default_mode: off` in the gateway's
`config.yaml` if you would rather opt in site by site; an origin you
explicitly set to `off` is always refused regardless of the default.

## Capability × mode

| Capability | `off` | `request` | `full` |
|---|---|---|---|
| **Attach a tab** (`browser_bridge_attach`) | Refused. | Refused — same message as `off`, pointing at switching to `full`. | Allowed. |
| **Snapshot** (`browser_bridge_snapshot`) | Refused. | Refused. | Allowed. Values matching the redaction kinds you have enabled are redacted regardless of mode — see [Redaction is orthogonal to modes](#redaction-is-orthogonal-to-modes) below. |
| **Read text** (`browser_bridge_read`) | Refused. | Refused. | Allowed. Same redaction as snapshot. |
| **Screenshot** (`browser_bridge_screenshot`) | Refused (can't attach, so nothing to screenshot). | Refused (same reason). | Allowed. Re-checked on every call, like snapshot and read — see security.md §2: if you downgrade this origin after attaching, all three start refusing on their very next call. |
| **Click / type / select / submit / scroll / key / navigate / wait\_for** (`browser_bridge_act`) | Refused. | **Parks for approval** — popup card, blocks until you answer `once`/`session`/`always`/`deny` or it times out (default 120s). `always` promotes the origin to a standing `full` grant. | Allowed immediately, no prompt. |
| **Ask a bare question, no candidates** (`browser_bridge_ask`) | N/A — needs an attached tab, so unreachable at `off` anyway. | Runs immediately, no approval — a bare question touches no page content. | Runs immediately. |
| **Ask with highlighted candidates** (`browser_bridge_ask` + `candidates`) | Unreachable (no attach). | **Parks for approval** — highlighting specific elements discloses page structure, the same disclosure a `read` would make, so it's gated like `act`. | Allowed immediately. |
| **Fetch, same origin as the attached tab** (`browser_bridge_fetch`) | Unreachable (no attach). | Parks for approval on the tab's own origin. | Allowed immediately — the flagship case (a console tab fetching its own API). |
| **Fetch, cross-origin, target origin explicitly granted `request`** | — | Parks for approval **against the target origin's own grant**, not the attached tab's. | — |
| **Fetch, cross-origin, target origin explicitly granted `full`** | — | — | Allowed immediately. |
| **Fetch, cross-origin, target origin has no grant row at all** | Refused outright, worded as an SSRF refusal if the target looks like a private/internal address, or as a generic "cross-origin, ungranted" refusal if it doesn't. **The config-wide `default_mode` fallback does not apply here** — an ungranted cross-origin target is always refused, even if `default_mode` happens to be `request` or `full` for everything else. | | |
| **Cookies, metadata only** (`browser_bridge_cookies`) | Refused. | Parks for approval. | Allowed immediately. |
| **Cookies, `include_values:true`** | Refused. | **Parks for approval, and an approval (once/session/always) does reveal the actual values.** This is *not* gated like network bodies below — a one-off approval is enough. | Allowed immediately. Values never appear in the audit log regardless of mode. |
| **Network log, metadata only** (`browser_bridge_network`) | Refused. | Parks for approval. | Allowed immediately. |
| **Network log, `include_bodies:true`** | Refused. | **Approval only unlocks the call itself — bodies are still withheld.** Bodies require a standing `full` grant for that origin specifically, checked independently of whatever the approval decided. The result explains why in `bodies_omitted_reason`. | Allowed immediately, bodies included (redacted the same way snapshot/read are). |
| **Console output** (`browser_bridge_console`) | Refused. | **Parks for approval even here** — `console` is one of G0.5's per-capability-ceiling capabilities, so **`full` does not skip the prompt either** (see the next row). Also requires the device's own `allowConsoleRead` setting to be on — off refuses before this table's mode even matters. | **Still parks for approval** — `console`'s ceiling means a `full` origin grant does not bypass it, unlike every other row above. Requires `allowConsoleRead` too. |

## Redaction is orthogonal to modes

Everything else on this page is **per origin**. Redaction is not — and the
easiest mistake to make with this document is to assume otherwise.

**Redaction is one global policy per device.** Five checkboxes in the
extension's Options page, applying identically to every origin you have ever
granted:

| Kind | Default | What it catches |
|---|---|---|
| `password` | **on** | Any field that reads as a password by *identity* — `type="password"`, a `current-password`/`new-password` autocomplete, or a password-ish `name`/`id` — whatever the value looks like. |
| `card` | **on** | 13–19 digit runs that pass a Luhn checksum, so ordinary long order and tracking numbers are left alone. |
| `ssn` | **on** | The `123-45-6789` shape. Shape-only — there is no checksum for SSNs — so it can false-positive on similarly-shaped IDs. |
| `email` | **off** | `local@domain.tld` with a dotted, letters-only TLD. |
| `phone` | **off** | North-American-ish shapes, separators required between groups, so a bare 10-digit run is not caught. |

Email and phone are off by default because both match constantly in ordinary
page text: turning them on trades page fidelity for coverage, and that trade
is yours to make rather than a default anyone should pick for you.

Three consequences worth being explicit about:

- **A mode and a redaction kind decide different things.** The grant decides
  whether a call happens at all; redaction decides what an allowed call is
  permitted to carry back. Granting an origin `full` does not switch any
  redaction kind off, and switching a kind off does not grant anything.
- **There is no per-origin redaction.** You cannot redact emails on one site
  and not another. `plan.md` §6.3 originally described a per-origin opt-out;
  a single global policy is what actually shipped, as a deliberate scope
  reduction — see `security.md` §3's "Still not per-origin" for the reasoning.
  If you need a site's contents protected more than the global policy
  provides, the tool for that is the origin's **mode**, not redaction.
- **Turning a kind off leaves a record.** Every change is written to the
  audit log as `redaction_policy_changed` with the full before/after policy.
  The change itself is never gated or refused — it is your own privacy
  preference for your own machine, not something the gateway approves — but
  it is durably recorded, because "a data protection was switched off" is
  worth being able to reconstruct later.

Both layers honour the same policy. Switching a kind off switches it off in
the extension *and* at the gateway's belt-and-braces re-check, so a checkbox
is not a suggestion that something downstream quietly overrides. The full
mechanism, including what happens to a gateway talking to an older extension
that reports no policy at all, is in `security.md` §3.

## The two "approval isn't enough" exceptions, side by side

It's easy to assume "approved once" and "granted `full`" mean the same thing
functionally. They don't, for exactly two things:

- **`browser_bridge_network(include_bodies=true)`** — approval unlocks the
  *call*; bodies still need standing `full`.
- **Everything else that goes through the approval queue** (`act`, `ask`
  with candidates, `fetch`, `cookies` — including cookie *values*) — a single
  `once`/`session` approval is fully sufficient; there is no additional
  standing-grant requirement layered on top.

If you were assuming cookie values needed a standing `full` grant the way
network bodies do, they currently don't — see `security.md` §1a for the exact
code path this comes from.

## Approval scopes, when a call does park

- **`once`** — allows this specific call, asks again next time.
- **`session`** — allows this device+capability+origin combination for the
  rest of the current Hermes session (bounded by
  `browser_bridge.approval_session_grant_ttl_hours`, default 12h — there's no
  reliable "session ended" hook to expire it exactly on session end, so it's
  time-boxed instead).
- **`always`** — same as `session`, plus **promotes the origin to a standing
  `full` grant** in the popup's own grants table. This is irreversible from
  the approval dialog itself — the popup warns "This is a standing grant, not
  a one-time approval" with a confirm dialog before committing it.
- **`deny`** — refuses this call. Does not affect future calls either way.
- **Timeout** (nobody answered within the TTL, default 120s) — treated
  identically to `deny`. Never defaults to allow.

## EP1: a second, orthogonal dimension for evaluate / upload / http_auth

Everything in the table above is about the **origin's mode**. For exactly
three capabilities — `evaluate` (`browser_bridge_evaluate`), `upload`
(`browser_bridge_upload`), and `http_auth`
(`browser_bridge_http_auth_status`) — there is a SECOND, per-device setting
that layers on top of `full` mode: the **approval policy**
(`evaluateApproval` / `uploadApproval` / `httpAuthApproval`, set in the
extension's Options page next to each capability's own on/off toggle). These
three can never hold a standing `always`/`session` grant the way `act`/
`fetch`/`cookies`/etc. can (the table above's approval-scope rules don't
apply to them at all) — the approval policy is the only lever for whether an
individual call still prompts.

| Policy | What happens on a `full`-mode origin | On a `request`-mode origin |
|---|---|---|
| `always_allow` | No prompt, ever. Still fully audited — the audit line is what you'd review instead of a prompt. | Ignored — **the floor**: `request` mode always prompts every call regardless of this setting. |
| `ask_per_session` | The first call on a given device+relay-connection+origin+capability prompts; any allow answer silences the rest of that connection's calls. Reconnecting the extension (or restarting the gateway) resets it — you'll be asked again. | Ignored — same floor as above. |
| `always_ask` | Every call prompts — the original, pre-EP1 behaviour. | Every call prompts (no change from the floor). |

Because a never-configured origin's effective mode is `full` (`default_mode`
ships as `full` — see above), the extension-reported policy is what actually
governs an unconfigured origin out of the box, not `request`'s per-call
floor.

The extension's own default differs per capability, set in Options next to
each toggle: `evaluateApproval` defaults to `always_allow`, while
`uploadApproval` and `httpAuthApproval` default to `ask_per_session`. Run JavaScript
(`allowEvaluate`) is likewise on by default on a fresh install, so out of the box
the agent's JavaScript runs on Full-access and zero-touch origins with no
prompt (every call audited); an install that stored it off keeps it off. That's
a fresh-install/fresh-storage default, and it's distinct from the gateway's
fallback for a device that never reports the field at all (an extension
older than 0.2.5) or reports something missing/corrupt: that always reads as
`always_ask` — the same fail-closed direction every other unknown-power-policy
field in this project takes. See `security.md`'s "EP1: configurable approval
policy" section for the full mechanism (audit fields, the connection-scoped
grant's exact lifetime, what does and doesn't change about the host grant
refusal).

## Background requests and agent JavaScript (EP2)

Background requests now also permits agent JS in the hidden worker, when Run
JavaScript is on. An origin set to Always allow runs `browser_bridge_silent_evaluate`
under the device's `evaluateApproval` policy; an origin set to Ask first prompts
on every evaluation regardless of that policy. Details:
[`silent-evaluate.md`](silent-evaluate.md).

## Picking a mode for a site, in practice

- **A site you want Hermes to actively work in** (an admin console you're
  delegating a task to, like the security-console/helpdesk/backup use cases this
  project was built for) — **`full`**. `request` mode's per-op approval isn't
  even available for the read side (snapshot/read/attach), so half-measures
  don't work here the way you might expect from other "ask me each time"
  tools.
- **A site where you want visibility into *actions* only, and you're willing
  to approve each one** — `request` still can't show Hermes the page at all
  (see above), so this mode is really only useful once you've *also* granted
  `full` (or the session already has an attached tab another way) and you
  specifically want individual `act`/`fetch`/`cookies`/`network` calls gated.
  In today's shipped design, `request` is best understood as "approve
  individual actions on a tab that's otherwise already fully visible," not
  "cautiously peek at a site."
- **Anything sensitive you never want touched** (banking, personal email,
  anything you wouldn't want in an audit log even redacted) — **`off`**, and
  leave it there. Redaction is defense in depth, not a reason to grant access
  you don't actually want to give.
- **A site with a login form you're worried about** — redaction runs
  regardless of mode once a capability call is actually allowed, and a `full`
  grant does not change that (see [Redaction is orthogonal to
  modes](#redaction-is-orthogonal-to-modes)). What *does* change it is the
  Options checkbox for that kind — a separate, deliberate, audited choice
  that applies to every origin at once, not just this one. With password
  redaction left on (the default), the risk in granting `full` to a sensitive
  site is exposure of the surrounding page content and structure, not
  credentials typed into a password field.
