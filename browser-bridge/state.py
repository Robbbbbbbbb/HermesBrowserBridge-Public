"""Device / grant / pairing registry backed by SQLite.

The popup is UX; this database is law (plan §6.2). Every gated decision the
relay makes is read from here, so a compromised extension cannot widen its own
access by lying about what the user selected.

Threading: the relay runs on its own thread and CLI commands run on the main
thread, so the connection is opened with ``check_same_thread=False`` and every
statement is serialised through a module lock. WAL keeps readers from blocking
the relay's writes.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    token_hash  TEXT NOT NULL,
    platform    TEXT,
    browser     TEXT,
    ext_version TEXT,
    created_at  INTEGER NOT NULL,
    last_seen   INTEGER,
    revoked_at  INTEGER
);

CREATE TABLE IF NOT EXISTS grants (
    device_id  TEXT NOT NULL,
    origin     TEXT NOT NULL,
    mode       TEXT NOT NULL CHECK (mode IN ('off', 'request', 'full')),
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (device_id, origin)
);

CREATE TABLE IF NOT EXISTS pair_codes (
    code       TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    consumed   INTEGER NOT NULL DEFAULT 0,
    label      TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    device_id  TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    ended_at   INTEGER,
    remote     TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_device ON sessions(device_id);

-- Approval REQUEST/RESOLUTION correlation is no longer persisted here as of
-- the native-transport migration (hermes_plugin/approvals.py): Hermes'
-- hermes_cli.approval_transport.invoke_approval_transport() already bounds
-- the wait on a worker thread, and a gateway restart kills the blocked
-- gateway-worker thread that owned a pending approval exactly like it kills
-- everything else in that process -- there is no cross-process "pending" row
-- left to make visible or reap. See approvals.py's module docstring.
--
-- The 'session' scope's effect: this device+session+origin+capability keeps
-- auto-approving (without asking again) until expires_at, bounded by
-- config's approval_session_grant_ttl_hours in the absence of a reliable
-- session-boundary hook (see Documentation/plugin-api-findings.md).
CREATE TABLE IF NOT EXISTS session_grants (
    device_id   TEXT NOT NULL,
    session_key TEXT NOT NULL,
    origin      TEXT NOT NULL,
    capability  TEXT NOT NULL,
    granted_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    PRIMARY KEY (device_id, session_key, origin, capability)
);

-- G0.5's per-capability ceiling grant: the "always" choice for a capability
-- in approvals.DANGEROUS_CAPABILITIES writes HERE instead of promoting the
-- whole origin to 'full' in the `grants` table above (approvals.py's
-- `_apply_result` -- "the ceiling wins": a pre-existing `full` row never
-- implies a dangerous capability, and this table never implies `full` for
-- anything else either). TTL-bounded the same way `session_grants` is and
-- for the same reason (no reliable revoke-on-session-end hook exists per
-- Documentation/plugin-api-findings.md) -- config's
-- `approval_session_grant_ttl_hours` sizes both. New table only, per this
-- module's migration policy below (`CREATE TABLE IF NOT EXISTS`, no
-- `ALTER TABLE`, no `user_version`) -- nothing here touches `grants`.
CREATE TABLE IF NOT EXISTS capability_grants (
    device_id   TEXT NOT NULL,
    origin      TEXT NOT NULL,
    capability  TEXT NOT NULL,
    granted_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    PRIMARY KEY (device_id, origin, capability)
);

-- The per-device redaction policy last reported over the wire (device.hello/
-- device.heartbeat/state.report — protocol/schema.json's `redactionPolicy`).
-- Persisted (unlike Connection.paused in relay.py, which is memory-only and
-- fine to lose on a gateway restart) because tools.py's belt-and-braces
-- re-check (`_gateway_redact`) needs a durable answer to "is this device
-- currently redacting card numbers?" and because an audited before/after on
-- a policy change (a security-relevant setting) requires a "before" that
-- survives longer than one WS connection. One JSON blob per device rather
-- than one row per kind: the five kinds are always read/written together
-- (get_redaction_policy always returns all five), so there's no query that
-- benefits from them being separate rows.
CREATE TABLE IF NOT EXISTS device_redaction_policy (
    device_id  TEXT PRIMARY KEY,
    policy     TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

-- The per-device POWER policy last reported over the wire (device.hello/
-- device.heartbeat/state.report -- protocol/schema.json's `powerPolicy`).
-- Same shape as device_redaction_policy above (one JSON blob per device,
-- persisted for the same audited-before/after reason) but the OPPOSITE fail
-- direction: redaction's "absent means unknown, treat as enabled" is the
-- safe read for a privacy protection, while granting an agent a capability
-- on an unknown or malformed report would be the unsafe one. See
-- `_power_enabled` below -- a device this table has never heard from gets
-- every capability disabled, not enabled.
CREATE TABLE IF NOT EXISTS device_power_policy (
    device_id  TEXT PRIMARY KEY,
    policy     TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

-- The per-device DEFAULT ACCESS MODE last validly reported over the wire
-- (device.hello/device.heartbeat/state.report -- protocol/schema.json's
-- `device_default_mode`). The user asked for a gate that lives in the extension's
-- OWN settings, not only in Hermes' config.yaml: this table is that gate.
-- New table only, per this module's migration policy (`CREATE TABLE IF NOT
-- EXISTS`, no `ALTER TABLE`, no `user_version`) -- it does not touch
-- `grants` (an explicit per-origin choice always outranks this) or
-- config.py's `default_mode` (the fleet-wide floor for a device that has
-- never reported one). One row per device, one mode column -- unlike the
-- two policy tables above there is nothing here to merge field-by-field:
-- a reported mode is either a valid `off`/`request`/`full` or it is
-- rejected outright and this table is left exactly as it was (see
-- `set_device_default_mode`). `get_mode`/`effective_default_mode` below are
-- the only readers; row-level keyed on `device_id`, the authenticated
-- connection's own id -- there is no code path that lets one device set
-- another's default.
CREATE TABLE IF NOT EXISTS device_default_mode (
    device_id  TEXT PRIMARY KEY,
    mode       TEXT NOT NULL CHECK (mode IN ('off', 'request', 'full')),
    updated_at INTEGER NOT NULL
);

-- The per-device TAB DRIVING-LEASE DURATION last validly reported over the
-- wire (device.hello/device.heartbeat/state.report -- protocol/schema.json's
-- `device_lease_seconds`). Same shape and migration policy as
-- device_default_mode immediately above (new table only, no `ALTER TABLE`,
-- no `user_version`): one row per device, `lease_seconds` column, keyed on
-- the authenticated connection's own device_id -- there is no code path that
-- lets one device set another's lease. `lease_seconds` is either 0
-- (UNLIMITED -- attach.py's AttachRegistry represents this in-memory as
-- `float('inf')`, never as a stored 0-as-if-it-were-a-duration) or an
-- integer in [10, 1200]; anything else is rejected outright by
-- `set_device_lease_seconds` and this table is left exactly as it was,
-- mirroring device_default_mode's "reject, never coerce" rule.
-- `get_mode`/`effective_default_mode`'s resolution-order comment above has
-- this table's own counterpart: `effective_lease_seconds` below is the only
-- reader, falling back to config.py's `lease_seconds` for a device that has
-- never reported one.
CREATE TABLE IF NOT EXISTS device_lease_seconds (
    device_id     TEXT PRIMARY KEY,
    lease_seconds INTEGER NOT NULL CHECK (lease_seconds = 0 OR (lease_seconds BETWEEN 10 AND 1200)),
    updated_at    INTEGER NOT NULL
);

-- speedimprovements.md H1: the per-device COMMITTING-ACTIONS MODE last
-- validly reported over the wire (device.hello/device.heartbeat/
-- state.report -- protocol/schema.json's `device_commit_mode`). Same shape
-- and migration policy as device_default_mode/device_lease_seconds above
-- (new table only, no `ALTER TABLE`, no `user_version`): one row per
-- device, one `mode` column, keyed on the authenticated connection's own
-- device_id -- there is no code path that lets one device set another's.
-- Unlike device_default_mode there is no fleet-wide config.yaml floor for
-- this (H1's own spec names "Auto proceed" as the default outright) -- see
-- `effective_commit_mode` below, which falls back directly to "auto"
-- rather than reading config.py. Enforcement using this value is entirely
-- gateway-side (hermes_plugin/tools.py's handle_act and its `steps` batch
-- preflight), not extension-side.
CREATE TABLE IF NOT EXISTS device_commit_mode (
    device_id  TEXT PRIMARY KEY,
    mode       TEXT NOT NULL CHECK (mode IN ('auto', 'pause')),
    updated_at INTEGER NOT NULL
);

-- G3.1 first-class sessions (coveragegaps.md §G3.1). A persistent, nameable,
-- resumable identity for "the work a given conversation is doing on a given
-- device" -- distinct from BOTH the `sessions` table above (that one is the
-- WebSocket connection id, never compared to this) and from whatever ad-hoc
-- string Hermes happens to pass as its own `session_id` tool-call kwarg
-- (tools.py's `_session_key`/`_resolve_session` -- that raw kwarg is bound
-- to one of these rows, not stored as identity itself; see
-- `session_bindings` below). New tables only, per this module's migration
-- policy (`CREATE TABLE IF NOT EXISTS`, no `ALTER TABLE`, no `user_version`)
-- -- a rollback leaves these three as orphan tables, which is fine: nothing
-- else references them.
CREATE TABLE IF NOT EXISTS agent_sessions (
    id             TEXT PRIMARY KEY,
    device_id      TEXT NOT NULL,
    label          TEXT NOT NULL DEFAULT '',
    created_at     INTEGER NOT NULL,
    last_active_at INTEGER,
    closed_at      INTEGER,
    notes          TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_device ON agent_sessions(device_id);

-- Per-tab bookkeeping for a session's workspace. Created under G3.1.1's
-- schema so G3.3 (coveragegaps.md §G3.3, the tab workspace itself) could
-- land with no migration of its own; G3.1 never wrote a row here, G3.3's
-- `tabs.py` is the first and only writer -- see `agent_session_tab_watch`
-- below for the live url/title/staleness half of the same logical row.
CREATE TABLE IF NOT EXISTS agent_session_tabs (
    session_id TEXT NOT NULL,
    tab_id     INTEGER NOT NULL,
    tab_key    TEXT,
    role       TEXT,
    added_at   INTEGER NOT NULL,
    PRIMARY KEY (session_id, tab_id)
);

-- Which agent_sessions row a raw Hermes `session_id` (or equivalent) kwarg
-- is currently bound to, per device. Keyed on (hermes_session_key,
-- device_id) rather than the raw key alone so two different devices whose
-- Hermes-side ids happen to collide (unlikely, but the raw value is an
-- unnamespaced string from outside this plugin) can never cross-bind each
-- other's sessions -- see tools.py's `_resolve_session`/`ensure_shadow_session`.
CREATE TABLE IF NOT EXISTS session_bindings (
    hermes_session_key TEXT NOT NULL,
    device_id          TEXT NOT NULL,
    agent_session_id   TEXT NOT NULL,
    bound_at           INTEGER NOT NULL,
    PRIMARY KEY (hermes_session_key, device_id)
);

-- G3.3 tab workspace (coveragegaps.md §G3.3): live freshness bookkeeping for
-- a row in `agent_session_tabs` above. Kept as its own table rather than
-- widening that one -- `agent_session_tabs` shipped with G3.1 under a
-- migration policy of "new tables only, no ALTER TABLE, no user_version"
-- (see this file's SCHEMA comment above `agent_sessions`), and that policy
-- applies to this table regardless of the fact that G3.1 itself never wrote
-- a row into it. `stale` is G3.3.4's "flagged stale" bit: set whenever the
-- gateway's `tab.changed` handler (tools.py's sibling `tabs.py`) sees this
-- concrete tab navigate or resize, cleared the next time THIS session
-- snapshots/reads/acts on it. Deliberately keyed on `(session_id, tab_id)`
-- alone, with no `device_id` column: every query that needs device scoping
-- goes through a `session_id IN (SELECT id FROM agent_sessions WHERE
-- device_id = ?)` subquery instead (see `mark_device_tab_changed` and
-- `delete_session_tabs_for_device_tab` below) -- the same IDOR discipline
-- `agent_session_tabs` itself relies on: a session's own rows, scoped by
-- session_id in the SQL, never a column a caller could spoof.
CREATE TABLE IF NOT EXISTS agent_session_tab_watch (
    session_id      TEXT NOT NULL,
    tab_id          INTEGER NOT NULL,
    last_url        TEXT,
    last_title      TEXT,
    last_digest     TEXT,
    last_looked_at  INTEGER,
    last_changed_at INTEGER,
    stale           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, tab_id)
);

-- H4 "clean up after yourself" (speedimprovements.md H4): records which
-- (session, tab) pairs were opened via browser_bridge_open_tab -- as
-- opposed to a tab the USER opened and shared, or one a DIFFERENT session
-- opened. This is the authorization boundary `browser_bridge_tabs
-- action=close_opened` and `browser_bridge_session close`'s
-- `close_opened_tabs` flag both enforce: neither ever closes a tab whose
-- (session_id, tab_id) isn't a row here, full stop -- see
-- `is_agent_opened_tab`/`get_agent_opened_tabs` below and hermes_plugin/
-- tabs.py's `close_opened_tabs`. New table only, per this module's
-- migration policy (`CREATE TABLE IF NOT EXISTS`, no `ALTER TABLE`, no
-- `user_version`) -- it does not touch `agent_session_tabs` (G3.3's
-- workspace row exists for ANY attached tab, agent-opened or not; this
-- table is the narrower "did *I* open this" fact layered on top of it, and
-- a row can outlive the workspace row -- e.g. the tab was released but
-- never closed).
--
-- `device_id` is stored directly (rather than joined through
-- `agent_sessions`) so `forget_agent_opened_tabs_for_device_tab` (the
-- `tab.changed`/`removed` cleanup hermes_plugin/tabs.py's `on_tab_changed`
-- calls) can scope by device_id in the SQL itself, the same IDOR discipline
-- `agent_session_tabs`'s own device-scoped delete uses.
CREATE TABLE IF NOT EXISTS agent_opened_tabs (
    session_id TEXT NOT NULL,
    tab_id     INTEGER NOT NULL,
    device_id  TEXT NOT NULL,
    opened_at  INTEGER NOT NULL,
    PRIMARY KEY (session_id, tab_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_opened_tabs_device ON agent_opened_tabs(device_id);

-- Skill Links SL1.6 (skilllinks.md): a bounded log of (origin, skill)
-- pairs actually seen together in a bridged task -- the "used" match signal
-- hermes_plugin/skill_links.py's match_page/match_skill treat as trusted,
-- alongside a reference/skill's own declared products/origins header.
-- `file_path` is empty for a SKILL.md view (the observation covers the
-- whole product skill) and the reference's own relative path
-- (`references/esxi-ui.md`) for a browser-bridge reference view -- see
-- skill_hooks.py's SL3.2/SL3.3 (the only writer, via
-- skill_links.record_used()). `source` is `'used'` for a binding
-- corroborated by anything other than the page's own title (trusted; counted
-- by match_page's "used" precedence step) or `'used_title'` for one
-- corroborated ONLY by the page's title (still page-authored, so surfaced in
-- observed_origins/products_index but never trusted on its own -- see
-- skill_links.record_used()'s docstring). New table only, per this module's
-- migration policy
-- (`CREATE TABLE IF NOT EXISTS`, no `ALTER TABLE`, no `user_version`).
CREATE TABLE IF NOT EXISTS skill_link_observations (
    origin     TEXT NOT NULL,
    skill      TEXT NOT NULL,
    file_path  TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'used',
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    count      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (origin, skill, file_path)
);
"""

# Mirrors protocol/schema.json's `definitions.redactionKind` enum and
# extension/src/lib/storage.ts's `RedactionPolicy` — kept as a tuple, not
# re-derived from either (this module has no reason to parse the schema or
# import extension TypeScript), so a kind added to one side without the
# other is a visible three-way diff, not a silent gap.
REDACTION_KINDS = ("password", "card", "ssn", "email", "phone")

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None


def connect() -> sqlite3.Connection:
    """Open (once) and return the shared connection."""
    global _conn
    with _lock:
        if _conn is None:
            # mkdir with mode=0o700 up front so there is no window where the
            # directory exists world-readable before we tighten it below —
            # mode is still masked by umask, so the explicit chmod after is
            # what actually guarantees it.
            config.STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(config.STATE_DIR, 0o700)
            except OSError:
                pass
            conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            conn.commit()
            _restrict_state_files()
            _conn = conn
        return _conn


def _restrict_state_files() -> None:
    """0600 the DB file and its WAL/SHM sidecars — WAL mode means the
    sidecars hold the same row data as state.db, so leaving them at the
    default umask is no hardening at all."""
    candidates = [
        config.DB_PATH,
        config.DB_PATH.with_name(config.DB_PATH.name + "-wal"),
        config.DB_PATH.with_name(config.DB_PATH.name + "-shm"),
    ]
    for path in candidates:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def _now() -> int:
    return int(time.time() * 1000)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- pairing ---------------------------------------------------------------

def create_pair_code(label: str = "") -> Dict[str, Any]:
    """Mint a one-time 6-digit pairing code.

    Uses ``secrets`` rather than ``random``: the code is the only thing standing
    between a LAN neighbour and a paired device token.
    """
    cfg = config.load()
    ttl_ms = int(cfg["pair_code_ttl_minutes"]) * 60 * 1000
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = _now()
    with _lock:
        conn = connect()
        conn.execute("DELETE FROM pair_codes WHERE expires_at < ? OR consumed = 1", (now,))
        conn.execute(
            "INSERT OR REPLACE INTO pair_codes (code, created_at, expires_at, consumed, label)"
            " VALUES (?, ?, ?, 0, ?)",
            (code, now, now + ttl_ms, label),
        )
        conn.commit()
    return {"code": code, "expires_at": now + ttl_ms, "ttl_minutes": cfg["pair_code_ttl_minutes"]}


def redeem_pair_code(code: str) -> str:
    """Consume a pairing code. Returns "" when the code is unknown/expired/used."""
    now = _now()
    with _lock:
        conn = connect()
        row = conn.execute("SELECT code, expires_at, consumed FROM pair_codes WHERE code = ?", (code,)).fetchone()
        if row is None or row["consumed"] or row["expires_at"] < now:
            return ""
        conn.execute("UPDATE pair_codes SET consumed = 1 WHERE code = ?", (code,))
        conn.commit()
        return code


# --- devices ---------------------------------------------------------------

def register_device(name: str, platform: str = "", browser: str = "", ext_version: str = "") -> Dict[str, str]:
    """Create a device row and return its id plus the raw token (shown once)."""
    device_id = "dev_" + secrets.token_hex(8)
    token = secrets.token_urlsafe(32)
    now = _now()
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO devices (id, name, token_hash, platform, browser, ext_version, created_at, last_seen)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (device_id, name or device_id, _hash_token(token), platform, browser, ext_version, now, now),
        )
        conn.commit()
    return {"device_id": device_id, "device_token": token}


class DeviceRevoked(Exception):
    """Raised by authenticate() for a device whose token matches but is
    revoked, so callers can tell a permanent failure from an unknown/forged
    one (both used to collapse to the same "not recognised" outcome)."""

    def __init__(self, device_id: str):
        super().__init__(f"device {device_id} is revoked")
        self.device_id = device_id


def authenticate(device_id: str, token: str) -> Optional[sqlite3.Row]:
    """Return the device row when the token matches. Raises DeviceRevoked when
    the token matches but the device has been revoked.

    Token comparison happens before the revoked check on purpose: revealing
    "revoked" vs. "unknown" to someone who doesn't hold the correct token
    would turn revocation status into a scanning oracle.
    """
    if not device_id or not token:
        return None
    with _lock:
        conn = connect()
        row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if row is None:
        return None
    if not secrets.compare_digest(row["token_hash"], _hash_token(token)):
        return None
    if row["revoked_at"]:
        raise DeviceRevoked(device_id)
    return row


def touch_device(device_id: str, **fields: Any) -> None:
    sets = ["last_seen = ?"]
    values: List[Any] = [_now()]
    for column in ("name", "platform", "browser", "ext_version"):
        if fields.get(column):
            sets.append(f"{column} = ?")
            values.append(fields[column])
    values.append(device_id)
    with _lock:
        conn = connect()
        conn.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id = ?", values)
        conn.commit()


def list_devices(include_revoked: bool = False) -> List[Dict[str, Any]]:
    query = "SELECT * FROM devices"
    if not include_revoked:
        query += " WHERE revoked_at IS NULL"
    query += " ORDER BY created_at"
    with _lock:
        rows = connect().execute(query).fetchall()
    return [dict(row) for row in rows]


def revoke_device(device_id: str) -> bool:
    with _lock:
        conn = connect()
        cur = conn.execute(
            "UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (_now(), device_id),
        )
        conn.commit()
        return cur.rowcount > 0


def has_active_device(offline_after_seconds: Optional[int] = None) -> bool:
    """True when at least one non-revoked device has checked in recently.

    Drives the tool ``check_fn`` so unpaired sessions never pay for dead tool
    schemas (CLAUDE.md rule 3 — prompt caching).
    """
    if offline_after_seconds is None:
        offline_after_seconds = int(config.load()["device_offline_after_seconds"])
    cutoff = _now() - offline_after_seconds * 1000
    with _lock:
        row = connect().execute(
            "SELECT COUNT(*) AS n FROM devices WHERE revoked_at IS NULL AND last_seen >= ?",
            (cutoff,),
        ).fetchone()
    return bool(row and row["n"])


# --- grants ----------------------------------------------------------------

def set_grant(device_id: str, origin: str, mode: str) -> None:
    if mode not in ("off", "request", "full"):
        raise ValueError(f"invalid mode: {mode}")
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO grants (device_id, origin, mode, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(device_id, origin) DO UPDATE SET mode = excluded.mode, updated_at = excluded.updated_at",
            (device_id, origin, mode, _now()),
        )
        conn.commit()


def get_mode(device_id: str, origin: str) -> str:
    """Resolution order (the user's 2026-09-23 ask, "default access mode" as an
    extension-owned gate, not only config.yaml's): an explicit per-origin
    grant wins first; then this device's own reported default
    (`effective_default_mode`, which itself falls back to config.py's
    `default_mode` when the device has never validly reported one); config's
    `default_mode` is therefore only ever reached through that second call,
    never duplicated here."""
    with _lock:
        row = connect().execute(
            "SELECT mode FROM grants WHERE device_id = ? AND origin = ?", (device_id, origin)
        ).fetchone()
    if row is not None:
        return row["mode"]
    return effective_default_mode(device_id)


def get_device_default_mode(device_id: str) -> Optional[str]:
    """The mode `device_id` most recently VALIDLY reported as its own
    default (protocol/schema.json's `device_default_mode`), or ``None`` if
    it has never reported one or every report so far has been rejected by
    `set_device_default_mode`. Distinct from ``None`` meaning "off": ``None``
    means "no device-level opinion on record", which is exactly the signal
    `effective_default_mode` needs to know it must fall back to config.py's
    fleet-wide `default_mode` instead.

    Defensive on read as well as on write: a row somehow containing anything
    outside {off, request, full} (the CHECK constraint should make this
    unreachable, but nothing here trusts a constraint alone as the sole line
    of defence) reads back as ``None`` rather than propagating a value nothing
    upstream validated."""
    with _lock:
        row = connect().execute(
            "SELECT mode FROM device_default_mode WHERE device_id = ?", (device_id,)
        ).fetchone()
    if row is None:
        return None
    mode = row["mode"]
    return mode if mode in ("off", "request", "full") else None


def set_device_default_mode(device_id: str, reported: Any) -> Optional[str]:
    """Persist a `device_default_mode` field exactly as it arrived on the
    wire (device.hello/device.heartbeat/state.report), strictly validated:
    only the literal strings "off", "request" or "full" are accepted.
    Anything else — wrong type, a stray casing, an old/misbehaving build
    sending something outside the enum — is REJECTED outright: nothing is
    written, and this returns ``None`` so the caller (relay.py's
    `_apply_reported_default_mode`) can audit the rejection and leave
    whatever this device last validly reported (or nothing at all) exactly
    as it was, per the user's "anything else is ignored ... and audited" ask.
    This is the actual enforcement point — the schema's `$ref` to the
    `mode` enum is documentation, not validation; nothing upstream checks a
    wire frame's field types before this function sees them.

    Row-level, keyed on `device_id` — always the authenticated connection's
    own id (relay.py never takes this from a param), so a device can only
    ever set its own default, never another device's.

    Returns the stored mode on success, so callers can diff it against a
    `get_device_default_mode()` taken before the call and audit an actual
    change the same way `set_redaction_policy`/`set_power_policy` do for
    their own tables.
    """
    if reported not in ("off", "request", "full"):
        return None
    now = _now()
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO device_default_mode (device_id, mode, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(device_id) DO UPDATE SET mode = excluded.mode, updated_at = excluded.updated_at",
            (device_id, reported, now),
        )
        conn.commit()
    return reported


def effective_default_mode(device_id: str) -> str:
    """What an origin with no explicit grant row actually gets for
    `device_id` right now: this device's own reported default if it has
    ever validly reported one, else config.py's fleet-wide `default_mode`.
    The single resolution point for "device default, else config default" —
    `get_mode` above calls this, and so does every other call site that used
    to read `config.load()["default_mode"]` directly (relay.py's hello/
    heartbeat results, navigation.py's open_tab bootstrap) so that a device
    which has set its own default is honoured everywhere the fleet default
    used to apply, not just in the origin-grant fallback."""
    device_default = get_device_default_mode(device_id)
    if device_default is not None:
        return device_default
    return str(config.load()["default_mode"])


def get_device_lease_seconds(device_id: str) -> Optional[int]:
    """The tab driving-lease duration `device_id` most recently VALIDLY
    reported as its own (protocol/schema.json's `device_lease_seconds`), or
    ``None`` if it has never reported one or every report so far has been
    rejected by `set_device_lease_seconds`. Distinct from ``None`` meaning
    "unlimited": ``None`` means "no device-level opinion on record", which is
    exactly the signal `effective_lease_seconds` needs to know it must fall
    back to config.py's fleet-wide `lease_seconds` instead. ``0`` (a real,
    valid return value) means unlimited -- see that table's own comment.

    Defensive on read as well as on write: a row somehow containing anything
    outside {0} ∪ [10, 1200] (the CHECK constraint should make this
    unreachable, but nothing here trusts a constraint alone as the sole line
    of defence) reads back as ``None`` rather than propagating a value
    nothing upstream validated. Excludes `bool` explicitly (Python's `bool`
    is an `int` subclass) the same way state.py's own `_power_max_upload_bytes`
    does, so a stray `True`/`False` can never read back as 1/0 seconds.
    """
    with _lock:
        row = connect().execute(
            "SELECT lease_seconds FROM device_lease_seconds WHERE device_id = ?", (device_id,)
        ).fetchone()
    if row is None:
        return None
    value = row["lease_seconds"]
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if (value == 0 or 10 <= value <= 1200) else None


def set_device_lease_seconds(device_id: str, reported: Any) -> Optional[int]:
    """Persist a `device_lease_seconds` field exactly as it arrived on the
    wire (device.hello/device.heartbeat/state.report), strictly validated:
    only a literal (non-bool) `int` that is either 0 or in [10, 1200] is
    accepted. Anything else -- wrong type (including a `bool`, which Python's
    `isinstance` would otherwise accept as an `int`), a float, a string, or
    an in-range-looking value outside {0} ∪ [10, 1200] -- is REJECTED
    outright: nothing is written, and this returns ``None`` so the caller
    (relay.py's `_apply_reported_lease_seconds`) can audit the rejection and
    leave whatever this device last validly reported (or nothing at all)
    exactly as it was, mirroring `set_device_default_mode`'s "reject, never
    coerce" rule exactly.

    Row-level, keyed on `device_id` -- always the authenticated connection's
    own id (relay.py never takes this from a param), so a device can only
    ever set its own lease, never another device's.

    Returns the stored value on success, so callers can diff it against a
    `get_device_lease_seconds()` taken before the call and audit an actual
    change the same way `set_device_default_mode` does for its own table.
    """
    if isinstance(reported, bool) or not isinstance(reported, int):
        return None
    if not (reported == 0 or 10 <= reported <= 1200):
        return None
    now = _now()
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO device_lease_seconds (device_id, lease_seconds, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(device_id) DO UPDATE SET lease_seconds = excluded.lease_seconds, updated_at = excluded.updated_at",
            (device_id, reported, now),
        )
        conn.commit()
    return reported


def effective_lease_seconds(device_id: str) -> int:
    """What `device_id`'s tab driving lease actually lasts right now, in the
    same 0-means-unlimited wire shape `device_lease_seconds` uses: this
    device's own reported value if it has ever validly reported one, else
    config.py's fleet-wide `lease_seconds` (default 60). The single
    resolution point every `attach.py` `AttachRegistry.attach()` call site
    (tools.py's handle_attach/handle_act, navigation.py's handle_open_tab,
    upload.py's handle_upload) reads through via `attach.ttl_seconds_for()`
    -- mirrors `effective_default_mode` exactly."""
    device_value = get_device_lease_seconds(device_id)
    if device_value is not None:
        return device_value
    return int(config.load()["lease_seconds"])


def get_device_commit_mode(device_id: str) -> Optional[str]:
    """speedimprovements.md H1: the mode `device_id` most recently VALIDLY
    reported as its own committing-actions preference (protocol/schema.json's
    `device_commit_mode`), or ``None`` if it has never reported one or every
    report so far has been rejected by `set_device_commit_mode`. Distinct
    from ``None`` meaning "auto": ``None`` means "no device-level opinion on
    record", which is exactly the signal `effective_commit_mode` needs to
    know it must fall back to "auto" (H1's own spec default -- there is no
    fleet-wide config.py key for this, unlike default_mode/lease_seconds).

    Defensive on read as well as on write: a row somehow containing anything
    outside {auto, pause} (the CHECK constraint should make this
    unreachable, but nothing here trusts a constraint alone as the sole line
    of defence) reads back as ``None`` rather than propagating a value
    nothing upstream validated."""
    with _lock:
        row = connect().execute(
            "SELECT mode FROM device_commit_mode WHERE device_id = ?", (device_id,)
        ).fetchone()
    if row is None:
        return None
    mode = row["mode"]
    return mode if mode in ("auto", "pause") else None


def set_device_commit_mode(device_id: str, reported: Any) -> Optional[str]:
    """speedimprovements.md H1: persist a `device_commit_mode` field exactly
    as it arrived on the wire (device.hello/device.heartbeat/state.report),
    strictly validated: only the literal strings "auto" or "pause" are
    accepted. Anything else -- wrong type, a stray casing, an old/
    misbehaving build sending something outside the enum -- is REJECTED
    outright: nothing is written, and this returns ``None`` so the caller
    (relay.py's `_apply_reported_commit_mode`) can audit the rejection and
    leave whatever this device last validly reported (or nothing at all)
    exactly as it was, mirroring `set_device_default_mode`'s "reject, never
    coerce" rule exactly.

    Row-level, keyed on `device_id` -- always the authenticated connection's
    own id (relay.py never takes this from a param), so a device can only
    ever set its own commit mode, never another device's.

    Returns the stored mode on success, so callers can diff it against a
    `get_device_commit_mode()` taken before the call and audit an actual
    change the same way `set_device_default_mode`/`set_device_lease_seconds`
    do for their own tables."""
    if reported not in ("auto", "pause"):
        return None
    now = _now()
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO device_commit_mode (device_id, mode, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(device_id) DO UPDATE SET mode = excluded.mode, updated_at = excluded.updated_at",
            (device_id, reported, now),
        )
        conn.commit()
    return reported


def effective_commit_mode(device_id: str) -> str:
    """speedimprovements.md H1: what `device_id`'s committing-actions mode
    actually is right now: this device's own reported value if it has ever
    validly reported one, else "auto" -- H1's own spec names "Auto proceed"
    as the default outright, so unlike `effective_default_mode`/
    `effective_lease_seconds` there is no fleet-wide config.py key to fall
    back to here; adding one was deliberately not done since the task never
    asked for a config.yaml knob for this. The single resolution point
    hermes_plugin/tools.py's handle_act and its `steps` batch preflight
    read through."""
    device_value = get_device_commit_mode(device_id)
    if device_value is not None:
        return device_value
    return "auto"


def has_explicit_grant(device_id: str, origin: str) -> bool:
    """True iff a grants-table row exists for this exact (device, origin) —
    i.e. ``get_mode`` would NOT be falling back to ``config.py``'s
    ``default_mode``. G2.2.13 (acting inside frames): an embedded frame's
    origin must never be authorized purely because the fleet's tab-level
    default happens to be permissive (the same rule ``tools.py``'s
    ``_reconcile_frame_origins`` already applies to snapshot text) — this is
    the primitive that lets ``_authorize``'s ``require_explicit_grant`` flag
    enforce that without duplicating the grants-table query."""
    with _lock:
        row = connect().execute(
            "SELECT 1 FROM grants WHERE device_id = ? AND origin = ?", (device_id, origin)
        ).fetchone()
    return row is not None


def list_grants(device_id: str = "") -> List[Dict[str, Any]]:
    query = "SELECT device_id, origin, mode, updated_at FROM grants"
    params: tuple = ()
    if device_id:
        query += " WHERE device_id = ?"
        params = (device_id,)
    with _lock:
        rows = connect().execute(query + " ORDER BY origin", params).fetchall()
    return [dict(row) for row in rows]


# --- sessions --------------------------------------------------------------

def open_session(device_id: str, remote: str = "") -> str:
    session_id = "ses_" + secrets.token_hex(8)
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO sessions (id, device_id, started_at, remote) VALUES (?, ?, ?, ?)",
            (session_id, device_id, _now(), remote),
        )
        conn.commit()
    return session_id


def close_session(session_id: str) -> None:
    with _lock:
        conn = connect()
        conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL", (_now(), session_id))
        conn.commit()


# --- session-scope grants (M2 'session' choice) -----------------------------
#
# This module only ever stores what approvals.py already redacted -- it does
# no scrubbing of its own.

def set_session_grant(device_id: str, session_key: str, origin: str, capability: str, ttl_seconds: int) -> None:
    now = _now()
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO session_grants (device_id, session_key, origin, capability, granted_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(device_id, session_key, origin, capability)"
            " DO UPDATE SET granted_at = excluded.granted_at, expires_at = excluded.expires_at",
            (device_id, session_key, origin, capability, now, now + ttl_seconds * 1000),
        )
        conn.commit()


def get_session_grant(device_id: str, session_key: str, origin: str, capability: str) -> Optional[Dict[str, Any]]:
    if not session_key:
        return None
    with _lock:
        conn = connect()
        row = conn.execute(
            "SELECT * FROM session_grants WHERE device_id = ? AND session_key = ? AND origin = ? AND capability = ?",
            (device_id, session_key, origin, capability),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] < _now():
            # Opportunistic cleanup -- not load-bearing (get_session_grant
            # returning None for an expired row is what actually matters).
            conn.execute(
                "DELETE FROM session_grants WHERE device_id = ? AND session_key = ? AND origin = ? AND capability = ?",
                (device_id, session_key, origin, capability),
            )
            conn.commit()
            return None
    return dict(row)



# --- per-capability ceiling grants (G0.5 'always' choice for a DANGEROUS
# capability) ----------------------------------------------------------------
#
# This module only ever stores what approvals.py already redacted/decided --
# it does no scrubbing and no DANGEROUS_CAPABILITIES membership check of its
# own (that lives in approvals.py, the one place SCOPES/DANGEROUS_CAPABILITIES
# are defined); a row here is honoured for whatever capability string it was
# written under, full stop.

def set_capability_grant(device_id: str, origin: str, capability: str, ttl_seconds: int) -> None:
    now = _now()
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO capability_grants (device_id, origin, capability, granted_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(device_id, origin, capability)"
            " DO UPDATE SET granted_at = excluded.granted_at, expires_at = excluded.expires_at",
            (device_id, origin, capability, now, now + ttl_seconds * 1000),
        )
        conn.commit()


def get_capability_grant(device_id: str, origin: str, capability: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = connect()
        row = conn.execute(
            "SELECT * FROM capability_grants WHERE device_id = ? AND origin = ? AND capability = ?",
            (device_id, origin, capability),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] < _now():
            # Opportunistic cleanup -- not load-bearing (returning None for
            # an expired row is what actually matters; see
            # get_session_grant's identical comment).
            conn.execute(
                "DELETE FROM capability_grants WHERE device_id = ? AND origin = ? AND capability = ?",
                (device_id, origin, capability),
            )
            conn.commit()
            return None
    return dict(row)


def clear_capability_grants(device_id: str = "", origin: str = "") -> int:
    """Drop per-capability ceiling grants. Same rationale as
    clear_session_grants: no caller wires this to a revocation hook yet: it
    exists so a future one -- or a test -- can force early expiry."""
    clauses = []
    params: List[Any] = []
    if device_id:
        clauses.append("device_id = ?")
        params.append(device_id)
    if origin:
        clauses.append("origin = ?")
        params.append(origin)
    query = "DELETE FROM capability_grants"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    with _lock:
        conn = connect()
        cur = conn.execute(query, params)
        conn.commit()
    return cur.rowcount


def clear_session_grants(device_id: str = "", session_key: str = "") -> int:
    """Drop session-scope grants. No caller wires this to a session-end hook
    yet (no reliable one exists per Documentation/plugin-api-findings.md);
    it exists so a future hook -- or a test -- can force early expiry."""
    clauses = []
    params: List[Any] = []
    if device_id:
        clauses.append("device_id = ?")
        params.append(device_id)
    if session_key:
        clauses.append("session_key = ?")
        params.append(session_key)
    query = "DELETE FROM session_grants"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    with _lock:
        conn = connect()
        cur = conn.execute(query, params)
        conn.commit()
    return cur.rowcount


# --- redaction policy (plan §6.3) -------------------------------------------

def _kind_enabled(value: Any) -> bool:
    """Interpret one kind's reported/stored value under the documented
    contract: "absent or unknown means ENABLED" (protocol/schema.json's
    `redactionPolicy`). This is deliberately NOT `bool(value)` — that
    coerces ANY JSON-falsy value (`null`, `0`, `""`, `[]`, `{}`) to `False`,
    which silently DISABLES a kind on a malformed-but-present report (e.g.
    `{"password": null}` reads as "explicitly turned off" instead of
    "unknown, treat as enabled") even though nothing validates the wire frame
    against the schema's `"type": "boolean"` before it reaches here (there is
    no jsonschema validation anywhere in this package — the schema is
    documentation, not enforcement). A present-but-wrong-typed value is
    exactly as "unknown" as an absent one, and the fail-safe direction for an
    unknown redaction setting is enabled, not disabled.

    The only thing that disables a kind is the literal JSON boolean `false`,
    which `json.loads` always turns into Python's `False` singleton — checked
    here with `is`, not `==`, specifically so `0`, `0.0`, and any other
    value that compares equal to `False` without actually being it (`0 ==
    False` is `True` in Python) can't disable a kind by accident. A string
    `"false"` does NOT disable a kind either: the schema declares this field
    as a JSON boolean, not a string, and a spec-compliant client sends the
    former — accepting string forms would invite exactly the guessing game
    ("false"? "False"? "0"? "no"?) this function exists to avoid.
    """
    return value is not False


def get_redaction_policy(device_id: str) -> Dict[str, bool]:
    """The redaction policy `device_id` most recently reported, one bool per
    REDACTION_KINDS. A device this table has never heard from — including
    every device paired before this feature existed — gets every kind True:
    protocol/schema.json's `redactionPolicy` defines an absent report as
    "unknown, treated as enabled", which for a device that has never reported
    ANYTHING is the same rule applied at the whole-record level. This is what
    keeps `_gateway_redact`'s original unconditional behaviour exactly intact
    for an extension build that predates the `redaction` field.

    Reads through `_kind_enabled` (not a bare `bool()`) so a malformed value
    that somehow made it into storage — a hand-edited DB row, or a future bug
    in `set_redaction_policy` — degrades to "enabled" on read too, not just
    on write; the fail-safe direction is enforced at both ends independently.
    """
    with _lock:
        row = connect().execute(
            "SELECT policy FROM device_redaction_policy WHERE device_id = ?", (device_id,)
        ).fetchone()
    if row is None:
        return {kind: True for kind in REDACTION_KINDS}
    try:
        stored = json.loads(row["policy"])
    except (TypeError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    return {kind: _kind_enabled(stored.get(kind, True)) for kind in REDACTION_KINDS}


def set_redaction_policy(device_id: str, reported: Dict[str, Any]) -> Dict[str, bool]:
    """Persist a `redaction` object exactly as one arrived on the wire
    (device.hello/device.heartbeat/state.report). Only call this when the
    field was actually present in `params` — relay.py's three handlers guard
    that with `"redaction" in params` before calling in, mirroring how they
    already treat `paused` — since the whole point of "absent means
    unknown/enabled" is a property of a MISSING report, not something this
    function should also apply to a report that omits one field of five (a
    kind absent from `reported` itself is still treated as enabled, per
    protocol/schema.json, but that's `reported.get(kind, True)` below, not a
    reason to skip persisting the rest of a call that did arrive).

    `_kind_enabled` (not `bool()`) is what makes a kind PRESENT with a
    malformed value — `null`, `0`, `""`, `[]`, `{}`, or anything else that
    isn't the JSON boolean `false` — read the same as an absent one: enabled.
    Nothing upstream validates a `redaction` object's field types against
    protocol/schema.json's `"type": "boolean"` before this function sees it,
    so this is the actual enforcement point, not the schema declaration.

    Returns the resulting policy (always all five kinds) so callers can diff
    it against a `get_redaction_policy()` taken before the call and audit the
    change — see relay.py's hello/heartbeat/state.report handlers.
    """
    policy = {kind: _kind_enabled(reported.get(kind, True)) for kind in REDACTION_KINDS}
    now = _now()
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO device_redaction_policy (device_id, policy, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(device_id) DO UPDATE SET policy = excluded.policy, updated_at = excluded.updated_at",
            (device_id, json.dumps(policy), now),
        )
        conn.commit()
    return policy


# --- power policy (G0.6) -----------------------------------------------------
#
# What the agent is allowed to DO, as opposed to redaction's what-it's-
# allowed-to-SEE above. Mirrors the redaction policy's shape (one JSON blob
# per device, persisted for the same audited-before/after reason) but
# inverts the fail direction end to end — see `_power_enabled`'s own
# docstring for why.

# Mirrors protocol/schema.json's `definitions.powerPolicy` boolean properties
# and extension/src/lib/storage.ts's `PowerPolicy` — kept as a tuple, not
# re-derived from either, for the same reason REDACTION_KINDS is: a
# capability added to one side without the other is a visible three-way
# diff, not a silent gap. `uploadRoots` and `maxUploadBytes` are handled
# separately below (a string and an integer, not gated booleans).
POWER_KINDS = (
    "allowFileUpload",
    "allowFileUploadFromAgent",
    "allowDialogDismiss",
    "allowDialogAccept",
    "allowEvaluate",
    "allowConsoleRead",
    "allowCookieWrite",
    "allowHttpAuth",
    "allowDownloadsRead",
)


def _power_enabled(value: Any) -> bool:
    """The inverse of `_kind_enabled` above — deliberately fail-CLOSED, not
    fail-safe. Redaction's "absent or malformed means enabled" is the right
    default for a privacy protection: an unknown state should still redact.
    A power is the opposite kind of thing — silently granting an agent a
    capability because a report came back malformed, or never arrived at
    all, is the unsafe direction. So here only the literal JSON boolean
    `true` enables a capability; anything else (absent, `null`, `0`, `""`,
    a stray string, a device that has never reported) disables it. Checked
    with `is`, not `==`, for the same reason `_kind_enabled` checks `is
    False`: equality would let values that merely compare equal to `True`
    slip through.
    """
    return value is True


def _power_upload_roots(value: Any) -> str:
    """`uploadRoots` fails closed the same direction as the booleans:
    anything that isn't actually a string — including absent/None — reads as
    empty, which is already "deny every local-path upload" per the setting's
    own contract (extension/src/lib/storage.ts's DEFAULT_SETTINGS)."""
    return value if isinstance(value, str) else ""


def _power_max_upload_bytes(value: Any) -> int:
    """`maxUploadBytes` fails closed to 0 (no bytes ever allowed) for
    anything that isn't a plain integer — explicitly excluding `bool`, since
    Python's `bool` is an `int` subclass and `isinstance(True, int)` is
    true, which would otherwise let a stray `true`/`false` read as 1/0
    bytes instead of failing closed."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


def get_power_policy(device_id: str) -> Dict[str, Any]:
    """The power policy `device_id` most recently reported: one bool per
    POWER_KINDS plus `uploadRoots`/`maxUploadBytes`. A device this table has
    never heard from — including every device paired before this feature
    existed — gets every capability disabled, `uploadRoots` empty and
    `maxUploadBytes` zero: protocol/schema.json's `powerPolicy` defines an
    absent report as "unknown, treated as disabled", the opposite of
    `get_redaction_policy`'s default, because granting capabilities to a
    device nobody has configured would be the unsafe direction.

    Reads through `_power_enabled` (not a bare `bool()`) so a malformed
    value that somehow made it into storage degrades to "disabled" on read
    too, not just on write — the fail-closed direction is enforced at both
    ends independently, exactly like `get_redaction_policy` does for its own
    (opposite) fail direction.
    """
    with _lock:
        row = connect().execute(
            "SELECT policy FROM device_power_policy WHERE device_id = ?", (device_id,)
        ).fetchone()
    if row is None:
        policy: Dict[str, Any] = {kind: False for kind in POWER_KINDS}
        policy["uploadRoots"] = ""
        policy["maxUploadBytes"] = 0
        return policy
    try:
        stored = json.loads(row["policy"])
    except (TypeError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    policy = {kind: _power_enabled(stored.get(kind)) for kind in POWER_KINDS}
    policy["uploadRoots"] = _power_upload_roots(stored.get("uploadRoots"))
    policy["maxUploadBytes"] = _power_max_upload_bytes(stored.get("maxUploadBytes"))
    return policy


def set_power_policy(device_id: str, reported: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a `powers` object exactly as it arrived on the wire
    (device.hello/device.heartbeat/state.report). Only call this when the
    field was actually present in `params` — relay.py's `_apply_reported_powers`
    guards that with `isinstance(reported, dict)`, mirroring how
    `_apply_reported_redaction` already treats `redaction`.

    `_power_enabled`/`_power_upload_roots`/`_power_max_upload_bytes` (not
    bare truthiness) are what make a PRESENT-but-malformed value read the
    same as an absent one: disabled/empty/zero. Nothing upstream validates a
    `powers` object's field types against protocol/schema.json's declared
    types before this function sees it, so this is the actual enforcement
    point, not the schema declaration.

    Returns the resulting policy (always all nine kinds plus the two bound
    fields) so callers can diff it against a `get_power_policy()` taken
    before the call and audit the change — see relay.py's hello/heartbeat/
    state.report handlers.
    """
    policy: Dict[str, Any] = {kind: _power_enabled(reported.get(kind)) for kind in POWER_KINDS}
    policy["uploadRoots"] = _power_upload_roots(reported.get("uploadRoots"))
    policy["maxUploadBytes"] = _power_max_upload_bytes(reported.get("maxUploadBytes"))
    now = _now()
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO device_power_policy (device_id, policy, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(device_id) DO UPDATE SET policy = excluded.policy, updated_at = excluded.updated_at",
            (device_id, json.dumps(policy), now),
        )
        conn.commit()
    return policy


def power_enabled(device_id: str, key: str) -> bool:
    """Clean single-capability reader for `hermes_plugin/tools.py`'s
    `_authorize` to consult before its per-origin grant check (G0.6.5c —
    that wiring belongs to a different workstream; this function is the
    seam it will call). `key` is one of POWER_KINDS (an unrecognised key —
    a typo, a capability this table doesn't know about — fails closed to
    False rather than raising, on the theory that an authorization check
    that fails open on a coding mistake is worse than one that fails closed
    on it). Fail-closed the same way `get_power_policy` is: an unknown
    device or an unset field both read False.
    """
    if key not in POWER_KINDS:
        return False
    return bool(get_power_policy(device_id).get(key, False))


# --- G3.1 first-class agent sessions ----------------------------------------
#
# See this module's SCHEMA comment above `agent_sessions` for the concept.
# Everything below is plain CRUD; the interesting policy (auto-provisioning
# a "shadow" session for a caller who never explicitly created one, binding a
# raw Hermes session kwarg to one of these rows, and refusing a closed
# session) lives in tools.py's `_resolve_session`, not here -- this module
# only stores what it's told, same discipline as the grants tables above.

def _new_agent_session_id() -> str:
    return "ags_" + secrets.token_hex(8)


def create_agent_session(
    device_id: str, label: str = "", notes: str = "", session_id: Optional[str] = None
) -> Dict[str, Any]:
    """Insert a new agent_sessions row and return it. ``session_id`` lets a
    caller pin the row's id to a specific value (tools.py's
    ``ensure_shadow_session`` uses this to make an auto-provisioned session's
    id equal to the raw Hermes kwarg it backs, so every caller that never
    touches ``browser_bridge_session`` at all keeps exactly the identity it
    always had); omitted, a fresh ``ags_<hex>`` id is minted. A brand new
    session never inherits anything -- no session_grants row can exist for an
    id nothing has used before, which is precisely G3.1.4's "new sessions
    never inherit grants" by construction, not by an extra check here.
    """
    session_id = session_id or _new_agent_session_id()
    now = _now()
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO agent_sessions (id, device_id, label, created_at, last_active_at, closed_at, notes)"
            " VALUES (?, ?, ?, ?, ?, NULL, ?)",
            (session_id, device_id, label, now, now, notes),
        )
        conn.commit()
    return {
        "id": session_id,
        "device_id": device_id,
        "label": label,
        "created_at": now,
        "last_active_at": now,
        "closed_at": None,
        "notes": notes,
    }


def get_agent_session(session_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = connect().execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row is not None else None


def list_agent_sessions(device_id: str = "", include_closed: bool = False) -> List[Dict[str, Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if device_id:
        clauses.append("device_id = ?")
        params.append(device_id)
    if not include_closed:
        clauses.append("closed_at IS NULL")
    query = "SELECT * FROM agent_sessions"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY last_active_at DESC, created_at DESC"
    with _lock:
        rows = connect().execute(query, params).fetchall()
    return [dict(row) for row in rows]


def most_recent_open_agent_session(device_id: str) -> Optional[Dict[str, Any]]:
    """G3.1.3's fallback target when a tool call carries no Hermes session
    kwarg at all: the most recently active OPEN session on this device,
    replacing the old shared ``"default-session"`` bucket. Deliberate
    behaviour change -- see tools.py's ``_resolve_session`` docstring."""
    with _lock:
        row = connect().execute(
            "SELECT * FROM agent_sessions WHERE device_id = ? AND closed_at IS NULL"
            " ORDER BY last_active_at DESC, created_at DESC LIMIT 1",
            (device_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def find_agent_sessions_by_label(device_id: str, label: str) -> List[Dict[str, Any]]:
    """Case-insensitive exact label match, scoped to ``device_id`` only --
    this scoping is itself half of G3.1.4's IDOR protection: a label search
    can never even see another device's sessions, let alone act on one."""
    with _lock:
        rows = connect().execute(
            "SELECT * FROM agent_sessions WHERE device_id = ? AND lower(label) = lower(?)"
            " ORDER BY last_active_at DESC, created_at DESC",
            (device_id, label),
        ).fetchall()
    return [dict(row) for row in rows]


def touch_agent_session(session_id: str) -> None:
    """Bump ``last_active_at``. A no-op (not an error) for a closed or
    unknown session -- callers that need to distinguish those cases check
    ``get_agent_session`` first, per tools.py's ``_resolve_session``."""
    with _lock:
        conn = connect()
        conn.execute(
            "UPDATE agent_sessions SET last_active_at = ? WHERE id = ? AND closed_at IS NULL",
            (_now(), session_id),
        )
        conn.commit()


def rename_agent_session(session_id: str, label: str) -> bool:
    with _lock:
        conn = connect()
        cur = conn.execute("UPDATE agent_sessions SET label = ? WHERE id = ?", (label, session_id))
        conn.commit()
    return cur.rowcount > 0


def close_agent_session(session_id: str) -> bool:
    with _lock:
        conn = connect()
        cur = conn.execute(
            "UPDATE agent_sessions SET closed_at = ? WHERE id = ? AND closed_at IS NULL", (_now(), session_id)
        )
        conn.commit()
    return cur.rowcount > 0


def ensure_shadow_session(device_id: str, raw_key: str) -> Dict[str, Any]:
    """Get-or-create the agent_sessions row backing a raw Hermes session
    kwarg that was never explicitly created via ``browser_bridge_session
    create``/``resume`` (G3.1.3's default path -- every tool call before
    G3.1 shipped, and the ordinary case afterward).

    Prefers making the row's id equal to ``raw_key`` itself, so a caller's
    session identity is bit-for-bit unchanged from before this feature
    existed -- that identity is also attach.py's lease holder and
    session_grants' key, neither of which this workstream touches. Falls
    back to a freshly minted id only on the rare cross-device collision
    where a DIFFERENT device already owns a row literally named ``raw_key``
    -- never reusing another device's row across the ownership boundary
    G3.1.4 requires.
    """
    existing = get_agent_session(raw_key)
    if existing is not None and existing["device_id"] == device_id:
        return existing
    if existing is None:
        return create_agent_session(device_id, label="", notes="", session_id=raw_key)
    return create_agent_session(device_id, label="", notes="")


def bind_hermes_session(hermes_session_key: str, device_id: str, agent_session_id: str) -> None:
    """Point a raw Hermes session kwarg at an agent_sessions row for this
    device -- the "Bind Hermes' session_id to an agent_sessions.id" of
    G3.1.3. Idempotent and always a full overwrite: a later bind (e.g. an
    explicit ``resume``) replaces whatever this raw key pointed at before,
    which is exactly how resuming a different stored session onto the
    current conversation is supposed to work.
    """
    now = _now()
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO session_bindings (hermes_session_key, device_id, agent_session_id, bound_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(hermes_session_key, device_id)"
            " DO UPDATE SET agent_session_id = excluded.agent_session_id, bound_at = excluded.bound_at",
            (hermes_session_key, device_id, agent_session_id, now),
        )
        conn.commit()


def get_session_binding(hermes_session_key: str, device_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = connect().execute(
            "SELECT * FROM session_bindings WHERE hermes_session_key = ? AND device_id = ?",
            (hermes_session_key, device_id),
        ).fetchone()
    return dict(row) if row is not None else None


# --- G3.3 tab workspace ------------------------------------------------------
#
# CRUD only, same discipline as the G3.1 block above -- the policy (auto-
# assigning a key, resolving `tab` case-insensitively with unique-prefix
# matching, refusing an ambiguous one) lives in `hermes_plugin/tabs.py`, not
# here. Every function below that touches `agent_session_tabs` or
# `agent_session_tab_watch` is scoped to a `session_id` (or, for the
# tab.changed handler, a `device_id` via the `agent_sessions` subquery) in
# the SQL itself -- never filtered after the fact in Python -- because that
# is what makes it impossible for one session (or device) to ever see or
# touch another's tab records.

def upsert_session_tab(session_id: str, tab_id: int, tab_key: str, role: str = "") -> None:
    """Give a (session, tab) its key/role row, or update it. A blank
    ``role`` never clobbers a previously-set one -- only a non-blank role
    replaces what's there, so re-attaching a tab without repeating its role
    doesn't erase it."""
    now = _now()
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO agent_session_tabs (session_id, tab_id, tab_key, role, added_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(session_id, tab_id) DO UPDATE SET"
            "   tab_key = excluded.tab_key,"
            "   role = CASE WHEN excluded.role != '' THEN excluded.role ELSE agent_session_tabs.role END",
            (session_id, tab_id, tab_key, role, now),
        )
        conn.commit()


def rename_session_tab(session_id: str, tab_id: int, tab_key: Optional[str] = None, role: Optional[str] = None) -> bool:
    """Partial update for `browser_bridge_tabs action=set` -- only the
    fields actually passed (non-None) change. False when no such row exists
    for this session (never another's -- session_id is the only scope)."""
    sets: List[str] = []
    params: List[Any] = []
    if tab_key is not None:
        sets.append("tab_key = ?")
        params.append(tab_key)
    if role is not None:
        sets.append("role = ?")
        params.append(role)
    if not sets:
        return False
    params.extend([session_id, tab_id])
    with _lock:
        conn = connect()
        cur = conn.execute(
            f"UPDATE agent_session_tabs SET {', '.join(sets)} WHERE session_id = ? AND tab_id = ?", params
        )
        conn.commit()
    return cur.rowcount > 0


def get_session_tabs(session_id: str) -> List[Dict[str, Any]]:
    """Every tab THIS session (and only this session) has keyed/labeled --
    the base of the `browser_bridge_tabs` workspace view."""
    with _lock:
        rows = connect().execute(
            "SELECT * FROM agent_session_tabs WHERE session_id = ? ORDER BY added_at ASC", (session_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_session_tab(session_id: str, tab_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        row = connect().execute(
            "SELECT * FROM agent_session_tabs WHERE session_id = ? AND tab_id = ?", (session_id, tab_id)
        ).fetchone()
    return dict(row) if row is not None else None


def find_session_tabs_by_key(session_id: str, selector: str) -> List[Dict[str, Any]]:
    """Case-insensitive exact match against THIS session's own `tab_key`
    column; falls back to a unique-prefix match only when there is no exact
    hit. Scoped to `session_id` alone in the SQL -- the IDOR protection
    G3.3.2 turns on, exactly like G3.1.4's `find_agent_sessions_by_label`:
    a key collision with another session (even one on the SAME device) can
    never resolve to the wrong tab, because that other session's rows are
    never part of this query at all."""
    with _lock:
        conn = connect()
        exact = conn.execute(
            "SELECT * FROM agent_session_tabs WHERE session_id = ? AND lower(tab_key) = lower(?)",
            (session_id, selector),
        ).fetchall()
        if exact:
            return [dict(row) for row in exact]
        prefix = conn.execute(
            "SELECT * FROM agent_session_tabs WHERE session_id = ? AND tab_key IS NOT NULL"
            " AND lower(tab_key) LIKE lower(?) || '%' ORDER BY tab_key",
            (session_id, selector),
        ).fetchall()
    return [dict(row) for row in prefix]


def delete_session_tab(session_id: str, tab_id: int) -> None:
    """A released tab disappears from THIS session's workspace (G3.3.6) --
    never touches another session's row for the same tab_id."""
    with _lock:
        conn = connect()
        conn.execute("DELETE FROM agent_session_tabs WHERE session_id = ? AND tab_id = ?", (session_id, tab_id))
        conn.execute("DELETE FROM agent_session_tab_watch WHERE session_id = ? AND tab_id = ?", (session_id, tab_id))
        conn.commit()


def delete_session_tabs_for_device_tab(device_id: str, tab_id: int) -> int:
    """Drop EVERY session's workspace row for a concrete (device, tab) --
    used only when the tab itself is verifiably gone (a `tab.changed`
    `removed` event, or the attach reconcile drop), never just because ONE
    session released it (that's `delete_session_tab`, scoped to that
    session alone). The subquery is the device-scoping half of this
    function's own IDOR guarantee: it can only ever touch rows belonging to
    a session that is itself owned by `device_id`."""
    with _lock:
        conn = connect()
        cur = conn.execute(
            "DELETE FROM agent_session_tabs WHERE tab_id = ? AND session_id IN"
            " (SELECT id FROM agent_sessions WHERE device_id = ?)",
            (tab_id, device_id),
        )
        dropped = cur.rowcount
        conn.execute(
            "DELETE FROM agent_session_tab_watch WHERE tab_id = ? AND session_id IN"
            " (SELECT id FROM agent_sessions WHERE device_id = ?)",
            (tab_id, device_id),
        )
        conn.commit()
    return dropped


def touch_session_tab_watch(session_id: str, tab_id: int, url: str = "", title: str = "", digest: str = "") -> None:
    """Called after a successful snapshot/read/act: THIS session just
    looked at the tab, so its own staleness flag clears and the cached
    url/title/digest refresh."""
    now = _now()
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO agent_session_tab_watch"
            " (session_id, tab_id, last_url, last_title, last_digest, last_looked_at, last_changed_at, stale)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0)"
            " ON CONFLICT(session_id, tab_id) DO UPDATE SET"
            "   last_url = excluded.last_url, last_title = excluded.last_title,"
            "   last_digest = excluded.last_digest, last_looked_at = excluded.last_looked_at, stale = 0",
            (session_id, tab_id, url, title, digest, now, now),
        )
        conn.commit()


def get_session_tab_watch(session_id: str, tab_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        row = connect().execute(
            "SELECT * FROM agent_session_tab_watch WHERE session_id = ? AND tab_id = ?", (session_id, tab_id)
        ).fetchone()
    return dict(row) if row is not None else None


def mark_device_tab_changed(device_id: str, tab_id: int, url: str = "", title: str = "") -> int:
    """G3.3.4: the gateway-side half of the `tab.changed` event (until this
    workstream, received but never dispatched -- see relay.py's
    `_on_tab_changed`). Flags every session's workspace row for this
    concrete (device, tab) as stale: 'this session's cached view of the tab
    may no longer match what's on screen, re-snapshot before trusting an
    idx' -- the exact meaning `page.act`'s own `attach.invalidate_index_map`
    already carries for the per-device index map, surfaced here into the
    per-session workspace view instead of a second, independent staleness
    channel. Scoped by the same `agent_sessions` subquery as
    `delete_session_tabs_for_device_tab` above. A blank ``url``/``title``
    (an extension build that omitted them) never blanks out what was
    already cached."""
    now = _now()
    with _lock:
        conn = connect()
        cur = conn.execute(
            "UPDATE agent_session_tab_watch SET stale = 1, last_changed_at = ?,"
            "   last_url = CASE WHEN ? != '' THEN ? ELSE last_url END,"
            "   last_title = CASE WHEN ? != '' THEN ? ELSE last_title END"
            " WHERE tab_id = ? AND session_id IN (SELECT id FROM agent_sessions WHERE device_id = ?)",
            (now, url, url, title, title, tab_id, device_id),
        )
        conn.commit()
    return cur.rowcount


def refresh_device_tab_cache(device_id: str, tab_id: int, url: str = "", title: str = "") -> None:
    """The non-staleness-forcing sibling of `mark_device_tab_changed` --
    used for `tab.changed` changes that refresh cached metadata (tab
    activated, or moved into/out of a tab group) without implying the DOM
    or index map went stale."""
    with _lock:
        conn = connect()
        conn.execute(
            "UPDATE agent_session_tab_watch SET"
            "   last_url = CASE WHEN ? != '' THEN ? ELSE last_url END,"
            "   last_title = CASE WHEN ? != '' THEN ? ELSE last_title END"
            " WHERE tab_id = ? AND session_id IN (SELECT id FROM agent_sessions WHERE device_id = ?)",
            (url, url, title, title, tab_id, device_id),
        )
        conn.commit()


# --- H4 "clean up after yourself" -------------------------------------------
#
# CRUD only, same discipline as the G3.3 block above -- the policy (which
# selectors resolve to which tab_ids, refusing a non-agent-opened one, the
# actual tabs.close wire call) lives in hermes_plugin/tabs.py's
# `close_opened_tabs`, not here.

def record_agent_opened_tab(session_id: str, device_id: str, tab_id: int) -> None:
    """Called from navigation.py's `handle_open_tab` right after a
    successful `tabs.create`, regardless of whether the attach itself
    succeeded -- an opened-but-unattached tab (chrome://, the Web Store, the
    PDF viewer all refuse chrome.debugger) is still a tab THIS session opened
    and should still be closeable by `close_opened`, even though it never
    gets an `agent_session_tabs` workspace row."""
    now = _now()
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO agent_opened_tabs (session_id, tab_id, device_id, opened_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(session_id, tab_id) DO UPDATE SET opened_at = excluded.opened_at",
            (session_id, tab_id, device_id, now),
        )
        conn.commit()


def is_agent_opened_tab(session_id: str, tab_id: int) -> bool:
    """The authorization check itself: True iff `session_id` is the exact
    session that opened `tab_id` via browser_bridge_open_tab. A tab the user
    opened by hand was never inserted here at all; a tab a DIFFERENT session
    opened has a row keyed under that OTHER session_id, not this one -- so
    this same single check is what refuses both cases close_opened must
    refuse, with no separate "whose tab is it really" query needed."""
    with _lock:
        row = connect().execute(
            "SELECT 1 FROM agent_opened_tabs WHERE session_id = ? AND tab_id = ?", (session_id, tab_id)
        ).fetchone()
    return row is not None


def get_agent_opened_tabs(session_id: str) -> List[int]:
    """Every tab THIS session (and only this session) opened via
    browser_bridge_open_tab and hasn't yet closed/forgotten -- the "close
    everything I opened" default when close_opened is called with no
    explicit selector list."""
    with _lock:
        rows = connect().execute(
            "SELECT tab_id FROM agent_opened_tabs WHERE session_id = ? ORDER BY opened_at ASC", (session_id,)
        ).fetchall()
    return [int(row["tab_id"]) for row in rows]


def forget_agent_opened_tab(session_id: str, tab_id: int) -> None:
    """Drop the "I opened this" record once it's actually been closed (or
    released and no longer worth tracking) -- scoped to `session_id` alone,
    never touching another session's row for the same tab_id (there cannot
    be one; a tab_id is opened by exactly one session, ever)."""
    with _lock:
        conn = connect()
        conn.execute("DELETE FROM agent_opened_tabs WHERE session_id = ? AND tab_id = ?", (session_id, tab_id))
        conn.commit()


def forget_agent_opened_tabs_for_device_tab(device_id: str, tab_id: int) -> int:
    """Drop EVERY session's "I opened this" record for a concrete (device,
    tab) once the tab is verifiably gone (a `tab.changed` `removed` event --
    the user closed an agent-opened tab by hand, or `close_opened` itself
    already closed it and the extension's own onRemoved listener is now
    reporting the fact back) -- mirrors
    `delete_session_tabs_for_device_tab`'s device-scoping exactly, except
    this table stores `device_id` directly rather than needing an
    `agent_sessions` subquery to get it."""
    with _lock:
        conn = connect()
        cur = conn.execute(
            "DELETE FROM agent_opened_tabs WHERE tab_id = ? AND device_id = ?", (tab_id, device_id)
        )
        conn.commit()
    return cur.rowcount


# --- skill link observations (skilllinks.md SL1.6) --------------------------
#
# The only table hermes_plugin/skill_links.py writes -- everything else it
# does is a read-only scan of the skill library. Bounded so a long-lived
# gateway never grows this table without limit: a genuinely new row that
# would push the table over the cap evicts the stalest (oldest `last_seen`)
# row(s) first.

SKILL_LINK_OBSERVATIONS_CAP = 500


def record_skill_link_observation(origin: str, skill: str, file_path: str = "", source: str = "used") -> None:
    """Upsert one (origin, skill, file_path) observation: a fresh row starts
    at count 1, a repeat bumps `last_seen` and `count` in place. No-ops on an
    empty origin/skill -- there is nothing useful to key an observation on."""
    if not origin or not skill:
        return
    now = _now()
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO skill_link_observations (origin, skill, file_path, source, first_seen, last_seen, count)"
            " VALUES (?, ?, ?, ?, ?, ?, 1)"
            " ON CONFLICT(origin, skill, file_path)"
            " DO UPDATE SET last_seen = excluded.last_seen, source = excluded.source, count = count + 1",
            (origin, skill, file_path, source, now, now),
        )
        conn.commit()
        row_count = conn.execute("SELECT COUNT(*) AS n FROM skill_link_observations").fetchone()["n"]
        if row_count > SKILL_LINK_OBSERVATIONS_CAP:
            conn.execute(
                "DELETE FROM skill_link_observations WHERE rowid IN ("
                " SELECT rowid FROM skill_link_observations ORDER BY last_seen ASC LIMIT ?)",
                (row_count - SKILL_LINK_OBSERVATIONS_CAP,),
            )
            conn.commit()


def list_skill_link_observations(skill: str = "", origin: str = "") -> List[Dict[str, Any]]:
    """Observed bindings, optionally filtered by skill and/or origin, newest
    first. Both filters are exact-match and cheap: the table is capped at
    `SKILL_LINK_OBSERVATIONS_CAP` rows, so no index beyond the primary key
    is worth the added migration surface."""
    query = "SELECT origin, skill, file_path, source, first_seen, last_seen, count FROM skill_link_observations"
    clauses = []
    params: List[Any] = []
    if skill:
        clauses.append("skill = ?")
        params.append(skill)
    if origin:
        clauses.append("origin = ?")
        params.append(origin)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY last_seen DESC"
    with _lock:
        rows = connect().execute(query, params).fetchall()
    return [dict(row) for row in rows]
