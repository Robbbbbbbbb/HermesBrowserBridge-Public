# Silent evaluate — JavaScript in the hidden worker tab

`browser_bridge_silent_evaluate` runs one JavaScript expression in the page
context of the same hidden, minimized worker tab that
[`browser_bridge_silent_fetch`](silent-fetch.md) uses, with the user's real
session and cookies. There is no attached tab, no lease, no visible tab and
no `tab_id` on the tool surface (passing one is refused).

Its purpose is batching. `silent_fetch` is one URL per tool call; a sweep of
900 URLs is 900 calls and 900 results through the agent's context. The
interactive `browser_bridge_evaluate` can batch, but it needs an attached,
visible tab. Silent evaluate puts the whole loop inside one expression, in
the background, and hands back either a small summary or a file.

Design source: `ProjectRules/ep2-silent-evaluate.md`. Where this document and
that brief differ, this document follows the code.

## Contents

1. [What it needs](#1-what-it-needs)
2. [The gates, in order](#2-the-gates-in-order)
3. [Approval: the floor and the policy](#3-approval-the-floor-and-the-policy)
4. [Rate limiting: bridge calls only](#4-rate-limiting-bridge-calls-only)
5. [Timeouts, wedges and worker recycling](#5-timeouts-wedges-and-worker-recycling)
6. [Results: inline, truncated, or spilled to a file](#6-results-inline-truncated-or-spilled-to-a-file)
7. [The debugger banner](#7-the-debugger-banner)
8. [Config keys](#8-config-keys)
9. [Audit fields](#9-audit-fields)
10. [Worked example: a 900-station pricing sweep](#10-worked-example-a-900-station-pricing-sweep)

## 1. What it needs

Three things, two on the device side and one gateway-side switch that is on by default:

- The origin's **Background requests** setting (popup) must permit
  background use — the same per-origin setting that gates silent fetch. An
  origin granted for silent fetch is granted for silent evaluate; there is no
  separate per-origin toggle.
  **This is zero-touch when the origin resolves that way.** An origin whose
  Background requests is **Always**, or an unconfigured origin under the
  defaults (`default_mode` full and `silent_fetch.full_implies_silent` true),
  is zero-touch. With Run JavaScript on and the default `evaluateApproval` of
  "Always allow", the agent's JavaScript runs there with **no prompt at all**.
  That holds even when the origin's ordinary access mode is `request`, if its
  Background requests is Always: a site the user granted silent fetch also
  permits eval, by design (spec §3). Only an origin set to Ask (or resolving to
  ask) prompts every call, and a device policy of `ask_per_session` or
  `always_ask` adds prompts on zero-touch origins (§3).
- The device's **Run JavaScript** setting (`allowEvaluate`, Options →
  Powers) must be on. It is on by default on a fresh install (an install that
  already stored it off keeps it off, and a device that never reported its
  power policy counts as off). This is the same switch as interactive evaluate.
- Gateway config must not turn it off (`silent_evaluate.enabled`,
  `silent_fetch.enabled`, and the operator kill switches in §2).

Silent evaluate is a strictly bigger power than silent fetch: an expression
can read `document`, `localStorage`, and call `fetch()` with the page's own
session. That is why it also needs `allowEvaluate` and has its own approval
policy row (§3), even though the per-origin trust boundary is shared.

## 2. The gates, in order

Each refusal is audited as `silent_eval_refused` with a `reason_class`.

1. **Parameters.** `url` must be http/https; `expression` is required and at
   most 4096 characters (`EVAL_EXPRESSION_TOO_LARGE` past that); `tab_id`/
   `tab` are refused; `world`, if given, must be `main`; `spill_to_path`
   must be a plain file name (§6).
2. **Enabled.** `silent_evaluate.enabled` and `silent_fetch.enabled` must
   both be true (the lane itself is silent fetch's).
3. **Operator kill switch.** `browser_bridge.powers.silent_evaluate: false`
   or `powers.evaluate: false` in `config.yaml` refuses every call.
4. **Device power.** `allowEvaluate` must be on for the device.
5. **SSRF guard.** The same guard silent fetch uses (private/internal
   targets need an explicit grant; https required for public hosts).
6. **Origin grant.** The same resolution silent fetch uses. The origin's
   access mode `off`, or its Background requests setting `off`, refuses with
   `SILENT_ORIGIN_NOT_GRANTED` (4257). Otherwise the origin resolves as
   **zero-touch** (Background requests `always`, or unset with mode `full`
   and `silent_fetch.full_implies_silent` true) or **ask** (`ask`, or unset
   in any other case).
7. **Approval.** See §3.
8. **Rate guard.** See §4.
9. **Per-origin slot.** One silent call at a time per (device, origin). A
   silent evaluate that arrives while any silent call (fetch or evaluate)
   holds the origin's slot is refused immediately with `SILENT_WORKER_BUSY`
   (4265, audited as `worker_busy`) and never reaches the extension. A silent
   fetch arriving while an evaluation holds it is refused the same way. Two
   fetches to one origin still queue, waiting up to 30 s.
10. **Relay call** to the extension, then a gateway redaction re-check of the
    result text, then shaping or spilling.

## 3. Approval: the floor and the policy

Silent evaluate has its own capability id, `silent_evaluate`. It reads the
device's `evaluateApproval` policy (EP1) but keeps its own state: an
interactive `evaluate` grant never covers it.

| Origin resolution (gate 6) | `always_allow` | `ask_per_session` | `always_ask` (also missing/corrupt) |
|---|---|---|---|
| Zero-touch | No prompt. | Prompts once, then silent for that browser connection. | Prompts every call. |
| Ask | **Prompts every call.** | **Prompts every call.** | Prompts every call. |

**The floor.** The policy applies only to a zero-touch origin. An origin the
user set to Ask first, or that resolves to ask by default, prompts on every
call regardless of the device's policy. A policy is a convenience for an
origin the user has already made silent, never a way to quiet one they have
not.

`ask_per_session` uses EP1's in-memory grant, keyed by the device, the
current relay connection, the origin and the capability. It is cleared when
the connection drops or the gateway restarts, so a reconnect prompts again.

The host approval request for this capability never offers "session" or
"always": it is in `APPROVAL_POLICY_KEYS`, so a standing grant can neither be
created nor honoured. The prompt shows the expression (secret-shaped spans
masked).

Under `always_allow` there is no human gate, so the audit trail (§9) is the
review mechanism.

## 4. Rate limiting: bridge calls only

Silent evaluate shares silent fetch's per-(device, origin) token bucket
(default 2 requests/s, burst 10). Mixed fetch and evaluate calls draw from one
bucket: once it is empty, the next call of either kind gets
`SILENT_RATE_LIMITED` (4260) with `retry_after_s`.

The guard counts **bridge calls only**. An expression that runs 900
`fetch()` calls inside the page is one call as far as the guard is
concerned; what the page does to the site is the page's business. That is
exactly why batching belongs inside one expression instead of one tool call
per item. Pace the loop yourself (a small `await` delay between requests) if
the target site rate-limits.

## 5. Timeouts, wedges and worker recycling

- `timeout_ms` defaults to 30000 and is clamped to
  `[1000, silent_evaluate.max_timeout_ms]` (default 600000, ten minutes). The
  gateway's relay wait is `timeout_ms` plus the worker bootstrap budget plus a
  15 s margin, so the extension's own deadline normally fires first (the relay
  wait is a backstop, not a guarantee).
- `600000` ms is a hard ceiling: it is the extension's own clamp, and the
  gateway config can only lower it, never raise it past what the extension
  honours.
- The deadline is enforced by the extension's own background-side timer (the
  same race helper interactive evaluate uses), not by the page or by CDP. On
  expiry the call fails with `SILENT_EVAL_TIMEOUT` (4264). A runaway
  synchronous loop and a wedged dialog both surface this way and both recycle
  the worker.
- The extension runs the evaluation start-then-poll: it starts it, then polls
  with each poll individually bounded, rather than making one long CDP call.
  A run past 5 minutes therefore does not depend on a single long
  service-worker API call staying alive.
- **Stop / pause** in the popup aborts an in-flight silent evaluation.
- After a timeout the extension **recycles the worker**: it closes the tab
  and detaches the debugger, and the next call for that origin launches a
  fresh worker (a `silent.worker` audit line with `action: eval_wedge`). This
  is what keeps a wedged dialog (`alert`/`confirm`/`prompt` blocks the
  evaluation and Chrome does not suppress it) from poisoning the pool. Never
  call those in an expression.
- Workers are single-flight. A call that arrives while the origin's worker is
  running a fetch or an evaluation is rejected **immediately** with
  `SILENT_WORKER_BUSY` (4265), not queued and not waited on: the gateway
  refuses before any relay call (audit `silent_eval_refused`, reason class
  `worker_busy`), and a fetch that hits an evaluation is refused the same way.
  The extension enforces the same rule as a backstop. Retry after the running
  call finishes.
- An expression that throws is not a tool error: you get a normal result with
  `status: "error"` and an `exception` string.

## 6. Results: inline, truncated, or spilled to a file

The expression's value is serialized by value: a string is returned as-is,
anything else is `JSON.stringify`'d, `undefined` becomes an empty string. The
text is redacted (card/SSN/password/token shapes, per the device's redaction
policy) in the extension and re-checked at the gateway before anything is
truncated or written.

**Inline (default).** Returns `{status, result, result_type, truncated,
total_bytes, redactions, timing, diagnostics}`. `result` is at most
`max_return_bytes` (default 32768, hard cap 1 MiB) of UTF-8, cut on a
character boundary, with `truncated: true` when cut. Return counts, samples
and status objects, not bulk data.

**`spill_to_path`.** Pass a file **name**, for example
`sweep-2026-09-28.json`. The whole redacted result is written under the
gateway's spill root and the tool returns only `{path, bytes, sha256,
truncated, preview, result_type, timing}` (`preview` is the first 2000
characters). Read the file with `execute_code`.

- The name must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` and contain no
  `..`. Slashes, absolute paths and leading dots are refused. It is a file
  name, not a path, on purpose: an agent-chosen path would be a
  write-anywhere primitive on the gateway host, fed by page-controlled data.
- The spill root is `silent_evaluate.spill_dir`, default
  `~/.hermes/browser_bridge/eval_spill`. It is created with mode 0700; files
  are 0600 and written atomically. Overwriting a regular file is allowed.
  A target that is a symlink (or a directory) is refused, before the
  expression runs and again just before the write.
- Results larger than one frame travel as `silent.fetch.chunk` frames and are
  reassembled by the relay; the sha256 the extension reports is verified
  against the reassembled bytes. `sha256` in the reply is the hash of the file
  actually written (after gateway redaction).
- `truncated: true` means the result exceeded `max_spill_bytes` and the file
  holds only the head.

## 7. The debugger banner

The extension keeps one persistent `chrome.debugger` session per worker,
attached on the first evaluation and detached when the worker is recycled,
evicted or killed. While attached, Chrome may show its "is debugging this
browser" banner on the worker's window. That window is the minimized
background window, so the banner is not in the user's way, but it can
appear if they open it. It is separate from the interactive lane: no glow,
no lease, no "shared" state.

If the user clicks Cancel on the banner, Chrome detaches; an evaluation in
flight fails with the ordinary CDP error and the next call re-attaches.

## 8. Config keys

Under `browser_bridge.silent_evaluate` in `config.yaml` (merged one level
deep, like `silent_fetch`; every value is clamped):

| Key | Default | What it controls |
|---|---|---|
| `enabled` | `true` | Gateway-wide switch for the tool. |
| `max_timeout_ms` | `600000` | Ceiling on a call's `timeout_ms`. Clamped to `[1000, 600000]`; 600000 is the extension's own clamp, so config can lower the ceiling but never raise it. |
| `max_spill_bytes` | `16 MiB` | Ceiling on a spilled result; never above `silent_fetch.max_bytes_cap`. Minimum 1024. |
| `spill_dir` | `~/.hermes/browser_bridge/eval_spill` | Where `spill_to_path` files go. Must be an absolute path, otherwise the default is used. |

Related switches: `browser_bridge.powers.silent_evaluate: false` (or
`powers.evaluate: false`) refuses every call; `silent_fetch.rate_per_s` and
`burst` also govern this tool.

## 9. Audit events

`~/.hermes/browser_bridge/audit.jsonl`:

- **`silent_eval`** — one line per call that reached the extension:
  `device`, `holder`, `origin`, `url` (origin and path, no query),
  `expression` (secret-shaped spans masked, truncated to 800 characters plus a
  length note), `timeout_ms`, `outcome` (`ok`, `error`), `result_bytes`,
  `truncated`, `spilled`, `spill_path` and `sha256` when spilled,
  `redactions`, `timing` (`total_ms`, `relay_ms`, `extension_ms`),
  `elapsed_ms`, and `error_code` for a failure.
- **`silent_eval_refused`** — a call stopped before the extension, with its
  `reason_class` (`invalid_params`, `invalid_spill_name`,
  `spill_target_refused`, `disabled`, `operator_disabled`, `device_power_off`,
  the SSRF classes, `origin_not_granted`, `approval_denied`, `timeout`,
  `approval_error`, `rate_limited`, `origin_slot_timeout`).
- **`grant_check`** — the approval decision, including
  `scope: policy` when the policy skipped the prompt.
- **`silent_worker`** with `action: eval_wedge` — a worker recycled after an
  evaluation timeout.
- **`redaction_gateway_catch`** — the gateway found something the extension
  missed.

The executed expression is never altered by redaction; only the audited and
displayed copies are masked.

## 10. Worked example: a 900-station pricing sweep

The incident that motivated this tool: a sweep of ~900 station pages that
runs about five minutes, unattended. One call, one file:

```
browser_bridge_silent_evaluate(
  url="https://www.example.com/",
  timeout_ms=420000,
  spill_to_path="sweep-2026-09-28.json",
  expression="""
    (async () => {
      const ids = await (await fetch('/api/stations')).json();
      const out = [];
      for (const id of ids) {
        const r = await fetch('/api/pricing?station=' + id);
        out.push({ id, status: r.status, body: r.ok ? await r.json() : null });
        await new Promise(res => setTimeout(res, 150));
      }
      return out;
    })()
  """)
```

The tool returns `{path, bytes, sha256, preview, ...}` after about five
minutes; the agent reads the file in `execute_code`. That is one bridge call
against the rate guard, not 900, and zero visible tabs. The loop paces itself
because the guard does not see the page's `fetch()` calls.

Things that matter for a run this long:

- Set `timeout_ms` above the expected runtime (up to `max_timeout_ms`). On
  timeout the worker is recycled and the partial work is lost; the call does
  not resume.
- Keep the expression under 4096 characters; put helper data inside it or
  fetch it from the page.
- For a result too large for one file cap, write per-batch summaries or raise
  `silent_evaluate.max_spill_bytes` (and `silent_fetch.max_bytes_cap`).
- Only one call runs per origin worker at a time. A concurrent
  `silent_fetch` to the same origin during the sweep is refused at once with
  `SILENT_WORKER_BUSY`.

## 11. Stuck worker: auto-reclaim, the CLI, and the tool

Symptom: every call to one origin fails with `SILENT_WORKER_BUSY` (4265) long
after the call that started it timed out. Cause: the gateway gave up waiting
(`TIMEOUT`, 4300) on a `silent.evaluate` that hung extension-side, but the
extension's worker stayed marked busy.

- **Automatic.** When the gateway's own wait for `silent.evaluate` expires it sends
  `silent.kill {origin}` to that device (best-effort, at most ~2 s, never
  raises into the tool result) and adds `worker_reclaimed` plus a hint to the
  error: retry and you get a fresh worker. A 4265 the gateway did not cause
  (nothing of its own holds that origin's slot) is treated the same way: one
  `silent.kill`, the 4265 returned with a "retry once" hint. 4264 and 4259
  never trigger it, since the extension already recycled or killed the worker.
  Every attempt is audited as `silent_worker_reclaim` (device, origin,
  `trigger` = `relay_timeout` / `orphaned_busy` / `operator` / `agent`,
  method, `sent`).
- **Agent tool.** `browser_bridge_silent_kill {origin, device_id?}` closes one
  origin's worker on demand. Always allowed, since it only closes the hidden
  worker tab.
- **Operator CLI.** `hermes browser-bridge silent kill [ORIGIN] [--all]
  [--device DEVICE_ID]`. It needs the relay in the same process, which is not
  the case for a normal shell (the relay lives in the gateway): there it
  refuses and points at the agent tool. Popup "Stop" / kill buttons also work.
