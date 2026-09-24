"""browser_bridge_open_tab — let the agent start a task from a URL.

Everything else in this plugin operates on a tab the *user* opened and
attached. That is a deliberate posture, and it is also a floor: an agent that
can only ever look at what is already on screen cannot follow a link, open a
second console, or begin a task from a bare URL without a human doing the
clicking first.

This module adds exactly one capability — open a tab, optionally attach it —
and governs it as an **action**, not as a read. Opening a URL inside a browser
that carries the user's live sessions has side effects at the destination
(hit counters are the harmless end of that range; one-click unsubscribe and
"confirm this action" links are not), so it goes through the same approval
machinery as ``browser_bridge_act`` rather than the read-only gate.

Two decisions here are worth stating out loud, because both are departures
from what the surrounding code does by default:

1. **An origin with no grant row never dead-ends.** ``state.get_mode``
   returns ``state.effective_default_mode`` for any origin the user has
   never configured — this device's own reported default if it has one,
   else ``config.default_mode``. When that default is ``full`` (the shipped
   default), this tool
   simply follows it like every other capability does. When it is ``off``,
   following it would make the tool permanently unusable rather than merely
   locked down: the popup's origin-mode picker is built from the origins of
   *attached* tabs, so an origin with no tab open has no row and no way for
   the user to give it one — a dead end with no door. Only in that case is a
   missing row treated as ``request`` instead, so the user gets an approval
   prompt showing the exact URL and can answer it. An origin the user
   explicitly set to ``off`` always refuses outright; that one is a real
   answer and it is honoured whatever the default is.

2. **The new tab is not focused by default.** ``active`` defaults to false.
   An agent working in the background must not be able to yank the user out
   of what they are doing, and an agent that opens six tabs while researching
   would otherwise make the browser unusable.

http/https only, enforced here *and* in the extension: a scheme check that
exists on only one side of a socket is not a check. ``file://`` would turn a
browser-automation tool into a local file reader, and ``javascript:`` would
turn it into arbitrary script execution in whatever page happened to be open.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

from . import attach as attach_mod
from . import audit, protocol
from . import relay as relay_mod
from . import state
from . import tools as tools_mod

OPEN_TAB_SCHEMA = {
    "name": "browser_bridge_open_tab",
    "description": (
        "Open a new tab at a URL in the user's browser and attach it, so you can read and "
        "drive it straight away — use this to start a task from a link instead of waiting "
        "for the user to open the page. http/https only. Needs the user's approval the "
        "first time you open a given origin (they see the exact URL), and is refused "
        "outright for an origin they set to 'off'. The tab opens in the background unless "
        "you pass active=true, so the user does not lose their place."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http:// or https:// URL to open."},
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "attach": {
                "type": "boolean",
                "description": "Attach the new tab so you can read and act in it. Defaults to true.",
            },
            "active": {
                "type": "boolean",
                "description": (
                    "Bring the new tab to the front. Defaults to false — opening in the "
                    "background avoids interrupting whatever the user is doing."
                ),
            },
        },
        "required": ["url"],
    },
}


def _validate_url(raw: Any) -> Tuple[str, str]:
    """Return (url, error_json). error_json is "" on success."""
    if not isinstance(raw, str) or not raw.strip():
        return "", tools_mod._err("url is required")
    url = raw.strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        return "", tools_mod._err(
            f"only http and https URLs can be opened; {parts.scheme or '(no scheme)'!r} is refused",
            code=protocol.URL_SCHEME_BLOCKED,
            hint="pass an absolute http:// or https:// URL",
        )
    if not parts.netloc:
        return "", tools_mod._err(
            "url has no host — pass an absolute URL, not a path",
            code=protocol.URL_SCHEME_BLOCKED,
        )
    return url, ""


def _has_grant_row(device_id: str, origin: str) -> bool:
    """True when the user has explicitly set a mode for this origin.

    ``state.get_mode`` cannot answer this — it folds "no row" and "row set to
    the default" into the same string. See this module's docstring for why the
    difference decides whether an unknown origin prompts or refuses.
    """
    return any(row.get("origin") == origin for row in state.list_grants(device_id))


def _authorize_open(device_id: str, origin: str, url: str, session_key: str) -> Optional[Tuple[str, int]]:
    """Grant/approval gate for opening a URL. None means proceed.

    Delegates to ``tools._authorize`` for every case it handles correctly, and
    only intervenes for the one case it does not: an origin with no grant row
    at all, which must prompt rather than inherit ``default_mode: off``.
    """
    summary = f"open a new tab at {url}"
    detail = f"Hermes wants to open {url} in a new tab and attach it."

    # The bootstrap below exists for exactly one situation: the configured
    # default would REFUSE, and the user has no way to change that for this
    # origin because the popup's mode picker is built from the origins of
    # attached tabs. Refusing there is a dead end with no door, so a prompt
    # is substituted for the refusal.
    #
    # It must not fire when the default already allows or already prompts.
    # With `default_mode: full` (the shipped default since 2026-09-22),
    # bootstrapping to "request" would make open_tab *more* restrictive than
    # every other capability on the same origin, and would prompt a user who
    # has already said "full is my default" — the opposite of what they asked
    # for. So: only a default of "off" is overridden here.
    if not _has_grant_row(device_id, origin):
        # state.effective_default_mode, not config.load()["default_mode"]
        # directly: a device can now report its own default (the user's
        # 2026-09-23 ask), which must be what this bootstrap check honours
        # too — otherwise a device whose own default is "off" would sail
        # through here on a fleet default of "full" and never get the
        # prompt this whole branch exists to substitute for a dead end.
        default_mode = state.effective_default_mode(device_id)
        if default_mode == "off":
            audit.record(
                "grant_bootstrap",
                device=device_id,
                origin=origin,
                capability="open_tab",
                reason="no grant row and default_mode is off; prompting instead of a dead-end refusal",
            )
            state.set_grant(device_id, origin, "request")

    return tools_mod._authorize(device_id, origin, "open_tab", summary, session_key, detail=detail)


def handle_open_tab(args: Dict[str, Any], **kwargs: Any) -> str:
    url, url_err = _validate_url(args.get("url"))
    if url_err:
        return url_err

    device_id, err = tools_mod._resolve_device(args)
    if err:
        return err
    paused = tools_mod._paused_refusal(device_id, "open_tab")
    if paused:
        return paused

    origin = tools_mod._origin_of(url)
    session_key, err = tools_mod._resolve_session(device_id, kwargs)
    if err:
        return err

    refusal = _authorize_open(device_id, origin, url, session_key)
    if refusal is not None:
        reason, code = refusal
        audit.record("tool_open_tab_denied", device=device_id, origin=origin, reason=reason)
        return tools_mod._err(reason, code=code, hint=tools_mod._hint_for_code(code))

    want_attach = args.get("attach", True) is not False
    relay = relay_mod.get_relay()
    try:
        result = relay.call(
            device_id,
            "tabs.create",
            {"url": url, "attach": want_attach, "active": bool(args.get("active", False))},
        )
    except relay_mod.BridgeError as exc:
        return tools_mod._bridge_err(exc, "tabs.create")

    tab = result.get("tab") or {}
    tab_id = tab.get("tabId")
    attached = bool(result.get("attached"))

    # H4 "clean up after yourself" (speedimprovements.md H4): record this as
    # a tab THIS session opened, regardless of whether the attach below
    # succeeds -- an opened-but-unattached tab (chrome://, the Web Store, the
    # PDF viewer) is still ours to close later via browser_bridge_tabs
    # action=close_opened or browser_bridge_session close's
    # close_opened_tabs flag, even though it never gets a lease or a
    # workspace row.
    if isinstance(tab_id, int):
        state.record_agent_opened_tab(session_key, device_id, tab_id)

    # Take the lease only for a tab the extension really did attach. Claiming
    # one for a tab that opened but refused the debugger would leave a lease
    # on a tab no tool can drive, and its own expiry (or, with an unlimited
    # lease, an explicit release) would be the only thing that ever cleared
    # it. Uses this device's own effective lease (state.py's
    # effective_lease_seconds), same as tools.py's handle_attach/handle_act.
    holder = session_key
    effective_lease = state.effective_lease_seconds(device_id)
    if attached and isinstance(tab_id, int):
        try:
            attach_mod.get_registry().attach(
                device_id, tab_id, holder, tab_ref=tab, ttl=attach_mod.ttl_seconds_for(effective_lease)
            )
        except attach_mod.AttachConflict as exc:
            audit.record("tool_open_tab", device=device_id, origin=origin, tab=tab_id, conflict=exc.holder)
            return tools_mod._err(
                f"opened the tab, but it is already leased by session {exc.holder!r}",
                tab=tab,
                hint=tools_mod._lease_wait_hint(device_id),
            )
        # G3.3.1-style workspace entry, mirroring tools.py's handle_attach:
        # give the newly leased tab a stable key so it's addressable by
        # `tab` and shows up in browser_bridge_tabs -- including its H4
        # `opened_by: "agent"` flag, which reads straight from
        # state.record_agent_opened_tab above and needs no extra wiring
        # here. Optional-at-load-time like every other cross-module call in
        # this file: a checkout without tabs.py just skips the workspace
        # entry rather than failing the open itself.
        try:
            from . import tabs as tabs_mod  # noqa: PLC0415 - optional sibling module, G3.3/H4

            tabs_mod.assign_tab_key(holder, tab_id, requested_key="", role="")
        except ImportError:
            pass

    audit.record(
        "tool_open_tab",
        device=device_id,
        origin=origin,
        url=url,
        tab=tab_id,
        attached=attached,
        holder=holder if attached else "",
    )

    payload: Dict[str, Any] = {"device_id": device_id, "tab": tab, "attached": attached}
    if want_attach and not attached:
        payload["hint"] = (
            "the tab opened but could not be attached — chrome://, the Chrome Web Store and the "
            "PDF viewer refuse a debugger attach; nothing can read or drive it"
        )
    elif attached:
        payload.update(attach_mod.lease_result_fields(effective_lease))
    return tools_mod._ok(**payload)


def register_navigation_tools(ctx) -> list:
    """Entry point ``tools.register_tools`` imports under a defensive seam,
    matching how vision.py and session_powers.py are wired in."""
    ctx.register_tool(
        name=OPEN_TAB_SCHEMA["name"],
        toolset=tools_mod.TOOLSET,
        schema=OPEN_TAB_SCHEMA,
        handler=handle_open_tab,
        check_fn=tools_mod.bridge_available,
        emoji="🌐",
    )
    return [OPEN_TAB_SCHEMA["name"]]
