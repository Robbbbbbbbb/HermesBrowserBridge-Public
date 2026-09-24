"""G0.8 refusal catalogue: the gateway-side half of the shared
``(capability or code, reason) -> message`` table -- see
``extension/src/lib/refusals.ts`` for the extension-side twin and the full
rationale (both modules' docstrings/comments are kept in sync by hand, the
same discipline ``background/powers.ts`` and ``tools.py``'s
``_CAPABILITY_POWER_KEYS`` already use).

The catalogue DATA lives in ``protocol/schema.json``'s ``refusals`` key and
is emitted into ``hermes_plugin/protocol.py``'s ``REFUSALS`` by
``protocol/codegen.py``. This module is the small, hand-written formatter
both ``tools.py`` and ``session_powers.py`` call instead of building a
message string themselves.
"""
from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

from . import protocol

# Mirrors extension/src/lib/refusals.ts's ALLOWED_PARAMS exactly -- see that
# file's comment for why each key is safe to interpolate (never a secret,
# never page content).
ALLOWED_PARAMS = frozenset({
    "capability",
    "settingNames",
    "origin",
    "selector",
    "basename",
    "segment",
    "expectedViewport",
    "actualViewport",
    "method",
    "session",
    "tab",
    # speedimprovements.md A2: a `fill` field's own position in the caller's
    # request array, not page content.
    "field_index",
    "question",
})

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


class UnknownRefusalError(KeyError):
    pass


def format_refusal(reason_id: str, **params: str) -> Tuple[str, Optional[int]]:
    """Renders catalogue entry ``reason_id`` with ``params``.

    Returns ``(message, code)`` -- ``code`` is ``None`` for a UI-only notice
    with no wire error (today, only ``once_only_capability``). Raises on any
    caller/catalogue mismatch: a param the entry doesn't declare, a
    declared param not supplied, or a declared param outside
    ``ALLOWED_PARAMS`` -- these are catalogue/call-site bugs and must fail a
    test immediately, never ship a mangled or blank refusal.
    """
    entry = protocol.REFUSALS.get(reason_id)
    if entry is None:
        raise UnknownRefusalError(f"unknown refusal reason id {reason_id!r}")

    declared = set(entry["params"])
    for key in declared:
        if key not in ALLOWED_PARAMS:
            raise ValueError(f"refusal {reason_id!r} declares disallowed param {key!r}")
        if key not in params:
            raise ValueError(f"refusal {reason_id!r} requires param {key!r}, which was not supplied")
    for key in params:
        if key not in declared:
            raise ValueError(f"refusal {reason_id!r} was called with undeclared param {key!r}")

    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in params:
            raise ValueError(f"refusal {reason_id!r} template referenced undeclared param {key!r}")
        return params[key]

    message = _PLACEHOLDER_RE.sub(_sub, entry["message"])
    code_name = entry["code"]
    code = getattr(protocol, code_name) if code_name is not None else None
    return message, code


def hint_for_code(code: int) -> str:
    """Replaces ``tools.py``'s hand-maintained ``_hint_for_code`` dict --
    every entry now lives in ``protocol/schema.json``'s ``codeHints`` and is
    generated into ``protocol.CODE_HINTS``, so this is the one lookup both
    the gateway and (via SKILL.md's drift check) the bundled skill agree on.
    """
    # CODE_HINTS is keyed by the error-code NAME (e.g. "GRANT_DENIED"), but
    # callers only have the numeric code -- reverse-resolve the name via the
    # small, fixed set of code constants protocol.py already exposes.
    for key, hint in protocol.CODE_HINTS.items():
        if getattr(protocol, key, None) == code:
            return hint
    return ""
