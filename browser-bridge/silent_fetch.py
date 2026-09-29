"""silentfetch.md SF5: the gateway tool surface, ``browser_bridge_silent_fetch``.

SF1-SF4 built everything this module leans on: the extension's worker pool
and ``silent.fetch`` handler (SF1/SF2), the chunked-body relay reassembly
(SF3), and every gate this handler calls in order (SF4's ``silent_grants``).
This module owns exactly three things SF4 deliberately left to it: the tool
schema/param validation, shaping the wire result into the tool's own result
contract, and the on-disk response cache/spill (fetch_cache).

**No ``tab_id`` parameter, ever** (silentfetch.md SF2 design rule 4 / SF5.1):
origin -> worker resolution is entirely internal to the extension's pool
(SF1) and the gateway's grants (SF4). A caller that passes ``tab_id`` (or
its ``tab`` alias, used elsewhere in this plugin) is refused outright, not
silently ignored -- accepting-and-ignoring would let a caller believe this
lane is scoped to a tab when it structurally cannot be.

Check order (SF5's brief, matching FETCH_SCHEMA/handle_fetch's own shape
where the two lanes overlap):

    validate params -> silent_fetch.enabled -> ssrf_guard -> authorize_silent_fetch
    -> cache lookup (still audited, still required authorization/SSRF above)
    -> silent_rate_guard -> origin_slot -> relay.call -> decode -> verify sha256
    -> redact -> spill/preview -> audit

Every REFUSAL up to and including ``authorize_silent_fetch`` audits itself
(see silent_grants.py's own docstrings); everything after that point --
including a cache hit -- is audited here.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import audit, config, protocol, relay as relay_mod, silent_grants
from . import origins as origins_mod
from . import tools as tools_mod
from .session_powers import _decode_wire_body, _redact_fetch_body, _strip_forbidden_headers

logger = logging.getLogger(__name__)

TOOLSET = tools_mod.TOOLSET

# -- tunables -------------------------------------------------------------

DEFAULT_TIMEOUT_MS = 30000
DEFAULT_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB
# Silent Fetch rev 2 §2: config.yaml's silent_fetch.max_bytes_cap (via
# silent_grants.silent_fetch_config(), the single source of truth every
# module reads this through) is now what this clamps to -- HARD_MAX_BYTES no
# longer duplicates that ceiling as a second, independently-hard-coded 16 MiB
# constant. config.SILENT_FETCH_SANITY_MAX_BYTES is the one remaining
# absolute (correctness-only) backstop, already applied inside
# silent_fetch_config() itself, so nothing here needs to re-apply it.
HARD_MAX_BYTES = config.SILENT_FETCH_SANITY_MAX_BYTES

DEFAULT_PREVIEW_CHARS = 20000
MIN_PREVIEW_CHARS = 500
MAX_PREVIEW_CHARS = 200000

# How long the tool handler waits to acquire the per-origin serialization
# slot (silent_grants.origin_slot) before giving up with an honest timeout
# rather than blocking the calling gateway thread forever behind a stuck
# sibling request to the same origin.
ORIGIN_SLOT_TIMEOUT_S = 30.0

FETCH_CACHE_DIRNAME = "fetch_cache"

# A spill/cache filename is always exactly a lowercase sha256 hex digest plus
# .json (the meta+body record) or .bin (raw bytes, written only for a binary
# body captured with expect:'binary'). Both the path-containment assertion
# below and the pruning sweep use this to recognise "a file this module
# wrote" and never touch anything else that might live in the directory.
_DIGEST_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_FILE_RE = re.compile(r"^[0-9a-f]{64}\.(json|bin|body)$")


# -- cache/spill paths ------------------------------------------------------


def _cache_dir() -> Path:
    return config.STATE_DIR / FETCH_CACHE_DIRNAME


def _ensure_cache_dir() -> Path:
    """Create fetch_cache/ at mode 0700 if it doesn't exist yet, and enforce
    that mode even if it already existed with something looser (an operator
    hand-editing permissions, or an umask that widened a fresh mkdir)."""
    d = _cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        logger.warning("browser_bridge: could not chmod fetch_cache dir to 0700", exc_info=True)
    return d


def _cache_key(
    device_id: str, method: str, url: str, body_bytes: bytes, credentials: str, headers: Dict[str, str],
) -> str:
    """sha256(device + method + url + credentials + canonical-headers + body)
    -- SF5.3/SF5.4: device is part of the key (a plan correction over
    Resources/headless-sync-fetch.md's own ``sha256(url+method+body)``) so
    two devices never share a cached response, even for the exact same
    request.

    ``credentials`` and ``headers`` (the POST-STRIP request headers -- Cookie
    already removed, Content-Type possibly auto-added for json_body) are
    folded in too: two calls that differ only in ``Accept``/``Authorization``/
    ``Content-Type``, or in ``credentials`` (omit vs include), are DIFFERENT
    requests and must never share a cache entry, even against the same URL
    and body. Headers are canonicalised before hashing -- name lowercased,
    entries sorted -- so the digest is order-independent (a caller passing
    the same headers in a different dict-iteration order still hits) while
    still being sensitive to which headers/values are actually present.
    """
    h = hashlib.sha256()
    h.update(device_id.encode("utf-8", errors="surrogateescape"))
    h.update(b"\0")
    h.update(method.encode("utf-8", errors="surrogateescape"))
    h.update(b"\0")
    h.update(url.encode("utf-8", errors="surrogateescape"))
    h.update(b"\0")
    h.update(credentials.encode("utf-8", errors="surrogateescape"))
    h.update(b"\0")
    canonical_headers = sorted((str(k).lower(), str(v)) for k, v in (headers or {}).items())
    for name, value in canonical_headers:
        h.update(name.encode("utf-8", errors="surrogateescape"))
        h.update(b"\0")
        h.update(value.encode("utf-8", errors="surrogateescape"))
        h.update(b"\0")
    h.update(b"\0")
    h.update(body_bytes)
    return h.hexdigest()


def _digest_path(cache_dir: Path, digest: str, suffix: str) -> Path:
    """``cache_dir / f"{digest}.{suffix}"`` -- but only after asserting
    ``digest`` is exactly a 64-char lowercase hex string. This is the one
    guard standing between a future bug upstream (a malformed or attacker-
    influenced digest) and writing outside fetch_cache/; nothing calls this
    with a caller-supplied string, but the assertion costs nothing and turns
    "silently wrote somewhere unexpected" into a loud, immediate failure."""
    assert _DIGEST_HEX_RE.match(digest), f"refusing to use non-digest cache key {digest!r}"
    return cache_dir / f"{digest}.{suffix}"


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically: a temp file in the SAME
    directory (so the final ``os.replace`` is a same-filesystem rename, never
    a copy), chmod'd to ``mode`` before any content lands in the visible
    name, then renamed into place. A reader can only ever see the old
    complete file or the new complete file, never a half-written one."""
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _prune_cache(cache_dir: Path, cfg: Dict[str, Any]) -> None:
    """SF5.4 retention: oldest-first prune, on every write. Only ever touches
    files matching ``_DIGEST_FILE_RE`` -- anything else a future feature (or
    an operator) drops into fetch_cache/ is left alone.

    A cache entry is now ``<digest>.json`` plus whichever sibling
    (``.body``/``.bin``) holds its actual content -- grouped and pruned as
    ONE unit here (previously each file was aged/sized/deleted
    independently, which is exactly what could leave an orphan: a ``.json``
    surviving an age/size cut its own ``.body``/``.bin`` didn't, or vice
    versa -- see ``_cache_entry_readable``, which exists to catch whatever
    still gets past this and refuse to serve it rather than an empty body).

    Two passes per entry: age first (an entry whose NEWEST member is older
    than ``cache_retention_hours`` is gone regardless of size -- using the
    newest, not the oldest, member's mtime means a re-redaction rewrite that
    only touches ``.json``+``.body`` together, or a ``.bin`` that predates
    them and is never rewritten, doesn't make a still-fresh entry look
    stale), then size (oldest-mtime-first over the survivors) until the
    directory is back under ``cache_max_bytes`` (summed across every member
    of each entry). An entry with no ``.json`` at all -- an orphaned
    ``.body``/``.bin`` left over from some other removal path -- is always
    deleted outright: there is no meta left that could ever address it.
    """
    now = time.time()
    retention_s = float(cfg["cache_retention_hours"]) * 3600.0
    max_bytes = int(cfg["cache_max_bytes"])

    try:
        candidates = [p for p in cache_dir.iterdir() if _DIGEST_FILE_RE.match(p.name)]
    except OSError:
        return

    groups: Dict[str, List[Path]] = {}
    for p in candidates:
        groups.setdefault(p.stem, []).append(p)

    entries: List[Tuple[List[Path], float, int, bool]] = []
    for paths in groups.values():
        newest_mtime = 0.0
        total_size = 0
        has_json = False
        for p in paths:
            if p.suffix == ".json":
                has_json = True
            try:
                st = p.stat()
            except OSError:
                continue
            newest_mtime = max(newest_mtime, st.st_mtime)
            total_size += st.st_size
        entries.append((paths, newest_mtime, total_size, has_json))

    def _delete_all(paths: List[Path]) -> None:
        for p in paths:
            with contextlib.suppress(OSError):
                p.unlink()

    survivors: List[Tuple[List[Path], float, int]] = []
    for paths, newest_mtime, size, has_json in entries:
        if not has_json or now - newest_mtime > retention_s:
            _delete_all(paths)
        else:
            survivors.append((paths, newest_mtime, size))

    survivors.sort(key=lambda e: e[1])  # oldest (newest-member) mtime first
    total = sum(e[2] for e in survivors)
    i = 0
    while total > max_bytes and i < len(survivors):
        paths, _mtime, size = survivors[i]
        _delete_all(paths)
        total -= size
        i += 1


def _write_spill(
    cache_dir: Path, digest: str, meta: Dict[str, Any], body_text: Optional[str], body_bytes: Optional[bytes],
) -> Path:
    """Write ``fetch_cache/<digest>.json`` (meta ONLY -- Silent Fetch rev 2
    §2 memory fix) plus, depending on shape, a sibling file holding the
    actual body:

      - ``<digest>.bin``: raw bytes, only when ``body_bytes`` is given
        (binary body AND ``expect:'binary'`` -- SF5.3's original carve-out,
        unchanged).
      - ``<digest>.body``: the body's own UTF-8 bytes verbatim, for a
        textual body of any size.

    The body no longer rides inside the json record the way SF5.3 originally
    wrote it (``{"meta": ..., "body": body_text}``): ``json.dumps`` on a dict
    containing a 100 MB string duplicates that string in memory just to
    escape it, for no benefit -- writing it as its own file, with no JSON
    escaping at all, avoids that copy entirely. ``_load_spill_body_text``
    still reads an OLDER on-disk record's inline ``"body"`` key straight out
    of ``meta``'s sibling dict for backward compatibility -- no migration of
    existing cache entries is needed, they just cost one extra copy on their
    (few) remaining reads until they age out.

    Returns the path the RESULT's ``body_file`` should point at: the actual
    downloadable payload (``.bin``/``.body``) when one was written, else the
    ``.json`` record itself (an empty/binary-not-spilled body has nothing
    else to point at)."""
    json_path = _digest_path(cache_dir, digest, "json")
    _atomic_write(json_path, json.dumps({"meta": meta}, default=str).encode("utf-8"))
    if body_bytes is not None:
        bin_path = _digest_path(cache_dir, digest, "bin")
        _atomic_write(bin_path, body_bytes)
        return bin_path
    if body_text is not None:
        body_path = _digest_path(cache_dir, digest, "body")
        _atomic_write(body_path, body_text.encode("utf-8"))
        return body_path
    return json_path


def _load_spill_body_text(cache_dir: Path, digest: str, record: Dict[str, Any]) -> str:
    """Reconstruct a cached text body for ``_reconstruct_from_cache``.

    A NEW-format record (``_write_spill`` above) carries no ``"body"`` key at
    all -- the text lives in a sibling ``<digest>.body`` file, read here
    verbatim (no JSON parsing of a giant string). An OLDER on-disk record
    (written before this change) still carries it inline under ``"body"``,
    and that path keeps working unmigrated: this is the one place that
    difference is resolved, so nothing else needs to know two formats exist.

    Only ever called after ``_cache_entry_readable`` has already confirmed
    the sibling file exists (an orphaned entry is filtered out as a cache
    MISS well before this point -- see that function's own docstring), so
    the ``except OSError`` below is belt-and-braces against a file vanishing
    in the narrow window between that check and this read (a concurrent
    prune), not the normal way a missing file is handled.
    """
    if "body" in record:
        return record.get("body") or ""
    try:
        return _digest_path(cache_dir, digest, "body").read_text(encoding="utf-8")
    except OSError:
        return ""


def _cache_entry_readable(cache_dir: Path, digest: str, record: Dict[str, Any], expect: str) -> bool:
    """False means this cache entry is ORPHANED -- its ``.json`` record
    survived (pruning previously handled ``.json``/``.body``/``.bin``
    independently, so one sibling could be gone while the others remained)
    but the actual body content it needs to answer THIS call isn't there.
    Silently serving an empty (or wrong) body would be worse than a plain
    cache miss, so the caller treats ``False`` here exactly like
    ``_cache_lookup`` returning ``None``: fall through to a live fetch.

    - An OLD-format record (body embedded inline under ``"body"``) never
      depends on a sibling file at all -- always readable.
    - A binary record only needs its ``.bin`` sibling when THIS call asks
      for ``expect: 'binary'`` (the one shape that actually returns bytes);
      any other ``expect`` reconstructs a binary hit with ``body: null`` and
      no file dependency, exactly as a live (non-spilled) auto-detected
      binary response already does.
    - A text record always needs its ``.body`` sibling -- the ONLY place its
      content lives in the new format.
    """
    meta = record.get("meta") or {}
    if "body" in record:
        return True
    if meta.get("binary"):
        return expect != "binary" or _digest_path(cache_dir, digest, "bin").exists()
    return _digest_path(cache_dir, digest, "body").exists()


def _cache_lookup(cache_dir: Path, digest: str, cache_ttl_s: Optional[float]) -> Optional[Dict[str, Any]]:
    """None if caching wasn't requested, no spill exists yet, the spill is
    older than ``cache_ttl_s``, or the record is unreadable/corrupt -- every
    one of those is a plain cache miss, never an error."""
    if not cache_ttl_s or cache_ttl_s <= 0:
        return None
    json_path = _digest_path(cache_dir, digest, "json")
    try:
        st = json_path.stat()
    except OSError:
        return None
    if (time.time() - st.st_mtime) > cache_ttl_s:
        return None
    try:
        with json_path.open("r", encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("meta"), dict):
        return None
    return record


# -- request shaping ---------------------------------------------------------

_VALID_EXPECT = ("auto", "text", "json", "binary")
_VALID_CREDENTIALS = ("include", "same-origin", "omit")


def _shape_body(
    payload: Dict[str, Any],
    raw_bytes: bytes,
    device_id: str,
    origin: str,
    expect: str,
    preview_chars: int,
    content_type: str,
    wire_truncated: bool,
) -> Tuple[Optional[str], Optional[bytes], int]:
    """Fills in ``payload``'s body-shaped fields (``binary``, and one of
    ``body_json``/``body``/``body_preview``+``preview_truncated``, plus
    ``sha256_16`` for a binary body) exactly the way
    ``session_powers.handle_fetch`` shapes ``browser_bridge_fetch``'s own
    response, so a caller already familiar with that tool sees the same
    shape here. Returns ``(text_for_spill_or_None, raw_bytes_for_spill_or_None,
    redaction_hits)`` -- the two "for spill" values are always the FULL
    (untruncated) representation, regardless of ``preview_chars``, so a
    later cache hit can re-derive the preview split under whatever
    ``preview_chars``/``expect`` THAT call asked for rather than being stuck
    with the first call's choice.
    """
    is_binary = expect == "binary"
    text: Optional[str] = None
    if not is_binary:
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            is_binary = True

    if is_binary:
        payload["binary"] = True
        payload["body"] = None
        payload["sha256_16"] = hashlib.sha256(raw_bytes).hexdigest()[:16]
        raw_for_spill = raw_bytes if expect == "binary" else None
        return None, raw_for_spill, 0

    assert text is not None
    text, redactions = _redact_fetch_body(text, device_id)
    payload["binary"] = False
    payload["total_chars"] = len(text)
    over_preview = len(text) > preview_chars
    looks_json = "json" in content_type.lower() or text.lstrip()[:1] in ("{", "[")
    parsed_json = None
    if (expect == "json" or (expect == "auto" and looks_json)) and not wire_truncated and not over_preview:
        try:
            parsed_json = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            parsed_json = None
    if over_preview:
        payload["body_preview"] = text[:preview_chars]
        payload["preview_truncated"] = True
    elif parsed_json is not None:
        payload["body_json"] = parsed_json
        payload["preview_truncated"] = False
    else:
        payload["body"] = text
        payload["preview_truncated"] = False
    return text, None, redactions


def _reconstruct_from_cache(
    cache_dir: Path, digest: str, record: Dict[str, Any], device_id: str, expect: str, preview_chars: int,
    spill_path: Path,
) -> Dict[str, Any]:
    """Cache-hit path: re-derive the SAME shaping ``_shape_body`` would have
    produced, from the full (untruncated) text/bytes the spill record holds,
    under THIS call's own ``expect``/``preview_chars`` -- not whatever the
    original call happened to ask for.

    Also re-runs redaction (``_redact_fetch_body``) against the stored text
    under the device's CURRENT policy, not the policy in force when the
    response was first spilled: a cache hit must never resurface a value
    that used to be redacted and still should be, just because the user
    later flips a redaction kind on after the entry was written (the kind
    was already off when it was cached, but the record predates the flip
    the other way just as easily -- either way, "policy at write time" is
    the wrong thing to trust for something served again later, unredacted,
    without ever touching the browser). ``redactions`` in the result is the
    hits already counted at write time PLUS any new hits this pass finds.
    If re-redaction changes the text, the on-disk spill record is rewritten
    atomically (same tmp-file + ``os.replace`` as the original write, same
    0600 mode) so ``body_file`` -- and any FUTURE cache hit -- reflects the
    now-current policy too, not just this one response.
    """
    meta = dict(record["meta"])
    payload: Dict[str, Any] = {k: v for k, v in meta.items() if k not in ("binary", "body_json", "body", "body_preview", "preview_truncated", "sha256_16", "total_chars", "redactions")}
    payload["device_id"] = device_id
    stored_redactions = int(meta.get("redactions") or 0)

    if meta.get("binary"):
        payload["binary"] = True
        payload["body"] = None
        payload["redactions"] = stored_redactions
        if "sha256_16" in meta:
            payload["sha256_16"] = meta["sha256_16"]
        if spill_path.suffix == ".bin":
            payload["body_file"] = str(spill_path)
    else:
        text = _load_spill_body_text(cache_dir, digest, record)
        text, new_redactions = _redact_fetch_body(text, device_id)
        total_redactions = stored_redactions + new_redactions
        if new_redactions:
            # The device's policy has widened since this response was
            # spilled (a kind that was off is now on) -- rewrite the record
            # in place so the file on disk, and every future cache hit,
            # matches the current policy rather than the stale one. Same
            # meta-only-json-plus-.body-sibling shape as _write_spill's own
            # write (never re-embed the (possibly huge) text into the json
            # record -- that would reintroduce the exact duplication-on-
            # write problem this format change exists to avoid, and on every
            # cache hit that finds new redactions, not just the first write).
            updated_meta = dict(meta)
            updated_meta["redactions"] = total_redactions
            try:
                json_path = _digest_path(cache_dir, digest, "json")
                _atomic_write(json_path, json.dumps({"meta": updated_meta}, default=str).encode("utf-8"))
                _atomic_write(_digest_path(cache_dir, digest, "body"), text.encode("utf-8"))
            except Exception:
                logger.exception(
                    "browser_bridge: silent_fetch cache-hit re-redaction rewrite failed for digest=%s", digest
                )
        payload["binary"] = False
        payload["total_chars"] = len(text)
        payload["redactions"] = total_redactions
        content_type = str(meta.get("content_type") or "")
        wire_truncated = bool(meta.get("wire_truncated"))
        over_preview = len(text) > preview_chars
        looks_json = "json" in content_type.lower() or text.lstrip()[:1] in ("{", "[")
        parsed_json = None
        if (expect == "json" or (expect == "auto" and looks_json)) and not wire_truncated and not over_preview:
            try:
                parsed_json = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                parsed_json = None
        if over_preview:
            payload["body_preview"] = text[:preview_chars]
            payload["preview_truncated"] = True
            payload["body_file"] = str(spill_path)
        elif parsed_json is not None:
            payload["body_json"] = parsed_json
            payload["preview_truncated"] = False
        else:
            payload["body"] = text
            payload["preview_truncated"] = False
    payload["from_cache"] = True
    return payload


# -- schema -------------------------------------------------------------------

SILENT_FETCH_SCHEMA = {
    "name": "browser_bridge_silent_fetch",
    "description": (
        "Headless origin fetch: runs an HTTP request from a hidden, minimized worker tab on the "
        "target origin -- Chrome's own network stack does the work (cookies, TLS/H2/H3 fingerprint, "
        "sec-fetch-*), with no attach, no lease, and no tab id ever crossing this tool's surface. Use "
        "this for bulk/paginated background fetching against an origin the user has granted -- for "
        "interactive work against a tab already on screen, use browser_bridge_fetch instead. Every "
        "call is gated by that origin's 'Background requests' popup setting (or the plugin-wide "
        "full_implies_silent default), rate-limited per origin, and audited more heavily than the "
        "interactive lane because it runs unattended. Large responses are written whole to a local "
        "file (`body_file`) instead of blowing your context -- read it with execute_code for paging "
        "loops. `cache_ttl_s` lets a repeated call within that window skip the browser round trip "
        "entirely (`from_cache:true`). For small request bursts the same origin grants the agent a "
        "cookie-export alternative (see cookies `include_values`); the bridge is preferred for "
        "long/unattended sweeps."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "url": {"type": "string", "description": "Absolute URL to request."},
            "method": {"type": "string", "description": "HTTP method. Default GET."},
            "headers": {
                "type": "object",
                "description": "Extra request headers as string->string. A `Cookie` header is always stripped -- cookies come from the worker tab's own jar.",
            },
            "body": {"type": "string", "description": "Raw request body. Mutually exclusive with json_body."},
            "json_body": {
                "type": "object",
                "description": "Convenience for JSON APIs: an object here is JSON-encoded and given Content-Type: application/json if you didn't set one.",
            },
            "credentials": {
                "type": "string",
                "enum": list(_VALID_CREDENTIALS),
                "description": "Cookie/session inclusion policy for the worker tab. Default include.",
            },
            "expect": {
                "type": "string",
                "enum": list(_VALID_EXPECT),
                "description": (
                    "auto: sniff content-type/decodability. text/json: decode as UTF-8 (json also tries to "
                    "parse it). binary: always treated as raw bytes, never decoded as text -- spilled whole "
                    "to body_file rather than summarised."
                ),
            },
            "max_bytes": {
                "type": "integer",
                "description": (
                    f"Cap on bytes captured off the wire. Default {DEFAULT_MAX_BYTES}. Hard-capped at this "
                    f"gateway's configured silent_fetch.max_bytes_cap (config.yaml; "
                    f"{silent_grants.silent_fetch_config()['max_bytes_cap']} bytes right now), itself bounded "
                    f"by a {HARD_MAX_BYTES}-byte correctness ceiling no config value can exceed."
                ),
            },
            "timeout_ms": {"type": "integer", "description": f"Request timeout. Default {DEFAULT_TIMEOUT_MS}."},
            "preview_chars": {
                "type": "integer",
                "description": f"Cap on how much TEXT lands inline in this result. Default {DEFAULT_PREVIEW_CHARS}. Above this, the full body is spilled to body_file.",
            },
            "cache_ttl_s": {
                "type": "number",
                "description": "If set and a cached response for this exact (device, method, url, body) is younger than this many seconds, return it with from_cache:true and skip the browser round trip.",
            },
            "bootstrap_path": {
                "type": "string",
                "description": "Overrides the worker's default bootstrap path (the origin root `/`) it navigates to once before fetching, so the site's own bot-manager JS runs and sets cookies.",
            },
            "bootstrap_timeout_ms": {
                "type": "integer",
                "description": (
                    "Overrides how long the worker may take to launch and bootstrap (commit a document at "
                    "this origin and reach DOMContentLoaded) before this call fails with "
                    "SILENT_WORKER_LAUNCH_FAILED -- for THIS call only, never the gateway's standing default. "
                    f"Clamped to [{config.SILENT_FETCH_MIN_BOOTSTRAP_TIMEOUT_MS}, "
                    f"{config.SILENT_FETCH_MAX_BOOTSTRAP_TIMEOUT_MS}]ms. Default, when omitted: this "
                    f"gateway's configured silent_fetch.bootstrap_timeout_ms "
                    f"({silent_grants.silent_fetch_config()['bootstrap_timeout_ms']}ms right now). Raise this "
                    "for a site with a slow interstitial/redirect chain (an Akamai bot check, an SSO hop) "
                    "rather than passing a longer timeout_ms, which covers only the fetch itself, not the "
                    "worker's one-time bootstrap."
                ),
            },
        },
        "required": ["url"],
    },
}


def reclaim_result(fields: Dict[str, Any], device_id: str, origin: str, trigger: str, method: str) -> str:
    """Send the best-effort ``silent.kill {origin}`` for ``trigger`` and fold
    the outcome into an error result (``fields`` is a ``_err_dict``). Shared
    with silent_evaluate. Never raises."""
    outcome = silent_grants.reclaim_worker(device_id, origin, trigger=trigger, method=method)
    fields["worker_reclaimed"] = bool(outcome["sent"])
    if outcome["sent"]:
        if trigger == "relay_timeout":
            note = (
                "the gateway stopped waiting for this call, so it asked the extension to close this origin's "
                "background worker; a retry gets a fresh worker"
            )
        else:
            note = (
                "this origin's background worker looked stuck from an earlier call the gateway already gave up "
                "on, so it was closed; retry once and it gets a fresh worker"
            )
        fields["hint"] = f"{note}. {fields['hint']}" if fields.get("hint") else note
    else:
        fields["worker_reclaim_error"] = outcome["error"]
        note = (
            "the gateway could not reach the extension to reclaim this origin's worker, so it may stay busy; "
            "call browser_bridge_silent_kill for this origin or ask the operator to run "
            "`hermes browser-bridge silent kill`"
        )
        fields["hint"] = f"{fields['hint']} {note}" if fields.get("hint") else note
    return json.dumps(fields, default=str)


def bridge_err_with_reclaim(exc: "relay_mod.BridgeError", device_id: str, origin: str, method: str) -> str:
    """``tools._bridge_err`` plus the worker-reclaim policy: a relay TIMEOUT
    (the gateway gave up waiting) reclaims the worker; a SILENT_WORKER_BUSY the
    gateway did not cause (nothing of its own holds the origin's slot) means the
    extension's worker is stuck from a call already abandoned, so reclaim it
    once. 4264 (eval timeout) and 4259 (killed) are never reclaimed -- the
    extension already recycled the worker itself."""
    fields = tools_mod._bridge_err_dict(exc, method)
    if exc.code == protocol.TIMEOUT:
        return reclaim_result(fields, device_id, origin, "relay_timeout", method)
    if exc.code == protocol.SILENT_WORKER_BUSY and not silent_grants.slot_in_flight(device_id, origin):
        return reclaim_result(fields, device_id, origin, "orphaned_busy", method)
    return json.dumps(fields, default=str)


def handle_silent_fetch(args: Dict[str, Any], **kwargs: Any) -> str:
    # -- no tab surface, ever (SF5.1) -----------------------------------
    if args.get("tab_id") is not None:
        return tools_mod._err(
            "browser_bridge_silent_fetch never takes tab_id -- origin -> worker resolution is entirely "
            "internal (the extension's own hidden worker pool). Use browser_bridge_fetch against an "
            "attached tab if you need tab-scoped behaviour.",
            code=protocol.INVALID_PARAMS,
        )
    if args.get("tab"):
        return tools_mod._err(
            "browser_bridge_silent_fetch never takes a tab alias -- this lane has no tab surface at all.",
            code=protocol.INVALID_PARAMS,
        )

    device_id, err = tools_mod._resolve_device(args, kwargs)
    if err:
        return err

    url = str(args.get("url") or "").strip()
    if not url:
        return tools_mod._err("url is required", code=protocol.INVALID_PARAMS)

    method = str(args.get("method") or "GET").strip().upper() or "GET"

    body = args.get("body")
    json_body = args.get("json_body")
    if body is not None and json_body is not None:
        return tools_mod._err("pass body or json_body, not both", code=protocol.INVALID_PARAMS)

    headers_in = args.get("headers") or {}
    if not isinstance(headers_in, dict):
        return tools_mod._err("headers must be an object of string -> string", code=protocol.INVALID_PARAMS)
    headers, stripped_headers = _strip_forbidden_headers(headers_in)

    if json_body is not None:
        try:
            body = json.dumps(json_body)
        except (TypeError, ValueError) as exc:
            return tools_mod._err(f"json_body is not JSON-serialisable: {exc}", code=protocol.INVALID_PARAMS)
        if not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "application/json"

    credentials = args.get("credentials") or "include"
    if credentials not in _VALID_CREDENTIALS:
        return tools_mod._err(
            f"credentials must be one of {', '.join(_VALID_CREDENTIALS)}", code=protocol.INVALID_PARAMS
        )

    expect = args.get("expect") or "auto"
    if expect not in _VALID_EXPECT:
        return tools_mod._err(f"expect must be one of {', '.join(_VALID_EXPECT)}", code=protocol.INVALID_PARAMS)

    cfg = silent_grants.silent_fetch_config()

    timeout_ms = int(args.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
    if timeout_ms <= 0:
        timeout_ms = DEFAULT_TIMEOUT_MS

    # cfg["max_bytes_cap"] is already sanity-clamped by
    # silent_grants.silent_fetch_config() itself -- no second min() needed
    # here, single source of truth.
    effective_max_bytes_cap = int(cfg["max_bytes_cap"])
    max_bytes = int(args.get("max_bytes") or DEFAULT_MAX_BYTES)
    max_bytes = max(1024, min(max_bytes, effective_max_bytes_cap))

    preview_chars = int(args.get("preview_chars") or DEFAULT_PREVIEW_CHARS)
    preview_chars = max(MIN_PREVIEW_CHARS, min(preview_chars, MAX_PREVIEW_CHARS))

    cache_ttl_s = args.get("cache_ttl_s")
    if cache_ttl_s is not None:
        try:
            cache_ttl_s = float(cache_ttl_s)
        except (TypeError, ValueError):
            return tools_mod._err("cache_ttl_s must be a number of seconds", code=protocol.INVALID_PARAMS)
        if cache_ttl_s < 0:
            return tools_mod._err("cache_ttl_s must not be negative", code=protocol.INVALID_PARAMS)

    bootstrap_path = args.get("bootstrap_path")
    if bootstrap_path is not None:
        bootstrap_path = str(bootstrap_path)

    # Rev 3 §D: a per-call override of the worker-launch timeout, clamped
    # the same [5000,120000]ms range the gateway-wide default is (shared
    # helper -- see silent_grants.clamp_bootstrap_timeout_ms's own
    # docstring). Omitted entirely from wire_params below when the caller
    # didn't ask for one, so the extension falls back to whatever it last
    # heard on hello/heartbeat rather than this call silently re-asserting
    # that same default.
    bootstrap_timeout_ms_arg = args.get("bootstrap_timeout_ms")
    bootstrap_timeout_ms: Optional[int] = None
    if bootstrap_timeout_ms_arg is not None:
        bootstrap_timeout_ms = silent_grants.clamp_bootstrap_timeout_ms(
            bootstrap_timeout_ms_arg, cfg["bootstrap_timeout_ms"]
        )

    origin = origins_mod.canonicalize_origin(tools_mod._origin_of(url))
    if not origin:
        return tools_mod._err(f"url is not a valid absolute URL: {url!r}", code=protocol.INVALID_PARAMS)

    # -- fleet-wide kill switch (SF5.5's config.py enabled key) ----------
    if not cfg["enabled"]:
        silent_grants.record_silent_fetch_refused(device_id, origin, method, url, reason_class="disabled")
        return tools_mod._err(
            "browser_bridge_silent_fetch is disabled on this gateway (silent_fetch.enabled: false in "
            "config.yaml)",
            code=protocol.SILENT_ORIGIN_NOT_GRANTED, device_id=device_id, origin=origin,
        )

    # -- SSRF (SF4.3) -----------------------------------------------------
    guard = silent_grants.ssrf_guard(url, device_id)
    if guard is not None:
        reason, code, audit_class = guard
        silent_grants.record_silent_fetch_refused(device_id, origin, method, url, reason_class=audit_class)
        return tools_mod._err(reason, code=code, device_id=device_id, origin=origin)

    holder, err = tools_mod._resolve_session(device_id, kwargs)
    if err:
        return err

    body_bytes = body.encode("utf-8", errors="surrogateescape") if body else b""
    summary = f"background {method} {url}"
    detail = json.dumps(
        {
            "method": method, "url": url, "credentials": credentials, "expect": expect,
            "header_keys": sorted(headers.keys()), "body_bytes": len(body_bytes),
            "stripped_headers": stripped_headers,
        },
        default=str,
    )

    # -- grants (SF4.2) ----------------------------------------------------
    denial = silent_grants.authorize_silent_fetch(device_id, url, method, holder, summary, detail)
    if denial is not None:
        reason, code, extra = denial
        return tools_mod._err(reason, code=code, device_id=device_id, origin=origin, **(extra or {}))

    # -- cache lookup (SF5.4) -- still gated by everything above ----------
    cache_dir = _ensure_cache_dir()
    digest = _cache_key(device_id, method, url, body_bytes, credentials, headers)
    cached = _cache_lookup(cache_dir, digest, cache_ttl_s)
    if cached is not None and not _cache_entry_readable(cache_dir, digest, cached, expect):
        # Orphaned spill: the .json record survived but the sibling file
        # that actually holds the body (or, for a binary hit under
        # expect:'binary', the .bin) is gone -- pruned independently, or
        # removed by hand. Treat this exactly like a plain cache miss (never
        # serve an empty/wrong body silently) and fall through to a live
        # fetch below, which will also re-spill a fresh, complete entry.
        cached = None
    if cached is not None:
        meta = cached["meta"]
        bin_path = _digest_path(cache_dir, digest, "bin")
        body_path = _digest_path(cache_dir, digest, "body")
        if meta.get("binary") and bin_path.exists():
            spill_path = bin_path
        elif body_path.exists():
            spill_path = body_path
        else:
            # Old-format record (body embedded inline in the json) or an
            # empty/never-spilled body -- the json record is the only file
            # that could possibly exist for it.
            spill_path = _digest_path(cache_dir, digest, "json")
        payload = _reconstruct_from_cache(cache_dir, digest, cached, device_id, expect, preview_chars, spill_path)
        silent_grants.record_silent_fetch(
            device_id, holder, origin, method, url,
            status=meta.get("status"), total_bytes=int(meta.get("total_bytes") or 0),
            from_cache=True, redactions=int(payload.get("redactions") or 0),
        )
        return tools_mod._ok(**payload)

    # -- rate guard (SF4.4) -------------------------------------------------
    rl = silent_grants.silent_rate_guard(device_id, origin)
    if rl is not None:
        reason, code, retry_after = rl
        return tools_mod._err(reason, code=code, device_id=device_id, origin=origin, retry_after_s=retry_after)

    wire_params: Dict[str, Any] = {
        "url": url,
        "method": method,
        "credentials": credentials,
        "expect": expect,
        "timeout_ms": timeout_ms,
        "max_bytes": max_bytes,
    }
    if headers:
        wire_params["headers"] = headers
    if body is not None:
        wire_params["body"] = body
    if bootstrap_path:
        wire_params["bootstrap_path"] = bootstrap_path
    if bootstrap_timeout_ms is not None:
        wire_params["bootstrap_timeout_ms"] = bootstrap_timeout_ms

    relay = relay_mod.get_relay()
    # Rev 3 §D: the relay's own wait must cover the worker's bootstrap PLUS
    # the fetch itself, worst case sequentially -- timeout_ms alone (the
    # pre-rev-3 bug this whole task exists to fix) never reached the
    # bootstrap, so a slow launch could blow past the relay's wait even
    # though the extension itself was still legitimately within its own
    # (now-configurable) launch budget.
    effective_bootstrap_timeout_ms = bootstrap_timeout_ms if bootstrap_timeout_ms is not None else cfg["bootstrap_timeout_ms"]
    relay_timeout_s = (timeout_ms + effective_bootstrap_timeout_ms) / 1000.0 + 15
    in_relay_call = False
    try:
        with silent_grants.origin_slot(device_id, origin, timeout_s=ORIGIN_SLOT_TIMEOUT_S):
            in_relay_call = True
            result = relay.call(device_id, "silent.fetch", wire_params, timeout=relay_timeout_s)
    except silent_grants.SilentWorkerBusy as exc:
        silent_grants.record_silent_fetch_refused(device_id, origin, method, url, reason_class="worker_busy")
        return tools_mod._err(
            f"{exc}; wait for it to finish and retry", code=protocol.SILENT_WORKER_BUSY,
            device_id=device_id, origin=origin,
        )
    except TimeoutError as exc:
        if in_relay_call:
            # the relay wait itself expired (not the slot wait): same as a relay TIMEOUT
            return reclaim_result(
                tools_mod._err_dict(str(exc), code=protocol.TIMEOUT, device_id=device_id, origin=origin),
                device_id, origin, "relay_timeout", "silent.fetch",
            )
        silent_grants.record_silent_fetch_refused(device_id, origin, method, url, reason_class="origin_slot_timeout")
        return tools_mod._err(str(exc), code=protocol.TIMEOUT, device_id=device_id, origin=origin)
    except relay_mod.BridgeError as exc:
        if exc.code in (protocol.METHOD_NOT_FOUND, protocol.UNSUPPORTED_METHOD):
            silent_grants.record_silent_fetch_refused(device_id, origin, method, url, reason_class="extension_outdated")
            return tools_mod._err(
                "the connected extension build does not support background (silent.fetch) requests -- "
                "reload the extension in chrome://extensions and reconnect, then retry",
                code=exc.code, device_id=device_id, origin=origin,
            )
        silent_grants.record_silent_fetch_refused(device_id, origin, method, url, reason_class="bridge_error")
        # Silent Fetch rev 4 §B(b): a TIMEOUT carrying the extension's own
        # diagnostic snapshot (silent-fetch.ts's buildTimeoutResult, forwarded
        # here as exc.data via offscreen.ts's requireOk/client.ts's error.data
        # -- see relay.py's BridgeError) gets its own, more specific audit
        # event on top of the generic silent_fetch_refused line above.
        if exc.code == protocol.TIMEOUT and isinstance(exc.data, dict):
            silent_grants.record_silent_fetch_failed(
                device_id, origin,
                phase=exc.data.get("phase"),
                elapsed_ms=exc.data.get("phase_elapsed_ms"),
                bytes_so_far=exc.data.get("bytes_so_far"),
                fetch_native=exc.data.get("fetch_native"),
            )
        return bridge_err_with_reclaim(exc, device_id, origin, "silent.fetch")

    status = result.get("status")
    resp_headers = result.get("headers") or {}
    wire_truncated = bool(result.get("wire_truncated"))
    total_bytes_source = result.get("total_bytes_source") or "stream"
    set_cookie_names = result.get("set_cookie_names") or []
    set_cookie_count = result.get("set_cookie_count", len(set_cookie_names))
    timing = result.get("timing") or {}

    # Memory (Silent Fetch rev 2 §2): a chunked/reassembled result carries the
    # already-decoded bytes straight from relay.py's Connection.resolve()
    # (`body_bytes`) -- use them directly rather than re-decoding a base64
    # string that only exists at all for a small, single-frame body. Avoids
    # two full-body-sized copies (the base64 encode relay.py no longer does,
    # and this decode) for exactly the large-body case this cap increase is
    # for.
    if isinstance(result.get("body_bytes"), (bytes, bytearray)):
        # No intermediate local variable holding result["body_bytes"]
        # itself: `bytes(...)` on an already-`bytes` value returns that SAME
        # object (no copy), so raw_bytes and result["body_bytes"] name one
        # object between them -- if a temporary held a THIRD reference to it
        # here, clearing result["body_bytes"] below would not be enough to
        # let the later `del raw_bytes` actually free it (a lingering local
        # would still be pinning it for the rest of this function).
        raw_bytes = bytes(result["body_bytes"])
        # `result` itself lives for the rest of this function (status/
        # headers/timing/etc. are all read from it below) -- dropping ITS
        # reference to the (possibly 100+ MB) body right away means the one
        # remaining reference (`raw_bytes`) is the only thing keeping these
        # bytes alive, so the later `del raw_bytes` actually frees them.
        result["body_bytes"] = None
    else:
        raw_bytes = _decode_wire_body(result.get("body") or "", str(result.get("bodyEncoding") or ""))

    # -- honesty check: trust nothing the extension SAYS about the bytes
    #    without checking it against the bytes actually reassembled --------
    reported_sha256 = result.get("sha256")
    if reported_sha256:
        actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        if actual_sha256.lower() != str(reported_sha256).lower():
            silent_grants.record_silent_fetch_refused(device_id, origin, method, url, reason_class="sha256_mismatch")
            return tools_mod._err(
                f"sha256 mismatch: the extension reported {reported_sha256}, but the gateway reassembled "
                f"{actual_sha256} from the wire -- refusing to trust this response body rather than "
                f"silently passing on bytes that may not be what was actually sent",
                code=protocol.INTERNAL_ERROR, device_id=device_id, origin=origin,
            )

    content_type = ""
    for key, value in resp_headers.items():
        if str(key).lower() == "content-type":
            content_type = str(value)
            break

    reported_total_bytes = result.get("total_bytes")
    payload: Dict[str, Any] = {
        "device_id": device_id,
        "url": url,
        "method": method,
        "status": status,
        "ok": isinstance(status, int) and 200 <= status < 300,
        "headers": resp_headers,
        "content_type": content_type,
        "total_bytes": int(reported_total_bytes) if isinstance(reported_total_bytes, (int, float)) else len(raw_bytes),
        "total_bytes_source": total_bytes_source,
        "wire_truncated": wire_truncated,
        "set_cookie_names": set_cookie_names,
        "set_cookie_count": set_cookie_count,
        "timing": timing,
        "from_cache": False,
    }
    if stripped_headers:
        payload["stripped_request_headers"] = stripped_headers

    body_text, body_bytes_for_spill, redactions = _shape_body(
        payload, raw_bytes, device_id, origin, expect, preview_chars, content_type, wire_truncated,
    )
    # Memory (Silent Fetch rev 2 §2): raw_bytes has done its job (decoded to
    # body_text, or handed off as body_bytes_for_spill for a binary body --
    # either way _shape_body already captured whatever it needed) and is
    # never read again below. Dropping this name lets CPython's refcounting
    # free it immediately rather than keeping a full extra body-sized copy
    # alive for the rest of the function (through redaction, spill write and
    # the audit call) for no reason.
    del raw_bytes
    if redactions:
        audit.record(
            "redaction_gateway_catch", device=device_id, origin=origin, hits=redactions, capability="silent_fetch",
        )
    payload["redactions"] = redactions

    exposes_body_file = bool(payload.get("preview_truncated")) or (payload.get("binary") and expect == "binary")
    needs_spill_write = exposes_body_file or bool(cache_ttl_s)

    if needs_spill_write:
        meta = {k: v for k, v in payload.items() if k not in ("body", "body_json", "body_preview")}
        try:
            spill_path = _write_spill(cache_dir, digest, meta, body_text, body_bytes_for_spill)
            if exposes_body_file:
                payload["body_file"] = str(spill_path)
            _prune_cache(cache_dir, cfg)
        except Exception:
            logger.exception("browser_bridge: silent_fetch spill write failed for digest=%s", digest)

    silent_grants.record_silent_fetch(
        device_id, holder, origin, method, url, status=status,
        total_bytes=payload["total_bytes"], from_cache=False, redactions=redactions,
    )
    return tools_mod._ok(**payload)


SILENT_KILL_SCHEMA: Dict[str, Any] = {
    "name": "browser_bridge_silent_kill",
    "description": (
        "Close the hidden background worker for one origin so the next browser_bridge_silent_fetch / "
        "browser_bridge_silent_evaluate to it gets a fresh worker. Use it when those calls keep failing with "
        "SILENT_WORKER_BUSY (4265) after an earlier call timed out. Always safe: it only closes the hidden "
        "worker tab (an in-flight call to that origin fails with 4259); it never touches the user's own tabs. "
        "The gateway already does this automatically after a timeout, so you rarely need it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "origin": {"type": "string", "description": "The origin (or any URL on it) whose worker to close, e.g. https://example.com."},
            "device_id": {"type": "string", "description": "Which paired browser. Omit to use the default device."},
        },
        "required": ["origin"],
    },
}


def handle_silent_kill(args: Dict[str, Any], **kwargs: Any) -> str:
    raw = str(args.get("origin") or "").strip()
    if not raw:
        return tools_mod._err("origin is required", code=protocol.INVALID_PARAMS)
    origin = origins_mod.canonicalize_origin(tools_mod._origin_of(raw))
    if not origin:
        return tools_mod._err(f"origin is not a valid absolute URL or origin: {raw!r}", code=protocol.INVALID_PARAMS)
    device_id, err = tools_mod._resolve_device(args, kwargs)
    if err:
        return err
    outcome = silent_grants.reclaim_worker(device_id, origin, trigger="agent", method="silent.kill")
    if not outcome["sent"]:
        return tools_mod._err(
            f"could not send silent.kill to the extension: {outcome['error']}",
            code=protocol.INTERNAL_ERROR, device_id=device_id, origin=origin,
        )
    return tools_mod._ok(
        device_id=device_id, origin=origin, killed=outcome["killed"] or [],
        hint="the worker (if any) was closed; the next background call to this origin gets a fresh one",
    )


def register_silent_fetch_tools(ctx) -> List[str]:
    """Entry point ``tools.register_tools`` imports under
    ``try/except ImportError``, the same defensive seam every sibling
    workstream module uses (vision.py/session_powers.py/etc) -- a checkout
    without this file degrades to "no silent-fetch tool", not a failed
    plugin load."""
    ctx.register_tool(
        name=SILENT_FETCH_SCHEMA["name"],
        toolset=TOOLSET,
        schema=SILENT_FETCH_SCHEMA,
        handler=handle_silent_fetch,
        check_fn=tools_mod.bridge_available,
        emoji="\U0001F47B",  # ghost -- an unattended, invisible fetch
    )
    ctx.register_tool(
        name=SILENT_KILL_SCHEMA["name"],
        toolset=TOOLSET,
        schema=SILENT_KILL_SCHEMA,
        handler=handle_silent_kill,
        check_fn=tools_mod.bridge_available,
        emoji="\U0001F47B",
    )
    return [SILENT_FETCH_SCHEMA["name"], SILENT_KILL_SCHEMA["name"]]
