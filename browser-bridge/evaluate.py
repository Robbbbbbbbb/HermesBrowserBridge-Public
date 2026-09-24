"""G1.5 (arbitrary JS evaluation): ``browser_bridge_evaluate``.

The single most powerful capability in this plugin — it hands the agent the
equivalent of the DevTools console on the attached tab. See
``docs/security.md`` §20 for the full blast-radius statement (this docstring
does not repeat it) and ``docs/devtools.md`` for the user-facing explanation.

Same ownership shape as ``dialogs.py`` relative to ``tools.py``: this file
freely imports ``tools.py``'s private helpers (``_resolve_device``,
``_resolve_attached_tab``, ``_authorize``, ``_origin_of``, ``_ok``/``_err``,
``_bridge_err``, ``_session_key``, ``_gateway_redact``) rather than
re-deriving them, and exposes exactly one public seam —
``register_evaluate_tools(ctx)`` — imported under the same defensive
``try/except ImportError`` pattern ``tools.register_tools`` already uses for
``vision.py``/``session_powers.py``/``dialogs.py``, so a checkout missing this
file degrades to "no evaluate tool" rather than failing plugin registration
outright.

## Gating (G0.5, G0.5.4, G0.6.6)

``evaluate`` is one of ``approvals.py``'s ``DANGEROUS_CAPABILITIES`` AND one
of the three ``NO_STANDING_GRANT_CAPABILITIES`` (alongside ``upload`` and
``http_auth``) — ``_authorize`` already refuses ``always``/``session`` for it
before this file does anything (``approvals.require`` never even offers those
choices; see G0.5.4), and a ``full``-mode origin still prompts (the
per-capability ceiling, G0.5.3). This file's own job is narrower: build the
approval summary, capture the expression once, and never let a display/audit
copy of it diverge from the copy actually sent over the wire.

## Why the expression goes in ``summary``, not ``detail``

The extension's popup (``popup/approvals-ui.ts``) renders only the
``ApprovalMessage.summary`` field today — ``detail`` is stored on the pending
approval but has no render path at all. G1.5.4 requires the prompt to show
the verbatim expression ("an approval the user cannot read is not consent"),
so the expression is placed in ``summary`` (truncated for display), not
``detail``. ``detail`` still carries a JSON copy for forward-compat (a future
UI that does render it) and because ``_authorize`` runs ``_gateway_redact``
over ``detail`` a second time before it reaches ``approvals.require`` — belt
and braces, not the only pass.

## Why the displayed/audited expression is redacted, and the executed one never is

G1.5.5 asks this capability's audit trail to record the expression verbatim —
the log is what makes the most powerful capability in the plugin reviewable
after the fact. But the expression is composed by the agent, not typed by a
human, and can legitimately embed a secret the agent read off the page a turn
earlier (a bearer token being replayed into a ``fetch()`` call, a session
cookie value). The live approval prompt is seen once, by the person who
already has the page open and legitimate access to whatever secret it
contains; ``audit.jsonl`` is a persistent, rotated file that anyone doing an
operational or security review of this device reads later, out of that
context — a different exposure profile, handled differently: both the
approval-prompt copy and the audit copy run through ``_redact_for_display``
below (the same card/SSN/email/phone/password-hint/AWS-key pass
``_gateway_redact`` already applies to every other capability's ``detail``,
plus an unconditional JWT/bearer/API-key/URL-secret-param pass — mirroring
``session_powers.py``'s own ``_redact_console_secrets`` and
``extension/src/lib/redaction.ts``'s ``redactSecrets``, duplicated here rather
than imported, the same "two independent implementations, neither trusts the
other" choice this codebase already makes for `_gateway_redact` itself).
This redacts *values*, not *code shape*: which functions the expression
calls, which URL it fetches, and its overall structure all survive in full;
only a literal secret-shaped span is masked.

**The wire payload actually executed is a SEPARATE, untouched copy.**
``handle_evaluate`` captures ``expression = str(args.get("expression") or "")``
into one local variable before doing anything else, and every later use of
it — the approval summary/detail, the audit record, AND the ``relay.call``
payload — reads only that variable. There is no code path that re-reads
``args`` after the approval decision comes back, and the value sent to
``relay.call`` is never the redacted display copy — it is the original,
byte-for-byte, always. ``tests/test_g15_evaluate.py`` proves this directly by
capturing what ``relay.call`` was actually invoked with and comparing it
against the input, for an expression that DOES contain a secret-shaped
substring, precisely so the redacted display copy and the executed copy
never get silently confused for each other.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

from . import audit, protocol
from . import relay as relay_mod
from . import tools as tools_mod

TOOLSET = tools_mod.TOOLSET

# G1.5.4: keeps the approval prompt readable (an unreadable prompt is not
# consent) and bounds tool_evaluate's audit volume against audit_max_bytes.
# Mirrors extension/src/background/evaluate.ts's MAX_EXPRESSION_CHARS exactly
# — duplicated, not imported (Python and TypeScript don't share constants);
# if the two drift, the extension's own cap is the one that actually binds,
# same relationship session_powers.py's MAX_DOWNLOADS_WAIT_MS documents for
# its own gateway-side mirror of an extension-side ceiling.
MAX_EXPRESSION_CHARS = 4096

DEFAULT_TIMEOUT_MS = 10000
MAX_TIMEOUT_MS = 30000
# How much slack this module gives relay.call beyond the evaluate's own
# timeout_ms, so a normal (non-wedged) evaluate's own wall-clock bound on the
# extension side always has room to return a proper TIMEOUT result before the
# relay's own wait gives up and reports a generic bridge timeout instead.
_RELAY_TIMEOUT_SLACK_S = 5.0

# How much of the expression is shown in the approval prompt / written to the
# audit log — the FULL expression still runs; this only bounds what a human
# reads and what audit.jsonl stores. Not a security boundary, a readability
# one (G1.5.4's "an unreadable prompt is not consent" cuts both ways: an
# 4KB wall of code is exactly as unreadable as an empty prompt).
EXPRESSION_DISPLAY_CHARS = 800

_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{4,2000}\.[A-Za-z0-9_-]{4,2000}\.[A-Za-z0-9_-]{4,2000}\b")
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9\-_.~+/]{8,2000}=*", re.IGNORECASE)
_API_KEY = re.compile(
    r"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret[_-]?key|client[_-]?secret)"
    r"\s*[:=]\s*['\"]?[A-Za-z0-9\-_.]{12,512}['\"]?",
    re.IGNORECASE,
)
_URL_SECRET_PARAM = re.compile(
    r"([?&](?:access_token|token|api[_-]?key|apikey|secret|password)=)[^&#\s]{1,2000}", re.IGNORECASE
)
_TOKEN_PLACEHOLDER = "[redacted:token]"


def _redact_expression_secrets(text: str) -> str:
    """Unconditional JWT/bearer/API-key/URL-secret-param pass — same shapes
    as ``session_powers.py``'s ``_redact_console_secrets`` and
    ``extension/src/lib/redaction.ts``'s ``redactSecrets``, deliberately
    re-implemented here rather than imported (see module docstring)."""
    if not text:
        return text

    def _mark(_match: "re.Match[str]") -> str:
        return _TOKEN_PLACEHOLDER

    text = _JWT.sub(_mark, text)

    def _bearer_sub(match: "re.Match[str]") -> str:
        if _TOKEN_PLACEHOLDER in match.group(0):
            return match.group(0)
        return f"Bearer {_TOKEN_PLACEHOLDER}"

    text = _BEARER.sub(_bearer_sub, text)

    def _api_key_sub(match: "re.Match[str]") -> str:
        whole = match.group(0)
        if _TOKEN_PLACEHOLDER in whole:
            return whole
        sep_pos = min((p for p in (whole.find(":"), whole.find("=")) if p != -1), default=-1)
        sep = whole[sep_pos] if sep_pos != -1 else "="
        name = whole[:sep_pos] if sep_pos != -1 else whole
        return f"{name}{sep}{_TOKEN_PLACEHOLDER}"

    text = _API_KEY.sub(_api_key_sub, text)

    def _url_param_sub(match: "re.Match[str]") -> str:
        if _TOKEN_PLACEHOLDER in match.group(0):
            return match.group(0)
        return f"{match.group(1)}{_TOKEN_PLACEHOLDER}"

    text = _URL_SECRET_PARAM.sub(_url_param_sub, text)
    return text


def _redact_for_display(expression: str, device_id: str) -> str:
    """The copy shown in the approval prompt and written to the audit log —
    NEVER the copy sent to ``relay.call`` (see module docstring). Truncated
    to EXPRESSION_DISPLAY_CHARS after redaction so a masked marker can't
    itself be cut in half."""
    redacted, _hits = tools_mod._gateway_redact(expression, device_id)
    redacted = _redact_expression_secrets(redacted)
    if len(redacted) > EXPRESSION_DISPLAY_CHARS:
        redacted = redacted[:EXPRESSION_DISPLAY_CHARS] + f"... ({len(expression)} chars total)"
    return redacted


EVALUATE_SCHEMA = {
    "name": "browser_bridge_evaluate",
    "description": (
        "Run an arbitrary JavaScript expression in the attached tab's own page context — the DevTools "
        "console's input line, handed to you. This is the single most powerful tool in this toolset: "
        "world:'main' (the only world available) gives full read/write access to document.cookie, "
        "localStorage/sessionStorage, and lets you call fetch() with the page's own session — it is a "
        "SUPERSET of browser_bridge_fetch/cookies/cookies_write combined, and using it makes those "
        "three gates advisory for this origin. Off by default (allowEvaluate), and unlike every other "
        "capability here it NEVER gets a standing approval — every single call prompts the user, shown "
        "the exact code that will run, even on an origin already set to 'full'. returnByValue is always "
        "used: you get a serialised result back (capped at 32KB, redacted for card/SSN/email/phone/"
        "password/bearer-token/JWT/API-key shapes), never a live handle into the page. Every call has a "
        "hard timeout (default 10s, max 30s) — if the expression opens a dialog, loops forever, or "
        "returns a promise that never settles, the call fails with a timeout rather than hanging every "
        "other browser_bridge_* call on that tab; you may need to ask the user to answer a dialog "
        "(browser_bridge_dialog) before retrying. Keep the expression under ~4KB — the approval prompt "
        "and the audit log both need to stay readable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "tab_id": {"type": "integer", "description": "Tab to evaluate in. Omit when exactly one tab is attached."},
            "tab": {"type": "string", "description": tools_mod.TAB_ALIAS_DESC},
            "expression": {
                "type": "string",
                "description": f"The JS expression to run, verbatim. Capped at {MAX_EXPRESSION_CHARS} characters.",
            },
            "await_promise": {
                "type": "boolean",
                "description": "If the expression's value is a Promise, wait for it to settle and use the resolved/rejected value. Default true.",
            },
            "timeout_ms": {
                "type": "integer",
                "description": f"Hard timeout for this call. Default {DEFAULT_TIMEOUT_MS}, capped at {MAX_TIMEOUT_MS}.",
            },
        },
        "required": ["expression"],
    },
}


def handle_evaluate(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = tools_mod._resolve_device(args)
    if err:
        return err
    tab, err = tools_mod._resolve_tab_target(device_id, args, kwargs, "evaluate")
    if err:
        return err
    tab_id = tab["tabId"]
    origin = tab.get("origin") or tools_mod._origin_of(tab.get("url", ""))

    # Captured ONCE, here — every later use of `expression` in this function
    # reads this local, never `args` again. See module docstring: this is
    # what guarantees the wire payload can never diverge from what was
    # approved, regardless of what any display/redaction pass does to a
    # SEPARATE copy of the string.
    expression = str(args.get("expression") or "")
    if not expression:
        return tools_mod._err("expression is required", code=protocol.INVALID_PARAMS)
    if len(expression) > MAX_EXPRESSION_CHARS:
        return tools_mod._err(
            f"expression is {len(expression)} chars, over the {MAX_EXPRESSION_CHARS}-char cap",
            code=protocol.EVAL_EXPRESSION_TOO_LARGE,
        )

    await_promise = args.get("await_promise")
    if await_promise is not None and not isinstance(await_promise, bool):
        return tools_mod._err("await_promise must be true or false", code=protocol.INVALID_PARAMS)
    timeout_ms = int(args.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
    timeout_ms = max(1, min(timeout_ms, MAX_TIMEOUT_MS))

    holder, err = tools_mod._resolve_session(device_id, kwargs)
    if err:
        return err

    # See module docstring for why the expression goes in `summary` (the
    # ONLY field the popup renders) rather than `detail`, and why both are
    # the REDACTED display copy, never the raw expression.
    display_expression = _redact_for_display(expression, device_id)
    summary = f"run custom JS in tab {tab_id} at {origin}:\n{display_expression}"
    detail = json.dumps({"expression": display_expression, "timeout_ms": timeout_ms})

    denial = tools_mod._authorize(
        device_id, origin, "evaluate",
        summary=summary,
        session_key=holder,
        detail=detail,
    )
    if denial is not None:
        reason, code = denial
        audit.record(
            "tool_evaluate_denied", device=device_id, tab_id=tab_id, origin=origin,
            expression=display_expression,
        )
        return tools_mod._err(reason, code=code, device_id=device_id, tab_id=tab_id, origin=origin)

    wire_params: Dict[str, Any] = {"tabId": tab_id, "expression": expression, "world": "main", "timeoutMs": timeout_ms}
    if await_promise is not None:
        wire_params["awaitPromise"] = await_promise

    relay = relay_mod.get_relay()
    relay_timeout = (timeout_ms / 1000.0) + _RELAY_TIMEOUT_SLACK_S
    try:
        result = relay.call(device_id, "page.evaluate", wire_params, timeout=relay_timeout)
    except relay_mod.BridgeError as exc:
        audit.record(
            "tool_evaluate", device=device_id, holder=holder, tab_id=tab_id, origin=origin,
            expression=display_expression, timeout_ms=timeout_ms, ok=False,
        )
        return tools_mod._bridge_err(exc, "page.evaluate")

    # §0.7's belt-and-braces gateway re-check — the extension already redacted
    # this result before the frame was built (background/evaluate.ts).
    value, gateway_hits = tools_mod._gateway_redact(str(result.get("result", "")), device_id)
    if gateway_hits:
        audit.record(
            "redaction_gateway_catch",
            device=device_id, tab_id=tab_id, origin=origin, hits=gateway_hits, capability="evaluate",
        )

    audit.record(
        "tool_evaluate", device=device_id, holder=holder, tab_id=tab_id, origin=origin,
        expression=display_expression, timeout_ms=timeout_ms, ok=True,
        result_truncated=bool(result.get("truncated")), redactions=gateway_hits,
    )
    return tools_mod._ok(
        device_id=device_id,
        tab_id=tab_id,
        result=value,
        result_type=result.get("resultType"),
        result_subtype=result.get("resultSubtype"),
        truncated=bool(result.get("truncated", False)),
    )


def register_evaluate_tools(ctx) -> List[str]:
    """Entry point ``tools.register_tools`` imports under
    ``try/except ImportError``, mirroring ``dialogs.py``'s/``vision.py``'s own
    ``register_*_tools`` seam."""
    ctx.register_tool(
        name=EVALUATE_SCHEMA["name"],
        toolset=TOOLSET,
        schema=EVALUATE_SCHEMA,
        handler=handle_evaluate,
        check_fn=tools_mod.bridge_available,
        emoji="\U0001F9EA",
    )
    return [EVALUATE_SCHEMA["name"]]
