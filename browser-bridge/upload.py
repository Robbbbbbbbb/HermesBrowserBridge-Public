"""G1.3 (file upload): ``browser_bridge_upload``.

Two tiers, one tool, deliberately NOT routed through ``handle_act``/
``ACT_ACTIONS`` even though tier 1 ends up sending a ``page.act`` frame with
``action: "upload"`` on the wire (act.ts's ``actUpload`` reuses that module's
element-resolution/settle/diff machinery). The reason: ``handle_act`` gates
every action behind the single generic ``"act"`` capability, but upload is
one of G0.5's ``DANGEROUS_CAPABILITIES`` -- it needs its OWN capability
check (``once``-only, never a standing grant, per G0.5.4), its OWN
tier-specific power check (never the generic "either setting" gate --
``_CAPABILITY_POWER_KEYS["upload"]`` answers "is upload allowed AT ALL", the
wrong question at either tier's entry point, mirrored exactly by
``extension/src/background/powers.ts``'s own comment), and its OWN approval
summary (naming the file, never the page). Building that inside
``handle_act`` would mean either weakening every other action's capability
model to carry upload's extra rules, or littering ``handle_act`` with
``if action == "upload"`` special cases in the one function most other
workstreams also touch (coveragegaps.md §0.9 names ``handle_act`` a
contended file). A separate module with its own tool, following the same
shape as ``dialogs.py`` (its own capability, its own gate, its own wire
call), keeps that logic in one place with one owner.

Same ownership shape as dialogs.py/session_powers.py relative to tools.py:
freely imports tools.py's private helpers (``_resolve_device``,
``_resolve_attached_tab``, ``_authorize``, ``_origin_of``, ``_ok``/``_err``,
``_bridge_err``, ``_session_key``, ``_hint_for_code``) rather than
re-deriving them, and exposes exactly one public seam --
``register_upload_tools(ctx)`` -- imported under the same defensive
``try/except ImportError`` pattern ``tools.register_tools`` already uses.

## Path allowlist (tier 1) -- see extension/src/background/upload.ts's header

This gateway has NO filesystem access to the path at all: it names a file on
the BROWSER's host machine (a different computer on the LAN --
ProjectFiles/CLAUDE.md), never this gateway's own disk. So, exactly like the
extension's own copy of this check, this is STRING validation only:
  - any raw '..' segment is refused outright, never resolved
  - a hidden (leading '.') or named-secret path segment is refused even
    inside an allowed root
  - the remaining path must sit under one of this device's reported
    uploadRoots, compared segment-by-segment (never a bare string prefix)
A symlink inside an allowed root pointing outside it is a real, documented,
UNCLOSED gap on this side too, for the identical reason: neither this
process nor the extension can read the target machine's filesystem to see
through it. See coveragegaps.md G1.3.3's own correction and
tests/test_g13_upload.py's explicit proof of the gap.

## Tier 2's byte cap and magic-byte sniff, mirrored gateway-side

``extension/src/background/upload.ts`` enforces ``maxUploadBytes`` and a
magic-byte sniff before ever touching the page; this module enforces the
SAME two checks before the frame is even sent, using the device's own
reported policy (``state.get_power_policy``) -- belt-and-braces, the same
"neither layer trusts the other" rule as G3.6's cookie-write bound.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from . import audit, protocol, refusals, state
from . import relay as relay_mod
from . import tools as tools_mod
from . import attach as attach_mod

TOOLSET = tools_mod.TOOLSET

# Mirrors extension/src/lib/storage.ts's DEFAULT_SETTINGS.maxUploadBytes --
# used only as a last-resort fallback if a device's reported policy somehow
# carries a non-positive value; state.get_power_policy's own fail-closed
# default for an UNCONFIGURED device is 0 (deny all bytes), which this does
# NOT override -- see the size-cap check below, which reads the policy value
# directly and only falls back to this constant when it is <= 0 AND the
# device has otherwise been granted the capability (a configured device that
# explicitly reported 0 still means 0).
_FALLBACK_MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Mirrors upload.ts's BANNED_SEGMENTS exactly -- keep the two lists in sync
# by hand; there is no codegen step for this table, same as the
# CAPABILITY_POWER_KEYS drift the extension's own tests check for.
_BANNED_SEGMENTS = {
    ".ssh",
    ".aws",
    ".gnupg",
    ".gnupg-agent",
    ".docker",
    ".kube",
    ".azure",
    ".gcloud",
    ".netrc",
    ".mozilla",
    "keychains",
    "user data",
}


def _norm_segments(p: str) -> List[str]:
    return [s for s in p.replace("\\", "/").split("/") if s]


def _norm_segment_for_compare(segment: str, is_first: bool) -> str:
    if is_first and len(segment) == 2 and segment[1] == ":" and segment[0].isalpha():
        return segment.lower()
    return segment


def _is_under_root(path_segments: List[str], root_segments: List[str]) -> bool:
    if not root_segments:
        return False
    if len(path_segments) < len(root_segments):
        return False
    for i, root_seg in enumerate(root_segments):
        if _norm_segment_for_compare(path_segments[i], i == 0) != _norm_segment_for_compare(root_seg, i == 0):
            return False
    return True


# Orchestrator finding (post-merge probe): a NUL or other control character
# embedded in the path passed both validators. A NUL specifically can
# truncate the string a lower-level filesystem call sees -- this Python
# string keeps every character, but the OS call underneath the browser's own
# DOM.setFileInputFiles may stop at the first \0 -- so a path that LOOKS
# like it ends safely inside an allowed root could, in the OS's own reading
# of it, name something entirely different. Checked first, against the raw,
# unmodified string.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# Orchestrator finding: a path with no leading separator at all
# ("Users/you/Downloads/report.csv") also passed both validators, because
# _is_under_root only ever compared SEGMENTS and a relative path's segments
# can coincide exactly with an absolute root's -- the leading '/' was never
# part of the comparison. DOM.setFileInputFiles resolves a relative path
# against Chrome's own process working directory, which this allowlist has
# no way to see or bound. Checked against the RAW string, before any
# backslash normalisation, so "\Users\..." (root-relative, no drive letter)
# and "C:foo" (drive-relative, no separator after the colon) are each
# refused as not absolute too, exactly like a bare relative path.
_WINDOWS_DRIVE_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def _is_absolute_upload_path(raw_path: str) -> bool:
    return raw_path.startswith("/") or bool(_WINDOWS_DRIVE_ABSOLUTE.match(raw_path))


def _path_denial(reason_id: str, **params: str) -> Tuple[bool, str, int]:
    message, code = refusals.format_refusal(reason_id, **params)
    return False, message, code or protocol.UPLOAD_PATH_DENIED


def _validate_upload_path(raw_path: str, upload_roots: str) -> Tuple[bool, str, int]:
    """Mirrors extension/src/background/upload.ts's validateLocalUploadPath
    exactly (see this module's own header). Returns (ok, message, code)."""
    if _CONTROL_CHARS.search(raw_path):
        return _path_denial("upload_path_control_char", basename=_basename(raw_path))

    if not _is_absolute_upload_path(raw_path):
        return _path_denial("upload_path_not_absolute")

    roots = [r.strip() for r in upload_roots.splitlines() if r.strip()]
    if not roots:
        return _path_denial("upload_roots_empty")

    raw_segments = raw_path.replace("\\", "/").split("/")
    if any(s == ".." for s in raw_segments):
        return _path_denial("upload_path_traversal", basename=_basename(raw_path))

    path_segments = _norm_segments(raw_path)
    for seg in path_segments:
        if seg.startswith(".") or seg.lower() in _BANNED_SEGMENTS:
            return _path_denial("upload_path_hidden_segment", basename=_basename(raw_path), segment=seg)

    if not any(_is_under_root(path_segments, _norm_segments(root)) for root in roots):
        return _path_denial("upload_path_outside_roots", basename=_basename(raw_path))

    return True, "", 0


# Mirrors upload.ts's EXECUTABLE_SIGNATURES/KNOWN_SIGNATURES exactly -- see
# that file's own comment for why the zip family is deliberately excluded.
_EXECUTABLE_SIGNATURES: List[Tuple[str, bytes]] = [
    ("Windows PE (MZ)", b"\x4d\x5a"),
    ("ELF", b"\x7f\x45\x4c\x46"),
    ("Mach-O 32-bit", b"\xfe\xed\xfa\xce"),
    ("Mach-O 64-bit", b"\xfe\xed\xfa\xcf"),
    ("Mach-O 32-bit (reverse)", b"\xce\xfa\xed\xfe"),
    ("Mach-O 64-bit (reverse)", b"\xcf\xfa\xed\xfe"),
    ("Mach-O fat binary", b"\xca\xfe\xba\xbe"),
]

_KNOWN_SIGNATURES: List[Tuple[List[str], bytes, str]] = [
    (["image/png"], b"\x89PNG", "PNG"),
    (["image/jpeg", "image/jpg"], b"\xff\xd8\xff", "JPEG"),
    (["image/gif"], b"GIF8", "GIF"),
    (["application/pdf"], b"%PDF", "PDF"),
]


def _sniff_mismatch(data: bytes, declared_mime: str) -> Optional[str]:
    for name, sig in _EXECUTABLE_SIGNATURES:
        if data.startswith(sig):
            return f"decoded bytes are a {name} executable, refused regardless of declared mimeType {declared_mime!r}"
    mime_lower = declared_mime.lower()
    for prefixes, sig, name in _KNOWN_SIGNATURES:
        if any(mime_lower.startswith(p) for p in prefixes):
            if not data.startswith(sig):
                return f"declared mimeType {declared_mime!r} claims {name}, but the decoded bytes' own signature does not match"
            break
    return None


UPLOAD_SCHEMA = {
    "name": "browser_bridge_upload",
    "description": (
        "Choose a file for a file input in the attached tab. TWO TIERS, gated by different device "
        "settings, mutually exclusive per call: tier 1 (file_path) reads a file already ON THE "
        "BROWSER's OWN HOST MACHINE at an absolute path under one of that device's configured "
        "uploadRoots -- 'the file you just downloaded with browser_bridge_downloads' (its absolute "
        "filename) is the sanctioned way to learn a valid path, since there is no directory-listing "
        "tool. Tier 2 (content_base64) hands bytes you already hold -- no local path on that machine "
        "involved at all -- capped at that device's maxUploadBytes. Always requires the user's live "
        "approval (once per call, never a standing grant, regardless of origin mode) -- the summary "
        "they see names the filename and origin only, never a full path or the file's contents. "
        "Verification: the result's `file_info` ({name, size, type}) is what the page's own "
        "input.files[0] actually reports after the pick -- read it, the same way you'd read `diff` "
        "for any other act, rather than assuming success from a bare ok."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "tab_id": {"type": "integer", "description": "Tab to act in. Omit when exactly one tab is attached."},
            "tab": {"type": "string", "description": tools_mod.TAB_ALIAS_DESC},
            "idx": {
                "type": "integer",
                "description": "The <input type=file> element's index from the tab's most recent browser_bridge_snapshot or act. Preferred over selector, same staleness rules as browser_bridge_act's idx.",
            },
            "selector": {
                "type": "string",
                "description": "CSS selector for the <input type=file>, when you already know it instead of an idx.",
            },
            "file_path": {
                "type": "string",
                "description": "Tier 1: an absolute path on the BROWSER's host machine (never this gateway's), under one of that device's configured uploadRoots. Mutually exclusive with content_base64.",
            },
            "content_base64": {
                "type": "string",
                "description": "Tier 2: base64-encoded file bytes you already hold. Requires filename and mime_type too. Mutually exclusive with file_path.",
            },
            "filename": {
                "type": "string",
                "description": "Tier 2 only: the filename the page should see (input.files[0].name).",
            },
            "mime_type": {
                "type": "string",
                "description": "Tier 2 only: advisory -- never trusted over the decoded bytes' own magic number; a clear mismatch against a small set of known formats (PNG/JPEG/GIF/PDF) is refused, and any executable signature is refused regardless of this field.",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "Max time to wait for the action. Default 10000.",
            },
        },
    },
}

_DEFAULT_TIMEOUT_MS = 10000


def _basename(raw_path: str) -> str:
    # Handles both POSIX and Windows separators regardless of which OS this
    # gateway process itself runs on (os.path.basename only splits on the
    # host OS's own separator) -- the path names a file on a DIFFERENT
    # machine, so this gateway's own platform is irrelevant to how it parses.
    return raw_path.replace("\\", "/").rsplit("/", 1)[-1]


def handle_upload(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = tools_mod._resolve_device(args)
    if err:
        return err
    tab, err = tools_mod._resolve_tab_target(device_id, args, kwargs, "upload")
    if err:
        return err
    tab_id = tab["tabId"]
    origin = tab.get("origin") or tools_mod._origin_of(tab.get("url", ""))
    holder, err = tools_mod._resolve_session(device_id, kwargs)
    if err:
        return err

    file_path = args.get("file_path")
    content_b64 = args.get("content_base64")
    if bool(file_path) == bool(content_b64):
        return tools_mod._err(
            "exactly one of file_path (tier 1) or content_base64 (tier 2) is required, never both or neither",
            code=protocol.INVALID_PARAMS,
        )

    # Acting IS driving, same rule browser_bridge_act enforces (plan §3.3):
    # assigning a file input is exactly as much "driving the tab" as a click,
    # so it takes the same lease. Uses this device's own effective lease
    # (state.py's effective_lease_seconds), same as tools.py's
    # handle_attach/handle_act.
    registry = attach_mod.get_registry()
    try:
        registry.attach(
            device_id, tab_id, holder, tab_ref=tab,
            ttl=attach_mod.ttl_seconds_for(state.effective_lease_seconds(device_id)),
        )
    except attach_mod.AttachConflict as exc:
        audit.record("act_lease_conflict", device=device_id, tab_id=tab_id, holder=exc.holder, requester=holder, action="upload")
        return tools_mod._err(
            f"tab {tab_id} is driven by session {exc.holder}, not you",
            code=protocol.TARGET_BUSY,
            holder=exc.holder,
            hint=f"{tools_mod._lease_wait_hint(device_id)}, before acting in this tab",
        )

    selector: Optional[str] = args.get("selector")
    idx = args.get("idx")
    backend_node_id: Optional[int] = None
    if idx is not None:
        idx = int(idx)
        resolved_selector = registry.resolve_index(device_id, tab_id, idx)
        if resolved_selector is None:
            return tools_mod._err(
                f"idx {idx} is not in tab {tab_id}'s current index map",
                code=protocol.INVALID_PARAMS,
                hint="the tab may have navigated or re-rendered since the last snapshot -- call browser_bridge_snapshot again and use a fresh idx",
            )
        selector = resolved_selector
        backend_node_id = registry.resolve_index_node(device_id, tab_id, idx)
    if not selector:
        return tools_mod._err("upload needs idx or selector to say which <input type=file> to use", code=protocol.INVALID_PARAMS)

    timeout_ms = int(args.get("timeout_ms") or _DEFAULT_TIMEOUT_MS)

    if file_path:
        return _handle_tier1(device_id, tab_id, origin, holder, selector, backend_node_id, str(file_path), timeout_ms, idx)
    return _handle_tier2(device_id, tab_id, origin, holder, selector, str(content_b64), args, timeout_ms)


def _handle_tier1(
    device_id: str,
    tab_id: int,
    origin: str,
    holder: str,
    selector: str,
    backend_node_id: Optional[int],
    file_path: str,
    timeout_ms: int,
    idx: Optional[int],
) -> str:
    # Tier 1's OWN power check -- allowFileUpload specifically, never the
    # generic "either setting" _CAPABILITY_POWER_KEYS["upload"] entry
    # _authorize's device-power gate uses below. See this module's header
    # and powers.ts's identical reasoning on the extension side.
    if not state.power_enabled(device_id, "allowFileUpload"):
        reason = (
            "upload (local path) needs 'allowFileUpload' switched on for this device -- open the "
            "extension's Options page, tick it under Powers, and press Save."
        )
        audit.record("grant_check", device=device_id, origin=origin, capability="upload", mode="device_power_disabled", decision="deny", reason=reason)
        return tools_mod._err(reason, code=protocol.GRANT_DENIED, device_id=device_id, tab_id=tab_id, origin=origin)

    policy = state.get_power_policy(device_id)
    ok, message, code = _validate_upload_path(file_path, policy.get("uploadRoots", ""))
    if not ok:
        audit.record("grant_check", device=device_id, origin=origin, capability="upload", mode="path_denied", decision="deny", reason=message)
        return tools_mod._err(message, code=code, device_id=device_id, tab_id=tab_id, origin=origin)

    basename = _basename(file_path)
    # G1.3.7: the approval summary names the file's basename and origin --
    # never the full path (which can embed a username/home-directory layout)
    # and never the file's contents. Size is not knowable for tier 1 before
    # the call -- only the browser, after DOM.setFileInputFiles, can report
    # it -- so tier 1's summary is basename+origin only; tier 2's (below) can
    # include size because the bytes are already in hand.
    denial = tools_mod._authorize(
        device_id, origin, "upload",
        summary=f"upload {basename!r} to tab {tab_id} at {origin}",
        session_key=holder,
        detail=json.dumps({"basename": basename, "origin": origin, "tier": 1}),
    )
    if denial is not None:
        reason, code = denial
        return tools_mod._err(reason, code=code, device_id=device_id, tab_id=tab_id, origin=origin)

    wire_params: Dict[str, Any] = {
        "tabId": tab_id,
        "action": "upload",
        "selector": selector,
        "filePath": file_path,
        "timeout_ms": timeout_ms,
    }
    if backend_node_id is not None:
        wire_params["backendNodeId"] = backend_node_id
    if idx is not None:
        expected_meta = attach_mod.get_registry().resolve_index_meta(device_id, tab_id, idx)
        if expected_meta:
            wire_params["expect"] = expected_meta

    relay = relay_mod.get_relay()
    try:
        result = relay.call(device_id, "page.act", wire_params, timeout=timeout_ms / 1000.0 + 5)
    except relay_mod.BridgeError as exc:
        return tools_mod._bridge_err(exc, "page.act")

    file_info = result.get("fileInfo")
    audit.record(
        "tool_upload", device=device_id, holder=holder, tab_id=tab_id, origin=origin,
        basename=basename, size=(file_info or {}).get("size"), tier=1,
    )
    return tools_mod._ok(
        device_id=device_id, tab_id=tab_id, origin=origin, tier=1,
        changed=result.get("changed"), diff=result.get("diff"), file_info=file_info,
    )


def _handle_tier2(
    device_id: str,
    tab_id: int,
    origin: str,
    holder: str,
    selector: str,
    content_b64: str,
    args: Dict[str, Any],
    timeout_ms: int,
) -> str:
    filename = str(args.get("filename") or "").strip()
    mime_type = str(args.get("mime_type") or "application/octet-stream")
    if not filename:
        return tools_mod._err("content_base64 (tier 2) also needs filename", code=protocol.INVALID_PARAMS)

    # Tier 2's OWN power check -- allowFileUploadFromAgent specifically,
    # never allowFileUpload (tier 1's setting).
    if not state.power_enabled(device_id, "allowFileUploadFromAgent"):
        reason = (
            "upload (bytes from agent) needs 'allowFileUploadFromAgent' switched on for this device -- "
            "open the extension's Options page, tick it under Powers, and press Save."
        )
        audit.record("grant_check", device=device_id, origin=origin, capability="upload", mode="device_power_disabled", decision="deny", reason=reason)
        return tools_mod._err(reason, code=protocol.GRANT_DENIED, device_id=device_id, tab_id=tab_id, origin=origin)

    try:
        decoded = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError):
        return tools_mod._err("content_base64 is not valid base64", code=protocol.INVALID_PARAMS)

    policy = state.get_power_policy(device_id)
    cap = policy.get("maxUploadBytes", 0) or 0
    if cap <= 0:
        cap = 0  # an unconfigured/zeroed device denies every byte -- fail closed, no fallback here
    if len(decoded) > cap:
        return tools_mod._err(
            f"decoded size {len(decoded)} bytes exceeds this device's maxUploadBytes ({cap}) -- refused, never truncated",
            code=protocol.INVALID_PARAMS,
        )

    mismatch = _sniff_mismatch(decoded, mime_type)
    if mismatch:
        return tools_mod._err(f"upload: {mismatch}", code=protocol.INVALID_PARAMS)

    basename = _basename(filename)
    denial = tools_mod._authorize(
        device_id, origin, "upload",
        summary=f"upload {basename!r} ({len(decoded)} bytes) to tab {tab_id} at {origin}",
        session_key=holder,
        detail=json.dumps({"basename": basename, "size": len(decoded), "origin": origin, "tier": 2}),
    )
    if denial is not None:
        reason, code = denial
        return tools_mod._err(reason, code=code, device_id=device_id, tab_id=tab_id, origin=origin)

    wire_params = {
        "tabId": tab_id,
        "selector": selector,
        "filename": filename,
        "mimeType": mime_type,
        "contentBase64": content_b64,
    }
    relay = relay_mod.get_relay()
    try:
        result = relay.call(device_id, "page.upload", wire_params, timeout=timeout_ms / 1000.0 + 5)
    except relay_mod.BridgeError as exc:
        return tools_mod._bridge_err(exc, "page.upload")

    file_info = result.get("fileInfo")
    audit.record(
        "tool_upload", device=device_id, holder=holder, tab_id=tab_id, origin=origin,
        basename=basename, size=len(decoded), tier=2,
    )
    return tools_mod._ok(device_id=device_id, tab_id=tab_id, origin=origin, tier=2, file_info=file_info)


def register_upload_tools(ctx) -> List[str]:
    """Entry point ``tools.register_tools`` imports under
    ``try/except ImportError``, mirroring dialogs.py's/session_powers.py's own
    ``register_*_tools`` seam."""
    ctx.register_tool(
        name=UPLOAD_SCHEMA["name"],
        toolset=TOOLSET,
        schema=UPLOAD_SCHEMA,
        handler=handle_upload,
        check_fn=tools_mod.bridge_available,
        emoji="\U0001F4C1",
    )
    return [UPLOAD_SCHEMA["name"]]
