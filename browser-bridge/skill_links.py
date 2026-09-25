"""Index of the skill library from the browser bridge's point of view.

Connects two things Hermes otherwise keeps apart: a *bridge reference*
(`references/<topic>.md` inside the local `browser-bridge` skill, holding
lessons learned by actually driving a site) and a *product skill* (any other
skill covering the same product through an API/CLI). Neither side names the
other today, so this module scans both, groups them by shared *product
terms* (normalized lowercase tokens such as `esxi`, `vcenter`), and
answers "what does Hermes already know about this origin/skill, and is it
connected to its counterpart yet." See ProjectRules/skilllinks.md §1-§2 for
the design rules and vocabulary this implements.

Read-only against the skill library. The one thing this module writes is a
bounded observation log in the bridge's own state.db (state.py's
`skill_link_observations` table) recording which (origin, skill) pairs have
actually been used together, so a real prior interaction becomes a trusted
match signal alongside a human-authored frontmatter header. Nothing here
ever writes into `~/.hermes/skills/` -- that stays Hermes' own job via
`skill_manage` (ledger, backups, protected-skill rules).

Every public function fails open: a scan error, a missing Hermes module, or
a malformed skill/reference degrades to "no match"/"unknown" rather than
raising, so a bug here can never break the tool call it was meant to enrich.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from . import state

# Generic words that never identify a product on their own (ProjectRules/
# skilllinks.md §2). Kept as one constant, covered by a dedicated test, so a
# future addition is a one-line, reviewable change.
BASE_STOPWORDS: frozenset = frozenset((
    "ui", "api", "web", "console", "admin", "client", "host", "server",
    "through", "via", "the", "and", "bridge", "browser", "skill", "query",
    "querying", "search", "report", "daily", "alerts", "security", "setup",
    "guide", "notes", "marketplace",
    # Bridge/generic vocabulary: words the browser-bridge tool surface and
    # its own reference prose use constantly (actions, UI chrome, browsing
    # nouns), which show up in nearly every reference regardless of what
    # product it's actually about -- left in, they mint fake "products" like
    # "fetch" or "listing" out of ordinary bridge mechanics.
    "fetch", "snapshot", "screenshot", "read", "act", "click", "type",
    "tab", "tabs", "page", "pages", "listing", "listings", "wizard",
    "dialog", "form", "network", "cookies", "research",
))

# A term appearing in more than this many skills' term sets is too common to
# identify a specific product (§2) and is folded into the dynamic stopword
# set computed at index-build time, alongside BASE_STOPWORDS above.
_COMMON_TERM_SKILL_THRESHOLD = 3

# How many independent uses of an unheadered origin earn it a promotion
# suggestion: "write this origin into the target's own header" (rev2's
# promotion_candidates). Independent means once per (holder, origin, skill,
# file_path) per gateway process -- skill_hooks.py enforces that count, this
# module just reads it back.
PROMOTE_AFTER_USES = 3

# Cap on how many promotion candidates a single products_index() row carries.
_PROMOTE_ROW_CAP = 3

# The bundled manual's own top heading -- used only to detect (never to fix)
# a deploy mistake where that file was copied over Hermes's own LEARNED
# browser-bridge skill (see bundled_manual_misplacement()).
BUNDLED_MANUAL_H1 = "# Using the Browser Bridge"

# How often the mtime/size stat walk that decides whether to rebuild may run
# (skilllinks.md's "Keep it cheap" rule). A call inside this window reuses
# whatever was cached at the last check, even if it is stale by a few
# seconds -- that staleness is the deliberate trade for never rescanning the
# skill library inside a hot tool-call path.
_CACHE_CHECK_INTERVAL_SECONDS = 10.0

# The local skill whose references/ subdirectory holds bridge references
# (§2). Only a LOCAL (non-external-dir) skill with this name counts --
# references are a lesson store the background reviewer patches directly on
# disk, not something an external/hub-owned skill package would carry.
BROWSER_BRIDGE_SKILL_NAME = "browser-bridge"

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.DOTALL)


# --- small tolerant parsing helpers -----------------------------------------

def _tokenize(*texts: Any) -> Set[str]:
    """Lowercase alphanumeric tokens (len >= 2) out of any number of texts --
    the normalized "product terms" vocabulary (§2). Falsy inputs are skipped
    so callers can pass optional fields without checking first."""
    terms: Set[str] = set()
    for text in texts:
        if not text:
            continue
        terms.update(t for t in _TOKEN_RE.findall(str(text).lower()) if len(t) >= 2)
    return terms


def _as_str_list(value: Any) -> List[str]:
    """A scalar-or-list frontmatter/config field as a list of stripped,
    non-empty strings. Mirrors the tolerant convention Hermes' own
    config/skill readers use: a bare scalar is one item, never its
    characters; ``None`` or anything else unexpected is empty rather than
    raising."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [s for s in (str(v).strip() for v in value) if s]


def _read_frontmatter_and_body(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Split ``---\\n...\\n---\\n`` YAML frontmatter off the front of a
    markdown file. Returns ``(None, text)`` when there is no fence, and also
    ``(None, body)`` when the fenced block fails to parse as YAML or does not
    parse to a mapping -- callers treat ``None`` as "no header", never as an
    error to propagate. A leading BOM is stripped first or it defeats the
    fence check, the same fix Hermes' own `parse_frontmatter` applies."""
    text = text.lstrip("﻿")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, text
    body = text[match.end():]
    try:
        import yaml

        parsed = yaml.safe_load(match.group(1))
    except Exception:
        return None, body
    return (parsed if isinstance(parsed, dict) else None), body


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _metadata_blocks(frontmatter: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
    """``(metadata.hermes, metadata.browser_bridge, has_bb_block)``. The two
    dicts are ``{}`` when absent or not a mapping; ``has_bb_block`` is
    whether ``metadata.browser_bridge`` was present AT ALL as a mapping --
    tracked separately from the (possibly empty) dict itself so a skill that
    declares an explicitly empty ``browser_bridge: {}`` still counts as
    having opted in (``bool({})`` is ``False``, so testing the dict's own
    truthiness would silently miss exactly that case)."""
    metadata = frontmatter.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    hermes_block = metadata.get("hermes")
    hermes_block = hermes_block if isinstance(hermes_block, dict) else {}
    bb_raw = metadata.get("browser_bridge")
    has_bb_block = isinstance(bb_raw, dict)
    bb_block = bb_raw if has_bb_block else {}
    return hermes_block, bb_block, has_bb_block


# --- HERMES_HOME / config resolution ----------------------------------------
#
# Mirrors state.py/config.py's own pattern (Path.home() as the base, no new
# HERMES_* env var introduced by US) generalized with the one env var
# skilllinks.md SL1.1 asks for: HERMES_HOME, which Hermes core itself already
# reads (hermes_constants.get_hermes_home()) to relocate its own home,
# skills included. Deliberately not importing that function: it pulls in a
# chunk of Hermes' own import graph for a one-line fallback we can express
# directly, and doing it ourselves keeps this module importable stand-alone
# in a plain test venv.

def _hermes_home() -> Path:
    override = os.environ.get("HERMES_HOME", "").strip()
    if override:
        return Path(os.path.expanduser(os.path.expandvars(override)))
    return Path.home() / ".hermes"


def _hermes_raw_config(home: Path) -> Dict[str, Any]:
    """Hermes' own merged config when `hermes_cli` is importable (the real
    gateway process, where profile overlays/env-var expansion should apply);
    otherwise a plain read of ``config.yaml`` under HERMES_HOME -- enough to
    see ``skills.*`` in any environment that has Hermes installed but not
    necessarily importable as a package (or this repo's own tests, which
    have neither). Missing/unreadable/malformed -> {}, never raises."""
    try:
        from hermes_cli.config import load_config  # type: ignore

        raw = load_config()
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    try:
        import yaml

        data = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _skills_config(home: Path) -> Dict[str, Any]:
    skills_cfg = _hermes_raw_config(home).get("skills")
    return skills_cfg if isinstance(skills_cfg, dict) else {}


def _skill_roots(home: Path, skills_cfg: Dict[str, Any]) -> List[Tuple[Path, bool]]:
    """``[(root, is_external), ...]``: the local skills dir first, then
    validated, deduplicated ``skills.external_dirs`` entries (``~``/``$VAR``
    expanded, relative entries resolved against HERMES_HOME -- Hermes' own
    convention), existing directories only."""
    local = home / "skills"
    roots: List[Tuple[Path, bool]] = [(local, False)]
    for entry in _as_str_list(skills_cfg.get("external_dirs")):
        candidate = Path(os.path.expanduser(os.path.expandvars(entry)))
        if not candidate.is_absolute():
            candidate = home / candidate
        if candidate == local or any(candidate == r for r, _ in roots):
            continue
        if candidate.is_dir():
            roots.append((candidate, True))
    return roots


def _disabled_skill_names(skills_cfg: Dict[str, Any]) -> Set[str]:
    return set(_as_str_list(skills_cfg.get("disabled")))


def _is_dot_path(path: Path, root: Path) -> bool:
    """True when any directory component between ``root`` and ``path``
    starts with ``.`` (``.archive``, `.curator_backups``, ``.locks``,
    ``.hub`` and friends) -- the filename itself is excluded from the
    check."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return any(part.startswith(".") for part in parts[:-1])


# --- provenance (writability) -----------------------------------------------

def _provenance_module():
    """Hermes' own skill-usage provenance helpers, or ``None`` when they are
    not importable (a stripped-down environment, e.g. this repo's own test
    suite, which is exactly why every caller of this treats ``None`` as
    "unknown", never as "not protected"). See tools/skill_usage.py on the
    v0.21.4 host: `is_bundled`, `is_hub_installed`, `get_record` (for
    `pinned`), `is_curator_managed`."""
    try:
        from tools import skill_usage

        return skill_usage
    except Exception:
        return None


def _compute_protected(provenance, name: str, external: bool) -> Tuple[Optional[bool], Optional[str]]:
    """Best-effort writability, plus WHY (skilllinks.md SL1.4, rev2's
    `protected_reason`): protected (not safely auto-patchable) when the
    skill is bundled, hub-installed, lives in an external skills dir, is
    pinned, or has never been adopted into curator management (`created_by:
    agent` -- SL3.3 only offers to edit a skill's frontmatter once it has
    been). The reason names exactly which of those applied
    (`"bundled"|"hub"|"external"|"pinned"|"unmanaged"`), so a caller can
    offer the fix that actually applies (adopt vs. unpin vs. "this is owned
    elsewhere, don't try"). ``(None, None)`` -- never a guessed
    ``(False, None)`` -- when the provenance module isn't importable or
    anything about the check raises: an unknown answer must never be read as
    "safe to write"."""
    if provenance is None:
        return None, None
    if external:
        return True, "external"
    try:
        if provenance.is_bundled(name):
            return True, "bundled"
        if provenance.is_hub_installed(name):
            return True, "hub"
        if bool(provenance.get_record(name).get("pinned")):
            return True, "pinned"
        if not provenance.is_curator_managed(name):
            return True, "unmanaged"
        return False, None
    except Exception:
        return None, None


def protected_reason_hint(reason: Optional[str], name: str) -> str:
    """The concrete action for a skill that can't be auto-linked, keyed by
    its `protected_reason`: a curator command for the two states a human can
    reverse (`"unmanaged"` -> adopt it; `"pinned"` -> unpin it), or an
    explanation for the three externally-owned ones
    (`"bundled"|"hub"|"external"`) where patching would just be overwritten
    later and there is no command to run at all. Anything else (``None``,
    or a future reason this function doesn't recognize yet) falls back to
    the same curator-adopt suggestion this wording used before
    `protected_reason` existed, so an older/degraded index that only knows
    `protected: True` keeps producing today's exact text."""
    if reason == "pinned":
        return f"hermes curator unpin {name}"
    if reason in ("bundled", "hub", "external"):
        return (
            "installed from outside the local library; an edit would be overwritten on "
            "update. Leave it unlinked; the reference's skills: header still links this side"
        )
    return f"hermes curator adopt {name}"


# --- SKILL.md / reference parsing -------------------------------------------

def _parse_skill(skill_md: Path, root: Path, external: bool) -> Optional[Dict[str, Any]]:
    """One skill's record, or ``None`` when it should be skipped entirely
    (unreadable file, missing/unparsable frontmatter, or no usable name --
    SL1.2's "bad YAML skips that one skill")."""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    frontmatter, _body = _read_frontmatter_and_body(text)
    if frontmatter is None:
        return None
    name = str(frontmatter.get("name") or skill_md.parent.name).strip()
    if not name:
        return None
    description = str(frontmatter.get("description") or "").strip()[:120]
    category = skill_md.parent.parent.name if skill_md.parent.parent != root else ""
    hermes_block, bb_block, has_bb_block = _metadata_blocks(frontmatter)
    tags = _as_str_list(hermes_block.get("tags")) or _as_str_list(frontmatter.get("tags"))
    related = _as_str_list(hermes_block.get("related_skills")) or _as_str_list(frontmatter.get("related_skills"))
    bb_products = _as_str_list(bb_block.get("products"))
    bb_origins = set(_as_str_list(bb_block.get("origins")))
    return {
        "name": name,
        "description": description,
        "category": category,
        "terms": _tokenize(name, " ".join(tags), " ".join(bb_products)),
        "linked": BROWSER_BRIDGE_SKILL_NAME in {r.lower() for r in related},
        # A skill that bothered to declare *any* metadata.browser_bridge
        # block (even an explicitly empty one) has opted into being
        # considered bridge-relevant, independent of whether it also shares
        # a term with a reference -- products_index() uses this to avoid
        # drowning in every skill in the library that merely happens to
        # share a common tag with something.
        "has_bb_metadata": has_bb_block,
        "origins": bb_origins,
        # Raw metadata.browser_bridge.products canonical names (not yet
        # tokenized) -- an orphan product group (products_index(), when this
        # skill has no overlapping reference) prefers these as its declared
        # "products" label over falling back to inferred terms.
        "products_declared": bb_products,
        "path": skill_md.parent,
        "external": external,
    }


def _parse_reference(ref_path: Path, skill_dir: Path) -> Dict[str, Any]:
    """One bridge reference's record. Header optional (§2): a valid YAML
    header supplies ``products``/``aliases``/``origins``/``skills``
    directly; otherwise terms are inferred from the filename stem and the
    first ``#`` heading. Never skipped -- an unreadable or headerless
    reference still contributes whatever can be inferred, per the "fail
    open, stay small" rule."""
    try:
        text = ref_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    frontmatter, body = _read_frontmatter_and_body(text)
    heading = _first_heading(body)
    rel_file = ref_path.relative_to(skill_dir).as_posix()

    if frontmatter is not None:
        products = _as_str_list(frontmatter.get("products"))
        aliases = _as_str_list(frontmatter.get("aliases"))
        origins = set(_as_str_list(frontmatter.get("origins")))
        ref_skills = set(_as_str_list(frontmatter.get("skills")))
        terms = _tokenize(" ".join(products), " ".join(aliases))
    else:
        products, origins, ref_skills = [], set(), set()
        stem_text = ref_path.stem.replace("-", " ").replace("_", " ")
        terms = _tokenize(stem_text, heading)

    return {
        "file": rel_file,
        "heading": heading,
        "terms": terms,
        "origins": origins,
        "skills": ref_skills,
        # Raw declared `products:` names (canonical, not tokenized) --
        # empty for a headerless reference. products_index()'s grouping
        # merges two DIFFERENT reference files into one product group only
        # when they share an entry here; inferred term overlap alone never
        # merges two reference files (see _build_reference_groups).
        "products_declared": products,
    }


def _final_stopwords(skill_records: List[Dict[str, Any]]) -> frozenset:
    """BASE_STOPWORDS plus any term appearing in more than
    `_COMMON_TERM_SKILL_THRESHOLD` skills' (pre-filter) term sets -- too
    common across the library to identify one product (§2)."""
    freq: Dict[str, int] = {}
    for rec in skill_records:
        for term in rec["terms"] - BASE_STOPWORDS:
            freq[term] = freq.get(term, 0) + 1
    dynamic = {term for term, count in freq.items() if count > _COMMON_TERM_SKILL_THRESHOLD}
    return frozenset(BASE_STOPWORDS | dynamic)


def _reference_load(file_path: str) -> str:
    return f"skill_view('{BROWSER_BRIDGE_SKILL_NAME}', file_path='{file_path}')"


def _build_reference_groups(references: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group bridge references into product groups for `products_index()`.

    Each reference starts as its own group -- so a single reference tagged
    with several inferred terms (an ESXi/vCenter reference mentioning both)
    produces ONE row, never one per term. Two DIFFERENT reference files
    merge into the same group only when they explicitly share an entry in
    their own `products:` header list; inferred term overlap alone never
    merges two reference files, so two unrelated headerless references never
    collide just because they happen to share a word.

    A skill named in any member reference's own `skills:` header (§2's
    reference->skill half of the link) attaches to the group unconditionally,
    even with zero term overlap -- see `named_skills` below -- but its OWN
    terms do NOT join the group's ``terms``. This is deliberately one-hop,
    the same as `match_page`: folding a named skill's terms into the group
    would transitively drag in every OTHER skill sharing that skill's tags
    (naming a heavily tagged product skill would otherwise pull in anything
    tagged `alerts`/`query`/... just because it happens to share a tag with
    the skill someone actually named).

    Returns one dict per group: ``{"terms": set, "declared_products": set,
    "refs": [reference dict, ...], "named_skills": set}`` (the member
    references themselves, not a copy) -- ``terms`` here comes ONLY from the
    references' own inferred/declared terms, never from an attached skill.
    Purely a function of the reference list, so it belongs in the cached
    index, not recomputed per `products_index()` call.
    """
    parent = list(range(len(references)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_declared_product: Dict[str, List[int]] = {}
    for i, ref in enumerate(references):
        for product in ref["products_declared"]:
            by_declared_product.setdefault(product.strip().lower(), []).append(i)
    for indices in by_declared_product.values():
        for other in indices[1:]:
            union(indices[0], other)

    components: Dict[int, List[int]] = {}
    for i in range(len(references)):
        components.setdefault(find(i), []).append(i)

    groups: List[Dict[str, Any]] = []
    for indices in components.values():
        member_refs = [references[i] for i in indices]
        terms: Set[str] = set()
        declared: Set[str] = set()
        named_skills: Set[str] = set()
        for ref in member_refs:
            terms |= ref["terms"]
            declared |= set(ref["products_declared"])
            named_skills |= ref["skills"]
        groups.append({"terms": terms, "declared_products": declared, "refs": member_refs, "named_skills": named_skills})
    return groups


def _empty_index() -> Dict[str, Any]:
    return {"skills": {}, "bridge_skill": None, "references": [], "reference_groups": [],
            "origin_index": {}, "stopwords": BASE_STOPWORDS}


def _build_index() -> Dict[str, Any]:
    home = _hermes_home()
    skills_cfg = _skills_config(home)
    roots = _skill_roots(home, skills_cfg)
    disabled = _disabled_skill_names(skills_cfg)
    provenance = _provenance_module()

    skills: Dict[str, Dict[str, Any]] = {}
    for root, external in roots:
        if not root.is_dir():
            continue
        for skill_md in sorted(root.rglob("SKILL.md")):
            if _is_dot_path(skill_md, root):
                continue
            rec = _parse_skill(skill_md, root, external)
            if rec is None or rec["name"] in disabled:
                continue
            # First occurrence wins: local root is scanned before external
            # dirs (roots[0] is always local), matching Hermes' own
            # local-before-external precedence for a same-named skill.
            skills.setdefault(rec["name"], rec)

    bridge_skill = skills.get(BROWSER_BRIDGE_SKILL_NAME)
    references: List[Dict[str, Any]] = []
    if bridge_skill is not None and not bridge_skill["external"]:
        ref_dir = bridge_skill["path"] / "references"
        if ref_dir.is_dir():
            for ref_path in sorted(ref_dir.glob("*.md")):
                if ref_path.is_file():
                    references.append(_parse_reference(ref_path, bridge_skill["path"]))

    # The umbrella skill itself is never a "product skill" -- SL2/SL3 index
    # product skills only.
    product_skills = {name: rec for name, rec in skills.items() if name != BROWSER_BRIDGE_SKILL_NAME}

    stopwords = _final_stopwords(list(product_skills.values()))
    for rec in product_skills.values():
        rec["terms"] = rec["terms"] - stopwords
        rec["protected"], rec["protected_reason"] = _compute_protected(provenance, rec["name"], rec["external"])
    for ref in references:
        ref["terms"] = ref["terms"] - stopwords

    # Exact-origin index: a reference's or skill's own declared `origins`
    # list is the strongest match signal there is (a human wrote it down),
    # stronger than any term overlap -- see match_page's "origin" path.
    origin_index: Dict[str, Set[str]] = {}
    for rec in product_skills.values():
        for origin in rec["origins"]:
            origin_index.setdefault(origin, set()).update(rec["terms"])
    for ref in references:
        for origin in ref["origins"]:
            origin_index.setdefault(origin, set()).update(ref["terms"])

    return {
        "skills": product_skills,
        "bridge_skill": bridge_skill,
        "references": references,
        "reference_groups": _build_reference_groups(references),
        "origin_index": origin_index,
        "stopwords": stopwords,
    }


# --- cache (SL1.5) -----------------------------------------------------------

_cache_lock = threading.Lock()
_cached_index: Optional[Dict[str, Any]] = None
_cached_signature: Optional[Tuple[Any, ...]] = None
_last_check_monotonic: float = 0.0


def _scan_signature() -> Tuple[Any, ...]:
    """A cheap (stat-only, no parsing) fingerprint of everything a rebuild
    would read: every scanned SKILL.md's (path, mtime, size), the bridge
    reference files, and the disabled-names list -- so toggling
    `skills.disabled` without touching a file still triggers a rebuild."""
    home = _hermes_home()
    skills_cfg = _skills_config(home)
    roots = _skill_roots(home, skills_cfg)
    disabled = _disabled_skill_names(skills_cfg)
    entries: List[Tuple[str, int, int]] = []
    for root, _external in roots:
        if not root.is_dir():
            continue
        for skill_md in root.rglob("SKILL.md"):
            if _is_dot_path(skill_md, root):
                continue
            try:
                st = skill_md.stat()
            except OSError:
                continue
            entries.append((str(skill_md), st.st_mtime_ns, st.st_size))
    bridge_refs = home / "skills" / BROWSER_BRIDGE_SKILL_NAME / "references"
    if bridge_refs.is_dir():
        for ref_path in bridge_refs.glob("*.md"):
            try:
                st = ref_path.stat()
            except OSError:
                continue
            entries.append((str(ref_path), st.st_mtime_ns, st.st_size))
    return (tuple(sorted(entries)), tuple(sorted(disabled)))


def _get_index() -> Dict[str, Any]:
    global _cached_index, _cached_signature, _last_check_monotonic
    now = time.monotonic()
    with _cache_lock:
        if _cached_index is not None and (now - _last_check_monotonic) < _CACHE_CHECK_INTERVAL_SECONDS:
            return _cached_index
        _last_check_monotonic = now
        try:
            signature = _scan_signature()
        except Exception:
            signature = _cached_signature  # an unreadable scan reads as "unchanged", not as "empty"
        if _cached_index is None or signature != _cached_signature:
            try:
                _cached_index = _build_index()
                _cached_signature = signature
            except Exception:
                if _cached_index is None:
                    _cached_index = _empty_index()
        return _cached_index


def invalidate() -> None:
    """Force the next call to rebuild immediately, bypassing the throttle --
    the `on_skill_lifecycle` hook (skill_hooks.py) calls this after any
    curator write so a just-linked skill or a just-saved reference is picked
    up right away instead of waiting out the cache window."""
    global _cached_index, _cached_signature, _last_check_monotonic
    with _cache_lock:
        _cached_index = None
        _cached_signature = None
        _last_check_monotonic = 0.0


# --- shared match helpers ----------------------------------------------------

def _all_known_terms(index: Dict[str, Any]) -> Set[str]:
    terms: Set[str] = set()
    for rec in index["skills"].values():
        terms |= rec["terms"]
    for ref in index["references"]:
        terms |= ref["terms"]
    return terms


def _origin_host(origin: str) -> str:
    """The bare hostname out of an `https://host[:port]` origin string --
    tokenized separately from the scheme/port, which never carry product
    signal."""
    host = origin.split("://", 1)[-1]
    host = host.split("/", 1)[0]
    host = host.split(":", 1)[0]
    return host


def _find_reference(index: Dict[str, Any], file_path: str) -> Optional[Dict[str, Any]]:
    for ref in index["references"]:
        if ref["file"] == file_path:
            return ref
    return None


def _skills_for_terms(index: Dict[str, Any], terms: Set[str], named_by_refs: Set[str] = frozenset()) -> List[Dict[str, Any]]:
    """Product skills matching ``terms`` by term overlap, PLUS any skill in
    ``named_by_refs`` (a matched reference's own `skills:` header naming it
    directly -- §2's reference->skill half of the link) even with zero term
    overlap. ``ref_names_skill`` on each entry says which reason applied."""
    if not terms and not named_by_refs:
        return []
    out = [
        {"name": rec["name"], "description": rec["description"], "category": rec["category"],
         "linked": rec["linked"], "protected": rec["protected"], "protected_reason": rec.get("protected_reason"),
         "ref_names_skill": rec["name"] in named_by_refs}
        for rec in index["skills"].values()
        if (terms and rec["terms"] & terms) or rec["name"] in named_by_refs
    ]
    out.sort(key=lambda d: d["name"])
    return out


def _matched_references(index: Dict[str, Any], terms: Set[str]) -> List[Dict[str, Any]]:
    """Raw reference records (not the display shape) whose terms overlap ``terms``."""
    if not terms:
        return []
    return [ref for ref in index["references"] if ref["terms"] & terms]


def _references_for_terms(index: Dict[str, Any], terms: Set[str]) -> List[Dict[str, Any]]:
    out = [
        {"file": ref["file"], "heading": ref["heading"], "load": _reference_load(ref["file"])}
        for ref in _matched_references(index, terms)
    ]
    out.sort(key=lambda d: d["file"])
    return out


# --- public API (skilllinks.md SL1.7 -- exact names/shapes; SL2/SL3/SL5 build
# on these) --------------------------------------------------------------

def match_page(origin: str, title: str) -> Dict[str, Any]:
    """What Hermes knows about the product behind ``origin`` (an attached
    tab's origin) and, only as a last resort, ``title`` (page-authored, so
    untrusted -- see module docstring and §1.3).

    Returns::

        {"origin": str, "product_terms": [str, ...],
         "matched_by": "origin" | "used" | "hostname" | "page_title" | None,
         "product_skills": [{"name", "description", "category", "linked", "protected", "ref_names_skill"}, ...],
         "bridge_references": [{"file", "heading", "load"}, ...]}

    Match precedence, trusted signals first: an exact match against a
    declared ``origins:`` list ("origin"); else a prior recorded (origin,
    skill) observation from actual use, source ``"used"`` only ("used") --
    also an exact origin match, just learned rather than authored; else the
    origin's own hostname tokens overlapping a known product term
    ("hostname") -- not page-controlled, so still trusted, but a weaker
    signal than an exact origin. Only when none of those fire are the page's
    own ``title`` tokens tried ("page_title") -- the page controls its
    title, so this is a hint, never a trusted binding on its own. A caller
    MAY still record an observation corroborated only by title (see
    ``record_used``'s ``source="used_title"``), but such a row is invisible
    to the "used" step above and can never promote itself into a trusted
    match by being seen again.

    ``product_skills`` includes a skill matched purely by term overlap AND a
    skill named in a matched bridge reference's own `skills:` header even
    with zero term overlap (§2's reference->skill half of the link) --
    ``ref_names_skill`` on each entry says whether that second reason
    applied. ``bridge_references`` itself stays term-overlap only.
    """
    try:
        return _match_page(origin or "", title or "")
    except Exception:
        return {"origin": origin or "", "product_terms": [], "matched_by": None,
                "product_skills": [], "bridge_references": []}


def _match_page(origin: str, title: str) -> Dict[str, Any]:
    index = _get_index()
    terms: Set[str] = set()
    matched_by: Optional[str] = None

    if origin in index["origin_index"]:
        terms = set(index["origin_index"][origin])
        matched_by = "origin"

    if not terms:
        used_terms: Set[str] = set()
        for obs in state.list_skill_link_observations(origin=origin):
            # A "used_title" row was only ever corroborated by the page's own
            # (untrusted) title -- see record_used's docstring. Counting it
            # here would let a title-only hint promote itself into a trusted
            # match just by being seen again; it stays visible in
            # observed_origins/products_index for a human to curate, but
            # never elevates matched_by past "page_title" on its own.
            if obs["source"] != "used":
                continue
            if obs["skill"] == BROWSER_BRIDGE_SKILL_NAME and obs["file_path"]:
                ref = _find_reference(index, obs["file_path"])
                if ref:
                    used_terms |= ref["terms"]
            else:
                rec = index["skills"].get(obs["skill"])
                if rec:
                    used_terms |= rec["terms"]
        if used_terms:
            terms, matched_by = used_terms, "used"

    if not terms:
        overlap = _tokenize(_origin_host(origin)) & _all_known_terms(index)
        if overlap:
            terms, matched_by = overlap, "hostname"

    if not terms:
        overlap = _tokenize(title) & _all_known_terms(index)
        if overlap:
            terms, matched_by = overlap, "page_title"

    named_by_refs: Set[str] = set()
    for ref in _matched_references(index, terms):
        named_by_refs |= ref["skills"]

    return {
        "origin": origin,
        "product_terms": sorted(terms),
        "matched_by": matched_by,
        "product_skills": _skills_for_terms(index, terms, named_by_refs),
        "bridge_references": _references_for_terms(index, terms),
    }


def match_skill(name: str) -> Optional[Dict[str, Any]]:
    """What Hermes knows about product skill ``name``'s bridge overlap.

    Returns ``None`` when ``name`` isn't an indexed product skill, or is one
    but shares no terms with any bridge reference, has no observed origins,
    and isn't itself linked -- i.e. nothing bridge-related to say. Otherwise::

        {"name": str, "product_terms": [str, ...],
         "bridge_references": [{"file", "heading", "load"}, ...],
         "observed_origins": [str, ...], "linked": bool, "ref_names_skill": bool,
         "protected": bool | None}

    A skill with observed origins but no matching reference (someone drove
    the product through the bridge, but nobody has written that up yet)
    still returns a dict -- ``bridge_references`` is simply ``[]`` in that
    case. It's only ``None`` when there is neither a reference nor an
    observed origin nor an explicit `related_skills: [browser-bridge]` link.

    ``linked`` and ``ref_names_skill`` report the two halves of the link
    separately (§2): ``linked`` is this skill's own `related_skills`
    including `browser-bridge`; ``ref_names_skill`` is whether ANY reference
    in the library names this skill in its own `skills:` header, regardless
    of term overlap -- the reference->skill half Hermes actually writes.
    """
    try:
        return _match_skill(name or "")
    except Exception:
        return None


def _match_skill(name: str) -> Optional[Dict[str, Any]]:
    index = _get_index()
    rec = index["skills"].get(name)
    if rec is None:
        return None

    bridge_references = _references_for_terms(index, rec["terms"])
    seen_files = {b["file"] for b in bridge_references}
    ref_names_skill = False
    for ref in index["references"]:
        # A reference can name this skill in its own `skills:` header with
        # no shared terms at all -- an explicit human-declared link that
        # term overlap alone would miss.
        if name in ref["skills"]:
            ref_names_skill = True
            if ref["file"] not in seen_files:
                bridge_references.append({"file": ref["file"], "heading": ref["heading"], "load": _reference_load(ref["file"])})
                seen_files.add(ref["file"])

    observed_origins = sorted({obs["origin"] for obs in state.list_skill_link_observations(skill=name)})

    if not bridge_references and not observed_origins and not rec["linked"]:
        return None

    return {
        "name": rec["name"],
        "product_terms": sorted(rec["terms"]),
        "bridge_references": bridge_references,
        "observed_origins": observed_origins,
        "linked": rec["linked"],
        "ref_names_skill": ref_names_skill,
        "protected": rec["protected"],
        "protected_reason": rec.get("protected_reason"),
    }


def products_index() -> List[Dict[str, Any]]:
    """One row per *product group*, not per term -- a single bridge
    reference defines one group even when it carries several inferred terms
    (an ESXi/vCenter reference doesn't split into an "esxi" row and a
    "vcenter" row), and grouping stays bridge-relevant rather than
    surfacing every ordinary tag shared by two skills somewhere in a large
    library (roughly 450 single-word "products" on a real library before
    this filtering, one per tag)::

        {"products": [str, ...], "terms": [str, ...],
         "bridge_references": [{"file", "heading"}, ...],
         "product_skills": [{"name", "linked", "protected", "ref_names_skill"}, ...],
         "origins": [str, ...], "missing_links": [str, ...]}

    Grouping rules:

    - Every bridge reference seeds a group. Two DIFFERENT reference files
      merge into one group only when they explicitly share an entry in
      their own `products:` header list -- inferred term overlap alone
      never merges two reference files (see `_build_reference_groups`).
    - A product skill attaches to every group whose terms it overlaps
      (a skill can attach to more than one group), AND to a group that
      names it in a member reference's own `skills:` header, even with
      zero term overlap -- §2's reference->skill half of the link, the one
      Hermes actually writes.
    - A product skill claimed by no reference group but otherwise
      "qualifying" -- `linked`, carrying a `metadata.browser_bridge` block,
      or with an observed origin -- gets its own group (other skills can
      still attach to it by the same term-overlap rule). A skill with none
      of those and no reference overlap appears nowhere here, same as
      before.

    ``products`` is the group's declared canonical names (unioned across its
    member references'/skill's `products:`/`metadata.browser_bridge.products`
    entries) when any exist, else its inferred ``terms`` -- "declared names,
    or inferred terms". ``origins`` unions declared origins with observed
    ones across the group's references and attached skills.

    Each attached skill reports the link's two halves separately:
    ``linked`` (its own `related_skills` includes `browser-bridge`) and
    ``ref_names_skill`` (some reference in THIS group lists it in `skills:`).
    ``missing_links`` names one message per missing half -- a reference not
    naming an attached skill, and/or an attached skill not linking back --
    so a plain term-overlap attachment with neither half declared reports
    both gaps. A group with a reference and no product skill attached at all
    keeps the blanket "<file> has no linked product skill"; a skill-only
    group instead says no bridge reference exists for it yet. These are the
    gaps SL3's `suggested_patch` and SL5's CLI both act on. Rows with at
    least one missing link sort first (so the CLI and the ≤8-row reviewer
    summary lead with what needs attention), each group then alphabetical by
    ``products``.
    """
    try:
        return _products_index()
    except Exception:
        return []


def _products_index() -> List[Dict[str, Any]]:
    index = _get_index()

    obs_by_skill: Dict[str, Set[str]] = {}
    obs_by_skill_file: Dict[Tuple[str, str], Set[str]] = {}
    for obs in state.list_skill_link_observations():
        obs_by_skill.setdefault(obs["skill"], set()).add(obs["origin"])
        obs_by_skill_file.setdefault((obs["skill"], obs["file_path"]), set()).add(obs["origin"])

    all_candidates = _promotion_candidates(None)
    all_skills = list(index["skills"].values())
    rows: List[Dict[str, Any]] = []
    claimed_skill_names: Set[str] = set()

    for group in index["reference_groups"]:
        terms = group["terms"]
        named_skills = group["named_skills"]
        # A skill named in this group's own `skills:` header attaches
        # unconditionally, even with zero term overlap (§2's reference->skill
        # half of the link, and the one Hermes actually writes) -- not just
        # relying on the terms-join in _build_reference_groups, which
        # wouldn't attach a named skill whose own terms happen to be empty.
        attached = [s for s in all_skills if (terms and s["terms"] & terms) or s["name"] in named_skills]
        claimed_skill_names |= {s["name"] for s in attached}

        origins: Set[str] = set()
        for ref in group["refs"]:
            origins |= ref["origins"]
            origins |= obs_by_skill_file.get((BROWSER_BRIDGE_SKILL_NAME, ref["file"]), set())
        for skill in attached:
            origins |= skill["origins"]
            origins |= obs_by_skill.get(skill["name"], set())

        # Report each half of the reference<->skill link separately (the
        # coordinator's fix): a group with no attached skill at all keeps the
        # blanket "no linked product skill" message; once at least one skill
        # is attached, each attached skill gets its own message per missing
        # half -- the reference not naming it, and/or the skill not linking
        # back -- so a plain term-overlap attachment with NEITHER half
        # declared reports both gaps, not a single vague one.
        missing_links: List[str] = []
        if not attached:
            for ref in group["refs"]:
                missing_links.append(f"{ref['file']} has no linked product skill")
        else:
            first_ref = sorted(group["refs"], key=lambda r: r["file"])[0]["file"] if group["refs"] else ""
            for skill in attached:
                if skill["name"] not in named_skills:
                    missing_links.append(f"{first_ref} does not list {skill['name']} in its skills: header")
                if not skill["linked"]:
                    missing_links.append(f"{skill['name']} lacks related_skills: [browser-bridge]")

        promote = _candidates_for_group(all_candidates, {r["file"] for r in group["refs"]},
                                         {s["name"] for s in attached})
        rows.append(_products_index_row(group["declared_products"], terms, group["refs"], attached, origins,
                                         missing_links, named_skills, promote))

    # Skill-only groups: a qualifying skill (linked, has its own
    # metadata.browser_bridge block, or has an observed origin) that no
    # reference group already claimed. Processed in a stable order so a
    # skill attached to an EARLIER skill-only group (by shared terms) isn't
    # given a redundant duplicate group of its own.
    for skill in sorted(all_skills, key=lambda s: s["name"]):
        if skill["name"] in claimed_skill_names:
            continue
        qualifies = skill["linked"] or skill["has_bb_metadata"] or bool(obs_by_skill.get(skill["name"]))
        if not qualifies:
            continue

        terms = skill["terms"]
        attached = [s for s in all_skills if s["terms"] & terms] if terms else [skill]
        claimed_skill_names |= {s["name"] for s in attached}

        origins = set()
        for s in attached:
            origins |= s["origins"]
            origins |= obs_by_skill.get(s["name"], set())

        missing_links = []
        if attached:
            names = ", ".join(sorted(s["name"] for s in attached))
            missing_links.append(f"no bridge reference exists yet for this product (covered by: {names})")

        promote = _candidates_for_group(all_candidates, set(), {s["name"] for s in attached})
        rows.append(_products_index_row(set(skill["products_declared"]), terms, [], attached, origins,
                                         missing_links, set(), promote))

    rows.sort(key=lambda row: (0 if row["missing_links"] else 1, ",".join(row["products"]) or ",".join(row["terms"])))
    return rows


def _products_index_row(
    declared_products: Set[str], terms: Set[str], refs: List[Dict[str, Any]],
    attached_skills: List[Dict[str, Any]], origins: Set[str], missing_links: List[str], named_skills: Set[str],
    promote: "List[Dict[str, Any]] | tuple" = (),
) -> Dict[str, Any]:
    return {
        "products": sorted(declared_products) if declared_products else sorted(terms),
        "terms": sorted(terms),
        "bridge_references": [{"file": r["file"], "heading": r["heading"]} for r in sorted(refs, key=lambda r: r["file"])],
        "product_skills": sorted(
            [{"name": s["name"], "linked": s["linked"], "protected": s["protected"],
              "protected_reason": s.get("protected_reason"),
              "ref_names_skill": s["name"] in named_skills} for s in attached_skills],
            key=lambda d: d["name"],
        ),
        "origins": sorted(origins),
        "missing_links": missing_links,
        "promote": list(promote),
    }


def _candidates_for_group(
    all_candidates: List[Dict[str, Any]], ref_files: Set[str], skill_names: Set[str]
) -> List[Dict[str, Any]]:
    """The promotion candidates (rev2) relevant to one `products_index()`
    row: a reference-observation candidate whose `file_path` is one of this
    group's own reference files, or a product-skill candidate (`file_path`
    empty) whose `skill` is one of this group's attached skills. Sorted by
    most-used first and capped at `_PROMOTE_ROW_CAP` -- the umbrella view
    (skill_hooks.py's `_trim_row`) trims every list to 3 anyway, but the CLI
    reads this list directly and shouldn't see an unbounded one."""
    matched = [
        c for c in all_candidates
        if (c["file_path"] and c["file_path"] in ref_files) or (not c["file_path"] and c["skill"] in skill_names)
    ]
    matched.sort(key=lambda c: (-c["uses"], c["skill"], c["file_path"]))
    return matched[:_PROMOTE_ROW_CAP]


def record_used(origin: str, skill: str, file_path: str = "", source: str = "used") -> None:
    """Record that ``origin`` and ``skill`` (a product skill name, or
    ``BROWSER_BRIDGE_SKILL_NAME`` with ``file_path`` set to a reference's
    relative path) were used together in this task.

    ``source`` grades how much this row should be trusted later, and the
    caller (skill_hooks.py) decides it by corroborating the binding against
    an INDEPENDENT ``match_page`` call before ever recording anything --
    this function itself does no corroboration and trusts what it's told:

    - ``"used"`` (default): the corroborating signal was NOT page-authored
      (an exact declared ``origins:`` match, the origin's own hostname
      tokens, or a prior ``"used"`` row) -- ``match_page``'s "used"
      precedence step (§1.3) treats this origin as a trusted match for this
      skill/reference from now on, same as a human-authored header.
    - ``"used_title"``: the ONLY corroborating signal was the page's own
      ``<title>`` -- page-authored and therefore still just a hint, per §1.3.
      ``match_page``'s "used" step ignores these rows entirely, so a
      title-only observation can never promote itself into a trusted match
      by repetition; it still surfaces in ``match_skill``'s
      ``observed_origins`` / ``products_index``'s ``origins`` so a human can
      turn it into a real ``origins:`` header.

    No-ops on a missing origin/skill and swallows any storage error -- this
    is telemetry, never load-bearing for the tool call that triggered it.
    """
    if not origin or not skill:
        return
    try:
        state.record_skill_link_observation(origin, skill, file_path or "", source)
    except Exception:
        pass


# --- promotion to the origin tier (rev2) -------------------------------------

def promotion_candidates(origin: Optional[str] = None) -> List[Dict[str, Any]]:
    """Observed (origin, skill[, file_path]) bindings (SL1.6) seen at least
    `PROMOTE_AFTER_USES` times whose target does not already declare that
    exact origin in its own header -- the shared "this has been used enough
    times, write it down" signal SL2's block, SL3's suggestions, and SL5's
    CLI all read from. A reference declares an origin via its header's
    `origins:`; a product skill via `metadata.browser_bridge.origins`.

    ``origin`` narrows to one tab's origin (what SL2 checks per block);
    omitted, every qualifying pair in the whole library comes back (SL3's
    umbrella/product-skill views, SL5's CLI, and this module's own
    `products_index()`). Each row is
    ``{"origin", "skill", "file_path", "uses", "grade"}`` -- ``grade`` is the
    observation's own `source` (`"used"` or `"used_title"`), unchanged from
    how ``record_used`` graded it, so a caller can still tell a
    page-title-corroborated binding apart from a fully trusted one. Sorted
    for determinism, not by any notion of priority -- callers pick their own
    "best" one when they only want to show a single sentence.

    Fails open to ``[]``, same as every other public function here.
    """
    try:
        return _promotion_candidates(origin)
    except Exception:
        return []


def _promotion_candidates(origin: Optional[str]) -> List[Dict[str, Any]]:
    index = _get_index()
    observations = state.list_skill_link_observations(origin=origin or "")
    candidates: List[Dict[str, Any]] = []
    for obs in observations:
        if obs["count"] < PROMOTE_AFTER_USES:
            continue
        obs_origin = obs["origin"]
        if obs["skill"] == BROWSER_BRIDGE_SKILL_NAME and obs["file_path"]:
            ref = _find_reference(index, obs["file_path"])
            if ref is None or obs_origin in ref["origins"]:
                continue  # unknown reference, or the header already names this origin
        else:
            rec = index["skills"].get(obs["skill"])
            if rec is None or obs_origin in rec["origins"]:
                continue
        candidates.append({
            "origin": obs_origin, "skill": obs["skill"], "file_path": obs["file_path"],
            "uses": obs["count"], "grade": obs["source"],
        })
    candidates.sort(key=lambda c: (c["origin"], c["skill"], c["file_path"]))
    return candidates


def promotion_sentence(candidate: Dict[str, Any]) -> str:
    """The one-sentence nudge for a single `promotion_candidates()` row:
    write the observed origin into the target's own header so a future visit
    matches by `origin` (the strongest signal) instead of whatever weaker one
    got it here. Never echoes the page's title -- only the origin, the
    file/skill name, and the use count, none of which the page authored."""
    origin = candidate.get("origin", "")
    uses = candidate.get("uses", 0)
    skill = candidate.get("skill", "")
    file_path = candidate.get("file_path", "")
    if skill == BROWSER_BRIDGE_SKILL_NAME and file_path:
        sentence = (
            f'This site has used {file_path} {uses} times. Add origins: ["{origin}"] '
            "to that reference's header so it matches directly."
        )
    else:
        sentence = (
            f'This site has used {skill} {uses} times. Add origins: ["{origin}"] '
            "to that skill's metadata.browser_bridge.origins so it matches directly."
        )
    if candidate.get("grade") == "used_title":
        sentence += " It was matched by the page's title, so confirm the site first."
    return sentence


# --- misplaced-manual guard (rev2; detect only, never fix) -------------------

def _plugin_manual_path() -> Path:
    return Path(__file__).resolve().parent / "skill" / "SKILL.md"


def bundled_manual_misplacement(home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Detect -- never fix -- the bundled manual having been deployed on top
    of Hermes's own LEARNED skill at ``<skills_root>/browser-bridge/SKILL.md``
    (a real incident: a deploy script copied the plugin's manual there,
    silently discarding whatever the background reviewer had saved --
    references, product-skill links, all of it). A local copy counts as the
    bundled manual when its sha256 matches the plugin's own ``skill/SKILL.md``
    byte-for-byte, or -- a hand-edited or older copy that won't hash-match --
    when its body starts with the manual's own H1 (`BUNDLED_MANUAL_H1`).

    Read-only: returns a description of what was found
    (``{"path", "matched_by", "local_size", "bundled_size"}``), or ``None``
    when nothing looks wrong. Never moves, restores, or writes a single byte
    -- the fix is a human restoring the learned skill from a backup, not this
    module attempting one (§1's "the plugin reads and suggests" rule applies
    here too). Fails open to ``None`` on any error, same as this module's
    other public functions.
    """
    try:
        return _bundled_manual_misplacement(home)
    except Exception:
        return None


def _bundled_manual_misplacement(home: Optional[Path]) -> Optional[Dict[str, Any]]:
    plugin_manual = _plugin_manual_path()
    if not plugin_manual.is_file():
        return None
    local_path = (home or _hermes_home()) / "skills" / BROWSER_BRIDGE_SKILL_NAME / "SKILL.md"
    if not local_path.is_file():
        return None
    try:
        if local_path.samefile(plugin_manual):
            return None  # a dev setup symlinking the two together -- not a misplacement
    except OSError:
        pass

    plugin_bytes = plugin_manual.read_bytes()
    local_bytes = local_path.read_bytes()
    matched_by: Optional[str] = None
    if hashlib.sha256(local_bytes).hexdigest() == hashlib.sha256(plugin_bytes).hexdigest():
        matched_by = "hash"
    else:
        _fm, body = _read_frontmatter_and_body(local_bytes.decode("utf-8", errors="replace"))
        if body.lstrip().startswith(BUNDLED_MANUAL_H1):
            matched_by = "heading"
    if matched_by is None:
        return None
    return {
        "path": str(local_path),
        "matched_by": matched_by,
        "local_size": len(local_bytes),
        "bundled_size": len(plugin_bytes),
    }
