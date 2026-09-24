"""Attach registry + single-driver lease (plan.md §3.3).

One Chrome tab can be "driven" by at most one Hermes session at a time — the
lease modelled here. A second session attaching a tab someone else is already
driving gets a clear "in use by session X" refusal (``AttachConflict``)
instead of silently stealing it (plan.md §3.3: "like terminal session
locks"). Leases default to 60s and renew simply by attaching again from the
same holder; nothing here auto-renews on its own, so a session that stops
calling tools naturally frees its tab for someone else after the TTL.

The lease duration is per-device configurable (the user's ask: extension Options
→ "Tab lease", 10-1200 seconds) via ``hermes_plugin/state.py``'s
``effective_lease_seconds`` / ``device_lease_seconds`` table, falling back to
``config.py``'s fleet-wide ``lease_seconds`` default (also 60) for a device
that has never reported its own. A device may also report **unlimited**
(wire value 0): ``ttl_seconds_for()`` below turns that into ``float('inf')``
for this module's own bookkeeping — every ``expires_at`` comparison in this
file (``>``, ``<=``) is a plain float comparison and handles ``inf``
correctly with no special-casing needed, EXCEPT ``snapshot()``'s arithmetic
(``expires_at - now``), which cannot be converted to an ``int`` — see that
method. ``float('inf')`` must never reach a tool result or any other JSON
payload directly (``json.dumps`` would render it as the non-conformant
literal ``Infinity``); ``lease_result_fields()`` below is the one place a
caller converts an effective-lease value into wire-safe fields.

This registry is deliberately in-memory, not persisted to ``state.db``: it
only needs to survive for the lifetime of the gateway process each Hermes
session runs in, and the extension is the authority on what is *actually*
attached in Chrome (plan.md, this workstream's brief) — surviving a gateway
restart would mean trusting stale lease data over a fresh ``tabs.list``
reconciliation anyway. ``reconcile()`` is how a caller (tools.py, before every
snapshot/read/attach) drops bookkeeping for tabs the extension no longer
reports as attached, which matters after a reconnect where the offscreen
document's own state may have moved on without us.

Threading: tool handlers run on plain gateway threads (not the relay's
asyncio loop), so this is a plain ``threading.Lock``, not asyncio-flavoured.
Mutating calls (attach/release/reconcile) take the lock; the plan's "reads are
lock-free" refers to the *browser* read tools (snapshot/read) never needing to
hold or contend for the driving lease at all — they only check that a tab is
attached, not who is driving it (§6.2's grant check is the thing that gates
them). ``holder_of``/``snapshot`` here still take the lock because it is cheap
and CPython dict reads aren't safe to leave fully unguarded across a mutator.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

LEASE_TTL_SECONDS = 60


def ttl_seconds_for(effective_lease_seconds: int) -> float:
    """Convert a device's effective lease setting (state.py's
    ``effective_lease_seconds`` -- an int 10..1200, or 0 meaning unlimited)
    into the ``ttl`` ``AttachRegistry.attach()`` expects: ``float('inf')``
    for unlimited, else the value itself as a float. Centralised here, not
    re-derived at each of tools.py's/navigation.py's/upload.py's call sites,
    so "0 means infinite" has exactly one place to get right.
    """
    return float("inf") if effective_lease_seconds <= 0 else float(effective_lease_seconds)


def lease_result_fields(effective_lease_seconds: int) -> Dict[str, Any]:
    """The ``lease_seconds``/``lease`` fields a tool result reports for a
    device's effective lease. The user's ask: unlimited reports
    ``lease_seconds: null`` plus ``lease: "unlimited"`` -- never a raw
    ``float('inf')``, which ``json.dumps`` would otherwise render as the
    non-JSON-conformant literal ``Infinity``. A finite lease reports the
    plain integer, exactly as every tool result did before this feature
    existed, with no ``lease`` key at all -- so an older caller/test reading
    only ``lease_seconds`` sees no difference for the (still default)
    60-second case.
    """
    if effective_lease_seconds <= 0:
        return {"lease_seconds": None, "lease": "unlimited"}
    return {"lease_seconds": effective_lease_seconds}


class AttachConflict(Exception):
    """A tab's driving lease is held by someone else."""

    def __init__(self, tab_id: int, holder: str):
        super().__init__(f"tab {tab_id} is driven by session {holder}")
        self.tab_id = tab_id
        self.holder = holder


class _Lease:
    __slots__ = ("tab_id", "device_id", "holder", "expires_at", "tab_ref")

    def __init__(self, tab_id: int, device_id: str, holder: str, expires_at: float, tab_ref: Optional[dict]):
        self.tab_id = tab_id
        self.device_id = device_id
        self.holder = holder
        self.expires_at = expires_at
        self.tab_ref = tab_ref or {}


class AttachRegistry:
    """Process-local bookkeeping of attached tabs and their driving leases."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: Dict[int, _Lease] = {}
        self._device_tabs: Dict[str, Set[int]] = {}
        # idx -> CSS selector, per (device_id, tab_id), from the most recent
        # dom.snapshot OR page.act of that tab. Keyed on the tab (not the
        # lease/holder, since reads — including snapshot itself — are
        # lock-free per plan §3.3 and don't require holding the driving
        # lease) and always REPLACED wholesale on a new snapshot, never
        # merged: a stale idx=3 pointing at a selector from the page the user
        # was on two navigations ago must never resolve for M2's act(idx) is
        # the one failure mode this whole feature exists to prevent.
        #
        # M2 defect fix (integration-verifier finding, post-M2): act()
        # itself re-walks the DOM to build its before/after diff, so a
        # successful page.act ALSO carries a fresh indexMap
        # (protocol/schema.json's page.act.result.indexMap) — tools.py's
        # handle_act now adopts it here via set_index_map() exactly like
        # handle_snapshot does, instead of leaving the pre-action map in
        # place to describe a page that action may have just re-rendered.
        # When the extension's after-walk fails and no indexMap comes back
        # at all, tools.py calls invalidate_index_map() below — an absent
        # map must mean "re-snapshot before the next idx-based act", never
        # "keep trusting the map from before this action ran".
        self._index_maps: Dict[Tuple[str, int], Dict[int, str]] = {}
        # {"url": ..., "set_at": ..., "viewport": ...} alongside each tab's
        # index map — the url the tab was on when that map was captured
        # (from dom.snapshot's own `url`, or page.act's post-action `url`).
        # This is a best-effort, URL-level staleness check only: it catches a
        # navigation between snapshot and act, but NOT a same-page re-render
        # (a table row deleted, shifting its siblings, with the URL
        # untouched) — that class of bug is what `_element_meta` below
        # closes. `viewport` ({width, height, dpr, scrollX, scrollY}, or
        # None) closes the THIRD dimension neither of those catches: a resize
        # or scroll changes neither the URL nor any element's role/name, so
        # both existing guards pass while every cached `xy` coordinate is
        # wrong (G2.5) — see set_index_map()'s docstring.
        self._index_map_meta: Dict[Tuple[str, int], Dict[str, Any]] = {}
        # idx -> {"role": ..., "name": ...}, per (device_id, tab_id) — the
        # ELEMENT_MISMATCH fix (protocol/schema.json's `indexMeta`, populated
        # by content/walker.ts's `describeElement` and carried on both
        # dom.snapshot and page.act's wire results). Stored and replaced
        # wholesale in lockstep with `_index_maps` (see set_index_map): a
        # role+name pair only means anything paired with the selector it
        # describes, from the SAME snapshot/act generation, so this can never
        # be allowed to survive one _index_maps replacement while the other
        # doesn't. handle_act reads this via resolve_index_meta() to populate
        # page.act's `expect` param, so the extension can compare it against
        # the LIVE element right before acting and refuse with
        # ELEMENT_MISMATCH instead of silently hitting whatever now sits at
        # that selector — the same-page-mutation case `_index_map_meta`'s URL
        # check above cannot catch.
        self._element_meta: Dict[Tuple[str, int], Dict[int, Dict[str, str]]] = {}
        # idx -> CDP backendNodeId (G2.6.2), per (device_id, tab_id) — the
        # node-handle fix. A backendNodeId is CDP's own stable identifier for
        # a specific DOM node, resolved by the extension's background worker
        # (which alone holds the chrome.debugger session) from the SAME
        # indexMap/wire result as `_index_maps`/`_element_meta` above, and
        # stored in lockstep with them for the identical reason: a
        # backendNodeId only means anything paired with the selector/role/name
        # it was captured alongside in the SAME snapshot/act generation.
        # Absent entries are normal, not an error — see set_index_map's
        # docstring for the two reasons an idx can lack one (a shadow-piercing
        # selector DOM.querySelector cannot evaluate, or a resolution
        # failure); handle_act simply omits `backendNodeId` for that idx and
        # act.ts falls back to selector re-resolution exactly as it always
        # has, per the module's back-compat story.
        self._node_maps: Dict[Tuple[str, int], Dict[int, int]] = {}
        # G2.2.13 (acting inside frames): frame-hop selector PREFIX -> its
        # own canonical origin, per (device_id, tab_id) — the same
        # ``frame_origins`` the extension already reports on every
        # ``dom.snapshot`` (``background/frames.ts``'s ``frameOrigins``,
        # reconciled gateway-side by ``tools.py``'s ``_reconcile_frame_origins``).
        # Stored here so ``handle_act`` can look up which origin a
        # frame-qualified idx/selector actually reaches WITHOUT re-deriving it
        # from a live extension round trip -- the same reasoning
        # ``resolve_index_meta``'s ``expect`` already relies on. Unlike
        # ``_index_maps``/``_element_meta``/``_node_maps``, this is NOT
        # replaced in lockstep with those on every ``page.act`` -- an act's
        # own after-walk is a single-frame (top-document-only) diff and never
        # recomputes frame origins, so overwriting this on every act would
        # erase what the last ``dom.snapshot`` established. It is replaced
        # only when a caller actually HAS a fresh value to give it (see
        # ``set_index_map``'s ``frame_origins`` param), and dropped alongside
        # everything else on release/reconcile below.
        self._frame_origins: Dict[Tuple[str, int], Dict[str, str]] = {}
        # B4 idx-reuse fix: per-(device_id, tab_id) MONOTONIC high-water mark
        # for idx already issued on this tab's CURRENT document — separate
        # from `_index_maps` because that dict only reflects the CURRENT
        # LIVE map, and a live map shrinks whenever an indexed element leaves
        # the DOM (a same-page re-render's fresh `indexMap` simply omits an
        # idx nothing matched during that walk; `set_index_map`'s own
        # wholesale-replace rule doesn't carry the old entry forward). Before
        # this field existed, `index_map_max_idx()` (still `max(live map)`,
        # kept for callers that genuinely want the live count) was ALSO used
        # to mint the next fresh idx — so removing the element that happened
        # to hold the highest idx let the very next snapshot/find/inspect
        # reissue that same number to a completely different element, the
        # exact class of stale-idx bug B4's "idx are never reused on a tab
        # until navigation" contract exists to prevent. `high_water_idx()`
        # below is what `start_index`/`next_idx` are computed from now: it
        # only ever goes up (via `_bump_high_water`, called from
        # `set_index_map`/`merge_index_map` — i.e. every path that mints or
        # accepts a fresh idx from a `dom.snapshot`/`page.act`/`dom.find`/
        # `dom.inspect` wire result) and is reset to 0 ONLY on an actual
        # navigation (detected the same way `index_map_meta`'s own
        # staleness check already does — this tab's stored `url` changing
        # between one `set_index_map` call and the next) or when the tab's
        # lease/bookkeeping is released outright (`release`/
        # `release_all_for_device`/`release_all_for_holder`/`reconcile`'s
        # stale-tab cleanup) — never on an ordinary same-page re-snapshot,
        # which must keep climbing so a re-rendered element never gets
        # yesterday's now-vacant number.
        self._high_water: Dict[Tuple[str, int], int] = {}

    def attach(self, device_id: str, tab_id: int, holder: str, tab_ref: Optional[dict] = None,
               ttl: float = LEASE_TTL_SECONDS) -> None:
        """Take (or renew) the driving lease for ``tab_id``.

        Raises ``AttachConflict`` when a different, still-live holder has it.
        Calling this again with the same holder is exactly how a lease is
        renewed — there is no separate renew entry point.
        """
        now = time.time()
        with self._lock:
            current = self._leases.get(tab_id)
            if current is not None and current.holder != holder and current.expires_at > now:
                raise AttachConflict(tab_id, current.holder)
            self._leases[tab_id] = _Lease(tab_id, device_id, holder, now + ttl, tab_ref)
            self._device_tabs.setdefault(device_id, set()).add(tab_id)

    def release(self, device_id: str, tab_id: int, holder: Optional[str] = None) -> bool:
        """Drop the lease for ``tab_id``. Returns False if nothing was held.

        Raises ``AttachConflict`` if ``holder`` is given, doesn't match the
        current live holder, and the lease hasn't expired — a session may
        only release its own tabs (the kill switch bypasses this by passing
        ``holder=None``).
        """
        now = time.time()
        with self._lock:
            current = self._leases.get(tab_id)
            if current is None:
                self._device_tabs.get(device_id, set()).discard(tab_id)
                return False
            if holder is not None and current.holder != holder and current.expires_at > now:
                raise AttachConflict(tab_id, current.holder)
            del self._leases[tab_id]
            self._device_tabs.get(device_id, set()).discard(tab_id)
            self._index_maps.pop((device_id, tab_id), None)
            self._index_map_meta.pop((device_id, tab_id), None)
            self._element_meta.pop((device_id, tab_id), None)
            self._node_maps.pop((device_id, tab_id), None)
            self._frame_origins.pop((device_id, tab_id), None)
            self._high_water.pop((device_id, tab_id), None)
            return True

    def release_all_for_device(self, device_id: str) -> List[int]:
        """Force-release EVERY lease on ``device_id``, regardless of holder —
        the same "kill switch" bypass ``release()``'s ``holder=None`` gives a
        single tab, widened to every tab this device has leased. Used when a
        device is revoked (``cli._cmd_revoke``): a revoked device_id can
        never call ``_fresh_tabs()``/``reconcile()`` again to self-heal its
        own stale bookkeeping, and an UNLIMITED lease never expires on its
        own either, so without this a revoked device's tab_id would stay
        permanently un-attachable by anyone (even the same physical browser,
        re-paired under a fresh device_id) until the gateway process
        restarts. A finite lease self-heals within its own TTL regardless —
        this exists specifically so an unlimited one does too, the moment the
        device that held it is banned. Returns the tab ids actually
        released."""
        released: List[int] = []
        with self._lock:
            for tab_id in list(self._device_tabs.get(device_id, set())):
                if tab_id in self._leases:
                    del self._leases[tab_id]
                self._device_tabs.get(device_id, set()).discard(tab_id)
                self._index_maps.pop((device_id, tab_id), None)
                self._index_map_meta.pop((device_id, tab_id), None)
                self._element_meta.pop((device_id, tab_id), None)
                self._node_maps.pop((device_id, tab_id), None)
                self._frame_origins.pop((device_id, tab_id), None)
                self._high_water.pop((device_id, tab_id), None)
                released.append(tab_id)
            self._device_tabs.pop(device_id, None)
        return released

    def release_all_for_holder(self, device_id: str, holder: str) -> List[int]:
        """Release every tab this holder (and only this holder) is driving
        on ``device_id``. Used by ``browser_bridge_release`` with no
        ``tab_id`` — "release everything I attached", never someone else's."""
        now = time.time()
        released: List[int] = []
        with self._lock:
            for tab_id in list(self._device_tabs.get(device_id, set())):
                lease = self._leases.get(tab_id)
                if lease is None:
                    continue
                if lease.holder == holder or lease.expires_at <= now:
                    del self._leases[tab_id]
                    self._device_tabs[device_id].discard(tab_id)
                    self._index_maps.pop((device_id, tab_id), None)
                    self._index_map_meta.pop((device_id, tab_id), None)
                    self._element_meta.pop((device_id, tab_id), None)
                    self._node_maps.pop((device_id, tab_id), None)
                    self._frame_origins.pop((device_id, tab_id), None)
                    self._high_water.pop((device_id, tab_id), None)
                    released.append(tab_id)
        return released

    def holder_of(self, tab_id: int) -> Optional[str]:
        """The live holder of ``tab_id``'s lease, or None if unheld/expired."""
        with self._lock:
            lease = self._leases.get(tab_id)
            if lease is None or lease.expires_at <= time.time():
                return None
            return lease.holder

    def attached_tab_ids(self, device_id: str) -> List[int]:
        with self._lock:
            return sorted(self._device_tabs.get(device_id, set()))

    def reconcile(self, device_id: str, extension_attached_tab_ids: Set[int]) -> int:
        """Drop leases/bookkeeping for tabs the extension no longer reports
        as attached for this device. The extension is the source of truth
        for what's actually attached (chrome.debugger sessions live there,
        not here) — call this with a fresh ``tabs.list`` result before
        trusting our own view, especially right after a reconnect. Returns
        how many stale entries were dropped.
        """
        with self._lock:
            known = self._device_tabs.get(device_id, set())
            stale = known - extension_attached_tab_ids
            for tab_id in stale:
                self._leases.pop(tab_id, None)
                self._index_maps.pop((device_id, tab_id), None)
                self._index_map_meta.pop((device_id, tab_id), None)
                self._element_meta.pop((device_id, tab_id), None)
                self._node_maps.pop((device_id, tab_id), None)
                self._frame_origins.pop((device_id, tab_id), None)
                self._high_water.pop((device_id, tab_id), None)
            if stale:
                self._device_tabs[device_id] = known - stale
            return len(stale)

    # -- index map (idx -> CSS selector), for M2's act(idx) -------------------

    def set_index_map(
        self,
        device_id: str,
        tab_id: int,
        index_map: Dict[int, str],
        url: Optional[str] = None,
        element_meta: Optional[Dict[int, Dict[str, str]]] = None,
        viewport: Optional[Dict[str, Any]] = None,
        node_map: Optional[Dict[int, int]] = None,
        frame_origins: Optional[Dict[str, str]] = None,
    ) -> None:
        """Replace the idx->selector map for this tab wholesale.

        Called once per ``dom.snapshot`` (tools.py's ``handle_snapshot``) AND
        once per successful ``page.act`` that comes back with its own fresh
        ``indexMap`` (tools.py's ``handle_act`` — see the module docstring's
        M2 defect-fix note above). Always a full replace, never a merge — a
        snapshot of a new page (or even the same page re-rendered) invalidates
        every previous index, so yesterday's idx=3 must not silently resolve
        to a selector that no longer means what the model thinks it means.

        ``url`` is the tab's URL at the moment this map was captured (the
        wire result's own ``url``, falling back to the caller's best guess).
        Stored alongside the map purely for ``index_map_meta()``'s
        best-effort staleness check — see that method and
        ``invalidate_index_map()`` for what this does and does not catch.

        ``element_meta`` is ``idx -> {"role": ..., "name": ...}`` from the
        SAME wire result's ``indexMeta`` (the ELEMENT_MISMATCH fix) — stored
        under ``_element_meta`` and replaced in lockstep with ``index_map``
        itself, never independently: a role/name pair is only meaningful
        paired with the selector it was captured alongside. Omitted/``None``
        (an older extension build that doesn't send ``indexMeta`` yet) clears
        this tab's element meta rather than leaving a previous generation's
        entries in place — ``resolve_index_meta()`` then reports "unknown"
        for every idx, and ``handle_act`` falls back to today's URL-only
        check for that idx instead of sending a stale/wrong ``expect``.

        ``viewport`` is ``{"width", "height", "dpr", "scrollX", "scrollY"}``
        from the SAME wire result (``dom.snapshot``/``page.act``'s own
        ``viewport`` field) — the G2.5 fix for the staleness dimension URL
        and role/name checks above cannot catch: a resize or scroll leaves
        both of those unchanged while moving every cached pixel coordinate.
        Stored under ``_index_map_meta`` alongside ``url``/``set_at`` (never
        a fourth dict — one more field on an existing one, same lifecycle:
        replaced wholesale, never merged) so ``index_map_meta()`` hands it
        back to ``handle_act`` as ``expectViewport`` for an ``xy`` act. Absent
        (an older extension build, or a snapshot from before this field
        existed) simply omits the key — ``handle_act`` then sends no
        ``expectViewport`` and act.ts's back-compat path proceeds unchecked,
        exactly like an absent ``expect`` does for selector/idx today.

        ``node_map`` is ``idx -> CDP backendNodeId`` (G2.6.2), from the SAME
        wire result's ``nodeMap`` — resolved by the extension's background
        worker (only it holds the chrome.debugger session; the content
        script cannot see a backendNodeId at all), not by the same walk that
        produced ``index_map``/``element_meta``. Stored under ``_node_maps``
        and replaced wholesale in lockstep with the other three, never
        independently, for the identical reason ``element_meta`` gives: a
        backendNodeId only means anything paired with the selector it was
        resolved from in the SAME generation. An idx legitimately absent from
        ``node_map`` (a shadow-piercing selector, or a resolution failure)
        simply has no handle — ``resolve_index_node`` reports ``None`` for
        it and ``handle_act`` omits ``backendNodeId``, falling back to
        today's selector-only behaviour for that one idx.

        ``frame_origins`` (G2.2.13) is frame-hop selector prefix -> canonical
        origin, from the SAME wire result's ``frameOrigins`` field
        (``dom.snapshot`` only today — ``page.act``'s own after-walk never
        recomputes it, see the field's own declaration comment). UNLIKE the
        other three, passing ``None`` here does NOT clear the existing
        value — it means "this caller has nothing fresh to report", and the
        previous generation's frame origins are left in place so a
        frame-qualified act still has something to authorize against between
        snapshots. Pass an explicit (possibly empty) dict to actually
        replace it.
        """
        with self._lock:
            key = (device_id, tab_id)
            # B4 idx-reuse fix: this is the ONE place that can tell a fresh
            # navigation apart from an ordinary same-page re-snapshot/act —
            # exactly the same URL comparison `index_map_meta()`'s own
            # staleness check already relies on elsewhere. A previously-known
            # url that DIFFERS from this call's url means the tab moved to a
            # new document, so idx numbering may restart from zero; a first
            # generation (no previous url yet) or an unchanged url must NOT
            # reset it, or a same-page re-render could reissue a number that
            # still means something else to the model from an earlier idx in
            # THIS same document.
            prev_meta = self._index_map_meta.get(key)
            prev_url = prev_meta.get("url") if prev_meta else None
            if prev_url is not None and url is not None and url != prev_url:
                self._high_water[key] = 0
            self._index_maps[key] = dict(index_map or {})
            self._index_map_meta[key] = {
                "url": url,
                "set_at": time.time(),
                "viewport": dict(viewport) if viewport else None,
            }
            self._element_meta[key] = dict(element_meta or {})
            self._node_maps[key] = dict(node_map or {})
            if frame_origins is not None:
                self._frame_origins[key] = {str(k): str(v) for k, v in frame_origins.items()}
            self._bump_high_water(key, index_map.keys() if index_map else ())

    def merge_index_map(
        self,
        device_id: str,
        tab_id: int,
        new_index_map: Dict[int, str],
        new_element_meta: Optional[Dict[int, Dict[str, str]]] = None,
    ) -> None:
        """B2's `browser_bridge_inspect` (``scrollables``/``at_point``): ADD
        (or refresh, for an idx that already resolved to the SAME selector)
        entries into this tab's live idx map, WITHOUT touching any entry
        this call didn't itself produce -- the opposite of `set_index_map`'s
        wholesale replace, and deliberately so.

        `inspect` can discover an element a prior `dom.snapshot` never
        indexed at all (its budget-truncated `tree` only bounds what gets
        PRINTED, but a snapshot of a different part of the page, or one
        taken before this element existed, genuinely never walked it) --
        the fresh idx values `content/inspect.ts`'s `IndexAssigner` mints
        for those never reuse a number already live in this tab's map (they
        continue from the gateway-supplied `next_idx`, computed as
        ``max(existing keys) + 1``), so merging them in can never
        overwrite what some OTHER, unrelated element's idx currently means.
        Calling `set_index_map` here instead would invalidate every idx
        `inspect` didn't even look at, the first time a session called
        `scrollables` after any real `dom.snapshot` -- exactly the
        ELEMENT_MISMATCH-class bug `set_index_map`'s own "always a full
        replace" rule exists to prevent for a DIFFERENT reason (a stale
        selector silently matching the wrong element after a re-render).

        No-ops (rather than raising) when this tab has no live map at all
        yet (never snapshotted, or dropped by release/reconcile) --
        `inspect` can still run before any `dom.snapshot` of this tab, and
        its own fresh entries become the map's first generation.
        """
        if not new_index_map:
            return
        with self._lock:
            key = (device_id, tab_id)
            existing_map = self._index_maps.setdefault(key, {})
            existing_map.update(new_index_map)
            if new_element_meta:
                existing_meta = self._element_meta.setdefault(key, {})
                existing_meta.update(new_element_meta)
            if key not in self._index_map_meta:
                self._index_map_meta[key] = {"url": None, "set_at": time.time(), "viewport": None}
            self._bump_high_water(key, new_index_map.keys())

    def _bump_high_water(self, key: Tuple[str, int], idx_values) -> None:
        """Raise ``key``'s high-water mark to at least ``max(idx_values)``.
        Never lowers it. MUST be called with ``self._lock`` already held —
        every call site (``set_index_map``/``merge_index_map``) is itself
        inside the lock, so this only ever mutates, never acquires."""
        try:
            candidate = max(int(i) for i in idx_values)
        except ValueError:
            return
        if candidate > self._high_water.get(key, 0):
            self._high_water[key] = candidate

    def high_water_idx(self, device_id: str, tab_id: int) -> int:
        """speedimprovements.md B4: the highest idx EVER issued on this tab's
        CURRENT document (not merely the current live map's own max — see
        this field's declaration comment in ``__init__`` for why those
        differ once an indexed element is removed from the page), or 0 when
        there is no live generation at all (never snapshotted, or dropped by
        release/reconcile/navigation). ``handle_snapshot``/``handle_find``/
        ``handle_act``/``handle_inspect`` all mint their next fresh idx as
        ``high_water_idx(...) + 1`` — never ``index_map_max_idx(...) + 1`` —
        so a number already handed to the model for a still-live document can
        never be reissued to a different element, even after the element
        that held it is removed. Only a real navigation (or release) resets
        this back to 0; an ordinary same-page re-snapshot never does."""
        with self._lock:
            return self._high_water.get((device_id, tab_id), 0)

    def frame_origins(self, device_id: str, tab_id: int) -> Dict[str, str]:
        """``{frame-hop selector prefix: canonical origin}`` from the most
        recent ``dom.snapshot`` of this tab (G2.2.13) — empty if this tab was
        never snapshotted, or was snapshotted before this field existed.
        ``handle_act`` looks up a frame-qualified target's own prefix here to
        learn which origin to `_authorize` against; a prefix missing from
        this dict is treated exactly like a denied origin — see
        ``tools.py``'s ``_resolve_act_target_origin``."""
        with self._lock:
            return dict(self._frame_origins.get((device_id, tab_id), {}))

    def invalidate_index_map(self, device_id: str, tab_id: int) -> None:
        """Drop this tab's idx->selector map outright, without waiting for
        the next ``dom.snapshot`` to replace it.

        Called by ``handle_act`` when a ``page.act`` result carries no
        ``indexMap`` key at all (the extension's own post-action DOM walk
        failed) — see the module docstring's M2 defect-fix note. An absent
        map must mean "you must re-snapshot before acting on an idx again",
        never "keep using whatever map we had before this action ran": the
        latter is exactly the failure mode (a stale idx that still
        ``querySelector``-matches something after an act-induced re-render,
        resolving to the WRONG element while reporting success) this method
        exists to close off.
        """
        with self._lock:
            self._index_maps.pop((device_id, tab_id), None)
            self._index_map_meta.pop((device_id, tab_id), None)
            self._element_meta.pop((device_id, tab_id), None)
            self._node_maps.pop((device_id, tab_id), None)

    def index_map_meta(self, device_id: str, tab_id: int) -> Optional[Dict[str, Any]]:
        """``{"url": ..., "set_at": ..., "viewport": ...}`` for this tab's
        current index map, or ``None`` if there is no live map at all (never
        captured, or dropped by release/reconcile/``invalidate_index_map``).

        Used by ``handle_act`` for a best-effort "this idx was captured on a
        different page than the tab is on now" refusal — a URL-level check
        only, not full per-element identity verification (see this field's
        declaration above for why that fuller check isn't wired up yet) — and
        for ``viewport``, which it forwards as ``page.act``'s
        ``expectViewport`` whenever the call resolves to an ``xy`` target
        (G2.5).
        """
        with self._lock:
            meta = self._index_map_meta.get((device_id, tab_id))
            return dict(meta) if meta is not None else None

    def resolve_index(self, device_id: str, tab_id: int, idx: int) -> Optional[str]:
        """The CSS selector ``idx`` resolved to as of the most recent
        snapshot of this tab, or None if there is no live map (never
        snapshotted, or the map was dropped on release/reconcile/a newer
        snapshot) or ``idx`` isn't in it. This is the lookup M2's
        ``browser_bridge_act`` will call before sending ``page.act`` with an
        ``idx`` — indices are valid only until the tab's next snapshot.
        """
        with self._lock:
            index_map = self._index_maps.get((device_id, tab_id))
            if not index_map:
                return None
            return index_map.get(idx)

    def resolve_index_meta(self, device_id: str, tab_id: int, idx: int) -> Optional[Dict[str, str]]:
        """``{"role": ..., "name": ...}`` for ``idx`` as of the most recent
        snapshot/act of this tab, or ``None`` when unknown — no live map, the
        idx isn't in it, or the extension build that produced this map never
        sent ``indexMeta`` at all (an older build, not an error).

        This is what ``handle_act`` reads to populate ``page.act``'s
        ``expect`` param (protocol/schema.json) alongside the resolved
        ``selector``: the extension compares it against the LIVE element
        right before acting and refuses with ELEMENT_MISMATCH on a mismatch,
        instead of trusting a selector that still resolves to SOMETHING after
        a same-page re-render — see the module docstring and ``_element_meta``
        above for the class of bug this closes that ``index_map_meta()``'s
        URL-level check alone cannot. Returning ``None`` here is always safe:
        ``handle_act`` simply omits ``expect`` and the extension acts exactly
        as it did before this fix (see act.ts's back-compat path).
        """
        with self._lock:
            meta = self._element_meta.get((device_id, tab_id))
            if not meta or idx not in meta:
                return None
            return dict(meta[idx])

    def resolve_index_node(self, device_id: str, tab_id: int, idx: int) -> Optional[int]:
        """The CDP ``backendNodeId`` for ``idx`` as of the most recent
        snapshot/act of this tab, or ``None`` when there is none — no live
        map, the idx isn't in it, or (the common case for now) the
        extension build that produced this map never resolved one for this
        idx at all (a shadow-piercing selector, an older extension build, or
        a resolution failure — see ``set_index_map``'s docstring). ``None``
        is always safe: ``handle_act`` omits ``backendNodeId`` and act.ts's
        documented fallback resolves ``selector`` exactly as it always has.
        """
        with self._lock:
            node_map = self._node_maps.get((device_id, tab_id))
            if not node_map or idx not in node_map:
                return None
            return node_map[idx]

    def current_index_map(self, device_id: str, tab_id: int) -> Dict[int, str]:
        """The tab's WHOLE current idx->selector map (a copy), or ``{}`` when
        there is no live map at all. B2's `browser_bridge_inspect` sends this
        down on `scrollables`/`at_point` calls (as `existing_index_map`) so
        `content/inspect.ts`'s `IndexAssigner` can recognize an
        already-indexed element by its selector and reuse that idx instead of
        minting a second one for the same node — see `merge_index_map`'s own
        docstring for why a fresh number is only ever assigned for a
        genuinely new element."""
        with self._lock:
            return dict(self._index_maps.get((device_id, tab_id), {}))

    def index_map_size(self, device_id: str, tab_id: int) -> int:
        """How many indices are currently live for this tab — what
        ``browser_bridge_snapshot`` reports back as ``indexed_elements``
        instead of the selectors themselves (selectors are long and the
        model never needs them; the ``[idx]`` markers already in the tree
        text are all it acts on)."""
        with self._lock:
            return len(self._index_maps.get((device_id, tab_id), {}))

    def index_map_max_idx(self, device_id: str, tab_id: int) -> int:
        """The highest idx in this tab's CURRENT LIVE map only, or 0 when
        there is no live map at all (never snapshotted, or dropped). Kept for
        callers that genuinely want the live count (e.g. reporting
        ``indexed_elements``) — but NOT for minting the next fresh idx: a
        live map shrinks whenever an indexed element leaves the DOM, so
        ``max(live map) + 1`` can reissue a number some OTHER, still-relevant
        idx already used earlier in this same document (the B4 idx-reuse
        defect). ``high_water_idx()`` above is what every ``start_index``/
        ``next_idx`` computation uses instead."""
        with self._lock:
            index_map = self._index_maps.get((device_id, tab_id))
            return max(index_map.keys()) if index_map else 0

    def snapshot(self) -> List[Dict[str, Any]]:
        """Every live lease, for `browser_bridge_status` / debugging.

        An UNLIMITED lease's ``expires_at`` is ``float('inf')`` --
        ``int(inf - now)`` raises ``OverflowError``, so that arithmetic is
        skipped entirely for it: ``expires_in_s`` reports ``None`` (never a
        raw ``float('inf')``, which ``json.dumps`` would otherwise render as
        the non-JSON-conformant literal ``Infinity``) alongside an explicit
        ``"lease": "unlimited"`` marker, mirroring `lease_result_fields()`'s
        wire shape for the same case.
        """
        now = time.time()
        with self._lock:
            entries: List[Dict[str, Any]] = []
            for lease in self._leases.values():
                if lease.expires_at <= now:
                    continue
                entry: Dict[str, Any] = {
                    "tab_id": lease.tab_id,
                    "device_id": lease.device_id,
                    "holder": lease.holder,
                    "url": lease.tab_ref.get("url", ""),
                    "title": lease.tab_ref.get("title", ""),
                }
                if lease.expires_at == float("inf"):
                    entry["expires_in_s"] = None
                    entry["lease"] = "unlimited"
                else:
                    entry["expires_in_s"] = max(0, int(lease.expires_at - now))
                entries.append(entry)
            return entries


_registry = AttachRegistry()


def get_registry() -> AttachRegistry:
    return _registry
