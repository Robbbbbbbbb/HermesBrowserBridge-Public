"""devices.md DV4: ``browser_bridge_devices`` -- the agent-facing half of
device priority. DV1-DV3 (liveness, priority state, resolution/failover)
already shipped in ``relay.py``/``state.py``/``tools.py``; this module is
the one new tool that lets the agent SEE that machinery (every paired
device's liveness/rank/current pick) and, within the user's rules (§1/§2 rule 5),
adjust it -- never rename or revoke a device (D1, out of scope here; that's
DV5's CLI-only ``devices rename``).

Three things this file is careful to get right, because DV3/DV2 already
solved them and re-solving them here would either drift or be a straight-up
re-implementation:

- ``current_pick`` (DV4.1) is computed by calling ``tools._resolve_device``
  itself -- the SAME function every other tool's implicit selection goes
  through -- with ``audit_writes=False`` so a mere preview never writes a
  ``device_selected``/``device_selection_refused`` line for a call nothing
  actually made. Its thread-local resolution meta is reset immediately after
  reading the device id, so a preview can never leak a stray ``device``/
  ``failed_over_from`` block into THIS tool's own result via
  ``_wrap_handler_with_timing``'s splice (see that function's docstring).
- The session-priority tier this file reads/writes is keyed on the SAME raw
  Hermes session identifier ``tools._resolve_device`` itself reads via
  ``tools._session_key(kwargs)`` -- not a resolved ``agent_sessions.id`` --
  because that is what DV3's ``session_id_for_order`` already is. Writing
  under any other key would silently never be seen by resolution.
- Validation (duplicate ids, unknown/revoked ids, the pin gate) is entirely
  ``state.py``'s (``_validate_priority_order``, ``set_global_priority``'s
  ``PriorityPinnedError``) -- this module only adds device NAME resolution
  on top, then lets those raise.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import audit, protocol, relay as relay_mod, state
from . import tools as tools_mod

TOOLSET = tools_mod.TOOLSET

DEVICES_SCHEMA = {
    "name": "browser_bridge_devices",
    "description": (
        "List every paired device -- liveness, protocol, pause state, its global/session priority rank, "
        "and current_pick: true on whichever one an implicit (device-less) browser_bridge_* call would use "
        "right now -- or change device priority. action 'list' (default) also returns the current global "
        "and session priority orders and whether the global order is pinned. action 'set_priority' takes "
        "`order` (device ids or unambiguous names, most-preferred first -- an ambiguous or unknown name is "
        "refused with the candidates) and `scope` ('global' or 'session'). A 'global' write is refused with "
        "DEVICE_PRIORITY_PINNED while the user has pinned it -- use scope 'session' instead, which is never "
        "pinned. A session is bound to one device from its FIRST real browser_bridge_* call onward and "
        "never fails over (devices.md D0), so call set_priority with scope 'session' as your very FIRST "
        "tool call in a task to steer which device that first call binds to -- calling it after you're "
        "already bound to a device is accepted but has no further effect on your own picks. action "
        "'clear_priority' with scope 'session' removes this session's own override (falling back to the "
        "global order); scope 'global' is always refused -- only the operator (CLI/popup) can clear the "
        "global order. Never renames or revokes a device -- that is the operator's own CLI command."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "set_priority", "clear_priority"],
                "description": "Defaults to 'list'.",
            },
            "order": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "set_priority only: device ids or names, most-preferred first. Case-insensitive name "
                    "match; an ambiguous or unknown name is refused, never guessed. No duplicates."
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["global", "session"],
                "description": "set_priority/clear_priority only: which order to write.",
            },
        },
        "required": [],
    },
}


# -- shared helpers -----------------------------------------------------------

def _current_pick(kwargs: Dict[str, Any]) -> Optional[str]:
    """devices.md DV4.1: "compute it with the same code path as
    _resolve_device, with no side effects and no audit, never a
    re-implementation." ``audit_writes=False`` suppresses every audit line
    _resolve_device would otherwise write for a real call; the thread-local
    resolution meta it may set is reset immediately after so it can never
    leak into THIS tool's own result (see module docstring)."""
    device_id, _err = tools_mod._resolve_device({}, kwargs, audit_writes=False)
    tools_mod._device_resolution.meta = None
    return device_id or None


def _priority_block(session_id: Optional[str]) -> Dict[str, Any]:
    pin = state.get_priority_pin()
    return {
        "global": state.get_global_priority(),
        "session": state.get_session_priority(session_id) if session_id else [],
        "pinned": pin["pinned"],
        "pinned_by": pin.get("pinned_by"),
    }


def _raw_session_id(kwargs: Dict[str, Any]) -> Optional[str]:
    """The raw Hermes session identifier -- exactly what DV3's
    `_resolve_device` keys its own session-priority tier on -- or None for a
    kwarg-less call. A pure read of `kwargs` (no DB access, no session
    creation), unlike `_resolve_session`, which is per-device and DOES
    create a row -- this file never needs that for reading, only for the
    'needs an open agent session' gate `_session_scope_id` below applies to
    a WRITE."""
    raw = tools_mod._session_key(kwargs)
    return raw if raw != tools_mod._DEFAULT_SESSION else None


def _session_scope_id(kwargs: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Resolve `scope: "session"`'s target identity: the SAME raw Hermes
    session identifier DV3's own `_resolve_device` keys its session-priority
    tier on (see `_raw_session_id`) -- not a device-bound
    `tools._resolve_session` call, which this tool cannot make (it has no
    device_id to resolve a session AGAINST; picking one is what the
    priority order decides).

    Refuses (a clear error, no side effects) only for a call carrying NO
    session identity at all (the shared default bucket -- "with none, give
    a clear error"), or one whose raw key already names a CLOSED
    `agent_sessions` row. Every other raw key is accepted, INCLUDING one
    that has never yet been bound to a device -- that is deliberately the
    common case: devices.md D0/§2 rule 6 pins a session to its device from
    its very FIRST real `browser_bridge_*` call onward, so calling
    `set_priority` BEFORE that first call is the only time a session
    override can actually steer which device that first call picks (this
    is why the SKILL.md note tells the agent to call this before other
    tools when it wants to prefer a device). A write against an
    ALREADY-established (open) session is accepted too -- harmless, just
    inert for THAT session's own future picks (D0 never re-homes it once
    bound), still visible via `list`'s `session_rank`.
    """
    raw = _raw_session_id(kwargs)
    if raw is None:
        return None, tools_mod._err(
            "session-scoped priority needs a session identity for this call (session_id/session_key) -- "
            "a call with no session at all has nothing to scope a session override to",
            code=protocol.INVALID_PARAMS,
        )
    session = state.get_agent_session(raw)
    if session is not None and session.get("closed_at") is not None:
        return None, tools_mod._err(
            f"session {raw!r} is closed and cannot take a new priority override -- start a new session",
            code=protocol.INVALID_PARAMS,
        )
    return raw, ""


def _resolve_order(order_in: Any) -> Tuple[Optional[List[str]], str]:
    """Resolve each entry of `order_in` (device id or name) to a device id.
    Case-insensitive name match against every NON-REVOKED paired device
    (revoked devices are already hidden from `list`, so a name only ever
    resolves among the ones the agent can actually see); an ambiguous or
    unknown name is refused with the candidates, never guessed. Duplicate
    ids/names in `order_in`, and unknown/revoked ids given directly, are
    left for `state.py`'s own `_validate_priority_order` to reject -- see
    module docstring on why that validation is not repeated here."""
    if not isinstance(order_in, list) or not order_in:
        return None, tools_mod._err(
            "order must be a non-empty list of device ids or names", code=protocol.INVALID_PARAMS,
        )
    rows = state.list_devices()
    by_id = {r["id"]: r for r in rows}
    by_name: Dict[str, List[str]] = {}
    for r in rows:
        by_name.setdefault((r.get("name") or "").lower(), []).append(r["id"])

    resolved: List[str] = []
    for raw in order_in:
        if not isinstance(raw, str) or not raw.strip():
            return None, tools_mod._err(f"invalid order entry: {raw!r}", code=protocol.INVALID_PARAMS)
        token = raw.strip()
        if token in by_id:
            resolved.append(token)
            continue
        matches = by_name.get(token.lower(), [])
        if len(matches) > 1:
            return None, tools_mod._err(
                f"device name {token!r} is ambiguous; specify device_id",
                code=protocol.INVALID_PARAMS,
                candidates=[{"device_id": m, "name": by_id[m].get("name")} for m in matches],
            )
        if len(matches) == 1:
            resolved.append(matches[0])
            continue
        return None, tools_mod._err(
            f"unknown device {token!r}",
            code=protocol.INVALID_PARAMS,
            hint="call browser_bridge_devices to see paired device ids/names",
        )
    return resolved, ""


def _device_entries(kwargs: Dict[str, Any]) -> List[Dict[str, Any]]:
    relay = relay_mod.get_relay()
    connected = relay.status()["connected"] if relay is not None else []
    connected_by_id = {c["device_id"]: c for c in connected}
    global_rank = {device_id: i for i, device_id in enumerate(state.get_global_priority())}
    raw_session = _raw_session_id(kwargs)
    session_rank = (
        {device_id: i for i, device_id in enumerate(state.get_session_priority(raw_session))}
        if raw_session
        else {}
    )
    pick = _current_pick(kwargs)

    entries: List[Dict[str, Any]] = []
    for device in state.list_devices():
        device_id = device["id"]
        conn = connected_by_id.get(device_id)
        online = conn is not None
        entries.append(
            {
                "device_id": device_id,
                "name": device.get("name") or device_id,
                "browser": device.get("browser") or "",
                "platform": device.get("platform") or "",
                "ext_version": device.get("ext_version") or "",
                "protocol_version": conn.get("protocol_version") if online else None,
                "protocol_up_to_date": bool(conn.get("protocol_up_to_date")) if online else None,
                "liveness": (conn.get("liveness") or "offline") if online else "offline",
                "last_heartbeat_age_s": conn.get("last_heartbeat_age_s") if online else None,
                "clock_skew_ms": conn.get("clock_skew_ms") if online else None,
                "paused": bool(conn.get("paused")) if online else None,
                "global_rank": global_rank.get(device_id),
                "session_rank": session_rank.get(device_id),
                "current_pick": device_id == pick,
            }
        )
    return entries


# -- action handlers -----------------------------------------------------------

def _handle_list(kwargs: Dict[str, Any]) -> str:
    raw_session = _raw_session_id(kwargs)
    return tools_mod._ok(devices=_device_entries(kwargs), priority=_priority_block(raw_session))


def _handle_set_priority(args: Dict[str, Any], kwargs: Dict[str, Any]) -> str:
    scope = str(args.get("scope") or "").strip().lower()
    if scope not in ("global", "session"):
        return tools_mod._err("scope must be 'global' or 'session'", code=protocol.INVALID_PARAMS)

    resolved, err = _resolve_order(args.get("order"))
    if err:
        return err

    if scope == "global":
        try:
            state.set_global_priority(resolved, set_by="agent")
        except state.PriorityPinnedError as exc:
            audit.record("device_priority_refused", scope="global", set_by="agent", order=resolved, reason="pinned")
            return tools_mod._err(
                str(exc), code=protocol.DEVICE_PRIORITY_PINNED, hint=tools_mod._hint_for_code(protocol.DEVICE_PRIORITY_PINNED),
            )
        except ValueError as exc:
            return tools_mod._err(str(exc), code=protocol.INVALID_PARAMS)
        audit.record("device_priority_set", scope="global", set_by="agent", order=resolved, session=None)
        raw_session = _raw_session_id(kwargs)
        return tools_mod._ok(priority=_priority_block(raw_session), current_pick=_current_pick(kwargs))

    session_id, err = _session_scope_id(kwargs)
    if err:
        return err
    try:
        state.set_session_priority(session_id, resolved)
    except ValueError as exc:
        return tools_mod._err(str(exc), code=protocol.INVALID_PARAMS)
    audit.record("device_priority_set", scope="session", set_by="agent", order=resolved, session=session_id)
    return tools_mod._ok(priority=_priority_block(session_id), current_pick=_current_pick(kwargs))


def _handle_clear_priority(args: Dict[str, Any], kwargs: Dict[str, Any]) -> str:
    scope = str(args.get("scope") or "").strip().lower()
    if scope == "global":
        audit.record("device_priority_refused", scope="global", set_by="agent", reason="operator_only")
        return tools_mod._err(
            "clearing the GLOBAL device priority order is an operator-only action -- set a session-scoped "
            "override instead (scope: 'session'), or ask the operator to clear it via the CLI/popup",
            code=protocol.DEVICE_PRIORITY_PINNED,
            hint=tools_mod._hint_for_code(protocol.DEVICE_PRIORITY_PINNED),
        )
    if scope != "session":
        return tools_mod._err("scope must be 'global' or 'session'", code=protocol.INVALID_PARAMS)

    session_id, err = _session_scope_id(kwargs)
    if err:
        return err
    state.clear_session_priority(session_id)
    audit.record("device_priority_set", scope="session", set_by="agent", order=[], session=session_id, cleared=True)
    return tools_mod._ok(priority=_priority_block(session_id), current_pick=_current_pick(kwargs))


def handle_devices(args: Dict[str, Any], **kwargs: Any) -> str:
    action = str(args.get("action") or "list").strip().lower()
    if action == "list":
        return _handle_list(kwargs)
    if action == "set_priority":
        return _handle_set_priority(args, kwargs)
    if action == "clear_priority":
        return _handle_clear_priority(args, kwargs)
    return tools_mod._err(
        f"unknown action {action!r}",
        code=protocol.INVALID_PARAMS,
        hint="action must be one of: list, set_priority, clear_priority",
    )


def register_devices_tools(ctx) -> List[str]:
    """Entry point ``tools.register_tools`` imports under
    ``try/except ImportError``, the same optional seam as
    ``register_http_auth_tools``/``register_session_tools``/etc."""
    registered: List[str] = []
    for schema, handler in ((DEVICES_SCHEMA, handle_devices),):
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            check_fn=tools_mod.bridge_available,
            emoji="\U0001F5A5",
        )
        registered.append(schema["name"])
    return registered
