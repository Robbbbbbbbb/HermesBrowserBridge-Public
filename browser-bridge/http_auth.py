"""G3.7 "HTTP basic auth" (coveragegaps.md, gap 12 second half):
``browser_bridge_http_auth_status`` -- a single, deliberately narrow tool.

This module owns the gateway half of a design that is split across three
places on purpose (see ``docs/security.md`` §11's basic-auth note and
``coveragegaps.md`` G3.7 for the fuller rationale):

- **The extension** (``extension/src/background/auth.ts``) is where a
  credential actually lives, arms, expires and is replayed. It is staged by
  the user in the popup and answers a real
  ``chrome.webRequest.onAuthRequired`` challenge locally, entirely without
  this gateway's involvement -- basic auth keeps working even if the socket
  to this plugin is down.
- **This module** exposes exactly one read: is a credential currently staged
  for an origin. That is all the agent ever needs to know to stop retrying
  against an invisible sign-in prompt and instead tell the user what to do.
- **Nothing here can arm, read back, or enumerate a credential.** There is no
  ``auth.arm`` wire method, no tool parameter that accepts a username or
  password, and the one wire method that does exist
  (``auth.status``, protocol/schema.json) is defined to never carry either
  field in its result. ``handle_http_auth_status`` strips them anyway before
  returning, belt-and-braces, so a future edit to the wire result can't
  silently start leaking one through this tool without a second, independent
  change also being needed here.

Ownership: this file is new and standalone (not touched by any other
workstream this plan names), imported the same optional-at-load-time way as
``vision.py``/``session_powers.py``/``navigation.py`` -- see
``tools.register_tools``'s own comment on why that seam exists.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import audit, relay as relay_mod
from . import tools as tools_mod

TOOLSET = tools_mod.TOOLSET

HTTP_AUTH_STATUS_SCHEMA = {
    "name": "browser_bridge_http_auth_status",
    "description": (
        "Check whether an HTTP basic/digest sign-in credential is currently staged for an origin, so "
        "you can tell the user what to do instead of retrying a request that will just 401 again. "
        "READ-ONLY: this tool can never supply, reveal, or enumerate a credential -- staging one is a "
        "user-only action in the extension popup (Options -> HTTP sign-in), per-origin, with a TTL. "
        "If armed is false, ask the user to open the popup and arm a credential for that origin; if "
        "true, the browser will answer the next sign-in challenge on its own the moment you retry the "
        "action that hit it. last_result: 'rejected' means the extension staged a credential, the site "
        "rejected it (wrong password), and the extension has already disarmed it and fallen back to "
        "Chrome's own sign-in prompt so the user is not locked out by a silent retry loop -- tell the "
        "user the password they staged for this origin was wrong, rather than retrying the same action "
        "again. The result never contains a username or password -- there is nothing this tool can leak "
        "even if asked to."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "origin": {
                "type": "string",
                "description": "Origin to check, e.g. 'https://intranet.example.com'. Omit to default to the currently attached tab's own origin.",
            },
            "tab_id": {"type": "integer", "description": "Only used to pick a default origin when `origin` is omitted."},
            "tab": {
                "type": "string",
                "description": "Alternative to tab_id, only used to pick a default origin when `origin` is omitted -- " + tools_mod.TAB_ALIAS_DESC,
            },
        },
        "required": [],
    },
}


def handle_http_auth_status(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = tools_mod._resolve_device(args)
    if err:
        return err
    paused = tools_mod._paused_refusal(device_id, "http_auth")
    if paused:
        return paused

    origin = str(args.get("origin") or "").strip()
    tabs: List[Dict[str, Any]] = []
    if not origin:
        tabs, exc = tools_mod._fresh_tabs(device_id)
        if exc is not None:
            return tools_mod._bridge_err(exc, "tabs.list")
        tab_id, err = tools_mod._tab_id_arg(device_id, args, kwargs)
        if err:
            return err
        if tab_id is not None:
            candidates = [t for t in tabs if t.get("tabId") == tab_id]
        else:
            candidates = [t for t in tabs if t.get("attached")]
        if len(candidates) == 1 and candidates[0].get("url"):
            origin = tools_mod._origin_of(candidates[0]["url"])
        else:
            return tools_mod._err(
                "origin is required (no single attached tab to default it from)",
                hint="pass origin explicitly, or attach exactly one tab first",
                candidates=tools_mod._tab_briefs(tabs),
            )

    holder, err = tools_mod._resolve_session(device_id, kwargs)
    if err:
        return err
    summary = f"check whether an HTTP sign-in credential is staged for {origin}"
    denial = tools_mod._authorize(
        device_id, origin, "http_auth", summary=summary, session_key=holder,
        detail=json.dumps({"origin": origin}),
    )
    if denial is not None:
        reason, code = denial
        audit.record("tool_http_auth_status_denied", device=device_id, holder=holder, origin=origin)
        return tools_mod._err(reason, code=code, device_id=device_id, origin=origin)

    relay = relay_mod.get_relay()
    try:
        result = relay.call(device_id, "auth.status", {"origin": origin})
    except relay_mod.BridgeError as exc:
        return tools_mod._bridge_err(exc, "auth.status")

    armed = bool(result.get("armed"))
    expires_at = result.get("expiresAt")
    # "rejected" is the only value protocol/schema.json's own enum allows for
    # this field -- never forward an unexpected value from an old/buggy
    # extension build as if it were a known, meaningful signal.
    last_result = result.get("lastResult")
    if last_result not in ("rejected",):
        last_result = None

    # Belt-and-braces (see module docstring): the wire result is defined to
    # never carry these, but a tool result is model-visible text, so this is
    # the very last place a leak could be silently introduced without this
    # line also having to change.
    for forbidden in ("username", "password"):
        result.pop(forbidden, None)

    # Never the username/password -- only origin + armed + count-shaped
    # metadata, same discipline as handle_cookies's own audit line.
    audit.record(
        "tool_http_auth_status", device=device_id, holder=holder, origin=origin, armed=armed,
        last_result=last_result,
    )

    if last_result == "rejected":
        # The wrong-password-loop guard's defect fix (coveragegaps.md G3.7):
        # the extension already disarmed the bad credential and fell back to
        # Chrome's own prompt on its own -- this tool call is how the agent
        # finds out, instead of retrying the same action against what looks
        # like an unanswered challenge.
        note = (
            "the credential staged for this origin was WRONG -- the site rejected it and the extension "
            "has already disarmed it and fallen back to Chrome's own sign-in prompt. Tell the user the "
            "password they staged was rejected; do not retry the same action expecting it to work"
        )
    elif not armed:
        note = (
            "no credential is staged for this origin -- ask the user to open the extension popup "
            "(Options -> HTTP sign-in) and arm one before retrying"
        )
    else:
        note = "a credential is staged for this origin; retry the action that hit the sign-in prompt"

    fields: Dict[str, Any] = dict(device_id=device_id, origin=origin, armed=armed, expires_at=expires_at, note=note)
    if last_result:
        fields["last_result"] = last_result
    return tools_mod._ok(**fields)


def register_http_auth_tools(ctx) -> List[str]:
    """Entry point ``tools.register_tools`` imports under
    ``try/except ImportError``, the same optional seam as
    ``register_vision_tools``/``register_session_tools``/``register_navigation_tools``."""
    registered: List[str] = []
    for schema, handler in ((HTTP_AUTH_STATUS_SCHEMA, handle_http_auth_status),):
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            check_fn=tools_mod.bridge_available,
            emoji="\U0001F511",
        )
        registered.append(schema["name"])
    return registered
