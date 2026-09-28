"""Configuration access for the browser bridge.

All settings live in Hermes' own ``config.yaml`` under ``browser_bridge.*``.
``.env`` is secrets-only per repo policy, and no ``HERMES_*`` env vars are
introduced. Defaults here are the shipped behaviour; a missing config file or
an unreadable key degrades to the default rather than failing plugin load.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

VERSION = "0.2.4"

# Silent Fetch rev 2 §2: a correctness-only sanity ceiling on
# silent_fetch.max_bytes_cap/max_buffered_bytes -- config.yaml may raise
# either with no code change, but never past this. Not a tuning knob: it
# exists so a typo'd config value (a zero, a stray extra digit) can't ask the
# gateway to buffer an unbounded amount of memory. 2 GiB is generous headroom
# over any host this plugin targets (CLAUDE.md's 3 GB gateway box) while
# still being a hard, documented backstop.
SILENT_FETCH_SANITY_MAX_BYTES = 2 * 1024 * 1024 * 1024

# Silent Fetch rev 3 §D: the clamp both the gateway-wide default
# (silent_fetch.bootstrap_timeout_ms) and a per-call silent.fetch
# bootstrap_timeout_ms param are bounded to. 5s floor: below that a worker
# launch against any real site is doomed regardless of what the caller
# wants. 120s ceiling: generous headroom over the live failure that
# motivated this (a chained redirect through an interstitial, ~18s) without
# letting a misconfigured value or a careless per-call override tie up a
# gateway thread indefinitely.
SILENT_FETCH_MIN_BOOTSTRAP_TIMEOUT_MS = 5000
SILENT_FETCH_MAX_BOOTSTRAP_TIMEOUT_MS = 120000

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "host": "0.0.0.0",
    "port": 8765,
    "path": "/bridge",
    # Minutes a printed pairing code stays usable.
    "pair_code_ttl_minutes": 10,
    # Seconds without a heartbeat before a device is considered offline.
    "device_offline_after_seconds": 90,
    # devices.md DV1.1: seconds since the last heartbeat (hello counts as one)
    # before an open-socket device is downgraded from "alive" to "stale" for
    # implicit selection purposes. 50s = 2.5x the 20s heartbeat interval —
    # tolerates one missed beat plus jitter without flapping, while still
    # being well short of device_offline_after_seconds so a caller sees
    # "stale" before the sweep below ever closes the socket. Effective value
    # is clamped by effective_device_alive_after_seconds(), never read raw.
    "device_alive_after_seconds": 50,
    # Default mode applied to an origin the user has never configured.
    # Mode applied to an origin the user has never configured. The user asked for
    # "full" as the default (2026-09-22). This is a real widening: an origin
    # with no grant row is now readable AND drivable the moment a tab on it is
    # attached, where before it was refused until the user opted in per site.
    # What still bounds it: nothing happens on any tab the user has not
    # attached, and the cross-origin fetch path deliberately ignores this
    # value (an ungranted cross-origin target is always refused, see
    # session_powers.py's SSRF guard) so this does not widen fetch's reach.
    # Set this back to "off" in config.yaml for per-site opt-in.
    "default_mode": "full",
    # Fleet-wide default seconds a tab's driving lease lasts (attach.py's
    # AttachRegistry) before another Hermes session may attach it, for any
    # device that has never reported its own (extension Options → "Tab
    # lease"). 60s matches today's hard-coded attach.LEASE_TTL_SECONDS --
    # unlike default_mode's "full" above, this is NOT a widening: behaviour
    # is unchanged until a device reports its own value. A device's own
    # reported lease_seconds always wins once it has reported one
    # (state.py's effective_lease_seconds) -- this is only the floor for a
    # device that never opens Options, same role this dict's own
    # default_mode plays for device_default_mode.
    "lease_seconds": 60,
    # Vision handling for screenshots: auto probes the model, force_* skips it.
    "vision": "auto",
    # Optional dedicated vision model for the delegation path (plan §5b layer 3).
    "vision_model": "",
    # Hours a probed provider+model's vision capability is trusted before
    # re-probing (a probe spends one real completion call — see vision.py).
    "vision_probe_ttl_hours": 24,
    # Generous on purpose: a 20-token budget was observed truncating a
    # reasoning model mid-<reasoning> before it ever emitted an answer.
    "vision_probe_max_tokens": 512,
    "vision_probe_timeout_s": 20,
    # Screenshot storage under STATE_DIR by default; override for testing or
    # to point at a different disk. Retention below is enforced on every
    # save (vision.py), not on a schedule — no gateway-startup hook exists
    # to hang a cron off of (see Documentation/plugin-api-findings.md).
    "vision_screenshot_dir": "",
    "vision_screenshot_retention_hours": 24,
    "vision_screenshot_max_bytes": 200 * 1024 * 1024,
    # Force-disable OCR even if pytesseract+tesseract are ever installed
    # (neither is present on this gateway today; the fallback degrades to a
    # layout-only description either way — see vision.py's try_ocr()).
    "vision_ocr_enabled": True,
    # Audit log rotation threshold.
    "audit_max_bytes": 25 * 1024 * 1024,
    "audit_keep": 5,
    # M2 approval queue (plan §3.4 / hermes_plugin/approvals.py). Hermes
    # v0.19.1 has no register_approval_transport (Documentation/
    # plugin-api-findings.md), so this bridge owns the whole request/response
    # cycle including its own timeouts -- never default-allow on any of these.
    #
    # How long a pending 'request'-mode approval blocks the calling gateway
    # worker thread before defaulting to deny.
    "approval_ttl_seconds": 120,
    # How long to wait for the extension to ack approval.request (the
    # "queued": true result) before treating an unreachable/slow device as an
    # immediate denial rather than blocking the tool call for the full TTL.
    "approval_delivery_timeout_seconds": 10,
    # How long a 'session' scope choice keeps auto-approving the same
    # device+session+origin+capability without re-asking. There is no
    # gateway-startup or reliable session-boundary hook to expire this
    # exactly when the Hermes session ends (plugin-api-findings.md), so it is
    # bounded by a generous TTL instead of lasting forever.
    "approval_session_grant_ttl_hours": 12,
    # G0.9 operator kill switch: `browser_bridge.powers.<capability>: false`
    # in config.yaml disables that capability fleet-wide, ANDed in
    # `tools.py::_authorize` with the origin grant / per-capability grant /
    # live approval decision -- none of those can override an operator "no".
    # A nested dict (not N flat `powers_<capability>` keys): config.yaml
    # naturally nests `browser_bridge.powers.evaluate: false`, and it keeps
    # every future capability name out of this top-level dict's own
    # namespace instead of requiring a new DEFAULTS key per capability. Empty
    # by default -- this switch only ever SUBTRACTS from what a device's own
    # grants would otherwise allow, so an operator who never touches it gets
    # today's behaviour unchanged. `_authorize` treats anything other than
    # the literal `False` (missing key, `True`, a typo'd string, ...) as
    # enabled -- the inverse of state.py's `_kind_enabled` fail-CLOSED
    # reading, because this default is fail-OPEN by design (see above) and a
    # malformed entry must not silently take a capability away that the
    # operator never asked to disable.
    "powers": {},
    # silentfetch.md SF4/SF5: browser_bridge_silent_fetch — a headless fetch
    # lane that runs from a hidden worker tab with no attach, no lease, no
    # tab id. Nested (like `powers` above), not N flat `silent_fetch_<key>`
    # top-level keys, so config.yaml can set `browser_bridge.silent_fetch.
    # rate_per_s: 5` without repeating every other key — see `load()`'s
    # per-key dict merge below, which is what makes a PARTIAL nested block
    # keep every default this dict doesn't mention.
    "silent_fetch": {
        "enabled": True,
        # The user's 2026-09-27 decision (silentfetch.md §1): true means an origin
        # whose EFFECTIVE grant mode is 'full' is zero-touch for silent
        # fetch too, with no live approval. false means a 'full' origin
        # still asks once per Hermes session, same as 'request'. Either way
        # an explicit per-origin popup override (off/ask/always) always
        # wins over this — see silent_grants.authorize_silent_fetch.
        "full_implies_silent": True,
        # Extension-side worker-pool ceiling (SF1), sent to the device on
        # hello so it never launches more hidden background tabs than this.
        "max_workers": 5,
        "worker_ttl_s": 1800,
        # Silent Fetch rev 3 §D: default worker-launch (bootstrap) timeout in
        # ms -- was a fixed 20000ms module constant in the extension with no
        # way to raise it; a live failure against a heavy chain (Tesla's
        # Akamai interstitial -> same-origin redirect -> geolocation/cookie
        # JS) took ~18s against that old 20s cap with no margin. Sent to the
        # device on hello/heartbeat (bootstrap_timeout_ms) next to
        # max_workers/worker_ttl_s; a per-call silent.fetch
        # bootstrap_timeout_ms param overrides it for one launch only. Both
        # paths clamp to [5000,120000] -- see silent_grants.
        "bootstrap_timeout_ms": 30000,
        # Token-bucket rate guard (SF4.4), per (device, origin).
        "rate_per_s": 2,
        "burst": 10,
        # Hard ceiling on `max_bytes` a caller may request (SF5) regardless
        # of what it asks for — distinct from browser_bridge_fetch's own
        # MAX_FETCH_MAX_BYTES, since silent fetch's chunked-frame transport
        # (silentfetch.md §1) tolerates a larger capture. Silent Fetch rev 2
        # §2: this is now the SINGLE SOURCE OF TRUTH for every ceiling this
        # feature enforces (the relay's per-request chunk-reassembly ceiling,
        # the tool's own max_bytes clamp, and the cap the extension is told
        # to honour on hello/heartbeat) — raise it here and every one of
        # those follows with no code change, up to SILENT_FETCH_SANITY_MAX_BYTES.
        # Raised from 16 MiB to 100 MiB after the pricing sweep (a bulk-fetch
        # workload) hit the old ceiling.
        "max_bytes_cap": 100 * 1024 * 1024,
        # Global in-flight ceiling across every concurrent silent.fetch chunk
        # stream on this gateway process (silent_grants.silent_fetch_config's
        # own default, when this is left unset: max_bytes_cap + one frame --
        # room for one max-size body in flight at a time, sized for this
        # host's ~1.6 GB free with the gateway's own ~560 MB RSS already
        # accounted for). Set explicitly here only to raise it past that
        # default; left absent (None) it is derived at read time from
        # whatever max_bytes_cap actually resolves to, which is preferable
        # to freezing today's default cap into this dict.
        "max_buffered_bytes": None,
        # SF5.4 response cache (`cache_ttl_s` per call): retention floor and
        # total size ceiling, pruned oldest-first on write.
        "cache_retention_hours": 24,
        "cache_max_bytes": 500 * 1024 * 1024,
    },
}

STATE_DIR = Path.home() / ".hermes" / "browser_bridge"
DB_PATH = STATE_DIR / "state.db"
AUDIT_PATH = STATE_DIR / "audit.jsonl"


def load() -> Dict[str, Any]:
    """Return the effective ``browser_bridge`` config merged over DEFAULTS.

    A top-level key whose DEFAULT value is itself a dict (``powers``,
    ``silent_fetch``) is merged one level deep rather than replaced outright:
    a user block that only sets ``silent_fetch.rate_per_s`` must not lose
    every other ``silent_fetch.*`` default (``enabled``, ``max_workers``, …)
    the way a flat ``cfg[key] = value`` overwrite would. Every other key
    (a plain scalar) keeps the simple overwrite behaviour unchanged. Only one
    level deep — none of today's nested dicts (``powers``' per-capability
    booleans, ``silent_fetch``'s own keys) nest a dict of their own, so a
    deeper recursive merge would be untested complexity with nothing to
    exercise it.
    """
    cfg = dict(DEFAULTS)
    try:
        from hermes_cli.config import load_config  # type: ignore

        raw = load_config() or {}
        section = raw.get("browser_bridge")
        if isinstance(section, dict):
            for key, value in section.items():
                if value is None:
                    continue
                default_value = DEFAULTS.get(key)
                if isinstance(default_value, dict) and isinstance(value, dict):
                    merged = dict(default_value)
                    merged.update(value)
                    cfg[key] = merged
                else:
                    cfg[key] = value
    except Exception:
        # Config unreadable (or running outside Hermes, e.g. unit tests):
        # defaults are safe — the relay still binds and pairing still works.
        pass
    return cfg


def _positive_int(value: Any, default: int) -> int:
    """Coerce to a positive int, falling back to ``default`` for anything
    that isn't one (wrong type, zero, negative) — same fail-safe-to-default
    discipline silent_grants.py's own numeric readers use, kept local here
    since this is the only scalar config value that needs it so far."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def effective_device_alive_after_seconds(cfg: Dict[str, Any] | None = None) -> int:
    """devices.md DV1.1: the effective ``device_alive_after_seconds``, clamped
    to [2 * the heartbeat interval, device_offline_after_seconds].

    The floor stops a config typo (or an operator tightening it "to be safe")
    from making a device flap alive/stale across ordinary heartbeat jitter —
    below 2x the interval, a single delayed beat alone could trip it. The
    ceiling keeps "stale" a real warning that precedes the DV1.3 sweep
    closing the socket, rather than a state a device could sit in forever
    without ever reaching "offline". If device_offline_after_seconds is
    itself misconfigured below the heartbeat floor, the floor wins (a
    device can never be required to go stale before it's even allowed to
    heartbeat) — see ``ceiling`` below.
    """
    cfg = cfg or load()
    from . import protocol  # local: protocol has no imports back into config, no cycle risk

    heartbeat_floor = 2 * (protocol.HEARTBEAT_INTERVAL_MS // 1000)
    offline_after = _positive_int(
        cfg.get("device_offline_after_seconds"), DEFAULTS["device_offline_after_seconds"]
    )
    ceiling = max(offline_after, heartbeat_floor)
    raw = cfg.get("device_alive_after_seconds", DEFAULTS["device_alive_after_seconds"])
    value = _positive_int(raw, DEFAULTS["device_alive_after_seconds"])
    return min(max(value, heartbeat_floor), ceiling)


def effective_device_offline_after_seconds(cfg: Dict[str, Any] | None = None) -> int:
    """``device_offline_after_seconds``, sanity-coerced the same way
    ``effective_device_alive_after_seconds`` coerces its own value — the
    public read path for it, so a caller (the DV1.3 sweep) never has to
    reach into a raw config dict and re-derive the fallback-on-garbage rule
    itself."""
    cfg = cfg or load()
    return _positive_int(cfg.get("device_offline_after_seconds"), DEFAULTS["device_offline_after_seconds"])


def ws_url(cfg: Dict[str, Any] | None = None, host_hint: str = "") -> str:
    """User-facing WebSocket URL printed by ``hermes browser-bridge pair``."""
    cfg = cfg or load()
    host = host_hint or _primary_lan_address(str(cfg["host"]))
    return f"ws://{host}:{cfg['port']}{cfg['path']}"


def _primary_lan_address(bind_host: str) -> str:
    """Resolve a dialable address for a wildcard bind.

    Extensions cannot dial 0.0.0.0, so pairing output needs the real LAN IP.
    Uses a UDP socket with no traffic sent — no DNS, no packets, no delay.
    """
    if bind_host not in ("0.0.0.0", "::", ""):
        return bind_host
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.168.1.1", 1))
        return sock.getsockname()[0]
    except Exception:
        return socket.gethostbyname(socket.gethostname())
    finally:
        sock.close()
