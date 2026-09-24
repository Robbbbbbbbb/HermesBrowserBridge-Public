"""Configuration access for the browser bridge.

All settings live in Hermes' own ``config.yaml`` under ``browser_bridge.*``.
``.env`` is secrets-only per repo policy, and no ``HERMES_*`` env vars are
introduced. Defaults here are the shipped behaviour; a missing config file or
an unreadable key degrades to the default rather than failing plugin load.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

VERSION = "0.1.0"

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "host": "0.0.0.0",
    "port": 8765,
    "path": "/bridge",
    # Minutes a printed pairing code stays usable.
    "pair_code_ttl_minutes": 10,
    # Seconds without a heartbeat before a device is considered offline.
    "device_offline_after_seconds": 90,
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
}

STATE_DIR = Path.home() / ".hermes" / "browser_bridge"
DB_PATH = STATE_DIR / "state.db"
AUDIT_PATH = STATE_DIR / "audit.jsonl"


def load() -> Dict[str, Any]:
    """Return the effective ``browser_bridge`` config merged over DEFAULTS."""
    cfg = dict(DEFAULTS)
    try:
        from hermes_cli.config import load_config  # type: ignore

        raw = load_config() or {}
        section = raw.get("browser_bridge")
        if isinstance(section, dict):
            for key, value in section.items():
                if value is not None:
                    cfg[key] = value
    except Exception:
        # Config unreadable (or running outside Hermes, e.g. unit tests):
        # defaults are safe — the relay still binds and pairing still works.
        pass
    return cfg


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
