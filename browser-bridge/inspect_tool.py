"""``browser_bridge_inspect`` (ProjectRules/speedimprovements.md B2): a
read-only, fixed-menu answer to ONE layout question per call --
``scrollables``, ``at_point``, ``visibility``, ``expanded``, ``options`` --
so a session never has to guess at any of these across several
snapshot/screenshot round trips. Never runs caller-supplied code, so it
needs no ``evaluate`` approval; gated exactly like ``browser_bridge_snapshot``
(``full`` required, same per-frame origin policy forwarded to the extension).

Kept in its OWN module, imported defensively by ``tools.py`` under the same
``try/except ImportError`` seam ``vision.py``/``session_powers.py`` use (see
``tools.py``'s ``register_tools`` docstring) -- Wave 2 has several workstreams
editing ``tools.py``'s core register/handler set at once (B1's
``browser_bridge_find``, B3's scoped snapshots), and this tool's own
idx-merge bookkeeping (``attach.AttachRegistry.merge_index_map``, this tab's
CURRENT index map as a wire param) is enough of its own thing to live
separately rather than growing that file's already-large surface further.

Entry point other modules use: ``register_inspect_tools(ctx)``. Everything
this file needs from ``tools.py`` (device/tab resolution, the gate, redaction,
error/hint plumbing) is imported as ``tools_mod`` and called through it --
nothing in ``tools.py`` needs to import this file at all except the one
defensive ``try/except ImportError`` block in ``register_tools``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import attach as attach_mod, audit, protocol, refusals, relay as relay_mod
from . import tools as tools_mod

QUESTIONS = (
    "scrollables",
    "at_point",
    "visibility",
    "expanded",
    "options",
    "form_state",
    "element",
    "listeners",
    "style",
)

# G3 (ProjectRules/speedimprovements.md): every question below except
# scrollables/at_point takes idx or selector, exactly the visibility/
# expanded/options contract this tuple already extends.
SELECTOR_QUESTIONS = ("visibility", "expanded", "options", "form_state", "element", "listeners", "style")

INSPECT_SCHEMA = {
    "name": "browser_bridge_inspect",
    "description": (
        "Answer ONE fixed layout question about the attached tab, read-only, no arbitrary code: "
        "'scrollables' (every visible element that actually scrolls: name, scroll %, more-above/below, "
        "rect, idx), 'at_point' (the topmost element at x,y: role/name/idx, and its nearest scrollable "
        "ancestor -- or whether the point hits another extension's overlay or an ungranted frame), "
        "'visibility' (idx/selector: visible / clipped by a scrollable region / off-screen / covered by "
        "something / display:none / zero-size), 'expanded' (idx/selector: expanded/collapsed/"
        "not-expandable, and how that was determined), 'options' (idx/selector of a select/listbox/"
        "combobox: its option labels, which are selected, disabled flags, capped at 100), "
        "'form_state' (idx/selector of a form/dialog/container: every field inside it -- label, type, "
        "value, required, disabled, readonly, checked/selected, validation message -- capped at 60 "
        "fields; password and other sensitive-field values are ALWAYS redacted), "
        "'element' (idx/selector: tag, role, name, rect, and an allowlisted, capped, redacted attribute "
        "dump), 'listeners' (idx/selector, FULL MODE ONLY: which event types are bound directly on the "
        "element vs. delegated from an ancestor up to 5 levels up, for click/keydown), "
        "'style' (idx/selector plus props from a fixed allowlist: computed style for those properties "
        "only). Prefer this over a screenshot when the question has a closed-form answer -- see the "
        "skill's own §1b. Gated exactly like browser_bridge_snapshot: refused unless the tab's current "
        "origin is 'full'. 'listeners' additionally needs the tab shared in full (debugger) mode."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "tab_id": {"type": "integer", "description": "Tab to inspect. Omit when exactly one tab is attached."},
            "tab": {"type": "string", "description": tools_mod.TAB_ALIAS_DESC},
            "question": {
                "type": "string",
                "enum": list(QUESTIONS),
                "description": "Exactly one of: scrollables, at_point, visibility, expanded, options, form_state, element, listeners, style.",
            },
            "idx": {
                "type": "integer",
                "description": (
                    "Element index from the tab's most recent browser_bridge_snapshot/inspect. Required "
                    "(with selector as the alternative) for visibility/expanded/options/form_state/element/"
                    "listeners/style. Unused for scrollables/at_point."
                ),
            },
            "selector": {
                "type": "string",
                "description": (
                    "CSS/pierce selector, as an alternative to idx, for visibility/expanded/options/"
                    "form_state/element/listeners/style."
                ),
            },
            "x": {"type": "number", "description": "Viewport-relative CSS px. Required for at_point only."},
            "y": {"type": "number", "description": "Viewport-relative CSS px. Required for at_point only."},
            "props": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "style only, required: computed-style property names to read, from a fixed allowlist "
                    "(display, visibility, opacity, overflow, overflow-x, overflow-y, pointer-events, "
                    "cursor, position, z-index, width, height). Anything outside that list is refused."
                ),
            },
        },
        "required": ["question"],
    },
}


def _redact_deep(value: Any, device_id: str) -> Tuple[Any, int]:
    """Applies `tools_mod._gateway_redact` to every string this result
    carries -- option labels, scrollable-region names, an at_point hit's
    name, a visibility 'covered by' name -- recursively through dicts/lists,
    the same belt-and-braces re-check every other capability's own text
    fields get (snapshot's `tree`, read's `content`, act's `diff`). Returns
    (redacted value, total new hits)."""
    if isinstance(value, str):
        return tools_mod._gateway_redact(value, device_id)
    if isinstance(value, list):
        total = 0
        out = []
        for item in value:
            redacted, hits = _redact_deep(item, device_id)
            out.append(redacted)
            total += hits
        return out, total
    if isinstance(value, dict):
        total = 0
        out = {}
        for k, v in value.items():
            redacted, hits = _redact_deep(v, device_id)
            out[k] = redacted
            total += hits
        return out, total
    return value, 0


def _validate_params(question: str, args: Dict[str, Any]) -> Optional[str]:
    """None when `question`'s own param combination is well-formed, else the
    refusal message (INVALID_INSPECT_QUESTION)."""
    if question not in QUESTIONS:
        return f"unsupported question {question!r} -- must be one of {', '.join(QUESTIONS)}"
    if question == "at_point":
        if not isinstance(args.get("x"), (int, float)) or not isinstance(args.get("y"), (int, float)):
            return "at_point requires numeric x and y"
        return None
    if question == "scrollables":
        return None
    # visibility / expanded / options / form_state / element / listeners / style
    if args.get("idx") is None and not args.get("selector"):
        return f"{question} requires idx or selector"
    if question == "style":
        props = args.get("props")
        if not isinstance(props, list) or not props or not all(isinstance(p, str) for p in props):
            return "style requires a non-empty props array of strings"
    return None


def handle_inspect(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = tools_mod._resolve_device(args)
    if err:
        return err
    tab, err = tools_mod._resolve_tab_target(device_id, args, kwargs, "inspect")
    if err:
        return err
    tab_id = tab["tabId"]
    origin = tab.get("origin") or tools_mod._origin_of(tab.get("url", ""))
    denial = tools_mod._gate_reason(device_id, origin, "inspect")
    if denial:
        return tools_mod._err(denial, code=protocol.GRANT_DENIED, device_id=device_id, tab_id=tab_id, origin=origin)

    question = str(args.get("question") or "")
    invalid = _validate_params(question, args)
    if invalid:
        reason, _code = refusals.format_refusal("invalid_inspect_question", question=question or "(none)")
        return tools_mod._err(invalid or reason, code=protocol.INVALID_INSPECT_QUESTION, hint=tools_mod._hint_for_code(protocol.INVALID_INSPECT_QUESTION))

    registry = attach_mod.get_registry()
    wire_params: Dict[str, Any] = {"tabId": tab_id, "question": question}

    if question in SELECTOR_QUESTIONS:
        selector = args.get("selector")
        if args.get("idx") is not None:
            idx = int(args["idx"])
            resolved = registry.resolve_index(device_id, tab_id, idx)
            if resolved is None:
                return tools_mod._err(
                    f"idx {idx} is not in tab {tab_id}'s current index map",
                    code=protocol.INVALID_PARAMS,
                    hint="the tab may have navigated or re-rendered since the last snapshot/inspect — call "
                    "browser_bridge_snapshot again and use a fresh idx",
                )
            selector = resolved
        wire_params["selector"] = selector
        if question == "style":
            wire_params["props"] = list(args.get("props") or [])
    elif question == "at_point":
        wire_params["x"] = args["x"]
        wire_params["y"] = args["y"]

    # B2's idx-merge contract (attach.AttachRegistry.merge_index_map, this
    # tab's CURRENT index map + next unused idx) -- only scrollables/at_point
    # can discover a not-yet-indexed element; visibility/expanded/options
    # never assign a new idx, so sending this for them costs a little wire
    # weight for nothing. Sent for all five anyway (simpler, and harmless):
    # the extension's IndexAssigner only ever reads it when it actually
    # allocates.
    existing_index_map = registry.current_index_map(device_id, tab_id)
    wire_params["existing_index_map"] = {str(k): v for k, v in existing_index_map.items()}
    # B4 idx-reuse fix: high_water_idx (survives a live-map element being
    # removed), never max(existing_index_map) + 1 — the latter reflects only
    # the CURRENT live map, which can shrink and reissue a stale idx to a
    # different element (see attach.py's high_water_idx() docstring).
    wire_params["next_idx"] = registry.high_water_idx(device_id, tab_id) + 1

    # G2.2 permission-model fix: same per-frame origin policy dom.snapshot
    # sends -- content/inspect.ts's walkComposed/hitTest apply it identically
    # for any same-origin inlined iframe this walk reaches.
    granted_origins, denied_origins = tools_mod._origin_policy_for_device(device_id)
    wire_params["granted_origins"] = granted_origins
    wire_params["denied_origins"] = denied_origins
    wire_params["default_full"] = False

    relay = relay_mod.get_relay()
    try:
        result = relay.call(device_id, "dom.inspect", wire_params)
    except relay_mod.BridgeError as exc:
        return tools_mod._bridge_err(exc, "dom.inspect")

    new_index_map_raw = result.pop("new_index_map", None) or result.pop("newIndexMap", None) or {}
    new_index_meta_raw = result.pop("new_index_meta", None) or result.pop("newIndexMeta", None) or {}
    new_index_map: Dict[int, str] = {}
    for raw_idx, selector in (new_index_map_raw or {}).items():
        try:
            new_index_map[int(raw_idx)] = str(selector)
        except (TypeError, ValueError):
            continue
    new_element_meta = tools_mod._parse_index_meta(new_index_meta_raw)
    if new_index_map:
        registry.merge_index_map(device_id, tab_id, new_index_map, new_element_meta)

    redacted_result, gateway_hits = _redact_deep(result, device_id)
    if gateway_hits:
        audit.record(
            "redaction_gateway_catch",
            device=device_id, tab_id=tab_id, origin=origin, hits=gateway_hits, capability="inspect",
        )

    audit.record(
        "tool_inspect", device=device_id, tab_id=tab_id, origin=origin, question=question,
        new_indices=len(new_index_map), redactions=gateway_hits,
    )
    if isinstance(redacted_result, dict):
        # `question` rides back verbatim in the content script's own answer
        # (content/inspect.ts's InspectAnswer always carries it) -- dropped
        # here so it doesn't collide with the explicit `question=question`
        # kwarg below (a second copy of the same value, not a real field).
        redacted_result.pop("tabId", None)
        redacted_result.pop("question", None)
    return tools_mod._ok(
        device_id=device_id,
        tab_id=tab_id,
        question=question,
        **(redacted_result if isinstance(redacted_result, dict) else {"data": redacted_result}),
    )


def register_inspect_tools(ctx) -> List[str]:
    ctx.register_tool(
        name=INSPECT_SCHEMA["name"],
        toolset=tools_mod.TOOLSET,
        schema=INSPECT_SCHEMA,
        handler=handle_inspect,
        check_fn=tools_mod.bridge_available,
        emoji="🔎",
    )
    return [INSPECT_SCHEMA["name"]]
