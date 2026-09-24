"""Capability-gated vision (plan.md §5b): probe whether the user's active
model can actually see images, cache the answer, and make
``browser_bridge_screenshot`` honest about what it returns.

Why this exists: "see the tab" has two implementations with very different
cost/fidelity (plan §5b) — a cheap DOM snapshot that is blind to canvas/
WebGL/video/CSS layout, or a screenshot that only works if the user's model
accepts image content parts. Hermes users run arbitrary models, and
OpenAI-compatible servers advertise no vision flag in ``/v1/models``
(verified empirically 2026-09-21 against a self-hosted vLLM server — no capability hints in the metadata). So probing is
mandatory, not optional.

Hard-won gotchas (see CLAUDE.md — do not rediscover these expensively):
  * The probe image must be >= 64x64 px. A 4x4 probe made vLLM throw HTTP 500
    during preprocessing while the same model handled 64x64 fine.
  * Use a generous ``max_tokens``. A 20-token budget truncated a reasoning
    model mid-``reasoning`` before it ever emitted an answer.
  * Never silently claim "no vision". A failed/ambiguous probe is UNKNOWN,
    surfaced as such — see ``parse_probe_answer`` and ``status_summary``.

Entry point other modules use: ``register_vision_tools(ctx)``. ``tools.py``
(owned by another workstream) imports this under ``try/except ImportError``
and calls it once during registration; nothing in this file reaches into
tools.py. ``status_summary()`` is the other public seam: ``browser_bridge_
status`` can call it to report vision state without ever triggering a probe
itself (probes cost a real completion call — status reads should stay
cheap).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import secrets
import shutil
import sqlite3
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import attach as attach_mod, audit, config, protocol, relay as relay_mod, state
from . import tools as tools_mod

logger = logging.getLogger(__name__)

SCREENSHOT_NAME = "browser_bridge_screenshot"

# ---------------------------------------------------------------------------
# Small JSON-result helpers (deliberately duplicated from tools.py rather
# than imported: they're one-liners, and importing them would couple this
# module to tools.py's private-by-convention names across a workstream
# boundary neither of us should be editing).
# ---------------------------------------------------------------------------


def _ok(**fields: Any) -> str:
    return json.dumps({"success": True, **fields}, default=str)


def _err(message: str, **fields: Any) -> str:
    return json.dumps({"success": False, "error": message, **fields}, default=str)


# ---------------------------------------------------------------------------
# Probe image generation — dependency-free (no PIL) so probing works even in
# a minimal test/dev environment. PIL 12.3 happens to be present in the
# gateway venv, but the probe is a two-flat-color 64x64 square: struct+zlib
# is simpler than pulling in an imaging library for that, and it mirrors the
# precedent already in this repo (extension/tools/make_icons.py writes PNGs
# the same way for the extension's own icons).
# ---------------------------------------------------------------------------

MIN_PROBE_SIZE = 64  # a 4x4 probe made vLLM 500 on preprocessing; 64x64 didn't.

_PALETTE: Dict[str, Tuple[int, int, int]] = {
    "red": (214, 39, 40),
    "green": (44, 160, 44),
    "blue": (31, 119, 180),
    "yellow": (219, 189, 25),
    "orange": (230, 126, 34),
    "purple": (142, 68, 173),
}


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def _encode_rgb_png(size: int, pixel_fn) -> bytes:
    """Minimal PNG encoder: 8-bit RGB (colour type 2), one IDAT, no filters."""
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            row.extend(pixel_fn(x, y))
        rows.append(bytes(row))
    raw = b"".join(b"\x00" + row for row in rows)  # filter type 0 (none) per row
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)),
            _png_chunk(b"IDAT", zlib.compress(raw, 9)),
            _png_chunk(b"IEND", b""),
        ]
    )


def make_probe_png(size: int = MIN_PROBE_SIZE) -> Tuple[bytes, str, str]:
    """Generate a two-tone PNG: a dominant interior fill plus a thinner
    border of a second colour. Returns ``(png_bytes, fill_color_name,
    border_color_name)`` — ``fill_color_name`` is the expected answer to
    "what color covers the larger area".

    Raises ``ValueError`` below ``MIN_PROBE_SIZE`` — this is a hard gate, not
    a suggestion (see module docstring).
    """
    if size < MIN_PROBE_SIZE:
        raise ValueError(
            f"vision probe images must be >= {MIN_PROBE_SIZE}x{MIN_PROBE_SIZE}px "
            f"(a smaller probe made vLLM throw HTTP 500 during preprocessing)"
        )
    fill_name, border_name = random.sample(list(_PALETTE), 2)
    fill, border = _PALETTE[fill_name], _PALETTE[border_name]
    border_px = max(4, size // 11)

    def pixel(x: int, y: int) -> bytes:
        on_border = x < border_px or y < border_px or x >= size - border_px or y >= size - border_px
        return bytes(border if on_border else fill)

    return _encode_rgb_png(size, pixel), fill_name, border_name


# ---------------------------------------------------------------------------
# Probe prompt + answer parsing
# ---------------------------------------------------------------------------

_PROBE_PROMPT = (
    "You are shown an image made of exactly two solid colors: a thin border "
    "color and a larger interior fill color. Identify the color of the "
    "LARGER area. You may reason first if you need to, but your reply must "
    "end with exactly one line in this exact format, with nothing after it:\n"
    "ANSWER: <color>\n"
    "Use a single common English color word for <color>."
)

# Phrases that mean "I was not actually given an image" regardless of
# whether the model still emits a guessed ANSWER: line afterward.
_NO_IMAGE_PHRASES = (
    "cannot see", "can't see", "unable to see",
    "no image", "without an image", "not able to view",
    "don't have the ability to view", "do not have the ability to view",
    "cannot view images", "can't view images", "cannot process images",
    "am unable to process images", "as a text-based", "as a text-only",
    "i have no visual", "no visual input",
)

_ANSWER_RE = re.compile(r"answer\s*:\s*([a-zA-Z]+)")


def parse_probe_answer(text: str, expected: str) -> Tuple[str, str]:
    """Classify a probe completion as ``true|false|unknown`` plus a detail
    string, tolerant of a reasoning model burying the answer after chatter.

    Never returns "false" on an empty/ambiguous response — that's the one
    rule this function exists to enforce (plan §5b, CLAUDE.md gotchas: a
    failed/ambiguous probe is UNKNOWN, not a silent "no vision").
    """
    if not text or not text.strip():
        return "unknown", (
            "empty completion — possible reasoning-token budget exhaustion "
            "before an answer was emitted"
        )

    lowered = text.lower()
    for phrase in _NO_IMAGE_PHRASES:
        if phrase in lowered:
            return "false", f"model reported an inability to view images ({phrase!r} in reply)"

    # Reasoning models may emit chain-of-thought before the marker, and may
    # even echo the instruction text ("...end with ANSWER: <color>") as part
    # of that reasoning. Taking the LAST match skips past an echoed
    # instruction toward whatever the model actually settled on; the
    # instruction's own "<color>" placeholder never matches [a-zA-Z]+ anyway
    # (it starts with "<"), so an unanswered echo doesn't produce a spurious
    # match at all.
    matches = _ANSWER_RE.findall(lowered)
    if matches:
        answer = matches[-1].strip(".,!:; ")
        if answer == expected:
            return "true", f"ANSWER: line matched the expected color {expected!r}"
        return "false", f"ANSWER: line said {answer!r}, expected {expected!r}"

    # No explicit marker. A bare mention of the expected color elsewhere in
    # the reply is weak evidence (could be guessing, could be echoing the
    # prompt) — treat it as UNKNOWN rather than crediting it as a pass.
    if re.search(rf"\b{re.escape(expected)}\b", lowered):
        return "unknown", f"expected color {expected!r} appears in the reply but no ANSWER: marker was found"
    return "unknown", "could not parse an answer from the model response"


# ---------------------------------------------------------------------------
# Capability registry — sqlite table via state.py's shared connection.
#
# Design choice: reuse hermes_plugin.state's existing connection singleton
# (WAL mode, thread lock discipline, 0600 file hardening) rather than a
# second sqlite file or a JSON blob under ~/.hermes/browser_bridge/. state.py
# already solved "small local durable store, safe across the relay thread
# and CLI/tool-handler threads, survives a gateway restart" — reinventing
# that in a JSON file (its own locking, its own atomic-write dance) for one
# more small table would be pure duplication. This module never edits
# state.py; it only calls the public state.connect() and executes its own
# CREATE TABLE against the connection it returns, which is the same pattern
# the brief asked for ("add it in your own module").
# ---------------------------------------------------------------------------

_VISION_SCHEMA = """
CREATE TABLE IF NOT EXISTS vision_capability (
    provider   TEXT NOT NULL,
    model      TEXT NOT NULL,
    capability TEXT NOT NULL CHECK (capability IN ('true', 'false', 'unknown')),
    probed_at  INTEGER NOT NULL,
    detail     TEXT,
    PRIMARY KEY (provider, model)
);
"""

# Added for the defect-2 fix: an empty-``text`` probe result (a reasoning model that
# answered only in reasoning/reasoning_content/reasoning_details, which
# ``ctx.llm.complete()`` cannot see — see ``probe_vision`` below) is still cached, so a
# flapping model isn't re-probed on every call, but it must NOT be trusted for a full
# 24h like a real verdict: a transient response shape shouldn't poison the cache for a
# day. Rows written with this flag set use ``vision_probe_unknown_empty_ttl_hours``
# (short) instead of ``vision_probe_ttl_hours`` (long) when checking freshness — see
# ``_effective_ttl_hours``. Migrated onto a pre-existing table with ALTER TABLE below
# since ``CREATE TABLE IF NOT EXISTS`` is a no-op against an already-created table.
_SHORT_TTL_COLUMN = "short_ttl"

_cache_lock = threading.Lock()
_table_ready = False


def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    conn = state.connect()
    with _cache_lock:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_VISION_SCHEMA)
        try:
            conn.execute(
                f"ALTER TABLE vision_capability ADD COLUMN {_SHORT_TTL_COLUMN} INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
        conn.commit()
    _table_ready = True


def _cache_set(provider: str, model: str, capability: str, detail: str = "", short_ttl: bool = False) -> None:
    """Write (or replace) the cached verdict for one provider+model.

    ``short_ttl`` marks a row that must not be trusted with the normal 24h TTL — see
    the module comment above ``_SHORT_TTL_COLUMN``. Defaults to ``False`` so every
    existing call site (a real true/false verdict, or an ordinary probe-call failure)
    keeps its current long-TTL behaviour unchanged.
    """
    _ensure_table()
    conn = state.connect()
    now_ms = int(time.time() * 1000)
    with _cache_lock:
        conn.execute(
            "INSERT INTO vision_capability (provider, model, capability, probed_at, detail, short_ttl)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(provider, model) DO UPDATE SET"
            "   capability = excluded.capability,"
            "   probed_at = excluded.probed_at,"
            "   detail = excluded.detail,"
            "   short_ttl = excluded.short_ttl",
            (provider, model, capability, now_ms, detail, 1 if short_ttl else 0),
        )
        conn.commit()


def current_provider_model() -> tuple[str, str]:
    """The provider+model a completion would actually use right now.

    These are the same host helpers ``agent.plugin_llm`` itself uses to
    attribute a completion (``_resolve_attribution``), and they reflect
    runtime overrides set via ``set_runtime_main()`` — no completion call, no
    duplicated credentials. Private names, so every failure degrades to
    ("", "") and the caller falls back to the most-recent cache row.
    """
    try:
        from agent.auxiliary_client import _read_main_model, _read_main_provider

        return (_read_main_provider() or "").strip(), (_read_main_model() or "").strip()
    except Exception:
        return "", ""


def _latest_cache_row() -> Optional[Dict[str, Any]]:
    """The cached verdict for the *currently active* provider+model.

    Keyed lookup first: a stale row for a model the user has since switched
    away from must never answer for the new one. Claiming ``vision: true`` for
    a model that cannot see would send images to a blind model and label the
    result ``pixels`` — the exact dishonesty §5b exists to prevent, and worse
    than reporting UNKNOWN. Falls back to the most recent row only when the
    host helpers are unavailable, so behaviour degrades rather than breaks.
    """
    _ensure_table()
    conn = state.connect()
    provider, model = current_provider_model()
    with _cache_lock:
        if provider or model:
            row = conn.execute(
                "SELECT provider, model, capability, probed_at, detail, short_ttl"
                " FROM vision_capability WHERE provider = ? AND model = ?",
                (provider, model),
            ).fetchone()
            if row is not None:
                return dict(row)
            # Known active model with no row of its own: report nothing rather
            # than borrowing another model's verdict.
            return None
        row = conn.execute(
            "SELECT provider, model, capability, probed_at, detail, short_ttl"
            " FROM vision_capability ORDER BY probed_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row is not None else None


def _row_is_fresh(row: Dict[str, Any], ttl_hours: float) -> bool:
    age_ms = int(time.time() * 1000) - int(row.get("probed_at", 0))
    return age_ms < ttl_hours * 3600 * 1000


def _effective_ttl_hours(row: Dict[str, Any], cfg: Dict[str, Any]) -> float:
    """The TTL that applies to *this* cache row.

    A row cached with ``short_ttl`` set (an empty-``text`` probe response — see
    ``probe_vision``) uses ``vision_probe_unknown_empty_ttl_hours`` (default 15
    minutes) instead of the normal ``vision_probe_ttl_hours`` (default 24h): a
    transient "the model returned no parseable content" response is real
    information (worth caching so a flapping model isn't re-probed on every
    single call) but must not get to squat on the verdict for a full day the way
    an actual true/false probe result does.
    """
    if row.get("short_ttl"):
        return float(cfg.get("vision_probe_unknown_empty_ttl_hours", 0.25))
    return float(cfg.get("vision_probe_ttl_hours", 24))


# ---------------------------------------------------------------------------
# The probe itself — uses ctx.llm.complete(), the host-owned completion path
# (agent.plugin_llm.PluginLlm.complete), so it exercises the *exact* route a
# screenshot would take through the MEDIA pipeline: an OpenAI-shaped message
# with an image_url content part. No bespoke HTTP client, no duplicated
# credentials.
# ---------------------------------------------------------------------------


def probe_vision(ctx, max_tokens: Optional[int] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
    """Run one live vision probe against the user's active model, cache the
    result, and return it. Always caches — including "unknown" outcomes,
    so a flapping model doesn't get re-probed on every single call within
    the TTL window; ``get_vision_state`` still surfaces "unknown" honestly
    each time it reads a stale-or-unknown cache row.
    """
    cfg = config.load()
    if max_tokens is None:
        max_tokens = int(cfg.get("vision_probe_max_tokens", 512))
    if timeout is None:
        timeout = float(cfg.get("vision_probe_timeout_s", 20))

    try:
        png_bytes, expected_color, border_color = make_probe_png()
    except Exception as exc:  # pragma: no cover - defensive, make_probe_png is pure
        logger.exception("browser_bridge: failed to generate the vision probe image")
        return {
            "capability": "unknown",
            "provider": "",
            "model": "",
            "detail": f"probe image generation failed: {exc}",
        }

    b64 = base64.b64encode(png_bytes).decode("ascii")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _PROBE_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }
    ]

    try:
        result = ctx.llm.complete(
            messages,
            max_tokens=max_tokens,
            timeout=timeout,
            purpose="browser_bridge_vision_probe",
        )
    except Exception as exc:
        logger.warning("browser_bridge: vision probe call failed: %s", exc)
        _cache_set("", "", "unknown", detail=f"probe call raised: {exc}")
        audit.record("vision_probe", provider="", model="", capability="unknown", detail=str(exc))
        return {"capability": "unknown", "provider": "", "model": "", "detail": f"probe call raised: {exc}"}

    text = getattr(result, "text", "") or ""
    provider = getattr(result, "provider", "") or "auto"
    model = getattr(result, "model", "") or "default"

    # Defect-2 fix: an empty ``result.text`` must never fall into
    # parse_probe_answer's generic "unknown" bucket and get cached at full
    # confidence. It happens specifically when the active model answered in a
    # reasoning field instead of ``content`` — ctx.llm.complete() -> PluginLlm.
    # complete() -> agent.plugin_llm._extract_text() reads *only*
    # ``message.content`` (verified against v0.21.4, agent/plugin_llm.py:364-377)
    # and never falls back the way the host's own
    # agent.auxiliary_client.extract_content_or_reasoning() does for
    # ``reasoning``/``reasoning_content``/``reasoning_details``. A plugin cannot
    # recover those fields itself: PluginLlmCompleteResult only exposes
    # text/provider/model/agent_id/usage/audit, and ``audit`` carries only
    # plugin_id/purpose/profile/task -- never the raw provider response (see
    # PluginLlm._finish, plugin_llm.py:521-543). So this is classified as UNKNOWN
    # with a reason distinct from a merely-unparseable answer, and cached with a
    # short TTL (see _effective_ttl_hours) instead of the normal 24h one, so a
    # transient response shape doesn't poison the verdict for a day.
    if not text.strip():
        capability = "unknown"
        detail = "model returned no content, possibly reasoning-only — vision state undetermined"
        _cache_set(provider, model, capability, detail=detail, short_ttl=True)
        audit.record("vision_probe", provider=provider, model=model, capability=capability, detail=detail)
        return {
            "capability": capability,
            "provider": provider,
            "model": model,
            "detail": detail,
            "expected_color": expected_color,
            "border_color": border_color,
            "raw_text": "",
        }

    capability, detail = parse_probe_answer(text, expected_color)

    _cache_set(provider, model, capability, detail=detail)
    audit.record("vision_probe", provider=provider, model=model, capability=capability, detail=detail)
    return {
        "capability": capability,
        "provider": provider,
        "model": model,
        "detail": detail,
        "expected_color": expected_color,
        "border_color": border_color,
        "raw_text": text[:500],
    }


def get_vision_state(ctx, force_probe: bool = False) -> Dict[str, Any]:
    """Resolve the effective vision capability for a live tool call
    (``browser_bridge_screenshot``): config override first, then a
    fresh-enough cache row, then a real probe as a last resort. This is the
    only path that may spend a completion call — ``status_summary()`` below
    never does.
    """
    cfg = config.load()
    override = str(cfg.get("vision", "auto"))
    if override == "force_on":
        return {"capability": "true", "provider": "", "model": "", "source": "config_override:force_on"}
    if override == "force_off":
        return {"capability": "false", "provider": "", "model": "", "source": "config_override:force_off"}

    row = None if force_probe else _latest_cache_row()
    if row is not None and _row_is_fresh(row, _effective_ttl_hours(row, cfg)):
        return {
            "capability": row["capability"],
            "provider": row["provider"],
            "model": row["model"],
            "source": "cache",
            "probed_at": row["probed_at"],
        }

    if ctx is None or getattr(ctx, "llm", None) is None:
        return {
            "capability": "unknown",
            "provider": "",
            "model": "",
            "source": "no_probe_available",
            "detail": "no ctx.llm available to run the vision probe",
        }

    probed = probe_vision(ctx)
    return {
        "capability": probed["capability"],
        "provider": probed["provider"],
        "model": probed["model"],
        "source": "probe",
        "detail": probed.get("detail", ""),
    }


def status_summary() -> Dict[str, Any]:
    """Cheap, probe-free vision status for ``browser_bridge_status``
    (exported for the tools.py workstream to call directly — no ctx
    needed). Never calls ctx.llm; only reads config + the cache table.

    Acceptance (plan §5b iii): a failed/never-run probe is reported as
    "vision state UNKNOWN, degraded mode active", never a silent false.
    """
    cfg = config.load()
    override = str(cfg.get("vision", "auto"))
    if override == "force_on":
        return {"capability": "true", "source": "config_override:force_on"}
    if override == "force_off":
        return {"capability": "false", "source": "config_override:force_off"}

    row = _latest_cache_row()
    if row is None:
        return {
            "capability": "unknown",
            "source": "auto",
            "note": "vision state UNKNOWN, degraded mode active — no probe has run yet",
        }

    fresh = _row_is_fresh(row, _effective_ttl_hours(row, cfg))
    out: Dict[str, Any] = {
        "capability": row["capability"],
        "source": "auto",
        "provider": row["provider"],
        "model": row["model"],
        "probed_at": row["probed_at"],
        "stale": not fresh,
    }
    # Surface *why* an UNKNOWN verdict is UNKNOWN (defect-2 fix) rather than just
    # the bare capability -- in particular the empty-text/reasoning-only case
    # (see probe_vision) needs a caller of browser_bridge_status to be able to
    # tell "the probe genuinely couldn't parse an answer" apart from "the model
    # answered in a reasoning field ctx.llm.complete() can't see".
    if row.get("detail"):
        out["detail"] = row["detail"]
    if row["capability"] == "unknown":
        out["note"] = "vision state UNKNOWN, degraded mode active"
    elif not fresh:
        out["note"] = "cached vision state is stale; will re-probe on next screenshot"
    return out


# ---------------------------------------------------------------------------
# OCR fallback — tesseract is NOT installed on the gateway (verified:
# neither the `tesseract` binary nor the `pytesseract` package are present,
# 2026-09-21). This must stay a soft dependency: detect it, use it if it
# ever shows up, degrade honestly if not. See the report for what an
# install would take.
# ---------------------------------------------------------------------------

_tesseract_checked = False
_tesseract_ok = False


def tesseract_available() -> bool:
    global _tesseract_checked, _tesseract_ok
    if _tesseract_checked:
        return _tesseract_ok
    _tesseract_checked = True
    cfg = config.load()
    if not cfg.get("vision_ocr_enabled", True):
        _tesseract_ok = False
        return False
    try:
        import pytesseract  # type: ignore  # noqa: F401

        _tesseract_ok = shutil.which("tesseract") is not None
    except ImportError:
        _tesseract_ok = False
    return _tesseract_ok


def try_ocr(png_bytes: bytes) -> Optional[str]:
    """Best-effort OCR text from a screenshot. Returns None (never raises)
    when tesseract/pytesseract are unavailable or extraction fails for any
    reason — callers degrade to the layout-only description in that case."""
    if not png_bytes or not tesseract_available():
        return None
    try:
        import io

        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore

        image = Image.open(io.BytesIO(png_bytes))
        text = pytesseract.image_to_string(image).strip()
        return text or None
    except Exception:
        logger.exception("browser_bridge: OCR attempt failed; degrading to layout-only text")
        return None


_OCR_INSTALL_NOTE = (
    "OCR unavailable on this gateway (neither the tesseract binary nor "
    "pytesseract are installed) — layout-only description. Installing "
    "would need the `tesseract-ocr` system package (apt) plus `pip install "
    "pytesseract` in the gateway venv; no code change required once both "
    "are present, this path self-enables."
)


# ---------------------------------------------------------------------------
# Layout description — built from page.screenshot's `boxes`, contributed by
# workstream A (tools.py/attach.py), plus the image dimensions. Used
# whenever vision is false/unknown.
#
# Contract (page.screenshot result, as of the M1 defect-fix pass): boxes are
# `[ { idx, role, name, x, y, width, height } ]` where `idx` matches the
# snapshot's own numbering for the same element (so a model can act(idx) on
# something it saw here), `role` is the snapshot's role token, `name` is the
# accessible name (already redacted upstream), and x/y/width/height are CSS
# pixels relative to the viewport. `viewport: {width, height}` sits alongside
# `boxes` at the top of the result.
#
# An OLDER extension build may still send the pre-fix shape
# (`{nodeId, x, y, width, height}` — no role/name/idx at all), which used to
# render the useless `[region region 80x32 at (12,500)]` (role fell back to
# "region", then got prefixed with a second literal "region" token). This
# function tolerates that shape too — missing fields degrade to a plainer
# line rather than crashing or duplicating "region".
# ---------------------------------------------------------------------------


def build_layout_description(boxes: Optional[List[Any]], width: int = 0, height: int = 0) -> str:
    """Render bounding boxes as spatial text, e.g.:

        Layout snapshot (940x640 viewport), 3 element(s):
        [3] button "Submit" region 80x32 at (12,500)
        [7] textbox "Email" region 240x28 at (200,60)
        [canvas region 940x540 at (12,88)]

    An element with a snapshot ``idx`` (interactive, in the a11y tree) is
    rendered ``[idx] role "name" region WxH at (x,y)`` — role and/or name are
    simply omitted if absent rather than printed empty. An element with no
    idx (canvas/video/iframe — anything outside the a11y tree, or an older
    extension build's boxes that never carried one) is rendered
    ``[role region WxH at (x,y)]`` when a role is known, matching plan.md
    §5b's own worked example verbatim, or the bare ``[region WxH at (x,y)]``
    when it isn't.
    """
    boxes = boxes or []
    header = "Layout snapshot"
    if width and height:
        header += f" ({width}x{height} viewport)"
    header += f", {len(boxes)} element(s):" if boxes else " — no interactive elements reported."
    lines = [header]

    for box in boxes:
        if not isinstance(box, dict):
            continue
        try:
            x, y = int(box.get("x", 0)), int(box.get("y", 0))
            w, h = int(box.get("width", 0)), int(box.get("height", 0))
        except (TypeError, ValueError):
            continue

        idx = box.get("idx")
        # `role`/`name` are the contract's fields. `tag`/`label` are
        # tolerated fallbacks for a pre-contract extension build that
        # sourced boxes from raw DOM nodes instead of the content script's
        # a11y-aware box list.
        role = box.get("role") or box.get("tag") or ""
        name = box.get("name") or box.get("label") or ""

        if idx is not None:
            descriptor = f"{role} " if role else ""
            quoted = f'"{name}" ' if name else ""
            lines.append(f"[{idx}] {descriptor}{quoted}region {w}x{h} at ({x},{y})")
        elif role:
            lines.append(f"[{role} region {w}x{h} at ({x},{y})]")
        else:
            lines.append(f"[region {w}x{h} at ({x},{y})]")

    return "\n".join(lines)


def _degraded_payload(
    capability: str,
    png_bytes: bytes,
    boxes: Optional[List[Any]],
    width: int,
    height: int,
) -> Dict[str, Any]:
    """The ocr+layout / text-only fallback payload, factored out of
    ``build_screenshot_result`` so it can also be reused by
    ``finalize_screenshot_result``'s pixels-embed-failure fallback below:
    even when the probe says the active model can see images, building the
    actual multimodal embed can still fail (Pillow missing, an unreadable
    saved file, no bytes at all because the capture itself failed) and that
    must degrade honestly to a real layout description rather than ship the
    ``pixels`` label with nothing behind it — the same failure mode as
    defect 1, just reached a different way.

    Honesty invariant: ``ocr+layout`` is returned if and only if
    ``ocr_text`` is truthy, and ``ocr_text`` can only be truthy when
    ``try_ocr()`` actually ran tesseract and got non-empty text back
    (``try_ocr`` returns ``None`` — never ``""`` — whenever tesseract is
    absent/disabled or extraction failed, see its own docstring). So with
    tesseract absent this always falls through to ``text-only`` with the
    install note attached below; it must never claim ``ocr+layout`` for a
    layout-only description with no real OCR behind it.
    """
    layout_text = build_layout_description(boxes, width, height)
    ocr_text = try_ocr(png_bytes) if png_bytes else None
    if ocr_text:
        return {
            "vision_fidelity": "ocr+layout",
            "vision_state": capability,
            "description": f"{ocr_text}\n\n{layout_text}",
        }

    payload: Dict[str, Any] = {
        "vision_fidelity": "text-only",
        "vision_state": capability,
        "description": layout_text,
    }
    if not tesseract_available():
        payload["note"] = _OCR_INSTALL_NOTE
    return payload


def build_screenshot_result(
    vision_state: Dict[str, Any],
    png_bytes: bytes,
    boxes: Optional[List[Any]],
    width: int,
    height: int,
) -> Dict[str, Any]:
    """Pure decision function separated from ``handle_screenshot`` so it can
    be unit tested without a live relay/extension connection: given a
    resolved vision capability and the raw screenshot material, decide the
    fidelity and build the payload fields that go on the wire.

    ``vision_fidelity`` is always one of pixels|ocr+layout|text-only
    (protocol/schema.json's ``fidelity`` enum) — set unconditionally, never
    left to be inferred by the caller (cheap honesty prevents a text-only
    model hallucinating from an OCR skeleton, plan §5b).

    The ``pixels`` branch here only decides the *label* — it stays disk-free
    and dependency-free on purpose (no PIL, no host ``tools.vision_tools``
    import) so this function keeps working as a pure, no-IO unit under test.
    Actually attaching the image bytes is ``finalize_screenshot_result``'s
    job (defect-1 fix): it calls this function first, then — only for the
    ``pixels`` label — tries to build the multimodal envelope from the saved
    screenshot path, falling back to ``_degraded_payload`` if that fails.
    """
    capability = vision_state.get("capability", "unknown")
    if capability == "true":
        return {"vision_fidelity": "pixels", "vision_state": capability}
    return _degraded_payload(capability, png_bytes, boxes, width, height)


# ---------------------------------------------------------------------------
# Screenshot persistence — always saved regardless of fidelity, so a
# vision-capable subagent can be pointed at the raw PNG later (plan §5b).
# Retention is age- AND size-bounded and enforced opportunistically on every
# save (same pattern as audit.py's rotation check) rather than via a cron —
# there is no gateway-startup hook to hang a scheduled job off of anyway
# (Documentation/plugin-api-findings.md point 4), and a 33GB gateway disk
# with the gateway's own 2.6GB ~/.hermes footprint cannot absorb unbounded
# screenshot growth.
# ---------------------------------------------------------------------------


def _screenshot_dir() -> Path:
    cfg = config.load()
    override = cfg.get("vision_screenshot_dir") or ""
    path = Path(override) if override else (config.STATE_DIR / "screenshots")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def save_screenshot(png_bytes: bytes) -> Path:
    directory = _screenshot_dir()
    name = f"{int(time.time() * 1000)}_{secrets.token_hex(4)}.png"
    path = directory / name
    path.write_bytes(png_bytes)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _cleanup_screenshots(directory)
    return path


def _cleanup_screenshots(directory: Path) -> None:
    cfg = config.load()
    retention_s = float(cfg.get("vision_screenshot_retention_hours", 24)) * 3600
    max_bytes = int(cfg.get("vision_screenshot_max_bytes", 200 * 1024 * 1024))
    now = time.time()

    entries: List[Tuple[float, int, Path]] = []
    total = 0
    for candidate in directory.glob("*.png"):
        try:
            st = candidate.stat()
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_size, candidate))
        total += st.st_size

    # Age-based eviction first.
    survivors: List[Tuple[float, int, Path]] = []
    for mtime, size, candidate in entries:
        if now - mtime > retention_s:
            try:
                candidate.unlink()
                total -= size
            except OSError:
                survivors.append((mtime, size, candidate))
        else:
            survivors.append((mtime, size, candidate))

    # Size-budget eviction, oldest first, only if still over budget.
    if total > max_bytes:
        survivors.sort(key=lambda entry: entry[0])
        for mtime, size, candidate in survivors:
            if total <= max_bytes:
                break
            try:
                candidate.unlink()
                total -= size
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Target resolution. The tab and its origin come from a FRESH tabs.list, the
# same source snapshot/read/act use (tools._fresh_tabs), never from
# relay.status()'s per-connection ``attached`` list: that list is filled by
# the extension heartbeat as ``[{tabId, attached: true}]`` with no url, so an
# origin derived from it is always '' and the grant check then fails as
# "origin '' is set to 'off'". Screenshot is a read, so only attachment is
# required, not the driving lease (plan §3.3).
# ---------------------------------------------------------------------------


class _TargetError(Exception):
    """Carries a ready-to-return JSON error string."""

    def __init__(self, error_json: str):
        super().__init__(error_json)
        self.error_json = error_json


def _match_target(attached: List[Dict[str, Any]], target: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Pick one attached tab by tabId, then url substring, then title
    substring. Returns (tab, error_message); error_message is "" on a match."""
    if not attached:
        return None, "no tab is attached"
    if not target:
        if len(attached) == 1:
            return attached[0], ""
        return None, f"{len(attached)} tabs are attached; specify target (tabId, url, or title)"
    exact = [t for t in attached if str(t.get("tabId")) == target]
    if exact:
        return exact[0], ""
    needle = target.lower()
    by_url = [t for t in attached if needle in str(t.get("url") or "").lower()]
    matched = by_url or [t for t in attached if needle in str(t.get("title") or "").lower()]
    if len(matched) == 1:
        return matched[0], ""
    if not matched:
        return None, f"no attached tab matches target={target!r}"
    return None, f"{len(matched)} attached tabs match target={target!r}; pass the tabId instead"


def _resolve_target(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
    """Return (device_id, tab, origin) or raise _TargetError.

    Device selection is tools._resolve_device (explicit ``device_id``, else
    the only connected device). ``origin`` is never '': a tab whose origin
    cannot be derived is refused here, naming the tab, before any grant check.
    """
    device_id, err = tools_mod._resolve_device(args)
    if err:
        raise _TargetError(err)
    paused = tools_mod._paused_refusal(device_id, "screenshot")
    if paused:
        raise _TargetError(paused)
    tabs, exc = tools_mod._fresh_tabs(device_id)
    if exc is not None:
        raise _TargetError(tools_mod._bridge_err(exc, "tabs.list"))
    attached = [t for t in tabs if t.get("attached")]
    target = str(args.get("target") if args.get("target") is not None else "").strip()
    tab, message = _match_target(attached, target)
    if tab is None:
        briefs = tools_mod._tab_briefs(attached if attached else tabs)
        hint = "call browser_bridge_attach first" if not attached else "pass target as one of these tabIds"
        raise _TargetError(_err(message, code=protocol.TARGET_NOT_ATTACHED if not attached else protocol.INVALID_PARAMS,
                                hint=hint, device_id=device_id, candidates=briefs))
    origin = str(tab.get("origin") or tools_mod._origin_of(str(tab.get("url") or "")))
    if not origin:
        audit.record(
            "tool_screenshot_denied", device_id=device_id, tab_id=tab.get("tabId"),
            origin="", reason="origin unresolved",
        )
        raise _TargetError(_err(
            f"could not resolve the origin of attached tab {tab.get('tabId')} (the extension reported "
            f"no url for it), so its grant cannot be checked; screenshot refused",
            hint="the tab may still be loading; retry, or call browser_bridge_status and re-attach it",
            device_id=device_id, tab_id=tab.get("tabId"),
        ))
    return device_id, tab, origin


def _validate_region(region: Any) -> Tuple[Optional[Dict[str, int]], str]:
    """``region`` must be {x, y, width, height} ints with x/y >= 0 and
    width/height > 0. Returns (region, error_message)."""
    if not isinstance(region, dict):
        return None, "region must be an object {x, y, width, height}"
    out: Dict[str, int] = {}
    for key in ("x", "y", "width", "height"):
        value = region.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return None, f"region.{key} must be an integer (got {value!r})"
        out[key] = value
    if out["x"] < 0 or out["y"] < 0:
        return None, "region.x and region.y must be >= 0"
    if out["width"] <= 0 or out["height"] <= 0:
        return None, "region.width and region.height must be > 0"
    return out, ""


# ---------------------------------------------------------------------------
# Defect-1 fix: the "pixels" fidelity path ships an actual image now, not just
# the label. v0.21.4's tool dispatcher (tools/registry.py's
# ``_normalize_handler_result``) accepts a ``{"_multimodal": True, "content":
# [...]}`` envelope in addition to a plain string; the sibling built-in
# ``tools/browser_use_cli.py``'s ``_native_screenshot_result`` is the host's
# own precedent for this exact situation (attaching a just-captured
# screenshot for a vision-capable model), and this mirrors its shape
# verbatim rather than inventing a new one. Generic downstream consumers
# (``agent/vision_message_prep.py``'s ``_tool_result_content_for_active_model``,
# ``agent/tool_dispatch_helpers.py``'s ``_multimodal_text_summary``) already
# degrade ANY tool's multimodal envelope to its ``text_summary`` string when
# the active model/provider turns out not to accept image tool-result
# content — so this module doesn't need to re-implement that gate; it only
# needs to build the envelope honestly when our own §5b probe says the model
# can see images.
# ---------------------------------------------------------------------------


def _pixels_multimodal_result(payload: Dict[str, Any], screenshot_path: Path) -> Optional[Dict[str, Any]]:
    """Build the multimodal tool-result envelope for a successful ``pixels``
    capture. ``payload`` must already carry the final ``path``/``width``/
    ``height``/``vision_fidelity``/``vision_state`` fields — they become
    ``text_summary`` verbatim (the same JSON string the non-pixels branches
    return), so a provider that can't take the multimodal envelope degrades
    to exactly the plain-JSON contract those branches already promise.

    Returns ``None`` on any failure (Pillow not installed, host helpers not
    importable, unreadable file) so the caller can fall back to a real
    layout description instead of ever claiming ``pixels`` with nothing
    attached — that fallback is ``finalize_screenshot_result``'s job, not
    this function's; this one only ever returns a full envelope or nothing.

    Downscaling is delegated entirely to the host's own sizing policy
    (``tools.vision_tools._resize_image_for_vision`` sized by
    ``tools.vision_tools_history_budget.resolve_embed_target_bytes()``)
    rather than hand-rolled here: the host already tuned this for the exact
    cost problem an embedded screenshot creates (#92699 in the host's own
    history — an unbounded embed bakes into conversation history and is
    re-sent every later turn; a 4MB/7900px embed was observed costing
    ~100-260K billed tokens per image). ``force_jpeg=True`` matches
    ``browser_use_cli.py``'s own choice: re-encode as JPEG when a resize is
    needed so a text-heavy screenshot keeps legible resolution and shrinks
    via JPEG quality steps instead of halving pixel dimensions.
    """
    try:
        from tools.vision_tools import _EMBED_MAX_DIMENSION, _resize_image_for_vision
        from tools.vision_tools_history_budget import resolve_embed_target_bytes

        data_url = _resize_image_for_vision(
            screenshot_path,
            mime_type="image/png",
            max_base64_bytes=resolve_embed_target_bytes(),
            max_dimension=_EMBED_MAX_DIMENSION,
            force_jpeg=True,
        )
        text = _ok(**payload)
        attached = text + "\n\nThe screenshot from this call is attached — inspect it with your native vision."
        return {
            "_multimodal": True,
            "text_summary": text,
            "meta": {"screenshot_path": str(screenshot_path), "native_vision": True},
            "content": [
                {"type": "text", "text": attached},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    except Exception as exc:
        logger.warning(
            "browser_bridge: pixels multimodal embed failed for %s, falling back to a layout "
            "description: %s", screenshot_path, exc,
        )
        return None


def _host_native_vision() -> Optional[bool]:
    """The host's own gate for image tool results
    (``tools.vision_tools._should_use_native_vision_fast_path``, the check
    ``browser_use_cli._native_screenshot_result`` makes before embedding).
    When it is False the host's ``_tool_result_content_for_active_model``
    replaces the envelope with ``text_summary``, so the model would get the
    ``pixels`` label with no image. None when the host helper is not
    importable (off the gateway); the embed itself then fails and degrades.
    """
    try:
        from tools.vision_tools import _should_use_native_vision_fast_path
    except Exception:
        return None
    try:
        return bool(_should_use_native_vision_fast_path())
    except Exception:
        return False


_HOST_NO_VISION_NOTE = (
    "vision probe reported this model can see images, but Hermes does not declare vision for the "
    "active provider/model, so it would strip an attached image; degraded to a layout description "
    "instead. Set `model.supports_vision: true` in config.yaml to receive pixels."
)


def _add_note(payload: Dict[str, Any], note: str) -> None:
    if note:
        payload["note"] = f"{payload['note']} {note}" if payload.get("note") else note


def _clip_rect(clip: Any) -> Optional[Tuple[int, int, int, int]]:
    if not isinstance(clip, dict):
        return None
    try:
        x, y = int(clip.get("x", 0)), int(clip.get("y", 0))
        w, h = int(clip.get("width", 0)), int(clip.get("height", 0))
    except (TypeError, ValueError):
        return None
    return (x, y, w, h) if w > 0 and h > 0 else None


def _boxes_in_clip(boxes: Optional[List[Any]], rect: Tuple[int, int, int, int]) -> List[Dict[str, Any]]:
    """Boxes intersecting the captured clip, cropped to it and re-expressed
    relative to its top-left, so the layout text describes the image that was
    actually captured. Boxes and clip are both viewport-relative CSS px."""
    cx, cy, cw, ch = rect
    out: List[Dict[str, Any]] = []
    for box in boxes or []:
        if not isinstance(box, dict):
            continue
        try:
            x, y = int(box.get("x", 0)), int(box.get("y", 0))
            w, h = int(box.get("width", 0)), int(box.get("height", 0))
        except (TypeError, ValueError):
            continue
        left, top = max(x, cx), max(y, cy)
        right, bottom = min(x + w, cx + cw), min(y + h, cy + ch)
        if right <= left or bottom <= top:
            continue
        out.append({**box, "x": left - cx, "y": top - cy, "width": right - left, "height": bottom - top})
    return out


def finalize_screenshot_result(
    vision_state: Dict[str, Any],
    png_bytes: bytes,
    boxes: Optional[List[Any]],
    width: int,
    height: int,
    screenshot_path: Optional[Path],
    clip: Optional[Dict[str, Any]] = None,
    capture_note: str = "",
    image: Optional[Dict[str, Any]] = None,
    scale: Optional[float] = None,
    format: Optional[str] = None,
    marks: Optional[List[Any]] = None,
    viewport_used: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, str]:
    """Decide + build the value ``handle_screenshot`` returns to the tool
    dispatcher: ``(result, fidelity)`` where ``result`` is either the plain
    JSON string every fidelity used to return, or — the defect-1 fix — the
    multimodal envelope for a successful ``pixels`` embed. Split out from
    ``handle_screenshot`` (which needs a live relay/extension connection) so
    this decision is unit-testable on its own, matching this module's
    existing policy for ``build_screenshot_result``.

    ``clip`` and ``capture_note`` are page.screenshot's own ``clip``/``note``
    result fields, carried into the payload (and so into ``text_summary``).
    With a clip, the layout fallback covers only boxes inside it, relative to
    it, and ``width``/``height`` are the clip's size (the captured image's).

    ``image``/``scale``/``format`` (D2) and ``marks`` (D1) describe the
    CAPTURE itself, not the vision embed decision — folded into every
    fidelity branch's payload (by ``_base`` below), not just ``pixels``, so a
    text-only or ocr+layout result still tells the caller the returned
    image's actual pixel size and which badges were drawn.
    """
    rect = _clip_rect(clip)
    if rect is not None:
        boxes = _boxes_in_clip(boxes, rect)
        width, height = rect[2], rect[3]

    def _base(payload: Dict[str, Any]) -> Dict[str, Any]:
        payload["path"] = str(screenshot_path) if screenshot_path else None
        payload["width"] = width
        payload["height"] = height
        if clip:
            payload["clip"] = clip
        if image:
            payload["image"] = image
        if scale is not None:
            payload["scale"] = scale
        if format:
            payload["format"] = format
        if marks:
            payload["marks"] = marks
        if viewport_used:
            payload["viewport_used"] = viewport_used
        _add_note(payload, capture_note)
        return payload

    payload = _base(build_screenshot_result(vision_state, png_bytes, boxes, width, height))

    if payload["vision_fidelity"] == "pixels":
        host_ok = _host_native_vision() if screenshot_path is not None else None
        envelope = None
        if screenshot_path is not None and host_ok is not False:
            envelope = _pixels_multimodal_result(payload, screenshot_path)
        if envelope is not None:
            return envelope, "pixels"
        # No pixels to actually show (host would strip the image, embed
        # failed, or there was never a saved file to embed — e.g. the capture
        # returned no bytes). Degrade honestly to the same OCR/layout fallback
        # the false/unknown branches use rather than ship the "pixels" label
        # with nothing behind it.
        degraded = _base(_degraded_payload(vision_state.get("capability", "unknown"), png_bytes, boxes, width, height))
        if screenshot_path is None:
            embed_note = (
                "vision probe reported this model can see images, but no screenshot was "
                "available to embed for this call; degraded to a layout description instead."
            )
        elif host_ok is False:
            embed_note = _HOST_NO_VISION_NOTE
        else:
            embed_note = (
                "vision probe reported this model can see images, but embedding this "
                "screenshot failed; degraded to a layout description instead."
            )
        _add_note(degraded, embed_note)
        payload = degraded

    return _ok(**payload), payload["vision_fidelity"]


# ---------------------------------------------------------------------------
# The tool itself
# ---------------------------------------------------------------------------

SCREENSHOT_SCHEMA = {
    "name": SCREENSHOT_NAME,
    "description": (
        "Capture a screenshot of an attached Chrome tab. If the active model "
        "can see images (probed automatically, cached), the result attaches "
        "the actual image (downscaled as needed) as a multimodal tool result "
        "so you see it directly in this turn — not just a path. If not (or "
        "unknown, or the image couldn't be embedded), the gateway converts it "
        "server-side first — OCR text plus a layout description of "
        "interactive element positions — so the model still gets spatial "
        "awareness without ever receiving raw pixels it can't use. Always "
        "check `vision_fidelity` in the result (pixels|ocr+layout|text-only) "
        "before trusting what came back. The raw PNG is always saved and its "
        "path returned regardless of fidelity, so a vision-capable subagent "
        "can be pointed at it later."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Device id from browser_bridge_status. Omit when exactly one device is connected.",
            },
            "target": {
                "type": "string",
                "description": (
                    "Which attached tab to capture: a tabId, a substring of its "
                    "url/title, or omitted when exactly one tab is attached."
                ),
            },
            "full": {
                "type": "boolean",
                "description": "Capture the full scrollable page instead of just the viewport.",
            },
            "idx": {
                "type": "integer",
                "description": "Capture just this element, by idx from the tab's latest snapshot/act.",
            },
            "selector": {
                "type": "string",
                "description": "Capture just this element, by CSS selector ('>>>' crosses shadow roots).",
            },
            "region": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                },
                "description": "Capture only this viewport-relative CSS-px rectangle. idx/selector take precedence.",
            },
            "scale": {
                "type": "number",
                "description": (
                    "Downscale factor 0.25-1 (default 1). Smaller images mean faster model "
                    "turns; the result's `image` field reports the returned image's own "
                    "pixel size so coordinates can be mapped back to CSS px."
                ),
            },
            "format": {
                "type": "string",
                "enum": ["png", "jpeg"],
                "description": "Image encoding (default jpeg). Use 'png' for a lossless capture, e.g. reading small text or a diagram.",
            },
            "quality": {
                "type": "integer",
                "description": "JPEG quality 1-100 (default 70; ignored for format:'png').",
            },
            "marks": {
                "type": "boolean",
                "description": (
                    "Draw a numbered badge on every interactive element (from the tab's "
                    "current index map) before capturing, then remove them — so 'click the "
                    "blue button' maps straight to an idx instead of a guessed xy. Capped at "
                    "150 badges, prioritising elements in the current viewport. The result's "
                    "`marks` array reports each badge's idx and CSS-px position."
                ),
            },
            "viewport": {
                "type": "object",
                "properties": {
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                },
                "description": (
                    "speedimprovements.md G2: emulate a taller/wider page for THIS call only "
                    "(width 320-3840, height 240-8000) — same meaning as browser_bridge_snapshot's "
                    "own `viewport`. Never resizes or moves the user's real window. Full mode "
                    "only. See the result's `viewport_used`."
                ),
            },
        },
        "required": [],
    },
}

_VALID_FORMATS = ("png", "jpeg")


def _validate_scale(scale: Any) -> Tuple[Optional[float], str]:
    """None (not given) is valid and means "default"; returns (value, error)."""
    if scale is None:
        return None, ""
    if isinstance(scale, bool) or not isinstance(scale, (int, float)):
        return None, f"scale must be a number (got {scale!r})"
    value = float(scale)
    if not (0.25 <= value <= 1):
        return None, "scale must be between 0.25 and 1"
    return value, ""


def _validate_quality(quality: Any) -> Tuple[Optional[int], str]:
    if quality is None:
        return None, ""
    if isinstance(quality, bool) or not isinstance(quality, int):
        return None, f"quality must be an integer (got {quality!r})"
    if not (1 <= quality <= 100):
        return None, "quality must be between 1 and 100"
    return quality, ""


def handle_screenshot(ctx, args: Dict[str, Any], **kwargs: Any) -> Any:
    """Returns a plain JSON string (ocr+layout/text-only, and pixels when the
    embed couldn't be built) or the ``_multimodal`` envelope dict for a
    successful pixels embed. Both are valid handler results per
    ``tools.registry.ToolRegistry._normalize_handler_result``."""
    region: Optional[Dict[str, int]] = None
    if args.get("region") is not None:
        region, message = _validate_region(args["region"])
        if region is None:
            return _err(message, code=protocol.INVALID_PARAMS)
    if args.get("idx") is not None and (isinstance(args["idx"], bool) or not isinstance(args["idx"], int)):
        return _err(f"idx must be an integer (got {args['idx']!r})", code=protocol.INVALID_PARAMS)
    scale, message = _validate_scale(args.get("scale"))
    if message:
        return _err(message, code=protocol.INVALID_PARAMS)
    quality, message = _validate_quality(args.get("quality"))
    if message:
        return _err(message, code=protocol.INVALID_PARAMS)
    fmt = args.get("format")
    if fmt is not None and fmt not in _VALID_FORMATS:
        return _err(f"format must be one of {_VALID_FORMATS} (got {fmt!r})", code=protocol.INVALID_PARAMS)
    marks = bool(args.get("marks"))

    # speedimprovements.md G2: shape-only validation here (numeric width/
    # height) -- the extension is the source of truth for the 320-3840/
    # 240-8000 bounds and the full-mode-only rule; those come back as
    # ordinary bridge errors (VIEWPORT_OUT_OF_RANGE/LIMITED_MODE_CAPABILITY_UNAVAILABLE)
    # via `_bridge_err` below.
    viewport_req: Optional[Dict[str, int]] = None
    if args.get("viewport") is not None:
        vp = args["viewport"]
        if not isinstance(vp, dict) or not isinstance(vp.get("width"), (int, float)) or not isinstance(vp.get("height"), (int, float)):
            return _err("viewport must be an object with numeric width and height", code=protocol.INVALID_PARAMS)
        viewport_req = {"width": int(vp["width"]), "height": int(vp["height"])}

    try:
        device_id, tab, origin = _resolve_target(args)
    except _TargetError as exc:
        return exc.error_json
    tab_id = tab.get("tabId")

    relay = relay_mod.get_relay()
    if relay is None:
        return _err("relay not running")

    # Gateway-side grant enforcement (plan §6.2), re-checked on EVERY call.
    #
    # Screenshot is a read-only capability, so it uses `_gate_reason` — the same
    # M1 gate as snapshot/read/attach — rather than `_authorize`, which is for
    # the mutating/revealing calls (act, ask-with-candidates) that route through
    # approvals. Mixing the two here would mean an origin the user set to
    # `request` refuses a snapshot while a screenshot of the same pixels parks
    # for approval, which is a strictly worse disclosure story than the cheaper
    # tool it would be standing in for.
    #
    # Re-checking matters independently of what attach did. `browser_bridge_attach`
    # requires `full`, so a tab can only become attached on a granted origin — but
    # attach state outlives the grant: the user can set that origin back to `off`
    # while the tab stays attached. snapshot/read read `state.get_mode()` fresh on
    # every call and start refusing immediately; screenshot must too, or it becomes
    # the one way to keep reading a page the user has just revoked. It is also the
    # most revealing of the three, because redaction cannot blank what a canvas has
    # already painted.
    refusal = tools_mod._gate_reason(device_id, origin, "screenshot")
    if refusal is not None:
        audit.record(
            "tool_screenshot_denied", device_id=device_id, tab_id=tab_id, origin=origin, reason=refusal
        )
        return _err(
            refusal, code=protocol.GRANT_DENIED, hint=tools_mod._hint_for_code(protocol.GRANT_DENIED),
            device_id=device_id, tab_id=tab_id, origin=origin,
        )

    params: Dict[str, Any] = {"tabId": tab_id}
    if args.get("full"):
        params["full"] = True
    if region is not None:
        params["region"] = region
    if scale is not None:
        params["scale"] = scale
    if fmt is not None:
        params["format"] = fmt
    if quality is not None:
        params["quality"] = quality
    if marks:
        params["marks"] = True
        # D1: badges may only cover elements in frames this device's grants
        # actually authorise -- same origin-policy triple dom.snapshot's
        # handle_snapshot forwards, and the same reasoning (an EMBEDDED
        # frame's own origin, not the top tab's already-checked one, gates
        # its content -- see handle_snapshot's own comment for why
        # default_full is always False here too).
        granted_origins, denied_origins = tools_mod._origin_policy_for_device(device_id)
        params["granted_origins"] = granted_origins
        params["denied_origins"] = denied_origins
        params["default_full"] = False
    if viewport_req is not None:
        params["viewport"] = viewport_req

    # idx -> selector through the attach registry, with handle_act's
    # missing-idx and captured-on-a-different-page refusals.
    if args.get("idx") is not None:
        idx = int(args["idx"])
        registry = attach_mod.get_registry()
        selector = registry.resolve_index(device_id, tab_id, idx)
        if selector is None:
            audit.record("tool_screenshot_error", device_id=device_id, tab_id=tab_id, origin=origin, error=f"idx {idx} not in index map")
            return _err(
                f"idx {idx} is not in tab {tab_id}'s current index map",
                code=protocol.INVALID_PARAMS,
                hint="the tab may have navigated or re-rendered since the last snapshot — call "
                     "browser_bridge_snapshot again and use a fresh idx",
            )
        map_meta = registry.index_map_meta(device_id, tab_id)
        current_url = str(tab.get("url") or "")
        if map_meta and map_meta.get("url") and current_url and map_meta["url"] != current_url:
            audit.record("tool_screenshot_error", device_id=device_id, tab_id=tab_id, origin=origin, error=f"idx {idx} stale")
            return _err(
                f"idx {idx} was captured on a different page ({map_meta['url']!r}) than tab {tab_id} is "
                f"on now ({current_url!r})",
                code=protocol.INVALID_PARAMS,
                hint="the tab navigated since the last snapshot/act — call browser_bridge_snapshot again "
                     "and use a fresh idx",
            )
        params["selector"] = selector
    elif args.get("selector"):
        params["selector"] = str(args["selector"])

    try:
        result = relay.call(device_id, "page.screenshot", params, timeout=30.0)
    except relay_mod.BridgeError as exc:
        audit.record("tool_screenshot_error", device_id=device_id, tab_id=tab_id, origin=origin, error=str(exc.message), code=exc.code)
        error_json = tools_mod._bridge_err(exc, "page.screenshot")
        if exc.code == protocol.CONTENT_SCRIPT_ERROR:
            # The shared hint points at this very tool; say what to change instead.
            error = json.loads(error_json)
            error["hint"] = (
                "the page refused script injection, so the element lookup could not run; retry "
                "browser_bridge_screenshot without idx/selector (use region to crop)"
                if "selector" in params else
                "the page refused script injection (chrome://, the Web Store and the PDF viewer always do)"
            )
            error_json = json.dumps(error, default=str)
        return error_json
    except Exception as exc:
        audit.record("tool_screenshot_error", device_id=device_id, tab_id=tab_id, origin=origin, error=str(exc))
        return _err(f"screenshot failed: {exc}")

    png_b64 = result.get("png_b64") or ""
    try:
        png_bytes = base64.b64decode(png_b64) if png_b64 else b""
    except Exception:
        png_bytes = b""
    # Contract: page.screenshot's result carries `viewport: {width, height}`
    # alongside `boxes` (so the two are read from the same place a box's
    # coordinates are relative to). Fall back to top-level width/height for
    # an older extension build that predates `viewport` — degrade, don't
    # throw, same policy as build_layout_description's box tolerance below.
    viewport = result.get("viewport") if isinstance(result.get("viewport"), dict) else {}
    width = int(viewport.get("width") or result.get("width") or 0)
    height = int(viewport.get("height") or result.get("height") or 0)
    boxes = result.get("boxes") or []
    clip = result.get("clip") if isinstance(result.get("clip"), dict) else None
    capture_note = str(result.get("note") or "")
    # D2: the returned image's own size/encoding/scale, and D1's drawn
    # badges — folded into every fidelity branch's payload by
    # finalize_screenshot_result's `_base`, not just the pixels one, since
    # they describe the CAPTURE, not the vision embed.
    image = result.get("image") if isinstance(result.get("image"), dict) else None
    result_scale = result.get("scale")
    result_format = result.get("format")
    result_marks = result.get("marks") if isinstance(result.get("marks"), list) else None
    # speedimprovements.md G2: present only when `viewport` was requested AND
    # the extension actually applied the override — see handle_snapshot's own
    # identically-shaped extraction of the same wire field.
    raw_viewport_used = result.get("viewportUsed")
    result_viewport_used: Optional[Dict[str, int]] = None
    if isinstance(raw_viewport_used, dict) and isinstance(raw_viewport_used.get("width"), (int, float)) and isinstance(raw_viewport_used.get("height"), (int, float)):
        result_viewport_used = {"width": int(raw_viewport_used["width"]), "height": int(raw_viewport_used["height"])}

    saved_path: Optional[Path] = None
    if png_bytes:
        try:
            saved_path = save_screenshot(png_bytes)
        except Exception:
            logger.exception("browser_bridge: failed to persist screenshot to disk")

    vision_state = get_vision_state(ctx)
    result, fidelity = finalize_screenshot_result(
        vision_state, png_bytes, boxes, width, height, saved_path, clip=clip, capture_note=capture_note,
        image=image, scale=result_scale, format=result_format, marks=result_marks,
        viewport_used=result_viewport_used,
    )

    audit.record(
        "tool_screenshot",
        device_id=device_id,
        tab_id=tab_id,
        origin=origin,
        fidelity=fidelity,
        vision_state=vision_state.get("capability"),
    )
    return result


def register_vision_tools(ctx) -> List[str]:
    """Entry point tools.py imports under ``try/except ImportError`` and
    calls once during its own registration. Registers
    ``browser_bridge_screenshot`` with the same service gate as the rest of
    the toolset (``tools.bridge_available``: a paired device is online) so
    an unpaired session pays no schema weight for it either.
    """

    def _handler(args: Dict[str, Any], **kwargs: Any) -> Any:
        # kwargs carries session_id, which the approval gate needs to scope a
        # "approve for this session" decision to the session that asked.
        return handle_screenshot(ctx, args, **kwargs)

    ctx.register_tool(
        name=SCREENSHOT_SCHEMA["name"],
        toolset=tools_mod.TOOLSET,
        schema=SCREENSHOT_SCHEMA,
        handler=_handler,
        check_fn=tools_mod.bridge_available,
        emoji="📸",
    )
    return [SCREENSHOT_SCHEMA["name"]]
