"""Skill-side half of Skill Links (skilllinks.md SL3): annotates ``skill_view``
results so a session (foreground or the background skill reviewer) loading a
product skill or the local ``browser-bridge`` umbrella sees what
``skill_links.py`` already knows about the other side, without Hermes core
ever being touched.

Two plugin hooks, both registered from ``__init__.py``'s ``register(ctx)``:

* ``transform_tool_result`` -- fires after every tool call
  (``model_tools.py``'s ``_apply_transform_tool_result_hook``). Acts only on
  a successful, non-dedup ``skill_view`` result and adds one top-level
  ``browser_bridge`` key, or returns ``None`` to leave the result untouched.
  ``content`` and every other existing key are never modified.
* ``on_skill_lifecycle`` -- fires after an authoritative skill-state change
  (a patch, a link, a create). Simply invalidates ``skill_links``' cache so
  the very next ``skill_view`` sees the fresh state instead of waiting out
  the cache's own throttle window.

Fail-open throughout: a malformed result, an unindexed skill, a missing
Hermes helper, or any exception degrades to "do nothing" rather than
touching the transcript the model reads.
"""
from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import attach, relay, skill_links, state

logger = logging.getLogger(__name__)

# Blocks stay small (skilllinks.md §1.4): at most this many entries in any
# list this module adds to a tool result.
_MAX_ITEMS = 3

_REFERENCES_PREFIX = "references/"
_UMBRELLA = skill_links.BROWSER_BRIDGE_SKILL_NAME  # "browser-bridge"

# rev2's "count uses fairly": once per (holder, origin, skill, file_path) per
# gateway process, so one session re-loading the same reference or product
# skill can never reach PROMOTE_AFTER_USES on its own -- a promotion
# candidate is meant to reflect independent uses (different tasks, likely
# different sessions), not one session replaying the same view. Bounded and
# never reset short of a process restart, same lifetime as skills_hint.py's
# own dedupe sets.
_MAX_RECORDED_USES = 2000
_use_lock = threading.Lock()
_recorded_uses: "OrderedDict[Tuple[str, str, str, str], None]" = OrderedDict()


def _mark_use_if_new(holder: str, origin: str, skill: str, file_path: str) -> bool:
    key = (holder, origin, skill, file_path)
    with _use_lock:
        if key in _recorded_uses:
            _recorded_uses.move_to_end(key)
            return False
        _recorded_uses[key] = None
        while len(_recorded_uses) > _MAX_RECORDED_USES:
            _recorded_uses.popitem(last=False)
        return True


# --- plugin registration -----------------------------------------------------

def register(ctx) -> None:
    """Called from ``hermes_plugin/__init__.py``'s ``register(ctx)``."""
    try:
        ctx.register_hook("transform_tool_result", on_transform_tool_result)
        ctx.register_hook("on_skill_lifecycle", on_skill_lifecycle)
    except Exception:
        logger.exception("browser_bridge: skill-link hook registration failed")


# --- on_skill_lifecycle -------------------------------------------------------

def on_skill_lifecycle(**_kwargs: Any) -> None:
    """A skill was created/patched/linked/archived elsewhere -- the index
    this module reads may now be stale. Bypass the cache's own throttle
    rather than waiting up to 10s for a rebuild (skill_links.py's
    ``_CACHE_CHECK_INTERVAL_SECONDS``)."""
    try:
        skill_links.invalidate()
    except Exception:
        logger.debug("browser_bridge: skill_links.invalidate() failed after a skill lifecycle event", exc_info=True)


# --- transform_tool_result ----------------------------------------------------

def on_transform_tool_result(
    *, tool_name: str = "", args: Optional[Dict[str, Any]] = None, result: Any = None,
    session_id: str = "", task_id: str = "", **_extra: Any,
) -> Optional[str]:
    """Every tool call in the gateway runs through this -- bail immediately
    for anything but ``skill_view`` before doing any parsing at all."""
    if tool_name != "skill_view":
        return None
    try:
        return _transform(args or {}, result, session_id or "", task_id or "")
    except Exception:
        return None


def _transform(args: Dict[str, Any], result: Any, session_id: str, task_id: str) -> Optional[str]:
    if not isinstance(result, str):
        return None
    try:
        parsed = json.loads(result)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict) or parsed.get("success") is not True:
        return None
    if parsed.get("dedup"):  # skills_tool_dedup.py's repeat-view stub
        return None

    raw_name = str(args.get("name") or "")
    name = str(parsed.get("name") or raw_name)
    if not name or ":" in name or ":" in raw_name:  # plugin-qualified; out of scope
        return None

    file_path = str(args.get("file_path") or "")
    holder = _mapped_holder(session_id, task_id)

    if file_path and file_path != "SKILL.md":
        # A linked-file view (references/templates/scripts/... under a skill
        # dir). Only browser-bridge's own references/ carry any signal here,
        # and even then only a used-observation, never a new key.
        if name == _UMBRELLA and file_path.startswith(_REFERENCES_PREFIX) and holder:
            for origin, title in _attached_origins_for_holder(holder):
                _record_reference_if_corroborated(holder, origin, title, file_path)
        return None

    # SKILL.md view from here on.
    if name == _UMBRELLA:
        parsed["browser_bridge"] = _umbrella_block()
        return json.dumps(parsed, default=str)

    # Recorded BEFORE (and regardless of) match_skill's own overlap check --
    # otherwise a product with no reference/link/observation yet (the common
    # case for a new product: nothing to say today) could never bootstrap its first
    # observation, since match_skill only returns non-None once one already
    # exists. Gated on an independent match_page(origin, title) call so
    # viewing THIS skill only ever binds an origin actually corroborated as
    # relevant to it -- see _record_if_corroborated's docstring for why (an
    # unrelated attached tab must record nothing).
    if holder:
        for origin, title in _attached_origins_for_holder(holder):
            _record_skill_if_corroborated(holder, origin, title, name)

    match = skill_links.match_skill(name)
    if match is None:
        return None

    parsed["browser_bridge"] = _product_skill_block(match, _is_review_fork())
    return json.dumps(parsed, default=str)


# --- block builders ------------------------------------------------------------

def _umbrella_block() -> Dict[str, Any]:
    rows = [_trim_row(row) for row in skill_links.products_index()[:8]]
    return {
        "products": rows,
        "note": "Products the bridge has references for; fix missing_links when you patch, "
                "and promote a listed origin into the matching header when a row carries one.",
    }


def _trim_row(row: Dict[str, Any]) -> Dict[str, Any]:
    trimmed = dict(row)
    for key, value in row.items():
        if isinstance(value, list):
            trimmed[key] = value[:_MAX_ITEMS]
    return trimmed


def _product_skill_block(match: Dict[str, Any], in_review: bool) -> Dict[str, Any]:
    refs = sorted(match.get("bridge_references") or [], key=lambda r: r["file"])
    trimmed_refs = [{"file": r["file"], "load": r["load"]} for r in refs[:_MAX_ITEMS]]
    observed = list(match.get("observed_origins") or [])[:_MAX_ITEMS]

    linked = bool(match.get("linked"))
    # `ref_names_skill` may not exist yet (a concurrent change to
    # skill_links.py's match_skill result) -- absent means "not gated on
    # this half at all", never "fails" it.
    ref_names_declared = "ref_names_skill" in match
    ref_names_ok = (not ref_names_declared) or bool(match.get("ref_names_skill"))
    link_state = "linked" if (linked and ref_names_ok) else "missing"

    prefix = "Do now:" if in_review else "When this task is done:"
    suggested: List[str] = []
    if not linked:
        suggested.append(f"{prefix} {_skill_side_text(match, refs)}")
    if ref_names_declared and not match.get("ref_names_skill"):
        text = _reference_side_text(match, refs)
        if text:
            suggested.append(f"{prefix} {text}")

    promote_text = _promote_text_for_skill(match.get("name", ""))
    if promote_text:
        suggested.append(f"{prefix} {promote_text}")

    return {
        "bridge_references": trimmed_refs,
        "observed_origins": observed,
        "bridge_connected": _bridge_connected(),
        "link": link_state,
        "suggested": suggested,
    }


def _promote_text_for_skill(name: str) -> Optional[str]:
    """rev2: this skill's own promotion candidates (an observed origin used
    enough times that isn't yet in its metadata.browser_bridge.origins), as
    one sentence -- the same signal skills_hint.py's `promote` field and the
    CLI's `promote:` line read, scoped here to just this skill."""
    if not name:
        return None
    candidates = [c for c in skill_links.promotion_candidates() if c.get("skill") == name]
    if not candidates:
        return None
    best = sorted(candidates, key=lambda c: (-c.get("uses", 0), c.get("file_path", "")))[0]
    return skill_links.promotion_sentence(best)


def _skill_side_text(match: Dict[str, Any], sorted_refs: List[Dict[str, Any]]) -> str:
    if match.get("protected") is False:
        if sorted_refs:
            return (
                "Add browser-bridge to this skill's metadata.hermes.related_skills and "
                f"one line under '## Via Browser Bridge' pointing to browser-bridge {sorted_refs[0]['file']}."
            )
        return "Add browser-bridge to this skill's metadata.hermes.related_skills."
    reason = match.get("protected_reason")
    name = match.get("name", "")
    hint = skill_links.protected_reason_hint(reason, name)
    if reason in ("bundled", "hub", "external"):
        return f"This skill can't be auto-linked -- it's {hint}."
    return (
        "This skill can't be auto-linked (protected or unknown provenance). If it should "
        f"link, tell the user: {hint}."
    )


def _reference_side_text(match: Dict[str, Any], sorted_refs: List[Dict[str, Any]]) -> Optional[str]:
    if not sorted_refs:
        return None
    return f"Add {match['name']} to the skills: header of browser-bridge {sorted_refs[0]['file']}."


def _is_review_fork() -> bool:
    try:
        from tools.skill_provenance import is_background_review
    except Exception:
        return False
    try:
        return bool(is_background_review())
    except Exception:
        return False


def _bridge_connected() -> bool:
    """Any paired device connected to the relay right now -- an in-process
    check only (relay.py's own connection registry), never a network call."""
    try:
        r = relay.get_relay()
        if r is None:
            return False
        return bool(r.status().get("connected"))
    except Exception:
        return False


# --- session -> attached origins, read-only -----------------------------------

def _mapped_holder(session_id: str, task_id: str) -> Optional[str]:
    """Best-effort, read-only resolution of the hook's raw session identity
    to the string attach.py's lease holder would carry for it.

    Deliberately does NOT call tools.py's ``_resolve_session``: that
    function creates an ``agent_sessions``/``session_bindings`` row for a
    raw key seen for the first time, and this module must never write on
    behalf of a call that only reads a tool result. A plain, keyless-by-
    device lookup against ``session_bindings`` covers the one case that
    matters (a prior ``browser_bridge_session resume`` bound this raw key to
    a different, older session); when no such row exists, the raw value
    itself already equals what ``ensure_shadow_session`` would have used --
    it prefers minting the session row's id AS the raw key -- so no DB read
    is required to get the common case right.
    """
    raw = session_id or task_id
    if not raw:
        return None
    try:
        conn = state.connect()
        row = conn.execute(
            "SELECT agent_session_id FROM session_bindings WHERE hermes_session_key = ?"
            " ORDER BY bound_at DESC LIMIT 1",
            (raw,),
        ).fetchone()
    except Exception:
        return raw
    if row is not None:
        try:
            return row["agent_session_id"]
        except Exception:
            return raw
    return raw


def _attached_origins_for_holder(holder: str, limit: int = _MAX_ITEMS) -> List[Tuple[str, str]]:
    """Distinct ``(origin, title)`` pairs for tabs this holder currently has
    attached (attach.py's lease ``tab_ref``), capped at ``limit`` distinct
    origins. ``title`` is whatever the lease's most recent
    snapshot/attach/act reported -- page-authored, so it is only ever used
    downstream as a corroborating HINT (see ``_record_if_corroborated``),
    never written as a trusted fact on its own."""
    if not holder:
        return []
    origins: List[Tuple[str, str]] = []
    seen = set()
    try:
        leases = attach.get_registry().snapshot()
    except Exception:
        return []
    for lease in leases:
        if lease.get("holder") != holder:
            continue
        origin = _origin_of(lease.get("url") or "")
        if origin and origin not in seen:
            seen.add(origin)
            origins.append((origin, str(lease.get("title") or "")))
        if len(origins) >= limit:
            break
    return origins


def _record_if_corroborated(holder: str, origin: str, title: str, *, skill: str, file_path: str = "") -> None:
    """Record a (origin, skill[, file_path]) observation only when an
    INDEPENDENT ``skill_links.match_page(origin, title)`` call actually names
    this same skill/reference for that origin.

    Without this check, viewing any product skill or browser-bridge
    reference while ANY unrelated tab happened to be attached would bind
    that tab's origin to it -- e.g. viewing `veeam-vbr-rest-api` while an
    ESXi tab is attached must record nothing, and viewing `esxi-ui.md` while
    a reddit tab is attached must not bind reddit to it either.

    The corroborating call's own ``matched_by`` grades how much the new row
    is trusted (see ``record_used``'s docstring): ``"page_title"`` is
    page-authored and stored as ``source="used_title"`` (never promotable to
    a trusted match by itself); every other ``matched_by`` (origin/used/
    hostname) already rests on a non-page-controlled signal and is stored as
    the fully trusted ``source="used"``.

    ``holder`` gates the actual write through `_mark_use_if_new` (rev2's
    "count uses fairly"): the corroboration check above still runs every
    time (cheap, and needed to keep `last_seen` honest for genuinely repeat
    uses), but a SECOND view of the exact same (holder, origin, skill,
    file_path) in this process never bumps `count` again -- one session
    replaying the same view must not be able to reach `PROMOTE_AFTER_USES`
    by itself.
    """
    try:
        m = skill_links.match_page(origin, title)
    except Exception:
        return
    if file_path:
        corroborated = file_path in {r["file"] for r in m.get("bridge_references", [])}
    else:
        corroborated = skill in {p["name"] for p in m.get("product_skills", [])}
    if not corroborated:
        return
    if not _mark_use_if_new(holder, origin, skill, file_path):
        return
    source = "used_title" if m.get("matched_by") == "page_title" else "used"
    skill_links.record_used(origin, skill, file_path, source=source)


def _record_skill_if_corroborated(holder: str, origin: str, title: str, skill: str) -> None:
    _record_if_corroborated(holder, origin, title, skill=skill)


def _record_reference_if_corroborated(holder: str, origin: str, title: str, file_path: str) -> None:
    _record_if_corroborated(holder, origin, title, skill=_UMBRELLA, file_path=file_path)


def _origin_of(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return ""
