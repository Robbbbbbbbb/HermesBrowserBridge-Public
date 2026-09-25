"""``hermes browser-bridge`` CLI: pair, devices, revoke, logs, status, export, skills.

Registered through ``ctx.register_cli_command``; ``setup_cli`` receives the
argparse subparser for ``browser-bridge`` and adds its own sub-subcommands.

These commands run in a separate short-lived CLI process, not in the gateway,
so they talk to SQLite directly and never assume a live relay. ``revoke`` also
asks a running relay to drop the device, which is best-effort by design: the
token is already dead in the database either way.

Named ``browser-bridge``, not ``browser``: Hermes core (v0.21.x) ships its own
hardcoded ``hermes browser`` subparser (real-Chrome-profile helpers /
close-profile), registered after the dynamic plugin-CLI loop, and argparse
silently overwrites on a name collision rather than erroring. Under the old
``browser`` name, core's subparser won and this module's six subcommands
(pair/devices/revoke/logs/status/export) were completely unreachable — no
warning, no error, just ``hermes browser pair`` failing with "'pair' is not a
`hermes browser` command." This was hit live against the gateway's v0.21.4
install (see docs/troubleshooting.md for the incident write-up) and fixed by
renaming this plugin's CLI namespace in ``hermes_plugin/__init__.py``. Do not
rename it back to ``browser`` without re-checking ``hermes --help`` for a
collision first.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import attach as attach_mod
from . import audit, config, relay as relay_mod, skill_links, state


def setup_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(
        dest="browser_command", metavar="{pair,devices,revoke,logs,status,export,skills}"
    )

    pair = sub.add_parser("pair", help="print a one-time pairing code for the Chrome extension")
    pair.add_argument("--label", default="", help="note stored with the code (e.g. 'my-laptop')")

    devices = sub.add_parser("devices", help="list paired devices")
    devices.add_argument("--all", action="store_true", help="include revoked devices")
    devices.add_argument("--json", action="store_true", help="machine-readable output")

    revoke = sub.add_parser("revoke", help="revoke a device token and disconnect it")
    revoke.add_argument("device_id")

    logs = sub.add_parser("logs", help="tail the bridge audit log")
    logs.add_argument("-n", "--lines", type=int, default=30)
    logs.add_argument("--filter", default="", help="substring match on the event name")
    logs.add_argument("--event", default="", help="exact match on the event name (e.g. 'grant_check')")
    logs.add_argument(
        "--session", default="",
        help="only entries for this agent session (G3.1) -- an id from `browser_bridge_session list`, "
        "or a label, matched case-insensitively across every device (ambiguous labels across devices are "
        "all included, not refused -- this is a read-only diagnostic, not the tool-call ownership check)",
    )
    logs.add_argument(
        "--since", default="", metavar="DURATION",
        help="only entries newer than this (e.g. '30m', '2h', '1d', '1w')",
    )
    logs.add_argument(
        "--include-rotated", action="store_true",
        help="also search rotated audit.jsonl.N files, not just the live log",
    )
    logs.add_argument(
        "--timing", action="store_true",
        help="E1: aggregate tool_timing audit events into per-tool count, p50, p95 and max total_ms "
        "instead of tailing raw log lines -- honours --since/--session/--include-rotated as the "
        "aggregation window, ignores --lines/--filter/--event (tool_timing is the only event read)",
    )
    logs.add_argument("--json", action="store_true")

    sub.add_parser("status", help="show relay configuration and paired devices")

    export = sub.add_parser(
        "export",
        help="write a portable audit bundle (JSONL + a devices/grants summary) for sharing or offline review",
    )
    export.add_argument("--out", required=True, help="output directory to write the bundle into (created if missing)")
    export.add_argument(
        "--include-rotated", action="store_true",
        help="also include rotated audit.jsonl.N files, not just the live log",
    )
    export.add_argument(
        "--force", action="store_true",
        help="overwrite --out if it already exists and is non-empty",
    )

    skills = sub.add_parser(
        "skills",
        help="show how bridge references and product skills are linked (read-only)",
    )
    skills.add_argument("--json", action="store_true", help="print products_index() as-is, for scripts")


def handle_cli(args: argparse.Namespace) -> int:
    command = getattr(args, "browser_command", None)
    handlers = {
        "pair": _cmd_pair,
        "devices": _cmd_devices,
        "revoke": _cmd_revoke,
        "logs": _cmd_logs,
        "status": _cmd_status,
        "export": _cmd_export,
        "skills": _cmd_skills,
    }
    handler = handlers.get(command or "")
    if handler is None:
        print("usage: hermes browser-bridge {pair,devices,revoke,logs,status,export,skills}")
        return 1
    return handler(args)


_DURATION_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def _parse_since(value: str) -> "tuple[Optional[int], str]":
    """Parse a ``--since`` duration like ``30m``/``2h``/``1d``/``1w`` into an
    epoch-millisecond cutoff. Returns (cutoff_ms_or_None, error_message) —
    error_message is "" on success, and empty/absent ``value`` is not an error
    (it just means "no --since filter"), matching every other optional flag
    on this command."""
    if not value:
        return None, ""
    match = _DURATION_RE.match(value.strip())
    if not match:
        return None, (
            f"--since {value!r} is not a duration — use a number followed by "
            f"s/m/h/d/w, e.g. '30m', '2h', '1d', '1w'"
        )
    amount, unit = match.groups()
    seconds = int(amount) * _DURATION_SECONDS[unit.lower()]
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - seconds * 1000
    return cutoff_ms, ""


def _cmd_pair(args: argparse.Namespace) -> int:
    cfg = config.load()
    issued = state.create_pair_code(getattr(args, "label", ""))
    url = config.ws_url(cfg)
    audit.record("pair_code_issued", label=getattr(args, "label", ""), expires_at=issued["expires_at"])
    print()
    print("  Hermes Browser Bridge — pair a Chrome device")
    print("  ─────────────────────────────────────────────")
    print(f"  Pairing code : {issued['code']}")
    print(f"  Gateway URL  : {url}")
    print(f"  Valid for    : {issued['ttl_minutes']} minutes (single use)")
    print()
    print("  In Chrome: open the Hermes Browser Bridge popup, paste the gateway")
    print("  URL if it differs from the default, enter the code, and press Connect.")
    print()
    return 0


def _cmd_devices(args: argparse.Namespace) -> int:
    devices = state.list_devices(include_revoked=getattr(args, "all", False))
    if getattr(args, "json", False):
        print(json.dumps(devices, indent=2))
        return 0
    if not devices:
        print("No paired devices. Run `hermes browser-bridge pair` to add one.")
        return 0
    print(f"{'DEVICE ID':<20} {'NAME':<22} {'PLATFORM':<10} {'LAST SEEN':<22} STATUS")
    for device in devices:
        status = "revoked" if device.get("revoked_at") else "active"
        print(
            f"{device['id']:<20} {device['name'][:21]:<22} {(device.get('platform') or '-')[:9]:<10} "
            f"{_stamp(device.get('last_seen')):<22} {status}"
        )
    return 0


def _cmd_revoke(args: argparse.Namespace) -> int:
    device_id = args.device_id
    if not state.revoke_device(device_id):
        print(f"No active device with id {device_id}.")
        return 1
    audit.record("device_revoked", device=device_id, source="cli")
    # A revoked device_id can never call _fresh_tabs()/reconcile() again to
    # self-heal its own stale attach-registry bookkeeping (tools.py's normal
    # path for dropping leases the extension no longer reports), and an
    # UNLIMITED lease never expires on its own either -- so without this, a
    # tab this device leased would stay permanently un-attachable by anyone,
    # even the same physical browser re-paired under a fresh device_id, until
    # the gateway process restarts. Force-release regardless of holder (the
    # same bypass the kill switch uses) before anything else, so a revoke
    # that later fails to reach the extension (offline, or relay is None in
    # a test harness) has still freed the tabs gateway-side.
    released = attach_mod.get_registry().release_all_for_device(device_id)
    if released:
        audit.record("device_revoked_leases_released", device=device_id, tabs=released)
    relay = relay_mod.get_relay()
    if relay is not None:
        relay.disconnect(device_id, reason="revoked")
    print(f"Revoked {device_id}. The device must be paired again to reconnect.")
    print("Note: a gateway restart is not required — the token is dead immediately.")
    return 0


def _resolve_session_ids(selector: str) -> "tuple[Optional[set], str]":
    """Resolve `--session <label-or-id>` (G3.1.5) into the set of
    agent_sessions.id values `audit.tail`'s `session_ids` filter should
    match. Returns (None, "") when no --session was given at all (no
    filtering). An id that resolves to nothing, or a label nobody's session
    uses, is reported as an error rather than silently returning zero
    results indistinguishable from "session ran but did nothing".
    """
    if not selector:
        return None, ""
    exact = state.get_agent_session(selector)
    if exact is not None:
        return {exact["id"]}, ""
    # Unlike the tool-side lookup (sessions.py's _lookup_named_session,
    # device-scoped for ownership), this CLI runs locally with full access to
    # state.db already -- there is no device boundary to enforce, so a label
    # match here is intentionally cross-device: `logs --session "ticket"`
    # should show that label's audit trail wherever it was created.
    matches = [s for s in state.list_agent_sessions(include_closed=True) if (s.get("label") or "").lower() == selector.lower()]
    if matches:
        return {s["id"] for s in matches}, ""
    return None, f"no session matches {selector!r} (checked as an id, then as a label across all devices)"


def _cmd_logs(args: argparse.Namespace) -> int:
    since_ms, error = _parse_since(getattr(args, "since", ""))
    if error:
        print(error)
        return 1
    session_ids, session_error = _resolve_session_ids(getattr(args, "session", ""))
    if session_error:
        print(session_error)
        return 1
    paths = [config.AUDIT_PATH]
    if getattr(args, "include_rotated", False):
        # Oldest-first so a --lines cutoff on the combined read keeps the most
        # recent entries regardless of which file they landed in.
        paths = audit.rotated_paths() + paths
    if getattr(args, "timing", False):
        # E1: the aggregation window is --since/--session/--include-rotated,
        # never --lines (a per-tool count/p50/p95/max over only the last 30
        # lines would be misleading) -- read every matching tool_timing entry
        # via a limit no --lines value could accidentally undercut.
        timing_entries = audit.tail(
            limit=sys.maxsize,
            event_exact="tool_timing",
            since_ms=since_ms,
            paths=paths,
            session_ids=session_ids,
        )
        return _print_timing_summary(timing_entries, as_json=args.json)
    entries = audit.tail(
        limit=args.lines,
        event_filter=args.filter,
        event_exact=getattr(args, "event", ""),
        since_ms=since_ms,
        paths=paths,
        session_ids=session_ids,
    )
    if args.json:
        print(json.dumps(entries, indent=2))
        return 0
    if not entries:
        print(f"No matching audit entries ({config.AUDIT_PATH}).")
        return 0
    for entry in entries:
        rest = {k: v for k, v in entry.items() if k not in ("ts", "event")}
        detail = " ".join(f"{k}={v}" for k, v in rest.items())
        print(f"{_stamp(entry.get('ts')):<22} {entry.get('event', ''):<26} {detail}")
    return 0


_EXPORT_CONTENTS_NOTE = (
    "This bundle contains: (1) audit.jsonl (and audit.jsonl.N if --include-rotated was "
    "given) — the bridge's own audit trail: every paired/revoked device, every grant "
    "change, and every gated tool call (snapshot/read/act/fetch/cookies/network) with its "
    "allow/deny decision and reason; (2) summary.json — the paired devices (id, name, "
    "platform, browser, extension version, timestamps — never a device token, which is "
    "stored only as a one-way SHA-256 hash and cannot be recovered from it anyway) and the "
    "per-origin grants table (origin, mode, last-updated). It never contains: cookie "
    "values, device tokens, or raw page/network bodies — those are withheld from (or never "
    "written to) the audit log in the first place (see docs/security.md). It CAN contain: "
    "URLs, page titles, CSS selectors, form field names/labels, and short free-text "
    "summaries of act()/ask() calls — treat it as operationally sensitive even though it "
    "holds no credentials, and share it only with someone who should see what this device "
    "did in the browser."
)


def _cmd_export(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).expanduser()
    if out_dir.exists():
        if any(out_dir.iterdir()) and not args.force:
            print(f"{out_dir} already exists and is not empty. Pass --force to overwrite its contents.")
            return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []

    live_path = config.AUDIT_PATH
    if live_path.exists():
        dest = out_dir / "audit.jsonl"
        shutil.copyfile(live_path, dest)
        written.append(dest.name)
    else:
        (out_dir / "audit.jsonl").write_text("", encoding="utf-8")
        written.append("audit.jsonl")

    if args.include_rotated:
        for rotated in audit.rotated_paths():
            dest = out_dir / rotated.name
            shutil.copyfile(rotated, dest)
            written.append(dest.name)

    devices = [
        {k: v for k, v in device.items() if k != "token_hash"}
        for device in state.list_devices(include_revoked=True)
    ]
    grants = state.list_grants()

    summary = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hermes_browser_bridge_version": config.VERSION,
        "device_count": len(devices),
        "devices": devices,
        "grants": grants,
        "audit_files": written,
        "contents": _EXPORT_CONTENTS_NOTE,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    written.append("summary.json")

    print(f"Wrote {len(written)} file(s) to {out_dir}:")
    for name in written:
        size = (out_dir / name).stat().st_size
        print(f"  {name} ({size} bytes)")
    print()
    print(_EXPORT_CONTENTS_NOTE)
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    cfg = config.load()
    print(f"Relay bind     : {cfg['host']}:{cfg['port']}{cfg['path']}")
    print(f"Dial-in URL    : {config.ws_url(cfg)}")
    print(f"Enabled        : {cfg['enabled']}")
    print(f"Default mode   : {cfg['default_mode']}")
    print(f"State database : {config.DB_PATH}")
    print(f"Audit log      : {config.AUDIT_PATH}")
    relay = relay_mod.get_relay()
    if relay is None:
        print("Relay          : not running in this process (expected — it lives in the gateway)")
    else:
        print(f"Relay          : {json.dumps(relay.status())}")
    return _cmd_devices(argparse.Namespace(all=False, json=False))


def _skills_root() -> Path:
    """Where `skill_links` looked for the local skill library — used only to
    give the empty-index message somewhere concrete to point at. Reaches into
    `skill_links`'s own HERMES_HOME resolution rather than duplicating it, so
    the two never drift apart."""
    return skill_links._hermes_home() / "skills"


def _protected_label(protected: "Optional[bool]") -> str:
    if protected is True:
        return "protected: yes"
    if protected is False:
        return "protected: no"
    return "protected: unknown"


def _skill_label(skill: dict, has_refs: bool = True) -> str:
    """One product skill entry (products_index()'s `product_skills` shape)
    rendered as ``name (linked, reference lists it, protected: yes)``.
    ``ref_names_skill`` is a newer, optional field (whether the bridge
    reference's own `skills:` header names this skill) — shown only when the
    row actually carries it, via `.get`, so this keeps working unchanged
    against an index built before that field existed."""
    parts = ["linked" if skill.get("linked") else "not linked"]
    ref_names_skill = skill.get("ref_names_skill")
    if ref_names_skill is not None and has_refs:
        parts.append("reference lists it" if ref_names_skill else "reference doesn't list it")
    parts.append(_protected_label(skill.get("protected")))
    return f"{skill['name']} ({', '.join(parts)})"


def _protected_reason_hints(by_reason: "dict[Optional[str], set]") -> "list[str]":
    """One hint line per group of unlinkable skills, keyed by *why* they
    can't be auto-linked (rev2's `protected_reason`): a curator command for
    the two reversible states, an explanation for the three externally-owned
    ones (no command exists), grouped separately so the operator isn't told
    to run `hermes curator adopt` on a skill that command can't help.
    `None` (unknown provenance) and `"unmanaged"` share the same line -- both
    are fixed the same way, and this is also the ONLY reason this repo's own
    bare test venv (no importable Hermes provenance module) can ever
    actually produce."""
    lines: "list[str]" = []
    generic = sorted(by_reason.get(None, set()) | by_reason.get("unmanaged", set()))
    if generic:
        lines.append(
            "Protected or unknown-writability skills can't be auto-linked — run "
            f"`hermes curator adopt <name>` first (affects: {', '.join(generic)})."
        )
    pinned = sorted(by_reason.get("pinned", set()))
    if pinned:
        lines.append(
            "Pinned skills can't be auto-linked — run `hermes curator unpin <name>` first "
            f"(affects: {', '.join(pinned)})."
        )
    external_owned = sorted(
        by_reason.get("bundled", set()) | by_reason.get("hub", set()) | by_reason.get("external", set())
    )
    if external_owned:
        lines.append(
            "Bundled/hub/external skills can't be auto-linked — installed from outside the "
            "local library; an edit would be overwritten on update. Leave them unlinked; the "
            f"reference's skills: header still links this side (affects: {', '.join(external_owned)})."
        )
    return lines


def _cmd_skills(args: argparse.Namespace) -> int:
    rows = skill_links.products_index()
    if getattr(args, "json", False):
        # --json is for scripts: products_index()'s own return value,
        # unchanged. The misplacement warning below is a human-facing report
        # line, never mixed into machine-readable output.
        print(json.dumps(rows, indent=2))
        return 0

    misplaced = skill_links.bundled_manual_misplacement()
    if misplaced:
        print(
            f"WARNING: {misplaced['path']} looks like the bundled manual, not Hermes's own "
            "learned browser-bridge skill. The manual ships inside the plugin directory and "
            "should never live in a skills directory — restore the learned skill from a backup."
        )
        print()

    if not rows:
        print(f"No bridge references or product skills found under {_skills_root()}.")
        return 0

    total_missing = 0
    by_reason: "dict[Optional[str], set]" = {}
    for i, row in enumerate(rows):
        if i:
            print()
        products = row.get("products") or row.get("terms")
        if products:
            print(", ".join(products))
        elif row.get("bridge_references"):
            print(f"(unnamed — see {row['bridge_references'][0]['file']})")
        else:
            print("(unnamed)")

        refs = row.get("bridge_references") or []
        if refs:
            ref_text = ", ".join(f"{r['file']} — {r['heading']}" if r.get("heading") else r["file"] for r in refs)
        else:
            ref_text = "(none)"
        print(f"  {'references:':<13}{ref_text}")

        product_skills = row.get("product_skills") or []
        if product_skills:
            skills_text = ", ".join(_skill_label(s, bool(refs)) for s in product_skills)
        else:
            skills_text = "(none)"
        print(f"  {'skills:':<13}{skills_text}")
        for s in product_skills:
            if not s.get("linked") and s.get("protected") is not False:
                by_reason.setdefault(s.get("protected_reason"), set()).add(s["name"])

        origins = row.get("origins") or []
        print(f"  {'origins:':<13}{', '.join(origins) if origins else '(none)'}")

        missing_links = row.get("missing_links") or []
        print(f"  {'missing:':<13}{'; '.join(missing_links) if missing_links else '(none)'}")
        total_missing += len(missing_links)

        promote = row.get("promote") or []
        if promote:
            promote_text = "; ".join(skill_links.promotion_sentence(c) for c in promote)
        else:
            promote_text = "(none)"
        print(f"  {'promote:':<13}{promote_text}")

    print()
    print(f"{len(rows)} product{'s' if len(rows) != 1 else ''}, "
          f"{total_missing} missing link{'s' if total_missing != 1 else ''}")
    for line in _protected_reason_hints(by_reason):
        print(line)
    return 0


def _percentile(sorted_values: "list[float]", fraction: float) -> float:
    """Linear-interpolated percentile over an already-sorted list (the same
    method as numpy's default `interpolation='linear'`) -- exact on n=1."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * fraction
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_values[int(rank)]
    frac = rank - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def _print_timing_summary(entries: "list[dict]", as_json: bool) -> int:
    """E1: `hermes browser-bridge logs --timing` -- per-tool count/p50/p95/max
    total_ms over whichever `tool_timing` audit entries the caller's
    --since/--session/--include-rotated window selected. `total_ms` is
    tools.py's `_wrap_handler_with_timing`'s own gateway-side wall-clock
    measurement (hermes_plugin/timing.py's `build_tool_timing`) -- present on
    every `tool_timing` entry, unlike `relay_ms`/`extension_ms` which are
    only present when the handler actually called the extension."""
    per_tool: "dict[str, list[float]]" = {}
    for entry in entries:
        tool = entry.get("tool")
        total_ms = entry.get("total_ms")
        if not isinstance(tool, str) or not isinstance(total_ms, (int, float)):
            continue
        per_tool.setdefault(tool, []).append(float(total_ms))

    rows = []
    for tool in sorted(per_tool):
        values = sorted(per_tool[tool])
        rows.append(
            {
                "tool": tool,
                "count": len(values),
                "p50_ms": round(_percentile(values, 0.5), 1),
                "p95_ms": round(_percentile(values, 0.95), 1),
                "max_ms": round(values[-1], 1),
            }
        )

    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print(f"No tool_timing audit entries found ({config.AUDIT_PATH}).")
        return 0
    print(f"{'TOOL':<34} {'COUNT':>7} {'P50 MS':>9} {'P95 MS':>9} {'MAX MS':>9}")
    for row in rows:
        print(
            f"{row['tool']:<34} {row['count']:>7} {row['p50_ms']:>9} {row['p95_ms']:>9} {row['max_ms']:>9}"
        )
    return 0


def _stamp(value) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
