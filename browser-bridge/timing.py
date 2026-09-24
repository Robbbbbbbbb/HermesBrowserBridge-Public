"""E1 (speedimprovements.md): per-tool-call timing, gateway side.

The goal: every ``browser_bridge_*`` tool result carries a ``timing`` object
— ``{total_ms, relay_ms, extension_ms, ...}`` — with total_ms measured at the
gateway (wall-clock time for the whole tool-handler call) and, when the
extension reported its own handling time on this call's wire response
(offscreen.ts's ``runTimed``, protocol/schema.json's per-method ``timing``
result field), split into ``relay_ms`` (everything outside the extension:
websocket/serialization/gateway bookkeeping) and ``extension_ms`` (the
extension's own ``total_ms``). Sub-phase fields the extension attaches for
some methods (``cdp_ms``/``settle_ms``/``animation_ms``) are passed through
unchanged.

Two moving pieces, one thread-local each tool-handler invocation ties
together:

  1. ``relay.Relay.call()`` is the ONE place every gateway->extension
     request goes through (``hermes_plugin/relay.py``). It calls
     ``record_relay_call()`` here after every round trip, win or lose.
  2. ``tools.py``'s ``register_tools()`` wraps every handler it registers
     (its own, and every sibling module's, via a proxied ``ctx`` — see
     ``_TimingProxy``) so it never has to be repeated per module. The
     wrapper calls ``reset()`` before invoking the handler and ``snapshot()``
     after, computes ``total_ms`` for the whole handler call, and merges the
     result into whatever JSON the handler already returned.

A handler that never calls ``relay.call()`` at all (a pure gateway-side
check) still gets a ``timing`` object — just one with no ``relay_ms``/
``extension_ms``, only ``total_ms`` for the handler's own gateway-side work.
Never logs page content: every field this module produces and every field
``audit.record("tool_timing", ...)`` writes is a tool name, a device id, and
numbers.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

_local = threading.local()

# Sub-phase keys the extension may attach to its own `timing` field (per
# protocol/schema.json's page.act/dom.snapshot result.timing) and that are
# simply forwarded, unchanged, onto the gateway's own timing object —
# `extension_ms` and the ones below are the entire allowed set; anything
# else the extension ever sent under `timing` is intentionally dropped here
# rather than silently forwarding an unbounded, untyped bag of fields to the
# model on every tool call.
_FORWARDED_EXTENSION_SUBFIELDS = ("cdp_ms", "settle_ms", "animation_ms")


def reset() -> None:
    """Start a fresh accumulator for one tool-handler invocation."""
    _local.relay_wall_ms = 0.0
    _local.extension_total_ms: Optional[float] = None
    _local.extension_subfields: Dict[str, float] = {}
    _local.calls = 0


def _ensure_reset() -> None:
    if not hasattr(_local, "relay_wall_ms"):
        reset()


def record_relay_call(elapsed_ms: float, extension_timing: Optional[Dict[str, Any]], method: str = "") -> None:
    """Called by ``relay.Relay.call()`` after every gateway->extension round
    trip, whether or not the tool handler that triggered it ever reads this
    back — a handler that never called ``reset()`` first (nothing here yet
    initialized) just starts a fresh accumulator implicitly, so a stray call
    from outside a wrapped tool handler never raises.
    """
    _ensure_reset()
    try:
        _local.relay_wall_ms += max(float(elapsed_ms), 0.0)
    except (TypeError, ValueError):
        pass
    _local.calls += 1
    if not isinstance(extension_timing, dict):
        return
    total = extension_timing.get("total_ms")
    if isinstance(total, (int, float)):
        # Sum across calls (a handler that calls relay.call() more than once,
        # e.g. release-then-attach) rather than keeping only the last one --
        # `extension_ms` should account for every millisecond the extension
        # itself reported across the whole tool call.
        _local.extension_total_ms = float(total) + (_local.extension_total_ms or 0.0)
    for key in _FORWARDED_EXTENSION_SUBFIELDS:
        value = extension_timing.get(key)
        if isinstance(value, (int, float)):
            _local.extension_subfields[key] = _local.extension_subfields.get(key, 0.0) + float(value)


def snapshot() -> Dict[str, Any]:
    """Read back everything accumulated since the last ``reset()``."""
    _ensure_reset()
    return {
        "relay_wall_ms": _local.relay_wall_ms,
        "extension_total_ms": _local.extension_total_ms,
        "extension_subfields": dict(_local.extension_subfields),
        "calls": _local.calls,
    }


def build_tool_timing(total_ms: float) -> Dict[str, float]:
    """Combine this handler invocation's own wall time with whatever
    ``relay.call()`` recorded into the ``timing`` object a tool result gets.

    ``relay_ms`` is deliberately clamped at 0: a slightly negative value from
    clock jitter (extension_total_ms very slightly exceeding the measured
    relay wall time) would otherwise read as a nonsensical negative
    round-trip cost.
    """
    snap = snapshot()
    extension_ms = snap["extension_total_ms"]
    if extension_ms is None:
        relay_ms = snap["relay_wall_ms"]
    else:
        relay_ms = max(snap["relay_wall_ms"] - extension_ms, 0.0)
    timing: Dict[str, float] = {
        "total_ms": round(float(total_ms), 1),
        "relay_ms": round(relay_ms, 1),
    }
    if extension_ms is not None:
        timing["extension_ms"] = round(extension_ms, 1)
    for key, value in snap["extension_subfields"].items():
        timing[key] = round(value, 1)
    return timing
