"""G1.4 (JS dialog handling): ``browser_bridge_dialog``.

Dialogs themselves are observed and reported as part of ``browser_bridge_act``
(``handle_act`` in tools.py surfaces the extension's ``page.act`` result's
``dialogs`` field, re-checking redaction gateway-side per plan.md §6.3). This
module is only the second half: resolving one of those recorded dialogs,
either by dismissing it (always allowed — the safe direction, and the
default policy) or accepting it (gated hard).

Same ownership shape as session_powers.py relative to tools.py: this file
freely imports tools.py's private helpers (``_resolve_device``,
``_resolve_attached_tab``, ``_authorize``, ``_origin_of``, ``_ok``/``_err``,
``_bridge_err``, ``_session_key``) rather than re-deriving them, and exposes
exactly one public seam — ``register_dialog_tools(ctx)`` — imported under
the same defensive ``try/except ImportError`` pattern ``tools.register_tools``
already uses for vision.py/session_powers.py/navigation.py, so a checkout
missing this file degrades to "no dialog tool" rather than failing plugin
registration outright.

## Why accepting needs an id AND an ack_message

Rev 1 of the coverage-gaps plan said "refuse an accept on a dialog the agent
has not read" without saying how that would actually be checked —
unimplementable as written, since nothing tracked what the agent had or had
not "read". The concrete mechanism: every dialog the extension records gets
an opaque id (``page.act``'s ``result.dialogs[].id``), and accepting one
requires that id PLUS ``ack_message`` equal to the dialog's own recorded
(already-redacted) message, checked extension-side against the dialog it is
actually still holding open (``dialogs.ts``'s ``handleDialogWire``). There is
no way to guess a state machine into skipping this: the id names an
INSTANCE, not a message string, so acting on a dialog id from three turns ago
(now resolved) is refused as not-found regardless of what ack_message says.

## Gating: only accept is gated

``allowDialogAccept`` (row 4 of §0.6's table) and the ``dialog`` capability
(G0.5's ``DANGEROUS_CAPABILITIES``) gate ACCEPTING only. Dismissing needs
neither — it is the safe direction, the shipped default
(``allowDialogDismiss``, on by default, enforced independently by
``dialogs.ts``'s own auto-dismiss timer, not by this tool at all). That timer
only ever fires for a dialog with ``hasBrowserHandler: false`` — nothing
shows the user anything in that case, so there is nothing to yank out from
under them; a dialog Chrome IS showing to the user (``hasBrowserHandler:
true``) is never auto-dismissed, no matter how long it sits open, because
that would invert the consent model this product is built on. Requiring an
approval prompt to close a wedged headless dialog would leave that wedge
with no way out, which is why dismiss stays ungated either way. This mirrors
hermes_plugin/tools.py's own
``_CAPABILITY_POWER_KEYS["dialog"] = ("allowDialogAccept",)`` comment.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from . import audit, protocol
from . import relay as relay_mod
from . import tools as tools_mod

TOOLSET = tools_mod.TOOLSET

DIALOG_SCHEMA = {
    "name": "browser_bridge_dialog",
    "description": (
        "Accept or dismiss a JS dialog (alert/confirm/prompt/beforeunload) that browser_bridge_act's "
        "result already recorded under `dialogs` — native dialogs in a headful tab are NOT "
        "auto-dismissed, ever (Runtime.evaluate cannot run while one is open, so every other "
        "browser_bridge_* call on that tab will time out until it is resolved): it sits on the "
        "user's screen until they answer it themselves or this tool resolves it. A record with "
        "`note` set means the dialog is one of these — tell the user rather than retrying. "
        "Dismissing needs no special permission — it is always allowed and is the safe direction. "
        "Accepting (confirming/submitting) is off by default and, when enabled, still requires "
        "`ack_message` to be the EXACT `message` you read from that dialog's record — there is no way "
        "to accept a dialog you have not actually read the text of."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "tab_id": {"type": "integer", "description": "Tab the dialog is open on. Omit when exactly one tab is attached."},
            "tab": {"type": "string", "description": tools_mod.TAB_ALIAS_DESC},
            "dialog_id": {
                "type": "string",
                "description": "The `id` from browser_bridge_act's result.dialogs[]. A resolved or unknown id is refused, not guessed at.",
            },
            "accept": {
                "type": "boolean",
                "description": "true to accept/confirm/submit, false to dismiss/cancel. Dismiss is always allowed; accept needs allowDialogAccept on for this device.",
            },
            "ack_message": {
                "type": "string",
                "description": "Required when accept is true: must equal the dialog's recorded `message` exactly. Not checked (and not needed) for a dismiss.",
            },
            "prompt_text": {
                "type": "string",
                "description": "type=prompt only, and only when accepting: the value to submit. Omitted falls back to the dialog's own default value.",
            },
        },
        "required": ["dialog_id", "accept"],
    },
}


def handle_dialog(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = tools_mod._resolve_device(args)
    if err:
        return err
    tab, err = tools_mod._resolve_tab_target(device_id, args, kwargs, "dialog")
    if err:
        return err
    tab_id = tab["tabId"]
    origin = tab.get("origin") or tools_mod._origin_of(tab.get("url", ""))

    dialog_id = str(args.get("dialog_id") or "").strip()
    if not dialog_id:
        return tools_mod._err("dialog_id is required", code=protocol.INVALID_PARAMS)
    accept = bool(args.get("accept"))
    ack_message = args.get("ack_message")
    prompt_text = args.get("prompt_text")
    holder, err = tools_mod._resolve_session(device_id, kwargs)
    if err:
        return err

    if accept:
        if not ack_message:
            return tools_mod._err(
                "ack_message is required to accept a dialog — pass the EXACT message you read from "
                "browser_bridge_act's result.dialogs[], to prove you read it before accepting",
                code=protocol.INVALID_PARAMS,
            )
        denial = tools_mod._authorize(
            device_id, origin, "dialog",
            summary=f"accept dialog {dialog_id} on tab {tab_id} at {origin}",
            session_key=holder,
            detail=json.dumps({"dialog_id": dialog_id, "prompt_text_provided": prompt_text is not None}),
        )
        if denial is not None:
            reason, code = denial
            audit.record("tool_dialog_denied", device=device_id, tab_id=tab_id, dialog_id=dialog_id, accept=True)
            return tools_mod._err(reason, code=code, device_id=device_id, tab_id=tab_id, origin=origin)

    wire_params: Dict[str, Any] = {"tabId": tab_id, "dialogId": dialog_id, "accept": accept}
    if accept and ack_message is not None:
        wire_params["ackMessage"] = str(ack_message)
    if accept and prompt_text is not None:
        wire_params["promptText"] = str(prompt_text)

    relay = relay_mod.get_relay()
    try:
        result = relay.call(device_id, "page.dialog", wire_params)
    except relay_mod.BridgeError as exc:
        return tools_mod._bridge_err(exc, "page.dialog")

    # Never the dialog's message/defaultPrompt in the audit trail — origin,
    # tab, dialog id and accept/dismiss are enough to reconstruct what
    # happened without holding page-authored text a second place.
    audit.record(
        "tool_dialog", device=device_id, holder=holder, tab_id=tab_id, origin=origin,
        dialog_id=dialog_id, accept=accept,
    )
    return tools_mod._ok(
        device_id=device_id, tab_id=tab_id, dialog_id=dialog_id, accept=accept,
        resolved=bool(result.get("resolved", True)),
    )


def register_dialog_tools(ctx) -> List[str]:
    """Entry point ``tools.register_tools`` imports under
    ``try/except ImportError``, mirroring session_powers.py's/vision.py's own
    ``register_*_tools`` seam."""
    ctx.register_tool(
        name=DIALOG_SCHEMA["name"],
        toolset=TOOLSET,
        schema=DIALOG_SCHEMA,
        handler=handle_dialog,
        check_fn=tools_mod.bridge_available,
        emoji="\U0001F4AC",
    )
    return [DIALOG_SCHEMA["name"]]
