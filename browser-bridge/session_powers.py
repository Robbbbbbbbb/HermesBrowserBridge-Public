"""M3 "session powers" (plan.md §5 / §6.6): ``browser_bridge_fetch``,
``browser_bridge_cookies``, ``browser_bridge_network`` — the cURL-killer and
its two support tools.

Ownership note (different from vision.py's situation): this file and
``tools.py`` are owned by the *same* workstream (I), not two parallel ones.
vision.py deliberately avoided reaching into tools.py's underscored helpers
because it was built by a different workstream against a public-by-convention
seam (``register_vision_tools``). No such boundary exists here, so this
module freely imports and reuses tools.py's private helpers (``_resolve_
device``, ``_resolve_attached_tab``, ``_authorize``, ``_origin_of``, ``_ok``/
``_err``, the CC/SSN redaction primitives, etc.) rather than re-implementing
or duplicating them — that would only create a second place for the same bug
to hide. The public seam *to* tools.py is still exactly the vision.py
pattern: ``register_session_tools(ctx)``, imported under
``try/except ImportError`` from ``tools.register_tools`` so this module is
optional at import time the same way vision.py is.

## The SSRF guard (plan §6.6) — non-negotiable, gateway-side

``browser_bridge_fetch`` runs a real HTTP request *inside the attached tab's
page context* (extension-side, via ``Runtime.evaluate`` calling the page's own
``fetch()``) so it inherits cookies, CSRF headers, TLS/HTTP2 fingerprint and
referrer automatically — that inheritance is the entire point (plan §1: kill
the Copy-as-cURL loop). But it also means the browser is a **confused
deputy**: it can reach the user's whole LAN and any localhost admin panel,
regardless of what the *model* was ever supposed to touch. The guard here
enforces, before any wire frame is built:

  1. **Same-origin with the attached tab** — always allowed through to the
     normal grant/approval gate (``_authorize``, capability ``"fetch"``).
     This is the flagship case: a security console tab fetching its own
     `/web/api/...` GraphQL endpoint.
  2. **Cross-origin** — allowed only when the target origin has an
     **explicit** row in the grants table (``state.list_grants`` — never the
     config default-mode fallback ``state.get_mode`` would otherwise apply)
     with mode ``request`` or ``full``. This is the legitimate case from
     plan §1: helpdesk/backup/network/container/hypervisor consoles are different origins
     than whatever tab happens to be attached, and the user grants them
     individually in the popup.
  3. **Cross-origin AND ungranted AND the target resolves to loopback,
     link-local, or RFC1918/ULA private space (or an internal-looking
     hostname: ``localhost``, ``*.local``, ``*.internal``)** — refused with a
     *specifically worded* SSRF message and a dedicated audit event
     (``fetch_ssrf_refused``), not the generic cross-origin-ungranted text.
     This is the exact "browser reaches admin panels nobody granted"
     scenario plan §6.6 calls out. Note this is not a *stricter* rule than
     #2 — an explicitly granted private-network origin (a real Portainer/
     UniFi grant) is fine, same as any other granted origin; the private-
     network classification only changes the wording/audit trail for the
     *ungranted* case, because that's the case worth flagging loudly.
  4. **Non-http(s) schemes** (``file:``, ``chrome:``, ``javascript:``,
     ``data:``, ``ftp:``, ...) are refused outright regardless of origin —
     page-context ``fetch()`` has no business with any of them, and some
     (``file:``, ``chrome:``) would be a sandbox escape if ever honoured.

Known, documented limitation: origin classification here is string/literal
based (IP literals + a short list of internal hostname suffixes), not a live
DNS resolution. The actual network connection happens in the *browser*, not
on this gateway — a DNS lookup done here would not reliably reflect what the
browser actually contacts (and a blocking DNS call on every fetch is not
something this 2 vCPU box should be doing per plan.md §1b's memory/CPU
budget). This means a public-looking hostname that resolves (or is later
rebound) to an internal address is not caught here. Full protection against
that class of attack needs response-side/DNS-pinned enforcement, which is
squarely the ``Fetch.*`` CDP domain the plan explicitly keeps out of scope
until phase 3 (opt-in MITM interception). Flagged in this workstream's report
as a residual risk, not silently swept under the rug.

## Cookies (plan §5 / §6): the highest-sensitivity tool in the product

A session cookie *is* a credential. ``browser_bridge_cookies`` is gated
through the exact same ``_authorize`` seam as ``fetch``/``act`` — 'full'
mode or an explicit per-call approval, 'off' refuses, and 'request' mode
with no approval queue loaded refuses rather than silently allowing (the M2
seam's own invariant, inherited unchanged). On top of that gate, values are
withheld by default (``include_values`` must be explicitly set) because most
tasks ("is there a live session for this origin?", "what's the CSRF cookie
named?") never need the value itself — only ``name``/``domain``/``expiry``/
``httpOnly``/etc. And regardless of ``include_values``, the audit trail never
carries a cookie value — nor even a cookie *name*, which the plan doesn't
strictly require but costs nothing to also leave out.

## Network log (plan §5): the discovery half of the cURL-killer

``browser_bridge_network`` returns metadata (method/url/status/resourceType)
gated the normal ``_authorize`` way. Bodies are a *strictly higher* bar than
that: even an approved 'request'-mode call only ever gets metadata back —
bodies require a standing 'full' grant for the tab's origin specifically,
checked directly against ``state.get_mode`` rather than trusting
``_authorize``'s decision (an approval "allow" is not the same thing as a
standing full grant). This is what lets an agent discover the shape of a
site's own XHR calls (page params, header names) cheaply, then replay one
with real params via ``browser_bridge_fetch``.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import audit, protocol, refusals, relay as relay_mod, state
from . import tools as tools_mod

logger = logging.getLogger(__name__)

TOOLSET = tools_mod.TOOLSET

# -- fetch: tunables (tool-level; distinct from protocol/schema.json's own
#    page.fetch wire defaults, which the extension enforces independently) --
DEFAULT_FETCH_TIMEOUT_MS = 30000
DEFAULT_FETCH_MAX_BYTES = 2097152  # matches protocol/schema.json page.fetch default
# Headroom under transport.maxFrameBytes (8388608) so a full-size fetch body
# plus JSON-RPC envelope/header overhead can never itself blow the frame
# limit and turn into a transport-level failure instead of an honest
# max_bytes-truncation report.
MAX_FETCH_MAX_BYTES = 6 * 1024 * 1024
# How much of a (already up-to-max_bytes) text body actually lands in the
# tool result / model transcript. Separate axis from max_bytes on purpose:
# max_bytes bounds what the EXTENSION captures off the wire (network-level,
# can be large); preview_chars bounds what makes it into context (token
# budget, must stay small even when max_bytes is generous). Pagination
# ergonomics (fixtures/README.md's 2,452-finding GraphQL endpoint) depend on
# this distinction — a caller pages by tightening its own request body, not
# by fighting a single giant response.
DEFAULT_PREVIEW_CHARS = 20000
MIN_PREVIEW_CHARS = 500
MAX_PREVIEW_CHARS = 200000

DEFAULT_NETWORK_LIMIT = 50
MAX_NETWORK_LIMIT = 500
# Per-entry cap when include_bodies is honoured — independent of the fetch
# preview budget above: a network log can legitimately return many entries
# in one call, so each one's body must stay small or the call itself becomes
# the token-budget problem browser_bridge_network exists to avoid.
NETWORK_BODY_PREVIEW_CHARS = 4000

_LOOPBACK_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
_PRIVATE_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa")


# -- SSRF guard (plan §6.6) ---------------------------------------------------

def _parse_int_flexible(part: str) -> Optional[int]:
    """Parse one dot-separated component of a numeric IP literal the way
    curl/most browsers do: ``0x``-prefixed hex, old-style (leading-zero,
    no ``0x``) octal, or plain decimal. Python's own ``int(x, 0)`` refuses
    leading-zero octal without an explicit ``0o`` prefix, which is exactly
    the classic obfuscation form (``0177`` meaning octal 127) attackers use
    to slip a private IP literal past a naive string-based filter — see
    ``_parse_inet_aton_style`` below. Returns ``None`` for anything that
    isn't a clean numeric token (an ordinary hostname label, for instance),
    so callers can tell "not this grammar at all" apart from "parsed to 0"."""
    if not part:
        return None
    lowered = part.lower()
    if lowered.startswith("0x"):
        digits = lowered[2:]
        if not digits or any(c not in "0123456789abcdef" for c in digits):
            return None
        return int(digits, 16)
    if len(part) > 1 and part[0] == "0":
        digits = part[1:]
        if any(c not in "01234567" for c in digits):
            return None
        return int(digits, 8)
    if not part.isdigit():
        return None
    return int(part, 10)


def _parse_inet_aton_style(host: str) -> Optional[str]:
    """Best-effort re-implementation of the classic BSD ``inet_aton`` numeric
    host grammar that curl and every mainstream browser still accept for a
    bare hostname: 1-4 dot-separated parts, each decimal/octal/hex, with the
    LAST part absorbing whatever bits the earlier parts didn't consume —
    so ``2130706433``, ``0x7f000001``, ``0177.0.0.1`` and ``127.1`` are all
    the same address as ``127.0.0.1``. This is exactly the obfuscation this
    module's own SSRF guard must not be fooled by (plan §6.6): a private/
    loopback target dressed up in one of these forms is still a private/
    loopback target, not a hostname ``ipaddress.ip_address`` fails to parse
    and therefore silently waves through as "not private" (defect: it was
    still correctly BLOCKED as ungranted-cross-origin either way, since an
    unparseable host never matches an explicit grant either — but the audit
    trail called it generic ``cross_origin_ungranted`` instead of naming the
    SSRF attempt, which is the honesty gap this function exists to close).

    Returns the canonical dotted-decimal string, or ``None`` if ``host``
    doesn't match this grammar at all (including an ordinary hostname like
    ``example.com`` — its labels aren't numeric tokens, so
    ``_parse_int_flexible`` rejects them immediately)."""
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    values = [_parse_int_flexible(p) for p in parts]
    if any(v is None for v in values):
        return None
    n = len(values)
    # Classic inet_aton bit-width rule: every part except the last must fit
    # in a single byte; the last part absorbs the remaining (32 - 8*(n-1))
    # bits (so a bare `2130706433` is the full 32-bit address, `127.1` is
    # `127.0.0.1`, etc).
    for v in values[:-1]:
        if v is not None and v > 0xFF:
            return None
    last_bits = 32 - 8 * (n - 1)
    last = values[-1]
    if last is None or last < 0 or last >= (1 << last_bits):
        return None
    total = 0
    for v in values[:-1]:
        total = (total << 8) | (v or 0)
    total = (total << last_bits) | last
    return str(ipaddress.IPv4Address(total))


def _classify_private_host(hostname: str) -> Optional[str]:
    """Reason string if ``hostname`` looks loopback/link-local/private/
    internal, else None. String/literal classification only — see this
    module's docstring for why a live DNS resolution isn't done here.

    Tries a plain ``ipaddress.ip_address`` parse first, then falls back to
    ``_parse_inet_aton_style`` for decimal/octal/hex-obfuscated IPv4 literals
    (``http://2130706433/``, ``http://0177.0.0.1/``) that plain parsing
    rejects outright — those forms resolve to a real address in every
    browser, so classifying them accurately (and labelling the reason
    "obfuscated" when normalization was needed) matters even though an
    ungranted cross-origin target was already being blocked either way; see
    ``_parse_inet_aton_style``'s docstring for the honesty-gap this closes.
    """
    h = (hostname or "").strip().lower().rstrip(".")
    if not h:
        return "empty host"
    if h in _LOOPBACK_HOSTNAMES:
        return "loopback hostname"
    for suffix in _PRIVATE_HOST_SUFFIXES:
        if h.endswith(suffix):
            return f"internal-network hostname ({suffix} suffix)"
    literal = h[1:-1] if h.startswith("[") and h.endswith("]") else h  # strip IPv6 brackets
    obfuscated = False
    try:
        ip_obj = ipaddress.ip_address(literal)
    except ValueError:
        normalized = _parse_inet_aton_style(literal)
        if normalized is None:
            return None  # not an IP literal and not a recognised internal suffix
        try:
            ip_obj = ipaddress.ip_address(normalized)
        except ValueError:
            return None
        obfuscated = True
    prefix = "obfuscated (decimal/octal/hex) " if obfuscated else ""
    if ip_obj.is_loopback:
        return f"{prefix}loopback address"
    if ip_obj.is_link_local:
        return f"{prefix}link-local address"
    if ip_obj.is_private:
        return f"{prefix}private-network (RFC1918/ULA) address"
    if ip_obj.is_unspecified:
        return f"{prefix}unspecified address (0.0.0.0 / ::)"
    if ip_obj.is_reserved:
        return f"{prefix}reserved address"
    if ip_obj.is_multicast:
        return f"{prefix}multicast address"
    return None


def _explicit_grant_mode(device_id: str, origin: str) -> Optional[str]:
    """The grants-table mode for exactly ``origin``, or None if no row
    exists — deliberately NOT ``state.get_mode``'s config-default fallback.
    A cross-origin fetch target must be explicitly granted; falling back to
    whatever ``default_mode`` happens to be configured would defeat the
    guard's whole purpose the day someone changes that default."""
    for row in state.list_grants(device_id):
        if row["origin"] == origin:
            return row["mode"]
    return None


def _ssrf_guard(device_id: str, tab_origin: str, target_url: str) -> Optional[Tuple[str, int, str]]:
    """Returns None when ``target_url`` may proceed to the normal grant/
    approval gate, or ``(reason, protocol_code, audit_class)`` when it must
    be refused outright. Every refusal here is audited by the caller
    (handle_fetch) under a dedicated event, distinct from the generic
    grant_check ``_authorize`` emits, because these are the specific
    confused-deputy attempts plan §6.6 calls out by name."""
    try:
        parts = urlsplit(target_url)
    except ValueError:
        return "url could not be parsed", protocol.INVALID_PARAMS, "malformed_url"

    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return (
            f"browser_bridge_fetch only supports http/https URLs; {scheme or '(no scheme)'!r} is refused "
            f"outright (no file:, chrome:, javascript:, data:, ftp:, blob:, etc. — page-context fetch has "
            f"no legitimate use for any of them, and some would be a sandbox escape if honoured)",
            protocol.INVALID_PARAMS, "scheme",
        )

    hostname = parts.hostname or ""
    if not hostname:
        return "url has no host to fetch", protocol.INVALID_PARAMS, "no_host"

    target_origin = tools_mod._origin_of(target_url)
    if target_origin == tab_origin:
        return None  # same-origin as the attached tab: the flagship case

    private_reason = _classify_private_host(hostname)
    grant_mode = _explicit_grant_mode(device_id, target_origin)
    if grant_mode is None or grant_mode == "off":
        if private_reason:
            return (
                f"{target_url!r} targets {hostname!r} — a {private_reason} — cross-origin from the attached "
                f"tab ({tab_origin!r}) with no explicit grant for {target_origin!r}. Refused as a same-"
                f"network SSRF attempt: the browser can technically reach your LAN/localhost, but that "
                f"reachability is not the same thing as the user authorizing this tool to talk to it. Ask "
                f"the user to add an explicit grant for {target_origin!r} in the extension popup, or fetch "
                f"the attached tab's own origin instead.",
                protocol.GRANT_DENIED, "ssrf_private_network",
            )
        return (
            f"{target_url!r} ({target_origin!r}) is cross-origin from the attached tab ({tab_origin!r}) and "
            f"has no explicit grant. browser_bridge_fetch only reaches the attached tab's own origin, or an "
            f"origin the user has explicitly granted in the extension popup.",
            protocol.GRANT_DENIED, "cross_origin_ungranted",
        )
    return None  # explicit grant exists (request or full) — fall through to _authorize


# -- fetch: request/response shaping -----------------------------------------

def _strip_forbidden_headers(headers: Dict[str, Any]) -> Tuple[Dict[str, str], List[str]]:
    """Drop a caller-supplied ``Cookie`` header (case-insensitive).

    The entire point of page-context fetch is inheriting the browser's own
    cookie jar automatically (``credentials: include``) — letting a caller
    hand-craft a ``Cookie`` header would silently defeat that guarantee (and
    is never something a legitimate pagination/replay call needs). Stripped,
    not rejected outright, to keep the ergonomic path smooth; every strip is
    reported back in the result and audited.
    """
    cleaned: Dict[str, str] = {}
    stripped: List[str] = []
    for key, value in (headers or {}).items():
        if str(key).strip().lower() == "cookie":
            stripped.append(str(key))
            continue
        cleaned[str(key)] = "" if value is None else str(value)
    return cleaned, stripped


def _decode_wire_body(raw: str, encoding: str = "") -> bytes:
    """Decode ``page.fetch``'s ``body`` field.

    The extension states the encoding explicitly in ``bodyEncoding``
    ("utf-8" or "base64") and this trusts that field rather than guessing.
    Guessing is not a safe fallback here: ``b64decode("test", validate=True)``
    *succeeds* and yields garbage, so any "try base64, fall back to text" rule
    silently corrupts every plain-text body whose characters happen to be
    valid base64 — a JSON API returning `"data"` or a token string is exactly
    that shape. Silent corruption is the worst failure mode available, because
    the agent then reasons confidently about bytes that were never sent.

    When the field is absent (an older extension build) we fall back to the
    old permissive behaviour, since that is strictly better than refusing to
    read a body at all, and the mismatch is logged so it is diagnosable.
    """
    if not raw:
        return b""
    declared = (encoding or "").strip().lower()
    if declared in ("utf-8", "utf8", "text"):
        return raw.encode("utf-8", errors="surrogateescape")
    if declared == "base64":
        try:
            return base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            logger.warning(
                "browser_bridge: body declared base64 but did not decode; "
                "treating as literal text"
            )
            return raw.encode("utf-8", errors="surrogateescape")
    logger.debug("browser_bridge: fetch body carried no bodyEncoding; guessing")
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return raw.encode("utf-8", errors="surrogateescape")


def _redact_fetch_body(text: str, device_id: str) -> Tuple[str, int]:
    """CC/SSN-shaped redaction for fetch/network bodies — belt-and-braces,
    plan §6.3's gateway-side re-check extended to this new payload type.

    Deliberately narrower than tools.py's ``_gateway_redact``: that
    function's password-line heuristic assumes tools.py's line-based a11y-
    tree/diff text shape (``role "name" "value"``, one element per line).
    A fetch/network body is typically a single-line minified JSON blob —
    running that heuristic against it would match on any incidental mention
    of the word "password" anywhere in the payload and then blank out
    whatever quoted value happens to sit at the very end of the *entire*
    response, corrupting an unrelated field. Every OTHER pattern
    (card/ssn/email/phone) is shape-based, provably linear in input length
    (see ``tools_mod._EMAIL``/``tools_mod._PHONE``'s own comment), and safe
    for arbitrary text — including a single giant minified-JSON line —
    regardless of line structure, so those four run here; only the
    line-shape-dependent password heuristic is left to tools.py's own path.

    Honours `device_id`'s reported redaction policy exactly like
    ``tools_mod._gateway_redact`` — a kind switched off in the extension is
    skipped here too, not just in the a11y-tree/read path, so a fetch/network
    body doesn't quietly become the one place the checkbox doesn't apply.
    """
    if not text:
        return text, 0
    policy = state.get_redaction_policy(device_id)
    hits = 0

    def _sub_cc(match: "re.Match[str]") -> str:
        nonlocal hits
        digits = re.sub(r"[ -]", "", match.group(0))
        if len(digits) < 13 or len(digits) > 19 or not tools_mod._luhn_ok(digits):
            return match.group(0)
        hits += 1
        return tools_mod.REDACTED_PLACEHOLDER

    if policy["card"]:
        text = tools_mod._CC_CANDIDATE.sub(_sub_cc, text)

    def _sub_ssn(_match: "re.Match[str]") -> str:
        nonlocal hits
        hits += 1
        return tools_mod.REDACTED_PLACEHOLDER

    if policy["ssn"]:
        text = tools_mod._SSN.sub(_sub_ssn, text)

    def _sub_email(_match: "re.Match[str]") -> str:
        nonlocal hits
        hits += 1
        return tools_mod.REDACTED_PLACEHOLDER

    if policy["email"]:
        text = tools_mod._EMAIL.sub(_sub_email, text)

    def _sub_phone(_match: "re.Match[str]") -> str:
        nonlocal hits
        hits += 1
        return tools_mod.REDACTED_PLACEHOLDER

    if policy["phone"]:
        text = tools_mod._PHONE.sub(_sub_phone, text)
    return text, hits


FETCH_SCHEMA = {
    "name": "browser_bridge_fetch",
    "description": (
        "The cURL-killer: run an HTTP request INSIDE the attached tab's page context, inheriting "
        "its cookies, CSRF headers, TLS/HTTP2 fingerprint and referrer automatically — no manual "
        "Copy-as-cURL, no risk of silently dropping the session cookie. Use this for any console/API "
        "endpoint that only accepts a real browser session (many admin consoles 401 a bare API token "
        "but work fine from the page itself). Only reaches the attached tab's own origin, or an origin "
        "the user has explicitly granted in the extension popup — everything else, including LAN/"
        "localhost targets the browser could technically reach, is refused as SSRF. For paginated APIs "
        "(the common case), just call this again with a different `json_body` per page; large or binary "
        "responses are truncated/summarized honestly rather than corrupting your context — check "
        "`wire_truncated`/`preview_truncated`/`binary` before assuming you saw the whole thing. A "
        "non-2xx `status` is not a tool error — read it and the body to see why (401/403/etc are useful "
        "signal, not a failure to recover from)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "tab_id": {"type": "integer", "description": "Attached tab to fetch from. Omit when exactly one tab is attached."},
            "tab": {"type": "string", "description": tools_mod.TAB_ALIAS_DESC},
            "url": {"type": "string", "description": "Absolute URL to request."},
            "method": {"type": "string", "description": "HTTP method. Default GET."},
            "headers": {
                "type": "object",
                "description": "Extra request headers as string->string. A `Cookie` header is always stripped — cookies come from the browser's own jar via `credentials`.",
            },
            "body": {"type": "string", "description": "Raw request body. Mutually exclusive with json_body."},
            "json_body": {
                "type": "object",
                "description": (
                    "Convenience for JSON APIs (e.g. a paginated GraphQL findings endpoint): pass an "
                    "object here instead of pre-serializing it yourself. Automatically JSON-encoded and "
                    "given a Content-Type: application/json header if you didn't set one. This is the "
                    "ergonomic path for paging — change just the page-number/cursor field and call again."
                ),
            },
            "credentials": {
                "type": "string",
                "enum": ["include", "same-origin", "omit"],
                "description": "Cookie/session inclusion policy. Default include — the whole point of this tool.",
            },
            "timeout_ms": {"type": "integer", "description": f"Request timeout. Default {DEFAULT_FETCH_TIMEOUT_MS}."},
            "max_bytes": {
                "type": "integer",
                "description": (
                    f"Cap on bytes the EXTENSION captures off the wire (network-level). Default "
                    f"{DEFAULT_FETCH_MAX_BYTES}, hard-capped at {MAX_FETCH_MAX_BYTES}. Separate from "
                    f"preview_chars below — raise this only if you genuinely need more raw bytes captured, "
                    f"not just more shown to you."
                ),
            },
            "preview_chars": {
                "type": "integer",
                "description": (
                    f"Cap on how much TEXT actually lands in this result, independent of max_bytes — keeps "
                    f"one big response from blowing your context. Default {DEFAULT_PREVIEW_CHARS}. For a "
                    f"paginated endpoint, prefer asking the API for a smaller page over raising this."
                ),
            },
        },
        "required": ["url"],
    },
}


def handle_fetch(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = tools_mod._resolve_device(args)
    if err:
        return err
    tab, err = tools_mod._resolve_tab_target(device_id, args, kwargs, "fetch")
    if err:
        return err
    tab_id = tab["tabId"]
    tab_origin = tab.get("origin") or tools_mod._origin_of(tab.get("url", ""))

    url = str(args.get("url") or "").strip()
    if not url:
        return tools_mod._err("url is required")

    method = str(args.get("method") or "GET").strip().upper() or "GET"

    body = args.get("body")
    json_body = args.get("json_body")
    if body is not None and json_body is not None:
        return tools_mod._err("pass body or json_body, not both")

    headers_in = args.get("headers") or {}
    if not isinstance(headers_in, dict):
        return tools_mod._err("headers must be an object of string -> string")
    headers, stripped_headers = _strip_forbidden_headers(headers_in)

    if json_body is not None:
        try:
            body = json.dumps(json_body)
        except (TypeError, ValueError) as exc:
            return tools_mod._err(f"json_body is not JSON-serialisable: {exc}")
        if not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "application/json"

    credentials = args.get("credentials") or "include"
    if credentials not in ("include", "same-origin", "omit"):
        return tools_mod._err("credentials must be one of include, same-origin, omit", code=protocol.INVALID_PARAMS)

    timeout_ms = int(args.get("timeout_ms") or DEFAULT_FETCH_TIMEOUT_MS)
    max_bytes = int(args.get("max_bytes") or DEFAULT_FETCH_MAX_BYTES)
    max_bytes = max(1024, min(max_bytes, MAX_FETCH_MAX_BYTES))
    preview_chars = int(args.get("preview_chars") or DEFAULT_PREVIEW_CHARS)
    preview_chars = max(MIN_PREVIEW_CHARS, min(preview_chars, MAX_PREVIEW_CHARS))

    guard = _ssrf_guard(device_id, tab_origin, url)
    if guard is not None:
        reason, code, audit_class = guard
        audit.record(
            "fetch_ssrf_refused", device=device_id, tab_id=tab_id, tab_origin=tab_origin,
            target_url=url, reason_class=audit_class,
        )
        return tools_mod._err(reason, code=code, tab_id=tab_id, tab_origin=tab_origin)

    target_origin = tools_mod._origin_of(url)
    holder, err = tools_mod._resolve_session(device_id, kwargs)
    if err:
        return err

    summary = f"{method} {url}"
    detail = json.dumps(
        {
            "method": method,
            "url": url,
            "credentials": credentials,
            "header_keys": sorted(headers.keys()),
            "body_bytes": len(body.encode("utf-8", errors="surrogateescape")) if body else 0,
            "stripped_headers": stripped_headers,
        },
        default=str,
    )
    denial = tools_mod._authorize(device_id, target_origin, "fetch", summary=summary, session_key=holder, detail=detail)
    if denial is not None:
        reason, code = denial
        return tools_mod._err(reason, code=code, device_id=device_id, tab_id=tab_id, target_origin=target_origin)

    wire_params: Dict[str, Any] = {
        "tabId": tab_id,
        "url": url,
        "method": method,
        "credentials": credentials,
        "timeout_ms": timeout_ms,
        "max_bytes": max_bytes,
    }
    if headers:
        wire_params["headers"] = headers
    if body is not None:
        wire_params["body"] = body

    relay = relay_mod.get_relay()
    try:
        result = relay.call(device_id, "page.fetch", wire_params, timeout=timeout_ms / 1000.0 + 10)
    except relay_mod.BridgeError as exc:
        return tools_mod._bridge_err(exc, "page.fetch")

    status = result.get("status")
    resp_headers = result.get("headers") or {}
    wire_truncated = bool(result.get("truncated"))
    raw_bytes = _decode_wire_body(
        result.get("body") or "",
        str(result.get("bodyEncoding") or result.get("encoding") or ""),
    )

    content_type = ""
    for key, value in resp_headers.items():
        if str(key).lower() == "content-type":
            content_type = str(value)
            break

    payload: Dict[str, Any] = {
        "device_id": device_id,
        "tab_id": tab_id,
        "url": url,
        "method": method,
        "status": status,
        "ok": isinstance(status, int) and 200 <= status < 300,
        "headers": resp_headers,
        "content_type": content_type,
        "total_bytes": len(raw_bytes),
        "wire_truncated": wire_truncated,
    }
    if stripped_headers:
        payload["stripped_request_headers"] = stripped_headers

    try:
        text = raw_bytes.decode("utf-8")
        is_binary = False
    except UnicodeDecodeError:
        text = None
        is_binary = True

    if is_binary:
        payload["binary"] = True
        payload["body"] = None
        payload["sha256_16"] = hashlib.sha256(raw_bytes).hexdigest()[:16]
    else:
        text, redaction_hits = _redact_fetch_body(text, device_id)
        if redaction_hits:
            audit.record(
                "redaction_gateway_catch",
                device=device_id, tab_id=tab_id, origin=target_origin, hits=redaction_hits, capability="fetch",
            )
        payload["total_chars"] = len(text)
        preview_truncated = len(text) > preview_chars
        looks_json = "json" in content_type.lower() or text.lstrip()[:1] in ("{", "[")
        parsed_json = None
        if looks_json and not wire_truncated and not preview_truncated:
            try:
                parsed_json = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                parsed_json = None
        payload["binary"] = False
        if parsed_json is not None:
            # Parsed once, embedded structured — cheaper in tokens than a
            # re-escaped JSON-in-a-JSON-string blob, and exactly what makes
            # "loop over N pages of this" cheap (plan's pagination ask).
            payload["body_json"] = parsed_json
            payload["preview_truncated"] = False
        else:
            payload["body"] = text[:preview_chars]
            payload["preview_truncated"] = preview_truncated

    audit.record(
        "tool_fetch", device=device_id, holder=holder, tab_id=tab_id, tab_origin=tab_origin,
        target_origin=target_origin, method=method, url=url, status=status, bytes=len(raw_bytes),
        wire_truncated=wire_truncated, binary=is_binary,
    )
    return tools_mod._ok(**payload)


# -- cookies -------------------------------------------------------------------

COOKIES_SCHEMA = {
    "name": "browser_bridge_cookies",
    "description": (
        "Read structured cookie metadata ({name, domain, path, expiry, httpOnly, secure, sameSite}) "
        "for one or more URLs/origins. The highest-sensitivity tool in this toolset — a session cookie "
        "IS a credential — so it is gated hard: 'full' access mode or an explicit user approval, never "
        "silently allowed in 'request' mode. Values are WITHHELD by default; most tasks only need to "
        "know a session cookie exists (and its name/expiry), not its value — pass include_values:true "
        "only when you actually need to use the raw value yourself. Every call is audited by origin and "
        "count; cookie values are never written to the audit log even when include_values is true. "
        "path/httpOnly/secure/sameSite report the literal string \"unknown\" — never a fabricated "
        "default — when the connected extension build doesn't send that field at all; treat "
        "\"unknown\" as genuinely unknown, not as false/absent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "URLs (or bare origins) to read cookies for. Omit to default to the currently attached tab's own URL.",
            },
            "tab_id": {"type": "integer", "description": "Only used to pick a default when urls is omitted."},
            "tab": {"type": "string", "description": "Alternative to tab_id, only used to pick a default when urls is omitted -- " + tools_mod.TAB_ALIAS_DESC},
            "include_values": {
                "type": "boolean",
                "description": "Reveal actual cookie values. Default false — prefer the metadata-only default whenever you don't need to use the raw value.",
            },
        },
        "required": [],
    },
}

_WITHHELD_VALUE = "[withheld — pass include_values:true to reveal; every access is audited, never the value itself]"

# Defect fix: a cookie field the extension's wire object genuinely doesn't
# carry (an older build's cookies.get that predates sending secure/sameSite/
# path at all — see cookies.ts's WireCookie) must be reported as UNKNOWN, not
# silently defaulted to a plausible-looking value. A confidently-wrong
# `secure: false` is worse than an honest "we don't know": the model (or a
# person reading its output) has no way to tell a real "this cookie is not
# marked Secure" from "the extension never told us either way", and the
# former is exactly the kind of security-relevant fact this tool exists to
# report accurately.
_UNKNOWN_COOKIE_FIELD = "unknown"


def _wire_cookie_field(cookie: Dict[str, Any], key: str) -> Any:
    """The wire value for ``key`` if the extension's cookie object actually
    carries that key (even if the value itself is ``null``/``None`` — that's
    a real, meaningful value for e.g. ``sameSite``), or the sentinel string
    ``"unknown"`` if the key is absent entirely. Never a fabricated default."""
    if key not in cookie:
        return _UNKNOWN_COOKIE_FIELD
    return cookie[key]


def _wire_cookie_bool_field(cookie: Dict[str, Any], key: str) -> Any:
    """Same contract as ``_wire_cookie_field``, coerced to ``bool`` when the
    key IS present — ``httpOnly``/``secure`` are boolean-shaped on the wire,
    but ``bool(None)`` (Python's default-for-missing idiom used here before
    this fix) silently turns "the extension never sent this" into a
    confident, wrong ``False``."""
    if key not in cookie:
        return _UNKNOWN_COOKIE_FIELD
    return bool(cookie[key])


def handle_cookies(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = tools_mod._resolve_device(args)
    if err:
        return err
    paused = tools_mod._paused_refusal(device_id, "cookies")
    if paused:
        return paused

    urls = args.get("urls")
    if urls is not None and not isinstance(urls, list):
        return tools_mod._err("urls must be a list of URL strings")

    if not urls:
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
            urls = [candidates[0]["url"]]
        else:
            return tools_mod._err(
                "urls is required (no single attached tab to default it from)",
                hint="pass urls explicitly, or attach exactly one tab first",
                candidates=tools_mod._tab_briefs(tabs),
            )

    urls = [str(u) for u in urls if u]
    if not urls:
        return tools_mod._err("urls is empty")

    include_values = bool(args.get("include_values") or False)
    holder, err = tools_mod._resolve_session(device_id, kwargs)
    if err:
        return err
    origins = sorted({tools_mod._origin_of(u) for u in urls})

    for origin in origins:
        summary = f"read cookies for {origin}" + (" (VALUES requested)" if include_values else " (metadata only)")
        denial = tools_mod._authorize(
            device_id, origin, "cookies", summary=summary, session_key=holder,
            detail=json.dumps({"origin": origin, "include_values": include_values}),
        )
        if denial is not None:
            reason, code = denial
            audit.record(
                "tool_cookies_denied", device=device_id, holder=holder, origins=origins,
                include_values=include_values, denied_origin=origin,
            )
            return tools_mod._err(reason, code=code, device_id=device_id, origin=origin)

    relay = relay_mod.get_relay()
    try:
        result = relay.call(device_id, "cookies.get", {"urls": urls})
    except relay_mod.BridgeError as exc:
        return tools_mod._bridge_err(exc, "cookies.get")

    shaped: List[Dict[str, Any]] = []
    for cookie in result.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        entry = {
            "name": cookie.get("name"),
            "domain": cookie.get("domain"),
            "path": _wire_cookie_field(cookie, "path"),
            "expiry": cookie.get("expiry", cookie.get("expirationDate")),
            "httpOnly": _wire_cookie_bool_field(cookie, "httpOnly"),
            "secure": _wire_cookie_bool_field(cookie, "secure"),
            "sameSite": _wire_cookie_field(cookie, "sameSite"),
            "value": cookie.get("value") if include_values else _WITHHELD_VALUE,
        }
        shaped.append(entry)

    # Never the cookie's name OR value in the audit trail — only what was
    # asked for (origins, count, whether values were revealed). Plan only
    # requires withholding values; names are left out too, defense in depth,
    # since the audit trail gains nothing from them an operator can't already
    # get from origins + count.
    audit.record(
        "tool_cookies", device=device_id, holder=holder, origins=origins,
        count=len(shaped), include_values=include_values,
    )
    return tools_mod._ok(device_id=device_id, urls=urls, origins=origins, cookies=shaped, include_values=include_values)


# -- cookie writing (G3.6) ------------------------------------------------------
#
# Gated through `_authorize`'s "cookies_write" capability, one of G0.5's
# DANGEROUS_CAPABILITIES: a 'full' access mode does NOT cover this (the
# ceiling — see tools.py's `_dangerous_capability`), so every call either
# hits a standing per-capability grant (an earlier "always") or raises a
# live approval prompt, same as `evaluate`/`upload`/etc. Two more gates run
# before that, in order: `_paused_refusal` (sharing paused refuses outright),
# then the origin bound below.
#
# G3.6.3's origin bound is enforced HERE too, not only in the extension
# (cookies.ts): a set whose target origin isn't a currently-attached tab's
# origin is refused before `_authorize` is ever consulted. Neither layer
# trusts the other (coveragegaps.md §0.6) — the extension can be lied to by
# a compromised or out-of-date build; the gateway can be lied to by a rogue
# device claiming an origin is attached when it isn't. Known, documented
# gap this bound cannot close: a device with `allowEvaluate` on can set
# `document.cookie` directly through `Runtime.evaluate`, which never comes
# through this tool and has no origin check of its own (G1.5.0).
COOKIES_SET_SCHEMA = {
    "name": "browser_bridge_cookie_set",
    "description": (
        "Set or overwrite a single cookie via chrome.cookies.set, for a url whose origin matches a tab "
        "this device currently has ATTACHED (not merely open) — refused otherwise. Always prompts for "
        "approval; 'full' access mode does NOT cover this call (G0.5's per-capability ceiling), because "
        "a session cookie IS a credential and writing one changes a site's authentication state. Three "
        "real chrome.cookies traps, spelled out because a plausible-looking wrong value silently fails "
        "the call: "
        "(1) same_site has NO 'none' member — chrome.cookies.SameSiteStatus is "
        "no_restriction | lax | strict | unspecified; passing 'none' is refused before the call is even "
        "attempted, not left to fail inside Chrome. "
        "(2) same_site='no_restriction' REQUIRES secure=true AND an https:// url, or chrome.cookies.set "
        "fails outright — set both together or omit same_site entirely. "
        "(3) partition_key controls CHIPS (Cookies Having Independent Partitioned State): omitting it "
        "operates on the UNPARTITIONED cookie jar only, so a partitioned cookie the page itself set is "
        "invisible to this tool (and to browser_bridge_cookies) unless the matching partition_key is "
        "supplied. "
        "Where the value should come from: read it off the page yourself (browser_bridge_cookies with "
        "include_values, or a snapshot/read) and replay it here — ordinary session continuity, not "
        "exfiltration. A user supplying a cookie of their own belongs in the extension's own popup "
        "affordance, never pasted into chat with this tool: a session cookie in a chat transcript is "
        "exactly the credential exposure this whole design exists to avoid. Every call is audited as "
        "{origin, name, httpOnly, secure, sameSite, expires} — the value itself is never written to the "
        "audit log, never included in an error message, and never returned in this tool's own success "
        "payload."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "url": {
                "type": "string",
                "description": "Cookie is set for this URL's origin. Must match a tab this device currently has attached, or the call is refused.",
            },
            "name": {"type": "string"},
            "value": {"type": "string"},
            "path": {"type": "string"},
            "domain": {"type": "string"},
            "secure": {"type": "boolean"},
            "http_only": {"type": "boolean"},
            "same_site": {
                "enum": ["no_restriction", "lax", "strict", "unspecified"],
                "description": "See the traps in this tool's description — there is no 'none' value, and 'no_restriction' has two hard requirements.",
            },
            "expiration_date": {
                "type": "number",
                "description": "Seconds since epoch. Omit for a session cookie.",
            },
            "partition_key": {
                "type": "object",
                "description": "CHIPS partition key. See trap (3) above — omitting this is not the same as 'no partition', it means 'the unpartitioned jar'.",
            },
        },
        "required": ["url", "name", "value"],
    },
}

_VALID_SAME_SITE = ("no_restriction", "lax", "strict", "unspecified")


def handle_cookies_set(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = tools_mod._resolve_device(args)
    if err:
        return err
    paused = tools_mod._paused_refusal(device_id, "cookies_write")
    if paused:
        return paused

    url = str(args.get("url") or "").strip()
    if not url:
        return tools_mod._err("url is required")
    name = str(args.get("name") or "")
    if not name:
        return tools_mod._err("name is required")
    value = args.get("value")
    if not isinstance(value, str) or not value:
        return tools_mod._err("value is required")

    same_site = args.get("same_site")
    if same_site is not None:
        if same_site == "none":
            return tools_mod._err(
                "same_site \"none\" is not a valid chrome.cookies.SameSiteStatus value — use "
                "\"no_restriction\" instead (with secure:true and an https:// url)",
                code=protocol.COOKIE_SAME_SITE_INVALID,
            )
        if same_site not in _VALID_SAME_SITE:
            return tools_mod._err(
                f"same_site must be one of {', '.join(_VALID_SAME_SITE)} (got {same_site!r})",
                code=protocol.COOKIE_SAME_SITE_INVALID,
            )
        if same_site == "no_restriction":
            if not args.get("secure"):
                return tools_mod._err(
                    "same_site=\"no_restriction\" requires secure=true, or chrome.cookies.set fails",
                    code=protocol.COOKIE_SAME_SITE_INVALID,
                )
            if not url.lower().startswith("https://"):
                return tools_mod._err(
                    "same_site=\"no_restriction\" requires an https:// url, or chrome.cookies.set fails",
                    code=protocol.COOKIE_SAME_SITE_INVALID,
                )

    target_origin = tools_mod._origin_of(url)
    if not target_origin:
        return tools_mod._err(f"url is not a valid absolute URL: {url!r}")

    # G3.6.3 origin bound (gateway-side half — see module docstring above).
    tabs, exc = tools_mod._fresh_tabs(device_id)
    if exc is not None:
        return tools_mod._bridge_err(exc, "tabs.list")
    attached_origins = {
        (t.get("origin") or tools_mod._origin_of(t.get("url", "")))
        for t in tabs if t.get("attached")
    }
    attached_origins.discard("")
    if target_origin not in attached_origins:
        audit.record(
            "cookie_write_origin_refused", device=device_id, target_origin=target_origin,
            attached_origins=sorted(attached_origins),
        )
        message, code = refusals.format_refusal("cookie_write_origin_unattached", origin=target_origin)
        return tools_mod._err(message, code=code, device_id=device_id, origin=target_origin)

    holder, err = tools_mod._resolve_session(device_id, kwargs)
    if err:
        return err
    # G3.6.4: the detail passed to the approval prompt (and everything
    # audited below) is built from exactly this dict, on purpose — one place
    # a future field could accidentally reintroduce `value`, easy to audit
    # by inspection, rather than the value being filtered out after the
    # fact. `_authorize` also runs this through `_gateway_redact` as a
    # second, independent layer before it ever reaches the approval
    # transport.
    safe_fields: Dict[str, Any] = {
        "origin": target_origin,
        "name": name,
        "httpOnly": bool(args.get("http_only", False)),
        "secure": bool(args.get("secure", False)),
        "sameSite": same_site or "unspecified",
        "expires": args.get("expiration_date"),
    }
    summary = f"set cookie {name!r} for {target_origin}"
    denial = tools_mod._authorize(
        device_id, target_origin, "cookies_write", summary=summary, session_key=holder,
        detail=json.dumps(safe_fields, default=str),
    )
    if denial is not None:
        reason, code = denial
        audit.record("tool_cookies_set_denied", device=device_id, holder=holder, **safe_fields)
        return tools_mod._err(reason, code=code, device_id=device_id, origin=target_origin)

    wire_params: Dict[str, Any] = {"url": url, "name": name, "value": value}
    if args.get("path") is not None:
        wire_params["path"] = str(args["path"])
    if args.get("domain") is not None:
        wire_params["domain"] = str(args["domain"])
    if args.get("secure") is not None:
        wire_params["secure"] = bool(args["secure"])
    if args.get("http_only") is not None:
        wire_params["httpOnly"] = bool(args["http_only"])
    if same_site is not None:
        wire_params["sameSite"] = same_site
    if args.get("expiration_date") is not None:
        wire_params["expirationDate"] = args["expiration_date"]
    if args.get("partition_key") is not None:
        wire_params["partitionKey"] = args["partition_key"]

    relay = relay_mod.get_relay()
    try:
        result = relay.call(device_id, "cookies.set", wire_params)
    except relay_mod.BridgeError as exc:
        return tools_mod._bridge_err(exc, "cookies.set")

    # Whitelisted output shape, mirroring handle_cookies's own discipline:
    # never forward anything beyond what's named here, even if the
    # extension's result object carries more — and never the value, which
    # isn't even in `result` (cookies.ts's WireCookieSetResult never sends
    # it back).
    shaped = {
        "name": result.get("name", name),
        "domain": result.get("domain"),
        "path": result.get("path"),
        "httpOnly": bool(result.get("httpOnly", False)),
        "secure": bool(result.get("secure", False)),
        "sameSite": result.get("sameSite"),
        "expires": result.get("expires"),
    }
    audit.record("tool_cookies_set", device=device_id, holder=holder, origin=target_origin, **shaped)
    return tools_mod._ok(device_id=device_id, origin=target_origin, cookie=shaped)


# -- network log ---------------------------------------------------------------

NETWORK_SCHEMA = {
    "name": "browser_bridge_network",
    "description": (
        "Recent request log for the attached tab (method, url, status, resourceType, failed) — the "
        "discovery half of the cURL-killer: find the XHR/fetch a site's own frontend sends (endpoint "
        "shape, header names, page-size params) before replaying it yourself with browser_bridge_fetch. "
        "Metadata only by default. include_bodies additionally requests response bodies AND the "
        "request's own headers/postData — this is what makes an authenticated endpoint reproducible, "
        "since the x-csrf/tenant-scope headers a console session needs live in the site's own XHR, not "
        "the cookie jar. Honoured ONLY when the tab's origin is in 'full' access mode — an approved "
        "'request'-mode call still gets metadata only; this discovery data requires a standing full "
        "grant, not a one-off approval. Cookie/Authorization header VALUES are withheld from "
        "requestHeaders even then, gateway-side, regardless of what the extension sends. Use `filter` "
        "(a URL substring, e.g. '/graphql' or '/xspm/findings') to narrow a busy tab's log."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "tab_id": {"type": "integer", "description": "Tab to read the network log for. Omit when exactly one tab is attached."},
            "tab": {"type": "string", "description": tools_mod.TAB_ALIAS_DESC},
            "filter": {"type": "string", "description": "Substring match against request URLs."},
            "limit": {
                "type": "integer",
                "description": f"Max entries to return, most-recent-first. Default {DEFAULT_NETWORK_LIMIT}, capped at {MAX_NETWORK_LIMIT}.",
            },
            "include_bodies": {
                "type": "boolean",
                "description": (
                    "Also return response bodies AND each request's own headers/postData (truncated "
                    "per-entry), not just method/url/status/resourceType/failed. Only honoured when the "
                    "origin is in 'full' mode — otherwise degrades to metadata-only and says why in "
                    "bodies_omitted_reason. Even when honoured, a Cookie or Authorization header's VALUE "
                    "is never forwarded (withheld gateway-side, belt-and-braces on top of the extension's "
                    "own stripping) — only that the header name was present."
                ),
            },
        },
        "required": [],
    },
}

# Header VALUES that must never reach the model via requestHeaders, even
# though the extension is expected to already strip them (plan §6.3's
# belt-and-braces pattern, applied to a new payload shape: the gateway is
# never the only thing standing between a buggy/compromised extension build
# and a credential leak). Matched case-insensitively against the header name;
# the header KEY is kept (so the model can still see "this endpoint requires
# an Authorization header") but its value is replaced with a placeholder.
_SENSITIVE_REQUEST_HEADER_NAMES = frozenset({"cookie", "authorization"})
_WITHHELD_HEADER_VALUE = "[withheld gateway-side — never forwarded, even if the extension sent it]"


def _scrub_sensitive_request_headers(headers: Any) -> Tuple[Dict[str, str], List[str]]:
    """Belt-and-braces re-check for ``network.log`` entries' ``requestHeaders``
    (protocol/schema.json: "Cookie/Authorization values are stripped
    extension-side") — never trusted as the only guard. Returns
    (scrubbed headers, names actually scrubbed here). Non-dict input (a
    malformed or absent field) returns ``({}, [])`` rather than raising."""
    if not isinstance(headers, dict):
        return {}, []
    scrubbed: Dict[str, str] = {}
    caught: List[str] = []
    for key, value in headers.items():
        name = str(key)
        if name.strip().lower() in _SENSITIVE_REQUEST_HEADER_NAMES:
            scrubbed[name] = _WITHHELD_HEADER_VALUE
            caught.append(name)
        else:
            scrubbed[name] = "" if value is None else str(value)
    return scrubbed, caught


def handle_network(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = tools_mod._resolve_device(args)
    if err:
        return err
    tab, err = tools_mod._resolve_tab_target(device_id, args, kwargs, "network")
    if err:
        return err
    tab_id = tab["tabId"]
    origin = tab.get("origin") or tools_mod._origin_of(tab.get("url", ""))

    limit = int(args.get("limit") or DEFAULT_NETWORK_LIMIT)
    limit = max(1, min(limit, MAX_NETWORK_LIMIT))
    filter_str = str(args.get("filter") or "")
    want_bodies = bool(args.get("include_bodies") or False)
    holder, err = tools_mod._resolve_session(device_id, kwargs)
    if err:
        return err

    summary = f"read network log for tab {tab_id} at {origin}" + (" (bodies requested)" if want_bodies else "")
    denial = tools_mod._authorize(
        device_id, origin, "network", summary=summary, session_key=holder,
        detail=json.dumps({"filter": filter_str, "limit": limit, "include_bodies": want_bodies}),
    )
    if denial is not None:
        reason, code = denial
        return tools_mod._err(reason, code=code, device_id=device_id, tab_id=tab_id, origin=origin)

    # Bodies need a standing 'full' grant, independent of the approval this
    # call itself may have just gotten under 'request' mode — see module
    # docstring.
    bodies_allowed = want_bodies and state.get_mode(device_id, origin) == "full"
    bodies_omitted_reason = ""
    if want_bodies and not bodies_allowed:
        bodies_omitted_reason = (
            f"origin {origin!r} is not in 'full' access mode; network bodies require a standing full "
            f"grant, not just this call's approval — metadata is returned instead"
        )

    wire_params: Dict[str, Any] = {"tabId": tab_id, "limit": limit}
    if filter_str:
        wire_params["filter"] = filter_str
    if bodies_allowed:
        # protocol/schema.json's network.log now declares `bodies` as a real
        # param ("include request headers and request post bodies ... this
        # is what makes an authenticated endpoint reproducible"), not the
        # additive/ignorable hint it used to be here before the extension
        # implemented it. An extension build that doesn't understand it yet
        # just omits entries[].body/requestHeaders/postData, and this tool
        # reports bodies_included=false either way rather than assuming the
        # extension complied.
        wire_params["bodies"] = True

    relay = relay_mod.get_relay()
    try:
        result = relay.call(device_id, "network.log", wire_params)
    except relay_mod.BridgeError as exc:
        return tools_mod._bridge_err(exc, "network.log")

    shaped: List[Dict[str, Any]] = []
    any_body_returned = False
    any_discovery_data_returned = False
    scrubbed_header_names: set = set()
    for entry in result.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        # Whitelisted output shape — never forward anything beyond what's
        # named here, even if the extension's entry object carries more.
        # `failed` is basic request metadata (did it complete at all), not
        # gated behind include_bodies/full mode, same as status/resourceType.
        shaped_entry: Dict[str, Any] = {
            "method": entry.get("method"),
            "url": entry.get("url"),
            "status": entry.get("status"),
            "resourceType": entry.get("resourceType"),
            "failed": bool(entry.get("failed", False)),
        }
        if bodies_allowed and entry.get("body") is not None:
            body_text = str(entry.get("body"))
            body_text, hits = _redact_fetch_body(body_text, device_id)
            if hits:
                audit.record(
                    "redaction_gateway_catch",
                    device=device_id, tab_id=tab_id, origin=origin, hits=hits, capability="network",
                )
            shaped_entry["body"] = body_text[:NETWORK_BODY_PREVIEW_CHARS]
            shaped_entry["body_truncated"] = len(body_text) > NETWORK_BODY_PREVIEW_CHARS
            any_body_returned = True
        if bodies_allowed and entry.get("requestHeaders") is not None:
            # Belt-and-braces re-check (plan §6.3's pattern, applied here):
            # the schema says the extension already strips Cookie/
            # Authorization VALUES, but this gateway is never the only thing
            # standing between a buggy/compromised extension build and a
            # credential leak — see _scrub_sensitive_request_headers.
            scrubbed_headers, caught = _scrub_sensitive_request_headers(entry.get("requestHeaders"))
            if caught:
                scrubbed_header_names.update(caught)
            shaped_entry["requestHeaders"] = scrubbed_headers
            any_discovery_data_returned = True
        if bodies_allowed and entry.get("postData") is not None:
            post_text = str(entry.get("postData"))
            post_text, hits = _redact_fetch_body(post_text, device_id)
            if hits:
                audit.record(
                    "redaction_gateway_catch",
                    device=device_id, tab_id=tab_id, origin=origin, hits=hits, capability="network_postdata",
                )
            shaped_entry["postData"] = post_text[:NETWORK_BODY_PREVIEW_CHARS]
            shaped_entry["postData_truncated"] = len(post_text) > NETWORK_BODY_PREVIEW_CHARS
            any_discovery_data_returned = True
        shaped.append(shaped_entry)

    if scrubbed_header_names:
        audit.record(
            "network_header_gateway_catch",
            device=device_id, tab_id=tab_id, origin=origin, headers=sorted(scrubbed_header_names),
        )
    if any_discovery_data_returned:
        # Discovery data (requestHeaders/postData) is a step up from plain
        # response-body metadata — it's the site's own auth material shape
        # (which headers it sends, what its CSRF token looks like), so every
        # return of it is audited on its own event, distinct from the
        # generic tool_network below, independent of whether the redaction
        # re-check above caught anything.
        audit.record(
            "network_discovery_returned",
            device=device_id, holder=holder, tab_id=tab_id, origin=origin,
            entries_with_discovery_data=sum(
                1 for e in shaped if "requestHeaders" in e or "postData" in e
            ),
        )

    audit.record(
        "tool_network", device=device_id, holder=holder, tab_id=tab_id, origin=origin,
        count=len(shaped), include_bodies_requested=want_bodies, bodies_included=any_body_returned,
        discovery_data_included=any_discovery_data_returned,
    )
    payload: Dict[str, Any] = {
        "device_id": device_id,
        "tab_id": tab_id,
        "origin": origin,
        "entries": shaped,
        "count": len(shaped),
        "bodies_included": any_body_returned,
        "discovery_data_included": any_discovery_data_returned,
    }
    if bodies_omitted_reason:
        payload["bodies_omitted_reason"] = bodies_omitted_reason
    return tools_mod._ok(**payload)


# -- console log reading (G2.4) ------------------------------------------------
#
# `browser_bridge_console` mirrors `browser_bridge_network`'s shape closely:
# metadata (in this case, already-redacted text) shaped from a whitelisted set
# of fields, gated by the same `_authorize` seam every other M3-style tool
# uses. `console` is one of G0.5's DANGEROUS_CAPABILITIES (approvals.py) and
# has a device-power-policy entry (`allowConsoleRead`, tools.py's
# `_CAPABILITY_POWER_KEYS`) — both already wired before this task, so
# `_authorize(..., "console", ...)` below gets the full two-layer gate for
# free: G0.9's operator kill switch, G0.6.6's device-reported power policy,
# G0.5's per-capability ceiling (a 'full' origin grant does not bypass it),
# and the approval queue.

DEFAULT_CONSOLE_LIMIT = 50
MAX_CONSOLE_LIMIT = 500
CONSOLE_TEXT_PREVIEW_CHARS = 4000

CONSOLE_SCHEMA = {
    "name": "browser_bridge_console",
    "description": (
        "Read the attached tab's console output — console.log/warn/error/etc calls, uncaught "
        "exceptions/unhandled rejections, and browser-generated messages (deprecations, security "
        "warnings, network/CSP violations). The buffer starts filling the moment the tab is "
        "ATTACHED, not at first read — a load-time error is already captured by the time you first "
        "call this. Gated behind the device's 'Allow console read' setting (off by default) and the "
        "'console' capability/approval — this is one of the most revealing read-only tools in the "
        "toolset (it can surface anything the page's own scripts print, including bugs the GUI "
        "never shows), which is why it needs an explicit opt-in rather than riding on 'full' access "
        "mode the way snapshot/read do. Bearer tokens, JWTs, API keys and secret-shaped URL query "
        "params are redacted before this ever reaches you, both extension-side and here — treat a "
        "`[redacted:token]` marker as 'a secret was here', not as a bug to work around."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "tab_id": {"type": "integer", "description": "Attached tab to read console output from. Omit when exactly one tab is attached."},
            "tab": {"type": "string", "description": tools_mod.TAB_ALIAS_DESC},
            "level_filter": {
                "type": "string",
                "enum": ["verbose", "info", "warning", "error"],
                "description": "Return only entries at exactly this normalised level.",
            },
            "text_filter": {"type": "string", "description": "Case-insensitive substring match against entry text."},
            "limit": {
                "type": "integer",
                "description": f"Max entries to return, most-recent-first. Default {DEFAULT_CONSOLE_LIMIT}, capped at {MAX_CONSOLE_LIMIT}.",
            },
            "since": {
                "type": "integer",
                "description": "ms since epoch; return only entries captured at or after this time (for polling 'what's new since I last looked').",
            },
        },
        "required": [],
    },
}

# Same shapes as lib/redaction.ts's `redactSecrets` (extension/src/lib/
# redaction.ts) — an independent second implementation of the same detector,
# same "two independent implementations of the same rule, neither trusts the
# other" pattern _gateway_redact/_redact_fetch_body already use for
# card/ssn/email/phone. Every quantifier below is explicitly bounded, same
# ReDoS discipline as tools.py's own _EMAIL/_PHONE.
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
# G1.6.8: bare AWS access key ids — mirrors extension/src/lib/redaction.ts's
# `AWS_ACCESS_KEY_PATTERN` (and tools.py's own `_AWS_ACCESS_KEY`) exactly.
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_TOKEN_PLACEHOLDER = "[redacted:token]"

# G1.6.8 (part C): mirrors extension/src/background/console.ts's key-name
# redaction over a rendered object/array preview (`{token: ..., userId: 42}`)
# — the extension already does this BEFORE the string ever reaches the
# gateway, so ordinarily this never fires. It exists for the same reason
# every other gateway pass in this module exists: a device that failed to
# redact (an old/misbehaving extension build) must not be trusted blind. The
# gateway never sees `preview.properties` structured data, only the already-
# joined string, so unlike the extension this is necessarily a STRING-LEVEL
# `key: value` / `"key": "value"` pattern match rather than a per-property
# check — see `_is_secret_key_name` for the key-matching rule itself, which
# is the same whole-segment rule as the extension's `isSecretKeyName`
# (ProjectFiles/extension/src/background/console.ts), reimplemented
# independently rather than shared, same as every other detector pair in
# this file.
_CAMEL_BOUNDARY_1 = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_BOUNDARY_2 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_KEY_SEGMENT_SPLIT = re.compile(r"[^A-Za-z0-9]+")

# Widened list (coordinator review) — kept identical to the extension's own
# `SECRET_KEY_SINGLE_TERMS`/`SECRET_KEY_COMPOUND_TERMS`
# (extension/src/background/console.ts): `bearer`, `jwt`, `cookie`,
# `credential`/`credentials`, `passphrase`, `signature` as bare terms, plus
# `secret_key`/`private_key` as compound (adjacent-segment) pairs alongside
# the original `api_key`/`access_token`/`refresh_token`/`client_secret`. See
# that file's own comment for the full per-term rationale and what was
# deliberately left OUT (`id`/`key`/`code`/`value`/`name` alone — each too
# common in ordinary, non-secret field names to add as a bare term).
# `test_console_preview_key_redaction.py` parses console.ts's own source and
# asserts these two collections match it exactly, so the two can't drift.
_SECRET_KEY_SINGLE_TERMS = {
    "token",
    "secret",
    "password",
    "passwd",
    "pwd",
    "apikey",
    "authorization",
    "auth",
    "session",
    "bearer",
    "jwt",
    "cookie",
    "credential",
    "credentials",
    "passphrase",
    "signature",
}
_SECRET_KEY_COMPOUND_TERMS = (
    ("api", "key"),
    ("access", "token"),
    ("refresh", "token"),
    ("client", "secret"),
    ("secret", "key"),
    ("private", "key"),
)


def _key_segments(name: str) -> List[str]:
    normalized = _CAMEL_BOUNDARY_1.sub(r"\1_\2", name)
    normalized = _CAMEL_BOUNDARY_2.sub(r"\1_\2", normalized)
    return [seg.lower() for seg in _KEY_SEGMENT_SPLIT.split(normalized) if seg]


def _is_secret_key_name(name: str) -> bool:
    segments = _key_segments(name)
    if any(seg in _SECRET_KEY_SINGLE_TERMS for seg in segments):
        return True
    for first, second in _SECRET_KEY_COMPOUND_TERMS:
        for i in range(len(segments) - 1):
            if segments[i] == first and segments[i + 1] == second:
                return True
    return False


# Two of three shapes here (the third, `key => value` for Map entries, is
# `_ARROW_KEY_VALUE` below — kept separate, see its own comment): the
# extension's own bare `name: value` preview rendering (value runs to the
# next `,`/`}`/`]`, matching `finishPreviewSummary`'s own join), and a quoted
# `"key": "value"` JSON-ish shape a differently-built preview renderer (or a
# raw `JSON.stringify` dump) might produce instead.
# Both key and value groups are explicitly length-bounded (ReDoS discipline
# matching every other pattern in this file); the bare pattern's negative
# lookbehind keeps it from re-matching a key immediately after a quote
# (avoiding overlap with the quoted pattern's own key). Both patterns also
# carry a negative LOOKAHEAD refusing to match a value that already starts
# with the token placeholder — not just belt-and-suspenders idempotency:
# the bare value's excluded-character class must exclude `]` (an array's own
# closing bracket), and `_TOKEN_PLACEHOLDER` itself contains a literal `]`,
# so a POST-HOC "does the captured value already contain the placeholder"
# check would see a value truncated one character short of the marker's own
# `]` and redact it a second time, doubling the bracket. Refusing the match
# up front side-steps that truncation entirely. The lookahead itself allows
# LEADING WHITESPACE before the marker (`\s*\[redacted:`, not just
# `\[redacted:`) — without it, the preceding `\s*` backtracks to consuming
# zero characters so the assertion can still pass with the separating space
# swallowed into the captured value instead, silently defeating the guard.
_QUOTED_KEY_VALUE = re.compile(r'"(?P<key>[A-Za-z_][A-Za-z0-9_]{0,63})"\s*:\s*"(?!\s*\[redacted:)(?P<value>[^"]{0,2000})"')
_BARE_KEY_VALUE = re.compile(r'(?<![\w"])(?P<key>[A-Za-z_][A-Za-z0-9_]{0,63})\s*:\s*(?!\s*\[redacted:)(?P<value>[^,}\]"\n]{1,2000})')
# A THIRD shape (G1.6.8 coordinator review): the extension's own Map preview
# rendering is `key => value`, not `key: value` — `renderPreview`'s entries
# branch (background/console.ts) joins a Map entry with a literal " => ",
# unquoted on both sides, same as a plain object's `name: value` is
# unquoted. Left OUT of `_BARE_KEY_VALUE` itself rather than making that
# pattern match either separator, because `:`  and `=>` need different
# surrounding context in practice (a bare object property vs. a Map entry)
# and folding them into one alternation regex only makes the already-dense
# pattern harder to audit for the same ReDoS/truncation discipline the other
# patterns already need. Same excluded-value character class, same
# already-redacted lookahead, same reasoning as `_BARE_KEY_VALUE` throughout.
_ARROW_KEY_VALUE = re.compile(r'(?<![\w"])(?P<key>[A-Za-z_][A-Za-z0-9_]{0,63})\s*=>\s*(?!\s*\[redacted:)(?P<value>[^,}\]"\n]{1,2000})')


def _redact_preview_key_values(text: str) -> Tuple[str, int]:
    """Key-name-based redaction over a rendered preview string — the belt to
    the extension's braces (`renderPreview`'s eager key-name check). Runs
    UNCONDITIONALLY, same as the rest of `_redact_console_secrets`: a
    property literally named `token`/`secret`/`password`/... has no
    legitimate reason to reach the agent regardless of its value's shape,
    and regardless of whether the device's redaction policy has any
    particular kind toggled off."""
    hits = 0

    def _sub_quoted(match: "re.Match[str]") -> str:
        nonlocal hits
        key = match.group("key")
        if not _is_secret_key_name(key):
            return match.group(0)
        hits += 1
        return f'"{key}": "{_TOKEN_PLACEHOLDER}"'

    text = _QUOTED_KEY_VALUE.sub(_sub_quoted, text)

    def _sub_bare(match: "re.Match[str]") -> str:
        nonlocal hits
        key = match.group("key")
        if not _is_secret_key_name(key):
            return match.group(0)
        hits += 1
        return f"{key}: {_TOKEN_PLACEHOLDER}"

    text = _BARE_KEY_VALUE.sub(_sub_bare, text)

    def _sub_arrow(match: "re.Match[str]") -> str:
        nonlocal hits
        key = match.group("key")
        if not _is_secret_key_name(key):
            return match.group(0)
        hits += 1
        return f"{key} => {_TOKEN_PLACEHOLDER}"

    text = _ARROW_KEY_VALUE.sub(_sub_arrow, text)

    return text, hits


def _redact_console_secrets(text: str) -> Tuple[str, int]:
    """Belt-and-braces re-check for `console.entries` text/url/stack-frame-url
    fields (G2.4.4) — the extension already redacts these before the frame
    leaves the machine (background/console.ts's `redactEntry`); this is the
    gateway-side second pass, unconditional (not gated by the redaction
    policy the way card/ssn/email/phone are — see lib/redaction.ts's own
    comment for why a bearer token/JWT/API key isn't treated as an
    ambiguous, opt-out-able kind the way a phone number is).
    """
    if not text:
        return text, 0
    hits = 0

    def _mark(_match: "re.Match[str]") -> str:
        nonlocal hits
        hits += 1
        return _TOKEN_PLACEHOLDER

    text = _JWT.sub(_mark, text)

    def _bearer_sub(match: "re.Match[str]") -> str:
        nonlocal hits
        if _TOKEN_PLACEHOLDER in match.group(0):
            return match.group(0)
        hits += 1
        return f"Bearer {_TOKEN_PLACEHOLDER}"

    text = _BEARER.sub(_bearer_sub, text)

    def _api_key_sub(match: "re.Match[str]") -> str:
        nonlocal hits
        whole = match.group(0)
        if _TOKEN_PLACEHOLDER in whole:
            return whole
        hits += 1
        sep_pos = min((p for p in (whole.find(":"), whole.find("=")) if p != -1), default=-1)
        sep = whole[sep_pos] if sep_pos != -1 else "="
        name = whole[:sep_pos] if sep_pos != -1 else whole
        return f"{name}{sep}{_TOKEN_PLACEHOLDER}"

    text = _API_KEY.sub(_api_key_sub, text)

    def _url_param_sub(match: "re.Match[str]") -> str:
        nonlocal hits
        if _TOKEN_PLACEHOLDER in match.group(0):
            return match.group(0)
        hits += 1
        return f"{match.group(1)}{_TOKEN_PLACEHOLDER}"

    text = _URL_SECRET_PARAM.sub(_url_param_sub, text)

    def _aws_key_sub(_match: "re.Match[str]") -> str:
        nonlocal hits
        hits += 1
        return _TOKEN_PLACEHOLDER

    text = _AWS_ACCESS_KEY.sub(_aws_key_sub, text)

    # G1.6.8 (part C): key-name-based redaction over a rendered
    # object/array/Map/Set preview — see `_redact_preview_key_values`'s own
    # comment for why this is string-level here rather than per-property.
    text, key_value_hits = _redact_preview_key_values(text)
    hits += key_value_hits

    return text, hits


def _redact_console_text(text: Any, device_id: str, tab_id: int, origin: str) -> str:
    """Runs both gateway-side passes on one console field: the same
    card/ssn/email/phone re-check `_redact_fetch_body` already applies to
    fetch/network bodies, plus the console-only secret pass above. Auditing
    is combined into one `redaction_gateway_catch` event per field so a
    caller of `audit.tail` sees exactly what this module already reports for
    fetch/network, not a third differently-shaped event."""
    if not isinstance(text, str) or not text:
        return "" if text is None else str(text)
    text, hits_a = _redact_fetch_body(text, device_id)
    text, hits_b = _redact_console_secrets(text)
    hits = hits_a + hits_b
    if hits:
        audit.record(
            "redaction_gateway_catch",
            device=device_id, tab_id=tab_id, origin=origin, hits=hits, capability="console",
        )
    return text


def handle_console(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = tools_mod._resolve_device(args)
    if err:
        return err
    tab, err = tools_mod._resolve_tab_target(device_id, args, kwargs, "console")
    if err:
        return err
    tab_id = tab["tabId"]
    origin = tab.get("origin") or tools_mod._origin_of(tab.get("url", ""))

    level_filter = args.get("level_filter")
    if level_filter is not None and level_filter not in ("verbose", "info", "warning", "error"):
        return tools_mod._err("level_filter must be one of verbose, info, warning, error", code=protocol.INVALID_PARAMS)
    text_filter = str(args.get("text_filter") or "")
    limit = int(args.get("limit") or DEFAULT_CONSOLE_LIMIT)
    limit = max(1, min(limit, MAX_CONSOLE_LIMIT))
    since = args.get("since")
    holder, err = tools_mod._resolve_session(device_id, kwargs)
    if err:
        return err

    summary = f"read console output for tab {tab_id} at {origin}"
    denial = tools_mod._authorize(
        device_id, origin, "console", summary=summary, session_key=holder,
        detail=json.dumps({"level_filter": level_filter, "text_filter": text_filter, "limit": limit}),
    )
    if denial is not None:
        reason, code = denial
        return tools_mod._err(reason, code=code, device_id=device_id, tab_id=tab_id, origin=origin)

    wire_params: Dict[str, Any] = {"tabId": tab_id, "limit": limit}
    if level_filter:
        wire_params["levelFilter"] = level_filter
    if text_filter:
        wire_params["textFilter"] = text_filter
    if isinstance(since, int):
        wire_params["since"] = since

    relay = relay_mod.get_relay()
    try:
        result = relay.call(device_id, "console.entries", wire_params)
    except relay_mod.BridgeError as exc:
        return tools_mod._bridge_err(exc, "console.entries")

    shaped: List[Dict[str, Any]] = []
    for entry in result.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        # Whitelisted output shape (G2.4.5's own brief: "never forward
        # anything beyond what's named here"), same discipline as
        # handle_network's `shaped_entry` above.
        text = _redact_console_text(entry.get("text"), device_id, tab_id, origin)
        shaped_entry: Dict[str, Any] = {
            "level": entry.get("level"),
            "source": entry.get("source"),
            "text": text[:CONSOLE_TEXT_PREVIEW_CHARS],
            "text_truncated": len(text) > CONSOLE_TEXT_PREVIEW_CHARS,
            "ts": entry.get("ts"),
        }
        if entry.get("url") is not None:
            shaped_entry["url"] = _redact_console_text(entry.get("url"), device_id, tab_id, origin)
        if entry.get("lineNumber") is not None:
            shaped_entry["line_number"] = entry.get("lineNumber")
        if entry.get("columnNumber") is not None:
            shaped_entry["column_number"] = entry.get("columnNumber")
        stack = entry.get("stack")
        if isinstance(stack, list):
            shaped_entry["stack"] = [
                {
                    "function_name": frame.get("functionName"),
                    "url": _redact_console_text(frame.get("url"), device_id, tab_id, origin),
                    "line_number": frame.get("lineNumber"),
                    "column_number": frame.get("columnNumber"),
                }
                for frame in stack if isinstance(frame, dict)
            ]
        shaped.append(shaped_entry)

    audit.record(
        "tool_console", device=device_id, holder=holder, tab_id=tab_id, origin=origin, count=len(shaped),
    )
    return tools_mod._ok(device_id=device_id, tab_id=tab_id, origin=origin, entries=shaped, count=len(shaped))


# -- downloads (coveragegaps.md G3.5, gap 11) --------------------------------
#
# Extension-only capture per the plan's own framing: `background/downloads.ts`
# owns the onCreated/onChanged listeners, the bounded attribution buffer, the
# origin-scoping and the bounded `wait_for_download` poll. This tool is a thin
# gate-then-relay wrapper, the same shape as handle_network above — the real
# read is `_resolve_attached_tab` (this is per-tab, exactly like network log)
# plus `_authorize("downloads", ...)`, which already has everything it needs:
# G0.5 lists `downloads` in DANGEROUS_CAPABILITIES, and G0.6.6's
# `_CAPABILITY_POWER_KEYS["downloads"] = ("allowDownloadsRead",)` already
# exists (both predate this task).
#
# ATTRIBUTION IS HEURISTIC, NOT CERTAIN (say so here, not just in the wire
# schema): chrome.downloads.DownloadItem has no tabId/frameId/initiator field
# at all, so the extension attributes a download to a tab either by current
# origin match or by a short activity-correlation window — neither is proof,
# both are documented as best-effort in DOWNLOADS_SCHEMA's own description so
# the model never treats a returned download as certainly caused by its own
# last action.

DEFAULT_DOWNLOADS_LIMIT = 50
MAX_DOWNLOADS_LIMIT = 200
# Mirrors extension/src/background/downloads.ts's own MAX_WAIT_MS — kept as
# a separate literal (not imported; Python and TypeScript don't share
# constants) rather than importing the wire ceiling from schema.json, since
# the extension enforces its own ceiling regardless of what this tool asks
# for. Duplicated intentionally; if the two drift, the extension's is the one
# that actually binds.
MAX_DOWNLOADS_WAIT_MS = 120000

DOWNLOADS_SCHEMA = {
    "name": "browser_bridge_downloads",
    "description": (
        "Read the download history for the attached tab's origin — 'did the export actually finish, "
        "and what's its absolute local path?'. Returns id/filename (absolute local path)/url (query "
        "string redacted)/mime/bytesReceived/totalBytes/state/startTime/endTime/danger, scoped to "
        "downloads whose own url matches the tab's current origin OR that started within a short "
        "window of recent activity on that tab. ATTRIBUTION IS A HEURISTIC, NOT A GUARANTEE: Chrome's "
        "DownloadItem carries no tabId/frameId/initiator at all, so a download that is same-origin but "
        "unrelated to anything the agent did can still appear, and a legitimate cross-origin download "
        "(e.g. served from a different CDN than the page that triggered it) is only caught when it "
        "started soon enough after the agent's own action. Set wait_mode=true to block (bounded, up to "
        f"{MAX_DOWNLOADS_WAIT_MS // 1000}s) until a matching download reaches state=complete, instead of "
        "polling this tool yourself — this is the 'click Export, then confirm' flow in one call. Never "
        "opens or executes a downloaded file, and never overrides a Chrome danger prompt — this tool is "
        "read-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "tab_id": {
                "type": "integer",
                "description": "Tab whose origin/activity scopes the results. Omit when exactly one tab is attached.",
            },
            "tab": {"type": "string", "description": tools_mod.TAB_ALIAS_DESC},
            "filter": {"type": "string", "description": "Substring match against filename or url."},
            "limit": {
                "type": "integer",
                "description": f"Max entries to return, most-recent-first. Default {DEFAULT_DOWNLOADS_LIMIT}, capped at {MAX_DOWNLOADS_LIMIT}.",
            },
            "wait_mode": {
                "type": "boolean",
                "description": (
                    "If true, block until a matching download reaches state=complete or wait_ms elapses, "
                    "instead of returning immediately."
                ),
            },
            "wait_ms": {
                "type": "integer",
                "description": f"Only used when wait_mode is true. Default 30000, capped at {MAX_DOWNLOADS_WAIT_MS}.",
            },
        },
        "required": [],
    },
}


def handle_downloads(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = tools_mod._resolve_device(args)
    if err:
        return err
    tab, err = tools_mod._resolve_tab_target(device_id, args, kwargs, "downloads")
    if err:
        return err
    tab_id = tab["tabId"]
    origin = tab.get("origin") or tools_mod._origin_of(tab.get("url", ""))
    holder, err = tools_mod._resolve_session(device_id, kwargs)
    if err:
        return err

    limit = int(args.get("limit") or DEFAULT_DOWNLOADS_LIMIT)
    limit = max(1, min(limit, MAX_DOWNLOADS_LIMIT))
    filter_str = str(args.get("filter") or "")
    wait_mode = bool(args.get("wait_mode") or False)
    wait_ms = int(args.get("wait_ms") or 30000) if wait_mode else 0
    wait_ms = max(0, min(wait_ms, MAX_DOWNLOADS_WAIT_MS))

    summary = f"read downloads for tab {tab_id} at {origin}" + (" (waiting for completion)" if wait_mode else "")
    denial = tools_mod._authorize(
        device_id, origin, "downloads", summary=summary, session_key=holder,
        detail=json.dumps({"filter": filter_str, "limit": limit, "wait_mode": wait_mode}),
    )
    if denial is not None:
        reason, code = denial
        return tools_mod._err(reason, code=code, device_id=device_id, tab_id=tab_id, origin=origin)

    wire_params: Dict[str, Any] = {"tabId": tab_id, "limit": limit}
    if filter_str:
        wire_params["filter"] = filter_str
    if wait_mode and wait_ms:
        wire_params["waitMs"] = wait_ms

    relay = relay_mod.get_relay()
    try:
        result = relay.call(device_id, "downloads.search", wire_params)
    except relay_mod.BridgeError as exc:
        return tools_mod._bridge_err(exc, "downloads.search")

    entries: List[Dict[str, Any]] = []
    for entry in result.get("downloads") or []:
        if not isinstance(entry, dict):
            continue
        # Whitelisted output shape, same discipline as handle_network above —
        # never forward anything beyond what's named here even if the
        # extension's entry carries more.
        entries.append({
            "id": entry.get("id"),
            "filename": entry.get("filename"),
            "url": entry.get("url"),
            "mime": entry.get("mime"),
            "bytesReceived": entry.get("bytesReceived"),
            "totalBytes": entry.get("totalBytes"),
            "state": entry.get("state"),
            "startTime": entry.get("startTime"),
            "endTime": entry.get("endTime"),
            "danger": entry.get("danger"),
        })

    timed_out = bool(result.get("timedOut") or False)
    audit.record(
        "tool_downloads", device=device_id, holder=holder, tab_id=tab_id, origin=origin,
        count=len(entries), wait_mode=wait_mode, timed_out=timed_out,
    )
    return tools_mod._ok(
        device_id=device_id, tab_id=tab_id, origin=origin, downloads=entries, count=len(entries),
        timed_out=timed_out,
        attribution_note=(
            "attribution is heuristic: origin match or a short activity-correlation window, never "
            "certain (DownloadItem carries no tabId/frameId/initiator)"
        ),
    )


def register_session_tools(ctx) -> List[str]:
    """Entry point ``tools.register_tools`` imports under
    ``try/except ImportError``, exactly mirroring vision.py's
    ``register_vision_tools`` seam — see this module's docstring for why
    that's a style choice here rather than a hard requirement."""
    registered: List[str] = []
    for schema, handler, emoji in (
        (FETCH_SCHEMA, handle_fetch, "\U0001F6F0"),
        (COOKIES_SCHEMA, handle_cookies, "\U0001F510"),
        (COOKIES_SET_SCHEMA, handle_cookies_set, "\U0001F36A"),
        (NETWORK_SCHEMA, handle_network, "\U0001F4E1"),
        (CONSOLE_SCHEMA, handle_console, "\U0001F4DC"),
        (DOWNLOADS_SCHEMA, handle_downloads, "\U0001F4BE"),
    ):
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            check_fn=tools_mod.bridge_available,
            emoji=emoji,
        )
        registered.append(schema["name"])
    return registered
