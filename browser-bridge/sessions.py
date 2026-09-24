"""G3.1 "first-class sessions" (coveragegaps.md §G3.1): `browser_bridge_session`.

Today, before this module, "session" only meant a raw string Hermes happens
to pass as a tool-call kwarg (`tools.py`'s `_session_key`) — unlisted,
unnameable, and gone the moment that Hermes conversation ends. This module is
the tool surface for the persistent object `state.py`'s `agent_sessions`
table backs: create one, label it, list what's open on a device, resume a
labeled session from a brand-new conversation, rename or close one, or
describe what it's doing right now.

Every OTHER browser_bridge_* tool already resolves its own session identity
through `tools.py`'s `_resolve_session` (see that function's docstring) —
this module is additive, not a prerequisite: a caller that never touches
`browser_bridge_session` at all keeps working exactly as it did before this
shipped, under an automatically-registered session whose id is simply its own
raw Hermes kwarg.

Ownership (G3.1.4's IDOR protection): `resume`/`rename`/`close`/`describe` all
resolve their `session` argument through `_lookup_named_session`, which
refuses outright (`SESSION_ACCESS_DENIED`) the moment a resolved row's
`device_id` doesn't match the calling device — a session can only be acted on
from the device that owns it. A label lookup can't even leak past this: it is
scoped to the calling device_id in the SQL itself (`state.
find_agent_sessions_by_label`), so a label collision with another device's
session is invisible, not merely refused.

Workspace tabs (G3.3): `describe` now also returns `workspace` — this
session's own keyed/labeled tabs (`tabs.py`'s `build_workspace`, imported
the same optional-at-load-time way as everything else in this module),
scoped to ONLY the tabs THIS session itself attached. `attached_tabs` below
is kept alongside it, unscoped, for backward compatibility with anything
that already reads it — it lists whatever is CURRENTLY attached on the
device from `attach.py`'s in-memory registry, regardless of which session
attached it.

Ownership of this file: new and standalone, imported the same
optional-at-load-time way as `vision.py`/`session_powers.py`/`navigation.py`/
`http_auth.py`/`dialogs.py`/`upload.py`/`evaluate.py` — see
`tools.register_tools`'s own comment on why that seam exists.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import attach as attach_mod
from . import audit, config, protocol
from . import refusals
from . import state
from . import tools as tools_mod

TOOLSET = tools_mod.TOOLSET

SESSION_ACTIONS = ("create", "list", "resume", "rename", "close", "describe")

SESSION_SCHEMA = {
    "name": "browser_bridge_session",
    "description": (
        "Manage first-class, persistent agent sessions (G3.1) -- a nameable, resumable identity for "
        "'the work this conversation is doing on a device', independent of the browser_bridge_status "
        "device/tab bookkeeping. You never have to call this to use any other browser_bridge_* tool -- "
        "every call is already tracked under an automatically-registered session -- but calling it lets "
        "you: 'create' a labeled session up front (e.g. 'helpdesk ticket 4412'); 'list' what's open "
        "on a device; 'resume' a session created in a PAST conversation so a fresh conversation can pick "
        "up where it left off (any approvals older than the configured session-grant TTL still re-prompt "
        "-- resuming never bypasses that); 'rename' or 'close' one; or 'describe' one, which also shows "
        "the tabs currently attached on its device. A session can only be resumed, renamed, closed or "
        "described from the device that owns it -- naming another device's session refuses outright. A "
        "closed session refuses every further tool call, from any tool, with a named reason -- create a "
        "new one or resume a different open one. 'close' with close_opened_tabs=true (H4 'clean up "
        "after yourself') also closes every tab THIS session opened itself with browser_bridge_open_tab "
        "-- never a tab the user opened by hand."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(SESSION_ACTIONS),
                "description": "create | list | resume | rename | close | describe",
            },
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "session": {
                "type": "string",
                "description": (
                    "Which session to act on for resume/rename/close/describe -- its id (from 'list' or "
                    "'create'), or its label (case-insensitive exact match; ambiguous labels are refused "
                    "with the matching candidates listed). Not used by 'create' or 'list'."
                ),
            },
            "label": {
                "type": "string",
                "description": "Human label for 'create' (e.g. 'helpdesk ticket 4412') or the new label for 'rename'.",
            },
            "notes": {
                "type": "string",
                "description": "Optional free-text notes stored with a newly created session.",
            },
            "include_closed": {
                "type": "boolean",
                "description": "For 'list': also include closed sessions (default false).",
            },
            "close_opened_tabs": {
                "type": "boolean",
                "description": (
                    "For 'close' (H4 'clean up after yourself'): also close every tab THIS session "
                    "opened itself with browser_bridge_open_tab. Defaults to false. Never touches a "
                    "tab the user opened by hand, or one a different session opened."
                ),
            },
        },
        "required": ["action"],
    },
}


def _session_view(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "label": row.get("label") or "",
        "created_at": row.get("created_at"),
        "last_active_at": row.get("last_active_at"),
        "closed_at": row.get("closed_at"),
        "notes": row.get("notes") or "",
        "open": row.get("closed_at") is None,
    }


def _lookup_named_session(device_id: str, args: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """Resolve the `session` argument (id or label) to an agent_sessions row
    OWNED BY `device_id`, or (None, error_json). Every one of
    resume/rename/close/describe goes through this — see the module
    docstring's "Ownership" paragraph for why a label match can never even
    see another device's rows, and why an id match that resolves to one is
    refused rather than silently acted on.
    """
    selector = str(args.get("session") or "").strip()
    if not selector:
        return None, tools_mod._err(
            "'session' (an id from browser_bridge_session list/create, or a label) is required for this action",
            code=protocol.INVALID_PARAMS,
        )
    row = state.get_agent_session(selector)
    if row is not None:
        if row["device_id"] != device_id:
            reason, code = refusals.format_refusal("session_access_denied", session=selector)
            return None, tools_mod._err(reason, code=code)
        return row, ""
    candidates = state.find_agent_sessions_by_label(device_id, selector)
    if len(candidates) == 1:
        return candidates[0], ""
    if len(candidates) > 1:
        return None, tools_mod._err(
            f"label {selector!r} matches {len(candidates)} sessions on this device -- use the id instead",
            candidates=[_session_view(c) for c in candidates],
        )
    reason, code = refusals.format_refusal("session_not_found", session=selector)
    return None, tools_mod._err(reason, code=code)


def _attached_tabs_for_device(device_id: str) -> List[Dict[str, Any]]:
    return [
        {"tab_id": entry["tab_id"], "url": entry.get("url", ""), "title": entry.get("title", ""), "holder": entry.get("holder")}
        for entry in attach_mod.get_registry().snapshot()
        if entry.get("device_id") == device_id
    ]


def handle_session(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = tools_mod._resolve_device(args)
    if err:
        return err

    action = str(args.get("action") or "").strip().lower()
    if action not in SESSION_ACTIONS:
        return tools_mod._err(
            f"action must be one of {list(SESSION_ACTIONS)}", code=protocol.INVALID_PARAMS
        )

    if action == "create":
        label = str(args.get("label") or "").strip()
        notes = str(args.get("notes") or "")
        session = state.create_agent_session(device_id, label=label, notes=notes)
        raw = tools_mod._session_key(kwargs)
        if raw != tools_mod._DEFAULT_SESSION:
            # Bind the CURRENT conversation onto the session it just created
            # -- otherwise the very next tool call in the same conversation
            # would resolve to a different (auto-provisioned) shadow session
            # instead of the one just named.
            state.bind_hermes_session(raw, device_id, session["id"])
        audit.record("session_create", device=device_id, session=session["id"], label=label)
        return tools_mod._ok(session=_session_view(session))

    if action == "list":
        include_closed = bool(args.get("include_closed") or False)
        sessions = state.list_agent_sessions(device_id, include_closed=include_closed)
        return tools_mod._ok(sessions=[_session_view(s) for s in sessions])

    target, err = _lookup_named_session(device_id, args)
    if err:
        return err
    assert target is not None  # _lookup_named_session's contract: err is "" iff target is set

    if action == "describe":
        payload = _session_view(target)
        payload["attached_tabs"] = _attached_tabs_for_device(device_id)
        # G3.3.3: this session's own tab workspace (key/role/url/title/
        # changed) -- optional-at-load-time like every other cross-module
        # call in this file, so a checkout without tabs.py just omits the
        # field rather than failing describe outright.
        try:
            from . import tabs as tabs_mod  # noqa: PLC0415 - optional sibling module, G3.3

            payload["workspace"] = tabs_mod.build_workspace(device_id, target["id"])
        except ImportError:
            pass
        audit.record("session_describe", device=device_id, session=target["id"])
        return tools_mod._ok(**payload)

    if target.get("closed_at") is not None:
        reason, code = refusals.format_refusal("session_closed", session=target.get("label") or target["id"])
        return tools_mod._err(reason, code=code)

    if action == "resume":
        raw = tools_mod._session_key(kwargs)
        if raw == tools_mod._DEFAULT_SESSION:
            return tools_mod._err(
                "resume needs a real Hermes session identity to bind onto (no session kwarg observed on this call)",
                code=protocol.INVALID_PARAMS,
            )
        state.bind_hermes_session(raw, device_id, target["id"])
        state.touch_agent_session(target["id"])
        session = state.get_agent_session(target["id"])
        audit.record("session_resume", device=device_id, session=target["id"])
        payload = _session_view(session)
        ttl_hours = int(config.load()["approval_session_grant_ttl_hours"])
        payload["note"] = (
            f"resumed -- this conversation now acts as session '{payload['label'] or payload['id']}'. "
            f"Any of its session-scope approvals older than {ttl_hours}h will re-prompt automatically; "
            "nothing here bypasses that."
        )
        return tools_mod._ok(**payload)

    if action == "rename":
        label = str(args.get("label") or "").strip()
        if not label:
            return tools_mod._err("label is required for rename", code=protocol.INVALID_PARAMS)
        state.rename_agent_session(target["id"], label)
        audit.record("session_rename", device=device_id, session=target["id"], label=label)
        return tools_mod._ok(session=_session_view(state.get_agent_session(target["id"])))

    if action == "close":
        close_opened_tabs = bool(args.get("close_opened_tabs") or False)
        closed_tabs: List[int] = []
        requested_tabs: List[int] = []
        if close_opened_tabs:
            # H4 "clean up after yourself": close BEFORE marking the session
            # closed -- close_opened_tabs's own bookkeeping (releasing the
            # lease, forgetting the workspace row) reads/writes rows keyed
            # by this still-open session_id, and there is no reason to make
            # it race against the close below. Optional-at-load-time like
            # every other cross-module call in this file: a checkout
            # without tabs.py just skips the cleanup rather than failing
            # the close itself.
            try:
                from . import tabs as tabs_mod  # noqa: PLC0415 - optional sibling module, G3.3/H4

                requested_tabs, closed_tabs, err = tabs_mod.close_opened_tabs(device_id, target["id"])
                if err:
                    # Only a malformed/refused call, which close_opened_tabs
                    # with no explicit selectors never produces -- kept as a
                    # defensive branch, not a reachable one today.
                    audit.record(
                        "session_close_opened_tabs_failed", device=device_id, session=target["id"], reason=err,
                    )
            except ImportError:
                pass
        state.close_agent_session(target["id"])
        audit.record(
            "session_close", device=device_id, session=target["id"],
            requested_opened_tabs=requested_tabs, closed_opened_tabs=closed_tabs,
        )
        payload: Dict[str, Any] = {"session": _session_view(state.get_agent_session(target["id"]))}
        if close_opened_tabs:
            payload["closed_opened_tabs"] = closed_tabs
            if len(closed_tabs) < len(requested_tabs):
                payload["warning"] = (
                    f"requested closing {len(requested_tabs)} agent-opened tab(s); the extension "
                    f"confirmed {len(closed_tabs)} -- the rest may already be closed, or the device is "
                    "unreachable right now"
                )
        return tools_mod._ok(**payload)

    # Unreachable: `action` was validated against SESSION_ACTIONS above.
    return tools_mod._err(f"unhandled action {action!r}", code=protocol.INVALID_PARAMS)


def register_session_management_tools(ctx) -> List[str]:
    """Entry point `tools.register_tools` imports under
    `try/except ImportError`, the same optional seam as every sibling
    `register_*_tools` function. Named `..._management_tools`, not
    `register_session_tools`, because `session_powers.py` already exports a
    `register_session_tools` for browser_bridge_fetch/_cookies/_network (M3
    "session powers" -- a different, pre-existing sense of "session")."""
    registered: List[str] = []
    for schema, handler in ((SESSION_SCHEMA, handle_session),):
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            check_fn=tools_mod.bridge_available,
            emoji="\U0001F5C2",
        )
        registered.append(schema["name"])
    return registered
