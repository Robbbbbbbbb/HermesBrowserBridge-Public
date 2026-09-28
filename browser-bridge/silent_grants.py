"""silentfetch.md SF4: gateway-side grants, SSRF guard, rate guard, per-origin
serialization and audit for ``browser_bridge_silent_fetch``. SF5 builds the
tool itself and registers it; this module is its sole gate (SF2's design
rule 4 — "the gateway gates. Grants, SSRF, rate limit and audit are enforced
in the plugin. The extension's pool is plumbing and trusts nothing but its
own kill switch").

Resolution order (SF4.2, the user's 2026-09-27 decisions, plan §1):

  1. The origin's ordinary EFFECTIVE grant mode (``state.get_mode`` — an
     explicit grants-table row for this exact origin, else this device's own
     reported default, else config.py's fleet-wide ``default_mode``). 'off'
     refuses outright (``SILENT_ORIGIN_NOT_GRANTED``) — nothing below this
     line is even consulted, because an origin the ordinary grants gate has
     closed is not reachable by ANY tool, silent or otherwise.
  2. Otherwise (mode is 'request' or 'full'), the origin's own explicit
     per-origin SILENT mode (``origin_silent_mode`` — SF4.1, the popup's
     "Background requests" control): 'off' refuses the same way; 'always'
     is zero-touch; 'ask' asks once per Hermes agent session (capability
     ``"silent_fetch"``, the existing session-grant mechanism —
     ``approvals.require``) and is silent for the rest of that session.
  3. No explicit per-origin silent mode on record — the plugin-wide
     default: 'full' + ``silent_fetch.full_implies_silent`` (config.yaml,
     default true) -> zero-touch; 'full' + not full_implies_silent -> ask
     once per session; 'request' -> ask once per session.

An explicit per-origin override at step 2 ALWAYS wins over step 3's default,
both ways — an 'off'/'always' row is never re-derived from the origin's
ordinary mode or the ``full_implies_silent`` setting.

The write-side vs read-side lesson (plan's own words): whatever refuses to
CREATE a standing grant must also refuse to HONOUR a pre-existing row that
shouldn't exist. ``state.py``'s own read-time defenses
(``get_origin_silent_mode`` re-checking its own CHECK constraint on every
read, ``get_mode`` never trusting a stray value outside off/request/full)
are this module's foundation for that specific lesson, not a belt-and-braces
layer added on top here — this module's own instance of the same lesson is
the bounded, LRU-evicted in-memory maps below: neither the per-origin
serialization lock nor the per-origin rate bucket can grow without limit
just because an agent fetched a lot of distinct origins over a long-lived
gateway process.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional, Tuple
from urllib.parse import urlsplit

from . import audit, config, protocol, state
from . import origins as origins_mod
from . import tools as tools_mod

logger = logging.getLogger(__name__)

CAPABILITY = "silent_fetch"

# -- config -------------------------------------------------------------------


def _positive_number(value: Any, default: float, integer: bool) -> float:
    """Reject anything that isn't a plain (non-bool) number > 0, falling back
    to ``default`` rather than clamping to some arbitrary floor — a
    misconfigured ``config.yaml`` (a negative rate, a string, a stray
    ``true``) must degrade to shipped behaviour, not to a nonsensical or
    silently-disabled guard."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value <= 0:
        return default
    return int(value) if integer else float(value)


def clamp_bootstrap_timeout_ms(value: Any, default: int) -> int:
    """Silent Fetch rev 3 §D: shared clamp for BOTH the gateway-wide default
    (``silent_fetch.bootstrap_timeout_ms`` -> ``silent_fetch_config()`` below)
    and a per-call ``silent.fetch`` ``bootstrap_timeout_ms`` param
    (``silent_fetch.py``'s ``handle_silent_fetch``) -- one clamp, one place,
    so the two paths can never drift apart on what "in range" means. Anything
    that isn't a plain (non-bool) number falls back to ``default`` rather
    than raising; a value that IS a number is clamped into
    ``[SILENT_FETCH_MIN_BOOTSTRAP_TIMEOUT_MS, SILENT_FETCH_MAX_BOOTSTRAP_TIMEOUT_MS]``,
    never rejected outright -- a caller asking for 2000ms gets 5000ms, not a
    refusal, the same "degrade to sane behaviour" discipline
    ``_positive_number`` above already follows for the other silent_fetch.*
    knobs.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return int(default)
    return int(
        min(
            config.SILENT_FETCH_MAX_BOOTSTRAP_TIMEOUT_MS,
            max(config.SILENT_FETCH_MIN_BOOTSTRAP_TIMEOUT_MS, value),
        )
    )


def silent_fetch_config() -> Dict[str, Any]:
    """The effective ``silent_fetch.*`` settings: ``config.load()``'s own
    per-key dict merge already fills in any key a partial user block
    omitted, so this only has to defend against an individual value that is
    present but nonsensical (wrong type, zero, negative).

    Silent Fetch rev 2 §2: this is the single point every other module reads
    ``max_bytes_cap``/``max_buffered_bytes`` through — relay.py's chunk
    ceilings, silent_fetch.py's tool-side clamp, and the ``max_body_bytes``
    figure sent to the extension on hello/heartbeat all call this rather than
    reading ``config.py`` or ``config.yaml`` directly, so there is exactly
    one place a raised cap can fail to reach.
    """
    defaults = config.DEFAULTS["silent_fetch"]
    cfg = config.load().get("silent_fetch")
    if not isinstance(cfg, dict):
        cfg = {}
    max_bytes_cap = min(
        int(_positive_number(cfg.get("max_bytes_cap"), defaults["max_bytes_cap"], integer=True)),
        config.SILENT_FETCH_SANITY_MAX_BYTES,
    )
    # Default, when unset: room for one max-size body in flight (the
    # per-request ceiling below) — see config.py's own DEFAULTS comment for
    # why that's sized the way it is.
    default_buffered = max_bytes_cap + protocol.MAX_FRAME_BYTES
    max_buffered_bytes = min(
        int(_positive_number(cfg.get("max_buffered_bytes"), default_buffered, integer=True)),
        config.SILENT_FETCH_SANITY_MAX_BYTES * 4,
    )
    return {
        "enabled": bool(cfg.get("enabled", defaults["enabled"])),
        "full_implies_silent": bool(cfg.get("full_implies_silent", defaults["full_implies_silent"])),
        "max_workers": _positive_number(cfg.get("max_workers"), defaults["max_workers"], integer=True),
        "worker_ttl_s": _positive_number(cfg.get("worker_ttl_s"), defaults["worker_ttl_s"], integer=True),
        "bootstrap_timeout_ms": clamp_bootstrap_timeout_ms(
            cfg.get("bootstrap_timeout_ms"), defaults["bootstrap_timeout_ms"]
        ),
        "rate_per_s": _positive_number(cfg.get("rate_per_s"), defaults["rate_per_s"], integer=False),
        "burst": _positive_number(cfg.get("burst"), defaults["burst"], integer=True),
        "max_bytes_cap": max_bytes_cap,
        "max_buffered_bytes": max_buffered_bytes,
        "cache_retention_hours": _positive_number(
            cfg.get("cache_retention_hours"), defaults["cache_retention_hours"], integer=True
        ),
        "cache_max_bytes": _positive_number(cfg.get("cache_max_bytes"), defaults["cache_max_bytes"], integer=True),
    }


# -- audit (SF4.5) -------------------------------------------------------------
#
# Never passed a body or a header VALUE — only counts, keys and the request
# line itself (method/url/status/bytes), same discipline session_powers.py's
# own fetch audit lines already follow.


def record_silent_fetch(
    device_id: str,
    holder: str,
    origin: str,
    method: str,
    url: str,
    status: int,
    total_bytes: int,
    from_cache: bool,
    redactions: int = 0,
) -> None:
    audit.record(
        "silent_fetch",
        device=device_id,
        holder=holder,
        origin=origin,
        method=method,
        url=url,
        status=status,
        bytes=total_bytes,
        from_cache=from_cache,
        redactions=redactions,
    )


def record_silent_fetch_refused(
    device_id: str,
    origin: str,
    method: str,
    url: str,
    reason_class: str,
) -> None:
    audit.record(
        "silent_fetch_refused",
        device=device_id,
        origin=origin,
        method=method,
        url=url,
        reason_class=reason_class,
    )


_VALID_SILENT_FETCH_PHASES = {
    "acquire", "preflight", "inject", "page_fetch_headers", "draining",
    "slicing", "pushing_chunk", "done",
}


def record_silent_fetch_failed(
    device_id: str,
    origin: str,
    phase: Any,
    elapsed_ms: Any,
    bytes_so_far: Any,
    fetch_native: Any,
) -> None:
    """Silent Fetch rev 4 §B(b): a new gateway-side audit event, distinct from
    ``silent_fetch_refused`` (which only ever carries a coarse
    ``reason_class``) -- this carries the extension's own diagnostic
    snapshot of WHERE a call died, echoed back from ``silent.fetch``'s
    TIMEOUT error ``data`` (silent-fetch.ts's ``buildTimeoutResult``).
    Called only for a TIMEOUT-class failure that actually carried diagnostic
    data -- never fabricates a phase/elapsed/bytes value the extension never
    reported. Every field is validated against its own closed shape (never
    trusted verbatim off the wire), same discipline
    ``relay.py``'s ``_on_silent_worker`` already applies to the sibling
    ``silent.worker`` event.
    """
    fields: Dict[str, Any] = {"device": device_id, "origin": origin}
    fields["phase"] = phase if isinstance(phase, str) and phase in _VALID_SILENT_FETCH_PHASES else "unknown"
    if isinstance(elapsed_ms, (int, float)) and not isinstance(elapsed_ms, bool) and elapsed_ms >= 0:
        fields["elapsed_ms"] = elapsed_ms
    if isinstance(bytes_so_far, (int, float)) and not isinstance(bytes_so_far, bool) and bytes_so_far >= 0:
        fields["bytes_so_far"] = int(bytes_so_far)
    if isinstance(fetch_native, bool):
        fields["fetch_native"] = fetch_native
    audit.record("silent_fetch_failed", **fields)


def record_silent_worker(
    device_id: str,
    origin: str,
    action: str,
    reason: str = "",
    served: Optional[int] = None,
    age_ms: Optional[float] = None,
    failure: Optional[str] = None,
    chrome_error: Optional[str] = None,
    last_url: Optional[str] = None,
    redirects: Optional[int] = None,
    redirect_kinds: Optional[Dict[str, int]] = None,
    ready_state_reached: Optional[str] = None,
    elapsed_ms: Optional[float] = None,
) -> None:
    """``action`` is one of the ``silent.worker`` wire event's own enum
    (protocol/schema.json) — "launch"/"launch_failed"/"recycle"/"evict"/
    "kill"/"closed"/"adopted" (SF1's worker pool lifecycle) — exposed here so
    relay.py's ``_on_silent_worker`` and any other caller both audit under
    the same event name without either owning the other. ``served``/
    ``age_ms`` are the worker's own counters at the moment of the lifecycle
    point (omitted entirely, not written as null, when the caller has none —
    e.g. a launch failure has no worker instance to report them from).

    Silent Fetch rev 3 §A: ``failure``/``chrome_error``/``last_url``/
    ``redirects``/``redirect_kinds``/``ready_state_reached``/``elapsed_ms``
    are launch_failed's own diagnostics — relay.py's ``_on_silent_worker``
    has already validated each against its own closed shape (a bad value
    arrives here as ``None``, never a caller's job to re-check), so this
    function only has to omit whichever ones are absent, the same "field
    present only when the caller actually has it" convention ``served``/
    ``age_ms`` already followed.
    """
    fields: Dict[str, Any] = {"device": device_id, "origin": origin, "action": action, "reason": reason}
    if served is not None:
        fields["served"] = served
    if age_ms is not None:
        fields["age_ms"] = age_ms
    if failure is not None:
        fields["failure"] = failure
    if chrome_error is not None:
        fields["chrome_error"] = chrome_error
    if last_url is not None:
        fields["last_url"] = last_url
    if redirects is not None:
        fields["redirects"] = redirects
    if redirect_kinds is not None:
        fields["redirect_kinds"] = redirect_kinds
    if ready_state_reached is not None:
        fields["ready_state_reached"] = ready_state_reached
    if elapsed_ms is not None:
        fields["elapsed_ms"] = elapsed_ms
    audit.record("silent_worker", **fields)


# -- SF4.3: SSRF guard for a tab-less request ----------------------------------


def ssrf_guard(target_url: str, device_id: str) -> Optional[Tuple[str, int, str]]:
    """Returns None when ``target_url`` may proceed to ``authorize_silent_fetch``,
    or ``(reason, protocol_code, audit_reason_class)`` when it must be refused
    outright — same three-tuple shape ``session_powers._ssrf_guard`` returns.

    Reuses ``session_powers._classify_private_host``/``_explicit_grant_mode``
    verbatim rather than reimplementing the hostname-trick detection (IPv6
    literals, decimal/octal/hex-obfuscated IPv4, a trailing dot, RFC1918/ULA/
    loopback/link-local/reserved/multicast/unspecified) — see that function's
    own docstring for exactly which tricks it already defeats. Imported
    lazily (module-function scope, not this module's top level) so importing
    ``silent_grants`` never forces ``session_powers`` to load first; both
    modules already import ``tools`` the same lazy-or-not way depending on
    who is the "owning" workstream (session_powers.py's own docstring), and
    this avoids taking a position on load order between two sibling modules
    neither of which owns the other.

    Unlike ``browser_bridge_fetch``'s guard, there is no attached tab to
    treat as an automatic same-origin allow — every silent-fetch target is
    evaluated on its own, and (plan §3.4/§1) https is required outright
    except for a target that is BOTH a private/internal host AND explicitly
    granted (a stricter rule than the interactive lane's, appropriate for a
    lane that runs completely unattended).
    """
    from .session_powers import _classify_private_host, _explicit_grant_mode  # noqa: PLC0415

    try:
        parts = urlsplit(target_url)
    except ValueError:
        return "url could not be parsed", protocol.INVALID_PARAMS, "malformed_url"

    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return (
            f"browser_bridge_silent_fetch only supports http/https URLs; {scheme or '(no scheme)'!r} is "
            f"refused outright (no file:, chrome:, javascript:, data:, ftp:, blob:, etc.)",
            protocol.INVALID_PARAMS, "scheme",
        )

    hostname = parts.hostname or ""
    if not hostname:
        return "url has no host to fetch", protocol.INVALID_PARAMS, "no_host"

    # Canonical form, so a spelling variant (case, :443, trailing dot) can't miss its grant row.
    origin = origins_mod.canonicalize_origin(tools_mod._origin_of(target_url))
    private_reason = _classify_private_host(hostname)
    explicit_mode = _explicit_grant_mode(device_id, origin)
    granted_private = private_reason is not None and explicit_mode not in (None, "off")

    # Private/internal-network refusal takes priority over the scheme check
    # below: an ungranted private target must be refused as SSRF regardless
    # of scheme (an https request to an ungranted 169.254.169.254 is exactly
    # as much a confused-deputy attempt as an http one), so this is checked
    # first rather than only catching the http case.
    if private_reason and not granted_private:
        return (
            f"{target_url!r} targets {hostname!r} — a {private_reason} — with no explicit grant for "
            f"{origin!r}. Refused as a same-network SSRF attempt, the same reason "
            f"browser_bridge_fetch refuses it unattended: the browser can technically reach your LAN/"
            f"localhost, but that reachability is not the user authorizing this unattended lane to talk "
            f"to it. Ask the user to grant {origin!r} explicitly (request or full) in the extension popup.",
            protocol.GRANT_DENIED, "ssrf_private_network",
        )

    if scheme == "http" and not granted_private:
        # https-only, except http for an explicitly granted PRIVATE origin
        # (plan §3.4 point 1) — reachable now only for a PUBLIC http host,
        # since an ungranted private one already returned above and a
        # granted private one is `granted_private`. Nothing about running
        # unattended should make plaintext-to-the-public-internet more
        # palatable than the interactive lane treats it.
        return (
            f"{target_url!r} uses http, not https — browser_bridge_silent_fetch requires https, except "
            f"for an explicitly granted private/internal origin (this one is {origin!r}, grant mode "
            f"{explicit_mode!r})",
            protocol.INVALID_PARAMS, "scheme",
        )
    return None


# -- SF4.2: grant + silent-mode resolution -------------------------------------


def _refuse_not_granted(device_id: str, origin: str, method: str, url: str, why: str) -> Tuple[str, int, Dict[str, Any]]:
    reason = (
        f"{origin!r} is not granted for background (silent.fetch) requests: {why}. Ask the user to set "
        f"this origin's 'Background requests' popup control to 'Always allow' (or 'Ask first'), or — if "
        f"the origin is already granted 'full' for ordinary use — set silent_fetch.full_implies_silent "
        f"to true in config.yaml, or use browser_bridge_fetch against an attached tab instead."
    )
    record_silent_fetch_refused(device_id, origin, method, url, reason_class="origin_not_granted")
    return reason, protocol.SILENT_ORIGIN_NOT_GRANTED, {"reason_class": "origin_not_granted"}


def _ask_once(
    device_id: str, origin: str, method: str, url: str, session_key: str, summary: str, detail: str,
) -> Optional[Tuple[str, int, Dict[str, Any]]]:
    """One approval per Hermes agent session, via the SAME transport/session-
    grant mechanism every other capability uses (``approvals.require`` — see
    that module for the session-grant/always-grant side effects a live
    "session"/"always" choice has; this module neither reimplements nor
    special-cases them). Capability ``"silent_fetch"``, distinct from
    ``"fetch"`` (the attached-tab lane) so a standing grant for one never
    silently covers the other."""
    try:
        from . import approvals  # noqa: PLC0415 - optional sibling module, same seam tools.py's _authorize uses
    except ImportError:
        reason = (
            f"the approval queue is not loaded on this gateway, so background requests to {origin!r} are "
            f"refused rather than silently allowed"
        )
        record_silent_fetch_refused(device_id, origin, method, url, reason_class="approval_transport_missing")
        return reason, protocol.SILENT_ORIGIN_NOT_GRANTED, {"reason_class": "approval_transport_missing"}

    try:
        decision = approvals.require(device_id, origin, CAPABILITY, summary, session_key, detail=detail)
    except Exception as exc:  # the approval transport itself failed — never silently allow
        reason = f"approval request failed ({type(exc).__name__}: {exc}); background requests are refused, not silently allowed"
        record_silent_fetch_refused(device_id, origin, method, url, reason_class="approval_error")
        return reason, protocol.INTERNAL_ERROR, {"reason_class": "approval_error"}

    if decision.allowed:
        return None
    if decision.scope == "timeout":
        record_silent_fetch_refused(device_id, origin, method, url, reason_class="timeout")
        return (decision.reason or "the user did not respond to the background-request approval in time"), protocol.TIMEOUT, {"reason_class": "timeout"}
    record_silent_fetch_refused(device_id, origin, method, url, reason_class="approval_denied")
    return (decision.reason or f"the user denied background requests for {origin!r}"), protocol.APPROVAL_DENIED, {"reason_class": "approval_denied"}


def authorize_silent_fetch(
    device_id: str,
    url: str,
    method: str,
    session_key: str,
    summary: str,
    detail: str = "",
) -> Optional[Tuple[str, int, Dict[str, Any]]]:
    """The SF4.2 gate. Returns None when the call may proceed, or
    ``(reason, protocol_code, extra)`` to refuse — ``extra`` at minimum
    carries ``reason_class`` for the caller's own audit line (SF5's tool
    handler audits the eventual fetch itself under ``silent_fetch``; this
    function already audits every REFUSAL under ``silent_fetch_refused``, so
    the caller does not need to audit a refusal a second time).

    Frames are never involved: the target URL's own origin is the grant
    origin, exactly like ``browser_bridge_fetch``'s cross-origin case — there
    is no attached tab whose origin could stand in for it.
    """
    method = (method or "GET").strip().upper() or "GET"
    origin = origins_mod.canonicalize_origin(tools_mod._origin_of(url))

    grant_mode = state.get_mode(device_id, origin)
    if grant_mode == "off":
        return _refuse_not_granted(device_id, origin, method, url, "the origin's access mode is 'off'")

    silent_mode = state.get_origin_silent_mode(device_id, origin)
    if silent_mode == "off":
        return _refuse_not_granted(device_id, origin, method, url, "the origin's 'Background requests' setting is 'off'")
    if silent_mode == "always":
        return None
    if silent_mode == "ask":
        return _ask_once(device_id, origin, method, url, session_key, summary, detail)

    # No explicit per-origin override on record (silent_mode is None) — the
    # plugin-wide default. grant_mode here is 'request' or 'full' (the only
    # two values left once 'off' was handled above).
    cfg = silent_fetch_config()
    if grant_mode == "full" and cfg["full_implies_silent"]:
        return None
    return _ask_once(device_id, origin, method, url, session_key, summary, detail)


# -- SF4.4: rate guard + per-origin serialization ------------------------------

_MAX_TRACKED_ORIGINS = 512  # shared bound for both maps below


class _BoundedKeyedStore:
    """A dict keyed by an arbitrary hashable, LRU-bounded at ``max_entries``.

    Both the per-origin serialization lock and the per-origin rate-limit
    bucket need exactly this shape: create-on-first-use, reused after that,
    and never allowed to grow without bound just because an agent fetched a
    lot of distinct origins over a long-lived gateway process (plan's own
    "bounded memory" requirement for SF4.4's per-origin queue).

    Eviction never removes an entry whose stored value reports itself
    ``locked()`` (a ``threading.Lock`` currently held mid-request) — a slot
    for an in-flight origin must not vanish out from under the caller
    holding it. When every tracked entry happens to be locked at once the
    store is left transiently over its cap rather than evicting a live one;
    it shrinks back under the cap the next time anything is evicted, which
    only ever needs one more entry to finish being unlocked, not all of
    them.
    """

    __slots__ = ("_lock", "_entries", "_max_entries")

    def __init__(self, max_entries: int = _MAX_TRACKED_ORIGINS) -> None:
        self._lock = threading.Lock()
        self._entries: "OrderedDict[Tuple[str, str], Any]" = OrderedDict()
        self._max_entries = max_entries

    def get_or_create(self, key: Tuple[str, str], factory: Callable[[], Any]) -> Any:
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                value = factory()
                self._entries[key] = value
            else:
                self._entries.move_to_end(key)
            self._evict_locked()
            return value

    def _evict_locked(self) -> None:
        if len(self._entries) <= self._max_entries:
            return
        for existing_key in list(self._entries.keys()):
            if len(self._entries) <= self._max_entries:
                return
            value = self._entries[existing_key]
            is_locked = getattr(value, "locked", None)
            if callable(is_locked) and is_locked():
                continue
            del self._entries[existing_key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_origin_locks = _BoundedKeyedStore()
_rate_buckets = _BoundedKeyedStore()


class _TokenBucket:
    """A standard token bucket: refills continuously at ``rate`` tokens/sec
    up to ``capacity``, one token per request. Its own lock makes
    ``try_take`` atomic across concurrent callers racing for the SAME
    origin's bucket before either has taken the per-origin serialization
    lock (``origin_slot`` below) — the rate guard is checked independently
    of, and before, that serialization."""

    __slots__ = ("capacity", "rate", "tokens", "last", "_lock")

    def __init__(self, capacity: float, rate: float, now: float) -> None:
        self.capacity = max(1.0, float(capacity))
        self.rate = max(0.0, float(rate))
        self.tokens = self.capacity
        self.last = now
        self._lock = threading.Lock()

    def try_take(self, now: float) -> Tuple[bool, float]:
        with self._lock:
            elapsed = max(0.0, now - self.last)
            self.last = now
            if self.rate > 0:
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True, 0.0
            deficit = 1.0 - self.tokens
            retry_after = (deficit / self.rate) if self.rate > 0 else float("inf")
            return False, retry_after


def _monotonic() -> float:
    return time.monotonic()


def silent_rate_guard(
    device_id: str, origin: str, now: Optional[float] = None,
) -> Optional[Tuple[str, int, float]]:
    """SF4.4 token bucket, per (device, origin). Returns None to proceed, or
    ``(reason, protocol_code, retry_after_s)`` when the bucket is empty.
    ``now`` is an injectable clock hook for tests (a deterministic fake
    monotonic clock) — real callers omit it and get ``time.monotonic()``.

    The bucket for a given (device, origin) is sized from
    ``silent_fetch.rate_per_s``/``burst`` at the moment it is FIRST created
    for that pair; a config.yaml edit reshapes buckets created after the
    edit, not ones already in flight — the alternative (re-reading config on
    every single call) would mean a live burst budget could shrink mid-
    session out from under a caller who already spent some of it, which is a
    worse surprise than a bounded staleness window.
    """
    cfg = silent_fetch_config()
    when = _monotonic() if now is None else now
    key = (device_id, origin)
    bucket: _TokenBucket = _rate_buckets.get_or_create(
        key, lambda: _TokenBucket(capacity=cfg["burst"], rate=cfg["rate_per_s"], now=when)
    )
    allowed, retry_after = bucket.try_take(when)
    if allowed:
        return None
    reason = (
        f"{origin!r} is rate-limited for background requests ({cfg['rate_per_s']}/s, burst {cfg['burst']}) "
        f"— retry in {retry_after:.2f}s"
    )
    record_silent_fetch_refused(device_id, origin, "", "", reason_class="rate_limited")
    return reason, protocol.SILENT_RATE_LIMITED, retry_after


@contextmanager
def origin_slot(device_id: str, origin: str, timeout_s: float = 30.0) -> Iterator[None]:
    """SF4.4 per-(device, origin) serialization: a queue/lock the tool
    handler holds for the DURATION of the relay call (Chrome would serialize
    same-origin worker use anyway; this makes the gateway's own dispatch
    match it, and gives a bounded, auditable wait instead of two concurrent
    calls silently racing the same worker tab). Different origins never
    contend — each gets its own lock from ``_origin_locks`` — so parallelism
    across origins is unaffected.

    Raises ``TimeoutError`` if the slot is still held by another call after
    ``timeout_s`` — the caller (SF5's tool handler) turns that into an
    honest refusal rather than blocking the gateway worker thread forever
    behind a stuck or very slow sibling request to the same origin.
    """
    key = (device_id, origin)
    lock: threading.Lock = _origin_locks.get_or_create(key, threading.Lock)
    acquired = lock.acquire(timeout=timeout_s)
    if not acquired:
        raise TimeoutError(
            f"timed out after {timeout_s}s waiting for the silent-fetch queue slot for {origin!r} on "
            f"device {device_id!r} (another silent fetch to this same origin is still in flight)"
        )
    try:
        yield
    finally:
        lock.release()
