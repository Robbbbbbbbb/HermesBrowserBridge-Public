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
import json
import logging
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from . import approvals, audit, config, protocol, state, timing as timing_mod

logger = logging.getLogger(__name__)

MAX_HELLO_FAILURES = 5
HELLO_TIMEOUT_SECONDS = 15

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
        self.last_heartbeat = time.time()
        self.attached: list[dict] = []
        self.pending: Dict[int, asyncio.Future] = {}
        self._next_id = 1
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

    def resolve(self, frame: Dict[str, Any]) -> None:
        """Complete a pending gateway→extension request from its response frame."""
        future = self.pending.get(frame.get("id"))
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
        else:
            future.set_result(frame.get("result") or {})


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

                # Responses to our own gateway→extension requests.
                if "method" not in frame and "id" in frame:
                    conn.resolve(frame)
                    continue

                method = frame.get("method", "")
                request_id = frame.get("id")
                self._check_seq(conn, frame)

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

                if request_id is not None:
                    await conn.send_result(request_id, result)
        except Exception as exc:
            logger.debug("browser_bridge: connection closed (%s)", exc)
        finally:
            self._drop(conn)

    def _check_seq(self, conn: Connection, frame: Dict[str, Any]) -> None:
        """Track inbound sequence numbers; a gap means frames were lost."""
        seq = frame.get("seq")
        if not isinstance(seq, int):
            return
        if seq != conn.expected_in_seq and conn.authenticated:
            audit.record(
                "seq_gap", device=conn.device_id, expected=conn.expected_in_seq, got=seq,
            )
            self._spawn(conn.notify("conn.resync", {"expected_seq": conn.expected_in_seq, "got_seq": seq}))
        conn.expected_in_seq = seq + 1

    def _drop(self, conn: Connection) -> None:
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

        conn.device_name = name
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
        }
        if issued_token:
            result["device_token"] = issued_token
        return result

    async def _on_heartbeat(self, conn: Connection, params: Dict[str, Any]) -> Dict[str, Any]:
        conn.last_heartbeat = time.time()
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
