"""Approval presentation for `request` mode, migrated onto Hermes' native
approval-transport contract (plan.md §3.2; ProjectRules/Plan.md 3.4.1).

MIGRATION NOTE (supersedes the M2-era module docstring): an earlier audit,
run against the WRONG source tree (``~/.hermes/hermes-agent``, v0.19.1),
reported that ``ctx.register_approval_transport`` did not exist. It does
exist on the gateway's real install, ``/usr/local/lib/hermes-agent``
(v0.21.4) -- see ``hermes_cli/plugins.py:430`` and
``hermes_cli/approval_transport.py``. This module now builds on that
contract instead of re-implementing it:

  * ``hermes_cli.approval_transport.ApprovalRequest`` / ``invoke_approval_transport``
    own request construction, digest binding, the bounded worker thread,
    timeout enforcement, and decision validation (stale/invalid/late
    responses are rejected by the HOST, not by anything in this file).
  * This module owns exactly two things the host cannot: (1) PRESENTATION --
    turning a request into the ``approval.request`` wire frame the extension
    already understands, and waiting for the matching ``approval.respond``;
    and (2) CORRELATION -- an in-memory ``request_id -> waiter`` table so a
    reply can find the thread that is blocked waiting for it. Everything
    else M2 built by hand (a sqlite ``approvals`` table with its own TTL
    math, a delivery-timeout config knob duplicated against the host's own
    ``approvals.timeout``, "reap orphaned rows on restart") is gone: the
    host's worker-thread-bounded wait already can't outlive a timeout, and a
    gateway restart kills the blocked tool-call thread exactly like it kills
    everything else in that process, so there is nothing left to persist or
    reap. See ``_Waiter``'s docstring for the detail.

TWO CALLERS, ONE PRESENTATION PATH:

  1. ``require(device_id, origin, capability, summary, session_key, detail)``
     -- called directly by ``tools.py``/``session_powers.py``'s ``_authorize``
     seam for THIS plugin's own `request`-mode capabilities (act/fetch/
     cookies/network/ask). It builds its own ``ApprovalRequest`` and drives
     ``invoke_approval_transport`` itself -- it does NOT go through Hermes'
     ``security.approval.transport`` selection gate. That's deliberate, not
     an oversight: the wire frame's ``origin``/``capability`` fields
     (protocol/schema.json's ``approval.request``, required + enum-
     constrained) only have real values at THIS call site, and Hermes'
     ``request_tool_approval``/``_present_with_selected_transport`` path has
     no such fields on its own ``ApprovalRequest`` (command/description/
     pattern_key only) -- so this plugin's own capability approvals stay
     fully functional and origin/capability-accurate regardless of whether
     the operator has selected "browser-bridge" as the system-wide
     transport. THIS is the fallback story for §4 of the migration: there
     never was a "transport not selected" failure mode for our own gate to
     fall back FROM, because it was never gated on that selection.

  2. ``present(request: ApprovalRequest) -> ApprovalDecision`` -- registered
     with ``ctx.register_approval_transport("browser-bridge", present)`` in
     ``register()``. Inactive until the operator explicitly sets
     ``security.approval.transport: browser-bridge`` in config.yaml (see
     docs/security.md); once selected, Hermes routes ITS OWN human-approval
     needs elsewhere in the system (a dangerous shell command, a plugin's
     ``request_tool_approval`` escalation, MCP elicitation surfaced via the
     CLI/gateway path) through this popup too. Those requests carry no
     origin/capability -- there is nothing browser-specific about "may I run
     rm -rf /tmp/x" -- so ``present()`` reports them under a fixed pseudo-
     origin/capability (``_HOST_PSEUDO_ORIGIN``/``_HOST_PSEUDO_CAPABILITY``)
     with the real, already-host-redacted command/description text in
     summary/detail. This is a bonus surface, not something any tool in this
     plugin depends on.

Both paths funnel into ``_present_to_devices()``, which does the actual
relay.call("approval.request", ...) delivery/broadcast and blocks on the
correlated ``approval.respond``.

Design invariants (plan §6, non-negotiable -- unchanged by this migration):
  * Never default-allow. Every exit path other than an explicit once/session/
    always response from the user returns ``allowed=False`` -- including no
    device connected, a delivery failure, a disconnect mid-approval, a host-
    side timeout, and a host-side worker-capacity/validation failure.
  * The gateway's grants table is authoritative, not the extension's say-so
    (state.py's own docstring: "the popup is UX; this database is law"). The
    'always' scope writes to that table (``state.set_grant``); 'session'
    writes to ``state.session_grants`` -- both still bridge-owned policy,
    never duplicated by Hermes (its own once/session/always persistence is
    keyed by session_key+pattern_key only, with no notion of "origin", and
    "always" there writes into the OPERATOR'S OWN ``command_allowlist`` --
    the wrong place for a per-origin browser grant, so ``require()``
    deliberately does not use ``tools.approval.request_tool_approval``).
  * A late or duplicate ``approval.respond`` must never flip a decision that
    already went the other way -- enforced here by ``resolve()``'s
    check-and-set under ``_lock`` (first writer wins), and independently
    re-checked by the host's own digest/allowed-choices validation inside
    ``invoke_approval_transport``.
  * Every request and every resolution is audited, including timeouts,
    disconnects, and rejected/duplicate responses.

PUBLIC CONTRACT (workstream G / hermes_plugin/tools.py + session_powers.py
code against this -- flag any deviation back before changing it):

    require(device_id, origin, capability, summary, session_key, detail="") -> Decision

    Decision.allowed: bool
    Decision.reason:  str
    Decision.scope:   one of "once" | "session" | "always" | "deny" | "timeout"
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from . import audit, config, state

logger = logging.getLogger(__name__)

TRANSPORT_NAME = "browser-bridge"

SCOPES = ("once", "session", "always", "deny", "timeout")
_RESPONSE_CHOICES = ("once", "session", "always", "deny")

# G0.5's per-capability ceiling. A pre-existing 'full' origin grant (whether
# from config's `default_mode` or an earlier plain "always") does NOT imply
# any of these -- tools.py's `_gate_reason`/`_authorize` must not early-return
# for a 'full' origin when the capability being checked is one of these, and
# `_apply_result` below must not let an "always" choice for one of these
# widen the origin the way a normal "always" does. Members and rationale
# (plan's own words, not restated per-item): `upload`/`evaluate`/`http_auth`
# are destructive or credential-adjacent primitives with no natural ceiling
# of their own; `cookies_write` mutates a site's auth state; `dialog` accepts
# something a page put in front of the user; `downloads`/`console` are both
# broad reads that would otherwise reach a never-configured origin with no
# prompt at all, because `config.py`'s `default_mode` is `"full"`. Mirrored
# (as a literal set, not derived from anything) in `protocol/schema.json`'s
# `approval.request.capability` enum and in
# `extension/src/popup/approvals-ui.ts`'s own copy of this same set -- see
# that file's comment for why it isn't imported instead.
DANGEROUS_CAPABILITIES = frozenset({
    "upload", "evaluate", "cookies_write", "http_auth", "dialog", "downloads", "console",
})

# A strict subset of DANGEROUS_CAPABILITIES for which even a per-capability
# standing grant is refused: arbitrary code execution (`evaluate`), a file
# handed to a site off the operator's own disk (`upload`), and a saved
# credential prompt (`http_auth`) are not things a single approve-once prompt
# can meaningfully authorize forever, or even for the rest of a Hermes
# session. `require()` below builds its `ApprovalRequest` with
# `allow_session`/`allow_permanent` set to `False` for these, so the refusal
# is enforced by the HOST's own `allowed_choices` validation (`resolve()`'s
# `choice not in waiter.request.allowed_choices` check) -- a compromised or
# out-of-date popup that sends "always"/"session" anyway is rejected
# server-side, not merely hidden client-side.
NO_STANDING_GRANT_CAPABILITIES = frozenset({"evaluate", "upload", "http_auth"})

# Used only by present() (the registered, host-driven transport path -- see
# module docstring, caller #2): a host ApprovalRequest carries no browser
# origin or one of protocol/schema.json's capability-enum values, so a
# request that arrives this way is reported under this fixed placeholder
# rather than inventing a fake real-looking origin. "act" is the closest
# semantic analog of "Hermes wants to do a thing" among the enum's values.
_HOST_PSEUDO_ORIGIN = "hermes-agent://approval"
_HOST_PSEUDO_CAPABILITY = "act"

DENY_UNREACHABLE = "no paired device is connected — there is no one to ask for approval"
DENY_DELIVERY_FAILED = "the approval request could not be delivered to any connected device"
DENY_DISCONNECTED = "the device disconnected before the user answered"
DENY_TIMEOUT = "the user did not respond before the approval expired"

_FAILURE_REASONS = {
    "timeout": DENY_TIMEOUT,
    "busy": "the approval system is at capacity (too many concurrent approvals); try again shortly",
    "interrupted": "the approval was interrupted before anyone answered",
    "error": "the approval presenter raised an error; refused, not silently allowed",
    "invalid": "the device sent back an invalid or out-of-scope decision; refused, not silently allowed",
    "stale": "the device's decision didn't match this exact request; refused, not silently allowed",
}


@dataclass(frozen=True)
class Decision:
    """The outcome of one ``require()`` call. Immutable so a decision, once
    handed back to a tool handler, can't be quietly mutated by anything else
    still holding a reference to it (e.g. a slow finally-block)."""

    allowed: bool
    reason: str
    scope: str  # one of SCOPES

    def __post_init__(self) -> None:
        if self.scope not in SCOPES:
            raise ValueError(f"invalid Decision.scope: {self.scope!r}")


# -- belt-and-braces redaction ------------------------------------------------
#
# The host already force-redacts command/description before ``present()``
# ever sees them (tools/approval_prompt.py's ``_present_with_selected_transport``
# calls ``redact_sensitive_text(..., force=True)``), and our own ``require()``
# callers are documented (protocol/schema.json's approval.request) as passing
# already-redacted display text. This is an independent second pass, in the
# same spirit as tools.py's gateway-side redaction re-check on snapshots: a
# caller bug (ours or the host's) must not put a live credential in the audit
# log or the popup. Deliberately self-contained (no import from tools.py) to
# avoid a tools<->approvals import cycle.
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_KV_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)\b\s*[:=]\s*\S+"
)
_REDACTED = "[redacted]"


def _scrub(text: str) -> str:
    if not text:
        return ""
    text = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}: {_REDACTED}", text)
    text = _CARD_RE.sub(_REDACTED, text)
    text = _SSN_RE.sub(_REDACTED, text)
    return text


# -- in-memory correlation ----------------------------------------------------
#
# Deliberately NOT persisted (see module docstring): the host's
# invoke_approval_transport() worker thread already bounds the wait, and a
# process restart kills the blocked gateway-worker thread that owns this
# waiter exactly like it kills everything else in that process. There is
# nothing to reap on the next boot because there is nothing left waiting for
# an answer -- unlike the old sqlite ``approvals`` table, an empty in-memory
# dict after a restart is simply the TRUTH, not a phantom to sweep.
class _Waiter:
    __slots__ = (
        "request", "remaining_devices", "event", "decision",
        "origin", "capability", "expires_at",
    )

    def __init__(self, request: Any, remaining_devices: Sequence[str], origin: str, capability: str) -> None:
        self.request = request
        self.remaining_devices = set(remaining_devices)
        self.event = threading.Event()
        self.decision: Optional[Any] = None  # ApprovalDecision once resolved
        self.origin = origin
        self.capability = capability
        self.expires_at = int(time.time() * 1000) + int(float(request.timeout_seconds) * 1000)


_lock = threading.Lock()
_waiters: Dict[str, _Waiter] = {}

# request_id -> (free-text reason, scope) for the auto-deny that just
# resolved it. Exists only because the host's ApprovalDecision/
# ApprovalTransportResult (hermes_cli/approval_transport.py) deliberately
# carries nothing richer than a scope choice ("once"/"session"/"always"/
# "deny") or a short failure code -- no room for "the device disconnected"
# vs "nobody was connected" vs a plain user "deny", and (because
# require()/_present_to_devices races its OWN internal wait to give up at or
# before the host's deadline -- see that function's `deadline` param
# docstring -- so a genuine TTL timeout always resolves as a plain "deny"
# ApprovalDecision rather than a host-detected `failure="timeout"`) no way
# to tell a real timeout apart from any other auto-deny either. require()'s
# _apply_result() pops its own entry (keyed by the ONE request_id it's
# resolving) right after the host call returns, so this never grows unless a
# caller abandons a request without ever reading the result back (bounded
# and harmless -- a handful of short (reason, scope) pairs at worst).
_deny_meta: Dict[str, tuple] = {}


def _connected_device_ids() -> List[str]:
    from . import relay as relay_mod

    relay = relay_mod.get_relay()
    if relay is None:
        return []
    return [entry["device_id"] for entry in relay.status().get("connected", [])]


def _deliver(request: Any, device_ids: Sequence[str], origin: str, capability: str,
             summary: str, detail: str, delivery_timeout: float) -> List[str]:
    """Best-effort broadcast of one ``approval.request`` wire frame to
    ``device_ids``. Returns the subset that actually queued it (a
    ``relay.call`` ack), which may be empty, one, or (present()'s multi-
    device broadcast case) several."""
    from . import relay as relay_mod

    relay = relay_mod.get_relay()
    if relay is None:
        return []
    wire = {
        "approval_id": request.request_id,
        "origin": origin,
        "capability": capability,
        "summary": summary,
        "detail": detail,
        "expires_at": int(time.time() * 1000) + int(float(request.timeout_seconds) * 1000),
    }
    reached: List[str] = []
    for device_id in device_ids:
        if not device_id:
            continue
        try:
            relay.call(device_id, "approval.request", wire, timeout=delivery_timeout)
            reached.append(device_id)
        except relay_mod.BridgeError as exc:
            audit.record(
                "approval_delivery_failed", approval_id=request.request_id, device=device_id,
                origin=origin, capability=capability, reason=str(exc),
            )
    return reached


def _present_to_devices(
    request: Any, device_ids: Sequence[str], origin: str, capability: str,
    summary: str, detail: str, delivery_timeout: float, deadline: Optional[float] = None,
) -> Any:
    """Deliver + block for the matching ``approval.respond`` -- the one
    presentation primitive both ``require()`` and ``present()`` share.

    Always returns an ``ApprovalDecision`` (``request.respond(...)``), never
    raises: every give-up path (nobody connected, delivery failed, nobody
    answered in time) resolves to "deny", matching plan §6's never-default-
    allow invariant. Must always return within roughly ``request.timeout_seconds``
    -- ``invoke_approval_transport`` runs this on a bounded worker thread and
    only releases that slot once this function returns (see
    ``hermes_cli/approval_transport.py``'s ``invoke_approval_transport``
    docstring); a present_fn that never returns leaks a worker slot forever.

    ``deadline`` (a ``time.monotonic()`` value) lets ``require()`` hand us the
    EXACT same deadline it gave ``invoke_approval_transport`` (captured right
    before calling it, so a few microseconds ahead of the host's own -- see
    that function's own comment). Without this we'd instead start our own
    ``request.timeout_seconds``-long wait here, strictly AFTER delivery has
    already eaten into the budget, so our internal timeout would fire LATER
    than the host's -- meaning the host would already have returned "timeout"
    (and ``_apply_result`` already have looked up, and missed, this request's
    ``_deny_meta`` entry) by the time we got around to writing it. Falls
    back to a fresh ``request.timeout_seconds`` wait when no deadline is
    given (``present()``'s path, where the host never tells us its own
    deadline at all -- see its own ``finally`` cleanup for why that's fine).
    """
    if deadline is None:
        deadline = time.monotonic() + float(request.timeout_seconds)
    device_ids = [d for d in device_ids if d]
    if not device_ids:
        audit.record(
            "approval_resolved", approval_id=request.request_id, origin=origin, capability=capability,
            scope="deny", allowed=False, reason=DENY_UNREACHABLE,
        )
        _deny_meta[request.request_id] = (DENY_UNREACHABLE, "deny")
        return request.respond("deny")

    waiter = _Waiter(request, device_ids, origin, capability)
    with _lock:
        _waiters[request.request_id] = waiter
    try:
        reached = _deliver(request, device_ids, origin, capability, summary, detail, delivery_timeout)
        if not reached:
            audit.record(
                "approval_resolved", approval_id=request.request_id, origin=origin, capability=capability,
                scope="deny", allowed=False, reason=DENY_DELIVERY_FAILED,
            )
            _deny_meta[request.request_id] = (DENY_DELIVERY_FAILED, "deny")
            return request.respond("deny")
        with _lock:
            waiter.remaining_devices = set(reached)

        remaining = max(0.0, deadline - time.monotonic())
        answered = waiter.event.wait(timeout=remaining)
        with _lock:
            decision = waiter.decision
        if answered and decision is not None:
            return decision

        audit.record(
            "approval_resolved", approval_id=request.request_id, origin=origin, capability=capability,
            scope="timeout", allowed=False, reason=DENY_TIMEOUT,
        )
        _deny_meta[request.request_id] = (DENY_TIMEOUT, "timeout")
        return request.respond("deny")
    finally:
        with _lock:
            _waiters.pop(request.request_id, None)


def present(request: Any) -> Any:
    """The ``browser-bridge`` Hermes approval transport (``ctx.
    register_approval_transport`` in ``register()``). See module docstring,
    caller #2, for why this reports a pseudo origin/capability."""
    cfg = config.load()
    delivery_timeout = max(1.0, float(cfg["approval_delivery_timeout_seconds"]))
    summary = _scrub(str(request.command or ""))
    description = _scrub(str(request.description or ""))
    detail = description if description and description != summary else ""
    try:
        return _present_to_devices(
            request, _connected_device_ids(), _HOST_PSEUDO_ORIGIN, _HOST_PSEUDO_CAPABILITY,
            summary, detail, delivery_timeout,
        )
    finally:
        # Unlike require(), nothing downstream of the host's own approval
        # engine ever reads _deny_meta for a request that arrived THIS way
        # (there is no _apply_result call on this path) -- pop it here
        # instead, purely so an auto-deny never leaves a stale entry behind.
        _deny_meta.pop(request.request_id, None)


def require(
    device_id: str,
    origin: str,
    capability: str,
    summary: str,
    session_key: str,
    detail: str = "",
) -> Decision:
    """Block until the user answers this approval, or it times out.

    Runs synchronously on the caller's own thread (a gateway worker thread in
    production, ``asyncio.to_thread`` in tests). Builds a host
    ``ApprovalRequest`` and drives ``invoke_approval_transport`` directly
    (see module docstring, caller #1, for why this doesn't go through
    Hermes' transport-selection gate).
    """
    summary = _scrub(str(summary or ""))
    detail = _scrub(str(detail or ""))
    device_id = str(device_id or "")
    origin = str(origin or "")
    capability = str(capability or "")
    session_key = str(session_key or "")

    cached_capability = _check_capability_grant(device_id, origin, capability)
    if cached_capability is not None:
        return cached_capability

    cached = _check_session_grant(device_id, session_key, origin, capability)
    if cached is not None:
        return cached

    cfg = config.load()
    ttl_seconds = max(1, int(cfg["approval_ttl_seconds"]))
    delivery_timeout = max(1.0, float(cfg["approval_delivery_timeout_seconds"]))

    try:
        from hermes_cli.approval_transport import ApprovalRequest, invoke_approval_transport
    except ImportError as exc:
        reason = (
            f"the host's approval-transport contract (hermes_cli.approval_transport) is not "
            f"available on this gateway ({type(exc).__name__}); {capability} is refused rather "
            f"than silently allowed"
        )
        audit.record(
            "grant_check", device=device_id, origin=origin, capability=capability, mode="request",
            decision="deny", reason="hermes_cli.approval_transport unavailable",
        )
        return Decision(allowed=False, reason=reason, scope="deny")

    # G0.5.4: evaluate/upload/http_auth never get a standing grant, session-
    # or origin-lifetime. Building the host's own ApprovalRequest with these
    # False (rather than filtering `_RESPONSE_CHOICES` after the fact) means
    # the refusal is enforced by `resolve()`'s existing
    # `choice not in waiter.request.allowed_choices` check -- the same path
    # that already rejects a stale/replayed response -- so there is exactly
    # one place that validates what choices a given request will accept.
    allow_standing = capability not in NO_STANDING_GRANT_CAPABILITIES
    request = ApprovalRequest.create(
        command=f"browser_bridge_{capability} at {origin}",
        description=summary + (f" — {detail}" if detail else ""),
        pattern_key=f"browser_bridge:{capability}",
        pattern_keys=(f"browser_bridge:{capability}", f"browser_bridge:origin:{origin}"),
        session_key=session_key,
        surface="browser-bridge",
        allow_session=allow_standing,
        allow_permanent=allow_standing,
        timeout_seconds=float(ttl_seconds),
    )
    audit.record(
        "approval_requested", approval_id=request.request_id, device=device_id, origin=origin,
        capability=capability, summary=summary, session=session_key, timeout_seconds=ttl_seconds,
    )

    # Captured immediately before invoke_approval_transport computes its OWN
    # deadline from the same timeout_seconds -- see _present_to_devices'
    # `deadline` param docstring for why sharing this (instead of letting our
    # worker start a fresh, later-starting wait of its own) matters.
    deadline = time.monotonic() + float(ttl_seconds)
    try:
        result = invoke_approval_transport(
            lambda req: _present_to_devices(
                req, [device_id], origin, capability, summary, detail, delivery_timeout, deadline,
            ),
            request,
            timeout_seconds=float(ttl_seconds),
        )
    except Exception as exc:  # the presentation path itself blew up -- never silently allow
        reason = f"approval request failed ({type(exc).__name__}: {exc}); {capability} is refused, not silently allowed"
        audit.record(
            "grant_check", device=device_id, origin=origin, capability=capability, mode="request",
            decision="deny", reason=f"invoke_approval_transport raised {type(exc).__name__}",
        )
        return Decision(allowed=False, reason=reason, scope="deny")

    return _apply_result(device_id, origin, capability, session_key, request.request_id, result)


def has_standing_grant(device_id: str, session_key: str, origin: str, capability: str) -> bool:
    """Public counterpart of `require()`'s own pre-checks (`_check_capability_grant`/
    `_check_session_grant`), exposed so a caller can ask "would `require()` skip the
    live prompt for this?" WITHOUT actually presenting one.

    speedimprovements.md A1: `browser_bridge_act`'s batched `steps` preflight
    (tools.py's `_act_would_need_live_prompt`) needs exactly this -- refusing the
    whole batch up front when a step's target would need a fresh approval, but
    not when a standing 'always'/session grant already covers it. Never itself a
    substitute for `_authorize`/`require()`, which still run for real once a step
    executes.
    """
    if _check_capability_grant(device_id, origin, capability) is not None:
        return True
    if session_key and _check_session_grant(device_id, session_key, origin, capability) is not None:
        return True
    return False


def _apply_result(
    device_id: str, origin: str, capability: str, session_key: str, request_id: str, result: Any,
) -> Decision:
    """Translate a host ``ApprovalTransportResult`` into our ``Decision``,
    applying the origin-promotion / session-grant side effects on an actual
    human "session"/"always" choice. Failure branches (timeout, no device,
    host-side validation failure) were already audited inside
    ``_present_to_devices``/``resolve()``, or -- for a rare host-validation
    failure that never reached ``resolve()`` at all -- are audited here.
    Always pops ``_deny_meta[request_id]`` (see its module-level comment) so
    a completed request never leaks that bookkeeping.

    A genuine local TTL timeout arrives here as a plain ``choice="deny"``
    with ``result.failure is None`` -- see ``_present_to_devices``'
    ``deadline`` param docstring for why our own giveup always resolves
    before the host's independent timeout detection would fire -- so
    ``stashed`` (not ``result.failure``) is what turns that back into
    ``scope="timeout"`` instead of a generic "deny"."""
    stashed = _deny_meta.pop(request_id, None)  # (reason, scope) | None
    if result.failure is not None:
        reason = (stashed[0] if stashed else None) or _FAILURE_REASONS.get(
            result.failure, f"approval failed ({result.failure})"
        )
        if result.failure not in ("timeout",):
            # "timeout"/no-device/delivery-failed were already audited at the
            # point of failure inside _present_to_devices; a host-side
            # validation failure (busy/interrupted/error/invalid/stale) was
            # not, because it happens in invoke_approval_transport's own
            # bookkeeping after present() already returned.
            audit.record(
                "grant_check", device=device_id, origin=origin, capability=capability, mode="request",
                decision="deny", reason=f"transport_{result.failure}",
            )
        scope = "timeout" if result.failure == "timeout" else "deny"
        return Decision(allowed=False, reason=reason, scope=scope)

    choice = result.choice
    allowed = choice != "deny"
    if allowed:
        reason, scope = "", choice
    elif stashed is not None:
        reason, scope = stashed
    else:
        reason, scope = "user denied", "deny"
    if choice == "always" and capability in DANGEROUS_CAPABILITIES:
        # G0.5.3: the ceiling wins. A dangerous capability's "always" never
        # promotes the origin to 'full' (that would hand every OTHER
        # capability a free pass through the very ceiling this exists to
        # enforce) -- it writes a per-capability grant instead, TTL-bounded
        # the same way the 'session' scope already is (there is no reliable
        # "revoke this later" hook per plugin-api-findings.md, so an
        # unbounded standing grant would outlive the user's intent to give
        # it). `require()` already refused to offer this choice at all for
        # NO_STANDING_GRANT_CAPABILITIES, so reaching here with one of those
        # would mean the host's own allowed_choices validation was bypassed
        # -- treat that as a bug worth surfacing, not something to special-
        # case quietly.
        assert capability not in NO_STANDING_GRANT_CAPABILITIES, (
            f"host allowed an 'always' choice for {capability!r}, which require() built with "
            f"allow_permanent=False -- resolve()'s allowed_choices check should have rejected this"
        )
        ttl_seconds = max(1, int(config.load()["approval_session_grant_ttl_hours"])) * 3600
        state.set_capability_grant(device_id, origin, capability, ttl_seconds)
        audit.record(
            "grant_set", device=device_id, origin=origin, capability=capability, mode="capability",
            source="approval_always",
        )
    elif choice == "always":
        state.set_grant(device_id, origin, "full")
        audit.record("grant_set", device=device_id, origin=origin, mode="full", source="approval_always")
    elif choice == "session":
        ttl_seconds = max(1, int(config.load()["approval_session_grant_ttl_hours"])) * 3600
        state.set_session_grant(device_id, session_key, origin, capability, ttl_seconds)
    audit.record(
        "grant_check", device=device_id, origin=origin, capability=capability, mode="request",
        decision="allow" if allowed else "deny", scope=scope, reason=reason,
    )
    return Decision(allowed=allowed, reason=reason, scope=scope)


def _check_capability_grant(device_id: str, origin: str, capability: str) -> Optional[Decision]:
    """The per-capability counterpart of `_check_session_grant`, for a
    DANGEROUS_CAPABILITIES "always" choice recorded by `_apply_result`.
    Harmless to call for a non-dangerous capability -- `state.
    get_capability_grant` only ever has rows for capabilities `_apply_result`
    actually wrote, i.e. members of DANGEROUS_CAPABILITIES -- but the
    membership check is still explicit here rather than relying on that, so
    this function's own behaviour doesn't quietly depend on nothing else
    ever writing to that table."""
    if capability not in DANGEROUS_CAPABILITIES:
        return None
    if capability in NO_STANDING_GRANT_CAPABILITIES:
        # The write side already refuses to create one of these (require()
        # builds the ApprovalRequest with allow_permanent=False, and the
        # host's own allowed_choices validation rejects an "always" that
        # arrives anyway). Re-checking on READ is not redundant: it is the
        # difference between "no code path writes such a row today" and "such
        # a row is never honoured". A migration, an admin fix applied
        # straight to the database, or a future capability wired in by
        # copying the "always" path without this exclusion would otherwise
        # hand out standing, never-again-prompted arbitrary code execution
        # until the row's TTL expired.
        audit.record(
            "standing_grant_refused", device=device_id, origin=origin, capability=capability,
            detail="a stored grant exists for a capability that may never have one; re-prompting",
        )
        return None
    row = state.get_capability_grant(device_id, origin, capability)
    if row is None:
        return None
    audit.record("approval_capability_reuse", device=device_id, origin=origin, capability=capability)
    return Decision(allowed=True, reason="approved earlier ('always' for this capability)", scope="always")


def _check_session_grant(device_id: str, session_key: str, origin: str, capability: str) -> Optional[Decision]:
    if not session_key:
        return None
    if capability in NO_STANDING_GRANT_CAPABILITIES:
        # Same reasoning as _check_capability_grant's own exclusion: a
        # capability that may never hold a standing grant must not be granted
        # one by a row that already exists, whatever put it there.
        audit.record(
            "standing_grant_refused", device=device_id, origin=origin, capability=capability,
            session=session_key,
            detail="a stored session grant exists for a capability that may never have one; re-prompting",
        )
        return None
    row = state.get_session_grant(device_id, session_key, origin, capability)
    if row is None:
        return None
    audit.record(
        "approval_session_reuse", device=device_id, origin=origin, capability=capability, session=session_key,
    )
    return Decision(allowed=True, reason="approved earlier this Hermes session", scope="session")


def resolve(device_id: str, approval_id: str, choice: str) -> bool:
    """Apply the extension's answer to a pending approval.

    Called from ``Relay._on_approval_respond`` on the relay's asyncio loop
    thread. Returns False -- and audits why -- for anything that must not be
    allowed to affect a decision: an unknown id, an id not currently
    targeting this device, an unrecognised or out-of-scope choice, or an id
    that's already resolved (a late answer racing a timeout, or a replayed
    frame). The check-and-set under ``_lock`` is what makes "at most one
    response ever counts" true; the host's own ``invoke_approval_transport``
    independently re-validates digest/choice as a second line of defense.

    Expiry is checked here against ``waiter.expires_at`` directly, rather
    than relying on ``waiter.decision`` having already been set by
    ``_present_to_devices``'s own timeout branch. Those are two independent
    clocks racing on two independent threads: ``invoke_approval_transport``
    (host code) polls a ``queue.Queue`` on the ORIGINAL caller's thread and
    gives up the instant its own ``deadline - time.monotonic() <= 0``, with
    no dependency on anything else being scheduled -- while our own
    cleanup (audit.record, ``_deny_meta`` write, popping ``_waiters``) only
    runs once the WORKER thread blocked in ``waiter.event.wait()`` actually
    wakes up and gets CPU time to execute it. Under load, that worker can
    lag well behind the host's own timeout returning control to
    ``require()``'s caller, leaving the waiter fully "live" (still in
    ``_waiters``, ``decision`` still ``None``) for a window that isn't
    microseconds -- it's however long the worker thread takes to get
    scheduled. A late response landing in that window must still be
    rejected, so expiry is authoritative here independent of which thread
    got there first.

    Both ``waiter.expires_at`` and ``now_ms`` here read wall-clock
    ``time.time()`` (matching the host's own request/response timestamps),
    not ``time.monotonic()`` (used for ``_present_to_devices``'s internal
    wait deadline) -- a backward system clock jump between the two reads
    could widen this race's window rather than close it, and a forward
    jump only makes this check fail closed (reject) earlier than strictly
    necessary, never fail open.
    """
    approval_id = str(approval_id or "")
    now_ms = int(time.time() * 1000)
    with _lock:
        waiter = _waiters.get(approval_id)
        if waiter is None:
            already_resolved = False
        elif waiter.decision is not None or now_ms >= waiter.expires_at:
            already_resolved = True
        elif device_id not in waiter.remaining_devices:
            audit.record(
                "approval_response_rejected", approval_id=approval_id, device=device_id, reason="device_mismatch",
            )
            return False
        elif choice not in _RESPONSE_CHOICES or choice not in waiter.request.allowed_choices:
            audit.record(
                "approval_response_rejected", approval_id=approval_id, device=device_id,
                reason="invalid_choice", choice=choice,
            )
            return False
        else:
            waiter.decision = waiter.request.respond(choice)
            already_resolved = False

    if waiter is None:
        audit.record("approval_response_rejected", approval_id=approval_id, device=device_id, reason="unknown_id")
        return False
    if already_resolved:
        audit.record(
            "approval_response_rejected", approval_id=approval_id, device=device_id, reason="already_resolved",
        )
        return False

    allowed = choice != "deny"
    audit.record(
        "approval_resolved", approval_id=approval_id, device=device_id, origin=waiter.origin,
        capability=waiter.capability, scope=choice, allowed=allowed,
    )
    waiter.event.set()
    return True


def deny_pending_for_device(device_id: str, reason: str = "device disconnected mid-approval") -> int:
    """A device dropping its connection while it has approvals outstanding is
    a denial, not a hang -- called from ``Relay._drop()``. A request
    broadcast to several devices (``present()``'s host-transport path) only
    finalizes as a denial once EVERY targeted device has dropped; this
    plugin's own single-device ``require()`` requests always deny
    immediately, matching the pre-migration behaviour. Returns the number of
    approvals this actually resolved (0 is the common case)."""
    with _lock:
        candidates = [w for w in _waiters.values() if device_id in w.remaining_devices]
        resolved: List[_Waiter] = []
        for waiter in candidates:
            waiter.remaining_devices.discard(device_id)
            if waiter.decision is not None or waiter.remaining_devices:
                continue
            waiter.decision = waiter.request.respond("deny")
            resolved.append(waiter)

    for waiter in resolved:
        audit.record(
            "approval_resolved", approval_id=waiter.request.request_id, device=device_id,
            origin=waiter.origin, capability=waiter.capability, scope="deny", allowed=False, reason=reason,
        )
        # See _deny_meta's module-level comment: the host's ApprovalDecision
        # has no room for this free text, so _apply_result reads it back out
        # by request_id once invoke_approval_transport returns control to
        # require(). Set BEFORE waiter.event.set() so require()'s thread can
        # never observe the decision without the reason already stashed.
        _deny_meta[waiter.request.request_id] = (reason, "deny")
        waiter.event.set()
    return len(resolved)


def list_pending(device_id: str = "") -> List[dict]:
    """In-memory snapshot of in-flight approvals, for ``browser_bridge_status``
    / future CLI use. Never persisted -- see module docstring."""
    with _lock:
        return [
            {
                "id": waiter.request.request_id,
                "origin": waiter.origin,
                "capability": waiter.capability,
                "devices": sorted(waiter.remaining_devices),
                "expires_at": waiter.expires_at,
            }
            for waiter in _waiters.values()
            if not device_id or device_id in waiter.remaining_devices
        ]
