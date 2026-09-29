"""ep2-silent-evaluate.md: the gateway tool ``browser_bridge_silent_evaluate``.

Runs a JS expression in the MAIN world of the silent pool's hidden worker tab
for a URL's origin (extension side: ``silent.evaluate``). No attach, no lease,
no visible tab, no ``tab_id``. This module owns the gate chain, the tool
schema, result shaping, the ``spill_to_path`` file sink, and the audit lines.

Gate chain, in this order (each refusal is audited as ``silent_eval_refused``):

    1  param validation (url http/https, expression <= 4096 chars, no tab_id/tab,
       world only "main", spill_to_path is a plain file name)
    2  silent_evaluate.enabled AND silent_fetch.enabled
    3  operator kill switch: powers.silent_evaluate or powers.evaluate is false
    4  device power allowEvaluate (same toggle as interactive evaluate)
    5  silent_grants.ssrf_guard
    6  origin grant: the SAME resolution silent.fetch uses
       (silent_grants.resolve_silent_origin): grant off / Background requests off
       refuse; otherwise "zero_touch" or "ask"
    7  approval, capability ``silent_evaluate``: the device's evaluateApproval
       policy applies ONLY to a zero-touch resolution (the floor). An "ask"
       resolution prompts every call, whatever the policy says.
    8  silent_grants.silent_rate_guard: the SAME bucket as silent.fetch
    9  silent_grants.origin_slot: shared with silent.fetch
    10 relay.call("silent.evaluate")
    11 gateway redaction re-check of the result text
    12 inline shaping or spill; audit

The rate guard counts bridge calls only. An expression that fans out into
hundreds of page-level fetch() calls is one call as far as the guard is
concerned; that is the point of batching inside one expression.

``spill_to_path`` is a file NAME, never a path: an agent-supplied absolute path
would be a write-anywhere primitive on the gateway host fed by page-controlled
data. The name is written under ``silent_evaluate.spill_dir``.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import audit, config, protocol, relay as relay_mod, silent_grants
from . import origins as origins_mod
from . import tools as tools_mod
from .evaluate import MAX_EXPRESSION_CHARS, _redact_expression_secrets, _redact_for_display
from . import silent_fetch as silent_fetch_mod
from .silent_fetch import _atomic_write

logger = logging.getLogger(__name__)

TOOLSET = tools_mod.TOOLSET
CAPABILITY = "silent_evaluate"

DEFAULT_TIMEOUT_MS = 30000
DEFAULT_MAX_RETURN_BYTES = 32768
MAX_RETURN_BYTES_CAP = 1024 * 1024
PREVIEW_CHARS = 2000
# Relay wait = timeout_ms + bootstrap budget + this margin (worker attach and
# chunk transfer happen inside it).
RELAY_MARGIN_S = 15.0

DEFAULT_SPILL_DIRNAME = "eval_spill"
_SPILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


# -- config ---------------------------------------------------------------------


def silent_evaluate_config() -> Dict[str, Any]:
    """Effective ``silent_evaluate.*`` settings; every value is clamped, and a
    value of the wrong type falls back to its default."""
    defaults = config.DEFAULTS["silent_evaluate"]
    cfg = config.load().get("silent_evaluate")
    if not isinstance(cfg, dict):
        cfg = {}

    def _int(value: Any, default: int) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return int(default)
        return int(value)

    max_timeout_ms = min(
        config.SILENT_EVAL_SANITY_MAX_TIMEOUT_MS,
        max(config.SILENT_EVAL_MIN_TIMEOUT_MS, _int(cfg.get("max_timeout_ms"), defaults["max_timeout_ms"])),
    )
    fetch_cap = int(silent_grants.silent_fetch_config()["max_bytes_cap"])
    max_spill_bytes = min(
        fetch_cap,
        max(config.SILENT_EVAL_MIN_SPILL_BYTES, _int(cfg.get("max_spill_bytes"), defaults["max_spill_bytes"])),
    )

    spill_dir = config.STATE_DIR / DEFAULT_SPILL_DIRNAME
    raw_dir = cfg.get("spill_dir")
    if isinstance(raw_dir, str) and raw_dir.strip():
        candidate = Path(raw_dir.strip()).expanduser()
        if candidate.is_absolute():
            spill_dir = candidate

    enabled = cfg.get("enabled", defaults["enabled"])
    return {
        "enabled": enabled if isinstance(enabled, bool) else bool(defaults["enabled"]),
        "max_timeout_ms": max_timeout_ms,
        "max_spill_bytes": max_spill_bytes,
        "spill_dir": spill_dir,
    }


# -- spill sink -------------------------------------------------------------------


def validate_spill_name(name: Any) -> Optional[str]:
    """None when ``name`` is an acceptable spill file name, else the reason."""
    if not isinstance(name, str) or not name:
        return "spill_to_path must be a non-empty string file name"
    if ".." in name:
        return "spill_to_path must not contain '..'"
    if not _SPILL_NAME_RE.fullmatch(name):
        return (
            "spill_to_path is a file NAME, not a path: 1-128 characters of letters, digits, '.', '_' or '-', "
            "starting with a letter or digit (no '/', no leading dot). It is written under the gateway's "
            "silent_evaluate.spill_dir"
        )
    return None


def _check_spill_target(target: Path) -> Optional[str]:
    """None when ``target`` may be written: it does not exist, or is a regular
    file. A symlink is refused (an agent able to plant one must not be able to
    aim this tool at it), as is anything else that is not a regular file."""
    if os.path.islink(target):
        return f"{target.name!r} in the spill directory is a symlink; refusing to write through or over it"
    if os.path.lexists(target) and not target.is_file():
        return f"{target.name!r} in the spill directory exists and is not a regular file"
    return None


def _ensure_spill_dir(spill_dir: Path) -> Path:
    spill_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(spill_dir, 0o700)
    except OSError:
        logger.warning("browser_bridge: could not chmod spill dir to 0700", exc_info=True)
    return spill_dir


# -- schema -----------------------------------------------------------------------

SILENT_EVALUATE_SCHEMA = {
    "name": "browser_bridge_silent_evaluate",
    "description": (
        "Run a JavaScript expression in a HIDDEN background worker tab on `url`'s origin (the same worker "
        "browser_bridge_silent_fetch uses), in the page's own MAIN world, with the user's real session "
        "and cookies: no attached tab, no visible tab, no tab id. This is the batching primitive: put the "
        "whole loop (hundreds of fetch() calls, parsing, aggregation) inside ONE expression and return a "
        "small summary, instead of one tool call per URL. The rate guard is shared with "
        "browser_bridge_silent_fetch and counts bridge calls only, so page-level fetch() calls inside the "
        "expression are not counted -- that is exactly why batching belongs here. It needs the same "
        "'Background requests' grant as silent fetch AND the device's Run JavaScript setting "
        "(allowEvaluate). Approval follows the device's evaluateApproval policy only on an origin whose "
        "background access is zero-touch: an origin whose Background requests is 'Always' (or that is "
        "unconfigured under the defaults) needs NO prompt at the default 'Always allow' policy, whatever its "
        "ordinary access mode; an origin set to 'ask' prompts every call. The expression may "
        "be at most 4096 characters; it is shown verbatim (secret-shaped spans masked) in any prompt and "
        "in the audit log. Results are serialized by value (a string as-is, anything else JSON) and "
        "redacted for card/SSN/password/token shapes; you get text back, not a live handle. Inline "
        "results are capped at max_return_bytes (default 32768, hard cap 1 MiB) with truncated:true; for "
        "bulk output pass spill_to_path (a plain file NAME, e.g. 'sweep-2026-09-28.json'): the whole "
        "result is written under the gateway's spill directory and you get back {path, bytes, sha256, "
        "preview} instead -- read the file with execute_code. Calls are single-flight per origin worker: "
        "a call that finds the origin's worker busy (a fetch or evaluation still running) is refused at "
        "once with SILENT_WORKER_BUSY, never queued: wait and retry. If the "
        "expression outlives timeout_ms (default 30000, up to the gateway's silent_evaluate.max_timeout_ms, at most 600000) "
        "or opens a dialog, the call fails with SILENT_EVAL_TIMEOUT and the worker tab is recycled. "
        "Never call alert/confirm/prompt. While an evaluation runs, Chrome may show its 'is debugging "
        "this browser' banner on the hidden worker's window."
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
                "description": (
                    "Absolute http(s) URL. Its origin picks the worker; when no idle worker exists for that "
                    "origin, the worker navigates here first and the expression runs on that page."
                ),
            },
            "expression": {
                "type": "string",
                "description": f"JS expression, run verbatim. Capped at {MAX_EXPRESSION_CHARS} characters. Wrap loops in an async IIFE.",
            },
            "await_promise": {
                "type": "boolean",
                "description": "If the value is a Promise, wait for it to settle. Default true.",
            },
            "timeout_ms": {
                "type": "integer",
                "description": f"Wall-clock budget for the evaluation. Default {DEFAULT_TIMEOUT_MS}; clamped to [{config.SILENT_EVAL_MIN_TIMEOUT_MS}, silent_evaluate.max_timeout_ms].",
            },
            "max_return_bytes": {
                "type": "integer",
                "description": f"Inline result budget in bytes. Default {DEFAULT_MAX_RETURN_BYTES}, clamped to [1, {MAX_RETURN_BYTES_CAP}]. Ignored with spill_to_path.",
            },
            "spill_to_path": {
                "type": "string",
                "description": (
                    "A file NAME (letters, digits, '.', '_', '-'; no slashes, no '..', no leading dot). The whole "
                    "redacted result is written there under the gateway's spill directory; the tool returns "
                    "{path, bytes, sha256, preview} only."
                ),
            },
            "bootstrap_timeout_ms": {
                "type": "integer",
                "description": (
                    "Overrides the worker launch/bootstrap budget for this call only, same meaning and clamp as "
                    "browser_bridge_silent_fetch's."
                ),
            },
        },
        "required": ["url", "expression"],
    },
}


# -- audit helpers ---------------------------------------------------------------


def _url_no_query(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}{parts.path}"[:512]


class _Call:
    """Per-call context shared by the refusal/audit helpers."""

    def __init__(self, device_id: str, url: str, expression: str) -> None:
        self.device_id = device_id
        self.url = url
        self.origin = ""
        self.expression = expression
        self.display_expression = ""
        self.start = time.monotonic()

    def refuse(self, reason: str, code: int, reason_class: str, **extra: Any) -> str:
        audit.record(
            "silent_eval_refused", device=self.device_id, origin=self.origin, url=_url_no_query(self.url),
            expression=self.display_expression, reason_class=reason_class,
        )
        fields: Dict[str, Any] = {"device_id": self.device_id, "reason_class": reason_class}
        if self.origin:
            fields["origin"] = self.origin
        fields.update(extra)
        return tools_mod._err(reason, code=code, **fields)


# -- approval ----------------------------------------------------------------------


def _approve(
    call: _Call, resolution: str, holder: str, summary: str, detail: str,
) -> Optional[Tuple[str, int, str]]:
    """Gate 7. Returns None to proceed, or ``(reason, code, reason_class)``.

    The device's evaluateApproval policy is consulted with mode "full" only for
    a zero-touch origin resolution; an "ask" resolution passes "request", which
    ``approvals.check_policy_grant`` never short-circuits, so it always prompts.
    """
    device_id, origin = call.device_id, call.origin
    try:
        from . import approvals  # noqa: PLC0415 - optional sibling module, same seam _authorize uses
    except ImportError:
        audit.record(
            "grant_check", device=device_id, origin=origin, capability=CAPABILITY, mode=resolution,
            decision="deny", reason="approvals module unavailable",
        )
        return (
            f"the approval queue is not loaded on this gateway, so {CAPABILITY} is refused rather than "
            f"silently allowed", protocol.SILENT_ORIGIN_NOT_GRANTED, "approval_transport_missing",
        )

    policy_mode = "full" if resolution == "zero_touch" else "request"
    relay = relay_mod.get_relay()
    connection_id = relay.connection_identity(device_id) if relay is not None else ""

    policy_decision = approvals.check_policy_grant(device_id, origin, CAPABILITY, policy_mode, connection_id)
    if policy_decision is not None:
        audit.record(
            "grant_check", device=device_id, origin=origin, capability=CAPABILITY, mode=policy_mode,
            decision="allow", scope=policy_decision.scope, reason=policy_decision.reason,
        )
        return None

    safe_detail, _ = tools_mod._gateway_redact(detail, device_id)
    try:
        decision = approvals.require(device_id, origin, CAPABILITY, summary, holder, detail=safe_detail)
    except Exception as exc:  # the approval transport itself failed: never silently allow
        audit.record(
            "grant_check", device=device_id, origin=origin, capability=CAPABILITY, mode=policy_mode,
            decision="deny", reason=f"approvals.require raised {type(exc).__name__}",
        )
        return (
            f"approval request failed ({type(exc).__name__}: {exc}); {CAPABILITY} is refused, not silently allowed",
            protocol.INTERNAL_ERROR, "approval_error",
        )

    allowed = bool(getattr(decision, "allowed", False))
    scope = str(getattr(decision, "scope", "") or "")
    decision_reason = str(getattr(decision, "reason", "") or "")
    audit.record(
        "grant_check", device=device_id, origin=origin, capability=CAPABILITY, mode=policy_mode,
        decision="allow" if allowed else "deny", scope=scope, reason=decision_reason,
    )
    if allowed:
        # An allow under ask_per_session on a zero-touch origin creates the
        # in-memory, connection-scoped grant. Re-read the connection first: a
        # drop during the prompt already cleared grants, so one recorded now
        # under the old id would be an orphan.
        if resolution == "zero_touch" and approvals.approval_policy(device_id, CAPABILITY) == "ask_per_session":
            still_connected = relay is not None and relay.connection_identity(device_id) == connection_id
            if still_connected:
                approvals.grant_policy_session(device_id, connection_id, origin, CAPABILITY)
        return None
    if scope == "timeout":
        return (
            decision_reason or f"the user did not respond to the {CAPABILITY} approval request in time",
            protocol.TIMEOUT, "timeout",
        )
    return decision_reason or f"the user denied {CAPABILITY} for {origin!r}", protocol.APPROVAL_DENIED, "approval_denied"


# -- shaping -------------------------------------------------------------------------


def _utf8_head(text: str, max_bytes: int) -> Tuple[str, bool]:
    """``text`` cut to at most ``max_bytes`` of UTF-8 without splitting a character."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    return raw[:max_bytes].decode("utf-8", errors="ignore"), True


def _int_or_none(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _result_text(result: Dict[str, Any]) -> str:
    """The serialized result text: reassembled chunks if the extension spilled,
    else the inline value (a string as-is, anything else as JSON)."""
    body = result.get("body_bytes")
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8", errors="replace")
    value = result.get("result")
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _redact_result_text(text: str, device_id: str) -> Tuple[str, int]:
    """Gateway re-check for result/exception text: ``_gateway_redact`` (card,
    SSN, password, email/phone per the device's policy) PLUS the unconditional
    JWT / Bearer / API-key / URL-secret-param pass the extension's
    ``redactSecrets`` applies (evaluate.py's ``_redact_expression_secrets``).
    Returns (text, count of new hits)."""
    text, hits = tools_mod._gateway_redact(text, device_id)
    marker = "[redacted:token]"
    before = text.count(marker)
    text = _redact_expression_secrets(text)
    hits += max(0, text.count(marker) - before)
    return text, hits


# -- handler -------------------------------------------------------------------------


def handle_silent_evaluate(args: Dict[str, Any], **kwargs: Any) -> str:
    device_id, err = tools_mod._resolve_device(args, kwargs)
    if err:
        return err

    expression = str(args.get("expression") or "")
    url = str(args.get("url") or "").strip()
    call = _Call(device_id, url, expression)
    if expression:
        call.display_expression = _redact_for_display(expression, device_id)

    # -- 1. param validation ---------------------------------------------------
    if args.get("tab_id") is not None or args.get("tab"):
        return call.refuse(
            "browser_bridge_silent_evaluate never takes tab_id or tab -- origin -> worker resolution is entirely "
            "internal (the extension's hidden worker pool). Use browser_bridge_evaluate against an attached tab "
            "if you need tab-scoped behaviour.", protocol.INVALID_PARAMS, "invalid_params",
        )
    world = args.get("world")
    if world is not None and world != "main":
        return call.refuse(
            "world must be 'main'; no other world is available", protocol.EVAL_WORLD_UNSUPPORTED, "invalid_params",
        )
    if not url:
        return call.refuse("url is required", protocol.INVALID_PARAMS, "invalid_params")
    try:
        scheme = (urlsplit(url).scheme or "").lower()
    except ValueError:
        scheme = ""
    if scheme not in ("http", "https"):
        return call.refuse("url must be an absolute http or https URL", protocol.INVALID_PARAMS, "invalid_params")
    origin = origins_mod.canonicalize_origin(tools_mod._origin_of(url))
    if not origin:
        return call.refuse(f"url is not a valid absolute URL: {url!r}", protocol.INVALID_PARAMS, "invalid_params")
    call.origin = origin
    if not expression:
        return call.refuse("expression is required", protocol.INVALID_PARAMS, "invalid_params")
    if len(expression) > MAX_EXPRESSION_CHARS:
        return call.refuse(
            f"expression is {len(expression)} chars, over the {MAX_EXPRESSION_CHARS}-char cap",
            protocol.EVAL_EXPRESSION_TOO_LARGE, "invalid_params",
        )

    await_promise = args.get("await_promise")
    if await_promise is not None and not isinstance(await_promise, bool):
        return call.refuse("await_promise must be true or false", protocol.INVALID_PARAMS, "invalid_params")

    cfg = silent_evaluate_config()

    def _numeric(name: str, default: int) -> Tuple[Optional[int], Optional[str]]:
        raw = args.get(name)
        if raw is None:
            return default, None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None, f"{name} must be a number"
        return int(raw), None

    timeout_ms, bad = _numeric("timeout_ms", DEFAULT_TIMEOUT_MS)
    if bad:
        return call.refuse(bad, protocol.INVALID_PARAMS, "invalid_params")
    assert timeout_ms is not None
    timeout_ms = max(config.SILENT_EVAL_MIN_TIMEOUT_MS, min(timeout_ms, cfg["max_timeout_ms"]))

    max_return_bytes, bad = _numeric("max_return_bytes", DEFAULT_MAX_RETURN_BYTES)
    if bad:
        return call.refuse(bad, protocol.INVALID_PARAMS, "invalid_params")
    assert max_return_bytes is not None
    max_return_bytes = max(1, min(max_return_bytes, MAX_RETURN_BYTES_CAP))

    sf_cfg = silent_grants.silent_fetch_config()
    bootstrap_timeout_ms: Optional[int] = None
    if args.get("bootstrap_timeout_ms") is not None:
        bootstrap_timeout_ms = silent_grants.clamp_bootstrap_timeout_ms(
            args["bootstrap_timeout_ms"], sf_cfg["bootstrap_timeout_ms"]
        )

    spill_name = args.get("spill_to_path")
    spill_target: Optional[Path] = None
    if spill_name is not None:
        bad_name = validate_spill_name(spill_name)
        if bad_name:
            return call.refuse(bad_name, protocol.INVALID_PARAMS, "invalid_spill_name")
        spill_target = cfg["spill_dir"] / spill_name
        bad_target = _check_spill_target(spill_target)
        if bad_target:
            return call.refuse(bad_target, protocol.INVALID_PARAMS, "spill_target_refused")

    # -- 2. enabled ---------------------------------------------------------------
    if not cfg["enabled"] or not sf_cfg["enabled"]:
        which = "silent_evaluate.enabled" if not cfg["enabled"] else "silent_fetch.enabled"
        return call.refuse(
            f"browser_bridge_silent_evaluate is disabled on this gateway ({which}: false in config.yaml)",
            protocol.SILENT_ORIGIN_NOT_GRANTED, "disabled",
        )

    # -- 3. operator kill switch ----------------------------------------------------
    powers = config.load().get("powers", {})
    if powers.get("silent_evaluate") is False or powers.get("evaluate") is False:
        audit.record(
            "grant_check", device=device_id, origin=origin, capability=CAPABILITY, mode="operator_disabled",
            decision="deny", reason="operator kill switch (powers.silent_evaluate or powers.evaluate is false)",
        )
        return call.refuse(
            "browser_bridge_silent_evaluate is turned off by the gateway operator "
            "(browser_bridge.powers.silent_evaluate or powers.evaluate is false in config.yaml)",
            protocol.GRANT_DENIED, "operator_disabled",
        )

    # -- 4. device power --------------------------------------------------------------
    power_denial = tools_mod._device_power_denial(device_id, CAPABILITY)
    if power_denial is not None:
        reason, code = power_denial
        return call.refuse(reason, code, "device_power_off")

    # -- 5. SSRF ---------------------------------------------------------------------
    guard = silent_grants.ssrf_guard(url, device_id)
    if guard is not None:
        reason, code, audit_class = guard
        return call.refuse(reason, code, audit_class)

    holder, err = tools_mod._resolve_session(device_id, kwargs)
    if err:
        return err

    # -- 6. origin grant (same resolution as silent.fetch) -----------------------------
    resolution = silent_grants.resolve_silent_origin(device_id, origin)
    if resolution in ("grant_off", "silent_off"):
        why = (
            "the origin's access mode is 'off'" if resolution == "grant_off"
            else "the origin's 'Background requests' setting is 'off'"
        )
        return call.refuse(
            f"{origin!r} is not granted for background evaluation: {why}. Ask the user to set this origin's "
            f"'Background requests' popup control to 'Always allow' (or 'Ask first').",
            protocol.SILENT_ORIGIN_NOT_GRANTED, "origin_not_granted",
        )

    # -- 7. approval ---------------------------------------------------------------------
    summary = f"run JS in a hidden background tab at {origin}:\n{call.display_expression}"
    detail = json.dumps({"expression": call.display_expression, "timeout_ms": timeout_ms, "url": _url_no_query(url)})
    denial = _approve(call, resolution, holder, summary, detail)
    if denial is not None:
        reason, code, reason_class = denial
        return call.refuse(reason, code, reason_class)

    # -- 8. rate guard (shared bucket with silent.fetch) -------------------------------
    rl = silent_grants.silent_rate_guard(device_id, origin)
    if rl is not None:
        reason, code, retry_after = rl
        audit.record(
            "silent_eval_refused", device=device_id, origin=origin, url=_url_no_query(url),
            expression=call.display_expression, reason_class="rate_limited",
        )
        return tools_mod._err(reason, code=code, device_id=device_id, origin=origin, retry_after_s=retry_after)

    wire_params: Dict[str, Any] = {
        "url": url,
        "expression": expression,
        "world": "main",
        "timeout_ms": timeout_ms,
        "await_promise": True if await_promise is None else await_promise,
        "max_return_bytes": max_return_bytes,
        "spill": spill_target is not None,
    }
    if spill_target is not None:
        wire_params["max_spill_bytes"] = cfg["max_spill_bytes"]
    if bootstrap_timeout_ms is not None:
        wire_params["bootstrap_timeout_ms"] = bootstrap_timeout_ms

    effective_bootstrap_ms = bootstrap_timeout_ms if bootstrap_timeout_ms is not None else sf_cfg["bootstrap_timeout_ms"]
    relay_timeout_s = (timeout_ms + effective_bootstrap_ms) / 1000.0 + RELAY_MARGIN_S

    def _audit_call(outcome: str, **fields: Any) -> None:
        audit.record(
            "silent_eval", device=device_id, holder=holder, origin=origin, url=_url_no_query(url),
            expression=call.display_expression, timeout_ms=timeout_ms, outcome=outcome,
            elapsed_ms=round((time.monotonic() - call.start) * 1000.0, 1), **fields,
        )

    relay = relay_mod.get_relay()
    relay_start = time.monotonic()
    # -- 9/10. per-origin slot, then the relay call ---------------------------------------
    in_relay_call = False
    try:
        with silent_grants.eval_origin_slot(device_id, origin):
            in_relay_call = True
            relay_start = time.monotonic()
            result = relay.call(device_id, "silent.evaluate", wire_params, timeout=relay_timeout_s)
    except silent_grants.SilentWorkerBusy as exc:
        # Single-flight per origin worker: refused at once, never queued, and
        # the relay is not called.
        return call.refuse(
            f"{exc}; wait for it to finish and retry rather than firing calls in parallel",
            protocol.SILENT_WORKER_BUSY, "worker_busy",
        )
    except TimeoutError as exc:
        if not in_relay_call:
            raise
        relay_ms = round((time.monotonic() - relay_start) * 1000.0, 1)
        _audit_call("error", error_code=protocol.TIMEOUT, relay_ms=relay_ms)
        return silent_fetch_mod.reclaim_result(
            tools_mod._err_dict(str(exc), code=protocol.TIMEOUT, device_id=device_id, origin=origin),
            device_id, origin, "relay_timeout", "silent.evaluate",
        )
    except relay_mod.BridgeError as exc:
        relay_ms = round((time.monotonic() - relay_start) * 1000.0, 1)
        if exc.code in (protocol.METHOD_NOT_FOUND, protocol.UNSUPPORTED_METHOD):
            _audit_call("error", error_code=exc.code, reason_class="extension_outdated", relay_ms=relay_ms)
            return tools_mod._err(
                "the connected extension build does not support background evaluation (silent.evaluate) -- "
                "reload the extension in chrome://extensions and reconnect, then retry",
                code=exc.code, device_id=device_id, origin=origin,
            )
        _audit_call("error", error_code=exc.code, relay_ms=relay_ms)
        return silent_fetch_mod.bridge_err_with_reclaim(exc, device_id, origin, "silent.evaluate")
    relay_ms = round((time.monotonic() - relay_start) * 1000.0, 1)

    # -- 11. honesty check + redaction re-check ------------------------------------------------
    body_bytes = result.get("body_bytes")
    reported_sha = result.get("sha256")
    if isinstance(body_bytes, (bytes, bytearray)) and reported_sha:
        actual_sha = hashlib.sha256(bytes(body_bytes)).hexdigest()
        if actual_sha.lower() != str(reported_sha).lower():
            _audit_call("error", error_code=protocol.INTERNAL_ERROR, reason_class="sha256_mismatch", relay_ms=relay_ms)
            return tools_mod._err(
                f"sha256 mismatch: the extension reported {reported_sha}, but the gateway reassembled "
                f"{actual_sha} from the wire -- refusing to trust this result",
                code=protocol.INTERNAL_ERROR, device_id=device_id, origin=origin,
            )

    text = _result_text(result)
    result["body_bytes"] = None
    text, gateway_hits = _redact_result_text(text, device_id)
    if gateway_hits:
        audit.record(
            "redaction_gateway_catch", device=device_id, origin=origin, hits=gateway_hits, capability=CAPABILITY,
        )
    redactions = (_int_or_none(result.get("redactions")) or 0) + gateway_hits

    status = "error" if result.get("status") == "error" else "ok"
    ext_timing = result.get("timing") if isinstance(result.get("timing"), dict) else {}
    timing: Dict[str, Any] = {
        "total_ms": round((time.monotonic() - call.start) * 1000.0, 1),
        "relay_ms": relay_ms,
        "extension_ms": _int_or_none(ext_timing.get("extension_ms")),
    }
    for key in ("eval_ms", "attach_ms"):
        if _int_or_none(ext_timing.get(key)) is not None:
            timing[key] = ext_timing[key]

    payload: Dict[str, Any] = {
        "device_id": device_id,
        "origin": origin,
        "status": status,
        "result_type": result.get("result_type"),
        "redactions": redactions,
        "timing": timing,
        "diagnostics": result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {},
    }
    if isinstance(result.get("exception"), str):
        exception_text, exc_hits = _redact_result_text(result["exception"], device_id)
        payload["exception"] = exception_text
        redactions += exc_hits
        payload["redactions"] = redactions
    if isinstance(result.get("serialized"), str):
        payload["serialized"] = result["serialized"]

    reported_total = _int_or_none(result.get("total_bytes"))

    # -- 12a. spill -----------------------------------------------------------------------------
    if spill_target is not None and status == "ok":
        raw = text.encode("utf-8")
        truncated = bool(result.get("truncated"))
        if len(raw) > cfg["max_spill_bytes"]:
            raw = raw[: cfg["max_spill_bytes"]]
            text = raw.decode("utf-8", errors="ignore")
            raw = text.encode("utf-8")
            truncated = True
        # Re-check right before writing: the target may have changed while the
        # expression ran.
        bad_target = _check_spill_target(spill_target)
        if bad_target:
            _audit_call("error", error_code=protocol.INVALID_PARAMS, reason_class="spill_target_refused", relay_ms=relay_ms)
            return tools_mod._err(bad_target, code=protocol.INVALID_PARAMS, device_id=device_id, origin=origin)
        try:
            _atomic_write(_ensure_spill_dir(cfg["spill_dir"]) / spill_target.name, raw)
        except OSError as exc:
            logger.exception("browser_bridge: silent_evaluate spill write failed")
            _audit_call("error", error_code=protocol.INTERNAL_ERROR, reason_class="spill_write_failed", relay_ms=relay_ms)
            return tools_mod._err(
                f"could not write the spill file ({type(exc).__name__}); the result was not saved",
                code=protocol.INTERNAL_ERROR, device_id=device_id, origin=origin,
            )
        digest = hashlib.sha256(raw).hexdigest()
        payload.update({
            "path": str(spill_target),
            "bytes": len(raw),
            "sha256": digest,
            "truncated": truncated,
            "preview": text[:PREVIEW_CHARS],
        })
        _audit_call(
            status, result_bytes=len(raw), truncated=truncated, spilled=True, spill_path=str(spill_target),
            sha256=digest, redactions=redactions, timing=timing,
        )
        return tools_mod._ok(**payload)

    # -- 12b. inline ----------------------------------------------------------------------------
    inline_text, cut = _utf8_head(text, max_return_bytes)
    truncated = bool(result.get("truncated")) or cut
    total_bytes = reported_total if reported_total is not None else len(text.encode("utf-8"))
    payload.update({
        "result": inline_text,
        "truncated": truncated,
        "total_bytes": max(total_bytes, len(inline_text.encode("utf-8"))),
    })
    _audit_call(
        status, result_bytes=len(inline_text.encode("utf-8")), truncated=truncated, spilled=False,
        redactions=redactions, timing=timing,
    )
    return tools_mod._ok(**payload)


def register_silent_evaluate_tools(ctx) -> List[str]:
    """Entry point ``tools.register_tools`` imports under ``try/except
    ImportError``, like every sibling tool module."""
    ctx.register_tool(
        name=SILENT_EVALUATE_SCHEMA["name"],
        toolset=TOOLSET,
        schema=SILENT_EVALUATE_SCHEMA,
        handler=handle_silent_evaluate,
        check_fn=tools_mod.bridge_available,
        emoji="\U0001F47B",
    )
    return [SILENT_EVALUATE_SCHEMA["name"]]
