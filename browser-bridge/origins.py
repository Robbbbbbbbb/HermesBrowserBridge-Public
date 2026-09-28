"""Dependency-free origin canonicalization.

Split out of ``tools.py`` so ``state.py`` (the grants storage layer) can
canonicalize an origin before writing or comparing it without importing
``tools.py`` — ``tools.py`` already imports ``state``, so the reverse import
would be a cycle. ``tools.py`` re-exports ``_canonicalize_origin`` from here
so every existing caller and test keeps working unchanged.

Mirrored EXACTLY by ``extension/src/lib/origin-policy.ts``'s
``canonicalizeOrigin`` — the shared vector file
``fixtures/origin-normalization-cases.json`` is what both sides' tests run
against, so the two can't silently drift.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional
from urllib.parse import urlsplit


def canonicalize_origin(origin: str) -> str:
    """Canonical form of an origin string.

    The opaque-origin sentinel ``"null"`` passes through unchanged. Lowercases
    scheme and host, strips the scheme's default port, strips a trailing dot
    from the host, and IDNA-encodes a unicode host to its ASCII (punycode)
    form. A malformed/unparseable ``origin`` passes through unchanged too
    (never raises) — it simply won't match anything in a real grant list,
    which is safe.
    """
    if not origin or origin == "null":
        return origin
    try:
        parts = urlsplit(origin)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").rstrip(".")
        try:
            host = host.encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError):
            host = host.lower()
        port = parts.port
        default_port = {"http": 80, "https": 443}.get(scheme)
        if port is not None and port == default_port:
            port = None
        netloc = host if port is None else f"{host}:{port}"
        return f"{scheme}://{netloc}"
    except ValueError:
        return origin


# Grant modes, ordered least- to most-permissive. Used when two rows in the
# same origin-keyed table canonicalize to the same origin (a pre-existing
# non-canonical row alongside a fresh canonical one, most commonly) but carry
# different modes: the MOST RESTRICTIVE recorded mode always wins, so a
# spelling variant can never be used to smuggle in a more permissive answer
# than the one actually on file for that origin.
GRANT_MODE_RANK: Dict[str, int] = {"off": 0, "request": 1, "full": 2}

# Same idea for the origin_silent_mode ("Background requests") table's own
# three-value enum — "off" is the most restrictive, "always" the least.
SILENT_MODE_RANK: Dict[str, int] = {"off": 0, "ask": 1, "always": 2}


def most_restrictive(modes: Iterable[str], rank: Dict[str, int]) -> Optional[str]:
    """The most restrictive mode present in ``modes`` per ``rank`` (lower
    rank = more restrictive), ignoring any value not in ``rank`` (defensive:
    a CHECK constraint should make an out-of-enum stored value unreachable,
    but nothing here trusts a constraint alone as the sole line of defence).
    Returns ``None`` when nothing usable was passed in.
    """
    usable = [m for m in modes if m in rank]
    if not usable:
        return None
    return min(usable, key=lambda m: rank[m])
