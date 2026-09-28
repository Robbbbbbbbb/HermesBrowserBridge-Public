"""WebSocket relay: the gateway half of the bridge.

Runs inside the Hermes gateway process. There is no gateway-startup hook in
v0.19.1 (see Documentation/plugin-api-findings.md), so the relay owns its own
daemon thread with a private asyncio loop: it never depends on a host loop and
a stall here can never wedge the gateway.

Connection lifecycle:
  1. Extension dials out and sends ``device.hello`` with a pairing code (first
     run) or a stored device token (reconnect). Nothing else is accepted first.
  2. On success the connection joins the registry keyed by device id; a second
     connection for the same device replaces the first.
  3. Heartbeats keep ``devices.last_seen`` fresh, which is what the tool
     ``check_fn`` reads to decide whether the toolset is live.

Tool handlers run on gateway threads, so outbound calls go through
``Relay.call()``, which marshals onto the relay loop via
``run_coroutine_threadsafe``.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from . import approvals, audit, config, protocol, state, timing as timing_mod

logger = logging.getLogger(__name__)

MAX_HELLO_FAILURES = 5
HELLO_TIMEOUT_SECONDS = 15

# devices.md DV1: every liveness-related clock read in this module goes
# through this one function rather than a bare time.time() call, so
# tests/test_dv1_liveness.py can drive the alive/stale/offline boundaries and
# the sweep with an injected clock instead of sleeping out real
# device_alive_after_seconds/device_offline_after_seconds waits. Production
# code never overrides it; it stays the real wall clock.
def _now() -> float:
    return time.time()


# DV1.3: how often the stale sweep runs. Independent of both
# device_alive_after_seconds and device_offline_after_seconds -- this is a
# polling cadence, not a liveness threshold, so it doesn't need to track
# either of them.
STALE_SWEEP_INTERVAL_SECONDS = 10.0

# DV1.4: |skew| past this many ms is audited once per excursion (see
# Connection.clock_skew_alert_audited). 120s is generous slack over ordinary
# NTP drift -- this exists to catch a browser host whose clock is genuinely
# wrong (wrong timezone applied twice, a VM that never synced), not to flag
# routine jitter.
CLOCK_SKEW_ALERT_THRESHOLD_MS = 120_000

# SF-audit: the worker-pool lifecycle actions silent-pool.ts's `silent.worker`
# notification (protocol/schema.json) may report. Anything else is refused
# and audited as `silent_worker_rejected` rather than trusted through.
SILENT_WORKER_ACTIONS = frozenset(
    {"launch", "launch_failed", "recycle", "evict", "kill", "closed", "adopted"}
)
# `reason` is a short machine token (ttl/lru/origin_drift/kill_switch/user_kill/
# user_closed/sw_restart/nav_error, or blank) -- bounded so a misbehaving or
# compromised extension build can't use this free-text field to smuggle
# anything sized like a URL or page content into the audit log.
SILENT_WORKER_REASON_MAX_LEN = 64

# Silent Fetch rev 3 §A: launch_failed's own diagnostic fields (silent-pool.
# ts's SilentWorkerDiagnostics) -- validated the same "drop, don't trust"
# way `reason` above already is. `failure` is a closed enum (silent-pool.ts's
# LaunchFailureKind); `chrome_error` must match Chrome's own net-error token
# shape or is dropped outright (never forwarded as free text); `last_url` is
# bounded, never a query string by construction on the extension side but
# still length-capped here in case a future/misbehaving build sends more.
SILENT_WORKER_FAILURE_KINDS = frozenset(
    {"timeout", "nav_error", "redirect_foreign", "tab_closed", "create_failed", "group_failed"}
)
SILENT_WORKER_READY_STATES = frozenset({"none", "committed", "interactive", "complete"})
SILENT_WORKER_CHROME_ERROR_RE = re.compile(r"^net::[A-Z_]+$")
SILENT_WORKER_LAST_URL_MAX_LEN = 128

# -- SF3.2: silent.fetch chunk reassembly ----------------------------------
#
# A silent.fetch body too big for one MAX_FRAME_BYTES frame arrives as a run
# of `silent.fetch.chunk` notifications (keyed by the *request's own envelope
# id*, not a separate id) followed by the silent.fetch result itself, which
# carries `chunks: N` instead of an inline `body`. Connection buffers those
# notifications per request id and Connection.resolve() reassembles the body
# in seq order before the pending Relay.call() future ever sees the result —
# SF5's tool layer always gets one whole body, chunked or not.
#
# Silent Fetch rev 2 §2: config.py's silent_fetch.max_bytes_cap is now the
# single source of truth for the captured-body ceiling (was a hard-coded
# 16 MiB constant here, independently of the tool's own cap and the
# extension's — three ceilings that could each say something different).
# These two functions derive the ceilings THIS module enforces from that one
# config value, read fresh on every call (never cached at import) so a
# config.yaml edit plus a gateway restart is all it takes — no code change.
# Lazy import: silent_grants -> tools -> relay would cycle at import time
# (see _on_silent_worker's own identical comment above).
def _effective_silent_fetch_cfg() -> Dict[str, Any]:
    from . import silent_grants
    return silent_grants.silent_fetch_config()


def silent_fetch_per_request_ceiling_bytes() -> int:
    """The captured-body ceiling (config's max_bytes_cap, already sanity-
    clamped by silent_grants.silent_fetch_config), plus room for one frame in
    flight when the cap lands mid-frame."""
    cfg = _effective_silent_fetch_cfg()
    return int(cfg["max_bytes_cap"]) + protocol.MAX_FRAME_BYTES


def silent_fetch_global_ceiling_bytes() -> int:
    """Global ceiling across every concurrent silent.fetch chunk stream on
    this gateway process — config's max_buffered_bytes (default: room for one
    max-size body in flight; see config.py's own DEFAULTS comment), also
    sanity-clamped upstream."""
    return int(_effective_silent_fetch_cfg()["max_buffered_bytes"])

# Process-wide running total of bytes currently buffered across every
# connection's chunk_buffers. The relay's asyncio loop is single-threaded and
# every mutation happens on it (notification handling and Connection.request's
# cleanup alike), so a plain module global needs no lock.
_silent_fetch_buffered_bytes = 0


class ChunkBuffer:
    """Buffers one silent.fetch response's chunk notifications until its
    matching result frame arrives, or the pending call fails/times out first.
    """

    __slots__ = ("parts", "expected_seq", "total_bytes", "finished")

    def __init__(self) -> None:
        self.parts: Dict[int, bytes] = {}
        self.expected_seq = 0
        self.total_bytes = 0
        # True once a chunk with `last: true` has been accepted -- any further
        # chunk for this id is "a chunk for a finished id" (SF3.2) and fails
        # the pending call.
        self.finished = False

    def free(self) -> None:
        """Release this buffer's bytes from the global ceiling. Idempotent:
        safe to call more than once (e.g. once from the notification handler's
        failure path and again from Connection.request's cleanup) because it
        zeroes total_bytes after crediting it back."""
        global _silent_fetch_buffered_bytes
        _silent_fetch_buffered_bytes -= self.total_bytes
        self.parts.clear()
        self.total_bytes = 0

# Sliding-window brute-force gate, keyed by remote IP rather than by connection
# — reconnecting must not reset an attacker's failure count the way the
# per-connection MAX_HELLO_FAILURES counter does.
HELLO_RATE_LIMIT_MAX_FAILURES = 10
HELLO_RATE_LIMIT_WINDOW_SECONDS = 10 * 60
# Pruning in _prune_hello_failures() is time-based only, so a distributed
# attack spread across many source IPs can grow _hello_failures_by_ip without
# bound within a single live window (< 30MB RSS budget on a 3GB box — see
# CLAUDE.md). This hard-caps distinct tracked IPs; past the cap the
# least-recently-active ones are evicted in a batch (see
# _enforce_hello_tracking_cap).
HELLO_RATE_LIMIT_MAX_TRACKED_IPS = 2048
# Evict down to (cap - this) rather than to (cap - 1), so a sustained attack
# pinning the dict at the cap triggers one eviction batch (and one audit
# line) per this-many new IPs instead of one per single new IP — the audit
# call itself must not become a log-amplification vector.
HELLO_RATE_LIMIT_EVICT_BATCH = 128


class BridgeError(Exception):
    """A JSON-RPC-shaped failure carrying a protocol error code."""

    def __init__(self, code: int, message: str = "", data: Optional[dict] = None):
        super().__init__(message or protocol.ERROR_MESSAGES.get(code, "error"))
        self.code = code
        self.message = message or protocol.ERROR_MESSAGES.get(code, "error")
        self.data = data or {}


class Connection:
    """One authenticated extension connection."""

    def __init__(self, ws, remote: str):
        self.ws = ws
        self.remote = remote
        self.device_id: str = ""
        self.device_name: str = ""
        self.session_id: str = ""
        self.out_seq = 0
        self.expected_in_seq = 1
        self.connected_at = time.time()
        self.last_heartbeat = _now()
        self.attached: list[dict] = []
        # devices.md DV1.2: alive/stale, updated ONLY through Relay.
        # _update_liveness (see that method's docstring for why every caller
        # — a status() read off a tool-handler thread, or the DV1.3 sweep on
        # the relay loop thread — goes through that one place rather than
        # comparing last_heartbeat directly). A fresh connection starts
        # "alive": it just heartbeat (device.hello counts, see _on_hello).
        self.liveness_state: str = "alive"
        # DV1.3: set once this connection has been handed to _close_stale, so
        # a sweep tick that lands before that close actually completes on the
        # wire doesn't audit device_swept a second time for the same silence.
        self.sweep_scheduled: bool = False
        # DV1.4: gateway_clock_ms - envelope `ts` from this connection's last
        # device.hello/device.heartbeat frame. None until the first one lands.
        self.clock_skew_ms: Optional[int] = None
        # DV1.4: whether the CURRENT skew excursion past +/-CLOCK_SKEW_ALERT_
        # THRESHOLD_MS has already been audited -- once-per-excursion, not
        # once-per-frame, mirroring liveness_state's own transition-only
        # auditing.
        self.clock_skew_alert_audited: bool = False
        self.pending: Dict[int, asyncio.Future] = {}
        self._next_id = 1
        # SF3.2: silent.fetch chunk reassembly buffers, keyed by the same
        # envelope id as `pending` above (one silent.fetch request <-> at
        # most one ChunkBuffer, for as long as that request is outstanding).
        self.chunk_buffers: Dict[int, "ChunkBuffer"] = {}
        # ASK 3 (pause sharing): mirrors the extension's own settings.paused,
        # kept up to date from whichever of device.hello/device.heartbeat/
        # state.report last carried it (protocol/schema.json declares
        # `paused` on all three). Purely informational here — the extension
        # is the one enforcing the refusal (offscreen.ts's inbound gate);
        # this is only read back out by browser_bridge_status so the agent
        # can see it and stop trying capability calls that will just come
        # back SHARING_PAUSED.
        self.paused: bool = False
        # Reported protocol_version from this device's device.hello, kept for
        # browser_bridge_status (G0.7.3) so a build that is connected but
        # behind the gateway's current protocolVersion is visible before a
        # method it lacks bites — the hello check below only proves it is
        # *inside* the supported range, not that it is current.
        self.protocol_version: str = ""

    @property
    def authenticated(self) -> bool:
        return bool(self.device_id and self.session_id)

    def _envelope(self) -> Dict[str, Any]:
        self.out_seq += 1
        env: Dict[str, Any] = {"jsonrpc": "2.0", "seq": self.out_seq, "ts": int(time.time() * 1000)}
        if self.session_id:
            env["session"] = self.session_id
        return env

    async def send_result(self, request_id: Any, result: Dict[str, Any]) -> None:
        await self.ws.send(json.dumps({**self._envelope(), "id": request_id, "result": result}))

    async def send_error(self, request_id: Any, code: int, message: str, data: Optional[dict] = None) -> None:
        error: Dict[str, Any] = {"code": code, "message": message}
        if data:
            error["data"] = data
        await self.ws.send(json.dumps({**self._envelope(), "id": request_id, "error": error}))

    async def notify(self, method: str, params: Optional[dict] = None) -> None:
        await self.ws.send(json.dumps({**self._envelope(), "method": method, "params": params or {}}))

    async def request(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> Dict[str, Any]:
        """Send a gateway→extension request and await its response."""
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self.ws.send(json.dumps({**self._envelope(), "id": request_id, "method": method, "params": params or {}}))
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            raise BridgeError(protocol.TIMEOUT, f"{method} timed out after {timeout}s")
        finally:
            self.pending.pop(request_id, None)
            # SF3.2: free any chunk buffer left over for this id on every exit
            # path -- normal completion, error, and (this branch) timeout
            # alike -- so a timed-out silent.fetch never leaks its buffer.
            buf = self.chunk_buffers.pop(request_id, None)
            if buf is not None:
                buf.free()

    def fail_all(self, reason: str) -> None:
        """Fail every pending request and free its chunk buffers. Called when
        the socket drops: a response can never arrive on a replacement
        connection, so waiting out each call's timeout only holds buffered
        bytes against the global ceiling."""
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(BridgeError(protocol.TOKEN_INVALID, reason))
        for buf in self.chunk_buffers.values():
            buf.free()
        self.chunk_buffers.clear()

    def resolve(self, frame: Dict[str, Any]) -> None:
        """Complete a pending gateway→extension request from its response frame."""
        request_id = frame.get("id")
        future = self.pending.get(request_id)
        if future is None or future.done():
            return
        if "error" in frame:
            err = frame["error"] or {}
            # Carry `data` through. ELEMENT_MISMATCH puts {expected, actual} in
            # there, and a tool that can read those as fields — rather than
            # parsing them back out of the prose message — can tell the model
            # precisely what changed under it.
            payload = err.get("data")
            future.set_exception(
                BridgeError(
                    err.get("code", protocol.INTERNAL_ERROR),
                    err.get("message", ""),
                    payload if isinstance(payload, dict) else None,
                )
            )
            return
        result = frame.get("result") or {}
        chunks = result.get("chunks")
        if isinstance(chunks, int) and chunks > 0:
            # SF3.2: this silent.fetch result declares its body arrived as
            # `chunks` prior silent.fetch.chunk notifications rather than
            # inline -- reassemble from this connection's buffer before the
            # caller (Relay.call, then the SF5 tool layer) ever sees it, so
            # from their side a chunked body looks identical to an inline one.
            buf = self.chunk_buffers.get(request_id)
            try:
                body_bytes = _reassemble_chunks(buf, chunks, request_id)
            except BridgeError as exc:
                future.set_exception(exc)
                return
            # Memory (Silent Fetch rev 2 §2): free this request's chunk
            # buffer THE MOMENT its parts are joined, rather than waiting for
            # Connection.request()'s own finally (which runs after the tool
            # layer has finished with the whole body too) -- buf.parts is the
            # same size as body_bytes and has no reason to stay alive next to
            # it. Popped (not just freed) so the finally block's own
            # chunk_buffers.pop is a no-op double-free-guard, not a second
            # real free.
            self.chunk_buffers.pop(request_id, None)
            if buf is not None:
                buf.free()
            result = dict(result)
            # Hand the tool layer raw bytes directly rather than a base64
            # string -- silent_fetch.py's own decode step would otherwise
            # undo exactly this encoding a moment later. This never crosses
            # the wire (this `result` dict is the relay's in-process return
            # value to Relay.call()'s synchronous caller, not a frame), so
            # there is no wire contract to keep: skipping the encode/decode
            # round trip avoids two full-body-sized copies for every chunked
            # response. `bodyEncoding` is kept as "base64" for any caller
            # still reading it as an informational hint (concatenated byte
            # slices are never assumed to land on a UTF-8 boundary, even for
            # an originally clean-text body -- same rule page.fetch's own
            # bodyEncoding follows), but body_bytes -- not body -- is now the
            # authoritative field for a chunked result.
            result["body_bytes"] = body_bytes
            result["bodyEncoding"] = "base64"
        future.set_result(result)


def _reassemble_chunks(buf: Optional["ChunkBuffer"], chunks_expected: int, request_id: Any) -> bytes:
    """Concatenate a ChunkBuffer's parts in seq order.

    Ingestion (`Relay._on_silent_fetch_chunk`) already enforces contiguous,
    gap-free, duplicate-free seq numbers starting at 0, so `buf.parts` having
    exactly `chunks_expected` entries is sufficient to know every seq
    0..chunks_expected-1 is present -- no separate ordering pass is needed.
    Anything short of that (no buffer at all, a short count, or a result that
    arrived before the extension ever sent a `last: true` chunk) is a
    reassembly failure, not a body to guess at.
    """
    have = 0 if buf is None else len(buf.parts)
    if buf is None or have != chunks_expected or not buf.finished:
        raise BridgeError(
            protocol.INTERNAL_ERROR,
            f"silent.fetch id={request_id}: result declared {chunks_expected} chunk(s) but "
            f"{have} were buffered (finished={False if buf is None else buf.finished})",
        )
    return b"".join(buf.parts[i] for i in range(chunks_expected))


def _parse_protocol_version(raw: Any) -> Optional[tuple]:
    """Parse a `MAJOR.MINOR` protocol_version string into a comparable tuple.

    Returns None for anything that isn't exactly two non-negative integers
    separated by a dot — a missing field, an empty string, "1", "1.0.0", or
    garbage all parse to None and are therefore treated as unsupported by
    `_protocol_version_supported` below. G0.7.1 requires an explicit answer
    for "a device that reports neither" (neither a version in range nor a
    recognisable one at all): fail closed, same refusal as an out-of-range
    version, never a soft pass.
    """
    if not isinstance(raw, str):
        return None
    parts = raw.split(".")
    if len(parts) != 2:
        return None
    if not all(p.isdigit() for p in parts):
        return None
    return (int(parts[0]), int(parts[1]))


def _protocol_version_supported(reported: Any) -> bool:
    """G0.7.1: range check, replacing the old exact-equality gate.

    A device is accepted when its MAJOR.MINOR falls within
    [minSupportedProtocolVersion, protocolVersion] of this schema, inclusive,
    compared as tuples (so a MAJOR bump is never silently treated as
    compatible just because the MINOR happens to sort higher). This lets an
    extension build that is older than the gateway — but still within the
    supported window — stay connected instead of being hard-refused on every
    point release, which is what turned one version bump into "every
    unreloaded extension is refused" before this task. A device outside the
    range, or one that sends no parseable version at all, is refused with
    PROTOCOL_MISMATCH exactly as the old exact-match gate refused it —
    nothing here widens what an incompatible device can do, it only narrows
    what counts as incompatible.
    """
    parsed = _parse_protocol_version(reported)
    if parsed is None:
        return False
    minimum = _parse_protocol_version(protocol.MIN_SUPPORTED_PROTOCOL_VERSION)
    current = _parse_protocol_version(protocol.PROTOCOL_VERSION)
    assert minimum is not None and current is not None  # both are this schema's own constants
    return minimum <= parsed <= current


def _apply_reported_redaction(device_id: str, params: Dict[str, Any]) -> None:
    """Persist a `redaction` field off any device.hello/device.heartbeat/
    state.report params (protocol/schema.json's `redactionPolicy`) and audit
    the change — called from all three handlers below, same shape as how
    they each already handle `paused`.

    A MISSING `redaction` field is a no-op (nothing was reported this frame,
    so whatever this device last reported stands); state.py's
    get_redaction_policy/set_redaction_policy own what a field that's present
    but partial means. Turning a protection off (or back on) is a
    security-relevant setting, so every actual change — not every frame, only
    ones that changed something — lands in the audit log with the full
    before/after, per plan §6.3/§6.2.
    """
    reported = params.get("redaction")
    if not isinstance(reported, dict):
        return
    before = state.get_redaction_policy(device_id)
    after = state.set_redaction_policy(device_id, reported)
    if after != before:
        audit.record("redaction_policy_changed", device=device_id, before=before, after=after)


def _apply_reported_powers(device_id: str, params: Dict[str, Any]) -> None:
    """Persist a `powers` field off any device.hello/device.heartbeat/
    state.report params (protocol/schema.json's `powerPolicy`) and audit the
    change — called from all three handlers below, same shape as
    `_apply_reported_redaction` immediately above.

    A MISSING `powers` field is a no-op (nothing was reported this frame, so
    whatever this device last reported stands — state.py's
    get_power_policy/set_power_policy own what a field that's present but
    partial means, and own the fail-closed direction that makes an absent
    per-capability key mean disabled rather than enabled). Turning a
    capability on (or back off) is a security-relevant setting exactly like a
    redaction toggle, so every actual change — not every frame, only ones
    that changed something — lands in the audit log with the full
    before/after.
    """
    reported = params.get("powers")
    if not isinstance(reported, dict):
        return
    before = state.get_power_policy(device_id)
    after = state.set_power_policy(device_id, reported)
    if after != before:
        audit.record("power_policy_changed", device=device_id, before=before, after=after)


def _apply_reported_default_mode(device_id: str, params: Dict[str, Any]) -> None:
    """Persist a `device_default_mode` field off any device.hello/
    device.heartbeat/state.report params (protocol/schema.json's
    `device_default_mode`) and audit the outcome — called from all three
    handlers below, same shape as `_apply_reported_redaction`/
    `_apply_reported_powers` above.

    A MISSING field is a no-op (nothing was reported this frame, so
    whatever this device last validly reported stands — or, if it has never
    reported one, `state.effective_default_mode` keeps falling back to
    config.py's `default_mode`).

    A PRESENT-but-invalid value (state.py's `set_device_default_mode`
    rejects anything outside off/request/full) is never silently coerced or
    dropped: it is ignored for enforcement purposes — this device's default
    stays exactly what it was before this frame — and the rejection itself
    is audited so a misbehaving build shows up in the trail. This is the
    ONLY branch here that audits on a no-change outcome, deliberately: a
    rejected value is itself the security-relevant fact, unlike an
    unchanged-but-valid report.

    A valid value that actually changed something is audited with the full
    before/after, exactly like a redaction/power policy change.
    """
    if "device_default_mode" not in params:
        return
    reported = params.get("device_default_mode")
    before = state.get_device_default_mode(device_id)
    after = state.set_device_default_mode(device_id, reported)
    if after is None:
        audit.record("device_default_mode_rejected", device=device_id, reported=reported)
        return
    if after != before:
        audit.record("device_default_mode_changed", device=device_id, before=before, after=after)


def _apply_reported_lease_seconds(device_id: str, params: Dict[str, Any]) -> None:
    """Persist a `device_lease_seconds` field off any device.hello/
    device.heartbeat/state.report params (protocol/schema.json's
    `device_lease_seconds`) and audit the outcome — called from all three
    handlers below, same shape as `_apply_reported_default_mode` above.

    A MISSING field is a no-op (nothing was reported this frame, so whatever
    this device last validly reported stands — or, if it has never reported
    one, `state.effective_lease_seconds` keeps falling back to config.py's
    `lease_seconds`).

    A PRESENT-but-invalid value (state.py's `set_device_lease_seconds`
    rejects anything that isn't a plain int either 0 or in [10, 1200]) is
    never silently coerced, clamped or dropped: it is ignored for enforcement
    purposes — this device's lease stays exactly what it was before this
    frame — and the rejection itself is audited so a misbehaving build shows
    up in the trail, exactly like `_apply_reported_default_mode` handles its
    own rejected values.

    A valid value that actually changed something is audited with the full
    before/after, exactly like a default-mode change.
    """
    if "device_lease_seconds" not in params:
        return
    reported = params.get("device_lease_seconds")
    before = state.get_device_lease_seconds(device_id)
    after = state.set_device_lease_seconds(device_id, reported)
    if after is None:
        audit.record("device_lease_seconds_rejected", device=device_id, reported=reported)
        return
    if after != before:
        audit.record("device_lease_seconds_changed", device=device_id, before=before, after=after)


def _apply_reported_commit_mode(device_id: str, params: Dict[str, Any]) -> None:
    """speedimprovements.md H1: persist a `device_commit_mode` field off any
    device.hello/device.heartbeat/state.report params (protocol/schema.json's
    `device_commit_mode`) and audit the outcome — called from all three
    handlers below, same shape as `_apply_reported_default_mode`/
    `_apply_reported_lease_seconds` above.

    A MISSING field is a no-op (nothing was reported this frame, so whatever
    this device last validly reported stands — or, if it has never reported
    one, `state.effective_commit_mode` keeps falling back to "auto").

    A PRESENT-but-invalid value (state.py's `set_device_commit_mode` rejects
    anything outside auto/pause) is never silently coerced or dropped: it is
    ignored for enforcement purposes — this device's commit mode stays
    exactly what it was before this frame — and the rejection itself is
    audited so a misbehaving build shows up in the trail, exactly like
    `_apply_reported_default_mode`/`_apply_reported_lease_seconds` handle
    their own rejected values.

    A valid value that actually changed something is audited with the full
    before/after, exactly like a default-mode/lease-seconds change.
    """
    if "device_commit_mode" not in params:
        return
    reported = params.get("device_commit_mode")
    before = state.get_device_commit_mode(device_id)
    after = state.set_device_commit_mode(device_id, reported)
    if after is None:
        audit.record("device_commit_mode_rejected", device=device_id, reported=reported)
        return
    if after != before:
        audit.record("device_commit_mode_changed", device=device_id, before=before, after=after)


def _silent_fetch_hello_fields() -> Dict[str, Any]:
    """The silent_fetch.* fields sent on both device.hello and every
    device.heartbeat: worker-pool caps (``max_workers``/``worker_ttl_s``) plus
    Silent Fetch rev 2 §2's new ``max_body_bytes`` — the effective, sanity-
    clamped ``silent_fetch.max_bytes_cap`` the extension must clamp its own
    captures to. All three come from ``silent_grants.silent_fetch_config()``,
    the single point config.yaml's ``silent_fetch.*`` block is read through
    (see that function's own docstring) — read fresh on every hello/heartbeat
    so a config edit plus a gateway restart is all it takes, exactly like
    ``default_mode`` just below.

    NOTE: this closes a pre-existing gap found while wiring ``max_body_bytes``
    in: protocol/schema.json has declared ``max_workers``/``worker_ttl_s`` on
    both these results since SF1/SF3, and the extension (offscreen/client.ts)
    has read them since then too, but nothing here ever actually populated
    them — every device.hello/device.heartbeat response landed with the
    pool's own DEFAULT_MAX_WORKERS/DEFAULT_WORKER_TTL_MS, never whatever
    config.yaml said. Fixed here alongside max_body_bytes rather than left
    broken next to a working sibling field.
    """
    cfg = _effective_silent_fetch_cfg()
    return {
        "max_workers": int(cfg["max_workers"]),
        "worker_ttl_s": int(cfg["worker_ttl_s"]),
        "max_body_bytes": int(cfg["max_bytes_cap"]),
        # Silent Fetch rev 3 §D: same shape as the three above -- the
        # effective, clamped silent_fetch.bootstrap_timeout_ms the extension
        # must apply as its own worker-launch timeout default (silent-pool.
        # ts's configurePool); a per-call silent.fetch bootstrap_timeout_ms
        # param overrides this for one launch only.
        "bootstrap_timeout_ms": int(cfg["bootstrap_timeout_ms"]),
    }


def _priority_hello_fields(device_id: str) -> Dict[str, Any]:
    """devices.md DV5.2/DV6: read-only device-priority info sent on both
    device.hello and every device.heartbeat result, sibling to
    `_silent_fetch_hello_fields` above -- same "read fresh every time, so an
    operator's CLI change is visible with no reconnect required" reasoning.

    Purely additive to the wire contract (three new optional result fields;
    no existing field's meaning changes), so this does not accompany a
    PROTOCOL_VERSION bump -- see protocol/schema.json's own version, which
    stays exactly what SF6/DV1 last left it at.

    `device_rank` is this device's 1-based position in
    `state.get_global_priority()` (the operator/agent-set global order, DV2),
    or ``None`` when this device has no explicit rank in that order --
    deliberately not `state.ordered_candidates()`'s fuller resolution
    (session override, then most-recent-heartbeat tail): that fuller order
    answers "which device does an implicit call pick", a question this
    frame's own recipient (the device asking about ITSELF, outside any
    particular agent session) has no session context to make meaningful, and
    a last-seen tiebreak position would be pure noise -- swapping every time
    another device's heartbeat lands, not "my rank" in any usual sense.
    `priority_pinned` mirrors `state.get_priority_pin()["pinned"]` exactly.
    `device_alive_after_s` is the exact threshold DV1's own liveness check
    uses (`config.effective_device_alive_after_seconds`), so the extension
    can render "this device goes stale after Ns of silence" without
    duplicating the gateway's own clamp logic.
    """
    order = state.get_global_priority()
    rank = order.index(device_id) + 1 if device_id in order else None
    pin = state.get_priority_pin()
    return {
        "device_rank": rank,
        "priority_pinned": bool(pin["pinned"]),
        "device_alive_after_s": config.effective_device_alive_after_seconds(config.load()),
    }


def _sanitize_last_url(value: Any) -> Optional[str]:
    """http(s) origin + path only: no userinfo, query or fragment, whatever
    the extension sent. Anything else is dropped."""
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"{parts.scheme}://{netloc}{parts.path}"[:SILENT_WORKER_LAST_URL_MAX_LEN]


def _canonical_origin(origin: str) -> str:
    # hermes_plugin/origins.py is dependency-free (no tools import), so this
    # no longer needs the lazy-import-to-dodge-a-cycle tools.py once required.
    from .origins import canonicalize_origin
    return canonicalize_origin(origin)


def _apply_reported_origin_silent_mode(device_id: str, params: Dict[str, Any]) -> None:
    """SF6 'Lost changes': persist a device's FULL `origin_silent_mode` map
    off a device.hello / device.heartbeat / state.report frame (protocol/
    schema.json's `origin_silent_mode`) with REPLACE semantics — called from
    all three handlers below, same shape as `_apply_reported_default_mode`/
    `_apply_reported_commit_mode` above, but per-origin rather than per-
    device, and a REPLACE rather than an upsert: see state.py's
    `replace_origin_silent_modes` for why a reported array now means "this
    is the whole table", not "here are some changes to merge in" — the old
    per-entry-upsert behaviour (SF4.1) left an origin's row exactly as it was
    forever if the popup's change never arrived or a later frame simply
    didn't mention it, which is precisely the drift this fix exists to close.

    A MISSING or non-list field is a no-op, same as every other field this
    module applies — nothing usable was reported this frame (an old
    extension build, or one with nothing to say), so whatever this device
    last validly reported stands untouched.

    Otherwise every origin is canonicalised (case, default port, trailing
    dot — `_canonical_origin`, same as `authorize_silent_fetch`/`ssrf_guard`)
    before the replace, diffed against what the table held immediately
    before this call, and audited precisely: an origin added, changed, or
    cleared (dropped from the map, or reported explicitly as `mode:
    "default"` — protocol/schema.json's `originSilentModeReport`) gets its
    own `origin_silent_mode_changed` line (`after=None` for a clear); an
    oversized report is refused wholesale and audited as
    `origin_silent_mode_overflow` (state.py's own cap, checked there); an
    individual malformed entry is audited as `origin_silent_mode_rejected`
    without aborting the rest of the map AND WITHOUT clearing that origin's
    existing row — state.py's `replace_origin_silent_modes` carries the
    prior value forward for it, so a single garbled entry can never fail an
    'off' override open to whatever the device-wide default resolves to.
    """
    reported = params.get("origin_silent_mode")
    if not isinstance(reported, list):
        return

    canonical_entries: List[Any] = []
    for entry in reported:
        if not isinstance(entry, dict):
            canonical_entries.append(entry)  # state.py's own isinstance check drops this
            continue
        canonical_entries.append(
            {"origin": _canonical_origin(str(entry.get("origin") or "")), "mode": entry.get("mode")}
        )

    # Keyed canonically, with the effective (most-restrictive) mode, so a
    # legacy variant-spelled row doesn't audit as a spurious clear plus add.
    before = {
        origin: state.get_origin_silent_mode(device_id, origin)
        for origin in {_canonical_origin(row["origin"]) for row in state.list_origin_silent_modes(device_id)}
    }
    result = state.replace_origin_silent_modes(device_id, canonical_entries)
    if result.get("overflow"):
        audit.record("origin_silent_mode_overflow", device=device_id, count=result.get("count"))
        return

    applied: Dict[str, str] = result.get("applied", {})
    for origin, bad_mode in result.get("rejected", []):
        audit.record("origin_silent_mode_rejected", device=device_id, origin=origin, reported=bad_mode)

    for origin in set(before) | set(applied):
        before_mode = before.get(origin)
        after_mode = applied.get(origin)
        if before_mode != after_mode:
            audit.record(
                "origin_silent_mode_changed", device=device_id, origin=origin, before=before_mode, after=after_mode
            )


class Relay:
    """Owns the WS server thread and the live connection registry."""

    def __init__(self) -> None:
        self.cfg = config.load()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.connections: Dict[str, Connection] = {}
        self.started_at: float = 0.0
        self.last_error: str = ""
        self._ready = threading.Event()
        self._stop = threading.Event()
        # device_id -> failed-hello timestamps and a per-IP brute-force gate.
        # Both are only ever touched from the relay's own asyncio loop thread
        # (inside _handle/_on_hello), so no lock is needed here.
        self._hello_failures_by_ip: Dict[str, List[float]] = {}
        # Background tasks fired with asyncio.ensure_future() (e.g. seq-gap
        # notifies, closing a superseded connection) — kept alive and logged
        # instead of being allowed to vanish silently on GC.
        self._background_tasks: "set[asyncio.Task]" = set()
        # devices.md DV1.2: guards Connection.liveness_state's read-modify-
        # write. _update_liveness is called both from a tool-handler thread
        # (status()/liveness(), off the relay loop entirely) and from the
        # DV1.3 sweep (on the relay loop thread) -- this is what keeps the
        # two from ever double-auditing the same alive<->stale transition.
        self._liveness_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        if self.thread is not None and self.thread.is_alive():
            return self.status()["listening"]
        self.thread = threading.Thread(target=self._run, name="browser-bridge-relay", daemon=True)
        self.thread.start()
        ready = self._ready.wait(timeout=10)
        if not ready:
            self.last_error = self.last_error or "relay did not become ready within 10s"
            logger.error("browser_bridge: relay failed to start within 10s (%s)", self.last_error)
            return False
        # _ready is set on both the successful-bind and the failed-bind paths
        # in _serve(), so "ready" alone doesn't mean "listening" — check the
        # actual outcome before reporting success.
        listening = self.status()["listening"]
        if not listening:
            logger.error("browser_bridge: relay failed to bind (%s)", self.last_error)
        return listening

    def stop(self) -> None:
        self._stop.set()
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except Exception as exc:  # pragma: no cover - surfaced via status tool
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("browser_bridge: relay crashed")
            self._ready.set()
        finally:
            loop.close()

    async def _serve(self) -> None:
        from websockets.asyncio.server import serve

        host = str(self.cfg["host"])
        port = int(self.cfg["port"])
        try:
            server = await serve(
                self._handle,
                host,
                port,
                ping_interval=20,
                ping_timeout=20,
                max_size=protocol.MAX_FRAME_BYTES,
            )
        except OSError as exc:
            self.last_error = f"bind {host}:{port} failed: {exc}"
            audit.record("relay_bind_failed", host=host, port=port, error=str(exc))
            self._ready.set()
            return

        self.started_at = time.time()
        self.last_error = ""
        audit.record("relay_started", host=host, port=port, protocol_version=protocol.PROTOCOL_VERSION)
        logger.info("browser_bridge: relay listening on ws://%s:%s%s", host, port, self.cfg["path"])
        self._ready.set()
        # No approval-reap step here any more: approvals.py's correlation table
        # is in-memory only (see its module docstring), so a fresh bind always
        # starts with zero pending approvals -- there is nothing left over
        # from a previous process to resolve, because that process's blocked
        # gateway-worker threads died with it.
        # devices.md DV1.3: the stale sweep, spawned once the socket is bound
        # so it runs for the lifetime of this server exactly like `server`
        # itself, on this same loop.
        self._spawn(self._stale_sweep_loop())
        async with server:
            await asyncio.Future()

    # -- connection handling ----------------------------------------------

    async def _handle(self, ws) -> None:
        remote, remote_ip = _remote_of(ws)
        conn = Connection(ws, remote)
        hello_failures = 0
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except (TypeError, ValueError):
                    await conn.send_error(None, protocol.PARSE_ERROR, "malformed frame")
                    continue
                if not isinstance(frame, dict):
                    await conn.send_error(None, protocol.INVALID_REQUEST, "frame must be an object")
                    continue

                # Silent Fetch rev 4 §F root cause: seq tracking must see
                # EVERY inbound frame, including a bare response to our own
                # gateway->extension request (no "method" field). This used to
                # `continue` before ever reaching _check_seq below, so
                # expected_in_seq silently fell one behind every such
                # response -- the very next request/notification the
                # extension sent then looked like a gap ("expected N got
                # N+1") that was never actually lost, just never counted.
                self._check_seq(conn, frame)

                # Responses to our own gateway→extension requests.
                if "method" not in frame and "id" in frame:
                    conn.resolve(frame)
                    continue

                method = frame.get("method", "")
                request_id = frame.get("id")

                if method == "device.hello" and self._hello_rate_limited(remote_ip):
                    audit.record("pairing_rate_limited", remote=remote)
                    if request_id is not None:
                        await conn.send_error(
                            request_id, protocol.RATE_LIMITED, protocol.ERROR_MESSAGES[protocol.RATE_LIMITED]
                        )
                    await ws.close(code=1008, reason="rate limited")
                    return

                if not conn.authenticated and method != "device.hello":
                    if request_id is not None:
                        await conn.send_error(request_id, protocol.NOT_AUTHENTICATED, "device.hello required first")
                    else:
                        audit.record("notification_failed", method=method, code=protocol.NOT_AUTHENTICATED)
                    continue

                try:
                    result = await self._dispatch(conn, method, frame.get("params") or {})
                except BridgeError as exc:
                    if method == "device.hello":
                        hello_failures += 1
                        self._record_hello_failure(remote_ip)
                    if request_id is not None:
                        await conn.send_error(request_id, exc.code, exc.message, exc.data)
                    else:
                        # JSON-RPC 2.0 forbids responding to a notification, but a
                        # failed one must still leave a trace rather than vanish.
                        audit.record(
                            "notification_failed", device=conn.device_id, method=method,
                            code=exc.code, message=exc.message,
                        )
                    if hello_failures >= MAX_HELLO_FAILURES:
                        audit.record("pairing_abuse_disconnect", remote=remote, failures=hello_failures)
                        await ws.close(code=1008, reason="too many failed hello attempts")
                        return
                    continue
                except Exception as exc:
                    logger.exception("browser_bridge: handler error for %s", method)
                    if request_id is not None:
                        await conn.send_error(request_id, protocol.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
                    else:
                        audit.record(
                            "notification_failed", device=conn.device_id, method=method,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    continue

                # devices.md DV1.4: clock skew from this frame's own envelope
                # `ts` vs. the gateway clock. After the dispatch, not before —
                # for device.hello this is the point conn.device_id is first
                # populated, so an excessive-skew audit line names a real
                # device rather than "".
                if method in ("device.hello", "device.heartbeat"):
                    self._track_clock_skew(conn, frame.get("ts"))

                if request_id is not None:
                    await conn.send_result(request_id, result)
        except Exception as exc:
            logger.debug("browser_bridge: connection closed (%s)", exc)
        finally:
            self._drop(conn)

    def _check_seq(self, conn: Connection, frame: Dict[str, Any]) -> None:
        """Track inbound sequence numbers; a gap means frames were lost.

        Silent Fetch rev 4 §F: called for EVERY inbound frame (requests,
        notifications, and bare responses to a gateway->extension request
        alike) -- a frame skipped here is a frame whose seq the gateway never
        counted, which is exactly what used to make the NEXT frame look like
        a gap that was never real. ``revealing_method`` names whatever this
        frame actually was (its own "method", or "<response>" when it is a
        bare response with none), so a genuine gap's audit line says what
        frame exposed it instead of just the bare numbers.
        """
        seq = frame.get("seq")
        if not isinstance(seq, int):
            return
        if seq != conn.expected_in_seq and conn.authenticated:
            revealing_method = frame.get("method") if isinstance(frame.get("method"), str) else "<response>"
            audit.record(
                "seq_gap", device=conn.device_id, expected=conn.expected_in_seq, got=seq,
                revealing_method=revealing_method,
            )
            self._spawn(conn.notify("conn.resync", {"expected_seq": conn.expected_in_seq, "got_seq": seq}))
        conn.expected_in_seq = seq + 1

    def _drop(self, conn: Connection) -> None:
        conn.fail_all(f"device {conn.device_id or '(unauthenticated)'} disconnected before answering")
        if conn.session_id:
            state.close_session(conn.session_id)
            audit.record("device_disconnected", device=conn.device_id, session=conn.session_id)
        if conn.device_id:
            # A device dropping its connection while a tool call is blocked
            # waiting on its answer is a denial, not a hang (plan §3.4) --
            # never leave approvals.require() waiting on a socket that's gone.
            try:
                denied = approvals.deny_pending_for_device(conn.device_id)
                if denied:
                    logger.info(
                        "browser_bridge: denied %d pending approval(s) for disconnected device %s",
                        denied, conn.device_id,
                    )
            except Exception:
                logger.exception("browser_bridge: failed to deny pending approvals for %s", conn.device_id)
        if conn.device_id and self.connections.get(conn.device_id) is conn:
            del self.connections[conn.device_id]

    def _spawn(self, coro: "Any") -> "asyncio.Task":
        """Fire a background coroutine on the relay loop without losing it.

        A bare ``asyncio.ensure_future(...)`` with no stored reference can be
        garbage-collected mid-flight, and any exception it raises vanishes
        silently. Keeping a reference (and a done-callback to discard it once
        finished) fixes both.
        """
        task = asyncio.ensure_future(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)
        return task

    def _on_background_task_done(self, task: "asyncio.Task") -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("browser_bridge: background task failed: %r", exc)

    # -- brute-force gate ----------------------------------------------------

    def _hello_rate_limited(self, remote_ip: str) -> bool:
        """True when this IP has already used up its failed-hello budget.

        Keyed by IP (not by connection) so reconnecting doesn't reset an
        attacker's count the way the per-connection MAX_HELLO_FAILURES guard
        does on its own.
        """
        cutoff = time.time() - HELLO_RATE_LIMIT_WINDOW_SECONDS
        self._prune_hello_failures(cutoff)
        return len(self._hello_failures_by_ip.get(remote_ip, [])) >= HELLO_RATE_LIMIT_MAX_FAILURES

    def _record_hello_failure(self, remote_ip: str) -> None:
        if not remote_ip:
            return
        self._hello_failures_by_ip.setdefault(remote_ip, []).append(time.time())
        self._enforce_hello_tracking_cap()

    def _enforce_hello_tracking_cap(self) -> None:
        """Bound tracked-IP count regardless of how wide an attack spreads.

        Evicts the IPs whose most recent failure is oldest (each IP's
        timestamp list is append-only, so ``attempts[-1]`` is its latest),
        in one batch down to ``cap - HELLO_RATE_LIMIT_EVICT_BATCH`` rather
        than trimming to exactly the cap — see the constant's comment for why.
        """
        overflow = len(self._hello_failures_by_ip) - HELLO_RATE_LIMIT_MAX_TRACKED_IPS
        if overflow <= 0:
            return
        to_evict = overflow + HELLO_RATE_LIMIT_EVICT_BATCH
        oldest_first = sorted(self._hello_failures_by_ip.items(), key=lambda kv: kv[1][-1])
        for ip, _ in oldest_first[:to_evict]:
            del self._hello_failures_by_ip[ip]
        audit.record(
            "hello_tracking_cap_evicted", evicted=min(to_evict, len(oldest_first)),
            tracked=len(self._hello_failures_by_ip),
        )

    def _prune_hello_failures(self, cutoff: float) -> None:
        """Drop expired attempts, and IPs with none left, so a wide attack
        spread across many source IPs can't grow this dict without bound."""
        dead_ips = []
        for ip, attempts in self._hello_failures_by_ip.items():
            fresh = [t for t in attempts if t >= cutoff]
            if fresh:
                self._hello_failures_by_ip[ip] = fresh
            else:
                dead_ips.append(ip)
        for ip in dead_ips:
            del self._hello_failures_by_ip[ip]

    # -- method dispatch ---------------------------------------------------

    async def _dispatch(self, conn: Connection, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        handler = {
            "device.hello": self._on_hello,
            "device.heartbeat": self._on_heartbeat,
            "device.goodbye": self._on_goodbye,
            "state.report": self._on_state_report,
            "grant.set": self._on_grant_set,
            "kill.switch": self._on_kill_switch,
            "approval.respond": self._on_approval_respond,
            "conn.resync": self._on_conn_resync,
            "auth.replayed": self._on_auth_replayed,
            "attach.retried": self._on_attach_retried,
            "attach.stripped": self._on_attach_stripped,
            "attach.sessionDropped": self._on_attach_session_dropped,
            "tab.changed": self._on_tab_changed,
            "silent.fetch.chunk": self._on_silent_fetch_chunk,
            "silent.worker": self._on_silent_worker,
            "conn.send_failed": self._on_conn_send_failed,
        }.get(method)
        if handler is None:
            raise BridgeError(protocol.METHOD_NOT_FOUND, f"unknown method: {method}")
        return await handler(conn, params)

    async def _on_hello(self, conn: Connection, params: Dict[str, Any]) -> Dict[str, Any]:
        reported_version = params.get("protocol_version")
        if not _protocol_version_supported(reported_version):
            raise BridgeError(
                protocol.PROTOCOL_MISMATCH,
                f"gateway supports protocol {protocol.MIN_SUPPORTED_PROTOCOL_VERSION}-{protocol.PROTOCOL_VERSION}, "
                f"client sent {reported_version!r} — reload the extension to update it",
            )
        device_info = params.get("device") or {}
        name = str(device_info.get("device_name") or "unnamed-device")[:64]
        platform = str(device_info.get("platform") or "")[:32]
        browser = str(device_info.get("browser") or "")[:32]
        ext_version = str(device_info.get("extension_version") or "")[:32]

        issued_token = ""
        pair_code = str(params.get("pair_code") or "")
        if pair_code:
            if not state.redeem_pair_code(pair_code):
                audit.record("pairing_rejected", remote=conn.remote, name=name)
                raise BridgeError(protocol.PAIR_CODE_INVALID, "pairing code unknown, expired, or already used")
            created = state.register_device(name, platform, browser, ext_version)
            conn.device_id = created["device_id"]
            issued_token = created["device_token"]
            audit.record("device_paired", device=conn.device_id, name=name, remote=conn.remote, platform=platform)
        else:
            device_id = str(params.get("device_id") or "")
            try:
                row = state.authenticate(device_id, str(params.get("device_token") or ""))
            except state.DeviceRevoked:
                audit.record("auth_rejected", remote=conn.remote, device=device_id, reason="revoked")
                raise BridgeError(protocol.TOKEN_REVOKED, "device token revoked")
            if row is None:
                audit.record("auth_rejected", remote=conn.remote, device=device_id)
                raise BridgeError(protocol.TOKEN_INVALID, "device token not recognised")
            conn.device_id = device_id
            state.touch_device(device_id, name=name, platform=platform, browser=browser, ext_version=ext_version)

        # devices.md DV5 fix: an operator's name override (state.py's
        # device_name_override, set via `hermes browser-bridge devices
        # rename`) outranks whatever this hello just reported -- touch_device
        # above already refused to write it into devices.name, and the live
        # in-memory Connection.device_name must not fall back to the raw
        # report either, or every OTHER read that still asks the live
        # connection (rather than state.db) would show the reverted name for
        # as long as this socket stays open. A freshly paired device
        # (the `if pair_code:` branch above) can never have an override yet
        # -- its device_id didn't exist before this call -- so this is a
        # no-op there; `name` is used as reported.
        conn.device_name = state.get_device_name_override(conn.device_id) or name
        # devices.md DV1.2: hello counts as a heartbeat -- a device that just
        # paired or reconnected is "alive" from this instant, not stale until
        # its first real device.heartbeat lands up to HEARTBEAT_INTERVAL_MS
        # later.
        conn.last_heartbeat = _now()
        # ASK 3: sent on every hello (including a reconnect) so a device that
        # reconnects while paused never briefly reports itself as live/
        # sharing before its next heartbeat catches up.
        conn.paused = bool(params.get("paused"))
        # G0.7.3: kept for browser_bridge_status, separate from the range
        # check above — a device can be inside the supported range and still
        # be behind the gateway's own protocolVersion.
        conn.protocol_version = str(reported_version or "")
        _apply_reported_redaction(conn.device_id, params)
        _apply_reported_powers(conn.device_id, params)
        _apply_reported_default_mode(conn.device_id, params)
        _apply_reported_lease_seconds(conn.device_id, params)
        _apply_reported_commit_mode(conn.device_id, params)
        # SF6 'Lost changes': resent on hello (and every heartbeat below) for
        # the same reason redaction/powers/device_default_mode are — a
        # state.report push alone is fire-and-forget over an unreliable
        # link, so a reconnecting device now re-establishes its full
        # background-requests map instead of trusting whatever the gateway
        # last durably heard.
        _apply_reported_origin_silent_mode(conn.device_id, params)
        conn.session_id = state.open_session(conn.device_id, conn.remote)
        previous = self.connections.get(conn.device_id)
        if previous is not None and previous is not conn:
            audit.record("device_reconnect_replaced", device=conn.device_id, session=previous.session_id)
            # The old socket's receive loop (and its touch_device() heartbeats)
            # keeps running until its websocket actually closes. Schedule that
            # close rather than awaiting it inline, so this hello's response
            # isn't held up by the old connection's close handshake.
            self._spawn(self._close_superseded(previous))
        self.connections[conn.device_id] = conn
        audit.record("device_connected", device=conn.device_id, session=conn.session_id, remote=conn.remote)

        result: Dict[str, Any] = {
            "session": conn.session_id,
            "device_id": conn.device_id,
            "protocol_version": protocol.PROTOCOL_VERSION,
            "min_supported_protocol_version": protocol.MIN_SUPPORTED_PROTOCOL_VERSION,
            "server_version": config.VERSION,
            "heartbeat_interval_ms": protocol.HEARTBEAT_INTERVAL_MS,
            "grants": [
                {"origin": g["origin"], "mode": g["mode"]} for g in state.list_grants(conn.device_id)
            ],
            # What an origin with no row in `grants` above actually gets. Sent
            # so the popup's per-origin picker can show the effective mode
            # instead of rendering blank and leaving the user to guess — the
            # gateway is still the only enforcement point, this is display
            # truth. Read fresh from config rather than cached at startup so
            # a config.yaml edit plus a gateway restart is all it takes.
            "default_mode": state.effective_default_mode(conn.device_id),
            "resumed": bool(params.get("resume_session")),
            **_silent_fetch_hello_fields(),
            **_priority_hello_fields(conn.device_id),
        }
        if issued_token:
            result["device_token"] = issued_token
        return result

    async def _on_heartbeat(self, conn: Connection, params: Dict[str, Any]) -> Dict[str, Any]:
        conn.last_heartbeat = _now()
        attached = params.get("attached")
        if isinstance(attached, list):
            conn.attached = attached
        # ASK 3: the heartbeat is the steady-state channel for this — hello
        # only fires once per connection, and the user can toggle pause at
        # any point during a long-lived one.
        if "paused" in params:
            conn.paused = bool(params.get("paused"))
        _apply_reported_redaction(conn.device_id, params)
        _apply_reported_powers(conn.device_id, params)
        _apply_reported_default_mode(conn.device_id, params)
        _apply_reported_lease_seconds(conn.device_id, params)
        _apply_reported_commit_mode(conn.device_id, params)
        _apply_reported_origin_silent_mode(conn.device_id, params)
        state.touch_device(conn.device_id)
        # Grants ride the heartbeat, not just hello. A grant can change with no
        # extension involvement whatsoever: answering an approval "always"
        # writes one here (approvals.py), and so does `hermes browser-bridge`
        # from a shell. A popup fed only by hello therefore showed a stale mode
        # for the rest of the connection — and did so most visibly right after
        # the user granted standing access, which is the one moment the display
        # has to be right. Worst-case staleness is now one heartbeat interval,
        # and it self-heals no matter how the grant changed.
        return {
            "ts": int(time.time() * 1000),
            "server_seq": conn.out_seq,
            "grants": [
                {"origin": g["origin"], "mode": g["mode"]} for g in state.list_grants(conn.device_id)
            ],
            "default_mode": state.effective_default_mode(conn.device_id),
            **_silent_fetch_hello_fields(),
            **_priority_hello_fields(conn.device_id),
        }

    async def _on_goodbye(self, conn: Connection, params: Dict[str, Any]) -> Dict[str, Any]:
        audit.record("device_goodbye", device=conn.device_id, reason=params.get("reason", ""))
        return {"ok": True}

    async def _on_state_report(self, conn: Connection, params: Dict[str, Any]) -> Dict[str, Any]:
        tabs = params.get("tabs")
        if isinstance(tabs, list):
            conn.attached = [tab for tab in tabs if tab.get("attached")]
        # ASK 3: the extension has no `paused`-only state.report call site (a
        # pause toggle only refreshes offscreen.ts's local gate — see
        # pause.changed's doc comment there), but `paused` is honored here the
        # same way hello/heartbeat are so it's correct the day one exists,
        # rather than a schema field nothing ever reads.
        if "paused" in params:
            conn.paused = bool(params.get("paused"))
        # Redaction DOES have a real state.report call site: offscreen.ts's
        # `redaction.changed` handler pushes one immediately after Options
        # saves a new policy, specifically so this doesn't wait for the next
        # heartbeat (see client.ts's reportRedactionPolicy()).
        _apply_reported_redaction(conn.device_id, params)
        # Same real call site for powers: offscreen.ts's `powers.changed`
        # handler (client.ts's reportPowerPolicy()).
        _apply_reported_powers(conn.device_id, params)
        # Same real call site for the device default mode: offscreen.ts's
        # `defaultMode.changed` handler (client.ts's reportDefaultMode()).
        _apply_reported_default_mode(conn.device_id, params)
        # Same real call site for the tab lease duration: offscreen.ts's
        # `lease.changed` handler (client.ts's reportLeaseSeconds()).
        _apply_reported_lease_seconds(conn.device_id, params)
        # speedimprovements.md H1: same real call site for the committing-
        # actions mode: offscreen.ts's `commitMode.changed` handler
        # (client.ts's reportCommitMode()).
        _apply_reported_commit_mode(conn.device_id, params)
        # SF6: same real call site for the per-origin silent-fetch override:
        # the popup's "Background requests" control pushes a state.report
        # immediately on change, mirroring `defaultMode.changed` above but
        # per-origin — and, per the 'Lost changes' fix, carrying the
        # device's FULL current map each time, not just this one change.
        _apply_reported_origin_silent_mode(conn.device_id, params)
        return {"ok": True}

    async def _on_grant_set(self, conn: Connection, params: Dict[str, Any]) -> Dict[str, Any]:
        origin = str(params.get("origin") or "")
        mode = str(params.get("mode") or "")
        if not origin or mode not in protocol.MODES:
            raise BridgeError(protocol.INVALID_PARAMS, "origin and a valid mode are required")
        state.set_grant(conn.device_id, origin, mode)
        audit.record("grant_set", device=conn.device_id, origin=origin, mode=mode, source="popup")
        return {"ok": True}

    async def _on_kill_switch(self, conn: Connection, params: Dict[str, Any]) -> Dict[str, Any]:
        released = len(conn.attached)
        conn.attached = []
        audit.record("kill_switch", device=conn.device_id, released=released, reason=params.get("reason", "popup"))
        return {"released": released}

    async def _close_superseded(self, conn: Connection) -> None:
        """Close a connection's socket after a fresher hello has replaced it in the registry."""
        try:
            await conn.ws.close(code=1000, reason="superseded by reconnect")
        except Exception:
            logger.debug("browser_bridge: closing superseded connection for %s failed", conn.device_id)

    # -- devices.md DV1: liveness, the stale sweep, clock skew --------------

    def _update_liveness(self, conn: Connection, now: Optional[float] = None) -> str:
        """Return conn's current "alive"/"stale" label, auditing the
        transition exactly once (§2 rule 1: alive is socket-open AND a
        heartbeat within effective_device_alive_after_seconds; a device with
        no open socket at all is "offline", handled by callers that check
        self.connections membership first — this method only ever sees
        connections that ARE in the registry).

        The single place both a read (status()/liveness(), typically off a
        tool-handler thread) and the DV1.3 sweep (on the relay loop thread)
        go through, so whichever of the two notices a transition first is
        the only one that audits it — see `_liveness_lock`.
        """
        now = now if now is not None else _now()
        alive_after = config.effective_device_alive_after_seconds(config.load())
        age = now - conn.last_heartbeat
        new_state = "stale" if age > alive_after else "alive"
        with self._liveness_lock:
            if new_state != conn.liveness_state:
                event = "device_alive_again" if new_state == "alive" else "device_stale"
                audit.record(event, device=conn.device_id, age_s=round(age, 1), alive_after_s=alive_after)
                conn.liveness_state = new_state
            return conn.liveness_state

    def liveness(self, device_id: str) -> str:
        """DV3's resolution order reads this: "alive" | "stale" | "offline".
        "offline" for anything with no open socket at all, regardless of what
        state.db's last_seen says -- §2 rule 1 draws the alive/stale line
        only across an OPEN connection.
        """
        conn = self.connections.get(device_id)
        if conn is None:
            return "offline"
        return self._update_liveness(conn)

    def alive_devices(self) -> List[str]:
        """Device ids currently alive — DV3's implicit-selection candidate
        pool starts from this list."""
        return [
            device_id for device_id, conn in list(self.connections.items())
            if self._update_liveness(conn) == "alive"
        ]

    def _track_clock_skew(self, conn: Connection, frame_ts: Any) -> None:
        """devices.md DV1.4: gateway clock minus this frame's own envelope
        `ts` (positive means the device's clock is BEHIND the gateway's). No
        new wire field — envelope `ts` has been on every frame since the
        protocol's shared envelope was defined; this is the first thing that
        reads it for this purpose.

        Audited once per excursion past +/-CLOCK_SKEW_ALERT_THRESHOLD_MS,
        not once per frame while it stays breached — clock_skew_alert_
        audited tracks that the same way liveness_state tracks alive/stale.
        """
        if not isinstance(frame_ts, int):
            return
        conn.clock_skew_ms = int(_now() * 1000) - frame_ts
        excessive = abs(conn.clock_skew_ms) > CLOCK_SKEW_ALERT_THRESHOLD_MS
        if excessive and not conn.clock_skew_alert_audited:
            audit.record("clock_skew_excessive", device=conn.device_id, skew_ms=conn.clock_skew_ms)
            conn.clock_skew_alert_audited = True
        elif not excessive:
            conn.clock_skew_alert_audited = False

    async def _stale_sweep_loop(self) -> None:
        """DV1.3: every STALE_SWEEP_INTERVAL_SECONDS, sweep for connections
        stale past device_offline_after_seconds. Runs for the lifetime of the
        relay loop; a failure in one pass is logged and never kills the loop
        (this must keep running even if one pass throws)."""
        while True:
            await asyncio.sleep(STALE_SWEEP_INTERVAL_SECONDS)
            try:
                self._sweep_stale_once()
            except Exception:
                logger.exception("browser_bridge: stale sweep failed")

    def _sweep_stale_once(self, now: Optional[float] = None) -> None:
        """One sweep pass: refresh every connection's liveness, then close
        the socket for any whose last heartbeat exceeds
        device_offline_after_seconds.

        Only ever iterates self.connections, which by construction holds
        AUTHENTICATED connections only — Connection.device_id is set (and
        the connection added to this dict) at the end of a successful
        `_on_hello`, never before. A socket still negotiating device.hello,
        or one that failed it, is simply not in this dict yet, so this sweep
        can never race pairing or interrupt a hello in flight — there is
        nothing here for it to touch.
        """
        now = now if now is not None else _now()
        offline_after = config.effective_device_offline_after_seconds(config.load())
        for conn in list(self.connections.values()):
            self._update_liveness(conn, now=now)
            if conn.sweep_scheduled:
                continue
            age = now - conn.last_heartbeat
            if age > offline_after:
                conn.sweep_scheduled = True
                audit.record("device_swept", device=conn.device_id, age_s=round(age, 1), offline_after_s=offline_after)
                self._spawn(self._close_stale(conn))

    async def _close_stale(self, conn: Connection) -> None:
        """DV1.3: close one swept connection's socket with 1001 (Going Away)
        — a plain "we're done talking to you", not a refusal of any specific
        request, so the standard code fits better than minting a bridge-
        specific 4xxx one (those are reserved for JSON-RPC error responses,
        a different code space this WS close doesn't need to share). The
        extension already reconnects on any unexpected close (client.ts)
        regardless of code.

        Nothing here calls Connection.fail_all directly: closing the socket
        makes _handle's `async for raw in ws` loop end exactly as it would
        for a real network drop, and its own `finally: self._drop(conn)`
        is what fails every pending call (Connection.fail_all) and releases
        leases/approvals — the same path a real disconnect takes, so a swept
        connection's pending calls fail promptly with no separate code path
        to keep in sync.
        """
        try:
            await conn.ws.close(code=1001, reason="stale: no heartbeat within device_offline_after_seconds")
        except Exception:
            logger.debug("browser_bridge: closing stale connection for %s failed", conn.device_id)

    def run_sweep_once(self) -> None:
        """Test hook (tests/test_dv1_liveness.py): run one sweep pass
        synchronously, marshaled onto the relay's own loop via
        run_coroutine_threadsafe exactly like Relay.call() does for outbound
        calls — a swept connection's ws.close() must run on the loop that
        actually owns its transport. Blocks until the pass (and the closes it
        schedules) have run once around the loop."""
        if self.loop is None:
            return

        async def _once() -> None:
            self._sweep_stale_once()
            # Let any _close_stale task this pass just spawned actually run
            # before this coroutine (and therefore this call) returns.
            await asyncio.sleep(0)

        future = asyncio.run_coroutine_threadsafe(_once(), self.loop)
        future.result(timeout=10)

    async def _on_conn_resync(self, conn: Connection, params: Dict[str, Any]) -> Dict[str, Any]:
        """Client saw a gap in our outbound seq and is telling us so.

        Realign our outbound counter to what the client now expects, so the
        next frame we send lines up instead of tripping the same gap again.
        """
        expected = params.get("expected_seq")
        if isinstance(expected, int) and expected > 0:
            conn.out_seq = expected - 1
        audit.record(
            "conn_resync", device=conn.device_id, expected_seq=params.get("expected_seq"),
            got_seq=params.get("got_seq"),
        )
        return {"ok": True}

    async def _on_conn_send_failed(self, conn: Connection, params: Dict[str, Any]) -> Dict[str, Any]:
        """Silent Fetch rev 4 §F: client.ts's serializeAndSend() queues one of
        these whenever a frame took a seq (was about to be sent with a real
        seq number) but never actually reached the wire (a JSON.stringify
        throw, or socket.send() itself throwing), and reports it on the next
        frame that DOES go out. Purely informational -- audited so a seq_gap
        that traces back to a genuine local failure (as opposed to a dropped
        TCP segment or a gateway-side bug) is distinguishable in the audit
        trail, never itself a reason to refuse or resync anything.
        """
        audit.record(
            "conn_send_failed", device=conn.device_id,
            method=str(params.get("method") or "unknown")[:64],
            seq=params.get("seq") if isinstance(params.get("seq"), int) else None,
            error_kind=str(params.get("error_kind") or "unknown")[:32],
        )
        return {"ok": True}

    async def _on_auth_replayed(self, conn: Connection, params: Dict[str, Any]) -> Dict[str, Any]:
        """G3.7.5: the extension notifies us every time a staged HTTP
        basic-auth credential was actually handed to a live
        chrome.webRequest.onAuthRequired challenge -- the audit-worthy event.
        Deliberately a bare notification (no id expected back): the audit
        write itself is the entire point, there is nothing here for the
        extension to need an answer to. `origin` is the only field this ever
        carries or logs -- never a username or password, which the extension
        never sends over this socket at all (see protocol/schema.json's
        `auth.replayed` description)."""
        origin = str(params.get("origin") or "")
        if not origin:
            raise BridgeError(protocol.INVALID_PARAMS, "origin is required")
        audit.record("http_auth_replayed", device=conn.device_id, origin=origin, timestamp=time.time())
        return {"ok": True}

    async def _on_silent_fetch_chunk(self, conn: Connection, params: Dict[str, Any]) -> Dict[str, Any]:
        """SF3.2: buffer one slice of a still-in-flight silent.fetch body.

        Bare notification (no id expected back, same JSON-RPC 2.0 shape as
        attach.retried above) -- but unlike those audit-only events, a
        problem found here doesn't just get logged: it fails the *pending
        silent.fetch call itself*, by resolving its future with a
        BridgeError exactly as if the extension's own result frame had
        carried an error. That is the only way a caller blocked in
        Relay.call() ever learns a chunk stream went wrong, since JSON-RPC
        forbids responding to a notification directly.
        """
        request_id = params.get("id")
        seq = params.get("seq")
        data_b64 = params.get("data_b64")
        last = bool(params.get("last"))

        future = conn.pending.get(request_id)

        def _fail(code: int, message: str) -> Dict[str, Any]:
            audit.record(
                "silent_fetch_chunk_error", device=conn.device_id, id=request_id, seq=seq, code=code, reason=message,
            )
            buf = conn.chunk_buffers.pop(request_id, None)
            if buf is not None:
                buf.free()
            if future is not None and not future.done():
                future.set_exception(BridgeError(code, message))
            return {"ok": False}

        if not isinstance(seq, int) or not isinstance(data_b64, str) or request_id is None:
            return _fail(protocol.INVALID_PARAMS, "silent.fetch.chunk: missing/malformed id, seq or data_b64")

        if future is None:
            # No pending silent.fetch call for this id at all -- never
            # existed on this connection, or its result/an earlier failure
            # already completed and freed the buffer. Nothing pending to
            # fail; audit the orphan chunk and drop it.
            audit.record("silent_fetch_chunk_orphan", device=conn.device_id, id=request_id, seq=seq)
            return {"ok": False}

        buf = conn.chunk_buffers.get(request_id)
        if buf is None:
            buf = ChunkBuffer()
            conn.chunk_buffers[request_id] = buf
        elif buf.finished:
            return _fail(
                protocol.INVALID_REQUEST,
                f"silent.fetch.chunk: seq={seq} arrived for id={request_id} after it was already finished",
            )

        if seq != buf.expected_seq:
            kind = "duplicate" if seq < buf.expected_seq else "gap"
            return _fail(
                protocol.SEQ_GAP,
                f"silent.fetch.chunk: sequence {kind} for id={request_id} (expected seq={buf.expected_seq}, got {seq})",
            )

        try:
            decoded = base64.b64decode(data_b64, validate=True)
        except Exception:
            return _fail(protocol.INVALID_PARAMS, f"silent.fetch.chunk: invalid base64 for id={request_id} seq={seq}")

        global _silent_fetch_buffered_bytes
        per_request_ceiling = silent_fetch_per_request_ceiling_bytes()
        if buf.total_bytes + len(decoded) > per_request_ceiling:
            return _fail(
                protocol.INTERNAL_ERROR,
                f"silent.fetch.chunk: id={request_id} exceeded the per-request "
                f"{per_request_ceiling}-byte ceiling",
            )
        if _silent_fetch_buffered_bytes + len(decoded) > silent_fetch_global_ceiling_bytes():
            return _fail(protocol.INTERNAL_ERROR, "silent.fetch.chunk: global chunk-buffer memory ceiling exceeded")

        buf.parts[seq] = decoded
        buf.total_bytes += len(decoded)
        buf.expected_seq += 1
        _silent_fetch_buffered_bytes += len(decoded)
        if last:
            buf.finished = True
        return {"ok": True}

    async def _on_silent_worker(self, conn: Connection, params: Dict[str, Any]) -> Dict[str, Any]:
        """SF-audit: silent-pool.ts's own worker-pool lifecycle trail --
        launch, launch failure, TTL/origin-drift recycle, LRU eviction, an
        explicit or global-kill-switch kill, a tab/window the user closed, and
        a service-worker-restart re-adopt -- closing the gap ProjectRules/
        silentfetch.md §2 rule 6 promised but nothing on the live call path
        actually delivered (silent_grants.record_silent_worker existed and
        was unit-tested well before anything called it for real traffic).

        Bare notification, same shape as attach.retried/auth.replayed above.
        The device attributed is ALWAYS ``conn.device_id`` -- the
        authenticated connection's own identity -- never a ``device_id`` a
        params dict might carry; a buggy or compromised extension build must
        never be able to attribute its own worker lifecycle to a different
        device's audit trail. ``origin`` is canonicalized the same way
        ``authorize_silent_fetch``/``ssrf_guard`` already do, and every other
        field is clamped or dropped rather than trusted verbatim.
        """
        from . import silent_grants  # noqa: PLC0415 - lazy: silent_grants -> tools -> relay would cycle at import time

        action = params.get("action")
        origin_raw = params.get("origin")
        origin = str(origin_raw) if isinstance(origin_raw, str) else ""

        if action not in SILENT_WORKER_ACTIONS or not origin:
            audit.record(
                "silent_worker_rejected",
                device=conn.device_id,
                origin=origin,
                action=action if isinstance(action, str) else repr(action),
            )
            return {"ok": False}

        origin = _canonical_origin(origin)

        reason = params.get("reason")
        reason = reason[:SILENT_WORKER_REASON_MAX_LEN] if isinstance(reason, str) else ""

        served = params.get("served")
        served = served if isinstance(served, int) and not isinstance(served, bool) else None

        age_ms = params.get("age_ms")
        age_ms = age_ms if isinstance(age_ms, (int, float)) and not isinstance(age_ms, bool) else None

        # Silent Fetch rev 3 §A: launch_failed's own diagnostics -- each
        # validated against its own closed shape and DROPPED (never coerced,
        # never partially trusted) on anything else, same discipline as
        # `reason`/`served`/`age_ms` above. Present on any action (not just
        # launch_failed) is harmless -- silent_grants.record_silent_worker
        # simply omits whatever it wasn't given.
        failure = params.get("failure")
        failure = failure if failure in SILENT_WORKER_FAILURE_KINDS else None

        chrome_error = params.get("chrome_error")
        chrome_error = (
            chrome_error
            if isinstance(chrome_error, str) and SILENT_WORKER_CHROME_ERROR_RE.match(chrome_error)
            else None
        )

        last_url = _sanitize_last_url(params.get("last_url"))

        redirects = params.get("redirects")
        redirects = redirects if isinstance(redirects, int) and not isinstance(redirects, bool) and redirects >= 0 else None

        redirect_kinds = params.get("redirect_kinds")
        if isinstance(redirect_kinds, dict):
            server_redirect = redirect_kinds.get("server_redirect")
            client_redirect = redirect_kinds.get("client_redirect")
            valid_counts = (
                isinstance(server_redirect, int) and not isinstance(server_redirect, bool) and server_redirect >= 0
                and isinstance(client_redirect, int) and not isinstance(client_redirect, bool) and client_redirect >= 0
            )
            redirect_kinds = (
                {"server_redirect": server_redirect, "client_redirect": client_redirect} if valid_counts else None
            )
        else:
            redirect_kinds = None

        ready_state_reached = params.get("ready_state_reached")
        ready_state_reached = ready_state_reached if ready_state_reached in SILENT_WORKER_READY_STATES else None

        elapsed_ms = params.get("elapsed_ms")
        elapsed_ms = elapsed_ms if isinstance(elapsed_ms, (int, float)) and not isinstance(elapsed_ms, bool) and elapsed_ms >= 0 else None

        silent_grants.record_silent_worker(
            conn.device_id, origin, action, reason=reason, served=served, age_ms=age_ms,
            failure=failure, chrome_error=chrome_error, last_url=last_url, redirects=redirects,
            redirect_kinds=redirect_kinds, ready_state_reached=ready_state_reached, elapsed_ms=elapsed_ms,
        )
        return {"ok": True}

    async def _on_attach_retried(self, conn: Connection, params: Dict[str, Any]) -> Dict[str, Any]:
        """The extension notifies us each time it retries a
        chrome.debugger.attach() attempt after Chrome's "different extension"
        foreign-frame refusal (see extension/src/background/cdp.ts) -- these
        overlays are often transient, so the extension retries a bounded
        number of times before giving up, and this is the audit trail for
        that. Bare notification (no id expected back), same shape as
        `auth.replayed` above. Never carries page content, only the tab id
        and attempt counters."""
        tab_id = params.get("tabId")
        attempt = params.get("attempt")
        max_attempts = params.get("maxAttempts")
        audit.record(
            "attach_retried", device=conn.device_id, tab_id=tab_id, attempt=attempt, max_attempts=max_attempts,
        )
        return {"ok": True}

    async def _on_attach_stripped(self, conn: Connection, params: Dict[str, Any]) -> Dict[str, Any]:
        """The extension notifies us each time a chrome.debugger.attach()
        call reached the 'strip and attach' workaround for Chrome's
        'different extension' foreign-frame refusal (crbug.com/40753571; see
        extension/src/background/cdp.ts) -- whether or not it ultimately
        helped. Bare notification, same shape as attach.retried above. Never
        carries page content: counts, extension ids and outcome flags only."""
        audit.record(
            "attach_stripped",
            device=conn.device_id,
            tab_id=params.get("tabId"),
            stripped_count=params.get("strippedCount"),
            extension_ids=params.get("extensionIds"),
            restored=params.get("restored"),
            session_survived=params.get("sessionSurvived"),
            reloaded=params.get("reloaded"),
            attached=params.get("attached"),
        )
        return {"ok": True}

    async def _on_attach_session_dropped(self, conn: Connection, params: Dict[str, Any]) -> Dict[str, Any]:
        """G4238 follow-up: the debugger session for a tab that was
        successfully attached got dropped later -- typically another
        extension's frame reappearing after the persistent guard
        (extension/src/content/foreign-frame-guard.ts) failed to catch it in
        time. The extension tries at most one automatic re-attach (never a
        loop) and reports the outcome here. Bare notification, same shape as
        attach.retried/attach.stripped above."""
        audit.record(
            "attach_session_dropped",
            device=conn.device_id,
            tab_id=params.get("tabId"),
            reason=params.get("reason"),
            extension_ids=params.get("extensionIds"),
            auto_reattach_attempted=params.get("autoReattachAttempted"),
            auto_reattach_succeeded=params.get("autoReattachSucceeded"),
        )
        return {"ok": True}

    async def _on_tab_changed(self, conn: Connection, params: Dict[str, Any]) -> Dict[str, Any]:
        """G3.3.4: until this workstream, `tab.changed` was declared in
        protocol/schema.json's `events` and pushed by every extension build,
        but never dispatched here -- it fell through `_dispatch`'s `unknown
        method` branch and every occurrence was silently audited as
        `notification_failed`. This is the gateway-side half of the tab
        workspace's freshness signal (coveragegaps.md G3.3.4): flag the
        tab's cached state stale (or drop it outright, for `removed`) in
        every session's own workspace on this device -- see
        `hermes_plugin/tabs.py`'s `on_tab_changed` for what each `change`
        value does and does not invalidate."""
        tab = params.get("tab") or {}
        change = str(params.get("change") or "")
        try:
            from . import tabs as tabs_mod  # noqa: PLC0415 - optional sibling module, G3.3
        except ImportError:
            return {"ok": True}
        try:
            tabs_mod.on_tab_changed(conn.device_id, tab if isinstance(tab, dict) else {}, change)
        except Exception:
            logger.exception("browser_bridge: tab.changed handling failed for device %s", conn.device_id)
        return {"ok": True}

    async def _on_approval_respond(self, conn: Connection, params: Dict[str, Any]) -> Dict[str, Any]:
        approval_id = str(params.get("approval_id") or "")
        choice = str(params.get("choice") or "")
        # approvals.resolve() is synchronous (an in-memory lock + a
        # threading.Event, no I/O) and does its own auditing, including for a
        # rejected response (unknown id, already resolved, wrong device, bad
        # choice) -- it is what decides whether this response gets to count
        # for anything.
        accepted = approvals.resolve(conn.device_id, approval_id, choice)
        audit.record(
            "approval_response", device=conn.device_id, approval_id=approval_id, choice=choice, accepted=accepted,
        )
        return {"ok": accepted}

    # -- outbound API for tool handlers (sync callers) ----------------------

    def call(self, device_id: str, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> Dict[str, Any]:
        """Call a method on a connected extension from synchronous code.

        E1 (speedimprovements.md): the ONE place every gateway->extension
        request/response round trip happens for tool handlers, so it is also
        the one place that measures it. ``timing_mod.record_relay_call()``
        is fed the wall-clock time of this call (request sent to response
        received, or to the exception that ends the wait) plus whatever
        ``timing`` object the extension attached to its own response — never
        raised into the caller, and harmless when no tool-timing wrapper
        (``tools.py``'s ``_TimingProxy``) is currently accumulating (see that
        module's ``_ensure_reset``).
        """
        conn = self.connections.get(device_id)
        if conn is None:
            raise BridgeError(protocol.TOKEN_INVALID, f"device {device_id} is not connected")
        if self.loop is None:
            raise BridgeError(protocol.INTERNAL_ERROR, "relay loop is not running")
        start = time.monotonic()
        future = asyncio.run_coroutine_threadsafe(conn.request(method, params, timeout), self.loop)
        try:
            result = future.result(timeout=timeout + 5)
            return result
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            try:
                res = locals().get("result")
                ext_timing = res.get("timing") if isinstance(res, dict) and isinstance(res.get("timing"), dict) else None
                timing_mod.record_relay_call(elapsed_ms, ext_timing, method=method)
            except Exception:
                logger.debug("browser_bridge: timing bookkeeping failed for %s", method, exc_info=True)

    def broadcast(self, method: str, params: Optional[dict] = None) -> int:
        """Fire-and-forget notification to every connected device."""
        if self.loop is None:
            return 0
        sent = 0
        for conn in list(self.connections.values()):
            asyncio.run_coroutine_threadsafe(conn.notify(method, params), self.loop)
            sent += 1
        return sent

    def disconnect(self, device_id: str, reason: str = "revoked") -> bool:
        """Drop a device now — the server side of the kill switch."""
        conn = self.connections.get(device_id)
        if conn is None or self.loop is None:
            return False

        async def _close() -> None:
            try:
                await conn.notify("device.offline", {"reason": reason})
            finally:
                await conn.ws.close(code=1000, reason=reason)

        asyncio.run_coroutine_threadsafe(_close(), self.loop)
        return True

    # -- introspection -----------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return {
            "listening": bool(self.started_at) and not self.last_error,
            "host": self.cfg["host"],
            "port": self.cfg["port"],
            "path": self.cfg["path"],
            "uptime_seconds": int(time.time() - self.started_at) if self.started_at else 0,
            "protocol_version": protocol.PROTOCOL_VERSION,
            # G0.7.3: the gateway's accepted range, so browser_bridge_status
            # can show "connected but behind" instead of only "connected".
            "min_supported_protocol_version": protocol.MIN_SUPPORTED_PROTOCOL_VERSION,
            "error": self.last_error,
            "connected": [
                {
                    "device_id": conn.device_id,
                    "device_name": conn.device_name,
                    "session": conn.session_id,
                    "remote": conn.remote,
                    "attached": conn.attached,
                    "last_heartbeat_age_s": int(time.time() - conn.last_heartbeat),
                    # devices.md DV1.2/DV1.4: liveness/last_heartbeat_at go
                    # through _update_liveness so a status() read is one of
                    # the two places (with the sweep) a transition can be
                    # noticed and audited exactly once; clock_skew_ms is None
                    # until this connection's first hello/heartbeat frame.
                    "liveness": self._update_liveness(conn),
                    "last_heartbeat_at": int(conn.last_heartbeat * 1000),
                    "clock_skew_ms": conn.clock_skew_ms,
                    "paused": conn.paused,
                    "protocol_version": conn.protocol_version,
                    "protocol_up_to_date": conn.protocol_version == protocol.PROTOCOL_VERSION,
                }
                # Snapshot with list(...): tool-handler threads call status()
                # while the relay loop thread mutates self.connections, and
                # iterating the live dict races with that ("dictionary changed
                # size during iteration") — broadcast() already snapshots this
                # way for the same reason.
                for conn in list(self.connections.values())
            ],
        }


def _remote_of(ws) -> "tuple[str, str]":
    """Returns (display 'ip:port' for logs, bare ip for rate-limit keying)."""
    try:
        peer = ws.remote_address
        if not peer:
            return "", ""
        return f"{peer[0]}:{peer[1]}", str(peer[0])
    except Exception:
        return "", ""


_relay: Optional[Relay] = None


def get_relay() -> Optional[Relay]:
    return _relay


# Top-level hermes flags that consume a following argv token as their value
# (space form, e.g. "-p x"); "--flag=value" is handled separately and needs no
# extra skip. Sourced from hermes_cli._parser.build_top_level_parser() plus
# --profile/-p, which lives in hermes_cli.main._apply_profile_override and is
# normally stripped from sys.argv before a plugin ever runs — kept here anyway
# since a monkeypatched argv (tests) or a future earlier call site may still
# carry it.
_GLOBAL_VALUE_FLAGS = {
    "-z", "--oneshot", "--usage-file", "-m", "--model", "--provider",
    "--reasoning", "-t", "--toolsets", "-r", "--resume", "-s", "--skills",
    "--profile", "-p",
}
# -c/--continue takes an optional value: only consumes the next token when it
# doesn't itself look like a flag (argparse nargs="?" semantics).
_GLOBAL_OPTIONAL_VALUE_FLAGS = {"-c", "--continue"}


def _first_subcommand_index(argv: List[str]) -> Optional[int]:
    """Find the index of hermes's first true positional token in argv.

    Walks past top-level flags — and the values some of them consume — so a
    flag's *value* (``--label gateway``, ``--queue gateway``) is never
    mistaken for the subcommand itself. This only needs to get argv parsing
    right up to the first positional; it isn't a full argparse
    reimplementation.
    """
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            return i + 1 if i + 1 < len(argv) else None
        head = arg.split("=", 1)[0]
        if "=" in arg and head in _GLOBAL_VALUE_FLAGS:
            i += 1
        elif arg in _GLOBAL_VALUE_FLAGS:
            i += 2
        elif arg in _GLOBAL_OPTIONAL_VALUE_FLAGS:
            i += 2 if (i + 1 < len(argv) and not argv[i + 1].startswith("-")) else 1
        elif arg.startswith("-"):
            # Any other flag (--yolo, --tui, -w, an option we don't know
            # about, ...) is assumed boolean: skip only itself. Worst case for
            # an unrecognised value-flag is mis-skipping a positional, never
            # mistaking its value for the subcommand.
            i += 1
        else:
            return i
    return None


def is_gateway_process() -> bool:
    """True only in the long-lived ``hermes gateway`` (or ``gateway run``) process.

    ``register(ctx)`` runs on every plugin load, including once per short-lived
    ``hermes browser-bridge ...`` CLI invocation. Without this guard each of those
    processes also tries to bind the relay port, collides with the real
    gateway's relay, and leaves a spurious ``relay_bind_failed`` audit line —
    confirmed happening in production (every CLI call, not just concurrent
    gateways). Callers (``__init__.register``) use this to decide whether to
    call ``start_relay()`` at all; ``start_relay()`` itself stays ungated so
    tests can start a relay directly without impersonating the gateway.

    Identifies the subcommand structurally (first positional token after
    hermes's own global flags) rather than scanning argv for "gateway"
    anywhere — a naive substring/index scan false-positives on argv *values*
    like ``hermes browser-bridge pair --label gateway`` or ``hermes kanban worker
    --queue gateway``, which would make that short-lived process also try to
    bind the relay port. Global flags may precede the subcommand with their
    own values (e.g. ``--profile socbot gateway run``, as seen on the live
    host), so those are walked past first. Both ``hermes gateway``
    (foreground; hermes_cli defaults to ``run`` when no gateway subcommand is
    given) and ``hermes gateway run`` start the persistent process; ``gateway
    stop/status/install/uninstall`` are themselves short-lived CLI calls and
    must not bind the port either.
    """
    argv = sys.argv[1:]
    idx = _first_subcommand_index(argv)
    if idx is None or argv[idx] != "gateway":
        return False
    next_idx = idx + 1
    return next_idx >= len(argv) or argv[next_idx] == "run"


def start_relay() -> Optional[Relay]:
    """Start the singleton relay. Returns None when disabled in config.

    Does not itself check ``is_gateway_process()`` — the plugin's own
    ``register()`` is what decides whether to call this at all; tests call it
    directly and are expected to actually bind a port.
    """
    global _relay
    cfg = config.load()
    if not cfg.get("enabled", True):
        logger.info("browser_bridge: relay disabled via config (browser_bridge.enabled: false)")
        return None
    if _relay is None:
        _relay = Relay()
        _relay.start()
    return _relay
