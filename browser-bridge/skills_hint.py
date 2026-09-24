"""Plugin-authored `skills` block on tool results (ProjectRules/skilllinks.md
SL2). Tells the model which product skill and which `browser-bridge`
reference already cover the product behind the tab it just attached to, or
navigated, snapshotted, or acted in -- so it loads what Hermes already knows
instead of re-deriving it, and knows where a new lesson belongs.

Called from `tools.py`'s `_wrap_handler_with_timing`, deliberately AFTER that
wrapper's own suspicious-text scan (skilllinks.md SS1.3): a product skill's
own description can legitimately contain a tool name or phrase the scanner
reacts to (`browser_bridge_...`, "call the tool"), and this block must never
be the thing that trips that page-injection detector.

Fails open unconditionally: `apply()` never lets an exception escape, and on
one it leaves `data` exactly as the wrapper already built it -- a bug here
must never turn a working tool call into a broken one.
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import audit
from . import skill_links

# The bundled umbrella skill's manual -- kept out of <available_skills> by
# Hermes itself (explicit-loads-only), so a holder that never happens to read
# SKILL.md's own §0b needs this spelled out once, in-band.
MANUAL_LOAD = "skill_view('browser-bridge:browser-bridge')"

# skilllinks.md SL2.3/SL2.2: cap each list at 3 entries, the whole serialized
# block at ~600 bytes, and the two process-lifetime dedupe sets at 2000
# entries each (bounded, oldest evicted) -- the same "stay small" budget the
# rest of this plugin holds itself to.
MAX_LIST_ITEMS = 3
MAX_BLOCK_BYTES = 600
_MAX_DEDUPE_ENTRIES = 2000

_TOOLS_WITH_SKILLS_HINT = frozenset(
    {
        "browser_bridge_attach",
        "browser_bridge_open_tab",
        "browser_bridge_snapshot",
        "browser_bridge_act",
    }
)

_PAGE_TITLE_NOTE = (
    "matched on the page's own title, which the page controls — confirm it is the "
    "product before relying on it"
)

# Mirrors tools.py's own `_session_key` exactly (same kwarg names, same
# default), duplicated rather than imported: this module must never call
# `_resolve_session` (it creates/touches agent_sessions rows, which a hint
# path has no business doing), and importing anything else from tools.py here
# would set up a circular import (tools.py is what calls into this module).
_SESSION_KWARG_NAMES = ("session_id", "session_key", "conversation_id", "session")
_DEFAULT_SESSION = "default-session"


def _fallback_session_key(kwargs: Dict[str, Any]) -> str:
    for name in _SESSION_KWARG_NAMES:
        value = kwargs.get(name)
        if value:
            return str(value)
    return _DEFAULT_SESSION


def _origin_of(url: str) -> str:
    """Same rule as tools.py's `_origin_of` (duplicated for the same
    no-circular-import reason as `_fallback_session_key` above)."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return parts.scheme + ":" if parts.scheme else url


# --- process-lifetime dedupe state ------------------------------------------
#
# Three independent bounded LRU-ish maps, one lock. `_emitted_origins` is
# SL2.2's "once per (holder, origin) per gateway process" set; `_manual_sent`
# is the same idea for the one-shot `manual` hint; `_last_act_origin` is
# act's own cheap pre-filter (see `_act_should_check`) and is NOT a dedupe
# set by itself -- it just remembers what origin act last saw for a given
# (holder, tab) so a run of same-page clicks doesn't re-run the matcher on
# every single call.

_lock = threading.Lock()
_emitted_origins: "OrderedDict[Tuple[str, str], None]" = OrderedDict()
_manual_sent: "OrderedDict[str, None]" = OrderedDict()
_last_act_origin: "OrderedDict[Tuple[str, Any], str]" = OrderedDict()


def _evict(store: "OrderedDict[Any, Any]") -> None:
    while len(store) > _MAX_DEDUPE_ENTRIES:
        store.popitem(last=False)


def _mark_emitted_if_new(holder: str, origin: str) -> bool:
    key = (holder, origin)
    with _lock:
        if key in _emitted_origins:
            _emitted_origins.move_to_end(key)
            return False
        _emitted_origins[key] = None
        _evict(_emitted_origins)
        return True


def _mark_manual_if_first(holder: str) -> bool:
    with _lock:
        if holder in _manual_sent:
            return False
        _manual_sent[holder] = None
        _evict(_manual_sent)
        return True


def _act_should_check(holder: str, tab_id: Any, origin: str, navigated: bool) -> bool:
    """SL2.1's act-specific gate: only bother matching when this action
    navigated, or the tab's origin has moved since act last looked at it.
    An already-attached tab's origin was already announced by `attach`
    (or by a prior `act` that DID trigger this), so most clicks/types/
    scrolls on the same page have nothing new to say here."""
    key = (holder, tab_id)
    with _lock:
        last = _last_act_origin.get(key)
        changed = navigated or last != origin
        if key in _last_act_origin:
            _last_act_origin.move_to_end(key)
        _last_act_origin[key] = origin
        _evict(_last_act_origin)
        return changed


# --- extracting (origin, title) targets out of each tool's own result shape -

def _targets_for(name: str, data: Dict[str, Any]) -> List[Tuple[str, str, Any]]:
    """(origin, title, tab_id) candidates this result names, most-relevant
    first, deduplicated by origin, capped at MAX_LIST_ITEMS."""
    if name == "browser_bridge_attach":
        tabs = data.get("attached")
        if not isinstance(tabs, list):
            return []
        out: List[Tuple[str, str, Any]] = []
        seen: set = set()
        for tab in tabs:
            if not isinstance(tab, dict):
                continue
            origin = tab.get("origin") or _origin_of(str(tab.get("url", "") or ""))
            if not origin or origin in seen:
                continue
            seen.add(origin)
            out.append((origin, str(tab.get("title", "") or ""), tab.get("tabId")))
            if len(out) >= MAX_LIST_ITEMS:
                break
        return out

    if name == "browser_bridge_open_tab":
        tab = data.get("tab")
        if not isinstance(tab, dict):
            return []
        origin = tab.get("origin") or _origin_of(str(tab.get("url", "") or ""))
        if not origin:
            return []
        return [(origin, str(tab.get("title", "") or ""), tab.get("tabId"))]

    if name in ("browser_bridge_snapshot", "browser_bridge_act"):
        origin = _origin_of(str(data.get("url", "") or ""))
        if not origin:
            return []
        return [(origin, str(data.get("title", "") or ""), data.get("tab_id"))]

    return []


# --- `learn` sentence selection (skilllinks.md SL2.4, exact wording) --------

_LEARN_NO_REFERENCE_NO_SKILL = (
    "No bridge reference or product skill covers this yet. Save what works as "
    "references/<product>.md under browser-bridge with a products/origins header; "
    "link any other skill for it via related_skills: [browser-bridge]."
)
_LEARN_SKILL_NO_REFERENCE = (
    "A product skill covers this product but no bridge reference exists. Load the skill "
    "for domain knowledge, then save browser-specific lessons as a reference here and list "
    "the skill in its skills: header."
)
_LEARN_REFERENCE_UNLINKED_WRITABLE = (
    "A bridge reference already covers this product, but the matching skill isn't linked. "
    "Add related_skills: [browser-bridge] to that skill so it points here too."
)


def _learn_sentence(
    product_skills: List[Dict[str, Any]], bridge_references: List[Dict[str, Any]]
) -> Optional[str]:
    if not product_skills and not bridge_references:
        return _LEARN_NO_REFERENCE_NO_SKILL
    if product_skills and not bridge_references:
        return _LEARN_SKILL_NO_REFERENCE
    if bridge_references and not product_skills:
        # A reference with no product skill at all -- nothing to link, omit.
        return None

    unlinked = [s for s in product_skills if not s.get("linked")]
    if not unlinked:
        return None  # everything linked

    if any(s.get("protected") is False for s in unlinked):
        return _LEARN_REFERENCE_UNLINKED_WRITABLE

    # Every unlinked skill is protected True or unknown (None).
    name = unlinked[0].get("name", "")
    return (
        "A bridge reference covers this product; the matching skill isn't linked and may be "
        f"protected. Load both; if they should link, tell the user: hermes curator adopt {name}."
    )


# --- block construction / size cap ------------------------------------------

def _block_bytes(block: Dict[str, Any]) -> int:
    return len(json.dumps(block, separators=(",", ":"), default=str))


def _enforce_size_cap(block: Dict[str, Any]) -> None:
    """Trim descriptions first, then drop list entries one at a time
    (product_skills, then bridge_references, then load), then `learn`, then
    `note` -- never touching `origin`/`matched_by`, the two fields worth
    keeping even in a maximally trimmed block."""
    if _block_bytes(block) <= MAX_BLOCK_BYTES:
        return

    product_skills = block.get("product_skills")
    if isinstance(product_skills, list):
        for entry in product_skills:
            if _block_bytes(block) <= MAX_BLOCK_BYTES:
                return
            if entry.get("description"):
                entry["description"] = entry["description"][:40].rstrip()
        while product_skills and _block_bytes(block) > MAX_BLOCK_BYTES:
            product_skills.pop()
        if not product_skills:
            block.pop("product_skills", None)
    if _block_bytes(block) <= MAX_BLOCK_BYTES:
        return

    bridge_references = block.get("bridge_references")
    if isinstance(bridge_references, list):
        while bridge_references and _block_bytes(block) > MAX_BLOCK_BYTES:
            bridge_references.pop()
        if not bridge_references:
            block.pop("bridge_references", None)
    if _block_bytes(block) <= MAX_BLOCK_BYTES:
        return

    load = block.get("load")
    if isinstance(load, list):
        while load and _block_bytes(block) > MAX_BLOCK_BYTES:
            load.pop()
        if not load:
            block.pop("load", None)
    if _block_bytes(block) <= MAX_BLOCK_BYTES:
        return

    block.pop("learn", None)
    if _block_bytes(block) <= MAX_BLOCK_BYTES:
        return
    block.pop("note", None)


def _build_block(
    origin: str, matched_by: Optional[str], product_skills: List[Dict[str, Any]], bridge_references: List[Dict[str, Any]]
) -> Dict[str, Any]:
    product_skills = product_skills[:MAX_LIST_ITEMS]
    bridge_references = bridge_references[:MAX_LIST_ITEMS]

    block: Dict[str, Any] = {"origin": origin}
    if matched_by:
        block["matched_by"] = matched_by

    load: List[str] = [f"skill_view('{s['name']}')" for s in product_skills if s.get("name")]
    load.extend(r["load"] for r in bridge_references if r.get("load"))
    if load:
        block["load"] = load[:MAX_LIST_ITEMS]

    if product_skills:
        block["product_skills"] = [
            {"name": s.get("name"), "description": s.get("description", "")} for s in product_skills
        ]
    if bridge_references:
        block["bridge_references"] = [
            {"file": r.get("file"), "heading": r.get("heading", "")} for r in bridge_references
        ]

    learn = _learn_sentence(product_skills, bridge_references)
    if learn:
        block["learn"] = learn

    if matched_by == "page_title":
        block["note"] = _PAGE_TITLE_NOTE

    _enforce_size_cap(block)
    return block


def _audit_block(tool: str, origin: str, matched_by: Optional[str], product_skills: List[Dict[str, Any]], bridge_references: List[Dict[str, Any]]) -> None:
    try:
        audit.record(
            "skills_hint",
            tool=tool,
            origin=origin,
            matched_by=matched_by,
            product_skills=[s.get("name") for s in product_skills],
            bridge_references=[r.get("file") for r in bridge_references],
        )
    except Exception:
        pass


# --- public entry point ------------------------------------------------------

def apply(name: str, data: Dict[str, Any], kwargs: Dict[str, Any]) -> None:
    """Add `data["skills"]` in place when `name` is one of the four tools
    this covers and there is something to say. Called from
    `_wrap_handler_with_timing` AFTER the suspicious-text scan (see module
    docstring). Fails open unconditionally -- never raises, never leaves a
    half-built key behind."""
    if name not in _TOOLS_WITH_SKILLS_HINT or not isinstance(data, dict):
        return
    if data.get("success") is False:
        return
    try:
        _apply(name, data, kwargs)
    except Exception:
        pass


def _apply(name: str, data: Dict[str, Any], kwargs: Dict[str, Any]) -> None:
    holder = data.get("holder") or _fallback_session_key(kwargs)
    if not holder:
        return

    targets = _targets_for(name, data)
    if not targets:
        return

    if name == "browser_bridge_act":
        origin, title, tab_id = targets[0]
        navigated = data.get("action") == "navigate"
        if not _act_should_check(holder, tab_id, origin, navigated):
            return

    blocks: List[Dict[str, Any]] = []
    for origin, title, _tab_id in targets:
        if not _mark_emitted_if_new(holder, origin):
            continue
        try:
            match = skill_links.match_page(origin, title)
        except Exception:
            continue
        matched_by = match.get("matched_by")
        product_skills = match.get("product_skills") or []
        bridge_references = match.get("bridge_references") or []
        block = _build_block(origin, matched_by, product_skills, bridge_references)
        blocks.append(block)
        _audit_block(name, origin, matched_by, product_skills[:MAX_LIST_ITEMS], bridge_references[:MAX_LIST_ITEMS])
        if len(blocks) >= MAX_LIST_ITEMS:
            break

    if not blocks:
        return

    if _mark_manual_if_first(holder):
        blocks[0]["manual"] = MANUAL_LOAD

    data["skills"] = blocks[0] if len(blocks) == 1 else blocks
