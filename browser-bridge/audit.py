"""Append-only JSONL audit trail (plan §6.2).

Every gated call, pairing event, grant change and revocation lands here. The
file is the record of what the agent did with the user's browser, so writes are
best-effort-but-loud: a failure is logged, never raised into the caller.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def record(event: str, **fields: Any) -> None:
    """Write one audit line. Rotates the log when it exceeds the configured size."""
    line = json.dumps(
        {"ts": int(time.time() * 1000), "event": event, **fields},
        default=str,
        separators=(",", ":"),
    )
    try:
        with _lock:
            path = config.AUDIT_PATH
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
            _rotate_if_needed(path)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            _restrict(path)
    except Exception:
        logger.exception("browser_bridge: audit write failed for event %s", event)


def _restrict(path: Path) -> None:
    """Force 0600 on the audit log whenever it is wider than that.

    Checked on every write rather than only at creation: a bridge installed
    before this hardening already has a log at the default umask (observed at
    0664 on the gateway), and that file is never recreated, so a
    create-time-only chmod would leave every existing install unprotected.
    """
    try:
        if path.stat().st_mode & 0o777 != 0o600:
            os.chmod(path, 0o600)
    except OSError:
        pass


def tail(
    limit: int = 50,
    event_filter: str = "",
    event_exact: str = "",
    since_ms: "int | None" = None,
    paths: "list[Path] | None" = None,
    session_ids: "set[str] | None" = None,
) -> list[dict]:
    """Return the most recent audit entries, newest last.

    ``event_filter`` is a substring match against the event name (existing
    M4-predates behaviour, unchanged). ``event_exact`` — added for `hermes
    browser logs --event` — additionally requires an exact match; the two can
    be combined (both must pass) though callers normally use only one.
    ``since_ms`` drops any entry older than that epoch-millisecond timestamp
    (``hermes browser-bridge logs --since``). ``paths`` overrides which file(s) are
    read, oldest-first — used by ``export`` to fold rotated logs into one
    chronological read without duplicating this parsing loop; the default
    (None) reads only the live ``config.AUDIT_PATH``, exactly as before.

    ``session_ids`` (G3.1.5, ``hermes browser-bridge logs --session <label>``)
    keeps only entries whose ``session`` OR ``holder`` field is one of these
    resolved ``agent_sessions.id`` values — every session-bound tool call
    audits under ``holder`` (the field name predates G3.1; it is what
    ``tools.py``'s per-tool ``audit.record(..., holder=holder, ...)`` calls
    already pass, and since G3.1's ``_resolve_session`` that value IS a
    persistent ``agent_sessions.id``), while this module's own
    ``session_*`` events (create/list/resume/rename/close/describe) audit
    under ``session`` directly. Checking both means one filter covers every
    session-scoped line without having to retrofit dozens of pre-existing
    ``audit.record`` call sites just to rename a kwarg.
    """
    read_paths = paths if paths is not None else [config.AUDIT_PATH]
    entries: list[dict] = []
    for path in read_paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if event_filter and event_filter not in entry.get("event", ""):
                    continue
                if event_exact and entry.get("event", "") != event_exact:
                    continue
                if since_ms is not None and int(entry.get("ts") or 0) < since_ms:
                    continue
                if session_ids is not None and entry.get("session") not in session_ids and entry.get("holder") not in session_ids:
                    continue
                entries.append(entry)
    return entries[-limit:]


def rotated_paths() -> list[Path]:
    """Existing rotated audit files, oldest-first (``.jsonl.<audit_keep>`` down
    to ``.jsonl.1``) — the reverse of the eviction order in ``_rotate_if_needed``,
    so a caller folding them in front of the live log gets true chronological
    order. Used by ``hermes browser-bridge export`` and available to ``logs`` callers
    that want to search further back than the live file."""
    path = config.AUDIT_PATH
    cfg = config.load()
    keep = int(cfg["audit_keep"])
    found: list[Path] = []
    for index in range(keep, 0, -1):
        candidate = path.with_suffix(f".jsonl.{index}")
        if candidate.exists():
            found.append(candidate)
    return found


def _rotate_if_needed(path: Path) -> None:
    cfg = config.load()
    max_bytes = int(cfg["audit_max_bytes"])
    keep = int(cfg["audit_keep"])
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    for index in range(keep - 1, 0, -1):
        older = path.with_suffix(f".jsonl.{index}")
        newer = path.with_suffix(f".jsonl.{index + 1}")
        if older.exists():
            older.replace(newer)
            try:
                os.chmod(newer, 0o600)
            except OSError:
                pass
    rotated = path.with_suffix(".jsonl.1")
    path.replace(rotated)
    try:
        os.chmod(rotated, 0o600)
    except OSError:
        pass
