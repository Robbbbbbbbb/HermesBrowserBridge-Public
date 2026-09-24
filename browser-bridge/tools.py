"""Tool schemas (what the model sees) and handlers.

Handler contract (verified against tools/registry.py in v0.19.1):
``handler(args: dict, **kwargs) -> str`` — the string is what lands in the
transcript, so every handler returns compact JSON.

Service gating: ``check_fn`` returns False when no device has checked in
recently, so unpaired sessions never carry dead tool schemas (CLAUDE.md rule 3
— the prompt cache is sacred). The registry caches check_fn results, so the
check itself must stay cheap: it is a single indexed SQLite count.

M1 additions (workstream C) wire attach/release/snapshot/read on top of the
M0 status tool, plus the gateway-side grant enforcement that plan.md §6.2
calls non-negotiable: every capability call checks
``state.get_mode(device_id, origin)`` *before* anything is dispatched to the
extension — ``_gate_reason`` handles this for the non-mutating M1 tools
(``off`` refuses outright, ``request`` refuses with an M2-pointer since no
approval queue existed yet, ``full`` proceeds).

M2 additions (this file's owner: workstream G) wire ``browser_bridge_act``
(click/type/select/submit/scroll/key/navigate/wait_for, idx resolved to a
CSS selector via ``attach.py``'s index map before the extension ever sees
it) and ``browser_bridge_ask`` (annotate-and-ask, gated the same way when it
would reveal page content). Both go through ``_authorize`` instead of
``_gate_reason``: ``full``/``off`` behave identically, but ``request`` now
tries the bridge-owned approval queue (``hermes_plugin/approvals.py``,
workstream H) via the same defensive ``try/except ImportError`` seam M1's
vision integration uses, falling back to M1's outright refusal if that
module isn't loaded on this gateway. The plan §6.5 CDP method whitelist
(``is_cdp_method_allowed`` / ``_cdp_gate``) lives here too, gateway-side,
ready for whenever a raw ``cdp.send`` passthrough is exposed. Every gated
decision — M1 or M2, allow or deny — is audited.

M3 additions (this file's owner: workstream I) live in the sibling module
``hermes_plugin/session_powers.py`` (``browser_bridge_fetch``/``_cookies``/
``_network``) rather than in this file directly — the M3 tools need enough
SSRF-guard and redaction machinery of their own that keeping them in a
separate file avoids this one growing without bound. ``register_tools``
below imports and calls ``session_powers.register_session_tools(ctx)`` under
the same defensive ``try/except ImportError`` seam vision.py uses for
``browser_bridge_screenshot``. See session_powers.py's own module docstring
for the SSRF guard (plan §6.6) and the cookie/network sensitivity handling.

Migration note: ``hermes_plugin/approvals.py`` no longer *is* the approval
system end to end — it now presents through Hermes' native
``ctx.register_approval_transport`` contract (v0.21.4; the "no
register_approval_transport" premise above was checked against the wrong
source tree, see approvals.py's module docstring). ``_authorize`` below is
unchanged in shape: 'request' mode still calls ``approvals.require(...)``
under the same defensive ``try/except ImportError`` seam, and still honours
its ``Decision`` the same way — only what happens *inside* ``require()``
changed.

G0.5 note (this file's owner: workstream S2): "``full``/``off`` behave
identically" above is no longer quite true for either gate. Both
``_gate_reason`` and ``_authorize`` now compute ``dangerous = capability in
approvals.DANGEROUS_CAPABILITIES`` before their ``mode == "full"`` check, and
skip the allow-immediately short-circuit when it's set — a per-capability
ceiling that a ``full`` origin grant does not rise above (destructive/
credential-adjacent/broad-read capabilities; see approvals.py's own
docstring for the full list and rationale, and docs/security.md §12). For
``_authorize`` this means a ``full`` origin for a dangerous capability falls
through to the exact same ``approvals.require(...)`` path ``request`` mode
uses. For ``_gate_reason`` — which has no approval-transport integration to
fall back on — it means refuse, not prompt; see that function's own comment
for why. G0.9's operator kill switch (``browser_bridge.powers.<capability>:
false``) is checked in ``_authorize`` even earlier than the ceiling, ANDed
with everything below it.
"""
from __future__ import annotations

import functools
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import attach as attach_mod
from . import audit, config, protocol
from . import refusals
from . import relay as relay_mod
from . import skills_hint
from . import state
from . import timing as timing_mod

TOOLSET = "browser_bridge"

# G1: the raw plugin ctx `register_tools(ctx)` was called with, stashed so
# `_handle_act_steps`'s read-only `tool: "screenshot"` steps can call
# `vision.handle_screenshot(ctx, args, **kwargs)` -- the same signature
# `vision.register_vision_tools`'s own closure calls it with -- without this
# module importing vision.py at load time (the same defensive seam
# `register_tools` already uses for it) or every OTHER handler in this file
# needing a ctx parameter it has never taken. Set once, early in
# `register_tools`, before any tool can be invoked; `None` until then (never
# invoked live before registration completes, so a `steps` screenshot step
# always sees the real ctx once the plugin has actually started).
_PLUGIN_CTX: Optional[Any] = None

# kwarg names tried, in order, to identify "which Hermes session is calling"
# for the attach lease's conflict messages and for scoping a no-arg release
# to only the caller's own tabs. v0.19.1's tool-handler kwargs beyond
# `handler(args, **kwargs)` are not documented in
# Documentation/plugin-api-findings.md (that file only verifies
# register_tool's own signature, not what the registry passes a handler at
# call time) — so this is a best-effort probe across the plausible names
# rather than a confirmed contract. Absent any match, every caller collapses
# to one shared pseudo-session, which degrades to "single active driver, no
# named conflicts" rather than raising. Flagged in this workstream's report
# as something to verify against the real gateway.
# Verified against the RUNTIME tree (v0.21.4, /usr/local/lib/hermes-agent —
# the gateway's ExecStart, not the v0.19.1 install that also exists on the
# host): tools/registry.py:880-896 builds dispatch kwargs as task_id +
# session_id, plus user_task for everything except execute_code, and
# `_kwargs_accepted_by` only narrows them for handlers with an explicit
# signature — every handler here takes **kwargs, so it receives all of them.
# `session_id` is therefore the real carrier
# of session identity; the others are kept as tolerant fallbacks in case a
# future Hermes renames it, and _DEFAULT_SESSION only degrades us to
# "one shared driver" rather than crashing.
_SESSION_KWARG_NAMES = ("session_id", "session_key", "conversation_id", "session")
_DEFAULT_SESSION = "default-session"

# G3.3.2: the shared schema prose for every tab-taking tool's `tab` param —
# one string, not eight subtly different ones, so a model reads the same
# description regardless of which tool it's calling.
TAB_ALIAS_DESC = (
    "Alternative to tab_id: this tab's key or label, from browser_bridge_tabs or a prior "
    "browser_bridge_attach's tab_key — case-insensitive, unique-prefix matched. Ambiguous "
    "(matches more than one of your tabs) is refused with the candidates listed, never guessed."
)

# -- belt-and-braces redaction re-check (plan §6.3) --------------------------
#
# The extension redacts password/CC/SSN-shaped values before a frame ever
# leaves the machine, honouring a per-kind policy the user sets in Options
# (extension/src/popup/options.ts) — password/card/ssn on by default,
# email/phone off. This is the gateway-side re-check plan.md requires in
# addition to that, not instead of it: hitting something here means the
# extension-side pass failed, so a hit is audited loudly
# (`redaction_gateway_catch`), not just quietly patched over.
#
# It honours the SAME policy, read back via `state.get_redaction_policy`
# (persisted from device.hello/device.heartbeat/state.report by relay.py's
# `_apply_reported_redaction`) — a kind the user switched off in the
# extension is not silently re-redacted here, which would make the checkbox
# a lie. A device that has never reported a policy at all (an older
# extension build, or one that hasn't connected since this shipped) gets
# every kind enabled, matching the original unconditional behaviour exactly
# — see state.get_redaction_policy's own docstring. All five kinds
# (password/card/ssn/email/phone) are gated here — email/phone parity with
# the extension's own detectors was added once `_EMAIL`/`_PHONE` below could
# be shown to be provably linear in input length the same way
# extension/src/content/redaction.ts's `EMAIL_PATTERN`/`PHONE_PATTERN` are
# (see that file's comment for the full ReDoS history this mirrors).
#
# REDACTED_PLACEHOLDER is the marker THIS module inserts when it catches
# something the extension missed — it must stay visually distinct from the
# extension's own marker family so a human/audit reader can tell "who caught
# this" at a glance.
#
# The extension's own marker (extension/src/content/redaction.ts:
# `redactionMarker()`) is `[redacted:<kind>]` — e.g. `[redacted:password]`,
# `[redacted:card]`, `[redacted:ssn]` — NOT this module's `[REDACTED]`. The
# "already redacted, skip" guard below must recognise THAT marker family, or
# every line already redacted in-extension looks unredacted to the gateway's
# password heuristic and gets its trailing value clobbered on every single
# snapshot/read, firing a bogus `redaction_gateway_catch` each time (a signal
# that is supposed to mean "the extension's redaction failed", not "business
# as usual"). `_ALREADY_REDACTED_RE` also matches this module's own
# `[REDACTED]` (case-insensitively, via the optional `:<kind>` group) so a
# value this pass already replaced is never re-processed either.
REDACTED_PLACEHOLDER = "[REDACTED]"
_CC_CANDIDATE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# Email/phone: the SAME bounded-quantifier construction as the extension's
# EMAIL_PATTERN/PHONE_PATTERN (extension/src/content/redaction.ts), for the
# same reason — Python's `re` module is a backtracking engine exactly like
# V8's, so an unbounded `[A-Za-z0-9.-]+\.` domain class is exactly as
# vulnerable to catastrophic backtracking here as it was there. Every
# quantifier below has an explicit small ceiling (RFC-realistic maximums,
# not arbitrary ones — see the extension's comment for the RFC citations) so
# no single starting offset can cost more than a constant amount of
# backtracking, which bounds the TOTAL cost to O(n) regardless of input
# shape. Verified empirically, not just argued: linear from 50K to 1M
# characters against the exact adversarial shapes that made the unbounded
# version take seconds (measured on both the domain-ambiguity shape and the
# separate "no matching '@'/no matching terminator anywhere" shape — see
# this workstream's report for the numbers). Mirrors the extension pattern
# closely enough to catch the same shapes, not so closely that a change on
# one side silently has to be mirrored on the other — these are two
# independent implementations of the same detector, same as card/ssn already
# are on both sides.
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.){1,8}[A-Za-z]{2,24}\b")
_PHONE = re.compile(r"\b(?:\+?1[-.\s])?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
_PASSWORD_HINT = re.compile(r"(?i)\bpassword\b")
# G1.6.8: CVV / one-time-code fields, mirroring the extension's
# `classifySensitiveField` (extension/src/lib/redaction.ts) at the only
# granularity the gateway has available — it never sees the field's
# `autocomplete`/`name`/`id` attributes, only the accessible NAME text a
# snapshot line renders (`role "name" "value"`), so this is a label-text
# heuristic, same limitation `_PASSWORD_HINT` already has. Deliberately NOT
# gated by `policy["password"]` (see `_gateway_redact`'s call site below) —
# same rationale as the extension: a user disabling password redaction is
# not thereby asking to expose a CVV or a one-time code.
#
# Plain `\b` (word boundary) — a HYPHEN is not a `\w` character, so it's
# already a boundary either side of it, meaning "csc-region" DOES match
# `\bcsc\b` (and "my-cvv-field" matches `\bcvv\b`). This is deliberate, a
# reversal of an earlier version that used a lookaround to exclude hyphen
# neighbours: `classifySensitiveField` (the extension side this mirrors)
# only ever decides whether to redact a FORM FIELD'S VALUE, so a name/id
# merely containing "csc"/"cvv" alongside an unrelated word costs at most an
# unrelated field's value getting blanked — for this control, over-redaction
# is the accepted-safe failure, and under-redacting a real hyphenated CVV/
# OTP field (`billing-cvv`, `otp-code` — both extremely common on real
# checkout/login forms) is the one that can't ship.
_ALWAYS_SENSITIVE_HINT = re.compile(
    r"(?i)\b(?:cvv|cvc|csc|otp|one[- ]?time code|security code|verification code|card verification)\b"
)


def _always_sensitive_hint(line: str) -> bool:
    return bool(_ALWAYS_SENSITIVE_HINT.search(line))


# speedimprovements.md H1: committing-action classification, gateway side.
# This is a 1:1 PORT of extension/src/background/commit-classify.ts's own
# COMMITTING_NAMES/isCommittingName/isCommittingTarget -- the two MUST stay
# identical (same names, same case-insensitive whole-word rule) or the
# gateway's cache-based pre-check (run against cached element_meta BEFORE
# dispatch -- see _act_classify_committing) and the extension's own
# classification (a `classify_only` answer, or the `committing` label on a
# real act's result) could disagree about the same element. If one list
# changes, change the other.
COMMITTING_NAMES: Tuple[str, ...] = (
    "Finish", "Submit", "Send", "Pay", "Purchase", "Buy", "Place order",
    "Delete", "Remove", "Confirm", "Publish", "Transfer", "Deploy", "Power off",
)
_COMMITTING_NAME_PATTERNS = tuple(
    re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE) for name in COMMITTING_NAMES
)


def _is_committing_name(name: Optional[str]) -> bool:
    """True when `name` (an element's accessible name) contains one of
    COMMITTING_NAMES as a case-insensitive whole word/phrase -- mirrors
    commit-classify.ts's `isCommittingName` exactly."""
    if not name:
        return False
    return any(p.search(name) for p in _COMMITTING_NAME_PATTERNS)


def _is_committing_target(action: str, role: str = "", name: Optional[str] = None) -> bool:
    """speedimprovements.md H1's full classification: true when `action` is
    a form submit (page.act's dedicated `submit` action) OR `name` matches
    one of COMMITTING_NAMES -- mirrors commit-classify.ts's
    `isCommittingTarget` exactly. `role` is accepted for symmetry with the
    extension-side call sites (element_meta's shape) but not currently used
    by the classification rule itself, which is name/action only, per H1's
    own spec wording."""
    del role  # unused, see docstring
    return action == "submit" or _is_committing_name(name)
_TRAILING_QUOTED_VALUE = re.compile(r'"[^"]*"\s*$')
_QUOTED_SPAN = re.compile(r'"[^"]*"')
_ALREADY_REDACTED_RE = re.compile(r"\[redacted(?::[a-z]+)?\]", re.IGNORECASE)
# G1.6.8: bare AWS access key ids — mirrors extension/src/lib/redaction.ts's
# `AWS_ACCESS_KEY_PATTERN` exactly (fixed 16-char trailing count, same reason:
# it's what keeps this from over-matching a longer token that merely starts
# with those four letters). `AKIA` = long-term IAM user key, `ASIA` =
# temporary STS-issued key.
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")


def _line_already_redacted(line: str) -> bool:
    """True if ``line`` already carries the extension's `[redacted:<kind>]`
    marker (or this module's own `[REDACTED]`) — i.e. nothing new to catch."""
    return bool(_ALREADY_REDACTED_RE.search(line))


def _luhn_ok(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _gateway_redact(text: str, device_id: str) -> Tuple[str, int]:
    """Re-scan text that already passed through in-extension redaction,
    honouring `device_id`'s reported redaction policy (`state.get_redaction_policy`).

    Returns (possibly-redacted text, count of NEW hits found here). Card-shaped
    digit runs are Luhn-checked before being treated as a hit, since admin
    pages are full of long numeric ids/order numbers that would otherwise
    flood the audit log with false positives. Password hits are heuristic:
    a line whose role/name mentions "password", isn't already marked
    redacted (``_line_already_redacted``), and carries a SEPARATE quoted
    value alongside its quoted name (2+ quoted spans — e.g. a snapshot line
    shaped `role "name" "value"`) has that trailing value blanked. A line
    with only ONE quoted span (`[4] text "Password"` — a bare label, no
    distinct value field) is left alone: it never held a secret to catch, so
    "catching" it would be its own false positive, just via a different
    mechanism than the marker mismatch this re-check exists to fix.

    Each check below is skipped outright when the policy has that kind
    turned off — not run-then-discarded, so a disabled kind costs nothing
    extra and, more importantly, can never accidentally increment `hits`
    (which drives the `redaction_gateway_catch` audit event: firing it for a
    kind the user deliberately disabled would misreport a deliberate choice
    as an extension-side redaction failure).
    """
    if not text:
        return text, 0
    policy = state.get_redaction_policy(device_id)
    hits = 0

    def _sub_cc(match: "re.Match[str]") -> str:
        nonlocal hits
        digits = re.sub(r"[ -]", "", match.group(0))
        if len(digits) < 13 or len(digits) > 19 or not _luhn_ok(digits):
            return match.group(0)
        hits += 1
        return REDACTED_PLACEHOLDER

    if policy["card"]:
        text = _CC_CANDIDATE.sub(_sub_cc, text)

    def _sub_ssn(_match: "re.Match[str]") -> str:
        nonlocal hits
        hits += 1
        return REDACTED_PLACEHOLDER

    if policy["ssn"]:
        text = _SSN.sub(_sub_ssn, text)

    def _sub_email(_match: "re.Match[str]") -> str:
        nonlocal hits
        hits += 1
        return REDACTED_PLACEHOLDER

    if policy["email"]:
        text = _EMAIL.sub(_sub_email, text)

    def _sub_phone(_match: "re.Match[str]") -> str:
        nonlocal hits
        hits += 1
        return REDACTED_PLACEHOLDER

    if policy["phone"]:
        text = _PHONE.sub(_sub_phone, text)

    # Password/CVV/OTP all share the same line-shaped mechanism (a snapshot
    # line's own name is quoted, and — if a distinct value field leaked too —
    # so is that): `_PASSWORD_HINT` is gated by `policy["password"]` exactly
    # as before; `_always_sensitive_hint` (G1.6.8, CVV/OTP) runs
    # UNCONDITIONALLY, regardless of that same toggle — see its own comment.
    # Both are checked per line so one hint firing doesn't suppress the
    # other, and a line already marked redacted is never processed twice.
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if _line_already_redacted(line):
            continue
        hinted = (policy["password"] and _PASSWORD_HINT.search(line)) or _always_sensitive_hint(line)
        if not hinted:
            continue
        if len(_QUOTED_SPAN.findall(line)) < 2:
            # Only the element's own name/label is quoted here (e.g. a
            # StaticText node reading "Password") — there is no distinct
            # value field to have leaked, so there is nothing to catch.
            continue
        new_line, n = _TRAILING_QUOTED_VALUE.subn(f'"{REDACTED_PLACEHOLDER}"', line)
        if n:
            lines[i] = new_line
            hits += n
    text = "\n".join(lines)

    # AWS access key ids (G1.6.8): bare `AKIA…`/`ASIA…`, unconditional and
    # shape-based (not line-dependent), same as card/ssn/email/phone above —
    # mirrors extension/src/lib/redaction.ts's `redactSecrets` AWS pattern.
    def _sub_aws(_match: "re.Match[str]") -> str:
        nonlocal hits
        hits += 1
        return REDACTED_PLACEHOLDER

    text = _AWS_ACCESS_KEY.sub(_sub_aws, text)

    return text, hits


def _ok_dict(**fields: Any) -> Dict[str, Any]:
    return {"success": True, **fields}


def _err_dict(message: str, **fields: Any) -> Dict[str, Any]:
    return {"success": False, "error": message, **fields}


def _ok(**fields: Any) -> str:
    return json.dumps(_ok_dict(**fields), default=str)


def _err(message: str, **fields: Any) -> str:
    return json.dumps(_err_dict(message, **fields), default=str)


def bridge_available() -> bool:
    """check_fn for every browser_bridge tool: is a paired device online?"""
    try:
        return state.has_active_device()
    except Exception:
        return False


def _session_key(kwargs: Dict[str, Any]) -> str:
    for name in _SESSION_KWARG_NAMES:
        value = kwargs.get(name)
        if value:
            return str(value)
    return _DEFAULT_SESSION


# -- G3.1 first-class sessions: resolving the raw Hermes kwarg above into a
# persistent agent_sessions.id --------------------------------------------
#
# `_session_key` above is unchanged and stays that way: it is still what
# every call site reads first, and its return value is still exactly what a
# caller that never touches `browser_bridge_session` gets back as its
# "holder" (see `ensure_shadow_session`'s docstring — the identity is
# preserved bit-for-bit for that, the overwhelmingly common, case). What
# changes with G3.1 is that this raw value is no longer used *directly* as
# the lease holder / session_grants key: it is first resolved, through
# `state.py`'s session_bindings table, to a persistent agent_sessions row —
# ordinarily a brand-new row whose id IS the raw value (so nothing observable
# changes), but after an explicit `browser_bridge_session resume` it can
# resolve to a DIFFERENT, older row instead. That is the whole point: a new
# Hermes conversation's raw session_id can pick up a previously labeled,
# persistent identity instead of starting cold (coveragegaps.md G3.1.3).
#
# This function is the one seam every `holder = _session_key(kwargs)` call
# site in this file and its siblings (session_powers.py, dialogs.py,
# evaluate.py, upload.py, navigation.py, http_auth.py) now goes through
# instead. It deliberately does NOT touch attach.py's lease machinery — the
# string it returns is simply what gets passed in as `holder`, exactly as
# `_session_key`'s return value always was; `attach.AttachRegistry` itself is
# untouched, per this workstream's scope.
#
# Deliberate behaviour change (G3.1.3): a call with no session kwarg at all
# used to collapse to one shared `"default-session"` bucket process-wide.
# It now resolves to the most recently active OPEN session on the CALLING
# DEVICE instead — still a single shared identity for that device's kwarg-less
# callers, but device-scoped rather than global, and a real, describable
# agent_sessions row rather than a magic string. A device with no session at
# all yet gets one auto-created on first use.
def _resolve_session(device_id: str, kwargs: Dict[str, Any]) -> Tuple[str, str]:
    """Return (session_id, error_json) — error_json is "" on success.

    session_id is always an OPEN agent_sessions.id belonging to device_id.
    The only failure mode is a raw kwarg explicitly bound (by a prior
    `browser_bridge_session resume`, or by this same raw kwarg's own earlier
    calls) to a session that has since been closed — G3.1.6's "a closed
    session refuses further tool calls with a named reason".
    """
    raw = _session_key(kwargs)
    if raw == _DEFAULT_SESSION:
        session = state.most_recent_open_agent_session(device_id)
        if session is None:
            session = state.create_agent_session(device_id, label="")
        state.touch_agent_session(session["id"])
        return session["id"], ""

    binding = state.get_session_binding(raw, device_id)
    session = state.get_agent_session(binding["agent_session_id"]) if binding is not None else None
    if session is None:
        session = state.ensure_shadow_session(device_id, raw)
        state.bind_hermes_session(raw, device_id, session["id"])
    elif session.get("closed_at") is not None:
        reason, code = refusals.format_refusal("session_closed", session=session.get("label") or session["id"])
        return "", _err(reason, code=code, session_id=session["id"])
    state.touch_agent_session(session["id"])
    return session["id"], ""


def _origin_of(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    # chrome://, about:, file:// etc. have no netloc; the scheme is the
    # closest thing to an "origin" and grants can still be set against it.
    return parts.scheme + ":" if parts.scheme else url


# G0.6.6 device layer: which extension setting must be ON for a capability.
# Capabilities absent from this table carry no device toggle (snapshot, read,
# act and the rest predate the powers section and are governed by the origin
# mode alone) -- absence means "no extra gate", never "denied", or every
# shipped tool would stop working the moment this table landed.
#
# Two entries are deliberately not one-to-one, per the seam report from the
# settings workstream:
#   upload  - two settings back it (`allowFileUpload` reads a path off the
#             operator's own disk, `allowFileUploadFromAgent` takes bytes the
#             gateway already holds). Either one enabled lets the CAPABILITY
#             through here; the upload call site must additionally check the
#             specific variant it is about to use, because this gate cannot
#             know which one that is.
#   dialog  - `allowDialogAccept` only. Dismissing a dialog is not gated: it
#             is the safe direction and the default behaviour, and requiring
#             a toggle to dismiss would leave a wedged renderer with no way out.
_CAPABILITY_POWER_KEYS: Dict[str, Tuple[str, ...]] = {
    "upload": ("allowFileUpload", "allowFileUploadFromAgent"),
    "evaluate": ("allowEvaluate",),
    "console": ("allowConsoleRead",),
    "cookies_write": ("allowCookieWrite",),
    "http_auth": ("allowHttpAuth",),
    "downloads": ("allowDownloadsRead",),
    "dialog": ("allowDialogAccept",),
}


def _device_power_denial(device_id: str, capability: str) -> Optional[Tuple[str, int]]:
    """The user-side half of G0.6.6's two-layer rule: has this device's own
    extension enabled the setting this capability needs?

    Returns None when there is nothing to refuse. Read fail-closed via
    `state.power_enabled` -- a device that has never reported a power policy
    has every power OFF, which is why a capability with no entry in
    `_CAPABILITY_POWER_KEYS` must return early rather than fall through to a
    lookup that would deny it.

    The extension enforces this too, before it ever touches the page. Neither
    layer alone is sufficient: the extension is not the security boundary
    (plan.md §6.5), and this one can be lied to by a device that reports a
    policy it does not honour.
    """
    keys = _CAPABILITY_POWER_KEYS.get(capability)
    if not keys:
        return None
    if any(state.power_enabled(device_id, key) for key in keys):
        return None
    setting_names = " or ".join(f"'{key}'" for key in keys)
    reason, _ = refusals.format_refusal(
        "power_capability_disabled", capability=capability, settingNames=setting_names,
    )
    audit.record(
        "grant_check", device=device_id, capability=capability, mode="device_power_disabled",
        decision="deny", reason=reason,
    )
    return reason, protocol.GRANT_DENIED


# Version-skew safety net for the ceiling set below. approvals.py is the
# single source of truth; this literal exists only for the window in which
# THIS file is newer than the hermes_plugin/approvals.py sitting beside it —
# a real state on this gateway, whose deploy is an rsync followed by a
# restart, with a documented __pycache__ staleness trap. Without it, a
# partially-updated tree raises AttributeError inside BOTH gates on every
# tool call rather than degrading. `test_gate_ceiling_fallback_matches`
# asserts the two agree whenever both are present, so drift fails a test
# instead of silently widening the ceiling.
_DANGEROUS_CAPABILITIES_FALLBACK = frozenset({
    "upload", "evaluate", "cookies_write", "http_auth", "dialog", "downloads", "console",
})


def _dangerous_capability(capability: str) -> bool:
    """Shared by `_gate_reason` and `_authorize`: is `capability` one of
    G0.5's DANGEROUS_CAPABILITIES, for which a 'full' origin grant must not
    early-return an allow?

    Two distinct degradations, deliberately handled differently:

    - `approvals.py` absent entirely (ImportError) -> no capability is
      dangerous. Such a gateway cannot call `approvals.require(...)` at all,
      so `_authorize` already refuses every 'request'-mode call outright and
      the ceiling is unreachable anyway.
    - `approvals.py` present but OLDER than this file (no
      DANGEROUS_CAPABILITIES attribute) -> fall back to the literal above and
      audit the skew. This case is NOT unreachable: `require()` still works,
      so the ceiling still matters, and treating "attribute missing" as "not
      dangerous" would silently drop the ceiling for exactly the capabilities
      it exists to hold. Reading the attribute unguarded instead raised into
      both gates, which is how it was found.
    """
    try:
        from . import approvals  # noqa: PLC0415 - optional sibling module, workstream H
    except ImportError:
        return False
    dangerous = getattr(approvals, "DANGEROUS_CAPABILITIES", None)
    if dangerous is None:
        dangerous = _DANGEROUS_CAPABILITIES_FALLBACK
        audit.record(
            "capability_set_skew",
            capability=capability,
            detail="hermes_plugin/approvals.py predates this tools.py; using the local ceiling set",
        )
    return capability in dangerous


def _gate_reason(device_id: str, origin: str, capability: str) -> Optional[str]:
    """Gateway-side grant enforcement (plan §6.2, non-negotiable).

    Returns None when the call may proceed, or the refusal text otherwise.
    Every decision is audited — allow and deny alike — independent of what
    the popup told the extension, because the grants table is the one this
    workstream treats as authoritative (state.py's own docstring: "the popup
    is UX; this database is law").
    """
    mode = state.get_mode(device_id, origin)
    dangerous = _dangerous_capability(capability)
    if mode == "full" and not dangerous:
        audit.record("grant_check", device=device_id, origin=origin, capability=capability, mode=mode, decision="allow")
        return None
    if mode == "off":
        reason, _ = refusals.format_refusal("origin_off", origin=origin, capability=capability)
    elif dangerous:
        # G0.5's ceiling, specifically the "'full', but the capability is
        # dangerous" case (mode == "off" was already handled above). Unlike
        # `_authorize`, `_gate_reason` has no approval-transport integration
        # of its own -- it is the M1 read-only gate, kept exactly as it was
        # for the capabilities that use it today (snapshot/read/screenshot/
        # attach, none of which are dangerous). If a future capability in
        # DANGEROUS_CAPABILITIES is ever wired through THIS gate instead of
        # `_authorize`, refusing here — rather than silently allowing under
        # 'full', or presenting the ordinary 'request'-mode copy below (which
        # opens with "origin is in 'request' mode", false in this branch) —
        # is the fail-closed choice until it is rehomed onto `_authorize`,
        # which can actually present a prompt.
        reason, _ = refusals.format_refusal("dangerous_ceiling_readonly_gate", capability=capability, origin=origin)
    else:
        reason, _ = refusals.format_refusal("origin_request_readonly", origin=origin, capability=capability)
    audit.record("grant_check", device=device_id, origin=origin, capability=capability, mode=mode, decision="deny", reason=reason)
    return reason


# -- M2: approval-aware gate for mutating/revealing capabilities -------------
#
# `_gate_reason` above is M1's gate and stays exactly as it was for
# snapshot/read/attach: 'request' mode is refused outright with a pointer at
# M2, because those calls never mutate anything and M1 shipped with no
# approval queue at all. `_authorize` below is the M2 gate used only for
# `browser_bridge_act` (always mutating) and `browser_bridge_ask` (only when
# it would surface page content back to the model — see handle_ask). It
# calls `hermes_plugin/approvals.py`'s `require()`, imported defensively
# exactly like M1's vision seam — approvals.py now presents through Hermes'
# native `ctx.register_approval_transport` contract (v0.21.4) rather than
# owning its own persisted queue, but that is entirely internal to
# `require()`; this seam and its fallback are unchanged. If that module
# isn't present at all on this gateway (an install missing approvals.py, not
# "the operator hasn't selected browser-bridge as the system transport" —
# `require()` never depends on that selection), 'request' mode falls back to
# M1's behaviour — refuse clearly — rather than silently treating 'request'
# as 'full'.
def _authorize(
    device_id: str,
    origin: str,
    capability: str,
    summary: str,
    session_key: str,
    detail: str = "",
    require_explicit_grant: bool = False,
) -> Optional[Tuple[str, int]]:
    """Gateway-side grant + approval enforcement for a mutating/revealing call.

    Returns None when the call may proceed, or ``(reason, protocol_code)``
    otherwise — the code lets callers surface an accurate wire error
    (``GRANT_DENIED`` for off/no-queue, ``APPROVAL_DENIED`` for an explicit
    user "no", ``TIMEOUT`` when nobody answered) instead of collapsing every
    refusal into one bucket. 'full' allows immediately; 'off' refuses
    immediately; 'request' calls ``approvals.require(...)`` and honours its
    Decision. Every outcome is audited under the same ``grant_check`` event
    M1 uses, so ``audit.tail(event_filter="grant_check")`` remains the one
    place to read every gated decision this plugin has ever made, M1 or M2
    alike.

    ``require_explicit_grant`` (G2.2.13): when true, an origin with NO
    grants-table row is refused outright — never falling back to
    ``config.py``'s ``default_mode`` the way an ordinary tab-origin check
    does. This is for authorizing an EMBEDDED FRAME's own origin (`handle_act`
    on a frame-qualified selector), which must never inherit a permissive
    fleet-wide default the user never separately approved for that specific
    third-party origin — the same rule ``_reconcile_frame_origins`` already
    applies to snapshot text, now applied to acting too. A `full` TOP-tab
    origin does not, by itself, make `state.get_mode` return `full` for an
    unrelated frame origin (they're different rows), but this flag closes
    the gap for the case where `default_mode` alone would have.
    """
    # G0.9 operator kill switch, checked first and ANDed with everything
    # below: `browser_bridge.powers.<capability>: false` in config.yaml is a
    # fleet-wide "no" that no per-device origin grant, per-capability grant,
    # or live approval decision can override — it exists so an operator can
    # pull a capability without touching Chrome on every paired device. Only
    # the literal `False` disables (see config.py's DEFAULTS comment on
    # `powers` for why "anything else means enabled" is the correct fail-open
    # default here, the mirror image of a redaction kind's fail-closed read).
    if config.load().get("powers", {}).get(capability) is False:
        reason, _ = refusals.format_refusal("operator_capability_disabled", capability=capability)
        audit.record(
            "grant_check", device=device_id, origin=origin, capability=capability, mode="operator_disabled",
            decision="deny", reason=reason,
        )
        return reason, protocol.GRANT_DENIED

    device_denial = _device_power_denial(device_id, capability)
    if device_denial is not None:
        return device_denial

    if require_explicit_grant and not state.has_explicit_grant(device_id, origin):
        reason, code = refusals.format_refusal("frame_no_explicit_grant", origin=origin, capability=capability)
        audit.record(
            "grant_check", device=device_id, origin=origin, capability=capability, mode="no_explicit_grant",
            decision="deny", reason=reason,
        )
        return reason, code

    mode = state.get_mode(device_id, origin)
    dangerous = _dangerous_capability(capability)
    if mode == "full" and not dangerous:
        audit.record("grant_check", device=device_id, origin=origin, capability=capability, mode=mode, decision="allow")
        return None
    if mode == "off":
        reason, _ = refusals.format_refusal("origin_off", origin=origin, capability=capability)
        audit.record("grant_check", device=device_id, origin=origin, capability=capability, mode=mode, decision="deny", reason=reason)
        return reason, protocol.GRANT_DENIED

    # mode == "request", OR mode == "full" with `capability` in
    # DANGEROUS_CAPABILITIES (G0.5's ceiling: a 'full' origin grant does not
    # cover these — they fall through to the same approvals.require() path
    # 'request' mode uses, which itself checks state.get_capability_grant()
    # (an earlier "always" for just this capability) before ever presenting
    # a live prompt).
    try:
        from . import approvals  # noqa: PLC0415 - optional sibling module, workstream H
    except ImportError:
        # G0.8: both the 'full'-but-dangerous and 'request' degradations
        # share one catalogue entry -- the cause (approvals.py not loaded)
        # and the workaround (wait for it, or set 'full' if not already)
        # are identical either way; the branch that got here is already
        # captured in the audited `mode` field below.
        reason, _ = refusals.format_refusal("approval_transport_missing", capability=capability, origin=origin)
        audit.record(
            "grant_check", device=device_id, origin=origin, capability=capability, mode=mode,
            decision="deny", reason="approvals module unavailable",
        )
        return reason, protocol.GRANT_DENIED

    safe_detail, _ = _gateway_redact(detail, device_id)
    try:
        decision = approvals.require(device_id, origin, capability, summary, session_key, detail=safe_detail)
    except Exception as exc:  # the approval transport itself failed — never silently allow
        reason = f"approval request failed ({type(exc).__name__}: {exc}); {capability} is refused, not silently allowed"
        audit.record(
            "grant_check", device=device_id, origin=origin, capability=capability, mode=mode,
            decision="deny", reason=f"approvals.require raised {type(exc).__name__}",
        )
        return reason, protocol.INTERNAL_ERROR

    allowed = bool(getattr(decision, "allowed", False))
    scope = str(getattr(decision, "scope", "") or "")
    decision_reason = str(getattr(decision, "reason", "") or "")
    audit.record(
        "grant_check", device=device_id, origin=origin, capability=capability, mode=mode,
        decision="allow" if allowed else "deny", scope=scope, reason=decision_reason,
    )
    if allowed:
        return None
    if scope == "timeout":
        return decision_reason or f"the user did not respond to the {capability} approval request in time", protocol.TIMEOUT
    return decision_reason or f"the user denied {capability} for {origin!r}", protocol.APPROVAL_DENIED


def _require_commit_approval(
    device_id: str, origin: str, session_key: str, summary: str, detail: str = ""
) -> Optional[Tuple[str, int]]:
    """speedimprovements.md H1: the user-approval half of commit-mode
    'pause' enforcement — ALWAYS required for a committing `page.act` in
    pause mode, layered ON TOP of (never instead of) whatever `_authorize`
    already granted for the ordinary "act" capability. Deliberately does
    NOT reuse `_authorize` itself: that function's `mode == "full" and not
    dangerous` early return would let a 'full' origin skip the approval
    prompt entirely, which is exactly what H1 says must NOT happen
    ("whatever the origin's mode"). Uses its own capability id ("commit",
    distinct from "act") so the popup's prompt reads as a specific
    committing-action confirmation and a standing "always" grant for
    ordinary acts never silently covers this too.

    Returns `(reason, protocol_code)` to refuse, or `None` to proceed — same
    Decision-to-(reason, code) mapping `_authorize`'s own `approvals.require`
    call site uses, reused verbatim rather than re-derived; a denial here is
    always `COMMIT_APPROVAL_DENIED` (never the generic `APPROVAL_DENIED`),
    per protocol/schema.json's `confirm` param description.
    """
    capability = "commit"
    try:
        from . import approvals  # noqa: PLC0415 - optional sibling module, workstream H
    except ImportError:
        reason, _ = refusals.format_refusal("approval_transport_missing", capability=capability, origin=origin)
        audit.record(
            "grant_check", device=device_id, origin=origin, capability=capability, mode="commit_pause",
            decision="deny", reason="approvals module unavailable",
        )
        return reason, protocol.GRANT_DENIED

    safe_detail, _ = _gateway_redact(detail, device_id)
    try:
        decision = approvals.require(device_id, origin, capability, summary, session_key, detail=safe_detail)
    except Exception as exc:  # the approval transport itself failed — never silently allow
        reason = f"approval request failed ({type(exc).__name__}: {exc}); {capability} is refused, not silently allowed"
        audit.record(
            "grant_check", device=device_id, origin=origin, capability=capability, mode="commit_pause",
            decision="deny", reason=f"approvals.require raised {type(exc).__name__}",
        )
        return reason, protocol.INTERNAL_ERROR

    allowed = bool(getattr(decision, "allowed", False))
    scope = str(getattr(decision, "scope", "") or "")
    decision_reason = str(getattr(decision, "reason", "") or "")
    audit.record(
        "grant_check", device=device_id, origin=origin, capability=capability, mode="commit_pause",
        decision="allow" if allowed else "deny", scope=scope, reason=decision_reason,
    )
    if allowed:
        return None
    if scope == "timeout":
        return decision_reason or "the user did not respond to the commit approval request in time", protocol.TIMEOUT
    return decision_reason or f"the user denied the committing action for {origin!r}", protocol.COMMIT_APPROVAL_DENIED


def _act_precheck_committing(
    device_id: str, tab_id: int, action: str, args: Dict[str, Any], registry: Any
) -> Optional[bool]:
    """speedimprovements.md H1: the CHEAP half of pre-dispatch
    classification — only what's already known with no round trip.

    Returns ``True``/``False`` when that's enough to decide: a form `submit`
    (always committing); an action with no element target at all (no idx,
    selector or xy — `fill`/`key`/`scroll`/`navigate`/`wait_for`/history
    moves; H1 classifies act TARGETS, so these are not committing); an idx
    whose cached element_meta (`registry.resolve_index_meta`, the same
    role/name `_act_run_one_step` sends as `expect`) names the control; or
    an idx that isn't in the index map at all (False here only because the
    real resolution in `_act_run_one_step` refuses that call before any
    dispatch, with its own more precise error).

    Returns ``None`` when the cache can't answer — a bare selector, a bare
    xy, or an idx with no element_meta. The caller must then ask the
    extension (`_act_classify_via_extension`) BEFORE dispatching; ``None``
    is never treated as "not committing".
    """
    if action == "submit":
        return True
    idx = args.get("idx")
    if idx is not None:
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            return False  # `_validate_act_args`/real resolution refuse this before dispatch
        if registry.resolve_index(device_id, tab_id, idx) is None:
            return False  # refused before dispatch by the real resolution below
        meta = registry.resolve_index_meta(device_id, tab_id, idx)
        if meta is None:
            return None
        return _is_committing_target(action, meta.get("role", ""), meta.get("name"))
    if args.get("selector") or args.get("xy") is not None:
        return None
    return False


# speedimprovements.md H1: the protocol version that introduced page.act's
# `classify_only` (protocol/schema.json's protocolVersion was bumped to 1.3
# for it). Never send classify_only to an older build -- it would ignore the
# flag and really act.
CLASSIFY_ONLY_MIN_PROTOCOL = (1, 3)


def _device_supports_classify_only(device_id: str) -> bool:
    """Whether `device_id`'s connected extension reported a protocol_version
    new enough to honour `classify_only`. Unknown/unparseable means no
    (fail closed: the caller then treats the target as committing)."""
    try:
        connected = relay_mod.get_relay().status().get("connected", [])
    except Exception:  # noqa: BLE001 - any relay failure means "can't tell", i.e. no
        return False
    for entry in connected:
        if entry.get("device_id") == device_id:
            parsed = relay_mod._parse_protocol_version(entry.get("protocol_version"))  # noqa: SLF001
            return parsed is not None and parsed >= CLASSIFY_ONLY_MIN_PROTOCOL
    return False


def _act_classify_via_extension(
    device_id: str, tab_id: int, action: str, args: Dict[str, Any], registry: Any, frame_origin: Optional[str],
) -> Tuple[bool, str]:
    """speedimprovements.md H1: the fail-closed half of pre-dispatch
    classification, for a target `_act_precheck_committing` couldn't
    decide from cache. Sends `page.act` with `classify_only: true` — the
    extension resolves the same target the real act would (selector, else
    xy hit-test plus the hit node's nearest accessibility ancestors) and
    returns `{role, name, committing}` WITHOUT dispatching anything (see
    act.ts's `classifyActTarget`).

    Returns ``(committing, source)``. ANY failure — a BridgeError (element
    gone, limited-mode xy, hit-test miss, timeout, an older extension build
    that doesn't know `classify_only`) or a malformed answer — is treated
    as committing (``(True, "classify_failed")``): pause mode must fail
    closed, never let an unclassifiable click through unconfirmed.

    An extension build that predates `classify_only` would ignore the flag
    and perform the act FOR REAL, so the request is only ever sent to a
    device whose reported protocol_version is at least
    `CLASSIFY_ONLY_MIN_PROTOCOL` (the version that introduced it); an older
    device is classified as committing without any wire call. A reply that
    looks like a real act (carries `diff`) is also treated as a failure.
    """
    if not _device_supports_classify_only(device_id):
        return True, "classify_failed"
    wire: Dict[str, Any] = {
        "tabId": tab_id,
        "action": action,
        "classify_only": True,
        "timeout_ms": DEFAULT_ACT_TIMEOUT_MS,
    }
    if args.get("idx") is not None:
        wire["selector"] = registry.resolve_index(device_id, tab_id, int(args["idx"]))
    elif args.get("selector"):
        wire["selector"] = str(args["selector"])
    if args.get("xy") is not None:
        wire["xy"] = [int(v) for v in args["xy"]]
    if frame_origin is not None:
        wire["frameOrigin"] = frame_origin
    try:
        result = relay_mod.get_relay().call(device_id, "page.act", wire, timeout=DEFAULT_ACT_TIMEOUT_MS / 1000.0 + 5)
    except relay_mod.BridgeError:
        return True, "classify_failed"
    committing = result.get("committing") if isinstance(result, dict) else None
    if not isinstance(committing, bool) or "diff" in result:
        return True, "classify_failed"
    return committing, "extension"


def _act_classify_committing(
    device_id: str, tab_id: int, action: str, args: Dict[str, Any], registry: Any, frame_origin: Optional[str],
) -> Tuple[bool, str]:
    """Pause-mode pre-dispatch classification: cache first, then the
    extension's classify-only round trip, failing closed. Returns
    ``(committing, source)`` with source in {"cache", "extension",
    "classify_failed"} for the audit trail."""
    cached = _act_precheck_committing(device_id, tab_id, action, args, registry)
    if cached is not None:
        return cached, "cache"
    return _act_classify_via_extension(device_id, tab_id, action, args, registry, frame_origin)


# -- CDP method whitelist (plan §6.5, non-negotiable) ------------------------
#
# No tool in this file sends `cdp.send` directly today: browser_bridge_act
# talks the higher-level `page.act` (the extension owns the CDP calls that
# implement click/type/navigate/etc). This gate exists anyway, gateway-side,
# for the day a raw passthrough IS exposed — plan.md §6.5 is explicit that
# "the whitelist lives here... because the popup and the extension are not
# the security boundary" — a compromised or buggy extension build must not be
# the only thing standing between the model and `Fetch.*`-grade MITM. Every
# refusal is audited (plan's own words: "audit every refusal").
#
# Runtime.evaluate is allowed but eval-limited per plan §6.5; no other
# Runtime.* method is on this list (Runtime.callFunctionOn,
# Runtime.awaitPromise, etc. would widen exactly the sandbox
# Runtime.evaluate's own limits exist to bound, so they are deliberately
# left off rather than swept in by a Runtime.-prefix rule).
CDP_EXACT_ALLOW = frozenset({
    "Page.navigate",
    "Page.reload",
    "Page.captureScreenshot",
    "Network.enable",
    "Runtime.evaluate",
    # Background-tab focus bug fix: makes an attached page BEHAVE as focused
    # (focus/blur events, :focus-visible, document.hasFocus()) without ever
    # raising the real OS window -- the opposite of a capability that needs
    # gating, so it is allowed exactly like the other read/behavior-only
    # entries above. The extension calls this directly via
    # chrome.debugger.sendCommand on every attach (background/cdp.ts);
    # nothing routes it through this whitelist today (see this module's own
    # "no tool sends a raw cdp.send frame" note above) -- named here anyway
    # so the day a raw passthrough IS exposed, this method is already
    # correctly classified rather than falling through to the generic
    # Emulation.* refusal a prefix rule would otherwise produce.
    "Emulation.setFocusEmulationEnabled",
    # G3 (ProjectRules/speedimprovements.md, browser_bridge_inspect's
    # `listeners` question): read-only introspection -- returns the event
    # listeners bound to a node (type, capture/passive/once, source
    # location), never adds/removes/fires one. Exact-allowed rather than
    # swept in by a DOMDebugger.-prefix rule the same way Emulation.* isn't:
    # only this one method is classified, nothing else in the DOMDebugger
    # domain (e.g. setDOMBreakpoint, which sets debugger state) is allowed.
    # See docs/security.md §4 for the read-only note this whitelist entry
    # mirrors. As with every other entry in this set, nothing routes it
    # through this gateway-side whitelist today (dom.inspect's CDP calls are
    # made directly by the extension's own background.ts, exactly like
    # Emulation.setFocusEmulationEnabled above) -- named here so the day a
    # raw passthrough IS exposed, this method is already correctly
    # classified.
    "DOMDebugger.getEventListeners",
    # speedimprovements.md G2: the taller/wider-page-for-one-call override
    # (background/viewport-override.ts's `withViewportOverride`, used by
    # dom.snapshot/page.screenshot's own `viewport` param). Like
    # setFocusEmulationEnabled above, the extension calls this directly via
    # chrome.debugger.sendCommand today (nothing routes it through this
    # whitelist yet — see this module's "no tool sends a raw cdp.send frame"
    # note above); named here so a future raw passthrough classifies it
    # correctly instead of falling through to the generic Emulation.*
    # refusal. Read-only in the sense that matters here: it changes what the
    # ATTACHED TAB's renderer believes its own viewport is, never the user's
    # real OS window, and is always paired with a clear in the same call.
    "Emulation.setDeviceMetricsOverride",
    "Emulation.clearDeviceMetricsOverride",
})
CDP_PREFIX_ALLOW = ("Input.", "DOM.")
# Named explicitly, never reachable via a prefix rule above: full
# request/response interception is MITM territory the plan defers to phase 3,
# opt-in at best — never on by default.
CDP_EXPLICIT_DENY_PREFIX = ("Fetch.",)
# Individual methods excluded even though they'd otherwise match a
# whitelisted domain's prefix rule. DOM.setFileInputFiles hands a page's
# <input type=file> a path off the OPERATOR's own disk — a local-file-access
# primitive with nothing in common with the read-only DOM.* inspection calls
# (getDocument, querySelector, ...) the DOM. prefix rule exists to allow.
CDP_EXPLICIT_DENY_EXACT = frozenset({"DOM.setFileInputFiles"})
# A well-formed CDP method is exactly `Domain.method`, both identifier-shaped
# — one dot, no more, no less. Matched (and the domain checked against
# _CDP_KNOWN_DOMAINS) BEFORE any prefix/exact-allow logic runs, because a
# naive `method.startswith("Input.")` check alone would let a crafted method
# like "Input.foo.Fetch.enable" ride the Input. prefix straight past the
# Fetch. deny list — the deny list only ever sees the FULL method string, and
# `startswith` doesn't care what comes after the prefix it matched.
_CDP_METHOD_SHAPE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)\.([A-Za-z][A-Za-z0-9]*)$")
# "Emulation" added for Emulation.setFocusEmulationEnabled above -- only that
# one exact method is allowed from the domain (see CDP_EXACT_ALLOW's own
# comment); being a known domain does not by itself grant a prefix rule the
# way Input./DOM. get one.
_CDP_KNOWN_DOMAINS = frozenset({"Page", "Input", "DOM", "Network", "Runtime", "Emulation", "DOMDebugger"})


def is_cdp_method_allowed(method: str) -> bool:
    """True if ``method`` may be sent via ``cdp.send`` under the plan §6.5 whitelist."""
    if not method:
        return False
    match = _CDP_METHOD_SHAPE_RE.match(method)
    if not match or match.group(1) not in _CDP_KNOWN_DOMAINS:
        # Not `Domain.method` (no dot, more than one dot, non-identifier
        # characters) or not one of the domains this whitelist even
        # considers — refused outright, before any prefix/exact-allow check
        # gets a chance to be fooled by a crafted method string.
        return False
    if method in CDP_EXPLICIT_DENY_EXACT:
        return False
    if any(method.startswith(prefix) for prefix in CDP_EXPLICIT_DENY_PREFIX):
        return False
    if method in CDP_EXACT_ALLOW:
        return True
    return any(method.startswith(prefix) for prefix in CDP_PREFIX_ALLOW)


def _cdp_gate(device_id: str, tab_id: Optional[int], method: str) -> Optional[str]:
    """Audit-and-refuse wrapper around ``is_cdp_method_allowed``.

    Returns None to proceed, or the refusal text. This is the one call site
    anything building a ``cdp.send`` frame must go through — see the module
    docstring above for why it exists even with no tool wired to it yet.
    """
    if is_cdp_method_allowed(method):
        audit.record("cdp_method_check", device=device_id, tab_id=tab_id, method=method, decision="allow")
        return None
    reason = (
        f"CDP method {method!r} is not on the browser_bridge whitelist (Input.*, DOM.*, "
        f"Page.navigate/reload/captureScreenshot, Network.enable, Runtime.evaluate, "
        f"Emulation.setFocusEmulationEnabled, Emulation.setDeviceMetricsOverride, "
        f"Emulation.clearDeviceMetricsOverride) and is refused "
        f"gateway-side, regardless of what the extension or popup would allow"
    )
    if method.startswith("Fetch."):
        reason += (
            " — Fetch.* is full request/response interception (MITM-grade); that's phase-3 "
            "opt-in at best, never on by default"
        )
    audit.record("cdp_method_check", device=device_id, tab_id=tab_id, method=method, decision="deny", reason=reason)
    return reason


def _send_cdp(device_id: str, tab_id: int, method: str, params: Optional[dict] = None) -> str:
    """Gate + dispatch a raw ``cdp.send`` frame through the plan §6.5 whitelist.

    Not wired to any tool schema in M2 — see the whitelist docstring above.
    Kept here, and exported, so the gate has exactly one call site once a
    future tool (or debugging path) needs raw CDP, and so it is directly
    testable today rather than only in theory.
    """
    reason = _cdp_gate(device_id, tab_id, method)
    if reason:
        return _err(reason, code=protocol.METHOD_NOT_FOUND, hint="see plan.md §6.5 for the CDP method whitelist")
    relay = relay_mod.get_relay()
    if relay is None:
        return _err("the bridge relay is not running")
    try:
        result = relay.call(device_id, "cdp.send", {"tabId": tab_id, "method": method, "params": params or {}})
    except relay_mod.BridgeError as exc:
        return _bridge_err(exc, "cdp.send")
    return _ok(device_id=device_id, tab_id=tab_id, method=method, result=result)


def _hint_for_code(code: int) -> str:
    # G0.8: every entry now lives in protocol/schema.json's `codeHints`,
    # generated into protocol.CODE_HINTS -- see refusals.hint_for_code for
    # the numeric-code -> name resolution this delegates to.
    return refusals.hint_for_code(code)


def _bridge_err_dict(exc: "relay_mod.BridgeError", method: str) -> Dict[str, Any]:
    hint = _hint_for_code(exc.code)
    fields: Dict[str, Any] = {"code": exc.code}
    if hint:
        fields["hint"] = hint
    # Structured detail the extension attached to the error (protocol/
    # schema.json's `error.data`) — e.g. ELEMENT_MISMATCH's
    # `{expected, actual}` (see act.ts's page.act `expect` check), carried
    # through by relay.py's Connection.resolve. Locally-raised BridgeErrors
    # still have no `data`, so `detail` stays absent for those; `exc.message`
    # above carries the same expected/actual detail in prose either way.
    if exc.data:
        fields["detail"] = exc.data
    return _err_dict(f"{method} failed: {exc.message}", **fields)


def _bridge_err(exc: "relay_mod.BridgeError", method: str) -> str:
    return json.dumps(_bridge_err_dict(exc, method), default=str)


def _connected_devices() -> List[Dict[str, Any]]:
    relay = relay_mod.get_relay()
    if relay is None:
        return []
    return relay.status()["connected"]


def _resolve_device(args: Dict[str, Any]) -> Tuple[str, str]:
    """Return (device_id, error_json). error_json is "" on success."""
    if relay_mod.get_relay() is None:
        return "", _err("the bridge relay is not running", hint="the gateway may be degraded; check browser_bridge_status")
    connected = _connected_devices()
    explicit = args.get("device_id")
    if explicit:
        if any(c["device_id"] == explicit for c in connected):
            return str(explicit), ""
        return "", _err(f"device {explicit!r} is not connected", hint="call browser_bridge_status for connected devices")
    if len(connected) == 1:
        return connected[0]["device_id"], ""
    if not connected:
        return "", _err("no device is connected", hint="run `hermes browser-bridge pair` and connect the extension")
    return "", _err(
        "multiple devices connected; specify device_id",
        devices=[c["device_id"] for c in connected],
    )


def _paused_refusal(device_id: str, capability: str) -> str:
    """Error JSON when the device's live connection reports sharing paused
    (relay Connection.paused, as handle_status reads it), else "". Checked
    gateway-side before any relay call; the extension refuses as well."""
    entry = next((c for c in _connected_devices() if c.get("device_id") == device_id), None)
    if not entry or not entry.get("paused"):
        return ""
    audit.record("sharing_paused_refusal", device=device_id, capability=capability)
    return _err(
        f"sharing is paused on device {device_id}; {capability} refused",
        code=protocol.SHARING_PAUSED,
        hint=_hint_for_code(protocol.SHARING_PAUSED),
        device_id=device_id,
    )


def _tab_briefs(tabs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    briefs = []
    for t in tabs:
        brief: Dict[str, Any] = {
            "tab_id": t.get("tabId"),
            "title": t.get("title", ""),
            "url": t.get("url", ""),
            "group_id": t.get("groupId"),
            "attached": bool(t.get("attached")),
        }
        # Limited-mode share fix (protocol/schema.json's tabRef.attachMode):
        # present only while attached is true, same rule the wire carries --
        # an older extension build that never reports it simply leaves this
        # key out, exactly like `attached` alone always worked before.
        mode = t.get("attachMode")
        if mode:
            brief["attach_mode"] = mode
            reason = t.get("attachModeReason")
            if reason:
                brief["attach_mode_reason"] = reason
        briefs.append(brief)
    return briefs


def _resolve_target(tabs: List[Dict[str, Any]], args: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    """Resolve an attach target against a fresh tabs.list result.

    Returns (matched_tabs, error_message). error_message is "" on an
    unambiguous match; a non-empty message with a non-empty matched list
    means "ambiguous, here are the candidates" rather than "not found".
    """
    if args.get("tab_id") is not None:
        matched = [t for t in tabs if t.get("tabId") == args["tab_id"]]
        if not matched:
            return [], f"tab_id {args['tab_id']} not found among this device's visible tabs"
        return matched, ""
    if args.get("group_id") is not None:
        matched = [t for t in tabs if t.get("groupId") == args["group_id"]]
        if not matched:
            return [], f"group_id {args['group_id']} has no visible tabs"
        return matched, ""
    if args.get("url"):
        needle = str(args["url"]).lower()
        matched = [t for t in tabs if needle in str(t.get("url", "")).lower()]
        if not matched:
            return [], f"no tab URL contains {args['url']!r}"
        if len(matched) > 1:
            return matched[:8], f"{len(matched)} tabs match {args['url']!r}; narrow with tab_id"
        return matched, ""
    if args.get("title"):
        needle = str(args["title"]).lower()
        matched = [t for t in tabs if needle in str(t.get("title", "")).lower()]
        if not matched:
            return [], f"no tab title contains {args['title']!r}"
        if len(matched) > 1:
            return matched[:8], f"{len(matched)} tabs match {args['title']!r}; narrow with tab_id"
        return matched, ""
    if len(tabs) == 1:
        return tabs, ""
    if not tabs:
        return [], "the device reports no visible tabs"
    return tabs[:8], "multiple tabs visible; specify tab_id, url, title, or group_id"


def _fresh_tabs(device_id: str) -> Tuple[List[Dict[str, Any]], Optional["relay_mod.BridgeError"]]:
    """tabs.list, then reconcile our lease bookkeeping against it.

    The extension is the source of truth for what's actually attached (this
    workstream's brief, and CLAUDE.md's own reconnect gotcha) — every
    snapshot/read/attach call re-derives origin and attach state from a live
    tabs.list rather than trusting a cache that could be stale after a
    reconnect the extension didn't tell us about yet.
    """
    relay = relay_mod.get_relay()
    try:
        result = relay.call(device_id, "tabs.list", {})
    except relay_mod.BridgeError as exc:
        return [], exc
    tabs = result.get("tabs", []) or []
    attached_ids = {t["tabId"] for t in tabs if t.get("attached") and "tabId" in t}
    dropped = attach_mod.get_registry().reconcile(device_id, attached_ids)
    if dropped:
        audit.record("attach_reconciled", device=device_id, dropped=dropped)
    return tabs, None


def _resolve_attached_tab(
    device_id: str, tab_id: Optional[int], capability: str = "page access"
) -> Tuple[Optional[Dict[str, Any]], str]:
    """For snapshot/read: find the target tab and confirm it's attached.

    Reads don't require the caller to hold the driving lease (plan §3.3:
    "reads are lock-free") — only that the tab is attached at all, which is
    the extension's own bookkeeping, re-fetched fresh each call.
    """
    # Paused first: Stop releases every tab, so a tab-resolution error here
    # would read as "attach first" and invite exactly what the user stopped.
    paused = _paused_refusal(device_id, capability)
    if paused:
        return None, paused
    tabs, exc = _fresh_tabs(device_id)
    if exc is not None:
        return None, _bridge_err(exc, "tabs.list")
    if tab_id is not None:
        matched = [t for t in tabs if t.get("tabId") == tab_id]
        if not matched:
            return None, _err(f"tab_id {tab_id} not found", candidates=_tab_briefs(tabs))
        tab = matched[0]
    else:
        attached = [t for t in tabs if t.get("attached")]
        if len(attached) == 1:
            tab = attached[0]
        elif not attached:
            return None, _err(
                "no tab is attached", hint="call browser_bridge_attach first", candidates=_tab_briefs(tabs)
            )
        else:
            return None, _err(
                "multiple tabs are attached; specify tab_id",
                candidates=_tab_briefs(attached),
            )
    if not tab.get("attached"):
        return None, _err(
            f"tab {tab.get('tabId')} is not attached",
            hint="call browser_bridge_attach first",
            candidates=_tab_briefs(tabs),
        )
    return tab, ""


# -- G3.3.2: `tab` as an alternative to `tab_id`, one seam ------------------
#
# Every tab-taking tool below (and in evaluate.py/dialogs.py/upload.py/
# session_powers.py) used to read `args.get("tab_id")` directly and hand it
# to `_resolve_attached_tab`. `_tab_id_arg` is the one seam that now sits in
# front of that: `tab_id` wins when present (byte-for-byte the old
# behaviour, no new work done); otherwise `tab` -- a short key like "t1" or
# an agent-chosen label like "ticket" (coveragegaps.md G3.3.1) -- is resolved
# through `tabs.py`'s `resolve_tab_selector`, which is scoped to the
# CALLING session's own `agent_session_tabs` rows ONLY. That scoping is the
# whole of this feature's IDOR protection: a session's tab keys are looked
# up by `session_id` in the SQL itself (see `state.find_session_tabs_by_key`),
# so a key collision with another session — even another session on the
# SAME device — can never resolve to the wrong tab, and a key ambiguous
# within the caller's own session is refused with the candidates listed,
# never guessed.
def _tab_id_arg(device_id: str, args: Dict[str, Any], kwargs: Dict[str, Any]) -> Tuple[Optional[int], str]:
    """Return (tab_id_or_None, error_json). error_json is "" whether or not
    a tab_id was found — None with no error means "caller named nothing",
    which every existing call site already treats as "fall back to the sole
    attached tab" (see `_resolve_attached_tab`)."""
    if args.get("tab_id") is not None:
        return args.get("tab_id"), ""
    selector = str(args.get("tab") or "").strip()
    if not selector:
        return None, ""
    session_id, err = _resolve_session(device_id, kwargs)
    if err:
        return None, err
    try:
        from . import tabs as tabs_mod  # noqa: PLC0415 - optional sibling module, G3.3
    except ImportError:
        return None, _err(
            "'tab' addressing needs hermes_plugin/tabs.py, which is not loaded on this gateway — use tab_id instead",
            code=protocol.INVALID_PARAMS,
        )
    return tabs_mod.resolve_tab_selector(device_id, session_id, selector)


def _resolve_tab_target(
    device_id: str, args: Dict[str, Any], kwargs: Dict[str, Any], capability: str = "page access"
) -> Tuple[Optional[Dict[str, Any]], str]:
    """`_resolve_attached_tab`, but resolving `tab` first via `_tab_id_arg`.
    This is the call every tab-taking tool's handler makes now instead of
    `_resolve_attached_tab(device_id, args.get("tab_id"), capability)`."""
    tab_id, err = _tab_id_arg(device_id, args, kwargs)
    if err:
        return None, err
    return _resolve_attached_tab(device_id, tab_id, capability)


def _note_tab_activity(
    device_id: str, kwargs: Dict[str, Any], tab_id: int, url: str = "", title: str = "", digest_text: str = ""
) -> None:
    """Called after a successful snapshot/read/act: clears THIS session's own
    staleness flag for the tab it just looked at (G3.3.4 — "since the agent
    last looked") and refreshes the cached url/title/digest
    `browser_bridge_tabs`/`browser_bridge_session describe` report. Never
    raises — a workspace-bookkeeping failure must not fail the underlying
    page operation that already succeeded."""
    try:
        from . import tabs as tabs_mod  # noqa: PLC0415 - optional sibling module, G3.3
    except ImportError:
        return
    session_id, err = _resolve_session(device_id, kwargs)
    if err:
        return
    tabs_mod.note_tab_activity(session_id, tab_id, url=url, title=title, digest_text=digest_text)


# -- browser_bridge_status (M0, extended in M1 with leases) ------------------

STATUS_SCHEMA = {
    "name": "browser_bridge_status",
    "description": (
        "Show the state of the Hermes Browser Bridge: paired Chrome devices and "
        "whether they are online, the tabs currently attached and who is driving "
        "them, per-origin access modes (off/request/full), pending approvals, and "
        "the relay's own health. Call this before any other browser_bridge_* tool "
        "— every other tool needs a device id and a target from here."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "include_grants": {
                "type": "boolean",
                "description": "Include the full per-origin grant list (default true).",
            }
        },
        "required": [],
    },
}


def _vision_status(cfg: Dict[str, Any]) -> Any:
    """Cheap, probe-free vision state for the status tool.

    Delegates to vision.py's own status_summary() (workstream D, §5b) when
    that module is present — it never triggers a real probe, just reads
    config + the cache table. Falls back to the raw config value so status
    still degrades gracefully if vision.py hasn't landed yet.
    """
    try:
        from . import vision  # noqa: PLC0415 - optional sibling module

        return vision.status_summary()
    except ImportError:
        return cfg.get("vision", "auto")
    except Exception:
        return cfg.get("vision", "auto")


def handle_status(args: Dict[str, Any], **_kwargs: Any) -> str:
    cfg = config.load()
    relay = relay_mod.get_relay()
    offline_after = int(cfg["device_offline_after_seconds"])
    now_ms = int(time.time() * 1000)

    connected_ids = set()
    # ASK 3: device_id -> paused, sourced from the live connection (relay.py's
    # Connection.paused, kept current by device.hello/device.heartbeat) —
    # never state.py, which has no notion of pause at all. Only meaningful
    # for a device that's actually online; an offline device reports
    # `paused: None` below rather than a stale/misleading last-known value.
    paused_by_device: Dict[str, bool] = {}
    # G0.7.3: per-device protocol_version + up-to-date flag, sourced the same
    # way `paused` already is — from the live Connection, only meaningful
    # while the device is actually online. keyed by device_id so an offline
    # device's dict.get(...) below falls through to None cleanly.
    protocol_by_device: Dict[str, str] = {}
    protocol_current_by_device: Dict[str, bool] = {}
    relay_status: Dict[str, Any] = {"listening": False, "error": "relay not started"}
    if relay is not None:
        relay_status = relay.status()
        connected_ids = {entry["device_id"] for entry in relay_status["connected"]}
        paused_by_device = {entry["device_id"]: bool(entry.get("paused")) for entry in relay_status["connected"]}
        protocol_by_device = {
            entry["device_id"]: entry.get("protocol_version", "") for entry in relay_status["connected"]
        }
        protocol_current_by_device = {
            entry["device_id"]: bool(entry.get("protocol_up_to_date")) for entry in relay_status["connected"]
        }

    devices = []
    for device in state.list_devices():
        last_seen = device.get("last_seen") or 0
        online = device["id"] in connected_ids
        devices.append(
            {
                "device_id": device["id"],
                "name": device["name"],
                "platform": device.get("platform") or "",
                "online": online,
                "last_seen_seconds_ago": int((now_ms - last_seen) / 1000) if last_seen else None,
                "stale": bool(last_seen) and (now_ms - last_seen) > offline_after * 1000,
                "paused": paused_by_device.get(device["id"]) if online else None,
                # G0.7.3: what protocol this extension build reported at its
                # last hello vs. what the gateway currently speaks — visible
                # here so a stale-but-still-connected build (accepted by the
                # G0.7.1 range check, not hard-refused) is spotted before it
                # calls a method it doesn't have, rather than after.
                "protocol_version": protocol_by_device.get(device["id"]) if online else None,
                "protocol_up_to_date": protocol_current_by_device.get(device["id"]) if online else None,
                # G0.6: what this device is currently allowed to DO (upload,
                # dialogs, evaluate, console, cookie writes, http auth,
                # downloads), so the agent can see its actual capabilities
                # up front instead of discovering each one through a refusal.
                # Fail-closed per state.get_power_policy — a device that has
                # never reported a power policy shows every capability off.
                "powers": state.get_power_policy(device["id"]),
            }
        )

    payload: Dict[str, Any] = {
        "devices": devices,
        "attached": [
            {"device_id": entry["device_id"], "tabs": entry["attached"]}
            for entry in relay_status.get("connected", [])
        ],
        "leases": attach_mod.get_registry().snapshot(),
        "relay": relay_status,
        "pending_approvals": [],  # M2
        "vision": _vision_status(cfg),
    }
    if args.get("include_grants", True):
        payload["grants"] = state.list_grants()
    if not devices:
        payload["hint"] = "No devices paired. Run `hermes browser-bridge pair` and enter the code in the extension popup."
    # G0.7.3: surface a stale-but-connected build before an unsupported-method
    # refusal does — `relay["protocol_version"]` above is the gateway's
    # ceiling; a device inside G0.7.1's supported range still shows up here
    # if it hasn't caught up to it.
    behind = [d["device_id"] for d in devices if d["online"] and d["protocol_up_to_date"] is False]
    if behind:
        payload["hint"] = (
            f"{', '.join(behind)}: extension build is behind the gateway's protocol "
            f"({relay_status.get('protocol_version')}) — reload the extension to pick up new methods."
        )

    audit.record("tool_status", devices=len(devices), online=len(connected_ids))
    return _ok(**payload)


# -- browser_bridge_attach / _release ----------------------------------------

def _lease_wait_hint(device_id: str) -> str:
    """The actionable half of a TARGET_BUSY-shaped refusal (handle_attach's
    'busy' conflicts, handle_act's/upload.py's/navigation.py's own
    AttachConflict catches): computed per-call against THIS device's actual
    effective lease (state.py's effective_lease_seconds), unlike the static
    "wait for its 60s lease to expire" wording this replaces, which quietly
    went wrong the moment a device configured a different duration — or
    unlimited, where there is no timeout to wait out at all."""
    effective = state.effective_lease_seconds(device_id)
    if effective <= 0:
        return (
            "ask the user to release it (browser_bridge_release), or wait for that session to "
            "disconnect — this device's tab lease is set to unlimited, so it never expires on its own"
        )
    return f"ask the user, or wait up to {effective}s for its lease to expire"


ATTACH_SCHEMA = {
    "name": "browser_bridge_attach",
    "description": (
        "Attach to a Chrome tab (or every tab in a tab group) so other browser_bridge_* "
        "tools can see and act in it. Resolves a friendly target — a tab_id from "
        "browser_bridge_status, a url/title substring, a group_id, or `tab` (a key/label from a "
        "PRIOR attach in THIS session, to re-attach it) — against the device's live tab list. "
        "Refused for an origin the user set to 'off', and for a tab another Hermes session is "
        "already driving (that session must release it, or its driving lease must expire — 60s by "
        "default, configurable per device, and possibly unlimited — first). "
        "Safe to call again on a tab you already hold — it just renews your lease. Give it a "
        "`tab_key` (short, memorable — e.g. 'ticket') and/or a `role` (free text — e.g. 'the "
        "helpdesk ticket') to address it later by name via `tab` on any other browser_bridge_* "
        "tool, or via browser_bridge_tabs; omitted, a key like 't1' is auto-assigned."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "tab_id": {"type": "integer", "description": "Attach this exact tab."},
            "tab": {
                "type": "string",
                "description": "Re-attach a tab you already gave a key/label to in THIS session (see tab_key below).",
            },
            "url": {"type": "string", "description": "Attach the tab whose URL contains this substring."},
            "title": {"type": "string", "description": "Attach the tab whose title contains this substring."},
            "group_id": {"type": "integer", "description": "Attach every tab in this tab group."},
            "tab_key": {
                "type": "string",
                "description": "Short, memorable key to address this tab by later (e.g. 'ticket'). Must be unique within this session; omitted, one like 't1' is auto-assigned.",
            },
            "role": {
                "type": "string",
                "description": "Free-text note on what this tab is for (e.g. 'the helpdesk ticket'), shown in browser_bridge_tabs.",
            },
            "reload_if_blocked": {
                "type": "boolean",
                "description": (
                    "Last resort when attach still fails because another extension's frame is in the page "
                    "(FOREIGN_EXTENSION_FRAME_DETECTED / a CDP_ERROR naming 'different extension'): reload the tab "
                    "and try once more. DISCARDS UNSAVED PAGE STATE -- in-progress form input, an unsubmitted "
                    "draft, anything the user typed that hasn't been saved. Default false; only set this after "
                    "warning the user their unsaved input on the page will be lost, or when you already know the "
                    "page has none."
                ),
            },
        },
        "required": [],
    },
}

RELEASE_SCHEMA = {
    "name": "browser_bridge_release",
    "description": (
        "Release a tab (or, with no tab_id/tab, every tab this session attached) so another "
        "session — or the user — can drive it. Always call this when you're done with a tab; "
        "it also happens automatically once the driving lease times out (60s by default, "
        "configurable per device in the extension's Options, and possibly unlimited — in which "
        "case only an explicit release, Stop, or detach frees the tab), but releasing explicitly "
        "is faster and clearer to the user. A released tab disappears from browser_bridge_tabs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "tab_id": {"type": "integer", "description": "Release this tab only. Omit to release everything you attached."},
            "tab": {"type": "string", "description": "Alternative to tab_id: this tab's key/label from browser_bridge_tabs."},
        },
        "required": [],
    },
}


def handle_attach(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = _resolve_device(args)
    if err:
        return err
    paused = _paused_refusal(device_id, "attach")
    if paused:
        return paused
    tabs, exc = _fresh_tabs(device_id)
    if exc is not None:
        return _bridge_err(exc, "tabs.list")

    resolve_args = args
    if args.get("tab_id") is None and str(args.get("tab") or "").strip():
        # G3.3.2: `tab` here means "re-attach a tab I already keyed/labeled
        # in THIS session" -- resolved up front, then handled exactly like
        # an explicit tab_id from here on. Needs the session early (attach
        # otherwise only resolves it further down) because the key lookup
        # is scoped to the calling session's own rows — see tabs.py's
        # module docstring for why that scoping is the whole point.
        early_session_id, err = _resolve_session(device_id, kwargs)
        if err:
            return err
        try:
            from . import tabs as tabs_mod  # noqa: PLC0415 - optional sibling module, G3.3
        except ImportError:
            return _err(
                "'tab' addressing needs hermes_plugin/tabs.py, which is not loaded on this gateway — use tab_id instead",
                code=protocol.INVALID_PARAMS,
            )
        resolved_tab_id, err = tabs_mod.resolve_tab_selector(device_id, early_session_id, str(args["tab"]).strip())
        if err:
            return err
        resolve_args = dict(args)
        resolve_args["tab_id"] = resolved_tab_id

    matched, resolve_err = _resolve_target(tabs, resolve_args)
    if not matched:
        return _err(resolve_err or "no matching tab", candidates=_tab_briefs(tabs))
    if resolve_err:
        return _err(resolve_err, candidates=_tab_briefs(matched))

    holder, err = _resolve_session(device_id, kwargs)
    if err:
        return err
    registry = attach_mod.get_registry()

    denials: Dict[int, Dict[str, str]] = {}
    for tab in matched:
        origin = tab.get("origin") or _origin_of(tab.get("url", ""))
        reason = _gate_reason(device_id, origin, "attach")
        if reason:
            denials[tab["tabId"]] = {"origin": origin, "reason": reason}
    allowed = [t for t in matched if t["tabId"] not in denials]
    if not allowed:
        return _err(
            "origin mode forbids attach for every matched tab",
            denials=denials,
            hint="ask the user to set the origin to 'full' in the extension popup",
        )

    # Every registry.attach call site uses the calling device's own effective
    # lease (state.py's effective_lease_seconds — this device's own reported
    # Options setting, else config.py's fleet-wide default), never the bare
    # LEASE_TTL_SECONDS constant.
    effective_lease = state.effective_lease_seconds(device_id)
    ttl = attach_mod.ttl_seconds_for(effective_lease)
    conflicts: Dict[int, str] = {}
    leased: List[Dict[str, Any]] = []
    for tab in allowed:
        try:
            registry.attach(device_id, tab["tabId"], holder, tab_ref=tab, ttl=ttl)
            leased.append(tab)
        except attach_mod.AttachConflict as exc2:
            conflicts[tab["tabId"]] = exc2.holder
    if not leased:
        return _err(
            "tab already in use by another session",
            conflicts=conflicts,
            hint=_lease_wait_hint(device_id),
        )

    group_id = args.get("group_id")
    wire_params: Dict[str, Any] = {"groupId": group_id} if group_id is not None else {"tabId": leased[0]["tabId"]}
    if args.get("reload_if_blocked") is True:
        wire_params["reload_if_blocked"] = True
    relay = relay_mod.get_relay()
    try:
        ext_result = relay.call(device_id, "tabs.attach", wire_params)
    except relay_mod.BridgeError as exc2:
        # Never leave a lease dangling for a tab the extension didn't actually attach.
        for tab in leased:
            registry.release(device_id, tab["tabId"], holder)
        return _bridge_err(exc2, "tabs.attach")

    attached_tabs = ext_result.get("attached") or leased
    audit.record(
        "tool_attach",
        device=device_id,
        holder=holder,
        tabs=[t.get("tabId") for t in attached_tabs],
        denied=list(denials.keys()),
        busy=list(conflicts.keys()),
    )

    # G3.3.1: give each leased tab a workspace entry (a stable key + this
    # session's own role note) so it's addressable by `tab` and shows up in
    # browser_bridge_tabs. An explicit `tab_key` only applies when exactly
    # one tab was leased (a group attach can't sensibly give every tab in
    # the group the SAME key); it never blocks the attach itself — a
    # conflicting key just falls back to an auto-assigned one rather than
    # refusing an attach that already succeeded.
    try:
        from . import tabs as tabs_mod  # noqa: PLC0415 - optional sibling module, G3.3
    except ImportError:
        tabs_mod = None  # type: ignore[assignment]
    if tabs_mod is not None:
        requested_key = str(args.get("tab_key") or "").strip()
        requested_role = str(args.get("role") or "")
        single = len(attached_tabs) == 1
        for t in attached_tabs:
            tid = t.get("tabId")
            if tid is None:
                continue
            key_for_this = requested_key if (requested_key and single) else ""
            _, key_err = tabs_mod.assign_tab_key(holder, tid, requested_key=key_for_this, role=requested_role)
            if key_err:
                tabs_mod.assign_tab_key(holder, tid, requested_key="", role=requested_role)

    payload: Dict[str, Any] = {
        "device_id": device_id,
        "attached": attached_tabs,
        "holder": holder,
        **attach_mod.lease_result_fields(effective_lease),
    }
    if denials:
        payload["denied"] = denials
    if conflicts:
        payload["busy"] = conflicts
    return _ok(**payload)


def _extension_released_count(ext_result: Dict[str, Any], fallback: int) -> int:
    """Coerce the extension's `tabs.release` reply into the integer count of
    tabs it actually detached (protocol/schema.json's `released` is declared
    an integer on both the single-tab and release-all paths — the gateway
    must report the same shape either way, not a list on one path and an int
    on the other). Tolerant of an old/malformed reply (missing key, a bool,
    a list) by falling back to ``fallback`` rather than crashing — but never
    used to inflate what the gateway *hopes* happened; callers pass the
    gateway's own optimistic count as ``fallback`` only for backward compat
    with an extension build that hasn't started returning the count yet, and
    a genuinely-observed lower number from the extension always wins.
    """
    value = ext_result.get("released")
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    return fallback


def handle_release(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = _resolve_device(args)
    if err:
        return err
    holder, err = _resolve_session(device_id, kwargs)
    if err:
        return err
    registry = attach_mod.get_registry()
    relay = relay_mod.get_relay()
    try:
        from . import tabs as tabs_mod  # noqa: PLC0415 - optional sibling module, G3.3
    except ImportError:
        tabs_mod = None  # type: ignore[assignment]
    tab_id = args.get("tab_id")
    if tab_id is None:
        selector = str(args.get("tab") or "").strip()
        if selector:
            if tabs_mod is None:
                return _err(
                    "'tab' addressing needs hermes_plugin/tabs.py, which is not loaded on this gateway — use tab_id instead",
                    code=protocol.INVALID_PARAMS,
                )
            tab_id, err = tabs_mod.resolve_tab_selector(device_id, holder, selector)
            if err:
                return err

    if tab_id is None:
        # "Release everything I attached" — drop our own lease bookkeeping
        # first (that part is unconditionally true regardless of what the
        # extension says), then ask the extension what it actually detached
        # and report THAT, not the gateway's own hopeful lease count. A
        # release that only half-worked is exactly what the caller needs to
        # see, not something to paper over with a number the gateway merely
        # wished were true.
        registry_released = registry.release_all_for_holder(device_id, holder)
        if tabs_mod is not None:
            for tid in registry_released:
                tabs_mod.forget_session_tab(holder, tid)
        try:
            ext_result = relay.call(device_id, "tabs.release", {})
        except relay_mod.BridgeError as exc:
            # The extension never confirmed anything, so it is not safe to
            # report any tabs as released even though our own bookkeeping is
            # already dropped (it will reconcile to the same truth on the
            # next heartbeat/state.report regardless).
            audit.record(
                "tool_release", device=device_id, holder=holder, tabs=registry_released,
                extension_error=exc.message,
            )
            return _ok(
                device_id=device_id,
                released=0,
                warning=(
                    f"extension release call failed ({exc.message}); the gateway dropped its own "
                    f"lease bookkeeping for {len(registry_released)} tab(s), but the extension never "
                    f"confirmed releasing them — they may still be attached in Chrome"
                ),
            )

        ext_released = _extension_released_count(ext_result, fallback=len(registry_released))
        payload: Dict[str, Any] = {"device_id": device_id, "released": ext_released}
        if ext_released < len(registry_released):
            payload["warning"] = (
                f"gateway held {len(registry_released)} lease(s) for this session, but the extension "
                f"reports releasing only {ext_released} — some tabs may still be attached in Chrome"
            )
        audit.record(
            "tool_release", device=device_id, holder=holder, tabs=registry_released,
            extension_released=ext_released,
        )
        return _ok(**payload)

    current_holder = registry.holder_of(tab_id)
    if current_holder and current_holder != holder:
        return _err(
            f"tab {tab_id} is driven by session {current_holder}, not you",
            code=protocol.TARGET_BUSY,
        )
    registry.release(device_id, tab_id, holder)
    if tabs_mod is not None:
        tabs_mod.forget_session_tab(holder, tab_id)
    try:
        ext_result = relay.call(device_id, "tabs.release", {"tabId": tab_id})
    except relay_mod.BridgeError as exc:
        return _bridge_err(exc, "tabs.release")
    released_count = _extension_released_count(ext_result, fallback=1)
    payload = {"device_id": device_id, "released": released_count}
    if released_count < 1:
        payload["warning"] = (
            f"tab {tab_id} release was requested, but the extension reports releasing 0 tabs "
            f"— it may already have been detached"
        )
    audit.record("tool_release", device=device_id, holder=holder, tab_id=tab_id, extension_released=released_count)
    return _ok(**payload)


# -- browser_bridge_snapshot --------------------------------------------------

#: speedimprovements.md B3: the gateway-side cap on `budget_bytes` (schema.json's
#: `transport.maxSnapshotBudgetBytes`, codegen'd onto both sides) — a caller-supplied
#: value above this is silently clamped, never refused, matching every other
#: clamp-not-refuse budget in this file (e.g. `wait_for_growth_ms`).
MAX_SNAPSHOT_BUDGET_BYTES = protocol.MAX_SNAPSHOT_BUDGET_BYTES

# speedimprovements.md A5: `tabs` on browser_bridge_read/_snapshot -- an
# alternative to tab/tab_id that fans out to up to this many tabs in one
# call, run CONCURRENTLY at the gateway (see `_run_multi_tab` below). Same
# "bounded, not unbounded" reasoning as MAX_ACT_STEPS/MAX_FILL_FIELDS further
# down this file.
MAX_MULTI_TAB = 5

TABS_PARAM_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "maxItems": MAX_MULTI_TAB,
    "description": (
        f"Alternative to tab/tab_id: up to {MAX_MULTI_TAB} tabs (each a tab key/label from "
        "browser_bridge_tabs, or a raw tab_id as a string) to read in ONE call, run "
        "concurrently at the gateway -- for comparing several tabs without N separate calls. "
        "One tab's failure (not found, ambiguous, refused by that origin's mode) never fails "
        "the others. When given, the result is `{results: [{tab, ok, result|error, timing}, "
        "...]}` — one entry per tab, in the order you listed them — instead of the single-tab "
        "fields this tool otherwise returns."
    ),
}

SNAPSHOT_SCHEMA = {
    "name": "browser_bridge_snapshot",
    "description": (
        "Read the attached tab as a compact accessibility-style tree: `[idx] role \"name\" "
        "[value/url]` lines, budgeted to ~4KB by default (that budget bounds the `tree` field "
        "itself; the full JSON result — url/title/device_id/tab_id plus the tree — runs "
        f"somewhat larger; `budget_bytes` is clamped to {MAX_SNAPSHOT_BUDGET_BYTES} even if you "
        "ask for more). Prefer this over a screenshot — it's far cheaper and every "
        "interactive element gets an index for later act() calls (M2): the result's "
        "`indexed_elements` count confirms how many indices are live. An idx stays valid for as "
        "long as its element is still on the page and passes act()'s own identity check — a "
        "later snapshot/find/inspect of the SAME document never invalidates it, and idx are "
        "never reused on a tab until it navigates. act() refuses (ELEMENT_MISMATCH, or 'not in "
        "the index map') only once the element is actually removed or the tab has navigated to a "
        "new document — take that refusal's hint and call browser_bridge_snapshot again rather "
        "than assuming every idx goes stale on its own. A truncated result carries "
        "a `hint` naming how to get the rest (raise budget_bytes, up to the cap above, or scope "
        "with root/dialog_only/viewport_only). "
        "Passwords/card numbers/SSNs are redacted before this ever reaches you, both in the "
        "extension and again here. Refused if the tab's current origin isn't set to 'full'. "
        "Pass `tabs` instead of tab/tab_id to snapshot several tabs at once (see `tabs`'s own "
        "description)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "tab_id": {"type": "integer", "description": "Tab to snapshot. Omit when exactly one tab is attached."},
            "tab": {"type": "string", "description": TAB_ALIAS_DESC},
            "tabs": TABS_PARAM_SCHEMA,
            "budget_bytes": {
                "type": "integer",
                "description": f"Max size of the returned tree. Default 4096, clamped to {MAX_SNAPSHOT_BUDGET_BYTES}.",
            },
            "selector": {"type": "string", "description": "CSS selector to scope the snapshot to part of the page."},
            "root": {
                "type": ["string", "integer"],
                "description": (
                    "speedimprovements.md B3: walk only this subtree instead of the whole page — an idx "
                    "from a PRIOR snapshot (resolved the same way browser_bridge_act resolves idx), or a "
                    "CSS selector (same as `selector` above; `root` wins if both are given). Refused with "
                    "SNAPSHOT_ROOT_NOT_FOUND if an idx no longer resolves."
                ),
            },
            "dialog_only": {
                "type": "boolean",
                "description": (
                    "speedimprovements.md B3: keep only the topmost open dialog's own lines. Refused with "
                    "NO_OPEN_DIALOG if no dialog is currently open."
                ),
            },
            "viewport_only": {
                "type": "boolean",
                "description": "speedimprovements.md B3: keep only lines currently within the viewport — content that needs scrolling to reach doesn't spend the budget.",
            },
            "interactive_only": {
                "type": "boolean",
                "description": (
                    "speedimprovements.md G6: keep only lines with an idx (interactive controls and "
                    "scrollable regions) plus the open dialog's own marker line, dropping headings and "
                    "plain text — for when you only need to act, not read. Combines with "
                    "root/dialog_only/viewport_only. The result's `interactive_only_bytes_saved` reports "
                    "how many bytes this saved."
                ),
            },
            "viewport": {
                "type": "object",
                "properties": {
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                },
                "description": (
                    "speedimprovements.md G2: emulate a taller/wider page for THIS call only (width "
                    "320-3840, height 240-8000), so a long list or wizard comes back in one snapshot "
                    "instead of several scroll-and-snapshot rounds. Never resizes or moves the user's "
                    "real window. Full mode only — refused with LIMITED_MODE_CAPABILITY_UNAVAILABLE "
                    "while the tab is shared in limited mode; out-of-range bounds are refused with "
                    "VIEWPORT_OUT_OF_RANGE. See the result's `viewport_used`."
                ),
            },
        },
        "required": [],
    },
}


def _parse_index_meta(raw: Any) -> Dict[int, Dict[str, str]]:
    """``indexMeta`` (idx -> {role, name}) off a `dom.snapshot`/`page.act`
    wire result, defensively: a non-dict `raw`, a non-int-parseable key, or a
    non-dict value for a given idx is skipped rather than raising — the same
    tolerance `handle_snapshot`/`handle_act` already give `indexMap` itself,
    since a malformed entry from a future/odd extension build should degrade
    that ONE idx's `expect` to "unknown" (handle_act just won't send it),
    never take down the whole snapshot/act call.
    """
    out: Dict[int, Dict[str, str]] = {}
    for raw_idx, meta in (raw or {}).items():
        try:
            idx = int(raw_idx)
        except (TypeError, ValueError):
            continue
        if isinstance(meta, dict):
            out[idx] = {"role": str(meta.get("role", "")), "name": str(meta.get("name", ""))}
    return out


def _shadow_counts(raw: Any) -> Optional[Dict[str, int]]:
    """``{open, closed}`` shadow roots the extension's walk descended into, or
    None when absent/malformed (an older extension build never sends it)."""
    if not isinstance(raw, dict):
        return None
    try:
        return {"open": int(raw.get("open") or 0), "closed": int(raw.get("closed") or 0)}
    except (TypeError, ValueError):
        return None


def _viewport_of(raw: Any) -> Optional[Dict[str, Any]]:
    """``{width, height, dpr, scrollX, scrollY}`` from a ``dom.snapshot``/
    ``page.act`` wire result's ``viewport`` field (protocol/schema.json,
    G2.5), or ``None`` when absent/malformed (an older extension build never
    sends it — the same "unknown, don't check" degrade every other optional
    wire field here uses, never a hard failure)."""
    if not isinstance(raw, dict):
        return None
    try:
        return {
            "width": int(raw["width"]),
            "height": int(raw["height"]),
            "dpr": float(raw["dpr"]),
            "scrollX": int(raw["scrollX"]),
            "scrollY": int(raw["scrollY"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _node_map_of(raw: Any) -> Dict[int, int]:
    """``nodeMap`` (idx -> CDP backendNodeId, G2.6.2) off a `dom.snapshot`/
    `page.act` wire result, defensively — same tolerance as `_parse_index_meta`:
    a non-dict `raw`, a non-int-parseable key or value is skipped rather than
    raising, so a malformed entry degrades that ONE idx's `backendNodeId` to
    absent (handle_act falls back to selector for it) rather than failing the
    whole snapshot/act call."""
    out: Dict[int, int] = {}
    for raw_idx, node_id in (raw or {}).items():
        try:
            out[int(raw_idx)] = int(node_id)
        except (TypeError, ValueError):
            continue
    return out


# -- G2.2 permission-model fix ------------------------------------------------
#
# A `dom.snapshot` result can now include content collected from frames whose
# origin differs from the top tab's — a cross-origin iframe, reached by
# extension/src/background/frames.ts's own separate injection. A `full`
# grant on the top tab's origin must not silently cover an embedded frame
# from a DIFFERENT origin (the bug this section closes: a `full` grant for
# site.example does not authorize reading an embedded bank.example widget).
#
# TWO layers, per the coordinator's report:
#   1. the extension is told which origins are granted (`_origin_policy_for_device`
#      below, sent on every `dom.snapshot` request) and must never inject
#      into, walk, or collect from an ungranted frame at all — enforced in
#      extension/src/background/frames.ts and content/walker.ts.
#   2. this gateway RE-CHECKS what actually came back (`_reconcile_frame_origins`)
#      — belt-and-braces against a stale policy snapshot, a race between grant
#      and fetch, or a buggy/misbehaving extension build that ignored (1).
#
# `FRAME_HOP` mirrors extension/src/lib/frame-selector.ts's own `FRAME_HOP`
# constant exactly — the ONE place this file (otherwise selector-opaque per
# docs/selectors.md §8) is required to recognize a piece of selector syntax,
# justified because this is a security-relevant defensive check, not general
# selector handling. `FRAME_MARKER_START`/`FRAME_MARKER_END` mirror
# `background/frames.ts`'s own constants of the same name — U+0000 is never
# legitimately part of rendered page text, so it can't collide with a page's
# own content, and these markers are ALWAYS stripped before the text reaches
# the model, whichever origin decision is made below.
FRAME_HOP = "|>>"
FRAME_MARKER_START = "\x00FRAME-START\x00"
FRAME_MARKER_END = "\x00FRAME-END\x00"


def _canonicalize_origin(origin: str) -> str:
    """Canonical form of an origin string, mirrored EXACTLY by
    `extension/src/lib/origin-policy.ts`'s `canonicalizeOrigin` — the shared
    vector file `fixtures/origin-normalization-cases.json` is what both
    sides' tests run against, so the two can't silently drift. Without this,
    two spellings of the same origin (`https://Bank.Example` vs
    `https://bank.example`, `:443` vs no port, a trailing DNS-root dot, a
    unicode IDN host vs. its punycode form) would compare unequal and a
    genuinely-granted origin could read as denied, or vice versa.

    The opaque-origin sentinel `"null"` passes through unchanged. Lowercases
    scheme and host, strips the scheme's default port, strips a trailing dot
    from the host, and IDNA-encodes a unicode host to its ASCII (punycode)
    form. A malformed/unparseable `origin` passes through unchanged too
    (never raises) — it simply won't match anything in a real grant list,
    which is safe.
    """
    if not origin or origin == "null":
        return origin
    try:
        parts = urlsplit(origin)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").rstrip(".")
        try:
            host = host.encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError):
            host = host.lower()
        port = parts.port
        default_port = {"http": 80, "https": 443}.get(scheme)
        if port is not None and port == default_port:
            port = None
        netloc = host if port is None else f"{host}:{port}"
        return f"{scheme}://{netloc}"
    except ValueError:
        return origin


def _origin_policy_for_device(device_id: str) -> Tuple[List[str], List[str]]:
    """``(granted_origins, denied_origins)`` for `device_id` — every explicit
    grants-table row (state.py), split by mode and CANONICALIZED (item 5),
    so a grant recorded with a different (but equivalent) spelling than the
    origin reported on the wire still matches. An origin with NO row is
    covered by neither list; the extension is separately told
    `default_full` (`config.py`'s `default_mode`) to decide those. Computed
    fresh on every `dom.snapshot` call (grants change rarely enough that
    this is not a hot path) rather than cached, so a grant revoked between
    calls takes effect on the very next snapshot."""
    granted: List[str] = []
    denied: List[str] = []
    for row in state.list_grants(device_id):
        origin = _canonicalize_origin(row["origin"])
        if row["mode"] == "full":
            granted.append(origin)
        elif row["mode"] in ("off", "request"):
            denied.append(origin)
    return granted, denied


def _frame_markers_balanced(tree: str) -> bool:
    """True iff every `FRAME_MARKER_START` in `tree` has a matching
    `FRAME_MARKER_END`, properly nested: an END is never seen before its
    START (or with none open at all), and nothing is left open at end of
    text. Item 2c/2d: page-forged or truncated markers (a page can set a
    literal U+0000 via script, and G2.2's extension-side fix strips that at
    the source, but this is the belt-and-braces layer for the case that
    somehow doesn't hold) make the line-oriented depth-tracking splitter
    below untrustworthy, so its caller must not attempt to parse frame
    boundaries out of malformed markers at all — drop everything instead of
    guessing which block is real."""
    depth = 0
    for line in (tree.split("\n") if tree else []):
        stripped = line.lstrip()
        if stripped.startswith(FRAME_MARKER_START):
            depth += 1
        elif stripped.startswith(FRAME_MARKER_END):
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _drop_all_frame_content(tree: str) -> str:
    """Fail-closed fallback (item 2c) for when the marker structure itself
    cannot be trusted: removes EVERY marker-delimited block wholesale
    (nesting depth clamped at 0 so a spurious extra END is a no-op rather
    than corrupting the count) and the marker lines themselves, keeping only
    lines that were never inside any block. A block left open at end of
    text has no trustworthy closing point, so everything from its START
    onward is dropped too — the clamped depth counter already achieves
    this, since depth never returns to 0 for the remainder of the text."""
    out: List[str] = []
    depth = 0
    for line in (tree.split("\n") if tree else []):
        stripped = line.lstrip()
        if stripped.startswith(FRAME_MARKER_START):
            depth += 1
            continue
        if stripped.startswith(FRAME_MARKER_END):
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            out.append(line)
    return "\n".join(out)


def _frame_prefixes_of(selector: str) -> List[str]:
    """Every ANCESTOR frame-hop prefix of `selector`, shortest first. For a
    target three frames deep, `'#a|>>#b|>>#c'`, this is `['#a', '#a|>>#b']`
    — `frames.ts` records exactly these same strings as `frame_origins`
    keys, one per frame it walked through to reach the target. Empty for a
    selector with no frame hop at all (the top document)."""
    parts = selector.split(FRAME_HOP)
    return [FRAME_HOP.join(parts[:i]) for i in range(1, len(parts))]


def _frame_prefix_of(selector: str) -> Optional[str]:
    """The full ancestor-chain prefix for `selector` — everything up to, but
    not including, its FINAL frame segment — or `None` for a selector with
    no frame hop at all (the top document). This is the key `frame_origins`
    (`attach.py`'s `set_index_map`/`frame_origins()`, populated from the
    SAME `frameOrigins` field `_frame_prefixes_of` above reads for
    snapshot's belt-and-braces check) stores the TARGET frame's own origin
    under — `_frame_prefixes_of`'s last element, when there is one."""
    prefixes = _frame_prefixes_of(selector)
    return prefixes[-1] if prefixes else None


def _resolve_act_target_origin(device_id: str, tab_id: int, tab_origin: str, selector: Optional[str]) -> Tuple[str, Optional[str]]:
    """G2.2.13: the origin `handle_act` must `_authorize` a `page.act` call
    against — the TARGET FRAME's own canonical origin for a frame-qualified
    `selector`, never the top tab's. Returns `(origin, denial_reason)`; a
    non-`None` `denial_reason` means the origin could not be established at
    ALL (an unrecognized or stale frame prefix — the extension has not
    reported this frame's origin, or it was reported under a different
    prefix) and the act must be refused before `_authorize` is even
    consulted, exactly like `_reconcile_frame_origins` treats an unrecorded
    prefix on `dom.snapshot`'s text: absent is not "assume granted", it is
    "assume denied, and say so."

    `selector` is whatever `handle_act` is about to send as `page.act`'s
    own `selector` (idx-resolved or raw) — a plain, non-frame-qualified one
    returns `(tab_origin, None)` unchanged, exactly today's behaviour.
    """
    if not selector or FRAME_HOP not in selector:
        return tab_origin, None
    prefix = _frame_prefix_of(selector)
    origins = attach_mod.get_registry().frame_origins(device_id, tab_id)
    origin = origins.get(prefix) if prefix is not None else None
    if origin is None:
        reason, _code = refusals.format_refusal("frame_origin_unrecorded", selector=selector)
        return tab_origin, reason
    return _canonicalize_origin(origin), None


def _strip_frame_markers(tree: str, granted_prefixes: "set") -> str:
    """Removes every marker-delimited block whose recorded selector-hop
    prefix is NOT in `granted_prefixes` (dropping the block's content lines
    too, including any further-nested block inside it — once a frame is
    not granted, none of its descendants are individually re-checked,
    which is the conservative and correct choice: the only way a nested
    block reached the gateway inside a not-granted one at all is a stale
    policy or a misbehaving extension, and either way nothing inside it is
    trustworthy). Keying on membership in `granted_prefixes` — rather than
    on a `denied_prefixes` set — is what makes an UNRECOGNIZED prefix (one
    `frame_origins` never mentioned at all, item 2e) fail closed exactly
    like an explicitly denied one: it is simply absent from
    `granted_prefixes` either way. ALWAYS strips the marker lines
    themselves — the model must never see a raw marker, granted or not.

    Only ever called after `_frame_markers_balanced` has confirmed the
    marker structure is well-formed — see `_reconcile_frame_origins`."""
    lines = tree.split("\n") if tree else []
    out: List[str] = []
    skip_depth = 0
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(FRAME_MARKER_START):
            payload = stripped[len(FRAME_MARKER_START):]
            _origin, _, prefix = payload.partition("\x00")
            if skip_depth > 0:
                skip_depth += 1
                continue
            if prefix not in granted_prefixes:
                skip_depth = 1
            continue
        if stripped.startswith(FRAME_MARKER_END):
            if skip_depth > 0:
                skip_depth -= 1
            continue
        if skip_depth > 0:
            continue
        out.append(line)
    return "\n".join(out)


def _frame_origin_denial_reason(origin: str, granted_set: "set", denied_set: "set") -> Optional[str]:
    """Item 4: `denied_set` is checked BEFORE `granted_set`, ALWAYS — an
    origin present in both (a stale or conflicting grants-table state, or
    any future caller that assembles the two sets some other way) is
    refused, never allowed. Returns `None` when `origin` is granted, or the
    audit reason string otherwise. A pure, directly unit-testable function
    (deliberately separate from `_origin_policy_for_device`, which cannot
    itself produce an origin present in both lists — state.py's grants
    table has one row per (device, origin), so a real conflict can only be
    exercised by constructing the sets directly, which is exactly what this
    split makes possible to test)."""
    if origin in denied_set:
        return "explicitly set to off/request for this device"
    if origin in granted_set:
        return None
    return "no explicit 'full' grant for this origin (embedded frames never inherit the tab-level default)"


def _reconcile_frame_origins(device_id: str, result: Dict[str, Any]) -> None:
    """Belt-and-braces re-check (G2.2 permission-model fix), mutating
    `result` in place before anything else in `handle_snapshot` reads
    `tree`/`indexMap` from it.

    Order of checks:
      1. (item 2c/2d) If the marker structure itself is malformed —
         unbalanced, an END with nothing open, anything left open at EOF —
         there is no trustworthy way to attribute ANY text to a frame, so
         ALL frame content is dropped and `frame_markers_malformed` is
         audited. Never guessed at.
      2. (item 3) `frame_origins` (protocol/schema.json's `dom.snapshot`
         result field) maps each included frame's own frame-hop selector
         prefix to its origin. The fail-closed rule keys on EACH indexMap
         entry's own prefix, not on the dict's presence as a whole: a
         frame-qualified entry whose prefix is missing from `frame_origins`
         entirely (an older extension build sends no field at all — same
         handling as `{}` — or a misbehaving one that forged entries `frames.ts`
         never recorded) is dropped exactly like one whose recorded origin
         is denied. `frame_origins[""]` (the top document) is never
         re-checked — it was already gated before this RPC was sent.
      3. (item 4) `denied_origins` is checked BEFORE `granted_origins` for
         each frame — an origin present in both (a stale or conflicting
         grants-table state) is refused, never allowed.

    Deliberately does NOT reuse `_gate_reason` (which the top-tab check
    above uses): `_gate_reason`/`state.get_mode` falls back to
    `config.py`'s `default_mode` ('full' by default) when an origin has no
    explicit grants-table row, which is the right behaviour for "may the
    device read the tab the user navigated to" but the WRONG behaviour for
    an embedded third-party frame — an origin the user never separately
    approved must never read as granted purely because the fleet's
    tab-level default happens to be permissive. An embedded frame is
    granted here ONLY when it has an EXPLICIT `full` row
    (`_origin_policy_for_device`'s `granted_origins`), and only when it is
    NOT also in `denied_origins`.

    ACCEPTED LIMIT (documented in docs/security.md, not fixed here): this
    re-check can only act on what the extension REPORTS. A misbehaving
    extension build that mislabels `frame_origins` or places frame content
    OUTSIDE any marker entirely is not detectable from here — the extension
    is the sole collection point, and this gateway has no independent way
    to observe the page. This catches an extension that is stale or buggy
    about reporting; it is not a defence against one that actively lies.
    """
    tree = result.get("tree", "")
    index_map = result.get("indexMap")

    if not _frame_markers_balanced(tree):
        audit.record("frame_markers_malformed", device=device_id)
        if isinstance(index_map, dict):
            dropped = [k for k, v in index_map.items() if FRAME_HOP in str(v)]
            for k in dropped:
                del index_map[k]
        result["tree"] = _drop_all_frame_content(tree)
        return

    frame_origins = result.get("frameOrigins")
    if not isinstance(frame_origins, dict):
        frame_origins = {}

    granted_origins, denied_origins = _origin_policy_for_device(device_id)
    granted_set = set(granted_origins)
    denied_set = set(denied_origins)

    # `granted_prefixes`: exactly the `frame_origins` entries whose ORIGIN is
    # (canonically) granted and NOT denied. A prefix `frame_origins` never
    # mentions at all is simply absent here too -- the SAME "not granted"
    # outcome as an explicitly denied one, which is what makes item 2e (an
    # empty or incomplete `frame_origins`) fail closed without a separate
    # code path.
    granted_prefixes: "set" = set()
    for prefix, origin in frame_origins.items():
        if prefix == "":
            continue
        origin_str = _canonicalize_origin(str(origin))
        reason = _frame_origin_denial_reason(origin_str, granted_set, denied_set)
        if reason is None:
            granted_prefixes.add(prefix)
        else:
            audit.record(
                "frame_origin_refused", device=device_id, origin=origin_str,
                selector_prefix=prefix, reason=reason,
            )

    if isinstance(index_map, dict):
        for k in list(index_map.keys()):
            selector = str(index_map[k])
            if FRAME_HOP not in selector:
                continue  # not frame-qualified at all -- the top document, always fine here
            # EVERY ancestor frame boundary this selector crosses must be
            # granted, not just its own immediate one -- an entry nested
            # inside a not-granted (or entirely unrecorded, item 2e) parent
            # frame is dropped regardless of what its own frame's origin
            # says, the same wholesale rule `_strip_frame_markers` applies
            # to text.
            if not all(p in granted_prefixes for p in _frame_prefixes_of(selector)):
                del index_map[k]

    result["tree"] = _strip_frame_markers(tree, granted_prefixes=granted_prefixes)


# -- speedimprovements.md A5: concurrent multi-tab fan-out ------------------
#
# Shared by browser_bridge_snapshot and browser_bridge_read: `tabs` fans out
# to N per-tab calls of the SAME single-tab handler (`_snapshot_one`/
# `_read_one` below), run in a small thread pool so they execute
# CONCURRENTLY rather than one after another. Every tab goes through the
# exact same `_resolve_tab_target` resolution, `_gate_reason` authorisation
# and relay/lease path a lone tab/tab_id call would -- this is a fan-out
# over the existing code path, never a second one, so an authorisation
# refusal, a stale-idx refusal, or any other per-tab behaviour is identical
# to what a single-tab call would have produced for that same tab.
#
# Concurrency safety: `timing.py`'s accumulator is `threading.local()` (see
# that module's own docstring), so each worker thread below gets its own,
# separate accumulator from `timing_mod.reset()` -- one tab's `relay.call()`
# timings can never bleed into another tab's entry, or into the OUTER
# call's own timing (built by `_wrap_handler_with_timing` in the caller's
# thread, which never itself touches `relay.call()` on the multi-tab path).
# `relay.Relay.call()` itself (hermes_plugin/relay.py) is already safe to
# call from several threads at once -- it hands off to the relay's own
# asyncio loop via `run_coroutine_threadsafe` and blocks only the CALLING
# thread on its own future, so concurrent tabs really do run concurrently
# at the wire, not just at the Python level.
def _multi_tab_entries(
    tabs_arg: Any, base_args: Dict[str, Any]
) -> Tuple[List[Tuple[str, Dict[str, Any]]], str]:
    if not isinstance(tabs_arg, list) or not tabs_arg:
        return [], _err("'tabs' must be a non-empty array of tab keys or tab_ids", code=protocol.INVALID_PARAMS)
    if len(tabs_arg) > MAX_MULTI_TAB:
        return [], _err(f"'tabs' accepts at most {MAX_MULTI_TAB} tabs per call", code=protocol.INVALID_PARAMS)
    entries: List[Tuple[str, Dict[str, Any]]] = []
    seen: set = set()
    for raw in tabs_arg:
        label = str(raw).strip()
        if not label:
            return [], _err("'tabs' entries must not be blank", code=protocol.INVALID_PARAMS)
        if label.lower() in seen:
            return [], _err(f"'tabs' lists {label!r} more than once", code=protocol.INVALID_PARAMS)
        seen.add(label.lower())
        per_tab_args = dict(base_args)
        if label.lstrip("-").isdigit():
            # Mirrors tabs.py's handle_tabs(action='set') selector parsing --
            # an all-digit entry is a raw tab_id, never looked up as a key.
            per_tab_args["tab_id"] = int(label)
            per_tab_args.pop("tab", None)
        else:
            per_tab_args["tab"] = label
            per_tab_args.pop("tab_id", None)
        entries.append((label, per_tab_args))
    return entries, ""


def _run_multi_tab(
    device_id: str,
    args: Dict[str, Any],
    kwargs: Dict[str, Any],
    tabs_arg: Any,
    single_fn: Callable[[str, Dict[str, Any], Dict[str, Any]], str],
    capability: str,
) -> str:
    base_args = {k: v for k, v in args.items() if k not in ("tab", "tab_id", "tabs")}
    entries, err = _multi_tab_entries(tabs_arg, base_args)
    if err:
        return err

    def _worker(label: str, per_tab_args: Dict[str, Any]) -> Dict[str, Any]:
        timing_mod.reset()  # fresh, thread-local accumulator -- this tab only
        start = time.monotonic()
        try:
            raw = single_fn(device_id, per_tab_args, kwargs)
        except Exception as exc:  # a handler must not raise, but one bad tab must never sink the pool
            raw = _err(f"internal error handling tab {label!r}: {exc}")
        tab_timing = timing_mod.build_tool_timing((time.monotonic() - start) * 1000.0)
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = {"success": False, "error": "malformed handler result"}
        ok = bool(parsed.pop("success", False))
        entry: Dict[str, Any] = {"tab": label, "ok": ok}
        if ok:
            entry["result"] = parsed
        else:
            entry["error"] = parsed.pop("error", "unknown error")
            if parsed:
                entry["details"] = parsed
        entry["timing"] = tab_timing
        return entry

    # `max_workers=len(entries)` (bounded by MAX_MULTI_TAB above): every tab
    # gets its own thread so none waits behind another for the pool itself.
    with ThreadPoolExecutor(max_workers=len(entries)) as pool:
        futures = [pool.submit(_worker, label, per_tab_args) for label, per_tab_args in entries]
        results = [f.result() for f in futures]

    ok_count = sum(1 for r in results if r["ok"])
    audit.record(
        "tool_multi_tab", capability=capability, device=device_id,
        tabs=len(entries), ok=ok_count, failed=len(entries) - ok_count,
    )
    return _ok(results=results)


def _resolve_root_selector(device_id: str, tab_id: int, root: Any) -> Tuple[Optional[str], Optional[Tuple[str, int]]]:
    """Resolves a `root` param (an idx, or a literal CSS selector) into a
    wire `selector` — the SAME rule `handle_snapshot`'s own `root` handling
    always used, factored out so `handle_act`'s `snapshot_after.root`
    (speedimprovements.md A4 follow-up) can resolve its OWN root the
    identical way without a second, potentially-drifting implementation. An
    idx is resolved through the SAME index map `browser_bridge_act`'s `idx`
    targeting already uses; a non-numeric `root` is a literal CSS selector,
    forwarded as-is. Returns `(selector, None)` on success, `(None, (message,
    code))` on failure — the caller shapes that into its own error format
    (`_err`/`_err_dict` differ in return type between `handle_snapshot` and
    `handle_act`)."""
    root_idx: Optional[int] = None
    if isinstance(root, int) and not isinstance(root, bool):
        root_idx = root
    elif isinstance(root, str) and root.strip().lstrip("-").isdigit():
        root_idx = int(root)
    if root_idx is not None:
        resolved = attach_mod.get_registry().resolve_index(device_id, tab_id, root_idx)
        if resolved is None:
            return None, (f"root idx {root_idx} is not in tab {tab_id}'s current index map", protocol.SNAPSHOT_ROOT_NOT_FOUND)
        return resolved, None
    return str(root), None


def handle_snapshot(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = _resolve_device(args)
    if err:
        return err
    tabs_arg = args.get("tabs")
    if tabs_arg is not None:
        return _run_multi_tab(device_id, args, kwargs, tabs_arg, _snapshot_one, "snapshot")
    return _snapshot_one(device_id, args, kwargs)


def _snapshot_one(device_id: str, args: Dict[str, Any], kwargs: Dict[str, Any]) -> str:
    tab, err = _resolve_tab_target(device_id, args, kwargs, "snapshot")
    if err:
        return err
    tab_id = tab["tabId"]
    origin = tab.get("origin") or _origin_of(tab.get("url", ""))
    denial = _gate_reason(device_id, origin, "snapshot")
    if denial:
        return _err(denial, code=protocol.GRANT_DENIED, device_id=device_id, tab_id=tab_id, origin=origin)

    requested_budget = int(args.get("budget_bytes") or 4096)
    wire_params: Dict[str, Any] = {"tabId": tab_id, "budget_bytes": max(1, min(requested_budget, MAX_SNAPSHOT_BUDGET_BYTES))}
    if args.get("selector"):
        wire_params["selector"] = args["selector"]
    # speedimprovements.md B3: `root` wins over a plain `selector` when both
    # are given (its own description says so) -- an idx is resolved through
    # the SAME index map browser_bridge_act's `idx` targeting already uses,
    # so a root scoped to a control found by a PRIOR snapshot/find behaves
    # exactly like acting on that idx would: refused, not guessed at, once
    # the map no longer has it. A non-numeric `root` is a literal CSS
    # selector, forwarded exactly like `selector` above.
    root = args.get("root")
    if root is not None:
        resolved_root, root_err = _resolve_root_selector(device_id, tab_id, root)
        if root_err:
            message, code = root_err
            return _err(message, code=code, device_id=device_id, tab_id=tab_id)
        wire_params["selector"] = resolved_root
    if args.get("dialog_only"):
        wire_params["dialog_only"] = True
    if args.get("viewport_only"):
        wire_params["viewport_only"] = True
    if args.get("interactive_only"):
        wire_params["interactive_only"] = True
    # speedimprovements.md G2: validated only for shape here (numeric width/
    # height) -- the extension itself is the source of truth for the
    # 320-3840/240-8000 bounds and the full-mode-only rule (VIEWPORT_OUT_OF_RANGE/
    # LIMITED_MODE_CAPABILITY_UNAVAILABLE come back as ordinary bridge errors
    # via `_bridge_err` below, ), so this never duplicates that validation.
    if args.get("viewport") is not None:
        vp = args["viewport"]
        if not isinstance(vp, dict) or not isinstance(vp.get("width"), (int, float)) or not isinstance(vp.get("height"), (int, float)):
            return _err("viewport must be an object with numeric width and height", code=protocol.INVALID_PARAMS)
        wire_params["viewport"] = {"width": int(vp["width"]), "height": int(vp["height"])}
    # speedimprovements.md B4 (stable element refs): the tab's current
    # idx->selector map, so the extension's walk can reuse idx for elements
    # it re-discovers instead of renumbering the whole page every call — see
    # background/frames.ts's `mergeIndex` and attach.py's `current_index_map`/
    # `index_map_max_idx` docstrings. `start_index` (the same field B1's
    # `browser_bridge_find` already sends) is the floor a genuinely new
    # element mints above, so it can never collide with anything this map
    # already holds.
    registry = attach_mod.get_registry()
    existing_map = registry.current_index_map(device_id, tab_id)
    if existing_map:
        wire_params["existing_index_map"] = {str(k): v for k, v in existing_map.items()}
    # B4 idx-reuse fix: high_water_idx (a monotonic mark that survives a
    # live-map element being removed), never index_map_max_idx (the CURRENT
    # live map's own max, which can go backwards and reissue a stale idx) —
    # see attach.py's high_water_idx()/index_map_max_idx() docstrings.
    wire_params["start_index"] = registry.high_water_idx(device_id, tab_id) + 1
    # G2.2 permission-model fix: the extension must never inject into, walk,
    # or otherwise collect content from a cross-origin (or same-origin-
    # accessible-but-differently-origined) frame unless ITS OWN origin is
    # granted — a `full` grant on the top tab's origin does not cover an
    # embedded frame from a different origin (docs: bank.example widget
    # inside a granted site.example page). This is the allowlist the
    # extension checks locally BEFORE fetching anything (data minimization);
    # `_reconcile_frame_origins` below is the belt-and-braces re-check of
    # what actually came back.
    granted_origins, denied_origins = _origin_policy_for_device(device_id)
    wire_params["granted_origins"] = granted_origins
    wire_params["denied_origins"] = denied_origins
    # Deliberately NEVER `config.load()["default_mode"] == "full"`: that
    # default answers "may the user's own device read the tab they
    # navigated to", a page-level browsing consent this plugin's own
    # `_gate_reason` already applies to the TOP tab (and only the top tab).
    # An EMBEDDED cross-origin frame is not "the tab the user is looking
    # at" — it is third-party content pulled in by that page — and must
    # never inherit that ambient default; only an EXPLICIT `full` grant on
    # the frame's OWN origin (`granted_origins` above) authorizes reading
    # it. This is the field this whole fix exists to get right: sending
    # `true` here would silently re-open the exact hole being closed (an
    # ungranted-but-never-explicitly-denied third-party origin reads as
    # granted purely because the fleet's tab-level default happens to be
    # 'full', which it is by default — see config.py).
    wire_params["default_full"] = False

    relay = relay_mod.get_relay()
    try:
        result = relay.call(device_id, "dom.snapshot", wire_params)
    except relay_mod.BridgeError as exc:
        return _bridge_err(exc, "dom.snapshot")

    _reconcile_frame_origins(device_id, result)

    # speedimprovements.md B3: dialog_only with no open dialog is a refusal,
    # not an (empty-looking, misleading) ordinary tree -- checked BEFORE any
    # of the index-map bookkeeping below runs, same as every other early-out
    # in this function.
    if args.get("dialog_only") and result.get("dialogMissing"):
        return _err(
            f"no open dialog on tab {tab_id}",
            code=protocol.NO_OPEN_DIALOG, device_id=device_id, tab_id=tab_id,
        )

    tree, gateway_hits = _gateway_redact(result.get("tree", ""), device_id)
    if gateway_hits:
        audit.record(
            "redaction_gateway_catch",
            device=device_id, tab_id=tab_id, origin=origin, hits=gateway_hits, capability="snapshot",
        )
    total_redactions = int(result.get("redactions") or 0) + gateway_hits

    # Persist idx -> CSS selector for M2's act(idx) to resolve later. This is
    # a wholesale replace (attach.AttachRegistry.set_index_map), keyed on
    # (device_id, tab_id): a stale map from a previous page/snapshot of this
    # same tab must never answer a later resolve_index() call. Only the
    # COUNT goes back to the model — the selectors themselves are long and
    # unnecessary; the `[idx]` markers already in `tree` are what the model
    # acts on.
    index_map: Dict[int, str] = {}
    for raw_idx, selector in (result.get("indexMap") or {}).items():
        try:
            index_map[int(raw_idx)] = str(selector)
        except (TypeError, ValueError):
            continue
    # idx -> {role, name} from the SAME wire result (protocol/schema.json's
    # `indexMeta`, the ELEMENT_MISMATCH fix) — stored alongside index_map so
    # handle_act can later populate page.act's `expect` param and let the
    # extension refuse an action whose live element no longer matches what
    # this very snapshot described. See attach.py's set_index_map()/
    # resolve_index_meta() docstrings for the full contract.
    element_meta = _parse_index_meta(result.get("indexMeta"))
    # G2.5.1: the viewport this very snapshot was captured against, so a
    # later xy-targeted act can be refused (VIEWPORT_MISMATCH) instead of
    # silently clicking wherever the page has since resized/scrolled to.
    # `_viewport_of` degrades to None on a malformed/absent field (an older
    # extension build) — see set_index_map()'s docstring for why that's safe.
    viewport = _viewport_of(result.get("viewport"))
    # G2.6.2: idx -> backendNodeId, resolved by the extension's background
    # worker (not the content script) from this same wire result. Absent for
    # any idx it couldn't resolve a handle for — see _node_map_of and
    # set_index_map()'s docstring; never a hard failure.
    node_map = _node_map_of(result.get("nodeMap"))
    # G2.2.13: the SAME frame_origins _reconcile_frame_origins just verified
    # above (a dict mapping each included frame's own frame-hop prefix to its
    # canonical origin) — stored so a LATER page.act on a frame-qualified
    # idx/selector can `_authorize` against the right origin without a fresh
    # extension round trip. Raw, not filtered to only the granted prefixes:
    # identity (which origin a structural prefix names) and the grant
    # decision (is that origin currently authorized) are different questions,
    # and the grant is re-checked fresh at act time regardless of what it was
    # at snapshot time.
    raw_frame_origins = result.get("frameOrigins")
    frame_origins = {str(k): str(v) for k, v in raw_frame_origins.items()} if isinstance(raw_frame_origins, dict) else {}
    attach_mod.get_registry().set_index_map(
        device_id, tab_id, index_map, url=result.get("url") or tab.get("url", ""),
        element_meta=element_meta, viewport=viewport, node_map=node_map,
        frame_origins=frame_origins,
    )
    # G2.6.2 cost bound: how many index-map entries the extension's bounded
    # bulk resolution pass dropped for time/entry-cap reasons (never an
    # ordinary per-selector miss) — audited, not surfaced to the model, so
    # "why did this idx have no handle" stays answerable from the audit log
    # without adding noise to every ordinary snapshot response.
    node_map_skipped = result.get("nodeMapSkipped")
    try:
        node_map_skipped = int(node_map_skipped) if node_map_skipped is not None else 0
    except (TypeError, ValueError):
        node_map_skipped = 0

    audit.record(
        "tool_snapshot", device=device_id, tab_id=tab_id, origin=origin,
        truncated=bool(result.get("truncated")), redactions=total_redactions,
        indexed_elements=len(index_map),
        node_handles_resolved=len(node_map), node_handles_skipped=node_map_skipped,
    )
    extra: Dict[str, Any] = {}
    shadow = _shadow_counts(result.get("shadow"))
    if shadow is not None:
        extra["shadow"] = shadow
    if viewport is not None:
        extra["viewport"] = viewport
    # speedimprovements.md G2: present only when `viewport` was requested AND
    # the extension actually applied the override (an older extension build,
    # or an apply failure the extension itself absorbed, simply omits it —
    # never a hard failure, same "absent means unknown" degrade every other
    # optional wire field here follows).
    viewport_used = result.get("viewportUsed")
    if isinstance(viewport_used, dict) and isinstance(viewport_used.get("width"), (int, float)) and isinstance(viewport_used.get("height"), (int, float)):
        extra["viewport_used"] = {"width": int(viewport_used["width"]), "height": int(viewport_used["height"])}
    # speedimprovements.md B3: only present when the extension actually
    # truncated -- see walker.ts's `hint` doc comment for exactly what it
    # names (raise budget_bytes up to MAX_SNAPSHOT_BUDGET_BYTES, or scope
    # with root/dialog_only/viewport_only).
    hint = result.get("hint")
    if isinstance(hint, str) and hint:
        extra["hint"] = hint
    # speedimprovements.md G6: only present when `interactive_only` was
    # requested -- see walker.ts's `interactiveOnlyBytesSaved` doc comment
    # for exactly what it measures.
    if args.get("interactive_only"):
        try:
            bytes_saved = int(result.get("interactiveOnlyBytesSaved") or 0)
        except (TypeError, ValueError):
            bytes_saved = 0
        extra["interactive_only_bytes_saved"] = bytes_saved
    result_url = result.get("url") or tab.get("url", "")
    result_title = result.get("title") or tab.get("title", "")
    _note_tab_activity(device_id, kwargs, tab_id, url=result_url, title=result_title, digest_text=tree)
    return _ok(
        device_id=device_id,
        tab_id=tab_id,
        url=result_url,
        title=result_title,
        tree=tree,
        truncated=bool(result.get("truncated", False)),
        redactions=total_redactions,
        indexed_elements=len(index_map),
        **extra,
    )


# -- browser_bridge_find (speedimprovements.md B1) ---------------------------

FIND_DEFAULT_LIMIT = 10
FIND_MAX_LIMIT = 50

FIND_SCHEMA = {
    "name": "browser_bridge_find",
    "description": (
        "Search the WHOLE attached tab -- every frame browser_bridge_snapshot may include (same "
        "per-frame origin gating) plus open and closed shadow roots -- for text/role, instead of "
        "paging through several snapshots raising budget_bytes. Matches accessible name, visible "
        "text, label, placeholder, (non-password/sensitive) field value, title, "
        "aria-describedby-referenced tooltip text, alt and aria-label, case-insensitively, ranked "
        "exact before prefix before substring before a small synonym table (sign in/log in/login; "
        "sign out/log out/logout; delete/remove/trash; settings/preferences/options/configuration; "
        "search/find; next/continue/proceed; back/previous; close/dismiss/cancel; add/create/new; "
        "save/apply/submit) -- and, within a tier, visible before clipped inside a scroll container "
        "before hidden-but-in-DOM. A query ending in a role word (button/link/checkbox/field/input/"
        "dropdown/menu/tab/row) filters by that role and matches the rest, e.g. 'save button' finds "
        "a button named like 'save' -- same as passing `role` explicitly, which wins if both are "
        "given. Each match is a snapshot-style line (`[idx] role \"name\" state (location, matched "
        "via X)`) with an idx immediately usable by browser_bridge_act: matches are MERGED into the "
        "tab's existing index map, never replacing it, so idx from your last snapshot stay valid. "
        "Redacted the same way snapshot is. Refused if the tab's current origin isn't set to 'full'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "tab_id": {"type": "integer", "description": "Tab to search. Omit when exactly one tab is attached."},
            "tab": {"type": "string", "description": TAB_ALIAS_DESC},
            "query": {
                "type": "string",
                "description": (
                    "Text to search for, matched case-insensitively. A trailing role word (button/"
                    "link/checkbox/field/input/dropdown/menu/tab/row) is stripped and used as the role "
                    "filter when `role` isn't given explicitly -- e.g. 'settings menu' searches for "
                    "'settings' restricted to role 'menu'."
                ),
            },
            "role": {
                "type": "string",
                "description": "Exact (case-insensitive) role filter, e.g. 'button' or 'textbox' — or 'text'/'heading' for a plain-content match. Wins over a role word parsed from `query`.",
            },
            "limit": {
                "type": "integer",
                "description": f"Max matches to return. Default {FIND_DEFAULT_LIMIT}, capped at {FIND_MAX_LIMIT}.",
            },
        },
        "required": ["query"],
    },
}

# speedimprovements.md G4: "a query ending in button/link/checkbox/field/
# input/dropdown/menu/tab/row filters by role and matches the rest" — maps
# each spoken role word to the exact (lower-case) role string
# walker.ts's `roleForElement`/explicit `role="…"` would print for it, so the
# stripped query can be forwarded as an ordinary `find.role` filter with no
# extension-side change needed.
FIND_ROLE_WORDS: Dict[str, str] = {
    "button": "button",
    "link": "link",
    "checkbox": "checkbox",
    "field": "textbox",
    "input": "textbox",
    "dropdown": "combobox",
    "menu": "menu",
    "tab": "tab",
    "row": "row",
}


def _strip_role_word(query: str) -> Tuple[str, Optional[str]]:
    """G4: 'save button' -> ('save', 'button'). Only strips when there is a
    non-empty remainder after the role word -- a bare 'button' alone is left
    as a literal search term rather than becoming an unbounded role filter
    with an empty query."""
    parts = query.rsplit(None, 1)
    if len(parts) != 2:
        return query, None
    rest, last = parts
    role = FIND_ROLE_WORDS.get(last.strip().lower())
    if role and rest.strip():
        return rest.strip(), role
    return query, None


def _redact_find_match(m: Dict[str, Any], device_id: str) -> Tuple[Dict[str, Any], int]:
    """Applies `_gateway_redact` to every string field a `findMatches` entry
    carries (`name`/`extra`/`role`/`location` — walker.ts's FindMatch has no
    separate value/text field; matched text lives in `name`, and a leaked
    value shows up in `extra`, e.g. `value="…"`). This is the SAME
    belt-and-braces re-check `handle_snapshot`'s `tree` and
    `inspect_tool._redact_deep` give their own text fields — FIND_SCHEMA
    promises "redacted the same way snapshot is", so a match's printed line
    must never carry raw text `handle_snapshot` would have caught. Returns a
    NEW dict (idx/matchTier/visTier/order untouched) plus the hit count."""
    total = 0
    out = dict(m)
    for key in ("name", "extra", "role", "location"):
        val = out.get(key)
        if isinstance(val, str) and val:
            redacted, hits = _gateway_redact(val, device_id)
            out[key] = redacted
            total += hits
    return out, total


def _format_find_match(m: Dict[str, Any]) -> str:
    """One `findMatches` entry (idx/role/name/extra/location/matchedVia,
    already ranked and sliced to `limit` by `handle_find`) as a
    snapshot-style printed line — the exact `[idx] role "name"` shape an
    ordinary browser_bridge_snapshot line has, plus the trailing state text
    (`value="…"`/`checked`/`expanded`/`disabled`, walker.ts's `extra`) and,
    in parentheses, `location` and (speedimprovements.md G4) `matchedVia` —
    e.g. `(in viewport, matched via title)` — so the model can tell it found
    this control by its tooltip/synonym rather than its printed name."""
    idx = m.get("idx")
    role = m.get("role", "")
    name = m.get("name", "")
    extra = str(m.get("extra") or "").strip()
    location = str(m.get("location") or "").strip()
    matched_via = str(m.get("matchedVia") or "").strip()
    line = f'[{idx}] {role} "{name}"'
    if extra:
        line += f" {extra}"
    parens = [p for p in (location, f"matched via {matched_via}" if matched_via else "") if p]
    if parens:
        line += f" ({', '.join(parens)})"
    return line


def _find_sort_key(m: Dict[str, Any]) -> Tuple[int, int, int]:
    """Global ranking across every frame's own (unranked, frame-traversal-
    order) matches: exact(0)/prefix(1)/substring(2)/synonym(3, speedimprovements.md
    G4) `matchTier` first, then visible(0)/clipped(1)/hidden(2) `visTier`,
    then the walk's own stable `order` — a malformed entry (an older/buggy
    extension build) sorts last rather than raising."""
    try:
        return (int(m.get("matchTier", 2)), int(m.get("visTier", 2)), int(m.get("order", 0)))
    except (TypeError, ValueError):
        return (2, 2, 0)


def handle_find(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = _resolve_device(args)
    if err:
        return err
    tab, err = _resolve_tab_target(device_id, args, kwargs, "find")
    if err:
        return err
    tab_id = tab["tabId"]
    origin = tab.get("origin") or _origin_of(tab.get("url", ""))
    # speedimprovements.md B1: "the capability and gating are the same as
    # snapshot (it's a read of the same data; `full` required)".
    denial = _gate_reason(device_id, origin, "snapshot")
    if denial:
        return _err(denial, code=protocol.GRANT_DENIED, device_id=device_id, tab_id=tab_id, origin=origin)

    query = str(args.get("query") or "").strip()
    if not query:
        return _err("query is required", code=protocol.INVALID_PARAMS)
    try:
        limit = int(args.get("limit") or FIND_DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = FIND_DEFAULT_LIMIT
    limit = max(1, min(limit, FIND_MAX_LIMIT))
    role = args.get("role")
    # speedimprovements.md G4: an explicit `role` arg always wins; only when
    # the caller left it out do we parse a trailing role word off the raw
    # query text itself.
    if not role:
        stripped_query, parsed_role = _strip_role_word(query)
        if parsed_role:
            query = stripped_query
            role = parsed_role

    registry = attach_mod.get_registry()
    # speedimprovements.md B1/B4: mint every fresh idx past this tab's
    # monotonic high-water mark (NOT the current live map's own max, which
    # can go backwards if the element holding the highest idx was removed —
    # the B4 idx-reuse defect), so merging (below) never invalidates OR
    # collides with an idx a prior browser_bridge_snapshot/act/find/inspect
    # already handed the model — see attach.py's
    # high_water_idx()/merge_index_map() docstrings.
    start_index = registry.high_water_idx(device_id, tab_id) + 1

    wire_params: Dict[str, Any] = {
        "tabId": tab_id,
        # Large enough that the tree this same walk also produces is never
        # truncated by a budget find doesn't care about at all (only
        # `findMatches` is read below) -- see MAX_SNAPSHOT_BUDGET_BYTES.
        "budget_bytes": MAX_SNAPSHOT_BUDGET_BYTES,
        "find": {"query": query, **({"role": str(role)} if role else {})},
        "start_index": start_index,
    }
    # speedimprovements.md B4 (stable element refs): find's own walk reuses
    # idx for an element a prior snapshot/act/inspect already indexed,
    # instead of always minting a fresh one past start_index — the same
    # "find, inspect and snapshot agree on idx" contract B4 exists for.
    existing_map = registry.current_index_map(device_id, tab_id)
    if existing_map:
        wire_params["existing_index_map"] = {str(k): v for k, v in existing_map.items()}
    granted_origins, denied_origins = _origin_policy_for_device(device_id)
    wire_params["granted_origins"] = granted_origins
    wire_params["denied_origins"] = denied_origins
    wire_params["default_full"] = False

    relay = relay_mod.get_relay()
    try:
        result = relay.call(device_id, "dom.snapshot", wire_params)
    except relay_mod.BridgeError as exc:
        return _bridge_err(exc, "dom.snapshot")

    _reconcile_frame_origins(device_id, result)

    raw_matches = result.get("findMatches")
    matches_in: List[Dict[str, Any]] = [m for m in raw_matches if isinstance(m, dict)] if isinstance(raw_matches, list) else []
    ranked = sorted(matches_in, key=_find_sort_key)[:limit]

    wire_index_map = result.get("indexMap") if isinstance(result.get("indexMap"), dict) else {}
    wire_index_meta = result.get("indexMeta") if isinstance(result.get("indexMeta"), dict) else {}
    index_map: Dict[int, str] = {}
    element_meta: Dict[int, Dict[str, str]] = {}
    lines: List[str] = []
    gateway_hits = 0
    for m in ranked:
        try:
            idx = int(m.get("idx"))
        except (TypeError, ValueError):
            continue
        selector = wire_index_map.get(str(idx))
        if selector is not None:
            index_map[idx] = str(selector)
        meta = wire_index_meta.get(str(idx))
        if isinstance(meta, dict):
            element_meta[idx] = {"role": str(meta.get("role", "")), "name": str(meta.get("name", ""))}
        # Gateway redaction re-check (belt-and-braces, same as handle_snapshot's
        # `tree`): applied to a COPY used only for the printed line — the
        # index_map/element_meta above stay the raw wire values, exactly like
        # handle_snapshot never redacts the selectors/meta it persists, only
        # the tree text it hands back to the model. Each field is redacted
        # individually FIRST (catches a card/ssn/email/phone/aws hit confined
        # to one field), then the fully assembled line is redacted a SECOND
        # time (catches the password/CVV/OTP heuristic, which needs the
        # printed `"name" ... "value"` shape — two quoted spans on one
        # line — that per-field redaction alone can't see since name/extra
        # are checked apart from each other). `_gateway_redact` is idempotent
        # on anything already caught (`_line_already_redacted` skips a line
        # already carrying `[REDACTED]`), so this never double-counts a hit.
        redacted_m, hits = _redact_find_match(m, device_id)
        gateway_hits += hits
        line, line_hits = _gateway_redact(_format_find_match(redacted_m), device_id)
        gateway_hits += line_hits
        lines.append(line)

    if gateway_hits:
        audit.record(
            "redaction_gateway_catch",
            device=device_id, tab_id=tab_id, origin=origin, hits=gateway_hits, capability="find",
        )

    # speedimprovements.md B1: MERGE, never replace — idx from this tab's
    # last browser_bridge_snapshot/act stay resolvable (attach.py's own
    # docstring has the full contract).
    registry.merge_index_map(device_id, tab_id, index_map, element_meta)

    audit.record(
        "tool_find", device=device_id, tab_id=tab_id, origin=origin,
        query=query, role=role, match_count=len(lines), total_candidates=len(matches_in),
        redactions=gateway_hits,
    )
    result_url = result.get("url") or tab.get("url", "")
    result_title = result.get("title") or tab.get("title", "")
    _note_tab_activity(device_id, kwargs, tab_id, url=result_url, title=result_title, digest_text="\n".join(lines))
    return _ok(
        device_id=device_id,
        tab_id=tab_id,
        url=result_url,
        matches=lines,
        match_count=len(lines),
        redactions=gateway_hits,
    )


# -- browser_bridge_read -------------------------------------------------------

READ_SCHEMA = {
    "name": "browser_bridge_read",
    "description": (
        "Extract clean text or markdown content from the attached tab (optionally scoped to a "
        "CSS selector) — for reading an article, a ticket body, a table, etc. Use "
        "browser_bridge_snapshot instead when you'll need element indices to click/type "
        "afterward (M2). Redacted the same way snapshot is. Refused if the tab's current "
        "origin isn't set to 'full'. Pass `tabs` instead of tab/tab_id to read several tabs at "
        "once (see `tabs`'s own description)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "tab_id": {"type": "integer", "description": "Tab to read. Omit when exactly one tab is attached."},
            "tab": {"type": "string", "description": TAB_ALIAS_DESC},
            "tabs": TABS_PARAM_SCHEMA,
            "selector": {"type": "string", "description": "CSS selector to scope extraction to part of the page."},
            "format": {"type": "string", "enum": ["text", "markdown"], "description": "Output format. Default markdown."},
        },
        "required": [],
    },
}


def handle_read(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = _resolve_device(args)
    if err:
        return err
    tabs_arg = args.get("tabs")
    if tabs_arg is not None:
        return _run_multi_tab(device_id, args, kwargs, tabs_arg, _read_one, "read")
    return _read_one(device_id, args, kwargs)


def _read_one(device_id: str, args: Dict[str, Any], kwargs: Dict[str, Any]) -> str:
    tab, err = _resolve_tab_target(device_id, args, kwargs, "read")
    if err:
        return err
    tab_id = tab["tabId"]
    origin = tab.get("origin") or _origin_of(tab.get("url", ""))
    denial = _gate_reason(device_id, origin, "read")
    if denial:
        return _err(denial, code=protocol.GRANT_DENIED, device_id=device_id, tab_id=tab_id, origin=origin)

    wire_params: Dict[str, Any] = {"tabId": tab_id, "format": args.get("format", "markdown")}
    if args.get("selector"):
        wire_params["selector"] = args["selector"]

    relay = relay_mod.get_relay()
    try:
        result = relay.call(device_id, "page.read", wire_params)
    except relay_mod.BridgeError as exc:
        return _bridge_err(exc, "page.read")

    content, gateway_hits = _gateway_redact(result.get("content", ""), device_id)
    if gateway_hits:
        audit.record(
            "redaction_gateway_catch",
            device=device_id, tab_id=tab_id, origin=origin, hits=gateway_hits, capability="read",
        )

    audit.record(
        "tool_read", device=device_id, tab_id=tab_id, origin=origin,
        truncated=bool(result.get("truncated")), redactions=gateway_hits,
    )
    _note_tab_activity(device_id, kwargs, tab_id, url=tab.get("url", ""), title=tab.get("title", ""), digest_text=content)
    return _ok(
        device_id=device_id,
        tab_id=tab_id,
        url=tab.get("url", ""),
        content=content,
        truncated=bool(result.get("truncated", False)),
        redactions=gateway_hits,
    )


# -- browser_bridge_act (M2) --------------------------------------------------

ACT_ACTIONS = (
    "click", "type", "select", "submit", "scroll", "key", "navigate", "wait_for", "hover", "drag",
    # speedimprovements.md A2: many fields, one wire call -- see `fields`'s
    # own ACT_SCHEMA entry and `_validate_act_args`'s "fill" branch below.
    "fill",
    # Limited-mode share fix: mode-agnostic -- chrome.tabs.goBack/goForward/
    # reload, never chrome.debugger -- so these work identically whether the
    # tab is attached full or limited.
    "back", "forward", "reload",
)
# Actions that need *something* to act on (an element or a point) — the ones
# that don't (scroll with no target scrolls the page; wait_for's own check
# below is more specific than "has a target") are left out deliberately.
# hover needs a target for the same reason click does: there is nothing to
# dwell over otherwise. drag needs a SOURCE target (checked here) and,
# separately, a DESTINATION target (checked in _validate_act_args below,
# since none of the existing idx/selector/xy fields cover it).
_ACT_REQUIRES_TARGET = ("click", "type", "select", "submit", "hover", "drag")
DEFAULT_ACT_TIMEOUT_MS = 10000
DEFAULT_HOVER_MS = 250
MAX_HOVER_MS = 5000
DRAG_MODES = ("auto", "pointer", "html5")
SCROLL_TO_VALUES = ("top", "bottom", "next_page")
DEFAULT_WAIT_FOR_GROWTH_MS = 1500
MAX_WAIT_FOR_GROWTH_MS = 10000
# speedimprovements.md A1: browser_bridge_act's `steps` batch, capped so one
# call can't turn into an unbounded, unreviewable sequence of actions.
MAX_ACT_STEPS = 20
# speedimprovements.md G1: a `steps` entry is either a mutating action
# (`{"action": ...}`, unchanged) or a read-only step (`{"tool": ...}`), run
# through the SAME standalone handler `browser_bridge_snapshot`/
# `_screenshot`/`_find`/`_read`/`_inspect` uses -- see `_run_read_only_step`.
READ_ONLY_STEP_TOOLS = ("snapshot", "screenshot", "find", "read", "inspect")
# speedimprovements.md G1: a batch's `content` can carry at most this many
# embedded screenshot images (the same multimodal envelope
# `browser_bridge_screenshot` itself returns for a `pixels`-fidelity capture,
# see `vision._pixels_multimodal_result`) -- an unbounded batch of image
# embeds is a cost/context blowup a single `browser_bridge_screenshot` call
# never risks. Enforced up front in `_handle_act_steps`'s validation pass: a
# batch requesting more than this many `tool: "screenshot"` steps is refused
# before anything runs, never silently truncated mid-batch.
MAX_STEP_SCREENSHOTS = 2
# speedimprovements.md A2: `fill`'s `fields`, same reasoning/cap as steps.
MAX_FILL_FIELDS = 20
# C2: wait_for's structured `condition` -- its own timeout budget, independent
# of the call's overall timeout_ms (which is raised automatically to cover it
# -- see handle_act's condition marshalling below).
WAIT_FOR_CONDITION_TYPES = (
    "text_appears", "text_gone", "element_visible", "element_gone", "url_matches", "network_idle",
)
DEFAULT_WAIT_FOR_CONDITION_MS = 5000
MAX_WAIT_FOR_CONDITION_MS = 30000

ACT_SCHEMA = {
    "name": "browser_bridge_act",
    "description": (
        "Click, type, select, submit, scroll, press a key, navigate, or wait in the attached tab — "
        "the only browser_bridge_* tool that changes anything. Targeting hierarchy, best to worst: "
        "an `idx` from any browser_bridge_snapshot/find/inspect/act of this tab's CURRENT document "
        "(idx are never reused on a tab until it navigates, and a later snapshot/find/inspect MERGES "
        "new indices in rather than invalidating the ones you already have) — an idx is refused "
        "(ELEMENT_MISMATCH, or 'not in the index map') only once its element is actually removed "
        "from the page or the tab has navigated to a new document, never merely because time has "
        "passed or another snapshot happened in between — an idx is already backed "
        "by the most durable selector the snapshot could find for it (an id, a data-testid/name/"
        "aria-label/role, or only as a last resort a positional path), so prefer it over writing "
        "your own `selector` when you have one; a raw CSS `selector`, durable when you can write one "
        "yourself (an attribute that identifies the element, not a brittle `nth-of-type` chain you "
        "guessed from the DOM); or pixel `xy`, ONLY for a target with no accessible element at all "
        "(a canvas, a map) and only for the exact snapshot/screenshot that produced it — see `xy`'s "
        "own description for what makes it unsafe otherwise. The result's `diff` field — not a bare "
        "success flag — is the evidence your action worked: read it before deciding what to do "
        "next, the same way you'd look at the screen after a real click. Trust `diff` (capped at 30 "
        "lines, with a count of what's not shown) instead of re-snapshotting after every step — it "
        "already carries what appeared, disappeared or changed, with fresh indices. For click/hover, "
        "`hit` names the element that actually received the event and whether it matched what you "
        "aimed at; a mismatch shows up here and in `diff`'s own notes, not as a silent wrong click. "
        "`snapshot_after: true` folds a full-page snapshot into this same call when you need more "
        "than `diff`/`hit` give you, reusing the walk this call already does rather than a second "
        "browser_bridge_snapshot round trip. Only one Hermes session "
        "drives a tab at a time; if another session is driving this one, this call is refused "
        "naming that session rather than interleaving actions with it. An origin in 'request' mode "
        "parks this call for the user's approval instead of running it immediately — expect it to "
        "take longer, and expect an occasional 'denied' or 'timed out' rather than a result. For "
        "action=type targeting a selector/idx, the result also carries `fieldValue`: what actually "
        "landed in the field (or a contenteditable's text), read back and redacted after the fact — "
        "a password field's value is never returned, and its absence isn't itself evidence of failure. "
        "If the "
        "result carries a `dialogs` field, one or more JS dialogs (alert/confirm/prompt/beforeunload) "
        "opened while this action ran — in a headful tab these are NOT auto-dismissed and sit on the "
        "user's screen (and every subsequent browser_bridge_* call on that tab will time out while one "
        "is open, since Runtime.evaluate cannot run while a dialog is up); use browser_bridge_dialog, "
        "passing that dialog's `id` and its exact `message` as `ack_message`, to resolve it. "
        "Working across more than one tab (G3.3): call browser_bridge_tabs to orient — it lists "
        "every tab you hold, each by its key/role, url/title, and whether it changed (navigated, or "
        "resized) since you last looked. Address the one you mean with `tab:\"ticket\"` (its key or "
        "role) instead of memorizing a numeric tab_id, then re-snapshot before acting if that tab "
        "is flagged changed (navigated) — a navigation is what actually resets idx numbering and "
        "makes the whole prior index map unresolvable; a mere resize does not invalidate idx at "
        "all, though it can move where an `xy` target now lands (VIEWPORT_MISMATCH)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "tab_id": {"type": "integer", "description": "Tab to act in. Omit when exactly one tab is attached."},
            "tab": {"type": "string", "description": TAB_ALIAS_DESC},
            "action": {
                "type": "string",
                "enum": list(ACT_ACTIONS),
                "description": (
                    "click/type/select/submit need idx, selector, or xy. type needs text; select needs "
                    "EITHER text (matches the option's value or its exact visible text) OR option_text "
                    "(speedimprovements.md D5 -- matches the option's VISIBLE text, case-insensitive, "
                    "a trimmed exact match then a unique prefix; on failure the result names up to 20 "
                    "of the select's own options). key needs text as the key name (e.g. 'Enter', "
                    "'Tab'). navigate needs url. "
                    "wait_for needs idx, selector, or text (whichever it should wait to appear), OR "
                    "a structured `condition` (see its own description) for a richer wait -- "
                    "text_appears/text_gone, element_visible/element_gone, url_matches, or "
                    "network_idle. A timed-out condition is not an error: the result's `wait_for` "
                    "field reports met:false so you can decide what to do next. "
                    "scroll works with or without a target. hover needs idx, selector, or xy, and "
                    "dwells at that point for hover_ms (default 250, cap 5000) before the result's "
                    "diff is taken, so a menu that opens on a delay shows up in it. Verified against "
                    "a hover-menu fixture: the revealed menu stays open through your very next act "
                    "call as long as that call's own target (idx/selector/xy) is inside the menu — "
                    "click the revealed item directly, no need to approach it gradually. Acting "
                    "anywhere else first (a different element, a scroll, another tab) closes the menu "
                    "before that call runs, same as moving the mouse away would. drag needs a SOURCE "
                    "(idx/selector/xy, same fields as every other action) AND, separately, a "
                    "DESTINATION (to_idx/to_selector/to_xy) — a real mouse press-move-release "
                    "sequence, which drives native HTML5 drag-and-drop directly on modern Chrome; "
                    "see `mode`'s own description for what it does and does not cover. Slider "
                    "CAPTCHAs and other bot-detection challenges are permanently out of scope for "
                    "this tool — it will not be extended to help defeat them, and a drag aimed at one "
                    "should not be expected to work."
                ),
            },
            "idx": {
                "type": "integer",
                "description": (
                    "Element index from any browser_bridge_snapshot/find/inspect/act of this tab's "
                    "CURRENT document (every successful act also adopts a freshly-walked index map for "
                    "the page as it is AFTER that action, merged into what the tab already had; a failed "
                    "after-walk invalidates the map outright instead of leaving a stale one in place). "
                    "Resolved to a CSS selector here "
                    "before the extension ever sees it — the extension never receives raw indices. That "
                    "selector prefers site semantics over position: an id, then a data-testid/data-test/"
                    "data-cy, then a form control's name, then aria-label, then an explicit role, and "
                    "only when none of those apply, a positional nth-of-type path — so an idx from a "
                    "page with any of those attributes usually survives a sibling being inserted or "
                    "removed nearby, where a hand-written positional selector would not. An idx from "
                    "an OLDER snapshot/act/find/inspect of this SAME document is still valid — indices "
                    "are never reused on a tab until it navigates, and a later snapshot/find/inspect "
                    "only ever adds to the map, never replaces it out from under an idx you already "
                    "hold. An idx not in the tab's current map at all (it was captured on a page the "
                    "tab has since navigated away from) is refused with a hint to re-snapshot, not "
                    "silently guessed at. Beyond that navigation-level check, the resolved selector's "
                    "live element is also compared, right before acting, against the role/name the "
                    "snapshot recorded for it — a same-page re-render that shifts what a selector "
                    "matches (e.g. a table row "
                    "deleted, sliding a sibling into its place) is refused with ELEMENT_MISMATCH instead "
                    "of silently acting on the wrong element, even though the URL never changed. Treat "
                    "`diff` as the evidence of what an action actually did either way."
                ),
            },
            "selector": {
                "type": "string",
                "description": (
                    "CSS selector, when you already know it instead of an idx. Prefer one anchored to "
                    "site semantics — an id, `[data-testid=...]`, a form control's `[name=...]`, "
                    "`[aria-label=...]`, or `[role=...]` — over a positional path like "
                    "`div:nth-of-type(3)>button:nth-of-type(2)`, which breaks the moment a sibling is "
                    "inserted or removed anywhere before it. When in doubt, take idx from a fresh "
                    "snapshot instead of writing a selector by hand — it already picks the most durable "
                    "one available for that element."
                ),
            },
            "xy": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
                "description": (
                    "[x, y] pixel coordinates, for targets with no accessible element (canvas, map, "
                    "etc) — the LAST resort, never the default: idx/selector is always the safer "
                    "target, because it re-resolves the element's live position at act time, so it "
                    "survives a resize, scroll, or re-render that moves the pixel these coordinates "
                    "meant. `xy` is valid ONLY for the exact browser_bridge_snapshot or "
                    "browser_bridge_screenshot call that produced it — a call in between (even one "
                    "you made yourself) can invalidate it. Given both idx/selector and xy, idx/selector "
                    "is always tried first and xy is never even considered. If the tab's viewport has "
                    "changed since that snapshot/screenshot (its size, pixel ratio, or scroll position), "
                    "an xy-targeted action is refused with VIEWPORT_MISMATCH rather than silently "
                    "clicking wherever the page has since moved to."
                ),
            },
            "to_idx": {
                "type": "integer",
                "description": (
                    "drag only: the DESTINATION, as an idx from the same recent snapshot/act idx/"
                    "selector/xy above targets the SOURCE. Same staleness rules as idx: refused if the "
                    "index map has gone stale, and the live destination is compared against the role/"
                    "name the snapshot recorded for it right before the drop, refusing with "
                    "ELEMENT_MISMATCH on a mismatch (e.g. a reordered kanban column) rather than "
                    "dropping onto the wrong place."
                ),
            },
            "to_selector": {
                "type": "string",
                "description": "drag only: destination CSS selector, when you already know it instead of a to_idx.",
            },
            "to_xy": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
                "description": (
                    "drag only: destination [x, y] pixel coordinates, for a drop target with no "
                    "accessible element (a canvas, a map). Same staleness rule as `xy`: valid only for "
                    "the exact snapshot/screenshot that produced it, and refused with VIEWPORT_MISMATCH "
                    "if the viewport has changed by the time the drop is about to happen — checked "
                    "twice, once before the drag starts moving and again immediately before the drop, "
                    "so a resize partway through the drag is still caught."
                ),
            },
            "mode": {
                "type": "string",
                "enum": list(DRAG_MODES),
                "description": (
                    "drag only: 'pointer' drives a real mouse press/move/release sequence, which "
                    "(modern Chrome) drives native HTML5 drag-and-drop directly — this is what 'auto' "
                    "(the default) uses today. 'html5' (a separate CDP drag-interception mechanism) is "
                    "not implemented and is refused outright; retry with 'pointer' or omit `mode`. "
                    "Slider CAPTCHAs and similar bot-detection challenges are out of scope regardless "
                    "of mode — this tool will not be extended to help defeat them."
                ),
            },
            "text": {
                "type": "string",
                "description": (
                    "Text to type/select, or the key name for action=key. For type: a field inside a "
                    "same-origin iframe is NOT reachable yet (blocked on iframe support landing "
                    "separately) -- idx/selector resolution only searches the top document and open/"
                    "closed shadow roots, so an iframe field's selector is refused as not found, not "
                    "silently mistyped elsewhere."
                ),
            },
            "option_text": {
                "type": "string",
                "description": (
                    "select only (speedimprovements.md D5): match the option by its VISIBLE text "
                    "instead of `text` (which matches the option's value OR exact visible text) -- "
                    "case-insensitive, a trimmed exact match first then a unique prefix, reusing "
                    "`fill`'s own select matcher rather than a second one. On no match or an "
                    "ambiguous prefix match, the call fails naming up to 20 of the select's own "
                    "option texts, so you can see what was actually there instead of guessing again."
                ),
            },
            "snapshot_after": {
                "oneOf": [
                    {"type": "boolean"},
                    {
                        "type": "object",
                        "properties": {
                            "root": {"description": "idx (from a prior snapshot/act/find/inspect) or a literal CSS selector -- same resolution rule as browser_bridge_snapshot's own `root`."},
                            "dialog_only": {"type": "boolean"},
                            "viewport_only": {"type": "boolean"},
                        },
                    },
                ],
                "description": (
                    "Any action (speedimprovements.md A4): true returns a snapshot of the page in "
                    "this SAME call, as the result's `snapshot_after` ({tree, url, title} -- exactly "
                    "browser_bridge_snapshot's own fields) -- reusing the after-action walk this call "
                    "already does for `diff`, no separate browser_bridge_snapshot round trip needed "
                    "for the common act-then-look loop. Prefer trusting `diff`/`hit` first (see their "
                    "own descriptions) and reach for this only when you actually need the fuller page "
                    "text a capped diff can't carry. An object form (A4 follow-up) scopes it exactly "
                    "like browser_bridge_snapshot's own `root`/`dialog_only`/`viewport_only` -- a "
                    "SEPARATE, extra content-script request, not the same walk `diff` is built from, "
                    "so `changed`/`diff` keep comparing the whole page regardless of how `snapshot_after` "
                    "is scoped. `dialog_only` with no open dialog omits `snapshot_after` entirely rather "
                    "than refusing the whole action."
                ),
            },
            "screenshot_after": {
                "oneOf": [
                    {"type": "boolean"},
                    {
                        "type": "object",
                        "properties": {
                            "scale": {"type": "number", "description": "0.25-1, default 1 — same meaning as browser_bridge_screenshot's own `scale`."},
                            "region": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "width": {"type": "integer"},
                                    "height": {"type": "integer"},
                                },
                                "description": "Viewport-relative CSS-px rectangle — same meaning as browser_bridge_screenshot's own `region`.",
                            },
                            "format": {"type": "string", "enum": ["png", "jpeg"]},
                        },
                    },
                ],
                "description": (
                    "Any action (speedimprovements.md G5): true (or an object to scope/tune it) "
                    "captures a screenshot of the tab in this SAME call, after the action has "
                    "settled — reusing browser_bridge_screenshot's own capture path (format/scale/ "
                    "quality defaults, presence-overlay hiding) rather than a separate round trip. "
                    "The image comes back attached to this call's result the same way "
                    "browser_bridge_screenshot attaches one — at most one image per call, and if "
                    "the active model can't take an attached image, it degrades the same way "
                    "browser_bridge_screenshot does."
                ),
            },
            "url": {"type": "string", "description": "Destination URL for action=navigate."},
            "timeout_ms": {
                "type": "integer",
                "description": f"Max time to wait for the action (and, for wait_for, the condition). Default {DEFAULT_ACT_TIMEOUT_MS}.",
            },
            "hover_ms": {
                "type": "integer",
                "description": (
                    f"hover only: how long to dwell at the target before the diff is taken. "
                    f"Default {DEFAULT_HOVER_MS}, capped at {MAX_HOVER_MS} — an out-of-range value is "
                    "clamped, not refused, and the clamp is noted in the result's diff."
                ),
            },
            "type_mode": {
                "type": "string",
                "enum": ["replace", "append", "prepend"],
                "description": (
                    "type only: how `text` combines with the field's current content. 'replace' "
                    "(default) selects-all and deletes first — today's only behaviour if you omit "
                    "this. 'append' moves the caret to the end first; 'prepend' moves it to the "
                    "start. A disabled or read-only field is refused outright (FIELD_NOT_EDITABLE) "
                    "before anything is dispatched, in every mode."
                ),
            },
            "press_enter": {
                "type": "boolean",
                "description": (
                    "type only: press Enter after the text lands. Omit this and the historical rule "
                    "applies instead — Enter iff `text` itself ends in a newline. Set it explicitly "
                    "(true or false) when you need that decoupled from the text's own trailing character."
                ),
            },
            "dispatch": {
                "type": "string",
                "enum": ["insert_text", "keys"],
                "description": (
                    "type only: 'insert_text' (default) is one fast call for the whole string, "
                    "invisible to a keydown/keypress listener. 'keys' sends one real key event per "
                    "character instead — use it for autocomplete comboboxes, masked/formatted inputs, "
                    "and rich-text editors that only react to per-character input; verified against "
                    "fields.html's masked-input and combobox rows."
                ),
            },
            "key_delay_ms": {
                "type": "integer",
                "description": "type only, dispatch='keys': delay between characters in milliseconds. Default 20.",
            },
            "to": {
                "type": "string",
                "enum": list(SCROLL_TO_VALUES),
                "description": (
                    "scroll only: a named target instead of a raw xy delta. 'next_page' moves one "
                    "viewport/container-height step and is the default scroll shape when you give "
                    "neither `to` nor `xy`. 'top'/'bottom' jump to the scrolled element's own extremes "
                    "-- use 'bottom' repeatedly (checking the result's scroll.at_bottom/content_grew each "
                    "time) to page through an infinite-scroll feed. Given an idx/selector target too, "
                    "this scrolls THAT element (or its nearest scrollable ancestor) directly -- never "
                    "scrollIntoView -- which is the intended way to page an infinite-scroll container "
                    "by idx/selector rather than a raw xy point. An idx/selector with NEITHER `to` NOR "
                    "`xy` keeps the historical behaviour: scrollIntoView, nothing more."
                ),
            },
            "wait_for_growth_ms": {
                "type": "integer",
                "description": (
                    f"scroll only: after scrolling, wait up to this long (default {DEFAULT_WAIT_FOR_GROWTH_MS}, "
                    f"clamped 0-{MAX_WAIT_FOR_GROWTH_MS}) for new content to appear -- the scrolled "
                    "element's height or element count growing (an infinite-scroll feed loading more "
                    "rows). Returns as soon as growth is seen and the page settles, not always the full "
                    "budget. The result's `scroll` field (present on every successful scroll) reports "
                    "target ('page' or 'container'), before/after/max scroll position, moved, at_top, "
                    "at_bottom (true only once nothing grew AND the position is at the end), "
                    "content_grew, height_before/height_after, and waited_ms -- read it instead of "
                    "guessing from `diff` whether a scroll moved anything, reached the end, or loaded "
                    "more content."
                ),
            },
            "fields": {
                "type": "array",
                "maxItems": MAX_FILL_FIELDS,
                "items": {
                    "type": "object",
                    "properties": {
                        "idx": {"type": "integer"},
                        "selector": {"type": "string"},
                        "value": {
                            "description": (
                                "A string for a text input/textarea/contenteditable or a <select> "
                                "(matched against the option's visible text, case-insensitive, a trimmed "
                                "exact match first then a unique prefix; ambiguous or no match fails and "
                                "lists up to 10 of the select's own options), or a boolean for a "
                                "checkbox/radio (the desired checked state -- clicked only when it "
                                "differs from the current state, never an unconditional toggle)."
                            ),
                        },
                    },
                    "required": ["value"],
                },
                "description": (
                    f"action 'fill' only: many fields, one call -- the most common multi-call pattern "
                    f"(typing several inputs, picking a select, ticking a checkbox) collapsed into one "
                    f"round trip. Each field needs idx OR selector (same targeting rules as the top-level "
                    f"idx/selector above) plus value. Filled in order and stopped at the FIRST failing "
                    f"field -- the result names which one and why, never fills the rest and hides the "
                    f"failure. At most {MAX_FILL_FIELDS} fields. Never echoes a field's own value back, "
                    f"unlike `type`'s fieldValue readback -- read the result's `diff` for what visibly "
                    f"changed instead."
                ),
            },
            "steps": {
                "type": "array",
                "maxItems": MAX_ACT_STEPS,
                "items": {
                    "type": "object",
                    "description": (
                        "Either an ACTION step -- same shape as this tool's own top-level parameters "
                        "(action, idx/selector/xy, text, url, and so on) -- everything above except "
                        "device_id/tab_id/tab/steps itself, which apply to the whole batch, not to one "
                        "step -- OR a READ-ONLY step: `{tool: \"snapshot\"|\"screenshot\"|\"find\"|\"read\"|"
                        "\"inspect\", ...that tool's own params}` (speedimprovements.md G1), same params "
                        "browser_bridge_snapshot/_screenshot/_find/_read/_inspect themselves take, minus "
                        "device_id/tab_id/tab (also batch-level). Give exactly one of `action` or `tool` "
                        "per step; `wait_for` is always an `action`, never a `tool`."
                    ),
                },
                "description": (
                    f"Run up to {MAX_ACT_STEPS} steps in ONE call instead of one browser_bridge_act call "
                    "per action -- the biggest lever for fewer round trips (speedimprovements.md A1, "
                    "extended by G1 to mix in reads): fill a form, click Next, wait_for the next field, "
                    "snapshot the result, all in one turn. Steps run sequentially and STOP at the first "
                    "failure -- the result is `{steps: [per-step result], completed: n, failed_at?: i}` "
                    "plus the last successful step's diff/url/title, so you can tell exactly how far the "
                    "batch got. Every ACTION step goes through the same authorization, approval, frame, "
                    "and lease checks as a standalone act call -- including a fresh re-check of the "
                    "step's own target origin, since an earlier step may have navigated the tab. If any "
                    "ACTION step's target would need a live approval prompt that isn't already covered by "
                    "a standing grant, the WHOLE batch is refused before any step runs, named which step "
                    "and why -- an agent cannot bury a prompt partway through a batch and have the rest "
                    "run unattended. Every READ-ONLY step (`tool: ...`) runs through that same tool's own "
                    "standalone gate/redaction/idx-registration -- read-only steps never trigger a live "
                    "approval prompt at all (they refuse outright instead, same as calling that tool on "
                    "its own would), so they never take part in that preflight. An idx from a snapshot "
                    "taken before the batch started can be invalidated by an earlier step (a re-render, a "
                    "navigation); the same ELEMENT_MISMATCH/stale-idx refusals a standalone act gives "
                    "apply per step, named with that step's index -- a read-only step's own fresh idx map "
                    "is what the NEXT action step should target. At most "
                    f"{MAX_STEP_SCREENSHOTS} `tool:\"screenshot\"` steps are allowed per batch (a batch "
                    "requesting more is refused up front, INVALID_STEP_KIND) -- their embedded images (if "
                    "any) come back attached to the whole batch result, the same way a standalone "
                    "browser_bridge_screenshot attaches one. Given `steps`, every other targeting "
                    "parameter on this call (action/idx/selector/xy/text/...) is ignored -- put them "
                    "inside each step instead. Example: click Next, then wait for the next screen, then "
                    "snapshot just its dialog: `{steps: [{action: \"click\", idx: 12}, {action: "
                    "\"wait_for\", text: \"Customize settings\"}, {tool: \"snapshot\", dialog_only: "
                    "true}]}`."
                ),
            },
            "condition": {
                "type": "object",
                "description": (
                    "wait_for only: a structured condition, richer than plain idx/selector/text above "
                    "(which keep working unchanged when this is omitted). `type` is exactly one of "
                    "'text_appears'/'text_gone' (with `text`: substring, case-insensitive, over visible "
                    "page text), 'element_visible'/'element_gone' (with `idx` or `selector`), "
                    "'url_matches' (with `pattern`: a plain substring or a /regex/ against the tab's "
                    "current URL), or 'network_idle' (no other field -- waits for no in-flight network "
                    "requests for 500ms; in limited/no-debugger share mode this isn't observable and "
                    "falls back to a DOM-quiet wait instead, noted in the result). `timeout_ms` here "
                    f"(default {DEFAULT_WAIT_FOR_CONDITION_MS}, clamped 0-{MAX_WAIT_FOR_CONDITION_MS}) is "
                    "this condition's OWN budget, independent of the call's own `timeout_ms` (which is "
                    "raised automatically to cover it). A condition that times out is not an error -- "
                    "the result's `wait_for` field reports {met, waited_ms, condition}, the same shape "
                    "whether it was met or not."
                ),
                "properties": {
                    "type": {"type": "string", "enum": list(WAIT_FOR_CONDITION_TYPES)},
                    "text": {"type": "string"},
                    "idx": {"type": "integer"},
                    "selector": {"type": "string"},
                    "pattern": {"type": "string"},
                    "timeout_ms": {"type": "integer", "default": DEFAULT_WAIT_FOR_CONDITION_MS},
                },
            },
        },
        "required": ["action"],
    },
}


def _validate_act_args(action: str, args: Dict[str, Any]) -> Optional[str]:
    """Local shape-check before anything is sent to the extension.

    Catches the obvious "this action can never succeed as called" cases with
    an actionable message instead of spending a round trip (and, in 'request'
    mode, an approval prompt to the user) on a call that was never going to
    work.
    """
    idx = args.get("idx")
    selector = args.get("selector")
    xy = args.get("xy")
    text = args.get("text")
    url = args.get("url")
    has_target = idx is not None or bool(selector) or xy is not None

    if xy is not None and (not isinstance(xy, (list, tuple)) or len(xy) != 2):
        return "xy must be a [x, y] pair of integers"
    if action in _ACT_REQUIRES_TARGET and not has_target:
        return f"action {action!r} needs idx, selector, or xy to say what to act on"
    if action == "type" and not text:
        return "action 'type' needs text"
    # D5: select needs EITHER the legacy `text` (value-or-exact-text) OR the
    # new `option_text` (visible-text, fuzzier matcher) -- never neither.
    option_text = args.get("option_text")
    if action == "select" and not text and not option_text:
        return "action 'select' needs text or option_text (the option to select)"
    if option_text is not None and not isinstance(option_text, str):
        return "option_text must be a string"
    if action == "key" and not text:
        return "action 'key' needs text (the key name, e.g. 'Enter')"
    if action == "navigate" and not url:
        return "action 'navigate' needs url"
    if action == "wait_for":
        condition = args.get("condition")
        if condition is not None:
            if not isinstance(condition, dict):
                return "condition must be an object"
            ctype = condition.get("type")
            if ctype not in WAIT_FOR_CONDITION_TYPES:
                return f"condition.type must be one of {list(WAIT_FOR_CONDITION_TYPES)}"
            if ctype in ("text_appears", "text_gone") and not condition.get("text"):
                return f"condition.type {ctype!r} needs condition.text"
            if ctype in ("element_visible", "element_gone") and condition.get("idx") is None and not condition.get("selector"):
                return f"condition.type {ctype!r} needs condition.idx or condition.selector"
            if ctype == "url_matches" and not condition.get("pattern"):
                return "condition.type 'url_matches' needs condition.pattern"
            c_timeout = condition.get("timeout_ms")
            if c_timeout is not None and not isinstance(c_timeout, (int, float)):
                return "condition.timeout_ms must be a number of milliseconds"
        elif not (idx is not None or selector or text):
            return "action 'wait_for' needs idx, selector, or text (or a structured condition) to say what to wait for"
    if action == "hover":
        hover_ms = args.get("hover_ms")
        if hover_ms is not None and not isinstance(hover_ms, (int, float)):
            return "hover_ms must be a number of milliseconds"
    if action == "drag":
        to_idx = args.get("to_idx")
        to_selector = args.get("to_selector")
        to_xy = args.get("to_xy")
        if to_xy is not None and (not isinstance(to_xy, (list, tuple)) or len(to_xy) != 2):
            return "to_xy must be a [x, y] pair of integers"
        if to_idx is None and not to_selector and to_xy is None:
            return "action 'drag' needs a destination: to_idx, to_selector, or to_xy"
        mode = args.get("mode")
        if mode is not None and mode not in DRAG_MODES:
            return f"mode must be one of {list(DRAG_MODES)}"
    if action == "type":
        type_mode = args.get("type_mode")
        if type_mode is not None and type_mode not in ("replace", "append", "prepend"):
            return "type_mode must be one of 'replace', 'append', 'prepend'"
        press_enter = args.get("press_enter")
        if press_enter is not None and not isinstance(press_enter, bool):
            return "press_enter must be true or false"
        dispatch = args.get("dispatch")
        if dispatch is not None and dispatch not in ("insert_text", "keys"):
            return "dispatch must be one of 'insert_text', 'keys'"
        key_delay_ms = args.get("key_delay_ms")
        if key_delay_ms is not None and not isinstance(key_delay_ms, (int, float)):
            return "key_delay_ms must be a number of milliseconds"
    if action == "scroll":
        to = args.get("to")
        if to is not None and to not in SCROLL_TO_VALUES:
            return f"to must be one of {list(SCROLL_TO_VALUES)}"
        wait_for_growth_ms = args.get("wait_for_growth_ms")
        if wait_for_growth_ms is not None and not isinstance(wait_for_growth_ms, (int, float)):
            return "wait_for_growth_ms must be a number of milliseconds"
    if action == "fill":
        fields = args.get("fields")
        if not isinstance(fields, list) or not fields:
            return "action 'fill' needs a non-empty fields array"
        if len(fields) > MAX_FILL_FIELDS:
            return f"fill fields must contain at most {MAX_FILL_FIELDS} entries"
        for i, field in enumerate(fields):
            if not isinstance(field, dict):
                return f"fields[{i}] must be an object"
            if field.get("idx") is None and not field.get("selector"):
                return f"fields[{i}] needs idx or selector"
            if "value" not in field:
                return f"fields[{i}] needs value"
            if not isinstance(field.get("value"), (str, bool)):
                return f"fields[{i}].value must be a string or boolean"
    return None


def _validate_read_step_args(tool: str, args: Dict[str, Any]) -> Optional[str]:
    """Local shape-check for a `steps` read-only entry (speedimprovements.md
    G1), before anything runs — the same "a batch that was never going to
    fully succeed refuses before it starts anything" rule
    `_validate_act_args` gives action steps. Deliberately reuses each tool's
    OWN validators where one already exists (`inspect_tool._validate_params`,
    `vision._validate_region`/`_validate_scale`/`_validate_quality`) rather
    than re-implementing them, so the two can never silently drift apart;
    the standalone handler itself still re-validates when it actually runs
    (this is a pre-flight, not a replacement for that check).
    """
    if tool == "snapshot":
        budget = args.get("budget_bytes")
        if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int)):
            return "budget_bytes must be an integer"
        for flag in ("dialog_only", "viewport_only"):
            if args.get(flag) is not None and not isinstance(args[flag], bool):
                return f"{flag} must be true or false"
        if args.get("selector") is not None and not isinstance(args["selector"], str):
            return "selector must be a string"
        return None
    if tool == "find":
        if not str(args.get("query") or "").strip():
            return "query is required"
        return None
    if tool == "read":
        fmt = args.get("format")
        if fmt is not None and fmt not in ("text", "markdown"):
            return "format must be one of 'text', 'markdown'"
        return None
    if tool == "inspect":
        try:
            from . import inspect_tool  # noqa: PLC0415 - optional sibling module, see docstring
        except ImportError:
            return "browser_bridge_inspect is not available on this build"
        question = str(args.get("question") or "")
        return inspect_tool._validate_params(question, args)
    if tool == "screenshot":
        try:
            from . import vision  # noqa: PLC0415 - optional sibling module, see docstring
        except ImportError:
            return "browser_bridge_screenshot is not available on this build"
        if args.get("region") is not None:
            _region, message = vision._validate_region(args["region"])
            if message:
                return message
        if args.get("idx") is not None and (isinstance(args["idx"], bool) or not isinstance(args["idx"], int)):
            return f"idx must be an integer (got {args['idx']!r})"
        _scale, message = vision._validate_scale(args.get("scale"))
        if message:
            return message
        _quality, message = vision._validate_quality(args.get("quality"))
        if message:
            return message
        fmt = args.get("format")
        if fmt is not None and fmt not in vision._VALID_FORMATS:
            return f"format must be one of {vision._VALID_FORMATS} (got {fmt!r})"
        return None
    return f"unsupported tool {tool!r}"


def _run_read_only_step(
    tool: str,
    step_args: Dict[str, Any],
    batch_args: Dict[str, Any],
    kwargs: Dict[str, Any],
    screenshots_remaining: int,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Run ONE `steps` read-only entry through the EXACT SAME standalone
    handler `browser_bridge_snapshot`/`browser_bridge_screenshot`/
    `browser_bridge_find`/`browser_bridge_read`/`browser_bridge_inspect`
    itself uses — same gate (`_gate_reason`, never `_authorize`: these never
    mutate anything and never present a live prompt, see
    `_handle_act_steps`'s own docstring on why read-only steps skip the A1
    prompt-preflight pass entirely), same redaction, same idx registration
    (`attach.AttachRegistry.set_index_map`/`merge_index_map`, called by the
    handler itself exactly as a standalone call would).

    `batch_args` is the WHOLE `browser_bridge_act` call's own top-level args
    (device_id/tab_id/tab/...) — the same tab-targeting fields the batch's
    action steps resolve against — overlaid with this step's own params
    (everything except `tool`), so a read-only step targets the SAME tab the
    batch itself is driving, never a step-local override.

    Returns `(step_result_dict, image_content_part)`: the second element is
    `screenshot`'s own multimodal `content` image_url entry (None for every
    other tool, and for a screenshot that degraded to ocr+layout/text-only —
    see `vision.finalize_screenshot_result`) so the caller can fold up to
    `MAX_STEP_SCREENSHOTS` of them into the batch's own multimodal envelope,
    the same way `browser_bridge_screenshot` returns one image to Hermes.
    """
    call_args: Dict[str, Any] = {k: v for k, v in batch_args.items() if k != "steps"}
    call_args.update({k: v for k, v in step_args.items() if k != "tool"})

    if tool == "snapshot":
        raw: Any = handle_snapshot(call_args, **kwargs)
    elif tool == "find":
        raw = handle_find(call_args, **kwargs)
    elif tool == "read":
        raw = handle_read(call_args, **kwargs)
    elif tool == "inspect":
        try:
            from . import inspect_tool  # noqa: PLC0415 - optional sibling module, see docstring
        except ImportError:
            return _err_dict("browser_bridge_inspect is not available on this build", code=protocol.INVALID_PARAMS), None
        raw = inspect_tool.handle_inspect(call_args, **kwargs)
    elif tool == "screenshot":
        try:
            from . import vision  # noqa: PLC0415 - optional sibling module, see docstring
        except ImportError:
            return _err_dict("browser_bridge_screenshot is not available on this build", code=protocol.INVALID_PARAMS), None
        if screenshots_remaining <= 0:
            # Defensive belt-and-braces only: `_handle_act_steps`'s Pass 1
            # already refuses a whole batch that REQUESTS more than
            # MAX_STEP_SCREENSHOTS `tool: "screenshot"` steps, so this branch
            # should never actually fire in practice.
            return _err_dict(
                f"steps: at most {MAX_STEP_SCREENSHOTS} screenshots may be embedded per batch",
                code=protocol.INVALID_PARAMS,
            ), None
        raw = vision.handle_screenshot(_PLUGIN_CTX, call_args, **kwargs)
    else:
        return _err_dict(f"unsupported tool {tool!r}", code=protocol.INVALID_PARAMS), None

    # `handle_screenshot` returns either the plain `_ok`/`_err` JSON string
    # every other handler here returns, or — for a successful `pixels`
    # embed — the `_multimodal` envelope dict (vision.py's own
    # `_pixels_multimodal_result`): `text_summary` is that SAME JSON string,
    # and `content` carries exactly one `image_url` part alongside the text.
    # Unwrap it here so every step_result in the batch's `steps` array has
    # the same plain-dict shape, and hand the image part back separately for
    # the caller to fold into the BATCH's own multimodal envelope.
    if isinstance(raw, dict) and raw.get("_multimodal"):
        try:
            step_dict = json.loads(raw.get("text_summary") or "{}")
        except (TypeError, ValueError):
            step_dict = _err_dict("screenshot step returned a malformed multimodal envelope")
        image_part = None
        for part in raw.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "image_url":
                image_part = part
                break
        if isinstance(step_dict, dict):
            step_dict["tool"] = tool
        return step_dict, image_part

    try:
        step_dict = json.loads(raw)
    except (TypeError, ValueError):
        step_dict = _err_dict(f"steps: {tool} step returned a malformed result")
    if isinstance(step_dict, dict):
        step_dict["tool"] = tool
    return step_dict, None


def _act_probe_target_selector(
    registry: Any, device_id: str, tab_id: int, args: Dict[str, Any], idx_key: str = "idx", selector_key: str = "selector"
) -> Optional[str]:
    """Read-only probe of what `idx_key`/`selector_key` resolves to RIGHT NOW,
    for authorization purposes only — never the real resolution (which still
    happens, separately, further down `_act_run_one_step`, and is what
    actually reports a bad/stale idx). Shared by the single-action path and
    `steps`' preflight, and by `drag`'s destination (a second call with
    `to_idx`/`to_selector`) — see `handle_act`'s original G2.2.13 comment for
    why this has to run before `_authorize`, not after.
    """
    if args.get(idx_key) is not None:
        try:
            return registry.resolve_index(device_id, tab_id, int(args[idx_key]))
        except (TypeError, ValueError):
            return None
    if args.get(selector_key):
        return str(args[selector_key])
    return None


def _act_would_need_live_prompt(
    device_id: str, origin: str, capability: str, holder: str, require_explicit_grant: bool = False
) -> bool:
    """Would `_authorize` for this origin/capability present a LIVE approval
    prompt right now? Mirrors `_authorize`'s own gate order up to (never
    including) the call into `approvals.require` — every denial-without-a-
    prompt case (`operator_capability_disabled`, a device power denial, a
    missing explicit grant, `origin_off`, `mode=='full'` and not dangerous)
    answers False here exactly as it would in `_authorize` itself, since none
    of those ever show the user anything to answer.

    speedimprovements.md A1: `steps`' preflight (`_handle_act_steps`) calls
    this for every step BEFORE running any of them, refusing the whole batch
    if any step would need a prompt not already covered by a standing grant
    — "an agent can't bury a prompt mid-batch." Never a substitute for
    `_authorize`, which still runs for real once a step executes.
    """
    if config.load().get("powers", {}).get(capability) is False:
        return False
    if _device_power_denial(device_id, capability) is not None:
        return False
    if require_explicit_grant and not state.has_explicit_grant(device_id, origin):
        return False
    mode = state.get_mode(device_id, origin)
    dangerous = _dangerous_capability(capability)
    if mode == "full" and not dangerous:
        return False
    if mode == "off":
        return False
    # mode == "request", or "full" + dangerous: `_authorize` falls into
    # approvals.require() here, which itself skips the live prompt when a
    # standing "always"/session grant already covers this origin/capability.
    try:
        from . import approvals  # noqa: PLC0415 - optional sibling module, workstream H
    except ImportError:
        return False  # _authorize denies outright here too (approval_transport_missing), never a prompt
    if approvals.has_standing_grant(device_id, holder, origin, capability):
        return False
    return True


def _act_would_need_commit_prompt(device_id: str, holder: str, origin: str) -> bool:
    """speedimprovements.md H1's `steps`-batch counterpart to
    `_act_would_need_live_prompt`, for the SEPARATE "commit" capability
    `_require_commit_approval` gates: would that call present a live
    approval prompt right now? True unless a standing grant for "commit"
    already covers this origin — mirrors `_act_would_need_live_prompt`'s own
    standing-grant check exactly, minus the origin-mode gate order (commit
    approval is required regardless of origin mode, so there is no
    mode=='full'-skips-the-prompt branch to mirror here)."""
    try:
        from . import approvals  # noqa: PLC0415 - optional sibling module, workstream H
    except ImportError:
        return False  # _require_commit_approval denies outright here too, never a prompt
    return not approvals.has_standing_grant(device_id, holder, origin, "commit")


def _act_run_one_step(
    device_id: str,
    tab: Dict[str, Any],
    action: str,
    args: Dict[str, Any],
    holder: str,
    registry: Any,
    kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """One `act` against one already-resolved tab: lease + frame-aware
    authorization + wire dispatch + result adoption — everything a
    standalone `browser_bridge_act` call does after its own device/tab/
    action/session resolution. Shared by `handle_act`'s single-action path
    and `_handle_act_steps`'s batched path (speedimprovements.md A1): every
    step of a batch goes through the EXACT SAME gates a standalone call
    would, one call at a time, never a shortcut version.

    Returns an `_ok_dict`/`_err_dict`-shaped plain dict (never a JSON
    string) — the caller decides whether to `json.dumps` it directly (the
    single-action path) or fold it into a `steps` array (the batch path).
    """
    tab_id = tab["tabId"]
    origin = tab.get("origin") or _origin_of(tab.get("url", ""))

    # Acting IS driving: take (or renew) the lease as part of the action
    # itself, same semantics as browser_bridge_attach's own renewal-by-
    # re-calling. A live lease held by someone else refuses outright, named,
    # rather than interleaving actions with theirs (plan §3.3). Uses this
    # device's own effective lease (state.py's effective_lease_seconds),
    # same as handle_attach above.
    try:
        registry.attach(device_id, tab_id, holder, tab_ref=tab, ttl=attach_mod.ttl_seconds_for(state.effective_lease_seconds(device_id)))
    except attach_mod.AttachConflict as exc:
        audit.record("act_lease_conflict", device=device_id, tab_id=tab_id, holder=exc.holder, requester=holder, action=action)
        return _err_dict(
            f"tab {tab_id} is driven by session {exc.holder}, not you",
            code=protocol.TARGET_BUSY,
            holder=exc.holder,
            hint=f"{_lease_wait_hint(device_id)}, before acting in this tab",
        )

    detail = json.dumps(
        {
            k: args.get(k)
            for k in ("idx", "selector", "xy", "text", "option_text", "url", "to_idx", "to_selector", "to_xy", "condition")
            if args.get(k) is not None
        },
        default=str,
    )

    # G2.2.13: authorize against the TARGET FRAME's own canonical origin for
    # a frame-qualified target, never the top tab's — probed here (idx
    # resolved read-only, exactly like the real resolution just below will
    # redo) so `_authorize` runs against the right origin BEFORE any wire
    # frame is sent. A bad/stale idx is not reported here — that error, with
    # its own precise wording, still comes from the real resolution below;
    # this probe simply leaves `auth_selector` as `None` (falling back to the
    # tab's own origin, today's behaviour) when it can't resolve one.
    auth_selector: Optional[str] = None
    if args.get("idx") is not None:
        try:
            auth_selector = registry.resolve_index(device_id, tab_id, int(args["idx"]))
        except (TypeError, ValueError):
            auth_selector = None
    elif args.get("selector"):
        auth_selector = str(args["selector"])
    auth_origin, frame_denial = _resolve_act_target_origin(device_id, tab_id, origin, auth_selector)
    if frame_denial:
        audit.record(
            "frame_origin_unknown", device=device_id, tab_id=tab_id, action=action,
            selector_prefix=_frame_prefix_of(auth_selector or ""),
        )
        return _err_dict(frame_denial, code=protocol.GRANT_DENIED, device_id=device_id, tab_id=tab_id, action=action)

    denial = _authorize(
        device_id, auth_origin, "act",
        summary=f"{action} on tab {tab_id} at {auth_origin}" + (" (embedded frame)" if auth_origin != origin else ""),
        session_key=holder,
        detail=detail,
        require_explicit_grant=auth_origin != origin,
    )
    if denial is not None:
        reason, code = denial
        return _err_dict(reason, code=code, device_id=device_id, tab_id=tab_id, origin=auth_origin, action=action)

    # G1.2/G2.2.13: `drag`'s destination is authorized independently of the
    # source -- they can be different frames (or one in a frame, one in the
    # top document) -- same probe-then-authorize shape as the source above.
    auth_to_origin: Optional[str] = None
    if action == "drag":
        auth_to_selector: Optional[str] = None
        if args.get("to_idx") is not None:
            try:
                auth_to_selector = registry.resolve_index(device_id, tab_id, int(args["to_idx"]))
            except (TypeError, ValueError):
                auth_to_selector = None
        elif args.get("to_selector"):
            auth_to_selector = str(args["to_selector"])
        auth_to_origin, to_frame_denial = _resolve_act_target_origin(device_id, tab_id, origin, auth_to_selector)
        if to_frame_denial:
            audit.record(
                "frame_origin_unknown", device=device_id, tab_id=tab_id, action=action,
                selector_prefix=_frame_prefix_of(auth_to_selector or ""),
            )
            return _err_dict(to_frame_denial, code=protocol.GRANT_DENIED, device_id=device_id, tab_id=tab_id, action=action)
        if auth_to_origin != origin:
            to_denial = _authorize(
                device_id, auth_to_origin, "act",
                summary=f"drag onto tab {tab_id} at {auth_to_origin} (embedded frame)",
                session_key=holder,
                detail=detail,
                require_explicit_grant=True,
            )
            if to_denial is not None:
                reason, code = to_denial
                return _err_dict(reason, code=code, device_id=device_id, tab_id=tab_id, origin=auth_to_origin, action=action)

    # speedimprovements.md H1: commit-mode 'pause' enforcement — an
    # ADDITIONAL gate layered ON TOP OF (never instead of) the origin-mode
    # `_authorize` call above, regardless of what that origin's own mode
    # already grants. Decided entirely BEFORE dispatch, and fail-closed:
    # cached element_meta first; if that can't answer (a bare xy/selector,
    # or an idx with no cached meta) the extension is asked to classify the
    # same target with a `classify_only` page.act that dispatches nothing;
    # and if THAT fails, the target is treated as committing. There is no
    # after-the-fact check in pause mode — by then a Finish/Delete/Pay click
    # would already have happened.
    commit_mode = state.effective_commit_mode(device_id)
    pre_committing = False
    if commit_mode == "pause":
        pre_committing, commit_source = _act_classify_committing(
            device_id, tab_id, action, args, registry, auth_origin if auth_origin != origin else None,
        )
        if pre_committing:
            if args.get("confirm") is not True:
                audit.record(
                    "commit_confirm_required", device=device_id, tab_id=tab_id, origin=auth_origin, action=action,
                    source=commit_source,
                )
                unclassified = (
                    " (its target could not be classified, so it is treated as committing)"
                    if commit_source == "classify_failed" else ""
                )
                return _err_dict(
                    f"{action} on tab {tab_id} at {auth_origin} is a committing action{unclassified} and this "
                    "device is in commit-mode 'pause' (Options > Committing actions > Pause for confirmation); "
                    "pass confirm: true and obtain user approval first",
                    code=protocol.COMMIT_CONFIRM_REQUIRED, device_id=device_id, tab_id=tab_id,
                    origin=auth_origin, action=action, committing=True,
                )
            commit_denial = _require_commit_approval(
                device_id, auth_origin, holder,
                summary=f"Confirm this committing action: {action} on tab {tab_id} at {auth_origin}",
                detail=detail,
            )
            if commit_denial is not None:
                reason, code = commit_denial
                return _err_dict(
                    reason, code=code, device_id=device_id, tab_id=tab_id, origin=auth_origin, action=action,
                    committing=True,
                )

    wire_params: Dict[str, Any] = {
        "tabId": tab_id,
        "action": action,
        "timeout_ms": int(args.get("timeout_ms") or DEFAULT_ACT_TIMEOUT_MS),
    }
    # G2.2.14 fix: pixel-routed dispatches (a bare `xy`, or a plain
    # top-document selector that happens to resolve onto/inside an
    # <iframe>) are sent at top-level-viewport coordinates and Chrome's
    # compositor delivers them to WHATEVER frame is under that point --
    # frameOrigin/toFrameOrigin below only ever cover a selector the gateway
    # already knew crossed a frame boundary. So the extension needs the same
    # (granted, denied, default_full) origin-policy triple `handle_snapshot`
    # already sends, on EVERY act (not just a frame-qualified one), to
    # hit-test the exact dispatch point (DOM.getNodeForLocation) and refuse
    # before ever calling Input.dispatchMouseEvent if it lands in a frame
    # whose own origin isn't authorized. Same function, same "gateway always
    # sends default_full=False" reasoning as handle_snapshot's identical
    # block -- an embedded third-party frame is never covered by the
    # tab-level default just because the fleet's default_mode happens to be
    # 'full'.
    granted_origins, denied_origins = _origin_policy_for_device(device_id)
    wire_params["granted_origins"] = granted_origins
    wire_params["denied_origins"] = denied_origins
    wire_params["default_full"] = False
    # G2.2.13 TOCTOU fix: the origin the gateway just authorized `_authorize`
    # against, sent so the extension can refuse if the LIVE frame's origin
    # (re-measured after resolveFrameTarget, right before dispatch) has
    # changed since the snapshot this authorization was based on -- a frame
    # can navigate between snapshot and act, and the gateway alone cannot
    # observe that; see act.ts's own TOCTOU note for the extension-side half.
    if auth_origin != origin:
        wire_params["frameOrigin"] = auth_origin
    if action == "drag" and auth_to_origin is not None and auth_to_origin != origin:
        wire_params["toFrameOrigin"] = auth_to_origin
    if args.get("idx") is not None:
        idx = int(args["idx"])
        selector = registry.resolve_index(device_id, tab_id, idx)
        if selector is None:
            return _err_dict(
                f"idx {idx} is not in tab {tab_id}'s current index map",
                code=protocol.INVALID_PARAMS,
                hint="the tab may have navigated or re-rendered since the last snapshot — call "
                     "browser_bridge_snapshot again and use a fresh idx",
            )
        # URL-level staleness check: catches a navigation between snapshot
        # and act. Deliberately NOT the only check any more — see `expect`
        # below for the per-element check that closes the "deleted table row
        # shifting its siblings" class of bug this alone cannot catch (the
        # URL never changes for a same-page re-render).
        map_meta = registry.index_map_meta(device_id, tab_id)
        current_url = tab.get("url", "")
        if map_meta and map_meta.get("url") and current_url and map_meta["url"] != current_url:
            return _err_dict(
                f"idx {idx} was captured on a different page ({map_meta['url']!r}) than tab {tab_id} is "
                f"on now ({current_url!r})",
                code=protocol.INVALID_PARAMS,
                hint="the tab navigated since the last snapshot/act — call browser_bridge_snapshot again "
                     "and use a fresh idx",
            )
        wire_params["selector"] = selector
        # ELEMENT_MISMATCH fix: the role/name the snapshot that produced this
        # idx recorded for it (attach.py's resolve_index_meta — populated
        # from dom.snapshot/page.act's `indexMeta`). Sent alongside `selector`
        # as page.act's `expect` param so the extension can compare it
        # against the LIVE element right before acting and refuse instead of
        # hitting whatever now sits at that selector. `None` (an older
        # extension build that never sent `indexMeta`) simply omits `expect`
        # — act.ts's documented back-compat path, not a new failure mode.
        expected_meta = registry.resolve_index_meta(device_id, tab_id, idx)
        if expected_meta:
            wire_params["expect"] = expected_meta
        # G2.6.2: the CDP backendNodeId recorded alongside this idx's
        # selector, if the snapshot that produced it resolved one (see
        # attach.py's set_index_map()/resolve_index_node() docstrings — a
        # shadow-piercing selector or a resolution failure legitimately has
        # none). Sent alongside `selector`/`expect` so act.ts can prefer
        # resolving the handle over re-querying the selector, falling back
        # to selector when the handle turns out stale.
        node_id = registry.resolve_index_node(device_id, tab_id, idx)
        if node_id is not None:
            wire_params["backendNodeId"] = node_id
    elif args.get("selector"):
        wire_params["selector"] = args["selector"]
    if args.get("xy") is not None:
        wire_params["xy"] = [int(v) for v in args["xy"]]
        # VIEWPORT_MISMATCH fix (G2.5.2/.3/.6): the viewport recorded
        # alongside the index map that produced this `xy` (or, if the model
        # also gave a selector/idx, alongside whatever this tab's most recent
        # snapshot/act was — resolvePoint tries selector first and only
        # falls back to xy, so this is only ever CHECKED when xy is the
        # coordinate actually used). Sent as `expectViewport`; the extension
        # re-measures the live viewport and refuses on any change in width,
        # height, dpr, scrollX or scrollY, naming both. `None` (never
        # snapshotted this tab, or an older extension build whose viewport
        # was never recorded) simply omits `expectViewport` — the same
        # back-compat story `expect` has for selector/idx.
        map_meta_for_xy = registry.index_map_meta(device_id, tab_id)
        if map_meta_for_xy and map_meta_for_xy.get("viewport"):
            wire_params["expectViewport"] = map_meta_for_xy["viewport"]
    if args.get("text") is not None:
        wire_params["text"] = args["text"]
    if args.get("url") is not None:
        wire_params["url"] = args["url"]
    # D5: select's own visible-text matcher -- independent of `text` above
    # (act.ts sends exactly one to the content script; both may be present
    # here since `_validate_act_args` only requires at least one).
    if action == "select" and args.get("option_text") is not None:
        wire_params["option_text"] = args["option_text"]
    # A4 follow-up: `snapshot_after` may now be the SAME scoping shape
    # `browser_bridge_snapshot` itself takes (`root`/`dialog_only`/
    # `viewport_only`) -- `root` is resolved through the SAME index map
    # `root`/`idx` targeting already uses (`_resolve_root_selector`), never
    # guessed at. A bare truthy non-dict value (the plain `true` case, or an
    # older-style call) still means "the whole page".
    snapshot_after = args.get("snapshot_after")
    if isinstance(snapshot_after, dict):
        scoped: Dict[str, Any] = {}
        sa_root = snapshot_after.get("root")
        if sa_root is not None:
            resolved_root, root_err = _resolve_root_selector(device_id, tab_id, sa_root)
            if root_err:
                message, code = root_err
                return _err_dict(message, code=code)
            scoped["selector"] = resolved_root
        if snapshot_after.get("dialog_only"):
            scoped["dialog_only"] = True
        if snapshot_after.get("viewport_only"):
            scoped["viewport_only"] = True
        wire_params["snapshot_after"] = scoped
    elif snapshot_after:
        wire_params["snapshot_after"] = True
    # speedimprovements.md G5: `screenshot_after` -- true or {scale, region,
    # format}, same shape/meaning as browser_bridge_screenshot's own params
    # of those names. Validated locally (not via vision.py's own
    # _validate_scale/_validate_region) so this call never depends on
    # vision.py being importable -- the same defensive-import discipline
    # register_tools already applies to vision.py as a whole (see its own
    # try/except ImportError above); the extension re-validates/clamps scale
    # and refuses an out-of-page region regardless.
    screenshot_after = args.get("screenshot_after")
    if isinstance(screenshot_after, dict):
        scoped_shot: Dict[str, Any] = {}
        sa_scale = screenshot_after.get("scale")
        if sa_scale is not None:
            if isinstance(sa_scale, bool) or not isinstance(sa_scale, (int, float)) or not (0.25 <= float(sa_scale) <= 1):
                return _err_dict("screenshot_after.scale must be a number between 0.25 and 1", code=protocol.INVALID_PARAMS)
            scoped_shot["scale"] = float(sa_scale)
        sa_region = screenshot_after.get("region")
        if sa_region is not None:
            if not isinstance(sa_region, dict) or not all(
                isinstance(sa_region.get(k), (int, float)) and not isinstance(sa_region.get(k), bool)
                for k in ("x", "y", "width", "height")
            ):
                return _err_dict("screenshot_after.region must be an object with numeric x, y, width and height", code=protocol.INVALID_PARAMS)
            scoped_shot["region"] = {k: int(sa_region[k]) for k in ("x", "y", "width", "height")}
        sa_format = screenshot_after.get("format")
        if sa_format is not None:
            if sa_format not in ("png", "jpeg"):
                return _err_dict(f"screenshot_after.format must be 'png' or 'jpeg' (got {sa_format!r})", code=protocol.INVALID_PARAMS)
            scoped_shot["format"] = sa_format
        wire_params["screenshot_after"] = scoped_shot if scoped_shot else True
    elif screenshot_after:
        wire_params["screenshot_after"] = True
    # speedimprovements.md B4 (stable element refs): the after-action walk
    # this call performs (act.ts's captureSnapshot) reuses idx for elements
    # it re-discovers, the same "keeps idx stable across calls" fix
    # browser_bridge_snapshot's own wire_params get above.
    existing_map = registry.current_index_map(device_id, tab_id)
    if existing_map:
        wire_params["existing_index_map"] = {str(k): v for k, v in existing_map.items()}
    # B4 idx-reuse fix: see the snapshot/find call sites' own comments above.
    wire_params["start_index"] = registry.high_water_idx(device_id, tab_id) + 1
    if action == "hover" and args.get("hover_ms") is not None:
        wire_params["hover_ms"] = args["hover_ms"]
    # G1.6.2: type's own params. Only marshalled for action=type (the
    # extension ignores them for every other action, but there is no reason
    # to send them across the wire for a click/scroll/etc call), and each one
    # gets its own line into wire_params -- a field not threaded through here
    # never reaches offscreen.ts's unpacking at all (§0.4's "silently
    # dropped" warning), independent of _validate_act_args below.
    if action == "type":
        if args.get("type_mode") is not None:
            wire_params["type_mode"] = args["type_mode"]
        if args.get("press_enter") is not None:
            wire_params["press_enter"] = bool(args["press_enter"])
        if args.get("dispatch") is not None:
            wire_params["dispatch"] = args["dispatch"]
        if args.get("key_delay_ms") is not None:
            wire_params["key_delay_ms"] = int(args["key_delay_ms"])

    # Infinite-scroll/pagination contract: scroll's own params, same
    # per-field-own-line/per-action rule as type's params immediately above --
    # not marshalled here means not reaching offscreen.ts's unpacking at all.
    # `wait_for_growth_ms` is clamped here (not just noted) since the
    # extension's own clamp is a last-resort safety net, not the primary
    # enforcement point -- same "clamp before it ever leaves this process"
    # posture as `timeout_ms` above.
    if action == "scroll":
        if args.get("to") is not None:
            wire_params["to"] = args["to"]
        if args.get("wait_for_growth_ms") is not None:
            wire_params["wait_for_growth_ms"] = max(0, min(MAX_WAIT_FOR_GROWTH_MS, int(args["wait_for_growth_ms"])))

    # G1.2: drag's own destination params. Same reasoning as `idx`/`selector`/
    # `xy` above (resolved here, never forwarded raw), and the same
    # per-field-own-line rule as type's params immediately above -- a
    # destination field not threaded through here never reaches offscreen.ts
    # at all (§0.4's "silently dropped" warning).
    if action == "drag":
        if args.get("to_idx") is not None:
            to_idx = int(args["to_idx"])
            to_selector = registry.resolve_index(device_id, tab_id, to_idx)
            if to_selector is None:
                return _err_dict(
                    f"to_idx {to_idx} is not in tab {tab_id}'s current index map",
                    code=protocol.INVALID_PARAMS,
                    hint="the tab may have navigated or re-rendered since the last snapshot — call "
                         "browser_bridge_snapshot again and use a fresh to_idx",
                )
            map_meta = registry.index_map_meta(device_id, tab_id)
            current_url = tab.get("url", "")
            if map_meta and map_meta.get("url") and current_url and map_meta["url"] != current_url:
                return _err_dict(
                    f"to_idx {to_idx} was captured on a different page ({map_meta['url']!r}) than tab "
                    f"{tab_id} is on now ({current_url!r})",
                    code=protocol.INVALID_PARAMS,
                    hint="the tab navigated since the last snapshot/act — call browser_bridge_snapshot "
                         "again and use a fresh to_idx",
                )
            wire_params["toSelector"] = to_selector
            # ELEMENT_MISMATCH fix's destination counterpart (G1.2.5): what
            # the snapshot that produced to_idx recorded for it, sent as
            # `expectTo` so the extension can refuse a drop onto a different
            # destination than the one the snapshot described (e.g. a
            # reordered kanban column) -- same back-compat rule as `expect`
            # above: `None` simply omits the field.
            expected_to_meta = registry.resolve_index_meta(device_id, tab_id, to_idx)
            if expected_to_meta:
                wire_params["expectTo"] = expected_to_meta
        elif args.get("to_selector"):
            wire_params["toSelector"] = args["to_selector"]
        if args.get("to_xy") is not None:
            wire_params["toXy"] = [int(v) for v in args["to_xy"]]
            # VIEWPORT_MISMATCH fix's destination counterpart (G2.5 cross-
            # reference): the same recorded viewport `xy` gets, sent as
            # `expectToViewport` -- the extension re-checks it once right
            # before the drop AND again right before the release (catching a
            # resize mid-drag, not just before it), refusing on any change.
            map_meta_for_to_xy = registry.index_map_meta(device_id, tab_id)
            if map_meta_for_to_xy and map_meta_for_to_xy.get("viewport"):
                wire_params["expectToViewport"] = map_meta_for_to_xy["viewport"]
        if args.get("mode") is not None:
            wire_params["mode"] = args["mode"]

    # speedimprovements.md A2: fill's own params. Each field's idx/selector
    # is resolved here, same rule (and same staleness check) as the
    # top-level idx above — the extension only ever sees a plain `selector`
    # per field, never an idx. Deliberately does NOT thread `expect`/
    # `backendNodeId` per field (out of scope for this action: fill is a
    # fast batch-write convenience, not the idx/ELEMENT_MISMATCH machinery
    # every other targeted action gets) -- not marshalled here means not
    # reaching offscreen.ts's unpacking at all, same "silently dropped"
    # warning as every other per-action field above.
    if action == "fill":
        wire_fields: List[Dict[str, Any]] = []
        for i, field in enumerate(args.get("fields") or []):
            if not isinstance(field, dict):
                return _err_dict(f"fields[{i}] must be an object", code=protocol.INVALID_PARAMS)
            field_idx = field.get("idx")
            field_selector = field.get("selector")
            if field_idx is not None:
                try:
                    fidx = int(field_idx)
                except (TypeError, ValueError):
                    return _err_dict(f"fields[{i}].idx must be an integer", code=protocol.INVALID_PARAMS)
                resolved_selector = registry.resolve_index(device_id, tab_id, fidx)
                if resolved_selector is None:
                    return _err_dict(
                        f"fields[{i}]: idx {fidx} is not in tab {tab_id}'s current index map",
                        code=protocol.INVALID_PARAMS,
                        hint="the tab may have navigated or re-rendered since the last snapshot — call "
                             "browser_bridge_snapshot again and use a fresh idx",
                    )
                map_meta = registry.index_map_meta(device_id, tab_id)
                current_url = tab.get("url", "")
                if map_meta and map_meta.get("url") and current_url and map_meta["url"] != current_url:
                    return _err_dict(
                        f"fields[{i}]: idx {fidx} was captured on a different page "
                        f"({map_meta['url']!r}) than tab {tab_id} is on now ({current_url!r})",
                        code=protocol.INVALID_PARAMS,
                        hint="the tab navigated since the last snapshot/act — call browser_bridge_snapshot "
                             "again and use a fresh idx",
                    )
            elif field_selector:
                resolved_selector = str(field_selector)
            else:
                return _err_dict(f"fields[{i}] needs idx or selector", code=protocol.INVALID_PARAMS)
            if "value" not in field:
                return _err_dict(f"fields[{i}] needs value", code=protocol.INVALID_PARAMS)
            value = field["value"]
            if not isinstance(value, (str, bool)):
                return _err_dict(f"fields[{i}].value must be a string or boolean", code=protocol.INVALID_PARAMS)
            wire_fields.append({"selector": resolved_selector, "value": value})
        wire_params["fields"] = wire_fields
    # C2: wait_for's structured `condition` -- resolved/clamped here, never
    # forwarded raw, same "own line, own per-action rule" as scroll's/drag's
    # own params above. An `idx` inside the condition (element_visible/
    # element_gone) is resolved to a selector the same way the top-level
    # `idx` field is, since the extension never receives raw indices at all
    # (the M2 contract every other selector on this call already follows).
    if action == "wait_for" and args.get("condition") is not None:
        condition = dict(args["condition"])
        ctype = condition.get("type")
        if ctype in ("element_visible", "element_gone") and condition.get("idx") is not None:
            cond_idx = int(condition["idx"])
            cond_selector = registry.resolve_index(device_id, tab_id, cond_idx)
            if cond_selector is None:
                return _err(
                    f"idx {cond_idx} is not in tab {tab_id}'s current index map",
                    code=protocol.INVALID_PARAMS,
                    hint="the tab may have navigated or re-rendered since the last snapshot — call "
                         "browser_bridge_snapshot again and use a fresh idx",
                )
            condition["selector"] = cond_selector
        condition_timeout_ms = max(0, min(MAX_WAIT_FOR_CONDITION_MS, int(condition.get("timeout_ms") or DEFAULT_WAIT_FOR_CONDITION_MS)))
        wire_condition: Dict[str, Any] = {"type": ctype, "timeout_ms": condition_timeout_ms}
        if condition.get("text") is not None:
            wire_condition["text"] = condition["text"]
        if condition.get("selector") is not None:
            wire_condition["selector"] = condition["selector"]
        if condition.get("pattern") is not None:
            wire_condition["pattern"] = condition["pattern"]
        wire_params["condition"] = wire_condition
        # The condition's own budget can legitimately exceed the call's
        # default overall timeout_ms (10s) -- raise the wire timeout_ms (and
        # this call's own relay timeout, below) to cover it, plus slack for
        # everything else page.act still has to do (settle, the after-diff).
        wire_params["timeout_ms"] = max(wire_params["timeout_ms"], condition_timeout_ms + 2000)

    relay = relay_mod.get_relay()
    timeout_s = wire_params["timeout_ms"] / 1000.0 + 5
    try:
        result = relay.call(device_id, "page.act", wire_params, timeout=timeout_s)
    except relay_mod.BridgeError as exc:
        return _bridge_err_dict(exc, "page.act")

    # Defect fix: act() already re-walks the DOM to build its diff, so a
    # successful page.act carries a fresh indexMap for the page AS IT IS
    # AFTER this action (protocol/schema.json's page.act.result.indexMap).
    # Adopt it wholesale here, exactly like handle_snapshot does for
    # dom.snapshot — leaving the PRE-action map in place would describe a
    # page this very action may have just re-rendered, which is the "stale
    # idx silently resolves to the wrong element" bug this closes. When the
    # extension's after-walk failed and no indexMap key comes back at all,
    # the old map is invalidated outright rather than kept: an absent map
    # must mean "re-snapshot before the next idx-based act", never "keep
    # trusting what we had before this action ran".
    if "indexMap" in result:
        post_index_map: Dict[int, str] = {}
        for raw_idx, sel in (result.get("indexMap") or {}).items():
            try:
                post_index_map[int(raw_idx)] = str(sel)
            except (TypeError, ValueError):
                continue
        # Same generation, same wire result as post_index_map above — see
        # attach.py's set_index_map() for why these two are replaced
        # together, never independently. `viewport` follows the same rule
        # (G2.5.1): the geometry this action itself may have changed (a
        # click that triggers a layout reflow, a scroll action) must be the
        # one a LATER xy act's expectViewport compares against, not the
        # pre-action recording.
        post_element_meta = _parse_index_meta(result.get("indexMeta"))
        post_viewport = _viewport_of(result.get("viewport"))
        # G2.6.2 cost bound: act.ts deliberately never runs the bulk
        # resolution pass on its own after-snapshot (too costly per-act; see
        # act.ts's report), so `result` has no "nodeMap" key here in
        # practice and this is always `{}` today — kept for forward
        # compatibility with a future extension build that might populate
        # it, and because set_index_map's own lockstep-replace rule needs
        # SOME value passed for every generation, never a merge with
        # whatever this tab's previous node map held.
        post_node_map = _node_map_of(result.get("nodeMap"))
        registry.set_index_map(
            device_id, tab_id, post_index_map, url=result.get("url") or tab.get("url", ""),
            element_meta=post_element_meta, viewport=post_viewport, node_map=post_node_map,
        )
    else:
        registry.invalidate_index_map(device_id, tab_id)

    diff, gateway_hits = _gateway_redact(result.get("diff", ""), device_id)
    if gateway_hits:
        audit.record(
            "redaction_gateway_catch",
            device=device_id, tab_id=tab_id, origin=origin, hits=gateway_hits, capability="act",
        )

    changed = result.get("changed")
    if changed is None:
        # protocol/schema.json's page.act result declares only `diff` and
        # `url` today (see this workstream's report: `changed` + `title`
        # need adding there and on the extension side). Fall back to
        # "diff is non-empty" so the model still gets a boolean now; a real
        # `changed` from a newer extension build always wins once it exists.
        changed = bool(diff)

    # speedimprovements.md H1: the `committing` label on the result and the
    # audit log — the extension's own post-dispatch classification, OR'd with
    # the pause-mode pre-dispatch decision above. Labelling only: in 'auto'
    # mode nothing is gated on it, and in 'pause' mode the gate already ran
    # before dispatch (never retroactively).
    committing = bool(result.get("committing")) or pre_committing

    audit.record(
        "tool_act", device=device_id, tab_id=tab_id, origin=origin, holder=holder, action=action,
        changed=bool(changed), redactions=gateway_hits, committing=committing,
    )
    extra: Dict[str, Any] = {"committing": committing}
    shadow = _shadow_counts(result.get("shadow"))
    if shadow is not None:
        extra["shadow"] = shadow
    # G1.6.5: the field's redacted post-type value. Already passed through
    # the extension's own redactFieldValue/redactText (content/targets.ts)
    # before this ever reached the gateway; `_gateway_redact` here is the
    # same belt-and-braces re-check every other page-text field on this
    # result gets (§0.7), not the only pass.
    field_value = result.get("fieldValue")
    if field_value is not None:
        field_value, fv_hits = _gateway_redact(str(field_value), device_id)
        if fv_hits:
            audit.record(
                "redaction_gateway_catch",
                device=device_id, tab_id=tab_id, origin=origin, hits=fv_hits, capability="act",
            )
        extra["fieldValue"] = field_value

    # Infinite-scroll/pagination contract: pass the extension's `scroll`
    # measurement through unchanged -- it's all numbers/booleans/an enum
    # string (never page text), so unlike fieldValue/diff there is nothing
    # here for _gateway_redact to check.
    scroll_result = result.get("scroll")
    if isinstance(scroll_result, dict):
        extra["scroll"] = scroll_result

    # C3: how long settle() itself took on this action, in ms -- present on
    # every successful act, not just wait_for's. All numbers, nothing here
    # for _gateway_redact to check, same reasoning as scroll above.
    settled_ms = result.get("settledMs")
    if isinstance(settled_ms, (int, float)):
        extra["settled_ms"] = int(settled_ms)

    # C2: wait_for's structured `condition` outcome -- present only when the
    # call sent one (see handle_act's condition marshalling above); a timed-
    # out condition (met: false) is a normal result, not an error.
    wait_for_result = result.get("waitFor")
    if isinstance(wait_for_result, dict):
        extra["wait_for"] = wait_for_result

    # G1.4: JS dialogs the extension observed while this action ran
    # (protocol/schema.json's page.act.result.dialogs). Already redacted
    # extension-side (dialogs.ts, before this frame was ever built) — this is
    # the gateway's belt-and-braces re-check, §0.7's redaction invariant
    # applied to this payload path. Message/dialog contents never appear in
    # the audit trail; only the fact that dialog(s) were seen and how many.
    raw_dialogs = result.get("dialogs")
    if isinstance(raw_dialogs, list) and raw_dialogs:
        shaped_dialogs: List[Dict[str, Any]] = []
        dialog_redaction_hits = 0
        for entry in raw_dialogs:
            if not isinstance(entry, dict):
                continue
            message, hits_m = _gateway_redact(str(entry.get("message") or ""), device_id)
            default_prompt, hits_d = _gateway_redact(str(entry.get("defaultPrompt") or ""), device_id)
            dialog_redaction_hits += hits_m + hits_d
            shaped_entry: Dict[str, Any] = {
                "id": entry.get("id"),
                "type": entry.get("type"),
                "message": message,
                "defaultPrompt": default_prompt,
                "url": entry.get("url"),
                "hasBrowserHandler": bool(entry.get("hasBrowserHandler")),
            }
            # Only present for hasBrowserHandler:true (the extension never
            # sends it for the other case) — passed through verbatim rather
            # than re-derived here, so the wording stays owned by one side.
            note = entry.get("note")
            if isinstance(note, str) and note:
                shaped_entry["note"] = note
            shaped_dialogs.append(shaped_entry)
        if dialog_redaction_hits:
            audit.record(
                "redaction_gateway_catch",
                device=device_id, tab_id=tab_id, origin=origin, hits=dialog_redaction_hits, capability="dialog",
            )
        if shaped_dialogs:
            extra["dialogs"] = shaped_dialogs
            audit.record(
                "tool_act_dialogs_observed", device=device_id, tab_id=tab_id, origin=origin,
                holder=holder, count=len(shaped_dialogs),
            )

    # D3: the element that actually received a click/hover's dispatched
    # event (protocol/schema.json's page.act.result.hit) -- extension-side
    # redaction already ran on `name` (act.ts's captureFullModeHit for full
    # mode, the content script's own describeElement for limited mode); this
    # is the same belt-and-braces gateway re-check every other page-text
    # field on this result gets.
    raw_hit = result.get("hit")
    if isinstance(raw_hit, dict) and isinstance(raw_hit.get("role"), str) and isinstance(raw_hit.get("name"), str):
        hit_name, hit_hits = _gateway_redact(raw_hit["name"], device_id)
        if hit_hits:
            audit.record(
                "redaction_gateway_catch",
                device=device_id, tab_id=tab_id, origin=origin, hits=hit_hits, capability="act",
            )
        shaped_hit: Dict[str, Any] = {"role": raw_hit["role"], "name": hit_name, "matched": bool(raw_hit.get("matched"))}
        if isinstance(raw_hit.get("idx"), int):
            shaped_hit["idx"] = raw_hit["idx"]
        extra["hit"] = shaped_hit

    # A4: the full after-action snapshot, when `snapshot_after` was sent and
    # the extension's after-walk produced one (protocol/schema.json's
    # page.act.result.snapshotAfter) -- `tree` is page text, same redaction
    # rule as `diff` above (already redacted extension-side; this is the
    # gateway's own re-check).
    raw_snapshot_after = result.get("snapshotAfter")
    if isinstance(raw_snapshot_after, dict) and isinstance(raw_snapshot_after.get("tree"), str):
        snap_tree, snap_hits = _gateway_redact(raw_snapshot_after["tree"], device_id)
        if snap_hits:
            audit.record(
                "redaction_gateway_catch",
                device=device_id, tab_id=tab_id, origin=origin, hits=snap_hits, capability="act",
            )
        extra["snapshot_after"] = {
            "tree": snap_tree,
            "url": raw_snapshot_after.get("url") or "",
            "title": raw_snapshot_after.get("title") or "",
        }

    # G5 (speedimprovements.md): screenshot_after -- the extension's
    # captureActScreenshotAfter already reused page.screenshot's own capture
    # path (format/scale/presence-overlay hiding); this saves the bytes to
    # disk the same way handle_screenshot does (vision.save_screenshot) and
    # names the path/size here. `handle_act` (the single-action, non-batched
    # path only) may additionally attach the image as a multimodal tool
    # result -- see `_act_screenshot_envelope` -- but this dict stays fully
    # JSON-safe either way, so `_handle_act_steps`'s batched path (which does
    # not attempt multimodal attachment) can return it unchanged.
    raw_screenshot_after = result.get("screenshotAfter")
    if isinstance(raw_screenshot_after, dict) and raw_screenshot_after.get("png_b64"):
        try:
            import base64 as _base64  # noqa: PLC0415

            from . import vision as vision_mod  # noqa: PLC0415 - optional sibling module

            png_bytes = _base64.b64decode(raw_screenshot_after["png_b64"])
            saved_path = vision_mod.save_screenshot(png_bytes)
            img = raw_screenshot_after.get("image") if isinstance(raw_screenshot_after.get("image"), dict) else {}
            extra["screenshot_after"] = {
                "path": str(saved_path),
                "width": img.get("width"),
                "height": img.get("height"),
                "format": raw_screenshot_after.get("format"),
                "scale": raw_screenshot_after.get("scale"),
            }
        except Exception:
            # Best-effort, same policy as fieldValue/fileInfo above -- a
            # failure to save/describe the screenshot never fails the action
            # itself, which already ran and whose diff/changed are the
            # primary evidence.
            pass

    act_url = result.get("url") or tab.get("url", "")
    act_title = result.get("title") or tab.get("title", "")
    _note_tab_activity(device_id, kwargs, tab_id, url=act_url, title=act_title, digest_text=diff)
    return _ok_dict(
        device_id=device_id,
        tab_id=tab_id,
        action=action,
        changed=bool(changed),
        diff=diff,
        url=act_url,
        title=act_title,
        **extra,
    )


def _handle_act_steps(device_id: str, args: Dict[str, Any], steps_arg: Any, kwargs: Dict[str, Any]) -> Any:
    """`browser_bridge_act`'s batched `steps` path (speedimprovements.md A1,
    extended by G1 with read-only steps).

    Every ACTION step (`{"action": ...}`) runs through `_act_run_one_step` —
    the EXACT SAME lease/authorize/frame/wire-dispatch/result-adoption path a
    standalone `browser_bridge_act` call uses, never a shortcut version.
    Every READ-ONLY step (`{"tool": "snapshot"|"screenshot"|"find"|"read"|
    "inspect", ...that tool's own params}`) runs through `_run_read_only_step`
    — the exact same standalone handler that tool's own top-level
    `browser_bridge_*` call uses (same gate, redaction, idx registration).
    Both run one call at a time, in order, STOPPING at the first failure,
    with results interleaved into `steps` in exactly the order they were
    given — `wait_for` stays an action (a `condition`/target to wait for, not
    a question about current state) and never takes a `tool` key.

    Before any step runs, every step's shape is validated (Pass 1: no side
    effects). Only ACTION steps go through Pass 2's approval preflight
    (`_act_would_need_live_prompt`): if ANY action step would need a live
    approval prompt not already covered by a standing grant, the WHOLE batch
    is refused up front, naming which step and why — an agent cannot bury a
    prompt mid-batch and have the rest run unattended (this is a fail-closed
    approximation for a step whose target only becomes resolvable after an
    earlier step's own DOM change: such a step is preflighted against the
    tab's own origin, the same origin its real per-step re-check below will
    use if nothing has navigated it away by the time it actually runs).
    READ-ONLY steps skip Pass 2 entirely and CANNOT trigger this preflight:
    they are gated by `_gate_reason` (via their own standalone handler), not
    `_authorize` — `_gate_reason` has no approval-transport integration at
    all and never presents a live prompt (a 'request'-mode origin is refused
    outright, the same M1 read-only-gate behaviour `browser_bridge_snapshot`/
    `_read`/`_find`/`_inspect`/`_screenshot` already have standalone) — so
    there is structurally nothing for a read-only step to bury. If that gate
    ever changes to present one, that would be a new failure mode for THIS
    function to close, not something already covered here.

    The tab is re-resolved fresh (via `_resolve_tab_target`, the same call
    `handle_act` itself makes) before EVERY action step, not just the first —
    a step may navigate, and the next step's own authorization/idx-staleness
    checks must run against wherever the tab actually is now, never a
    snapshot taken before an earlier step ran. Read-only steps resolve their
    own tab the same way, inside their own standalone handler, every time
    they run.

    Returns a plain JSON string (the ordinary case), or — when one or more
    `tool: "screenshot"` steps embedded a `pixels`-fidelity image (capped at
    `MAX_STEP_SCREENSHOTS`) — the same `_multimodal` envelope dict
    `browser_bridge_screenshot` itself returns, carrying every embedded
    image alongside the batch's own JSON summary. Both are valid handler
    results per `tools.registry.ToolRegistry._normalize_handler_result`
    (see `vision.handle_screenshot`'s own docstring).
    """
    if not isinstance(steps_arg, list) or not steps_arg:
        return _err("steps must be a non-empty array of step objects", code=protocol.INVALID_PARAMS)
    if len(steps_arg) > MAX_ACT_STEPS:
        return _err(f"steps must contain at most {MAX_ACT_STEPS} entries", code=protocol.INVALID_PARAMS)

    holder, err = _resolve_session(device_id, kwargs)
    if err:
        return err
    registry = attach_mod.get_registry()

    # Pass 1: shape-validate every step up front (no side effects) — a
    # batch that was never going to fully succeed refuses before it starts
    # anything, exactly like a single malformed act call does today. Each
    # entry becomes {"kind": "action"|"read", "action"|"tool": name, "args":
    # raw_step} — `kind` drives which of Pass 2/Pass 3's two branches below
    # handles it; the list stays 1:1 with `steps_arg` (one entry appended per
    # input index, none skipped), so index `i` means the same step in every
    # pass.
    normalized_steps: List[Dict[str, Any]] = []
    screenshot_step_count = 0
    for i, raw_step in enumerate(steps_arg):
        if not isinstance(raw_step, dict):
            return _err(f"steps[{i}] must be an object", code=protocol.INVALID_PARAMS)
        has_action = raw_step.get("action") is not None
        has_tool = raw_step.get("tool") is not None
        if has_action and has_tool:
            return _err(
                f"steps[{i}] has both 'action' and 'tool' — give exactly one "
                "('action' for a mutating step, 'tool' for a read-only one)",
                code=protocol.INVALID_STEP_KIND, failed_at=i,
                hint=_hint_for_code(protocol.INVALID_STEP_KIND),
            )
        if not has_action and not has_tool:
            return _err(
                f"steps[{i}] needs either 'action' (a mutating step) or 'tool' "
                f"(a read-only step: one of {list(READ_ONLY_STEP_TOOLS)})",
                code=protocol.INVALID_STEP_KIND, failed_at=i,
                hint=_hint_for_code(protocol.INVALID_STEP_KIND),
            )
        if has_tool:
            step_tool = str(raw_step.get("tool") or "")
            if step_tool not in READ_ONLY_STEP_TOOLS:
                return _err(
                    f"steps[{i}]: unknown tool {step_tool!r}",
                    code=protocol.INVALID_STEP_KIND, failed_at=i,
                    hint=f"tool must be one of {list(READ_ONLY_STEP_TOOLS)}",
                )
            read_validation_error = _validate_read_step_args(step_tool, raw_step)
            if read_validation_error:
                return _err(f"steps[{i}]: {read_validation_error}", code=protocol.INVALID_PARAMS)
            if step_tool == "screenshot":
                screenshot_step_count += 1
            normalized_steps.append({"kind": "read", "tool": step_tool, "args": raw_step})
        else:
            step_action = str(raw_step.get("action") or "")
            if step_action not in ACT_ACTIONS:
                return _err(
                    f"steps[{i}]: unknown action {step_action!r}",
                    code=protocol.INVALID_PARAMS,
                    hint=f"action must be one of {list(ACT_ACTIONS)}",
                )
            step_validation_error = _validate_act_args(step_action, raw_step)
            if step_validation_error:
                return _err(f"steps[{i}]: {step_validation_error}", code=protocol.INVALID_PARAMS)
            normalized_steps.append({"kind": "action", "action": step_action, "args": raw_step})

    if screenshot_step_count > MAX_STEP_SCREENSHOTS:
        return _err(
            f"steps: at most {MAX_STEP_SCREENSHOTS} tool:\"screenshot\" steps are allowed per batch "
            f"(got {screenshot_step_count})",
            code=protocol.INVALID_STEP_KIND,
            hint=_hint_for_code(protocol.INVALID_STEP_KIND),
        )

    tab, err = _resolve_tab_target(device_id, args, kwargs, "act")
    if err:
        return err
    tab_id = tab["tabId"]

    # Pass 2: preflight authorization for every ACTION step against the tab
    # as it stands right now, BEFORE any step runs — the "can't bury a
    # prompt mid-batch" guarantee. Each step's own real authorization still
    # runs for real, per-step, in pass 3 below; this pass only ever REFUSES
    # early, never grants anything a real per-step _authorize wouldn't.
    # Read-only steps are skipped entirely here — see this function's own
    # docstring for why they structurally cannot need this preflight.
    for i, step_entry in enumerate(normalized_steps):
        if step_entry["kind"] != "action":
            continue
        step_args = step_entry["args"]
        step_action = step_entry["action"]
        origin = tab.get("origin") or _origin_of(tab.get("url", ""))
        auth_selector = _act_probe_target_selector(registry, device_id, tab_id, step_args)
        auth_origin, frame_denial = _resolve_act_target_origin(device_id, tab_id, origin, auth_selector)
        if frame_denial:
            return _err(
                f"steps[{i}] refused before running: {frame_denial}",
                code=protocol.GRANT_DENIED, failed_at=i, device_id=device_id, tab_id=tab_id, action=step_action,
            )
        if _act_would_need_live_prompt(
            device_id, auth_origin, "act", holder, require_explicit_grant=auth_origin != origin
        ):
            return _err(
                f"steps[{i}] ({step_action} at {auth_origin}) would need a live approval prompt; "
                "refusing the whole batch rather than pausing on it partway through — ask the user to "
                "grant that origin (or run this one step alone) first",
                code=protocol.APPROVAL_REQUIRED, failed_at=i, device_id=device_id, tab_id=tab_id, action=step_action,
            )
        if step_action == "drag":
            auth_to_selector = _act_probe_target_selector(registry, device_id, tab_id, step_args, "to_idx", "to_selector")
            auth_to_origin, to_frame_denial = _resolve_act_target_origin(device_id, tab_id, origin, auth_to_selector)
            if to_frame_denial:
                return _err(
                    f"steps[{i}] refused before running: {to_frame_denial}",
                    code=protocol.GRANT_DENIED, failed_at=i, device_id=device_id, tab_id=tab_id, action=step_action,
                )
            if auth_to_origin != origin and _act_would_need_live_prompt(
                device_id, auth_to_origin, "act", holder, require_explicit_grant=True
            ):
                return _err(
                    f"steps[{i}] (drag onto {auth_to_origin}) would need a live approval prompt; "
                    "refusing the whole batch rather than pausing on it partway through",
                    code=protocol.APPROVAL_REQUIRED, failed_at=i, device_id=device_id, tab_id=tab_id, action=step_action,
                )
        # speedimprovements.md H1: a committing step ANYWHERE in the batch,
        # in commit-mode 'pause', is refused up front — the exact same
        # "can't bury a prompt mid-batch" guarantee `_act_would_need_live_prompt`
        # gives the ordinary approval-prompt case immediately above, applied
        # to the SEPARATE commit-confirm/commit-approval gate. Uses the same
        # fail-closed classification `_act_run_one_step` uses for real
        # (`_act_classify_committing`: cached element_meta, else a
        # dispatch-nothing `classify_only` round trip, else "committing").
        # A step whose target only appears after an earlier step runs can't
        # be classified yet, so it counts as committing here: put
        # `confirm: true` on it (and have a standing "commit" grant), or run
        # it alone. Pass 3 re-classifies every step for real before it runs.
        if state.effective_commit_mode(device_id) == "pause":
            step_committing, step_source = _act_classify_committing(
                device_id, tab_id, step_action, step_args, registry, auth_origin if auth_origin != origin else None,
            )
            if step_committing:
                if step_args.get("confirm") is not True:
                    audit.record(
                        "commit_confirm_required", device=device_id, tab_id=tab_id, origin=auth_origin,
                        action=step_action, source=step_source, batch_step=i,
                    )
                    return _err(
                        f"steps[{i}] ({step_action} at {auth_origin}) is a committing action and this "
                        "device is in commit-mode 'pause' (Options > Committing actions); pass "
                        "confirm: true on that step and obtain user approval first — refusing the whole "
                        "batch rather than pausing on it partway through",
                        code=protocol.COMMIT_CONFIRM_REQUIRED, failed_at=i, device_id=device_id,
                        tab_id=tab_id, action=step_action,
                    )
                if _act_would_need_commit_prompt(device_id, holder, auth_origin):
                    return _err(
                        f"steps[{i}] ({step_action} at {auth_origin}) needs a live commit-approval "
                        "prompt; refusing the whole batch rather than pausing on it partway through",
                        code=protocol.APPROVAL_REQUIRED, failed_at=i, device_id=device_id, tab_id=tab_id,
                        action=step_action,
                    )

    # Pass 3: actually run them, one at a time, stopping at the first
    # failure. The tab is re-resolved before each ACTION step (not just
    # reused from pass 2) so a navigating step is caught by the very next
    # step's own checks, never by a stale snapshot of "where the tab was" —
    # read-only steps re-resolve their own tab internally, every time, via
    # their own standalone handler.
    step_results: List[Dict[str, Any]] = []
    collected_images: List[Dict[str, Any]] = []
    completed = 0
    failed_at: Optional[int] = None
    last_ok: Optional[Dict[str, Any]] = None
    for i, step_entry in enumerate(normalized_steps):
        if step_entry["kind"] == "read":
            screenshots_remaining = MAX_STEP_SCREENSHOTS - len(collected_images)
            step_result, image_part = _run_read_only_step(
                step_entry["tool"], step_entry["args"], args, kwargs, screenshots_remaining
            )
            step_results.append(step_result)
            if image_part is not None:
                collected_images.append(image_part)
            if not step_result.get("success"):
                failed_at = i
                break
            completed += 1
            last_ok = step_result
            continue

        step_action = step_entry["action"]
        step_args = step_entry["args"]
        fresh_tab, tab_err = _resolve_tab_target(device_id, args, kwargs, "act")
        if tab_err:
            try:
                step_results.append(json.loads(tab_err))
            except (TypeError, ValueError):
                step_results.append(_err_dict(tab_err))
            failed_at = i
            break
        step_result = _act_run_one_step(device_id, fresh_tab, step_action, step_args, holder, registry, kwargs)
        step_results.append(step_result)
        if not step_result.get("success"):
            failed_at = i
            break
        completed += 1
        last_ok = step_result
        tab = fresh_tab

    payload: Dict[str, Any] = {
        "success": failed_at is None,
        "device_id": device_id,
        "tab_id": tab_id,
        "steps": step_results,
        "completed": completed,
    }
    if failed_at is not None:
        payload["failed_at"] = failed_at
        payload["error"] = step_results[failed_at].get("error", f"steps[{failed_at}] failed")
    if last_ok is not None:
        payload["diff"] = last_ok.get("diff", "")
        payload["url"] = last_ok.get("url", "")
        payload["title"] = last_ok.get("title", "")
        payload["changed"] = any(bool(r.get("changed")) for r in step_results if r.get("success"))
    audit.record(
        "tool_act_batch", device=device_id, tab_id=tab_id, holder=holder,
        steps=len(normalized_steps), completed=completed, failed_at=failed_at,
        screenshots=len(collected_images),
    )
    result_text = json.dumps(payload, default=str)

    # G1: fold up to MAX_STEP_SCREENSHOTS embedded images into the SAME
    # `_multimodal` envelope shape `browser_bridge_screenshot` itself returns
    # (vision.py's `_pixels_multimodal_result`) — `text_summary` is this same
    # JSON string, so a provider that can't take the multimodal envelope
    # degrades to exactly the plain-JSON contract a batch with no
    # screenshots already returns.
    if collected_images:
        note = (
            f"\n\n{len(collected_images)} screenshot(s) from this batch's steps are attached below, "
            "in the order their steps ran — inspect them with your native vision."
        )
        return {
            "_multimodal": True,
            "text_summary": result_text,
            "meta": {"screenshot_count": len(collected_images)},
            "content": [{"type": "text", "text": result_text + note}, *collected_images],
        }
    return result_text


def handle_act(args: Dict[str, Any], **kwargs: Any) -> Any:
    device_id, err = _resolve_device(args)
    if err:
        return err

    # speedimprovements.md A1: `steps` takes over the whole call — every
    # other targeting param on `args` (action/idx/selector/...) belongs to a
    # single step, not the batch, so it's ignored here rather than merged in
    # confusingly. G1: `_handle_act_steps` may now return the `_multimodal`
    # envelope dict (one or more `tool: "screenshot"` steps embedded an
    # image), not always a plain JSON string — see its own docstring.
    steps_arg = args.get("steps")
    if steps_arg is not None:
        return _handle_act_steps(device_id, args, steps_arg, kwargs)

    tab, err = _resolve_tab_target(device_id, args, kwargs, "act")
    if err:
        return err

    action = str(args.get("action") or "")
    if action not in ACT_ACTIONS:
        return _err(f"unknown action {action!r}", hint=f"action must be one of {list(ACT_ACTIONS)}")
    validation_error = _validate_act_args(action, args)
    if validation_error:
        return _err(validation_error, code=protocol.INVALID_PARAMS)

    holder, err = _resolve_session(device_id, kwargs)
    if err:
        return err
    registry = attach_mod.get_registry()

    result = _act_run_one_step(device_id, tab, action, args, holder, registry, kwargs)
    envelope = _act_screenshot_envelope(result)
    if envelope is not None:
        return envelope
    return json.dumps(result, default=str)


def _act_screenshot_envelope(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """speedimprovements.md G5: best-effort multimodal image attachment for
    a single (non-batched) `browser_bridge_act` call's `screenshot_after` —
    the same envelope shape `browser_bridge_screenshot` itself returns
    (vision.py's `_pixels_multimodal_result`: `_multimodal`/`text_summary`/
    `content` with one `image_url` part, at most one image), built from the
    SAME host embed helpers that module uses rather than a second
    implementation of the resize/budget logic. Reads the PNG `_act_run_one_step`
    already saved to disk (result['screenshot_after']['path']) back off disk,
    exactly like vision.py's own `_pixels_multimodal_result` does from ITS
    saved path.

    Returns None (falls back to the plain JSON result, which still names the
    saved path/size) whenever: no screenshot_after was requested, the host
    helpers aren't importable (off the gateway), the host's active model
    would strip an attached image, or the embed itself fails for any reason
    -- an image attachment is a nice-to-have and must never turn a
    successful action into a failed tool call. `_handle_act_steps`'s batched
    path deliberately does not call this -- see its own header comment.
    """
    shot = result.get("screenshot_after")
    if not isinstance(shot, dict) or not shot.get("path"):
        return None
    try:
        from pathlib import Path as _Path

        from tools.vision_tools import _EMBED_MAX_DIMENSION, _resize_image_for_vision, _should_use_native_vision_fast_path
        from tools.vision_tools_history_budget import resolve_embed_target_bytes

        if not _should_use_native_vision_fast_path():
            return None
        data_url = _resize_image_for_vision(
            _Path(shot["path"]),
            mime_type="image/png",
            max_base64_bytes=resolve_embed_target_bytes(),
            max_dimension=_EMBED_MAX_DIMENSION,
            force_jpeg=True,
        )
        text = json.dumps(result, default=str)
        attached = text + "\n\nThe screenshot from this action is attached — inspect it with your native vision."
        return {
            "_multimodal": True,
            "text_summary": text,
            "meta": {"screenshot_path": shot["path"], "native_vision": True},
            "content": [
                {"type": "text", "text": attached},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    except Exception:
        return None


# -- browser_bridge_ask (M2) --------------------------------------------------

DEFAULT_ASK_TIMEOUT_MS = 60000

ASK_SCHEMA = {
    "name": "browser_bridge_ask",
    "description": (
        "Push a short question to the user as an overlay in their attached tab, instead of guessing "
        "which element you mean. Pass `candidates` — indices from the tab's most recent "
        "browser_bridge_snapshot — to highlight them as numbered choices; the user clicks (or "
        "types) the one they mean and you get back which index they chose. Blocks until they "
        "answer or `timeout_ms` elapses. Keep the question short: it renders as a small overlay, "
        "not a chat message. Use this whenever an idx/selector is genuinely ambiguous rather than "
        "acting on a guess. Because pointing at specific candidates exposes which elements exist on "
        "the page (the same information a browser_bridge_read of that page would), a 'request'-mode "
        "origin parks a call that includes candidates for approval, the same as browser_bridge_act "
        "would; a bare question with no candidates never touches page content and is not gated that "
        "way."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "tab_id": {"type": "integer", "description": "Tab to ask in. Omit when exactly one tab is attached."},
            "question": {
                "type": "string",
                "description": "Short question shown in the overlay, e.g. 'Which button should I click?'",
            },
            "candidates": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Element indices (from the last snapshot) to highlight as numbered choices.",
            },
            "timeout_ms": {
                "type": "integer",
                "description": f"How long to wait for the user's answer. Default {DEFAULT_ASK_TIMEOUT_MS}.",
            },
        },
        "required": ["question"],
    },
}


def handle_ask(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = _resolve_device(args)
    if err:
        return err
    tab, err = _resolve_attached_tab(device_id, args.get("tab_id"), "ask")
    if err:
        return err
    tab_id = tab["tabId"]
    origin = tab.get("origin") or _origin_of(tab.get("url", ""))

    question = str(args.get("question") or "").strip()
    if not question:
        return _err("question is required")

    raw_candidates = args.get("candidates") or []
    try:
        candidates = [int(c) for c in raw_candidates]
    except (TypeError, ValueError):
        return _err("candidates must be a list of integer indices")

    holder, err = _resolve_session(device_id, kwargs)
    if err:
        return err

    # Highlighting specific candidates reveals which elements exist on the
    # page — functionally the same disclosure a browser_bridge_read would
    # make — so it's gated the same way browser_bridge_act is. A bare
    # question with no candidates carries no page content either direction
    # and is left ungated here (the tab already had to be attached, which
    # itself required the origin to not be 'off').
    if candidates:
        denial = _authorize(
            device_id, origin, "read",
            summary=f"ask (with {len(candidates)} highlighted candidate(s)) on tab {tab_id} at {origin}",
            session_key=holder,
            detail=json.dumps({"question": question, "candidates": candidates}, default=str),
        )
        if denial is not None:
            reason, code = denial
            return _err(reason, code=code, device_id=device_id, tab_id=tab_id, origin=origin)

    timeout_ms = int(args.get("timeout_ms") or DEFAULT_ASK_TIMEOUT_MS)
    wire_params: Dict[str, Any] = {"tabId": tab_id, "question": question}
    if candidates:
        wire_params["candidates"] = candidates

    relay = relay_mod.get_relay()
    try:
        result = relay.call(device_id, "annotate", wire_params, timeout=timeout_ms / 1000.0 + 5)
    except relay_mod.BridgeError as exc:
        return _bridge_err(exc, "annotate")

    choice = result.get("choice")
    audit.record(
        "tool_ask", device=device_id, tab_id=tab_id, origin=origin, holder=holder,
        question=question, candidates=candidates, choice=choice,
    )
    return _ok(device_id=device_id, tab_id=tab_id, question=question, candidates=candidates, choice=choice)


# -- H2 (speedimprovements.md): content_trust + suspicious_text, one central
# place -----------------------------------------------------------------
#
# Every tool listed in `PAGE_CONTENT_TOOLS` returns text the extension read
# OFF THE PAGE -- an accessibility tree, article/ticket text, a search match,
# an inspect answer, an act's diff/hit, console output, a network/fetch
# body, a dialog's message (surfaced via `act`'s own `dialogs` field), or a
# screenshot's OCR/layout description. None of that text was authored by the
# user or by this tool; it is data the site chose to put on the page, and it
# can contain a prompt-injection attempt aimed at whatever model reads the
# tool result next. `_wrap_handler_with_timing` (the one wrapper every
# `browser_bridge_*` handler is already routed through, alongside `timing`)
# is the natural single place to mark that: it stamps `content_trust:
# "untrusted_page_content"` on every result from one of these tools, and
# separately scans every string value the result carries for text that
# reads as an instruction aimed at an AI, surfacing up to
# `_SUSPICIOUS_TEXT_MAX_HITS` matches as `suspicious_text` plus one audit
# event. The scan NEVER alters or removes the page text itself -- it only
# ever adds a sibling field pointing at it.
#
# `PAGE_CONTENT_TOOLS` names every registered tool whose result can carry
# page-derived text (H2's list, later widened by the coverage-gap pass noted
# below): snapshot, find, read, inspect, act (its diff/hit/dialogs), console,
# network, fetch, screenshot, dialog, downloads, session, tabs, attach. Tools
# left out either never echo page text back (status/release/ask/upload/
# cookie_set/http_auth_status/open_tab) or return a value the agent itself
# supplied rather than page content (evaluate's return value is arguably
# page-adjacent but out of H2's explicit list, so left alone here).
#
# H2 coverage-gap fix: `dialog`/`downloads`/`session`/`tabs`/`attach` were
# originally left off this list on the theory that they "never echo page
# text back" — wrong for all five. `browser_bridge_dialog` resolves a JS
# dialog whose `message`/`defaultPrompt` is page-authored text the page
# chose to put in front of the user (dialogs.py's own module docstring: the
# id names an instance, but nothing stops a future revision of this file
# from echoing the message back, and even today `ack_message` round-trips
# page text through the request the model constructs). `browser_bridge_
# downloads` returns `filename`/`url` for each download — both attacker-
# influenced (session_powers.py's DOWNLOADS_SCHEMA description: attribution
# is heuristic, and a page controls the suggested filename outright).
# `browser_bridge_session` (`describe`) and `browser_bridge_tabs` (`list`)
# both surface each tab's `title`, read straight off the page's own <title>
# element (sessions.py/tabs.py). `browser_bridge_attach` returns the same
# page-authored `title` for every tab it leases or lists as a candidate
# (tools.py's `_tab_briefs`/`handle_attach`). None of these five needed a
# code change to start carrying page text — they already did; only their
# absence from this set was the gap.
PAGE_CONTENT_TOOLS = frozenset(
    {
        "browser_bridge_snapshot",
        "browser_bridge_find",
        "browser_bridge_read",
        "browser_bridge_inspect",
        "browser_bridge_act",
        "browser_bridge_console",
        "browser_bridge_network",
        "browser_bridge_fetch",
        "browser_bridge_screenshot",
        "browser_bridge_dialog",
        "browser_bridge_downloads",
        "browser_bridge_session",
        "browser_bridge_tabs",
        "browser_bridge_attach",
    }
)

CONTENT_TRUST_UNTRUSTED = "untrusted_page_content"

_SUSPICIOUS_TEXT_MAX_HITS = 5
_SUSPICIOUS_TEXT_EXCERPT_MAX = 120
_SUSPICIOUS_TEXT_EXCERPT_CONTEXT = 40

# Deliberately narrow and phrase-shaped, not single words -- "Previous" (a
# pagination button) or "Instructions" (a form's own section heading) must
# never fire on their own; only a recognisable injection-style phrase does.
# Each pattern is `(label, compiled regex)`; the label rides back verbatim
# in `suspicious_text[].pattern` so a reviewer can see which rule fired
# without re-deriving it from the excerpt.
_SUSPICIOUS_TEXT_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("ignore_previous_instructions", re.compile(r"ignore\s+(?:all\s+)?previous\s+instructions", re.IGNORECASE)),
    ("disregard_instructions", re.compile(r"disregard\s+your\s+instructions", re.IGNORECASE)),
    ("you_are_an_ai", re.compile(r"you\s+are\s+(?:now\s+)?an?\s+(?:ai|assistant|agent)\b", re.IGNORECASE)),
    ("system_prompt", re.compile(r"system\s+prompt", re.IGNORECASE)),
    ("assistant_colon", re.compile(r"assistant\s*:", re.IGNORECASE)),
    ("call_the_tool", re.compile(r"call\s+the\s+tool", re.IGNORECASE)),
    ("browser_bridge_tool_name", re.compile(r"browser_bridge_", re.IGNORECASE)),
)


def _scan_for_suspicious_text(value: Any, path: str, hits: List[Dict[str, str]]) -> None:
    """Recurses through a parsed tool result (dict/list/str, same shape
    `inspect_tool.py`'s `_redact_deep` walks) looking for injection-style
    phrasing in every string it finds. Appends up to
    `_SUSPICIOUS_TEXT_MAX_HITS` `{field, excerpt, pattern}` dicts to `hits`
    in place and stops early once that cap is hit -- cheap even on a large
    snapshot tree. `path` is a dotted/bracketed key path (`"tree"`,
    `"matches[2]"`, `"entries[0].text"`) so a hit can be traced back to
    exactly which field carried it, without ever changing the field itself."""
    if len(hits) >= _SUSPICIOUS_TEXT_MAX_HITS:
        return
    if isinstance(value, str):
        for label, rx in _SUSPICIOUS_TEXT_PATTERNS:
            if len(hits) >= _SUSPICIOUS_TEXT_MAX_HITS:
                return
            match = rx.search(value)
            if not match:
                continue
            start = max(0, match.start() - _SUSPICIOUS_TEXT_EXCERPT_CONTEXT)
            end = min(len(value), match.end() + _SUSPICIOUS_TEXT_EXCERPT_CONTEXT)
            excerpt = value[start:end][:_SUSPICIOUS_TEXT_EXCERPT_MAX]
            hits.append({"field": path or "(root)", "excerpt": excerpt, "pattern": label})
        return
    if isinstance(value, dict):
        for key, sub in value.items():
            if len(hits) >= _SUSPICIOUS_TEXT_MAX_HITS:
                return
            _scan_for_suspicious_text(sub, f"{path}.{key}" if path else str(key), hits)
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            if len(hits) >= _SUSPICIOUS_TEXT_MAX_HITS:
                return
            _scan_for_suspicious_text(item, f"{path}[{i}]", hits)
        return


# -- E1 (speedimprovements.md): timing wrapper, one central place -----------
#
# The handler contract (module docstring above) is `handler(args, **kwargs)
# -> str`, a compact JSON string. `_wrap_handler_with_timing` wraps any such
# handler so its JSON result gains a `timing` object before it ever reaches
# the model, without touching the handler itself: it starts a fresh
# `timing_mod` accumulator, calls the handler, and — if the handler made any
# `relay.call()`s along the way (relay.py's ONE choke point for every
# gateway->extension round trip) — folds that accumulator plus this
# handler's own wall time into `timing_mod.build_tool_timing()`.
#
# It is also H2's one central place: after the handler's own dict is
# parsed (needed anyway to splice in `timing`), a tool in
# `PAGE_CONTENT_TOOLS` additionally gets `content_trust` stamped on and a
# `suspicious_text` scan run over the WHOLE result -- so no individual
# handler (snapshot/read/find/inspect/act/console/network/fetch/screenshot)
# had to be touched, and no future one can forget it.
#
# Applied via `_TimingProxy`, a thin ctx wrapper that intercepts
# `register_tool` and wraps whatever handler it's given. `register_tools`
# below builds ONE proxy and threads it through every sibling module's own
# `register_*_tools(ctx)` call (vision.py, session_powers.py, navigation.py,
# etc.) instead of the raw ctx — so this is truly one central place: every
# module that registers a `browser_bridge_*` tool gets timing (and, for the
# page-content tools, content_trust/suspicious_text) without ANY of them
# importing timing.py or repeating this wrapper themselves.
def _wrap_handler_with_timing(name: str, handler: Callable[..., str]) -> Callable[..., str]:
    @functools.wraps(handler)
    def wrapped(args: Dict[str, Any], **kwargs: Any) -> str:
        timing_mod.reset()
        start = time.monotonic()
        raw = handler(args, **kwargs)
        total_ms = (time.monotonic() - start) * 1000.0
        tool_timing = timing_mod.build_tool_timing(total_ms)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict):
            data["timing"] = tool_timing
            if name in PAGE_CONTENT_TOOLS:
                data["content_trust"] = CONTENT_TRUST_UNTRUSTED
                hits: List[Dict[str, str]] = []
                _scan_for_suspicious_text(data, "", hits)
                if hits:
                    data["suspicious_text"] = hits
                    # Pattern labels and a field path only -- never the
                    # excerpt itself -- mirrors `redaction_gateway_catch`'s
                    # numbers-and-names-only shape (this file, inspect_tool.py):
                    # the audit log is safe to read/aggregate without ever
                    # holding a copy of the untrusted page text.
                    audit.record(
                        "suspicious_text_detected",
                        tool=name,
                        device_id=data.get("device_id"),
                        tab_id=data.get("tab_id"),
                        hits=len(hits),
                        patterns=sorted({h["pattern"] for h in hits}),
                        fields=[h["field"] for h in hits],
                    )
            # skilllinks.md SL2: the `skills` hint block goes on AFTER the
            # suspicious-text scan above, never before -- a product skill's
            # own description can legitimately contain a tool name or phrase
            # that scan reacts to, and this plugin-authored block must never
            # be what trips it. skills_hint.apply is unconditionally fail-open.
            skills_hint.apply(name, data, kwargs)
            raw = json.dumps(data)
        # Numbers only (tool name, device id if this handler's own audit
        # calls already recorded one under a different event -- this one
        # never reads `data` beyond the timing dict it just built), never
        # page content: safe for the aggregate `hermes browser-bridge logs
        # --timing` to summarize across every call, ever.
        audit.record("tool_timing", tool=name, **tool_timing)
        return raw

    return wrapped


class _TimingProxy:
    """Wraps a plugin ``ctx`` so every ``register_tool`` call it forwards
    gets its handler wrapped with ``_wrap_handler_with_timing`` first.
    Everything else (``register_cli_command``, ``register_skill``, ``llm``,
    ...) passes straight through via ``__getattr__`` — this only ever
    touches the one call that installs a tool handler.
    """

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    def register_tool(self, *, name: str, handler: Callable[..., str], **kwargs: Any) -> Any:
        return self._ctx.register_tool(name=name, handler=_wrap_handler_with_timing(name, handler), **kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._ctx, item)


def register_tools(ctx) -> list[str]:
    """Register the M1+M2 tool surface. Returns the names registered.

    Vision (browser_bridge_screenshot) is workstream D's tool, landing in
    vision.py in parallel with this file — imported defensively so this
    module works whether or not that file exists yet at any given moment
    during parallel development.

    E1 (speedimprovements.md): `ctx` below is a `_TimingProxy`, not the raw
    plugin ctx — see its docstring and `_wrap_handler_with_timing` above.
    Every `register_*_tools` call this function makes (its own inline block,
    and every sibling module's) gets timing this way, with zero changes to
    any of those modules.
    """
    global _PLUGIN_CTX
    # G1: stash the RAW ctx (before _TimingProxy wraps it below) so a `steps`
    # screenshot step can later call `vision.handle_screenshot(ctx, ...)`
    # exactly as `vision.register_vision_tools`'s own closure does — see
    # `_PLUGIN_CTX`'s own docstring above.
    _PLUGIN_CTX = ctx
    ctx = _TimingProxy(ctx)
    registered: list[str] = []

    for schema, handler in (
        (STATUS_SCHEMA, handle_status),
        (ATTACH_SCHEMA, handle_attach),
        (RELEASE_SCHEMA, handle_release),
        (SNAPSHOT_SCHEMA, handle_snapshot),
        (FIND_SCHEMA, handle_find),
        (READ_SCHEMA, handle_read),
        (ACT_SCHEMA, handle_act),
        (ASK_SCHEMA, handle_ask),
    ):
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            check_fn=bridge_available,
            emoji="🌐",
        )
        registered.append(schema["name"])

    try:
        from . import vision  # noqa: PLC0415 - optional sibling module, see docstring

        vision_tools = vision.register_vision_tools(ctx)
        if vision_tools:
            registered.extend(vision_tools)
    except ImportError:
        pass

    # M3 (workstream I): browser_bridge_fetch/_cookies/_network. Same
    # defensive seam as vision.py above — session_powers.py is owned by this
    # same workstream (unlike vision.py's cross-workstream boundary), but the
    # import stays guarded anyway so a partial checkout (this file present,
    # session_powers.py not yet landed) degrades to "M1+M2 tools only"
    # instead of failing plugin registration outright.
    try:
        from . import session_powers  # noqa: PLC0415 - optional sibling module, see docstring

        session_tools = session_powers.register_session_tools(ctx)
        if session_tools:
            registered.extend(session_tools)
    except ImportError:
        pass

    # M4: browser_bridge_open_tab. Same defensive seam as the two above — a
    # checkout without navigation.py degrades to "no open-tab tool" rather
    # than failing plugin registration and taking every other tool with it.
    try:
        from . import navigation  # noqa: PLC0415 - optional sibling module, see docstring

        nav_tools = navigation.register_navigation_tools(ctx)
        if nav_tools:
            registered.extend(nav_tools)
    except ImportError:
        pass

    # G3.7: browser_bridge_http_auth_status. Same defensive seam as the three
    # above -- a checkout without http_auth.py degrades to "no basic-auth
    # status tool" rather than failing plugin registration outright.
    try:
        from . import http_auth  # noqa: PLC0415 - optional sibling module, see docstring

        auth_tools = http_auth.register_http_auth_tools(ctx)
        if auth_tools:
            registered.extend(auth_tools)
    except ImportError:
        pass

    # G1.4: browser_bridge_dialog (accept/dismiss a recorded JS dialog). Same
    # defensive seam as the three blocks above — a checkout without
    # dialogs.py degrades to "no dialog tool", not a failed plugin load.
    try:
        from . import dialogs as dialogs_mod  # noqa: PLC0415 - optional sibling module, see docstring

        dialog_tools = dialogs_mod.register_dialog_tools(ctx)
        if dialog_tools:
            registered.extend(dialog_tools)
    except ImportError:
        pass

    # G1.3: browser_bridge_upload (file upload, both tiers). Same defensive
    # seam as the four blocks above -- a checkout without upload.py degrades
    # to "no upload tool", not a failed plugin load.
    try:
        from . import upload as upload_mod  # noqa: PLC0415 - optional sibling module, see docstring

        upload_tools = upload_mod.register_upload_tools(ctx)
        if upload_tools:
            registered.extend(upload_tools)
    except ImportError:
        pass

    # G1.5: browser_bridge_evaluate (arbitrary JS evaluation). Same defensive
    # seam as the five blocks above — a checkout without evaluate.py degrades
    # to "no evaluate tool", not a failed plugin load.
    try:
        from . import evaluate as evaluate_mod  # noqa: PLC0415 - optional sibling module, see docstring

        evaluate_tools = evaluate_mod.register_evaluate_tools(ctx)
        if evaluate_tools:
            registered.extend(evaluate_tools)
    except ImportError:
        pass

    # G3.1: browser_bridge_session (first-class, nameable, resumable
    # sessions). Same defensive seam as every block above -- a checkout
    # without sessions.py degrades to "no session-management tool, but every
    # other tool still works via its own auto-provisioned shadow session",
    # not a failed plugin load.
    try:
        from . import sessions as sessions_mod  # noqa: PLC0415 - optional sibling module, see docstring

        session_mgmt_tools = sessions_mod.register_session_management_tools(ctx)
        if session_mgmt_tools:
            registered.extend(session_mgmt_tools)
    except ImportError:
        pass

    # G3.3: browser_bridge_tabs (the agent-addressable tab workspace). Same
    # defensive seam as every block above -- a checkout without tabs.py
    # degrades to "no workspace view, and `tab` on other tools always
    # refuses with a named reason", not a failed plugin load.
    try:
        from . import tabs as tabs_mod  # noqa: PLC0415 - optional sibling module, see docstring

        tab_tools = tabs_mod.register_tab_workspace_tools(ctx)
        if tab_tools:
            registered.extend(tab_tools)
    except ImportError:
        pass

    # B2: browser_bridge_inspect (ProjectRules/speedimprovements.md). Same
    # defensive seam as every block above -- a checkout without
    # inspect_tool.py degrades to "no inspect tool", not a failed plugin load.
    try:
        from . import inspect_tool  # noqa: PLC0415 - optional sibling module, see docstring

        inspect_tools = inspect_tool.register_inspect_tools(ctx)
        if inspect_tools:
            registered.extend(inspect_tools)
    except ImportError:
        pass

    return registered
