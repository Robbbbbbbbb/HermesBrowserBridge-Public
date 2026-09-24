"""G3.3 "the tab workspace -- agent-addressable tabs" (coveragegaps.md
§G3.3): `browser_bridge_tabs`, plus the resolution/bookkeeping seam every
other tab-taking tool goes through for `tab` (an alternative to `tab_id`).

Today, before this module, the agent addresses a tab only by its raw
numeric ``tab_id`` from ``browser_bridge_status`` -- unmemorable, unstable
across a session, and impossible to reason about across more than one open
tab ("which one was the helpdesk ticket again?"). This module gives each
attached tab a short, stable **key** (agent-chosen, e.g. ``"ticket"``, or
auto-assigned ``t1``/``t2``/...) plus a free-text **role**, both scoped to
the session that attached it, and a workspace view (`browser_bridge_tabs`)
listing every tab a session holds with its key/role/url/title and whether
it changed since that session last looked.

Row-level scoping (IDOR protection, the load-bearing property of this whole
feature): a tab key/label is only ever resolved against the CALLING
session's own ``agent_session_tabs`` rows -- ``state.find_session_tabs_by_key``
scopes the SQL to ``session_id`` alone, so a key collision with ANOTHER
session (even one on the same device) can never resolve to the wrong tab,
and a device boundary is enforced the same way (``state
.delete_session_tabs_for_device_tab``'s subquery against ``agent_sessions
.device_id``). An ambiguous key/prefix is refused outright, listing the
candidates -- never guessed.

Freshness (G3.3.4) rides the SAME ``tab.changed`` event background.ts
already pushes for every navigation, resize (G2.5.4's ResizeObserver reuses
it too -- see that task's own note), tab-group change and CDP detach; this
module's ``on_tab_changed`` is what the gateway calls now that
``relay.py``'s dispatch table actually routes it (previously: declared in
``protocol/schema.json``'s ``events``, but never dispatched -- every
``tab.changed`` notification audited as ``notification_failed`` and
otherwise silently dropped). No second staleness channel is invented here.

Ownership of this file: new and standalone, imported the same
optional-at-load-time way as ``sessions.py``/``vision.py``/
``session_powers.py``/``navigation.py``/``http_auth.py``/``dialogs.py``/
``upload.py``/``evaluate.py`` -- see ``tools.register_tools``'s own comment
on why that seam exists. A checkout missing this file degrades to "no
workspace view, and `tab` on every other tool always refuses with a named
reason", never a failed plugin load.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Tuple

from . import attach as attach_mod
from . import audit, protocol
from . import refusals
from . import relay as relay_mod
from . import state
from . import tools as tools_mod

TOOLSET = tools_mod.TOOLSET

TABS_ACTIONS = ("list", "set", "close_opened")

TABS_SCHEMA = {
    "name": "browser_bridge_tabs",
    "description": (
        "The tab workspace (G3.3): every tab YOUR session currently holds attached, each with its "
        "key (a short id like 't1', or a label you gave it, e.g. 'ticket'), its role (a free-text "
        "note on what it's for), its current url/title, and `changed`: true if it navigated or "
        "resized since you last snapshotted/read/acted on it -- re-snapshot before trusting an idx "
        "on a `changed` tab, the same way you would after any other same-tab re-render. Call this "
        "to orient before deciding which tab to act on next, especially once you're driving more "
        "than one. Never shows another session's tabs, even on the same device -- this is always "
        "just your own workspace. `action:'list'` (the default) reads it; `action:'set'` renames a "
        "tab's key and/or role after the fact (e.g. once you've figured out what a tab actually is); "
        "`action:'close_opened'` (H4 'clean up after yourself') actually CLOSES tabs -- but only ones "
        "THIS session opened itself with browser_bridge_open_tab. Pass `tabs` (a list of keys/labels/"
        "ids) to close specific ones, or omit it to close every tab this session has opened and not "
        "yet closed. A tab the user opened by hand, or one a different session opened, is refused "
        "outright, never silently skipped or closed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(TABS_ACTIONS),
                "description": "list (default) | set | close_opened",
            },
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "tab": {
                "type": "string",
                "description": "For 'set': the tab to rename -- its current key/label, or its numeric tab_id as a string.",
            },
            "tab_id": {"type": "integer", "description": "For 'set': alternative to 'tab' -- the raw tab_id."},
            "key": {
                "type": "string",
                "description": "For 'set': the new key (short, memorable -- e.g. 'ticket'). Must be unique within your session; a conflicting key is refused, not silently overwritten.",
            },
            "role": {
                "type": "string",
                "description": "For 'set': free-text description of what this tab is for.",
            },
            "tabs": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "For 'close_opened': which tabs to close -- each entry a key/label or a numeric "
                    "tab_id as a string. Omit entirely to close every tab this session has opened via "
                    "browser_bridge_open_tab and not yet closed."
                ),
            },
        },
        "required": [],
    },
}


def digest_of(text: str) -> str:
    """Short, cheap fingerprint of what a session last saw on a tab -- purely
    informational (shown in the workspace view), never itself the source of
    the `changed` flag, which comes only from `tab.changed` (G3.3.4)."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def _next_auto_key(session_id: str) -> str:
    existing = {
        str(row["tab_key"]).lower() for row in state.get_session_tabs(session_id) if row.get("tab_key")
    }
    i = 1
    while f"t{i}" in existing:
        i += 1
    return f"t{i}"


def resolve_tab_selector(device_id: str, session_id: str, selector: str) -> Tuple[Optional[int], str]:
    """G3.3.2's one seam: resolve `tab` (a key or label, case-insensitive,
    unique-prefix matched) to a tab_id -- scoped to `session_id`'s OWN
    `agent_session_tabs` rows only. An ambiguous prefix, or one matching
    nothing, is refused outright with the candidates (when any), never
    guessed. `device_id` is accepted for symmetry with every other
    resolver in this file but not itself part of the query -- `session_id`
    already belongs to exactly one device (see `state.create_agent_session`)
    so there is nothing left for it to additionally scope."""
    del device_id  # see docstring: session_id alone is the whole scope
    matches = state.find_session_tabs_by_key(session_id, selector)
    if not matches:
        reason, code = refusals.format_refusal("tab_not_found", tab=selector)
        return None, tools_mod._err(reason, code=code)
    if len(matches) > 1:
        reason, code = refusals.format_refusal("tab_ambiguous", tab=selector)
        return None, tools_mod._err(
            reason,
            code=code,
            candidates=[
                {"tab_id": m["tab_id"], "tab_key": m.get("tab_key") or "", "role": m.get("role") or ""}
                for m in matches
            ],
        )
    return matches[0]["tab_id"], ""


def assign_tab_key(
    session_id: str, tab_id: int, requested_key: str = "", role: str = ""
) -> Tuple[str, str]:
    """Called after a successful attach (G3.3.1): give this (session, tab) a
    stable key -- the caller's own choice if it gave one and that key isn't
    already used by a DIFFERENT tab in this same session, else the tab's
    existing key (a re-attach) or a freshly auto-assigned ``t1``/``t2``/....
    Returns (key, error_json); error_json is non-empty ONLY for an explicit
    conflicting key -- callers (tools.py's handle_attach) treat that as
    "fall back to auto-assign", never as a reason to fail the attach itself,
    since the tab is already leased and driveable by the time this runs."""
    requested_key = requested_key.strip()
    if requested_key:
        conflict = [
            m for m in state.find_session_tabs_by_key(session_id, requested_key)
            if str(m.get("tab_key") or "").lower() == requested_key.lower() and m["tab_id"] != tab_id
        ]
        if conflict:
            reason, code = refusals.format_refusal("tab_key_conflict", tab=requested_key)
            return "", tools_mod._err(reason, code=code)
        key = requested_key
    else:
        current = state.get_session_tab(session_id, tab_id)
        key = str(current["tab_key"]) if current and current.get("tab_key") else _next_auto_key(session_id)
    state.upsert_session_tab(session_id, tab_id, key, role=role)
    return key, ""


def forget_session_tab(session_id: str, tab_id: int) -> None:
    """A released tab disappears from the workspace (G3.3.6)."""
    state.delete_session_tab(session_id, tab_id)


def note_tab_activity(
    session_id: str, tab_id: int, url: str = "", title: str = "", digest_text: str = ""
) -> None:
    """Called from tools.py after a successful snapshot/read/act: clears
    THIS session's own staleness flag for the tab it just looked at, and
    refreshes the cached url/title/digest the workspace view reports. Never
    raises -- see `tools._note_tab_activity`'s own docstring for why a
    bookkeeping failure here must never fail the page operation that
    already succeeded."""
    state.touch_session_tab_watch(session_id, tab_id, url=url, title=title, digest=digest_of(digest_text))


def on_tab_changed(device_id: str, tab: Dict[str, Any], change: str) -> None:
    """Gateway-side handler for the `tab.changed` notification (G3.3.4) --
    now actually dispatched by relay.py's `_on_tab_changed`, where before it
    fell through to "unknown method" and was silently dropped. Reuses the
    SAME signal every navigation, G2.5.4's resize watcher, and a tab-group
    change already push (background.ts's `notifyTabChanged` — see that
    file's own comment on why a resize and a navigation are indistinguishable
    here, and are therefore treated identically), rather than inventing a
    second staleness channel. `removed` (tab closed, or its CDP session
    detached) drops the tab from every session's workspace outright rather
    than leaving a zombie entry with a key nothing will ever reuse."""
    tab_id = tab.get("tabId")
    if not isinstance(tab_id, int):
        return
    url = str(tab.get("url") or "")
    title = str(tab.get("title") or "")
    if change == "removed":
        dropped = state.delete_session_tabs_for_device_tab(device_id, tab_id)
        if dropped:
            audit.record("tab_workspace_dropped", device=device_id, tab_id=tab_id)
        # H4: the tab is verifiably gone -- whether the user closed an
        # agent-opened tab by hand, or close_opened itself just closed it and
        # this is that same removal being reported back -- so no session's
        # "I opened this" record for it should linger either.
        forgotten = state.forget_agent_opened_tabs_for_device_tab(device_id, tab_id)
        if forgotten:
            audit.record("tab_opened_record_dropped", device=device_id, tab_id=tab_id)
        return
    if change == "updated":
        rows = state.mark_device_tab_changed(device_id, tab_id, url=url, title=title)
        if rows:
            audit.record("tab_flagged_stale", device=device_id, tab_id=tab_id, change=change)
        return
    # "activated"/"grouped"/anything else future: refresh cached metadata
    # without forcing a re-snapshot -- neither implies the DOM or index map
    # went stale.
    state.refresh_device_tab_cache(device_id, tab_id, url=url, title=title)


def build_workspace(device_id: str, session_id: str) -> List[Dict[str, Any]]:
    """The `browser_bridge_tabs` / `browser_bridge_session describe` payload
    (G3.3.3). Scoped to `session_id`'s own `agent_session_tabs` rows ONLY --
    see this module's docstring's IDOR paragraph. `holder` is cross-checked
    against attach.py's live lease registry as a courtesy (today it is
    always this same session, since a row only exists because THIS session
    attached the tab) -- it is not itself part of the scoping, which is
    already complete before this function is ever called."""
    registry = attach_mod.get_registry()
    workspace: List[Dict[str, Any]] = []
    for row in state.get_session_tabs(session_id):
        tab_id = row["tab_id"]
        watch = state.get_session_tab_watch(session_id, tab_id) or {}
        workspace.append({
            "tab_id": tab_id,
            "tab_key": row.get("tab_key") or "",
            "role": row.get("role") or "",
            "url": watch.get("last_url") or "",
            "title": watch.get("last_title") or "",
            "changed": bool(watch.get("stale")),
            "last_looked_at": watch.get("last_looked_at"),
            "last_changed_at": watch.get("last_changed_at"),
            "holder": registry.holder_of(tab_id),
            "session_id": session_id,
            # H4: "agent" iff THIS session opened this tab itself via
            # browser_bridge_open_tab (state.is_agent_opened_tab -- the same
            # check close_opened enforces as its authorization boundary);
            # "user" otherwise, which fails closed the safe direction -- a
            # tab this table has no opinion on (every tab opened before H4
            # shipped, or one the user attached by hand) is never mistaken
            # for one close_opened may touch.
            "opened_by": "agent" if state.is_agent_opened_tab(session_id, tab_id) else "user",
        })
    return workspace


def close_opened_tabs(
    device_id: str, session_id: str, selectors: Optional[List[str]] = None
) -> Tuple[List[int], List[int], str]:
    """H4 'clean up after yourself': the shared implementation behind both
    `browser_bridge_tabs action=close_opened` and `browser_bridge_session
    close`'s `close_opened_tabs` flag.

    Returns ``(requested_tab_ids, closed_tab_ids, error_json)``.
    ``error_json`` is non-empty ONLY when the call is refused outright
    before anything is touched (an explicit selector that resolves to a tab
    this session did not itself open, per `state.is_agent_opened_tab` --
    covers both "the user opened it" and "a different session opened it" in
    one check, since a tab_id is opened by exactly one session ever) --
    everything named in ``selectors`` is closed, or none of it is; there is
    no partial refusal. When `selectors` is omitted (or empty), every tab
    this session has opened and not yet closed is targeted, and this never
    refuses -- there is nothing for it to be wrong about.

    Closing itself is best-effort past that point: a `tabs.close` bridge
    error (device disconnected, extension gone) still drops this session's
    own lease/workspace bookkeeping for every requested tab (the tab may
    already be gone from Chrome's perspective, and holding a lease on a tab
    nothing can reach helps no one), and reports zero closed rather than
    raising -- callers (both this module's own `handle_tabs` and
    sessions.py's session-close flow) decide how loudly to surface that.
    """
    if selectors:
        tab_ids: List[int] = []
        for raw in selectors:
            selector = str(raw).strip()
            if not selector:
                reason, code = refusals.format_refusal("tab_not_found", tab="")
                return [], [], tools_mod._err(reason, code=code)
            if selector.lstrip("-").isdigit():
                tab_id = int(selector)
            else:
                resolved, err = resolve_tab_selector(device_id, session_id, selector)
                if err:
                    return [], [], err
                tab_id = resolved
            if not state.is_agent_opened_tab(session_id, tab_id):
                reason, code = refusals.format_refusal("tab_not_agent_opened", tab=selector)
                audit.record(
                    "tool_tabs_close_opened_denied", device=device_id, session=session_id, tab=selector,
                )
                return [], [], tools_mod._err(reason, code=code)
            tab_ids.append(tab_id)
    else:
        tab_ids = state.get_agent_opened_tabs(session_id)

    if not tab_ids:
        return [], [], ""

    registry = attach_mod.get_registry()
    for tab_id in tab_ids:
        # Idempotent either way: a tab not currently leased/workspace-tracked
        # by this session (e.g. it opened but never attached -- chrome://,
        # the Web Store, the PDF viewer) is a harmless no-op for both calls.
        registry.release(device_id, tab_id, session_id)
        forget_session_tab(session_id, tab_id)

    relay = relay_mod.get_relay()
    try:
        result = relay.call(device_id, "tabs.close", {"tabIds": tab_ids})
    except relay_mod.BridgeError:
        # Gateway-side bookkeeping is already dropped above regardless --
        # see this function's own docstring on why that is deliberate even
        # when the extension never confirmed anything.
        return tab_ids, [], ""

    closed_raw = result.get("closed")
    closed = sorted({int(t) for t in closed_raw if isinstance(t, (int, float))}) if isinstance(closed_raw, list) else []
    for tab_id in closed:
        state.forget_agent_opened_tab(session_id, tab_id)
    return tab_ids, closed, ""


def handle_tabs(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = tools_mod._resolve_device(args)
    if err:
        return err
    action = str(args.get("action") or "list").strip().lower()
    if action not in TABS_ACTIONS:
        return tools_mod._err(f"action must be one of {list(TABS_ACTIONS)}", code=protocol.INVALID_PARAMS)

    session_id, err = tools_mod._resolve_session(device_id, kwargs)
    if err:
        return err

    if action == "list":
        audit.record("tool_tabs_list", device=device_id, session=session_id)
        return tools_mod._ok(device_id=device_id, session_id=session_id, tabs=build_workspace(device_id, session_id))

    if action == "close_opened":
        raw_tabs = args.get("tabs")
        selectors: Optional[List[str]] = None
        if raw_tabs is not None:
            if not isinstance(raw_tabs, list) or not raw_tabs:
                return tools_mod._err(
                    "'tabs' must be a non-empty array of tab keys/labels/ids for action='close_opened'",
                    code=protocol.INVALID_PARAMS,
                )
            selectors = [str(t) for t in raw_tabs]
        requested, closed, err = close_opened_tabs(device_id, session_id, selectors)
        if err:
            return err
        # Audit every close -- the full requested/closed lists, not just a
        # count, so an audit reader can see exactly which tab_ids were asked
        # for and which the extension actually confirmed.
        audit.record(
            "tool_tabs_close_opened", device=device_id, session=session_id, requested=requested, closed=closed,
        )
        payload: Dict[str, Any] = {
            "device_id": device_id, "session_id": session_id, "requested": requested, "closed": closed,
        }
        if len(closed) < len(requested):
            payload["warning"] = (
                f"requested closing {len(requested)} tab(s); the extension confirmed {len(closed)} -- "
                "the rest may already be closed, or the device is unreachable right now"
            )
        return tools_mod._ok(**payload)

    # action == "set"
    selector = str(args.get("tab") or "").strip()
    raw_tab_id = args.get("tab_id")
    tab_id: Optional[int] = int(raw_tab_id) if raw_tab_id is not None else None
    if tab_id is None and selector:
        if selector.lstrip("-").isdigit():
            tab_id = int(selector)
        else:
            resolved, err = resolve_tab_selector(device_id, session_id, selector)
            if err:
                return err
            tab_id = resolved
    if tab_id is None:
        return tools_mod._err("'tab' or 'tab_id' is required for action='set'", code=protocol.INVALID_PARAMS)
    if state.get_session_tab(session_id, tab_id) is None:
        return tools_mod._err(
            f"tab {tab_id} is not in your workspace -- attach it first with browser_bridge_attach",
            code=protocol.INVALID_PARAMS,
        )

    raw_key = args.get("key")
    new_key: Optional[str] = None
    if raw_key is not None:
        new_key = str(raw_key).strip()
        if not new_key:
            return tools_mod._err("'key' must not be blank", code=protocol.INVALID_PARAMS)
        conflict = [
            m for m in state.find_session_tabs_by_key(session_id, new_key)
            if str(m.get("tab_key") or "").lower() == new_key.lower() and m["tab_id"] != tab_id
        ]
        if conflict:
            reason, code = refusals.format_refusal("tab_key_conflict", tab=new_key)
            return tools_mod._err(reason, code=code)

    raw_role = args.get("role")
    new_role = str(raw_role) if raw_role is not None else None

    if new_key is None and new_role is None:
        return tools_mod._err("pass 'key' and/or 'role' to update for action='set'", code=protocol.INVALID_PARAMS)

    state.rename_session_tab(session_id, tab_id, tab_key=new_key, role=new_role)
    audit.record("tool_tabs_set", device=device_id, session=session_id, tab_id=tab_id, key=new_key, role=new_role)
    updated = next((t for t in build_workspace(device_id, session_id) if t["tab_id"] == tab_id), None)
    return tools_mod._ok(tab=updated)


def register_tab_workspace_tools(ctx) -> List[str]:
    """Entry point `tools.register_tools` imports under `try/except
    ImportError`, the same optional seam as every sibling
    `register_*_tools` function."""
    registered: List[str] = []
    for schema, handler in ((TABS_SCHEMA, handle_tabs),):
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            check_fn=tools_mod.bridge_available,
            emoji="\U0001F5C3",
        )
        registered.append(schema["name"])
    return registered
