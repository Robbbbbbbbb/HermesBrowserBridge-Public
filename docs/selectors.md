# Frame-qualified selector grammar (G2.2.1)

Status: grammar, parser and spec only. No frame injection, routing, aggregation,
or coordinate translation — that is G2.2.2 onward. This document is frozen
before any of those land; changing the string format after they exist means
re-touching every selector already in flight.

## 1. What problem this solves

Before this task, a selector was a single-scope path: a plain CSS selector, or
a `>>>`-joined chain (`content/pierce.ts`) that crosses **shadow** boundaries
only — `hostPath>>>innerPath` means "resolve `hostPath` in the current scope,
then resolve `innerPath` inside that host's open-or-closed shadow root."
Nothing marks a selector as reaching into an **iframe**. Per the G2.2 audit in
`ProjectRules/coveragegaps.md`, an element inside a same-origin iframe today
gets emitted as a plain document-relative path with no frame marker, and gets
resolved against the top document — which can silently match the wrong
element there instead of failing.

This grammar adds the missing dimension: a **frame hop**, symmetric with the
existing shadow hop, that composes with it in either order and at any depth.

## 2. Frame identity: how a frame is named

Two candidate designs: name a frame by Chrome's `frameId`, or by the CSS
selector of the `<iframe>` element that hosts it (searched in the enclosing
scope). **This grammar uses the iframe element's own selector — never
`frameId` — for the string, and treats `frameId` as a separate, ephemeral,
runtime-only mapping.** Both are needed, for different jobs:

- **The selector string encodes structure**: "the frame you reach by finding
  *this* element in *this* scope." It is what the model reads, what gets
  logged, and what a later snapshot re-derives from scratch. It must be
  diagnosable and durable — the same requirement G2.6.1 imposed on ordinary
  element selectors and the reason it built `id → data-testid → name →
  aria-label → role → positional` precedence instead of stopping at
  positional. A frame's own selector is built by the **same** precedence
  chain (`selector-path.ts`'s `selfAttributeCandidate`/positional fallback,
  reused unchanged) applied to the `<iframe>` element itself, because an
  `<iframe>` is just an element with attributes like any other — it has no
  shadow root of its own that would need special handling, and cross-origin
  content does not stop the *parent* document from selecting the iframe
  element by its own attributes (`id`, `data-testid`, `name`, `src`, etc.).
  This is why the same string form works for same-origin and cross-origin
  frames: naming a frame never requires reading *into* it.
- **`frameId` (plus `parentFrameId`, `url`) is runtime metadata**, resolved
  when a selector is actually dispatched and cached per-idx alongside the
  index map, the same way `attach.py`'s `_index_map_meta` already carries
  `{url, set_at, viewport}` for the whole tab (`attach.py:99,292-300`). G2.2.3
  extends that per-idx, not per-tab, and is explicitly **out of scope here**
  — this document only says the shape should be reused, not that it is
  built. Chrome's `frameId` is stable only within one document's life
  (reused across navigations is undefined), so embedding it in a string meant
  to survive being read back later, or hand-typed by a model, would make the
  string unstable in a way redaction and durability already forbid for
  ordinary attribute values (§6). Keeping it out of the grammar also means
  the grammar has zero dependency on CDP session state, `Target.setAutoAttach`
  flat-session support (G0.1.1), or which Chrome version is running — the
  string format is exactly as usable before and after G2.2.6 lands OOPIF
  reach.

**Consequence for idx.** An index-map entry is, and stays, one selector
string per idx (`hermes_plugin/attach.py`'s `_index_maps: Dict[(device,tab),
Dict[int, str]]` shape is unchanged). When the walker (a later task) crosses
a frame boundary while building an idx's selector, the string it emits is
simply longer — it gains a frame hop — not differently shaped. No wire
change, no new field, is needed on the index map itself for the grammar to
reach into frames. The **separate** per-idx frame-identity cache G2.2.3
proposes (frameId/parentFrameId/url, for fast dispatch and staleness
detection) sits next to that string, keyed the same way, and is invalidated
the same way a stale idx already is — it is bookkeeping for speed, not part
of what the selector means.

## 3. Grammar

Two hop kinds, both fixed literal tokens, chosen to start with different
characters so a scanner never has to look ahead to disambiguate them:

| Hop | Token | Meaning |
|---|---|---|
| shadow hop | `>>>` (existing, `pierce.ts`) | resolve the preceding text as a host in the current scope; continue in its open-or-closed shadow root |
| frame hop | `\|>>` (new) | resolve the preceding text as an `<iframe>` (or legacy `<frame>`) element in the current scope; continue inside its content document |

A frame-qualified selector is one or more **frame segments** joined by frame
hops:

```
frame-selector  := frame-segment ("|>>" frame-segment)*
frame-segment   := pierce-selector          ; content/pierce.ts's existing grammar, unchanged
pierce-selector := plain-selector (">>>" plain-selector)*
plain-selector  := any non-empty CSS selector string containing neither
                    "|>>" nor ">>>" as a literal substring OUTSIDE a quoted
                    attribute-value string or a `[...]` attribute bracket
```

Everything to the left of the *last* frame hop names a chain of iframe
elements to walk into, in order, starting from the top document. The final
frame segment is resolved with the **existing, unmodified** pierce resolver
inside whatever document/shadow-tree that walk lands in — this grammar does
not re-implement shadow resolution, it wraps it.

**Splitting is quote/bracket-aware, not a plain `String.split` (G2.6.9).**
Found live on `main` while verifying this task: a durable selector built from
a page's own attribute text (G2.6.1) can carry a literal hop token *inside a
quoted value* — `[data-testid="a>>>b"]` — and a naive split on `>>>` or
`|>>` cuts that string in the wrong place, misrouting to the wrong element or
none. Fixed two ways, deliberately redundant:
1. **The splitter** (`content/pierce.ts`'s `splitTopLevel`, shared by both
   hops — `lib/frame-selector.ts` imports it rather than re-implementing it)
   tracks `"…"`/`'…'` quoting (honouring a `\`-escape, including an escaped
   quote) and `[…]` bracket depth, and only splits on a hop token at the top
   level. An unterminated quote or bracket is rejected with a precise error,
   the same way an empty segment is.
2. **The producer** (`cssEscapeAttrValue` in `content/selector-path.ts`) now
   also escapes `>` and `|` (`\>`, `\|` — both valid CSS string escapes that
   resolve to the same character), so a durable selector this codebase
   *generates* never carries a literal hop token in a value in the first
   place. The splitter above is still needed for selectors from elsewhere —
   a hand-authored `selector` param, or an older extension build.

Because a frame segment is itself an ordinary pierce-selector, everything the
composition list in the task asked for falls out of the two-hop-kind grammar
directly, with no extra cases:

| Case | Example |
|---|---|
| same-origin frame | `#comments-frame\|>>.reply-button` |
| cross-origin frame | `#ads-frame\|>>button[aria-label="Close"]` (naming never differs — see §2) |
| nested frames | `#outer\|>>#middle\|>>#inner` — walk three documents deep |
| frame inside a shadow root | `#widget-host>>>iframe[data-testid="checkout"]\|>>#pay-button` — the shadow hop happens *inside* the segment that names the iframe |
| shadow root inside a frame | `#billing-frame\|>>#widget-host>>>#pay-button` — the frame hop happens first, the shadow hop is in the segment after it |
| targeting the iframe element itself | no trailing hop: `#billing-frame` alone, in a context expecting an element (e.g. screenshot-by-selector clipping to the frame's own box, G2.2.8) — needs no special syntax, it is simply a selector chain that happens to end on an `<iframe>` node |

## 4. Module

`extension/src/lib/frame-selector.ts`. **Placed in `lib/`, not `content/`**,
because both the content script (frame-local resolution, run once per frame
after G2.2.2) and the background worker (deciding which `frameId` to route a
message to, and building the frame-identity cache in §2) need it, and this
codebase's existing precedent for exactly that split is `lib/redaction.ts`
— G2.4.4 moved redaction out of `content/` into `lib/` for the same reason
(console capture runs in the background worker) and left `content/redaction.ts`
as a re-export shim. `frame-selector.ts` needs no shim because nothing
existing imports a module by this name yet.

The module is pure: no DOM access, no `document`, no `chrome.*`. It only
splits and rejoins strings, so it is unit-testable with plain fixtures and
importable from either world without pulling in browser globals.

```ts
export type FrameHopKind = "shadow" | "frame";

export interface FrameSelectorPath {
  /** One or more frame segments, in walk order. Each is itself a valid
   * (or, until resolved, merely well-formed) pierce-selector string —
   * this module does not interpret shadow syntax, `pierce.ts` still does. */
  segments: string[];
}

export type ParseResult =
  | { ok: true; path: FrameSelectorPath }
  | { ok: false; error: string };

export function parseFrameSelector(selector: string): ParseResult;
export function formatFrameSelector(path: FrameSelectorPath): string;
export function isFrameQualified(selector: string): boolean; // segments.length > 1 after a successful parse
```

`formatFrameSelector` is `path.segments.join(FRAME_HOP)`. Because splitting
and rejoining on a fixed literal separator with no regex is an exact inverse
whenever the separator's occurrences are unambiguous, `format(parse(s).path)
=== s` holds for **every** string `parse` accepts — the round-trip property
is a consequence of using `String.split`/`Array.join` on a literal token, not
a separately-maintained invariant that could drift.

### Rejections (precise, never a thrown exception)

`parseFrameSelector` returns `{ ok: false, error }`, mirroring `pierce.ts`'s
existing convention of reporting `invalid` rather than throwing, for:

- the empty string, or a string that is only whitespace — `"empty selector"`
- a leading or trailing frame hop (`"|>>foo"`, `"foo|>>"`) — `"frame selector cannot start or end with '|>>'"`
- two frame hops with nothing between them (`"a|>>|>>b"`) — `"empty frame segment between '|>>' hops"`
- a frame segment that is itself an invalid pierce chain — i.e. it contains
  `>>>` but splitting on it (via `pierce.ts`'s own `splitPierceSelector`)
  yields an empty part, such as a leading/trailing/doubled shadow hop inside
  one frame segment (`"#a|>>>>>#b"`, `"#a|>>#b>>>"`) — the error names which
  frame segment (0-based) failed and reuses `pierce.ts`'s own wording so the
  two layers report faults consistently: `"frame segment 1: empty selector part in \"#b>>>\""`

What it deliberately does **not** validate: CSS syntax within a segment. A
segment like `1invalid[[[` is structurally well-formed (non-empty, no bare
hops) and parses successfully; it only fails later, at resolution time,
inside `pierce.ts`, exactly as a malformed plain selector does today. This
module never calls `querySelector` and cannot tell valid CSS from invalid
CSS — mirroring `resolvePierceSelector`'s own `try/catch` around `queryAll`,
which is the only place in this codebase that currently discovers a bad CSS
string, and it discovers it at resolution time, not parse time.

## 5. Backward compatibility

Every string that does not contain the literal substring `|>>` parses to a
**single-segment** `FrameSelectorPath` whose one segment is the original
string unchanged, and formats back to exactly that string. This covers, with
no special-casing:

- a plain CSS selector (`#submit-button`)
- a `>>>` shadow-pierce chain, at any depth, including the redacted-fallback
  positional forms G2.6.1 produces (`div:nth-of-type(3)>>>button:nth-of-type(1)`)
- every id/test-id/name/aria-label/role-based durable selector G2.6.1 emits —
  **including one whose attribute value itself contains the literal text
  `>>>` or `|>>`** (§3's G2.6.9 fix: `cssEscapeAttrValue` escapes `>`/`|` at
  the producer, and `splitTopLevel`'s quote/bracket tracking means even an
  unescaped occurrence from elsewhere splits correctly rather than
  misrouting). This claim previously (rev 1 of this doc) rested on "a page's
  own attribute value happening to contain a hop token can't arise from this
  codebase's own selector-building path" — that was wrong the moment G2.6.1
  started embedding attribute text verbatim, and is now fixed rather than
  merely asserted; see `tests/content/pierce.test.ts` and
  `tests/content/selector-path.test.ts`'s G2.6.9 cases.

Proof, not assertion: `tests/lib/frame-selector.test.ts` runs a corpus of
real selector strings **extracted at test-run time** from
`selector-path.test.ts`, `shadow-dom.test.ts` and `act.test.ts`
(`tests/lib/selector-corpus.ts` — read from those files' own literal
expectations, not hand-copied, so it can't drift from what those suites
actually assert) — plain, `id`, `data-testid`, `aria-label`, `role`,
positional, and multi-level `>>>` chains — through `parseFrameSelector`,
asserting `segments.length === 1` and `format(parse(s)) === s` for each.

## 6. Redaction interplay

This module never inspects attribute values — it has no DOM access, so it
cannot itself detect page-authored text. The redaction obligation therefore
falls entirely on the **caller** that builds a frame segment naming an
iframe, and the mechanism already exists and is reused unchanged: an
`<iframe>` element's candidate selectors (`id`, `data-testid`/`data-test`/
`data-cy`, `name`, `aria-label`) go through `selector-path.ts`'s
`selfAttributeCandidate`, which already rejects any candidate `wouldRedact`
(the same `redactText`/policy check §0.7 of `coveragegaps.md` requires, and
the exact rule G2.6.1 added after its first build leaked
`aria-label="Remove jane@example.com"` into a selector). A frame hop adds no
new redaction surface and needs no new check: naming the iframe host is
naming an element, and every element-naming code path in this codebase
already goes through that gate. The one new value a frame segment could
introduce — the iframe's `src` URL — is deliberately **not** one of
`selector-path.ts`'s candidate kinds (form-control `name` is scoped to form
controls, not `src`), so it is never offered as a selector candidate in the
first place, and cannot leak page/query-string text via that path.

**A separate, narrower leak was found and fixed alongside this (G2.6.9):**
redaction decides *whether* a value may appear in a selector at all; it does
not stop a permitted value from containing this grammar's own hop tokens.
`cssEscapeAttrValue` now escapes `>` and `|` (§3) so a page's own
`data-testid="a>>>b"` can't make an *unrelated* selector split in the wrong
place — this is a grammar-correctness fix, not a redaction-policy change
(the value itself was already allowed through; only its literal spelling in
the emitted selector changed).

## 7. What this grammar deliberately cannot express

- **Which live Chrome frame a hop resolves to right now.** That is `frameId`
  (§2), resolved at dispatch, not encoded in the string. A selector is a
  recipe for finding a frame, not a pointer to one.
- **A specific navigation of a frame.** If a frame at a given position
  navigates, the same selector still finds *an* `<iframe>` element there, but
  the segment after the hop now addresses whatever document is currently
  loaded — the grammar has no way to pin "this exact page load." Detecting
  that mismatch is G2.2.3's job (reusing the url-in-metadata pattern), not
  this grammar's.
- **CDP session identity for OOPIF-level operations** (`sessionId`,
  `Target.setAutoAttach`). The string is CDP-agnostic by construction (§2) —
  it says nothing about whether reaching the target frame needs a flat
  session, so it carries no obligation forward if G2.2.6 changes how that
  reach works or bumps `minimum_chrome_version`.
- **Disambiguating two frames (or two elements) that are truly
  indistinguishable from the outside** — identical tag, no unique attribute,
  same position. This is the same limitation ordinary positional selectors
  already have (§0's `selfAttributeCandidate` falls through to positional and
  can still collide); a frame hop inherits it rather than solving it.
- **Coordinates, viewport offsets, or scroll position** inside a frame
  (G2.2.5). The grammar names an element; where that element currently sits
  on screen is computed at dispatch time, same as it is today for a
  non-framed element.
- **A frame reached only via `window.frames[n]` with no `<iframe>` element in
  the accessible DOM** (vanishingly rare, and not reachable by
  `document.querySelector` from the parent either) is out of scope; the
  grammar assumes every reachable frame has a locatable host element, which
  is true for every case in the G2.2 fixtures.

## 8. Python / gateway mirror

**Not built, and not needed for this task.** `hermes_plugin/tools.py` does
not parse or validate selector syntax at all today — the only appearance of
`>>>` in that file is inside a tool-schema description string
(`tools.py:785-786`) explaining the syntax to the model in prose; there is no
code path that splits, validates, or branches on `>>>`, and no `frameId`
concept anywhere in the file. Selectors are opaque strings from the gateway's
point of view: it forwards them to the extension, which is where every
existing selector error (`invalid`, `not found`, `ELEMENT_MISMATCH`) is
discovered and reported back over the wire. A frame-qualified selector is the
same kind of opaque string to the gateway, and this task does not change
that — parsing and validation stay extension-side, in
`extension/src/lib/frame-selector.ts`, exactly mirroring where `>>>` handling
already lives. If a later G2.2 task needs the gateway to reason about frame
segments directly (for example, a schema description that must warn the
model about `|>>`), it should add that then, alongside a shared JSON
test-vector file — none is needed yet because there is nothing on the Python
side to keep in sync with.

## 9. Error codes (updated by G2.2.5-8/.10-12 — acting inside frames)

Three codes now cover acting on a frame-qualified target, allocated in
`ProjectRules/coveragegaps.md`'s table:

- **4226 `FRAME_ACTION_UNSUPPORTED`** — narrowed from this task's original
  blanket refusal to its two genuinely-unsupported cases: an ancestor
  `<iframe>`'s `src` maps to no live Chrome `frameId` (or ambiguously to
  more than one), or that frame's own content script cannot be reached at
  all (injection refused, an out-of-process frame this build has no CDP
  session for). `background/frames.ts`'s `resolveFrameTarget` is the one
  place that emits it.
- **4227 `FRAME_TARGET_UNRESOLVED`** — an anchor segment resolves to
  something, but not to an `<iframe>`/`<frame>` element (a stale selector, a
  wrapper `<div>`, or a selector that no longer matches at all). Also
  emitted by `resolveFrameTarget`.
- **4228 `FRAME_ORIGIN_DENIED`** — reserved for an extension-side origin
  check on the acted frame (mirroring the gateway's own G2.2.13 check);
  **not wired on this pass** — see §11 below for why the gateway-side check
  alone was judged sufficient for now, and what would need to change to
  also enforce it here.

## 10. Coordinate translation (G2.2.5) and dispatch (G2.2.6/.7/.8)

`background/frames.ts`'s `resolveFrameTarget(tabId, selector)` walks a
frame-qualified selector's ancestor segments one hop at a time — each
resolved as an `<iframe>`/`<frame>` element WITHIN the frame the previous
hop landed in (`content/targets.ts`'s `resolveFrameAnchor`, a new
content-script message, never by descending through `contentDocument`,
which throws across a genuine cross-origin boundary) — and returns:

- the live Chrome `frameId` owning the selector's FINAL segment (matched by
  the anchor's already-absolute `.src` against
  `chrome.webNavigation.getAllFrames`, reusing `aggregateSnapshot`'s own
  `matchChildFrame` so the two paths cannot disagree about which frame a
  given anchor names);
- the plain local selector to resolve inside that frame;
- the accumulated top-level-viewport offset (each ancestor iframe's
  CONTENT-box top-left corner — border-box rect plus its own
  border+padding — summed root to target) that every rect resolved inside
  that frame must be shifted by before `Input.dispatchMouseEvent` (which
  only understands top-level-viewport coordinates) can use it.

`background/act.ts` threads this through every content-script call a
frame-qualified action needs (`resolveTarget`, `resolveEditableTarget`,
`resolveElement` for `expect`/ELEMENT_MISMATCH, `selectOption`,
`submitTarget`, `readField`, `readFileInput`) via the routed `frameId` on
`chrome.tabs.sendMessage`'s options, and shifts every resulting rect/point
by the offset before dispatching CDP `Input` events — click, hover, type,
select and drag (source and destination independently) all work inside
same-origin AND cross-origin frames this way, with `expect` verified
against the LIVE element in the RIGHT frame and screenshot-by-selector
(page.screenshot's clip) built on the same shifted rect.

**Node handles (G2.6.2) stay out of scope for a frame-qualified target,
deliberately.** A `backendNodeId` is a `chrome.debugger`-session-local
identifier; for a genuinely cross-origin (out-of-process) frame it would
need that frame's OWN CDP session (`Target.setAutoAttach`, Chrome 125+,
G0.1.1), which this build does not open. `resolvePoint` skips both the
gateway-supplied handle and the on-demand single-selector resolution
outright once a frame is resolved, falling back to the (frame-aware)
selector + coordinate-translation path unconditionally — this is why
`minimum_chrome_version` was NOT bumped for this task: plain CDP `Input`
dispatch at TOP-level viewport coordinates already reaches an OOPIF's
rendered pixels (compositor-level hit-testing, independent of which
process rendered what is under those coordinates), so no flat CDP session
is needed for click/hover/type/select/drag at all — only true CDP-level
per-frame operations (a frame-scoped `DOM.getContentQuads`/backendNodeId)
would need it, and none of those are exercised by this task.

## 11. Authorization (G2.2.13) — gateway-side, not (yet) extension-side

An act on a frame-qualified target is authorized against the TARGET
FRAME's own canonical origin, never the top frame's — `hermes_plugin/
tools.py`'s `handle_act` resolves the target selector's frame-hop prefix
against `attach.py`'s `frame_origins()` (populated from the SAME
`frameOrigins` `dom.snapshot` already reports) and calls `_authorize`
against that origin with `require_explicit_grant=True`, which refuses an
origin with no EXPLICIT grants-table row rather than falling back to
`config.py`'s `default_mode` the way an ordinary tab origin does — the
same rule `_reconcile_frame_origins` already applies to snapshot text.

**Extension-side enforcement of the same rule is NOT wired on this pass**
(§9's reserved 4228). The snapshot path (G2.2.13, already shipped) has an
extension-side check because `background/frames.ts` already carries the
device's origin policy on every `dom.snapshot` call; `page.act`'s wire
params carry no such policy today, and adding one would mean extending
`protocol/schema.json`'s `page.act` params, `offscreen.ts`'s unpacking, and
a new set of tests — real work, deliberately deferred rather than done
as a drive-by addition to this task. The gateway check is not defense in
depth here, it is the ONLY check; say so plainly rather than implying
parity with the snapshot path.

## 12. A note for the first integrator (G2.2.2+) — historical

`background/act.ts` already special-cases pierce selectors twice by checking
`selector.includes(">>>")` (`act.ts:1777`, `act.ts:1845`) to skip node-handle
targeting (G2.6.2) for a selector shadow-piercing can't give a stable
`backendNodeId` for. A frame-qualified selector needs the same treatment for
the same reason, and checking `selector.includes("|>>")` would work but
duplicates logic this module already owns — prefer `isFrameQualified` (or a
successful `parseFrameSelector` with `segments.length > 1`) so the two
call sites and this module cannot drift apart on what counts as
frame-qualified. **Done** (G2.2.5/.6): `resolvePoint`'s `inTopFrame` guard
now applies this rule to both the gateway-supplied `backendNodeId` and the
on-demand resolution path.
